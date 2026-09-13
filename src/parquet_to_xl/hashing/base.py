"""The hasher interface and the base model every hash result extends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    import polars as pl

    from parquet_to_xl.hashing import HashedDataframe


class HashedDataframeBase(BaseModel, frozen=True):
    """Fields every hash result carries, whichever hasher produced it.

    ``identifier`` is the discriminator: each concrete subclass narrows it to its own
    ``Literal``, which is how ``HashedDataframe`` selects the right subclass when a record
    is revalidated from a plain mapping. ``version`` lets one hasher's output format change
    without needing a new identifier.
    """

    identifier: str
    version: int


class DataFrameHasherBaseClass(ABC):
    """A content hasher for Polars columns and frames.

    Both methods are exposed on the base class deliberately. A caller that only needs a
    whole-frame digest should not have to know that it happens to be derivable from the
    column digests, and a hasher for which that identity does not hold is still free to
    implement the two independently.
    """

    def hash_all(self, df: pl.DataFrame) -> tuple[tuple[HashedDataframe, ...], HashedDataframe]:
        """Return column digests in dataframe order and the whole-frame digest.

        Hashers may override this to reuse cell encodings across both aggregates.
        The default preserves compatibility with implementations of the two original methods.

        Args:
            df: The frame to digest, without mutation.

        Returns:
            The ordered column records and the dataframe record.
        """
        return tuple(self.hash_column(df[name]) for name in df.columns), self.hash_dataframe(df)

    @abstractmethod
    def hash_column(self, column: pl.Series) -> HashedDataframe:
        """Return the digest of one column's values.

        Args:
            column: The column to digest.

        Returns:
            A hash record with ``scope`` set to ``"column"``.
        """

    @abstractmethod
    def hash_dataframe(self, df: pl.DataFrame) -> HashedDataframe:
        """Return the digest of every value in the frame.

        Args:
            df: The frame to digest.

        Returns:
            A hash record with ``scope`` set to ``"dataframe"``.
        """
