"""Checking a written export against the manifest recorded while it was being written.

**One ``fast_excel_reader`` call per fragment, not per workbook.** The reader's contract is that
every sheet holds the same columns and the sheets are concatenated -- true for a row-partitioned
single source, false the moment fan-in puts two sources' sheets in one workbook. So each fragment
is read with ``sheet_names=[fragment.sheet_name]``.

That is also strictly better than reading whole workbooks, rather than merely necessary for fan-in.
Restoring a trailing run of all-null rows is only well defined for a single sheet, and with
``Fragment.row_count`` per sheet it is well defined everywhere.

The cost is real and designed around: each call opens the workbook itself, so a twelve-sheet
workbook is parsed twelve times, and the reader's own thread pool no longer helps because it
parallelises sheets *within* a call. Parallelism moves up here, across fragments -- one
``fastexcel`` handle per fragment, which is what ``library-spec.md`` already requires, since a
shared handle raises ``Already borrowed`` across threads.

**No Parquet is opened.** The whole point of the sidecar and the manifest is that the source file
need not be present, or even reachable, for an export to be checked.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

import fastexcel
import polars as pl
from pqx_excel.fast_reader import fast_excel_reader
from pqx_frame.conversion.to_excel import DataframeConversionToExcel
from pqx_frame.hashing.binary_aggregate import BinaryAggregateHashedDataframe, DataFrameHasherBinaryAggregateHash

from pqx_verify.validation import (
    COLUMNS_DIFFER,
    DIGEST_MISMATCH,
    SUPPORTED_HASHER_IDENTIFIER,
    SUPPORTED_HASHER_VERSION,
    UNSUPPORTED_CONVERSION,
    UNSUPPORTED_HASHER,
    VALID,
    WORKBOOK_UNREADABLE,
    Verdict,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pqx_plan.manifest import Fragment, RunManifest, SourceFragments
    from upath import UPath

__all__ = ["ManifestVerdict", "SourceVerdict", "verify_fragment", "verify_manifest", "verify_source"]

ROW_COUNT_MISMATCH_DETAIL: str = "the fragments do not add up to the row count recorded for this source"
"""Checked before the digests, because a dropped fragment gives a clearer error this way."""


@dataclass(frozen=True)
class SourceVerdict:
    """What one source's fragments, taken together, say about the export.

    Attributes:
        source_alias: Whose fragments these are.
        fragments: One verdict per fragment, in manifest order.
        rows_found: The fragments' row counts, summed.
        rows_expected: What the manifest records for the whole source.
        reassembly: The verdict of combining the fragments' digests, or ``None`` when
            ``total_and_disjoint`` is false and the fragments no longer cover the source.
    """

    source_alias: str
    fragments: tuple[Verdict, ...]
    rows_found: int
    rows_expected: int
    reassembly: Verdict | None

    @property
    def rows_match(self) -> bool:
        """Whether the fragments add up to the recorded row count."""
        return self.rows_found == self.rows_expected

    @property
    def valid(self) -> bool:
        """Whether every fragment is valid, the rows add up, and the reassembly holds."""
        return all(verdict.valid for verdict in self.fragments) and self.rows_match and (self.reassembly is None or self.reassembly.valid)


@dataclass(frozen=True)
class ManifestVerdict:
    """What a whole run says about itself.

    Attributes:
        sources: One verdict per source, in manifest order.
    """

    sources: tuple[SourceVerdict, ...]

    @property
    def valid(self) -> bool:
        """Whether every source checks out."""
        return all(source.valid for source in self.sources)

    @property
    def failures(self) -> tuple[Verdict, ...]:
        """Return every fragment verdict that is not valid, for a report to list."""
        return tuple(verdict for source in self.sources for verdict in source.fragments if not verdict.valid)


def _written_header(workbook_path: UPath, sheet_name: str) -> list[str | None]:
    """Return one sheet's header cells exactly as written.

    Read separately, and before the reader is asked for data, because the reader normalises
    headers on the way in: duplicates are suffixed and blanks are named. Comparing the names it
    hands back could not see a header that normalises *into* a name it would have generated
    anyway -- a sheet headed ``n, n`` deduplicates to ``n, n_1`` and would match a record of
    exactly those columns.
    """
    reader: fastexcel.ExcelReader = fastexcel.read_excel(str(workbook_path))
    first: pl.DataFrame = reader.load_sheet(sheet_name, header_row=None, n_rows=1).to_polars()
    return [] if first.height == 0 else [None if cell is None else str(cell) for cell in first.row(0)]


def verify_fragment(destination: UPath, fragment: Fragment, schema: Mapping[str, pl.DataType]) -> Verdict:
    """Read one sheet back and decide whether it holds what the manifest says it should.

    Args:
        destination: The directory every ``DestinationRelativePath`` in the manifest is relative to.
        fragment: The fragment to check.
        schema: The dtypes of the converted frame that was written, from the source's sidecar.

    Returns:
        The verdict. Every judgement about the file is a value rather than an exception, so a run
        of a thousand fragments does not stop at the first bad one.

    Raises:
        FileNotFoundError: The workbook is not there. A path that does not exist is a wiring
            mistake rather than a statement about the data, and reporting it as "invalid" would
            make it hard to find.
    """
    workbook_path: UPath = destination / fragment.workbook
    where: str = f"{fragment.workbook}!{fragment.sheet_name}"
    if fragment.expected.identifier != SUPPORTED_HASHER_IDENTIFIER or fragment.expected.version != SUPPORTED_HASHER_VERSION:
        return Verdict(
            UNSUPPORTED_HASHER,
            f"{where} was digested by {fragment.expected.identifier} v{fragment.expected.version}, and this build recomputes with {SUPPORTED_HASHER_IDENTIFIER} v{SUPPORTED_HASHER_VERSION}",
        )
    if not workbook_path.exists():
        message: str = f"no workbook at {workbook_path}"
        raise FileNotFoundError(message)

    actual: str
    # The boundary covers reading, converting and hashing, not reading alone: a cell can be
    # readable and still unusable -- an Excel date serial of 2958466 parses to a Polars date in
    # year 10000, which only fails when the hasher reads the value out.
    try:
        written: list[str | None] = _written_header(workbook_path, fragment.sheet_name)
        if written != fragment.column_names:
            return Verdict(COLUMNS_DIFFER, f"{where} is headed {written} where the manifest records {fragment.column_names}", expected_digest=fragment.expected.digest_hex)
        back: pl.DataFrame = fast_excel_reader(workbook_path, sheet_names=[fragment.sheet_name], schema=schema, expected_rows=fragment.row_count)
        converted: pl.DataFrame = DataframeConversionToExcel().metadata_of_converted_dataframe(back, []).converted_dataframe
        actual = DataFrameHasherBinaryAggregateHash().hash_dataframe(converted).digest_hex
    except (ValueError, KeyError, pl.exceptions.PolarsError, fastexcel.FastExcelError) as error:
        return Verdict(WORKBOOK_UNREADABLE, f"{where} could not be read as the manifest describes it: {error}", expected_digest=fragment.expected.digest_hex)

    if actual != fragment.expected.digest_hex:
        return Verdict(DIGEST_MISMATCH, f"{where} does not hold the rows the manifest records for it", expected_digest=fragment.expected.digest_hex, actual_digest=actual)
    return Verdict(VALID, f"{where} holds the rows the manifest records for it", expected_digest=fragment.expected.digest_hex, actual_digest=actual)


def verify_source(destination: UPath, source: SourceFragments, schema: Mapping[str, pl.DataType], *, max_workers: int | None = None) -> SourceVerdict:
    """Check every fragment of one source, then check that they add back up to it.

    Two arithmetic checks follow the per-fragment ones, and the order matters. The row-count sum
    runs first because a dropped fragment fails it with a clearer error than a digest difference
    would give: "the fragments do not add up" names the problem, where a digest mismatch on the
    whole source only says that something, somewhere, is different.

    Args:
        destination: The directory the manifest's paths are relative to.
        source: The source's fragments and its whole-source expectations.
        schema: The dtypes of the converted frame that was written.
        max_workers: Threads to read fragments with. Parallelism lives here rather than inside the
            reader because one call now reads one sheet, and a ``fastexcel`` handle raises
            ``Already borrowed`` when shared across threads -- so each fragment gets its own.

    Returns:
        The source's verdict.
    """
    if source.conversion.version != DataframeConversionToExcel.version:
        detail: str = (
            f"source {source.source_alias!r} was written under {source.conversion.identifier} v{source.conversion.version}, "
            f"and this build implements v{DataframeConversionToExcel.version}; its digests describe different rules"
        )
        unsupported: Verdict = Verdict(UNSUPPORTED_CONVERSION, detail)
        return SourceVerdict(source_alias=source.source_alias, fragments=(unsupported,), rows_found=source.expected_row_count, rows_expected=source.expected_row_count, reassembly=None)

    def check(fragment: Fragment) -> Verdict:
        """Read one fragment back. Closes over the destination and the schema, which are constant here."""
        return verify_fragment(destination, fragment, schema)

    pool: ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        verdicts: tuple[Verdict, ...] = tuple(pool.map(check, source.fragments))

    rows_found: int = sum(fragment.row_count for fragment in source.fragments)
    return SourceVerdict(
        source_alias=source.source_alias,
        fragments=verdicts,
        rows_found=rows_found,
        rows_expected=source.expected_row_count,
        reassembly=_reassembly(source),
    )


def _reassembly(source: SourceFragments) -> Verdict | None:
    """Combine the fragments' recorded digests and compare against the whole-source digest.

    Arithmetic only -- it sums what the manifest *records*, not what was read back, because the
    per-fragment checks have already established whether each file matches its record. What this
    adds is that the records themselves account for the whole source: a fragment that was planned
    and never written, or one silently dropped from the manifest, fails here.

    Skipped when ``total_and_disjoint`` is false, because the fragments then no longer cover the
    source and the sum is not expected to reach it.
    """
    if not source.total_and_disjoint:
        return None
    if not source.fragments:
        return Verdict(DIGEST_MISMATCH, f"source {source.source_alias!r} lists no fragments, so nothing can add up to it", expected_digest=source.expected_whole.digest_hex)
    combined: BinaryAggregateHashedDataframe = DataFrameHasherBinaryAggregateHash.combine([fragment.expected for fragment in source.fragments])
    if combined.digest_hex != source.expected_whole.digest_hex or combined.row_digest_hex != source.expected_whole.row_digest_hex:
        return Verdict(
            DIGEST_MISMATCH,
            f"the fragments of source {source.source_alias!r} do not reassemble into the digest recorded for the whole source",
            expected_digest=source.expected_whole.digest_hex,
            actual_digest=combined.digest_hex,
        )
    return Verdict(VALID, f"the fragments of source {source.source_alias!r} reassemble into the digest recorded for it", expected_digest=source.expected_whole.digest_hex, actual_digest=combined.digest_hex)


def verify_manifest(destination: UPath, manifest: RunManifest, schemas: Mapping[str, Mapping[str, pl.DataType]], *, max_workers: int | None = None) -> ManifestVerdict:
    """Check every source a run wrote, against the manifest it wrote alongside them.

    ``schemas`` is supplied rather than discovered. Locating and reading the published sidecars is
    the pipeline's business -- it knows the destination layout, the credentials and what has been
    staged -- and doing it here would put remote path discovery inside the package whose whole job
    is a judgement about bytes already in hand.

    Args:
        destination: The directory every ``DestinationRelativePath`` in the manifest is relative to.
        manifest: The run's manifest.
        schemas: The converted-frame dtypes per source alias, from each source's sidecar.
        max_workers: Threads to read fragments with, per source.

    Returns:
        The run's verdict.

    Raises:
        KeyError: A source in the manifest has no schema in ``schemas``. A missing schema is a
            wiring mistake, not a judgement about the export.
    """
    return ManifestVerdict(sources=tuple(verify_source(destination, source, schemas[source.source_alias], max_workers=max_workers) for source in manifest.sources))
