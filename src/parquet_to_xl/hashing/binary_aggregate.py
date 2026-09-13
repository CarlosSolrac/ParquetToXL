"""The order-independent xxh3-128 additive hasher and the record it produces."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

import xxhash
from pydantic import Field

from parquet_to_xl.hashing.base import DataFrameHasherBaseClass, HashedDataframeBase
from parquet_to_xl.hashing.canonical import encode_series

if TYPE_CHECKING:
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
