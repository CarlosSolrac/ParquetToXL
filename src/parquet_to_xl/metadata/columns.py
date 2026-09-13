"""Whole-frame metadata: the ordered column records plus the frame-level hashes."""

from __future__ import annotations

from pydantic import BaseModel

from parquet_to_xl.hashing import HashedDataframe
from parquet_to_xl.metadata.column import DataframeColumnMetadata


class ConversionIdentity(BaseModel, frozen=True):
    """Which conversion produced a record, and which version of it.

    A ``DataframeMetadata`` holds one ``DataframeColumnsMetadata`` per conversion in a plain
    list, so without this the only way to find the ToExcel record is to know its position in
    the sequence the caller happened to pass. That is fragile in memory and unusable once
    the record is persisted, where the caller's argument list is long gone -- and the JSON
    sidecar is required to record conversion identifiers and versions.

    The fields mirror the ``ClassVar``s on ``DataframeConversionBaseClass`` and are filled
    from them, so they cannot drift from the conversion they name.
    """

    identifier: str
    version: str
    version_number: int


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

    conversion: ConversionIdentity | None = None
    """The conversion these records describe, or ``None`` for a source frame.

    ``None`` is the source-frame case rather than a missing value: the frame as it was read
    is not the output of any conversion, so there is nothing to name."""
