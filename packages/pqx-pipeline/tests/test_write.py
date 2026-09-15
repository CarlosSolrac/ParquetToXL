"""Frozen tests for turning a plan into workbooks and the manifest that describes them.

These go all the way round: plan, write, then verify what was written against the manifest that
was produced alongside it. That loop is the project's whole claim, so it is checked here rather
than assembled for the first time by a CLI.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from pqx_calendar.periods import UNDATED, Bucket, PeriodKey
from pqx_common.paths import ZPath
from pqx_pipeline.ingest import Observation, build_sidecar, observe, read_parquet
from pqx_pipeline.locations import keep_set, manifest_path, receipt_path
from pqx_pipeline.write import WrittenWorkbook, conversion_identity, write_profile
from pqx_plan.allocate import allocate_workbooks, order_profile_sheets
from pqx_plan.config import CalendarPartitioning, NamingSettings, ProfileLimits
from pqx_plan.manifest import SourceFragments
from pqx_plan.naming import NamedWorkbook, name_workbooks
from pqx_plan.partition import PlannedSheet, plan_calendar_sheets
from pqx_verify.fragments import SourceVerdict, verify_source

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from upath import UPath

CREATED: dt.datetime = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)


def _naming(**changes: Any) -> NamingSettings:  # noqa: ANN401
    """The example configuration's naming block."""
    settings: dict[str, Any] = {
        "workbook": "{profile}_{period_label}_{workbook_index:03d}.xlsx",
        "worksheet": "{source} {period_label}",
        "single_worksheet": "{source} Data",
        "overflow_worksheet": "{source} {period_label}_p{part_index:02d}",
        "month_format": "numeric",
        "worksheet_prefix": "",
        "sheet_collision": "error",
    }
    settings.update(changes)
    return NamingSettings.model_validate(settings)


def _partitioning(**changes: Any) -> CalendarPartitioning:  # noqa: ANN401
    """A greedy calendar profile over years."""
    settings: dict[str, Any] = {
        "algorithm": "calendar_greedy",
        "base_period": "year",
        "period_order": "ascending",
        "year_split_months": [6, 4, 3, 2, 1],
        "oversized_period": "balanced_rows",
        "null_dates": "separate",
    }
    settings.update(changes)
    return CalendarPartitioning.model_validate(settings)


def _source(tmp_path: Path, alias: str, rows: int) -> Observation:
    """A real Parquet file, described and observed."""
    path: UPath = ZPath(str(tmp_path / f"{alias}.parquet"))
    pl.DataFrame(
        {
            "id": pl.Series(list(range(rows)), dtype=pl.Int64),
            "name": pl.Series([f"{alias} {index}" for index in range(rows)], dtype=pl.String),
            "booked": pl.Series([dt.date(2025, 1, 1) + dt.timedelta(days=index % 300) for index in range(rows)], dtype=pl.Date),
        },
    ).write_parquet(str(path))
    frame: pl.DataFrame = read_parquet(path)
    return observe(alias, frame, original_path=path, metadata=build_sidecar(frame, original_path=path, created_utc=CREATED))


def _plan(observations: Mapping[str, Observation], counts: Mapping[str, dict[Bucket, int]], limits: ProfileLimits, naming: NamingSettings) -> tuple[NamedWorkbook, ...]:
    """Run the planner over the given bucket counts and return the named workbooks."""
    partitioning: CalendarPartitioning = _partitioning()
    per_source: list[tuple[int, tuple[PlannedSheet, ...]]] = [(position, plan_calendar_sheets(observations[alias].shape, counts[alias], partitioning, limits)) for position, alias in enumerate(observations)]
    ordered: tuple[PlannedSheet, ...] = order_profile_sheets(per_source, partitioning.period_order)
    widths: dict[str, int] = {alias: seen.shape.columns for alias, seen in observations.items()}
    return name_workbooks(
        allocate_workbooks(ordered, widths, limits.max_cells_per_workbook),
        naming,
        profile="annual_review",
        stems={alias: seen.shape.stem for alias, seen in observations.items()},
    )


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------


def test_a_plan_becomes_workbooks_and_a_manifest(tmp_path: Path) -> None:
    observations: dict[str, Observation] = {"sales": _source(tmp_path, "sales", 30)}
    limits: ProfileLimits = ProfileLimits(max_data_rows_per_worksheet=1000, max_cells_per_workbook=10_000_000)
    named: tuple[NamedWorkbook, ...] = _plan(observations, {"sales": {PeriodKey("month", 2025, 1): 30}}, limits, _naming())
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    scratch.mkdir(parents=True)
    written: tuple[WrittenWorkbook, ...]
    sources: tuple[SourceFragments, ...]
    written, sources = write_profile(named, observations, scratch, sidecar_names={"sales": "sales.parquet.json"})
    assert len(written) == 1
    assert written[0].path.exists()
    assert written[0].rows == 30
    assert len(sources) == 1
    assert sources[0].expected_row_count == 30


