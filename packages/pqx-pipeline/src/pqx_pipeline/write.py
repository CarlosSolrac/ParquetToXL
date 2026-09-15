"""Turning a plan into workbooks on disk and the manifest that describes them.

The digest for each fragment is **computed at write time**, while the converted frame is still in
memory. Three options were live and this is the chosen one: recomputing from the source at verify
time reopens the file that verification exists to have unloaded, and storing nothing and checking
only the total cannot localize a mismatch to a workbook and a sheet.

Rows reach their sheets by **slicing the sorted frame in plan order**. The plan says how many rows
each sheet holds; the sort order says which. Nothing here re-derives a bucket: the counts the
planner worked from and the slices taken here come from one ordering, so a disagreement between
them is impossible rather than merely unlikely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pqx_excel.workbook import write_workbook
from pqx_frame.conversion.to_excel import DataframeConversionToExcel
from pqx_frame.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash
from pqx_frame.metadata.columns import ConversionIdentity
from pqx_plan.manifest import Fragment, SourceFragments

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import polars as pl
    from pqx_plan.naming import NamedSheet, NamedWorkbook
    from upath import UPath

    from pqx_pipeline.ingest import Observation

__all__ = ["WrittenWorkbook", "conversion_identity", "write_profile"]


@dataclass(frozen=True, slots=True)
class WrittenWorkbook:
    """One workbook on disk, and what it cost.

    Attributes:
        filename: The basename, as published.
        path: Where it was written, in scratch.
        sheets: How many worksheets it holds.
        rows: Data rows across those sheets.
        cells: Cells across those sheets, header rows included.
    """

    filename: str
    path: UPath
    sheets: int
    rows: int
    cells: int


def conversion_identity() -> ConversionIdentity:
    """Return this build's ToExcel identity, as the manifest pins it.

    Pinned so a later build that changed the conversion is a version mismatch rather than a digest
    mismatch, which names the wrong problem.
    """
    return ConversionIdentity(
        identifier=DataframeConversionToExcel.identifier,
        version=DataframeConversionToExcel.version,
        version_number=DataframeConversionToExcel.version_number,
    )


def _slices(observations: Mapping[str, Observation], workbooks: Sequence[NamedWorkbook]) -> dict[tuple[str, str], pl.DataFrame]:
    """Cut each source's frame into the sheets the plan allocated, in plan order.

    Keyed by ``(workbook filename, sheet name)``, which the naming layer already guarantees unique
    -- filenames are unique within the profile and sheet names within a workbook -- so this reuses
    a property the plan enforces rather than inventing a second one.

    The cursor per source is what makes the cut total and disjoint: every sheet takes the next rows
    the plan asked for, and nothing is read twice or skipped. That is the property ``combine``
    relies on, and it holds by construction rather than by a check afterwards.
    """
    taken: dict[str, int] = dict.fromkeys(observations, 0)
    pieces: dict[tuple[str, str], pl.DataFrame] = {}
    workbook: NamedWorkbook
    named: NamedSheet
    for workbook in workbooks:
        for named in workbook.sheets:
            alias: str = named.sheet.source_alias
            start: int = taken[alias]
            pieces[workbook.filename, named.name] = observations[alias].frame.slice(start, named.sheet.rows)
            taken[alias] = start + named.sheet.rows
    return pieces


def write_profile(
    workbooks: Sequence[NamedWorkbook],
    observations: Mapping[str, Observation],
    scratch: UPath,
    *,
    sidecar_names: Mapping[str, str],
) -> tuple[tuple[WrittenWorkbook, ...], tuple[SourceFragments, ...]]:
    """Write every workbook the plan named, and return them with the manifest's source records.

    Args:
        workbooks: The named, allocated workbooks, in output order.
        observations: Each source's converted frame and shape, keyed by alias.
        scratch: Where the workbooks are written. Local, because the writers hand ``str(path)`` to
            a library that opens it as a local filename -- and because verification then reads from
            scratch rather than over SMB or Azure.
        sidecar_names: Each source's destination-relative sidecar name, keyed by alias.

    Returns:
        The workbooks written, and one ``SourceFragments`` per source, in the order the
        observations name them.

    Raises:
        KeyError: A named sheet refers to a source with no observation, or a source has no sidecar
            name. Both are wiring mistakes rather than statements about the data.
    """
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    pieces: dict[tuple[str, str], pl.DataFrame] = _slices(observations, workbooks)
    fragments: dict[str, list[Fragment]] = {alias: [] for alias in observations}

    written: list[WrittenWorkbook] = []
    workbook: NamedWorkbook
    named: NamedSheet
    for workbook in workbooks:
        sheets: list[tuple[str, pl.DataFrame]] = []
        for named in workbook.sheets:
            frame: pl.DataFrame = pieces[workbook.filename, named.name]
            sheets.append((named.name, frame))
            fragments[named.sheet.source_alias].append(
                Fragment(
                    workbook=workbook.filename,
                    sheet_name=named.name,
                    source_alias=named.sheet.source_alias,
                    period_label=named.period_label,
                    part_index=named.sheet.part_index,
                    row_count=frame.height,
                    column_names=list(frame.columns),
                    # Taken here, while the converted frame is still in memory, so verification
                    # needs only the manifest, the sidecar and the workbooks.
                    expected=hasher.hash_dataframe(frame),
                ),
            )
        write_workbook(sheets, scratch / workbook.filename)
        written.append(
            WrittenWorkbook(
                filename=workbook.filename,
                path=scratch / workbook.filename,
                sheets=len(sheets),
                rows=sum(frame.height for _, frame in sheets),
                cells=sum(frame.width * (frame.height + 1) for _, frame in sheets),
            ),
        )

    identity: ConversionIdentity = conversion_identity()
    return tuple(written), tuple(
        SourceFragments(
            source_alias=alias,
            source_path=str(observations[alias].metadata.full_path),
            sidecar_path=sidecar_names[alias],
            conversion=identity,
            expected_whole=hasher.hash_dataframe(observations[alias].frame),
            expected_row_count=observations[alias].shape.rows,
            fragments=fragments[alias],
        )
        for alias in observations
    )
