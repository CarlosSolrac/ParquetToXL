"""Frozen tests for checking a written export against its manifest.

Real workbooks, written by the same multi-sheet writer the pipeline uses, then read back. A test
that built the workbook another way would be checking a file the exporter never produces.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import fastexcel
import polars as pl
import pytest
from pqx_common.paths import ZPath
from pqx_excel.workbook import write_workbook
from pqx_frame.conversion.to_excel import DataframeConversionToExcel
from pqx_frame.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash
from pqx_frame.metadata.columns import ConversionIdentity
from pqx_plan.config import ExportConfig
from pqx_plan.corpus import BASE
from pqx_plan.manifest import Fragment, RunManifest, SourceFragments
from pqx_verify.fragments import ManifestVerdict, SourceVerdict, verify_fragment, verify_manifest, verify_source
from pqx_verify.validation import Verdict

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from upath import UPath

HASHER: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
CONVERSION: DataframeConversionToExcel = DataframeConversionToExcel()


def _source_frame(rows: int, *, offset: int = 0) -> pl.DataFrame:
    """A frame of the dtypes an export actually carries."""
    return pl.DataFrame(
        {
            "id": pl.Series([offset + index for index in range(rows)], dtype=pl.Int64),
            "name": pl.Series([f"row {offset + index}" for index in range(rows)], dtype=pl.String),
            "amount": pl.Series([(offset + index) / 4 for index in range(rows)], dtype=pl.Float64),
            "booked": pl.Series([dt.date(2025, 1, 1) + dt.timedelta(days=offset + index) for index in range(rows)], dtype=pl.Date),
        },
    )


def _converted(frame: pl.DataFrame) -> pl.DataFrame:
    """The frame as it is written, which is what both sides of the digest describe."""
    return CONVERSION.metadata_of_converted_dataframe(frame, []).converted_dataframe


def _schema(frame: pl.DataFrame) -> Mapping[str, pl.DataType]:
    """The converted frame's dtypes, standing in for the sidecar's record of them."""
    converted: pl.DataFrame = _converted(frame)
    return {name: converted.schema[name] for name in converted.columns}


def _fragment(workbook: str, sheet: str, alias: str, frame: pl.DataFrame) -> Fragment:
    """A manifest fragment describing one written sheet."""
    converted: pl.DataFrame = _converted(frame)
    return Fragment(
        workbook=workbook,
        sheet_name=sheet,
        source_alias=alias,
        period_label=None,
        row_count=converted.height,
        column_names=list(converted.columns),
        expected=HASHER.hash_dataframe(converted),
    )


def _write(destination: UPath, filename: str, sheets: list[tuple[str, pl.DataFrame]]) -> None:
    """Write one workbook through the real multi-sheet writer."""
    write_workbook([(name, _converted(frame)) for name, frame in sheets], destination / filename)


def _source(alias: str, frames: list[pl.DataFrame], fragments: list[Fragment]) -> SourceFragments:
    """A manifest entry for one source and everything it contributed."""
    whole: pl.DataFrame = _converted(pl.concat(frames))
    return SourceFragments(
        source_alias=alias,
        source_path=f"/nowhere/{alias}.parquet",
        sidecar_path=f"{alias}.parquet.json",
        conversion=ConversionIdentity(identifier=DataframeConversionToExcel.identifier, version=DataframeConversionToExcel.version, version_number=DataframeConversionToExcel.version_number),
        expected_whole=HASHER.hash_dataframe(whole),
        expected_row_count=whole.height,
        fragments=fragments,
    )


# --------------------------------------------------------------------------------------
# One fragment
# --------------------------------------------------------------------------------------


def test_a_fragment_that_holds_what_the_manifest_says_is_valid(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    frame: pl.DataFrame = _source_frame(20)
    _write(destination, "book_001.xlsx", [("sales 2025", frame)])
    verdict: Verdict = verify_fragment(destination, _fragment("book_001.xlsx", "sales 2025", "sales", frame), _schema(frame))
    assert verdict.valid, verdict.detail
    assert verdict.actual_digest == verdict.expected_digest


def test_only_the_named_sheet_is_read_from_a_fan_in_workbook(tmp_path: Path) -> None:
    # The reason for one call per fragment: the reader concatenates every sheet it is given, and
    # a fan-in workbook's sheets hold different sources with different columns.
    destination: UPath = ZPath(str(tmp_path))
    sales: pl.DataFrame = _source_frame(15)
    returns: pl.DataFrame = _source_frame(6, offset=100)
    _write(destination, "book_001.xlsx", [("sales 2025", sales), ("returns 2025", returns)])
    assert verify_fragment(destination, _fragment("book_001.xlsx", "sales 2025", "sales", sales), _schema(sales)).valid
    assert verify_fragment(destination, _fragment("book_001.xlsx", "returns 2025", "returns", returns), _schema(returns)).valid


def test_mutating_one_cell_reports_a_digest_mismatch_on_exactly_that_fragment(tmp_path: Path) -> None:
    # The spec's own negative check.
    destination: UPath = ZPath(str(tmp_path))
    clean: pl.DataFrame = _source_frame(10)
    tampered: pl.DataFrame = clean.with_columns(pl.when(pl.col("id") == 5).then(pl.lit("changed")).otherwise(pl.col("name")).alias("name"))
    _write(destination, "book_001.xlsx", [("sales 2025", tampered), ("other 2025", clean)])
    bad: Verdict = verify_fragment(destination, _fragment("book_001.xlsx", "sales 2025", "sales", clean), _schema(clean))
    good: Verdict = verify_fragment(destination, _fragment("book_001.xlsx", "other 2025", "sales", clean), _schema(clean))
    assert bad.kind == "digest-mismatch"
    assert good.valid
    assert bad.actual_digest != bad.expected_digest


def test_a_header_that_differs_reports_columns_differ(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    frame: pl.DataFrame = _source_frame(5)
    _write(destination, "book_001.xlsx", [("sales 2025", frame.rename({"name": "label"}))])
    verdict: Verdict = verify_fragment(destination, _fragment("book_001.xlsx", "sales 2025", "sales", frame), _schema(frame))
    assert verdict.kind == "columns-differ"
    assert "label" in verdict.detail


def test_a_row_count_above_what_the_sheet_holds_becomes_a_digest_mismatch(tmp_path: Path) -> None:
    # Not an error: a row whose cells are all empty emits no <row> element, so a trailing run of
    # all-null rows leaves no trace and the reader restores it. Restoring rows that were never
    # there changes the data, which is exactly what the digest is for.
    destination: UPath = ZPath(str(tmp_path))
    frame: pl.DataFrame = _source_frame(10)
    _write(destination, "book_001.xlsx", [("sales 2025", frame)])
    fragment: Fragment = _fragment("book_001.xlsx", "sales 2025", "sales", frame).model_copy(update={"row_count": 99})
    assert verify_fragment(destination, fragment, _schema(frame)).kind == "digest-mismatch"


def test_a_row_count_below_what_the_sheet_holds_reports_the_workbook_unreadable(tmp_path: Path) -> None:
    # The other direction is not restoration but a contradiction: the workbook and the manifest
    # describe different data, rather than a row having gone missing.
    destination: UPath = ZPath(str(tmp_path))
    frame: pl.DataFrame = _source_frame(10)
    _write(destination, "book_001.xlsx", [("sales 2025", frame)])
    fragment: Fragment = _fragment("book_001.xlsx", "sales 2025", "sales", frame).model_copy(update={"row_count": 3})
    verdict: Verdict = verify_fragment(destination, fragment, _schema(frame))
    assert verdict.kind == "workbook-unreadable"
    assert "more rows than expected" in verdict.detail


def test_a_missing_sheet_reports_the_workbook_unreadable(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    frame: pl.DataFrame = _source_frame(5)
    _write(destination, "book_001.xlsx", [("sales 2025", frame)])
    fragment: Fragment = _fragment("book_001.xlsx", "no such sheet", "sales", frame)
    assert verify_fragment(destination, fragment, _schema(frame)).kind == "workbook-unreadable"


def test_a_missing_workbook_raises_rather_than_reporting_invalid(tmp_path: Path) -> None:
    # A path that does not exist is a wiring mistake, not a statement about the data, and
    # reporting it as "invalid" would make it hard to find.
    destination: UPath = ZPath(str(tmp_path))
    frame: pl.DataFrame = _source_frame(5)
    with pytest.raises(FileNotFoundError):
        verify_fragment(destination, _fragment("absent.xlsx", "sales 2025", "sales", frame), _schema(frame))


def test_a_digest_from_another_hasher_version_is_refused_rather_than_compared(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    frame: pl.DataFrame = _source_frame(5)
    _write(destination, "book_001.xlsx", [("sales 2025", frame)])
    base: Fragment = _fragment("book_001.xlsx", "sales 2025", "sales", frame)
    legacy: Fragment = base.model_copy(update={"expected": base.expected.model_copy(update={"version": 1})})
    assert verify_fragment(destination, legacy, _schema(frame)).kind == "unsupported-hasher"


def test_an_empty_fragment_verifies(tmp_path: Path) -> None:
    # A source with no rows in a period still gets a header-only worksheet.
    destination: UPath = ZPath(str(tmp_path))
    frame: pl.DataFrame = _source_frame(0)
    _write(destination, "book_001.xlsx", [("sales Undated", frame)])
    assert verify_fragment(destination, _fragment("book_001.xlsx", "sales Undated", "sales", frame), _schema(frame)).valid


def test_no_parquet_is_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The spec's path-access spy. The whole point of the sidecar and the manifest is that the
    # source need not be present, or even reachable, for an export to be checked.
    destination: UPath = ZPath(str(tmp_path))
    frame: pl.DataFrame = _source_frame(10)
    _write(destination, "book_001.xlsx", [("sales 2025", frame)])
    opened: list[str] = []

    def refuse(*args: object, **kwargs: object) -> object:  # noqa: ARG001
        """Record the call and fail it. Nothing here should ever reach a Parquet file."""
        opened.append("parquet")
        message: str = "verification opened a Parquet file"
        raise RuntimeError(message)

    monkeypatch.setattr(pl, "scan_parquet", refuse)
    monkeypatch.setattr(pl, "read_parquet", refuse)
    assert verify_fragment(destination, _fragment("book_001.xlsx", "sales 2025", "sales", frame), _schema(frame)).valid
    assert opened == []


# --------------------------------------------------------------------------------------
# One source
# --------------------------------------------------------------------------------------


def test_a_source_split_across_fragments_reassembles(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    first: pl.DataFrame = _source_frame(10)
    second: pl.DataFrame = _source_frame(7, offset=10)
    _write(destination, "book_001.xlsx", [("sales p01", first), ("sales p02", second)])
    source: SourceFragments = _source(
        "sales",
        [first, second],
        [_fragment("book_001.xlsx", "sales p01", "sales", first), _fragment("book_001.xlsx", "sales p02", "sales", second)],
    )
    verdict: SourceVerdict = verify_source(destination, source, _schema(first))
    assert verdict.valid
    assert verdict.rows_match
    assert verdict.reassembly is not None
    assert verdict.reassembly.valid


def test_a_source_split_across_workbooks_reassembles(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    first: pl.DataFrame = _source_frame(10)
    second: pl.DataFrame = _source_frame(7, offset=10)
    _write(destination, "book_001.xlsx", [("sales p01", first)])
    _write(destination, "book_002.xlsx", [("sales p02", second)])
    source: SourceFragments = _source(
        "sales",
        [first, second],
        [_fragment("book_001.xlsx", "sales p01", "sales", first), _fragment("book_002.xlsx", "sales p02", "sales", second)],
    )
    assert verify_source(destination, source, _schema(first)).valid


def test_deleting_a_fragment_fails_the_row_count_and_the_reassembly(tmp_path: Path) -> None:
    # The spec's other negative check. Both fail, and the row count is what names the problem.
    destination: UPath = ZPath(str(tmp_path))
    first: pl.DataFrame = _source_frame(10)
    second: pl.DataFrame = _source_frame(7, offset=10)
    _write(destination, "book_001.xlsx", [("sales p01", first), ("sales p02", second)])
    complete: SourceFragments = _source(
        "sales",
        [first, second],
        [_fragment("book_001.xlsx", "sales p01", "sales", first), _fragment("book_001.xlsx", "sales p02", "sales", second)],
    )
    dropped: SourceFragments = complete.model_copy(update={"fragments": complete.fragments[:1]})
    verdict: SourceVerdict = verify_source(destination, dropped, _schema(first))
    assert not verdict.valid
    assert not verdict.rows_match
    assert verdict.rows_found == 10
    assert verdict.rows_expected == 17
    assert verdict.reassembly is not None
    assert verdict.reassembly.kind == "digest-mismatch"
    # Every fragment that is still there is individually fine, which is exactly why the
    # arithmetic checks exist.
    assert all(fragment.valid for fragment in verdict.fragments)


def test_a_source_with_no_fragments_cannot_reassemble(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    frame: pl.DataFrame = _source_frame(10)
    empty: SourceFragments = _source("sales", [frame], []).model_copy(update={"fragments": []})
    verdict: SourceVerdict = verify_source(destination, empty, _schema(frame))
    assert verdict.reassembly is not None
    assert verdict.reassembly.kind == "digest-mismatch"
    assert "nothing can add up to it" in verdict.reassembly.detail


def test_the_reassembly_check_is_skipped_when_the_fragments_do_not_cover_the_source(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    first: pl.DataFrame = _source_frame(10)
    _write(destination, "book_001.xlsx", [("sales p01", first)])
    partial: SourceFragments = _source("sales", [first], [_fragment("book_001.xlsx", "sales p01", "sales", first)]).model_copy(update={"total_and_disjoint": False})
    verdict: SourceVerdict = verify_source(destination, partial, _schema(first))
    assert verdict.reassembly is None
    assert verdict.valid


def test_a_conversion_from_another_version_is_refused_rather_than_compared(tmp_path: Path) -> None:
    # Pinned so a later build that changed the conversion is a version mismatch rather than a
    # digest mismatch, which names the wrong problem.
    destination: UPath = ZPath(str(tmp_path))
    frame: pl.DataFrame = _source_frame(5)
    _write(destination, "book_001.xlsx", [("sales 2025", frame)])
    source: SourceFragments = _source("sales", [frame], [_fragment("book_001.xlsx", "sales 2025", "sales", frame)])
    skewed: SourceFragments = source.model_copy(update={"conversion": source.conversion.model_copy(update={"version": 99})})
    verdict: SourceVerdict = verify_source(destination, skewed, _schema(frame))
    assert not verdict.valid
    assert verdict.fragments[0].kind == "unsupported-conversion"


@pytest.mark.parametrize("max_workers", [1, 2, 4])
def test_the_verdict_does_not_depend_on_the_thread_count(tmp_path: Path, max_workers: int) -> None:
    # Parallelism lives across fragments because a fastexcel handle raises Already borrowed when
    # shared; each fragment opening its own is what makes that safe.
    destination: UPath = ZPath(str(tmp_path))
    frames: list[pl.DataFrame] = [_source_frame(6, offset=start) for start in (0, 6, 12, 18)]
    sheets: list[tuple[str, pl.DataFrame]] = [(f"sales p{index:02d}", frame) for index, frame in enumerate(frames, start=1)]
    _write(destination, "book_001.xlsx", sheets)
    fragments: list[Fragment] = [_fragment("book_001.xlsx", name, "sales", frame) for name, frame in sheets]
    source: SourceFragments = _source("sales", frames, fragments)
    assert verify_source(destination, source, _schema(frames[0]), max_workers=max_workers).valid


# --------------------------------------------------------------------------------------
# A whole manifest
# --------------------------------------------------------------------------------------


def _manifest(sources: list[SourceFragments], workbooks: list[str]) -> RunManifest:
    """A manifest carrying the given sources."""
    return RunManifest(
        run_id="2026-09-15T12-00-00Z-annual_review",
        config_path="/nowhere/export-config.json",
        profile="annual_review",
        resolved_config=ExportConfig.model_validate(BASE),
        planner_version="1",
        output_directory="/nowhere",
        started_utc=dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC),
        sources=sources,
        workbooks=workbooks,
        sidecars=[source.sidecar_path for source in sources],
    )


def test_a_fan_in_run_verifies_end_to_end(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    sales: pl.DataFrame = _source_frame(12)
    returns: pl.DataFrame = _source_frame(5, offset=500)
    _write(destination, "book_001.xlsx", [("sales 2025", sales), ("returns 2025", returns)])
    manifest: RunManifest = _manifest(
        [
            _source("sales", [sales], [_fragment("book_001.xlsx", "sales 2025", "sales", sales)]),
            _source("returns", [returns], [_fragment("book_001.xlsx", "returns 2025", "returns", returns)]),
        ],
        ["book_001.xlsx"],
    )
    verdict: ManifestVerdict = verify_manifest(destination, manifest, {"sales": _schema(sales), "returns": _schema(returns)})
    assert verdict.valid
    assert verdict.failures == ()
    assert [source.source_alias for source in verdict.sources] == ["sales", "returns"]


def test_one_bad_source_does_not_hide_the_good_ones(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    sales: pl.DataFrame = _source_frame(12)
    returns: pl.DataFrame = _source_frame(5, offset=500)
    tampered: pl.DataFrame = returns.with_columns(pl.lit("changed").alias("name"))
    _write(destination, "book_001.xlsx", [("sales 2025", sales), ("returns 2025", tampered)])
    manifest: RunManifest = _manifest(
        [
            _source("sales", [sales], [_fragment("book_001.xlsx", "sales 2025", "sales", sales)]),
            _source("returns", [returns], [_fragment("book_001.xlsx", "returns 2025", "returns", returns)]),
        ],
        ["book_001.xlsx"],
    )
    verdict: ManifestVerdict = verify_manifest(destination, manifest, {"sales": _schema(sales), "returns": _schema(returns)})
    assert not verdict.valid
    assert verdict.sources[0].valid
    assert not verdict.sources[1].valid
    assert len(verdict.failures) == 1


def test_a_source_with_no_schema_is_a_wiring_mistake(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    sales: pl.DataFrame = _source_frame(5)
    _write(destination, "book_001.xlsx", [("sales 2025", sales)])
    manifest: RunManifest = _manifest([_source("sales", [sales], [_fragment("book_001.xlsx", "sales 2025", "sales", sales)])], ["book_001.xlsx"])
    with pytest.raises(KeyError):
        verify_manifest(destination, manifest, {})


def test_the_reader_is_not_asked_for_the_whole_workbook(tmp_path: Path) -> None:
    # Pins the design rather than the outcome: a fan-in workbook read whole would concatenate two
    # sources' sheets, which have different columns, and fail.
    destination: UPath = ZPath(str(tmp_path))
    sales: pl.DataFrame = _source_frame(12)
    returns: pl.DataFrame = _source_frame(5, offset=500).rename({"name": "reason"})
    _write(destination, "book_001.xlsx", [("sales 2025", sales), ("returns 2025", returns)])
    reader: fastexcel.ExcelReader = fastexcel.read_excel(str(destination / "book_001.xlsx"))
    assert reader.sheet_names == ["sales 2025", "returns 2025"]
    assert verify_fragment(destination, _fragment("book_001.xlsx", "returns 2025", "returns", returns), _schema(returns)).valid