def test_every_row_reaches_exactly_one_sheet(tmp_path: Path) -> None:
    # Total and disjoint by construction: each sheet takes the next rows the plan asked for.
    observations: dict[str, Observation] = {"sales": _source(tmp_path, "sales", 250)}
    limits: ProfileLimits = ProfileLimits(max_data_rows_per_worksheet=100, max_cells_per_workbook=10_000_000)
    named: tuple[NamedWorkbook, ...] = _plan(observations, {"sales": {PeriodKey("month", 2025, 1): 250}}, limits, _naming())
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    scratch.mkdir(parents=True)
    sources: tuple[SourceFragments, ...]
    _: tuple[WrittenWorkbook, ...]
    _, sources = write_profile(named, observations, scratch, sidecar_names={"sales": "sales.parquet.json"})
    assert sum(fragment.row_count for fragment in sources[0].fragments) == 250
    assert len(sources[0].fragments) == 3


def test_the_fragments_reassemble_into_the_whole_source(tmp_path: Path) -> None:
    # The additive identity, end to end: the digests taken per sheet at write time add back up to
    # the digest of the source they were cut from.
    from pqx_frame.hashing.binary_aggregate import BinaryAggregateHashedDataframe, DataFrameHasherBinaryAggregateHash

    observations: dict[str, Observation] = {"sales": _source(tmp_path, "sales", 250)}
    limits: ProfileLimits = ProfileLimits(max_data_rows_per_worksheet=100, max_cells_per_workbook=10_000_000)
    named: tuple[NamedWorkbook, ...] = _plan(observations, {"sales": {PeriodKey("month", 2025, 1): 250}}, limits, _naming())
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    scratch.mkdir(parents=True)
    sources: tuple[SourceFragments, ...]
    _: tuple[WrittenWorkbook, ...]
    _, sources = write_profile(named, observations, scratch, sidecar_names={"sales": "sales.parquet.json"})
    combined: BinaryAggregateHashedDataframe = DataFrameHasherBinaryAggregateHash.combine([fragment.expected for fragment in sources[0].fragments])
    assert combined.digest_hex == sources[0].expected_whole.digest_hex
    assert combined.row_digest_hex == sources[0].expected_whole.row_digest_hex


def test_what_was_written_verifies_against_the_manifest_written_beside_it(tmp_path: Path) -> None:
    # The project's whole claim, checked as one loop rather than as two halves that were never
    # put together.
    observations: dict[str, Observation] = {"sales": _source(tmp_path, "sales", 120), "returns": _source(tmp_path, "returns", 40)}
    limits: ProfileLimits = ProfileLimits(max_data_rows_per_worksheet=50, max_cells_per_workbook=10_000_000)
    counts: dict[str, dict[Bucket, int]] = {"sales": {PeriodKey("month", 2025, 1): 120}, "returns": {PeriodKey("month", 2025, 1): 40}}
    named: tuple[NamedWorkbook, ...] = _plan(observations, counts, limits, _naming())
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    scratch.mkdir(parents=True)
    sources: tuple[SourceFragments, ...]
    _: tuple[WrittenWorkbook, ...]
    _, sources = write_profile(named, observations, scratch, sidecar_names={alias: f"{alias}.parquet.json" for alias in observations})
    source: SourceFragments
    for source in sources:
        schema: dict[str, pl.DataType] = dict(observations[source.source_alias].frame.schema)
        verdict: SourceVerdict = verify_source(scratch, source, schema)
        assert verdict.valid, [fragment.detail for fragment in verdict.fragments if not fragment.valid]


def test_a_fan_in_workbook_holds_both_sources(tmp_path: Path) -> None:
    observations: dict[str, Observation] = {"sales": _source(tmp_path, "sales", 10), "returns": _source(tmp_path, "returns", 5)}
    limits: ProfileLimits = ProfileLimits(max_data_rows_per_worksheet=1000, max_cells_per_workbook=10_000_000)
    counts: dict[str, dict[Bucket, int]] = {"sales": {PeriodKey("month", 2025, 1): 10}, "returns": {PeriodKey("month", 2025, 1): 5}}
    named: tuple[NamedWorkbook, ...] = _plan(observations, counts, limits, _naming())
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    scratch.mkdir(parents=True)
    written: tuple[WrittenWorkbook, ...]
    _: tuple[SourceFragments, ...]
    written, _ = write_profile(named, observations, scratch, sidecar_names={alias: f"{alias}.parquet.json" for alias in observations})
    assert len(written) == 1
    assert written[0].sheets == 2


def test_an_undated_fragment_records_its_label(tmp_path: Path) -> None:
    observations: dict[str, Observation] = {"sales": _source(tmp_path, "sales", 12)}
    limits: ProfileLimits = ProfileLimits(max_data_rows_per_worksheet=1000, max_cells_per_workbook=10_000_000)
    counts: dict[str, dict[Bucket, int]] = {"sales": {PeriodKey("month", 2025, 1): 9, UNDATED: 3}}
    named: tuple[NamedWorkbook, ...] = _plan(observations, counts, limits, _naming())
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    scratch.mkdir(parents=True)
    sources: tuple[SourceFragments, ...]
    _: tuple[WrittenWorkbook, ...]
    _, sources = write_profile(named, observations, scratch, sidecar_names={"sales": "sales.parquet.json"})
    assert [fragment.period_label for fragment in sources[0].fragments] == ["2025", "Undated"]


