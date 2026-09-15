"""Reading a staged Parquet file and describing it, with the original path attached.

**The trap this module exists to not fall into.** ``extract_metadata_from_dataframe`` stats whatever
path it is handed, and its docstring is explicit that "the pairing is the caller's responsibility"
-- frame and path are never checked against each other. That is exactly what lets the pipeline pass
a frame read from **scratch** together with the **original remote path**, giving ``full_path`` the
real URI and ``modified_utc`` the real source time.

Hand it the scratch path instead and T1 silently becomes the stage-in time. Nothing fails, nothing
warns, and the freshness rule is poisoned permanently while looking perfectly healthy: every
subsequent run compares the source's real mtime against a staging time that will never match, so
the source is stale forever. :func:`build_sidecar` takes both paths as separate required arguments
so that confusing them takes effort, and a test pins it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import polars as pl
from pqx_frame.conversion.to_excel import DataframeConversionToExcel
from pqx_frame.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash
from pqx_frame.metadata.extract import extract_metadata_from_dataframe
from pqx_plan.capacity import SourceShape
from pqx_sidecar.store import get_sidecar_store

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Sequence

    from pqx_frame.metadata.dataframe import DataframeMetadata
    from upath import UPath

__all__ = ["PARQUET_SUFFIX", "IngestError", "build_sidecar", "observe", "read_parquet", "source_stem"]

PARQUET_SUFFIX: str = ".parquet"
"""Stripped to give ``{source_stem}`` its value."""


class IngestError(Exception):
    """A source could not be read or described."""


def source_stem(path: UPath) -> str:
    """Return a source's basename without its final ``.parquet``.

    The ``{source_stem}`` naming token. Only the final extension is removed, so
    ``sales.2025.parquet`` stems to ``sales.2025`` rather than to ``sales``.

    Args:
        path: The source path.

    Returns:
        The stem.
    """
    name: str = path.name
    return name[: -len(PARQUET_SUFFIX)] if name.endswith(PARQUET_SUFFIX) else name


def read_parquet(staged_path: UPath) -> pl.DataFrame:
    """Read one staged Parquet file into memory.

    Eagerly, not lazily. Gate 0d measured the eager read plus sort at about 0.023 GiB per million
    cells and found the streaming engine peaks at 70-80% of it, which is not the order of magnitude
    that would change any decision here -- so the simpler path wins and the cell ceiling is what
    bounds the cost.

    Args:
        staged_path: The **local** copy. Reading goes through the path as a string, so this is not
            a remote read: staging is what makes the file local first.

    Returns:
        The frame.

    Raises:
        IngestError: The file could not be read as Parquet.
    """
    try:
        return pl.read_parquet(str(staged_path))
    except (OSError, pl.exceptions.PolarsError) as error:
        message: str = f"could not read {staged_path} as Parquet: {error}"
        raise IngestError(message) from error


def build_sidecar(
    frame: pl.DataFrame,
    *,
    original_path: UPath,
    created_utc: dt.datetime,
    timezones: Sequence[str] = (),
    store: str = "json",
) -> DataframeMetadata:
    """Describe a staged frame as coming from its original source, and persist the sidecar.

    ``original_path`` rather than the staged copy, and named so it cannot be passed positionally
    by accident. See this module's own docstring for what handing over the scratch path does.

    Args:
        frame: The frame, read from the staged copy.
        original_path: The **source's** location, which supplies ``full_path`` and T1.
        created_utc: T2, the instant this run is describing its sources at.
        timezones: IANA zones to render the modification time in, beside UTC.
        store: Identifier of the sidecar store to write through.

    Returns:
        The metadata recorded.

    Raises:
        IngestError: Extraction failed. ``extract_metadata_from_dataframe`` returns ``None`` rather
            than raising -- it is a per-file boundary for batch jobs, and one unreadable file
            should cost that file's metadata rather than the whole run. Here the run *is* the file,
            so a ``None`` becomes a failure rather than a silently empty description.
    """
    metadata: DataframeMetadata | None = extract_metadata_from_dataframe(
        frame,
        original_path,
        timezones,
        [DataFrameHasherBinaryAggregateHash()],
        [DataframeConversionToExcel()],
    )
    if metadata is None:
        message: str = f"could not describe the frame read for {original_path}; extraction logged the reason and returned nothing"
        raise IngestError(message)
    get_sidecar_store(store).write(metadata, original_path, created_utc=created_utc)
    return metadata


@dataclass(frozen=True, slots=True)
class Observation:
    """What one source turned out to be, once read.

    Attributes:
        shape: The four numbers the planner works over.
        frame: The frame itself, converted for Excel and ready to slice into sheets.
        metadata: The description recorded in the sidecar.
    """

    shape: SourceShape
    frame: pl.DataFrame
    metadata: DataframeMetadata


def observe(alias: str, frame: pl.DataFrame, *, original_path: UPath, metadata: DataframeMetadata) -> Observation:
    """Convert a source's frame for Excel and measure what the planner needs to know about it.

    The conversion happens **here**, before planning, for two reasons. The exported column count
    ``C_s`` is the *converted* frame's width, so measuring the unconverted one would size every
    sheet against a shape that is never written. And the digest recorded per fragment is taken over
    the converted frame, which is already in memory at this point -- so taking it later would mean
    converting twice.

    Args:
        alias: The source alias.
        frame: The frame as read from the staged Parquet.
        original_path: The source's location, for ``{source_stem}``.
        metadata: The description already recorded for this source.

    Returns:
        The observation.
    """
    converted: pl.DataFrame = DataframeConversionToExcel().metadata_of_converted_dataframe(frame, []).converted_dataframe
    return Observation(
        shape=SourceShape(alias=alias, stem=source_stem(original_path), columns=converted.width, rows=converted.height),
        frame=converted,
        metadata=metadata,
    )
