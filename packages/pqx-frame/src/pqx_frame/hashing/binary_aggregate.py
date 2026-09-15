"""The order-independent xxh3-128 additive hasher and the record it produces."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

import xxhash
from pydantic import Field

from pqx_frame.hashing.base import DataFrameHasherBaseClass, HashedDataframeBase
from pqx_frame.hashing.canonical import encode_series

if TYPE_CHECKING:
    from collections.abc import Sequence

    import polars as pl

MODULUS: int = 1 << 128
"""The additive group the per-value digests are summed in."""

HASH_BATCH_ROWS: int = 4096
"""Maximum rows of working cell hashes held at once; never changes the digest."""

HASH_BATCH_BYTES: int = 4 * 1024 * 1024
HASH_BYTES: int = 16
"""Target cell-hash payload per batch, and the fixed width of an xxh3-128 digest."""


class BinaryAggregateHashedDataframe(HashedDataframeBase, frozen=True):
    """Version 2 sums cell hashes plus hashes of each row's ordered cell hashes."""

    identifier: Literal["binary-aggregate-xxh3-128"] = "binary-aggregate-xxh3-128"
    version: int = 2
    scope: Literal["column", "dataframe"]
    bit_width: Literal[128] = 128
    digest_hex: str = Field(pattern=r"^[0-9a-f]{32}$")
    row_digest_hex: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    """Sum of the logical row-hash column; absent for column and legacy version 1 records."""