def test_an_overflow_fragment_records_its_part_index(tmp_path: Path) -> None:
    observations: dict[str, Observation] = {"sales": _source(tmp_path, "sales", 30)}
    limits: ProfileLimits = ProfileLimits(max_data_rows_per_worksheet=10, max_cells_per_workbook=10_000_000)
    # All 30 rows in January and a capacity of 10: no grid from semesters down to months fits, so
    # the finest one is used anyway and January goes to the overflow rule.
    counts: dict[str, dict[Bucket, int]] = {"sales": {PeriodKey("month", 2025, 1): 30}}
    named: tuple[NamedWorkbook, ...] = _plan(observations, counts, limits, _naming())
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    scratch.mkdir(parents=True)
    sources: tuple[SourceFragments, ...]
    _: tuple[WrittenWorkbook, ...]
    _, sources = write_profile(named, observations, scratch, sidecar_names={"sales": "sales.parquet.json"})
    assert [fragment.part_index for fragment in sources[0].fragments] == [1, 2, 3]


def test_the_manifest_records_the_original_source_path_not_the_scratch_copy(tmp_path: Path) -> None:
    # The sidecar's modified_utc is the remote source's mtime, and this is the path it belongs to;
    # recording the staging path would make the pair describe a file deleted before verification
    # even starts.
    observations: dict[str, Observation] = {"sales": _source(tmp_path, "sales", 5)}
    limits: ProfileLimits = ProfileLimits(max_data_rows_per_worksheet=1000, max_cells_per_workbook=10_000_000)
    named: tuple[NamedWorkbook, ...] = _plan(observations, {"sales": {PeriodKey("month", 2025, 1): 5}}, limits, _naming())
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    scratch.mkdir(parents=True)
    sources: tuple[SourceFragments, ...]
    _: tuple[WrittenWorkbook, ...]
    _, sources = write_profile(named, observations, scratch, sidecar_names={"sales": "sales.parquet.json"})
    assert "scratch" not in sources[0].source_path
    assert sources[0].source_path.endswith("sales.parquet")


def test_the_conversion_is_pinned_in_the_manifest() -> None:
    # So a later build that changed the conversion is a version mismatch rather than a digest
    # mismatch, which names the wrong problem.
    from pqx_frame.conversion.to_excel import DataframeConversionToExcel

    assert conversion_identity().identifier == DataframeConversionToExcel.identifier
    assert conversion_identity().version == DataframeConversionToExcel.version


def test_a_named_sheet_with_no_observation_is_a_wiring_mistake(tmp_path: Path) -> None:
    observations: dict[str, Observation] = {"sales": _source(tmp_path, "sales", 5)}
    limits: ProfileLimits = ProfileLimits(max_data_rows_per_worksheet=1000, max_cells_per_workbook=10_000_000)
    named: tuple[NamedWorkbook, ...] = _plan(observations, {"sales": {PeriodKey("month", 2025, 1): 5}}, limits, _naming())
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    scratch.mkdir(parents=True)
    with pytest.raises(KeyError):
        write_profile(named, observations, scratch, sidecar_names={})


# --------------------------------------------------------------------------------------
# Locations and the keep set
# --------------------------------------------------------------------------------------


def test_the_bookkeeping_lives_beside_the_lease(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    assert manifest_path(destination, "annual_review").name == "annual_review.manifest.json"
    assert receipt_path(destination, "annual_review").name == "annual_review.receipt.json"


def test_the_keep_set_holds_the_runs_own_records() -> None:
    # Without them the run deletes its own records on the way out.
    keep: frozenset[str] = keep_set("annual_review", workbooks=("a.xlsx",), sidecars=("sales.parquet.json",))
    assert "annual_review.manifest.json" in keep
    assert "annual_review.receipt.json" in keep
    assert "annual_review.lock" in keep
    assert "reports" in keep


def test_the_keep_set_holds_the_sidecars_the_run_published() -> None:
    # With the sidecars beside the output they share this directory, and a keep-set without them
    # would have the run delete what it published a few steps earlier.
    assert "sales.parquet.json" in keep_set("annual_review", workbooks=(), sidecars=("sales.parquet.json",))


def test_a_nested_name_is_kept_by_its_first_segment() -> None:
    # The delete set is computed from a listing of the directory itself rather than a walk.
    assert keep_set("annual_review", workbooks=("nested/a.xlsx",), sidecars=()) >= {"nested"}


def test_reports_live_in_their_own_directory(tmp_path: Path) -> None:
    # Excluded from reconciliation wholesale, so a failed run's report survives the next run.
    from pqx_pipeline.locations import REPORTS_DIRECTORY, reports_directory

    assert reports_directory(ZPath(str(tmp_path))).name == REPORTS_DIRECTORY
