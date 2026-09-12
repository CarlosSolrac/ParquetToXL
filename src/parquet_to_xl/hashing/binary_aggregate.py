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


class BinaryAggregateHashedDataframe(HashedDataframeBase, frozen=True):
    """A digest produced by summing per-value xxh3-128 hashes modulo 2**128."""

    identifier: Literal["binary-aggregate-xxh3-128"] = "binary-aggregate-xxh3-128"
    version: int = 1
    scope: Literal["column", "dataframe"]
    bit_width: Literal[128] = 128
    digest_hex: str = Field(pattern=r"^[0-9a-f]{32}$")


class DataFrameHasherBinaryAggregateHash(DataFrameHasherBaseClass):
    """Hashes by summing the xxh3-128 of each encoded value, modulo 2**128.

    Addition is commutative, which is the whole point: the digest does not depend on the
    order rows arrive in. That is what lets the headline test split a frame across two
    workbooks, read them back in the wrong order, concatenate, and still match. It also
    makes the whole-frame digest the modular sum of the column digests, since both are
    sums over the same multiset of encoded values.

    The tradeoff is deliberate and worth stating: an additive digest is not
    collision-resistant against an adversary, who can trivially construct two multisets
    with the same sum. It detects accidental corruption and round-trip damage, which is
    what this library is for. It is not a security primitive.
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
        """Return the modular sum of the per-value digests of every column.

        Equivalently, and asserted by a test, the modular sum of this frame's column
        digests: both sum the same values in the same group.

        Args:
            df: The frame to digest.

        Returns:
            The record, with ``scope="dataframe"``.
        """
        total: int = 0
        name: str
        for name in df.columns:
            encoded: bytes
            for encoded in encode_series(df[name]):
                total = (total + xxhash.xxh3_128(encoded).intdigest()) % MODULUS
        return BinaryAggregateHashedDataframe(scope="dataframe", digest_hex=f"{total:032x}")