class DataFrameHasherBinaryAggregateHash(DataFrameHasherBaseClass):
    """Aggregate cell and row hashes while preserving column order and row relationships.

    Each canonical cell becomes a 16-byte xxh3-128 digest. A row's digest hashes the
    concatenation of those bytes in column order. Column totals and the extra row-hash
    total are summed modulo 2**128 for the dataframe digest. Whole rows can be reordered
    or split across workbooks; moving a cell between rows now changes the row-hash total.

    ``hash_all`` calculates every aggregate in one pass over cells, using bounded batches
    instead of materialising an entire hash dataframe. Column names are not hashed and
    the source dataframe is never mutated. Column digest payloads are unchanged from v1;
    dataframe digests have a new meaning, so all new records carry version 2.

    This detects accidental changes; xxh3 and additive aggregation are not security primitives.
    """

    identifier: ClassVar[str] = "binary-aggregate-xxh3-128"

    def hash_column(self, column: pl.Series) -> BinaryAggregateHashedDataframe:
        """Return the modular sum of the per-value digests of one column.

        Each value is passed through ``encode_series`` and hashed with ``xxh3_128``; the
        integer digests are summed modulo 2**128. An empty column digests to zero, which
        renders as 32 zero characters rather than as an error.

        Args:
            column: The column to digest.

        Returns:
            The record, with ``scope="column"`` and ``digest_hex`` the 32-character
            lowercase hex of the sum, zero-padded.
        """
        total: int = 0
        encoded: bytes
        for encoded in encode_series(column):
            total = (total + xxhash.xxh3_128(encoded).intdigest()) % MODULUS
        return BinaryAggregateHashedDataframe(scope="column", digest_hex=f"{total:032x}")

    def hash_dataframe(self, df: pl.DataFrame) -> BinaryAggregateHashedDataframe:
        """Return the modular sum of the original columns and the extra row-hash column.

        Args:
            df: The frame to digest.

        Returns:
            The record, with ``scope="dataframe"``.
        """
        return self.hash_all(df)[1]

    def hash_all(self, df: pl.DataFrame) -> tuple[tuple[BinaryAggregateHashedDataframe, ...], BinaryAggregateHashedDataframe]:
        """Hash each cell once and return all column and dataframe aggregates.

        Fixed-width digest bytes make row concatenation unambiguous without separators.
        Row buffers are filled a column at a time, in their original positions. Summation
        is deferred to each batch boundary to reduce modulo operations. Memory is bounded
        by the batch payload target (or one exceptionally wide row) plus buffer overhead.

        Args:
            df: The frame to hash. Row order may vary; column order must be stable.

        Returns:
            Ordered column totals and the dataframe total, which also records the
            aggregate of the logical extra row-hash column in ``row_digest_hex``.
        """
        totals: list[int] = [0] * df.width
        row_total: int = 0
        batch_rows: int = max(1, min(HASH_BATCH_ROWS, HASH_BATCH_BYTES // max(1, df.width * HASH_BYTES)))
        offset: int
        for offset in range(0, df.height, batch_rows):
            batch: pl.DataFrame = df.slice(offset, batch_rows)
            rows: list[bytearray] = [bytearray() for _ in range(batch.height)]
            index: int
            name: str
            for index, name in enumerate(batch.columns):
                row: bytearray
                encoded: bytes
                for row, encoded in zip(rows, encode_series(batch[name]), strict=True):
                    cell: bytes = xxhash.xxh3_128_digest(encoded)
                    row.extend(cell)
                    totals[index] += int.from_bytes(cell, "big")
                totals[index] %= MODULUS
            row_total = (row_total + sum(xxhash.xxh3_128_intdigest(row) for row in rows)) % MODULUS
        columns: tuple[BinaryAggregateHashedDataframe, ...] = tuple(BinaryAggregateHashedDataframe(scope="column", digest_hex=f"{total:032x}") for total in totals)
        combined: int = (sum(totals) + row_total) % MODULUS
        return columns, BinaryAggregateHashedDataframe(scope="dataframe", digest_hex=f"{combined:032x}", row_digest_hex=f"{row_total:032x}")

    @classmethod
    def combine(cls, records: Sequence[BinaryAggregateHashedDataframe]) -> BinaryAggregateHashedDataframe:
        """Return the digest of the whole from the digests of its disjoint parts.

        The property gate 0b measured and this method turns into a call: because every aggregate
        here is a modular sum over cells and rows, hashing a frame is the same as hashing its
        pieces and adding the results. Verification can therefore read back N fragments, digest
        each, and check the source as a whole -- **without reassembling the frame in memory**, and
        without opening the Parquet the sidecar exists to avoid re-reading.

        Only sound for parts that are **total and disjoint**: every row in exactly one fragment.
        That is a property of the plan, not of these records, so the caller is what must establish
        it -- ``SourceFragments.total_and_disjoint`` is where the manifest records it, and the
        comparison is skipped when it is false.

        Args:
            records: The parts' records. Must agree on ``identifier``, ``version`` and ``scope``,
                because an aggregate produced by a different hasher, a different version of this
                one, or at a different scope is not a term in the same sum. Version skew is a live
                failure mode here, which is why it is a refusal rather than a mismatched digest
                somewhere downstream.

        Returns:
            One record of the same identifier, version and scope, holding the modular sums.
            ``row_digest_hex`` is present when every part carries one, and ``None`` when none does
            -- which is what column-scope records look like.

        Raises:
            ValueError: ``records`` is empty, the parts disagree on identifier, version or scope,
                or some carry a row digest and others do not.
        """
        if not records:
            empty_message: str = "combine needs at least one record; the digest of nothing is not the identity of a hasher whose parameters are unknown"
            raise ValueError(empty_message)
        first: BinaryAggregateHashedDataframe = records[0]
        record: BinaryAggregateHashedDataframe
        for record in records[1:]:
            if (record.identifier, record.version, record.scope) != (first.identifier, first.version, first.scope):
                skew_message: str = (
                    f"cannot combine {record.identifier!r} v{record.version} {record.scope!r} with {first.identifier!r} v{first.version} {first.scope!r}; "
                    f"aggregates from different hashers, versions or scopes are not terms in one sum"
                )
                raise ValueError(skew_message)
        with_rows: int = sum(record.row_digest_hex is not None for record in records)
        if with_rows not in {0, len(records)}:
            partial_message: str = f"{with_rows} of {len(records)} records carry a row digest; a partial sum would silently describe fewer rows than it claims"
            raise ValueError(partial_message)
        total: int = sum(int(record.digest_hex, 16) for record in records) % MODULUS
        row_total: str | None = f"{sum(int(record.row_digest_hex or '0', 16) for record in records) % MODULUS:032x}" if with_rows else None
        return BinaryAggregateHashedDataframe(scope=first.scope, digest_hex=f"{total:032x}", row_digest_hex=row_total)
