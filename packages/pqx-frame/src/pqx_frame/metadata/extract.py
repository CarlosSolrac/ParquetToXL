"""The top-level entry point: describe one file's frame, and never raise doing it."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import structlog

from pqx_frame.metadata.builder import build_columns_metadata
from pqx_frame.metadata.dataframe import DataframeMetadata

if TYPE_CHECKING:
    from collections.abc import Sequence

    import polars as pl
    from upath import UPath

    from pqx_frame.conversion.base import ConvertedDataframe, DataframeConversionBaseClass
    from pqx_frame.hashing.base import DataFrameHasherBaseClass

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DescribedDataframe:
    """A description, together with the converted frames it was taken over.

    ``extract_metadata_from_dataframe`` builds every conversion's frame in order to describe it and
    then drops the frames, which costs a caller that wants one a second pass over the whole thing.
    This carries both halves so a caller can keep what was already built.

    Attributes:
        metadata: The record, exactly as ``extract_metadata_from_dataframe`` returns it.
        converted: One frame per requested conversion, at the same index as ``conversions``.
    """

    metadata: DataframeMetadata
    converted: tuple[pl.DataFrame, ...]


def describe_dataframe(
    df: pl.DataFrame,
    path: UPath,
    timezones: Sequence[str],
    hashers: Sequence[DataFrameHasherBaseClass],
    conversions: Sequence[DataframeConversionBaseClass],
) -> DescribedDataframe | None:
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
        The record and every conversion's frame, or ``None`` when anything raised. A ``None``
        return always has a corresponding logged exception.
    """
    try:
        modified_utc: dt.datetime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
        results: list[ConvertedDataframe] = [conversion.metadata_of_converted_dataframe(df, hashers) for conversion in conversions]
        metadata: DataframeMetadata = DataframeMetadata(
            file_name=path.name,
            full_path=str(path),
            modified_utc=modified_utc,
            modified_in_timezones={name: modified_utc.astimezone(ZoneInfo(name)) for name in timezones},
            source_columns_metadata=build_columns_metadata(df, hashers),
            column_metadata_of_conversions=[result.columns_metadata for result in results],
        )
        return DescribedDataframe(metadata=metadata, converted=tuple(result.converted_dataframe for result in results))
    except Exception:
        # Deliberately broad. Every failure mode here is a reason to skip one file rather
        # than to stop: an unreadable path, an unknown timezone name, a dtype no hasher can
        # encode, a conversion that rejects the frame. Narrowing this would turn each new
        # one into a crash the first time it is met in a batch.
        logger.exception("extract_metadata_failed", path=str(path))
        return None


def extract_metadata_from_dataframe(
    df: pl.DataFrame,
    path: UPath,
    timezones: Sequence[str],
    hashers: Sequence[DataFrameHasherBaseClass],
    conversions: Sequence[DataframeConversionBaseClass],
) -> DataframeMetadata | None:
    """Describe one file's frame, discarding the converted frames.

    :func:`describe_dataframe` without the frames. A caller that only wants the record pays the
    same conversions either way; one that wants a frame back should call that instead rather than
    converting a second time.

    ⚠️ **No caller in this workspace uses it any more.** ``pqx-pipeline`` moved to
    :func:`describe_dataframe` so it could keep the conversion it had already paid for. This stays
    because ``pqx-frame`` is a library and this is its documented entry point: a caller wanting a
    description and nothing else should not have to unpack a frame in order to drop it. The tests
    against it cover both functions, the other being a thin wrapper over the same work.

    Args:
        df: The frame to describe.
        path: Where the data came from. Read for its name and modification time only.
        timezones: IANA zone names, each becoming one key of ``modified_in_timezones``.
        hashers: The hashers to digest the source frame and every conversion with.
        conversions: The conversions to model, in order.

    Returns:
        The record, or ``None`` when anything raised.
    """
    described: DescribedDataframe | None = describe_dataframe(df, path, timezones, hashers, conversions)
    return None if described is None else described.metadata
