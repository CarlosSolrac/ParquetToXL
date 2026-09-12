"""The top-level entry point: describe one file's frame, and never raise doing it."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import structlog

from parquet_to_xl.metadata.builder import build_columns_metadata
from parquet_to_xl.metadata.dataframe import DataframeMetadata

if TYPE_CHECKING:
    from collections.abc import Sequence

    import polars as pl
    from upath import UPath

    from parquet_to_xl.conversion.base import DataframeConversionBaseClass
    from parquet_to_xl.hashing.base import DataFrameHasherBaseClass
    from parquet_to_xl.metadata.columns import DataframeColumnsMetadata

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def extract_metadata_from_dataframe(
    df: pl.DataFrame,
    path: UPath,
    timezones: Sequence[str],
    hashers: Sequence[DataFrameHasherBaseClass],
    conversions: Sequence[DataframeConversionBaseClass],
) -> DataframeMetadata | None:
    """Describe one file's frame, its modification time, and every conversion of it.

    Returns ``None`` rather than raising, on any failure whatsoever. That is the point of
    the function: it is the boundary a batch job calls per file, and one unreadable file
    should cost that file's metadata rather than the whole run. The failure is not silent --
    it is logged with a traceback through ``logger.exception`` -- but it is not the caller's
    problem to catch.

    ``df`` is supplied rather than read from ``path``. The two are not checked against each
    other: ``path`` contributes only the name, the string form, and the modification time,
    so a caller that passes a frame from somewhere else gets metadata that says it came from
    this path. That is deliberate -- the headline test needs to describe a frame read back
    from a workbook as though it came from its Parquet file -- but it does mean the pairing
    is the caller's responsibility.

    ``modified_utc`` is read from the filesystem, so it is the only value here that is not a
    function of ``df``. Each entry of ``modified_in_timezones`` is the same instant as
    ``modified_utc``, rendered in one requested zone; they differ in wall-clock reading and
    offset, never in the moment they name.

    Args:
        df: The frame to describe.
        path: Where the data came from. Read for its name and modification time only.
        timezones: IANA zone names, e.g. ``"America/Chicago"``. Each becomes one key of
            ``modified_in_timezones``. An empty sequence yields an empty mapping.
        hashers: The hashers to digest the source frame and every conversion with.
        conversions: The conversions to model, in order. Each contributes one entry to
            ``column_metadata_of_conversions``, at the same index.

    Returns:
        The record, or ``None`` when anything raised. A ``None`` return always has a
        corresponding logged exception.
    """
    try:
        modified_utc: dt.datetime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
        converted: list[DataframeColumnsMetadata] = [conversion.metadata_of_converted_dataframe(df, hashers).columns_metadata for conversion in conversions]
        return DataframeMetadata(
            file_name=path.name,
            full_path=str(path),
            modified_utc=modified_utc,
            modified_in_timezones={name: modified_utc.astimezone(ZoneInfo(name)) for name in timezones},
            source_columns_metadata=build_columns_metadata(df, hashers),
            column_metadata_of_conversions=converted,
        )
    except Exception:
        # Deliberately broad. Every failure mode here is a reason to skip one file rather
        # than to stop: an unreadable path, an unknown timezone name, a dtype no hasher can
        # encode, a conversion that rejects the frame. Narrowing this would turn each new
        # one into a crash the first time it is met in a batch.
        logger.exception("extract_metadata_failed", path=str(path))
        return None
