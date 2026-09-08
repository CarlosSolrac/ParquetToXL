"""The top-level record describing a Parquet file and every conversion of its frame."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from parquet_to_xl.metadata.columns import DataframeColumnsMetadata


class DataframeMetadata(BaseModel, frozen=True):
    """Metadata for one source file: where it came from, when, and what it contains.

    ``source_columns_metadata`` describes the frame as it was read. Each entry in
    ``column_metadata_of_conversions`` describes the same frame after one conversion, so a
    ToExcel entry records the digests a workbook written from this frame should reproduce.
    That pairing is what the headline round-trip test compares.
    """

    file_name: str
    full_path: str

    modified_utc: datetime
    """Timezone-aware, in UTC. Naive values are a bug, not a shorthand."""

    modified_in_timezones: dict[str, datetime]
    """The same instant per requested IANA zone, keyed by the zone names that were asked for."""

    source_columns_metadata: DataframeColumnsMetadata
    column_metadata_of_conversions: list[DataframeColumnsMetadata]
    """One entry per conversion supplied, in the order they were given."""
