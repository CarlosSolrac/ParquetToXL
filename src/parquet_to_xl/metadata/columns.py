"""Whole-frame metadata: the ordered column records plus the frame-level hashes."""

from __future__ import annotations

from pydantic import BaseModel

from parquet_to_xl.hashing import HashedDataframe
from parquet_to_xl.metadata.column import DataframeColumnMetadata


class DataframeColumnsMetadata(BaseModel, frozen=True):
    """The per-column records for one frame, and the digests of the frame as a whole.

    This is what both paths produce: the source frame's metadata and each conversion's
    metadata are the same shape, which is what lets the headline test compare a digest
    recorded from Parquet against one computed from a workbook.
    """

    columns: list[DataframeColumnMetadata]
    """In dataframe column order, not sorted."""

    dataframe_hashes: list[HashedDataframe]
    """One entry per hasher, each scoped to ``"dataframe"``."""
