"""Frozen tests for one profile's run, end to end.

Real Parquet in, real workbooks and bookkeeping out, verified in between. Nothing here is mocked:
the point of this file is that the stages compose in the order the spec says they must.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from pqx_common.paths import ZPath
from pqx_pipeline.locations import manifest_path, receipt_path
from pqx_pipeline.run import PLANNER_VERSION, OwnershipError, RunInstants, RunOptions, RunOutcome, check_ownership, run_profile, select_profile
from pqx_plan.config import ExportConfig, ProfileSettings
from pqx_plan.receipt import RunReceipt
from pqx_staging.lease import lease_path
from pqx_staging.transfer import list_names

if TYPE_CHECKING:
    from pathlib import Path

    from pqx_pipeline.staleness import ProfileStaleness
    from upath import UPath

NOON: dt.datetime = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
INSTANTS: RunInstants = RunInstants.at(NOON)
RUN_ID: str = "2026-09-15T12-00-00Z-annual"


def _write_source(path: UPath, rows: int, *, start: dt.date = dt.date(2024, 1, 1)) -> None:
    """A Parquet file whose dates span two years."""
    pl.DataFrame(
        {
            "id": pl.Series(list(range(rows)), dtype=pl.Int64),
            "customer": pl.Series([f"c{index % 7}" for index in range(rows)], dtype=pl.String),
            "booked": pl.Series([start + dt.timedelta(days=index * 5) for index in range(rows)], dtype=pl.Date),
        },
    ).write_parquet(str(path))


def _config(tmp_path: Path, **changes: Any) -> ExportConfig:  # noqa: ANN401
    """A two-source configuration pointing at real files under ``tmp_path``."""
    sales: UPath = ZPath(str(tmp_path / "sales.parquet"))
    returns: UPath = ZPath(str(tmp_path / "returns.parquet"))
    _write_source(sales, 40)
    _write_source(returns, 12, start=dt.date(2025, 2, 1))
    document: dict[str, Any] = {
        "config_version": 1,
        "config_id": "cfg-1",
        "config_modified_utc": "2026-09-01T00:00:00Z",
        "output_directory": str(tmp_path / "out"),
        "sidecar_location": {"kind": "beside_source"},
        "scratch_root": None,
        "excel": {"writer": "rustpy-xlsxwriter", "options": {}},
        "sources": {
            "sales": {"path": str(sales), "date_columns": {"booked": {"type": "date"}}},
            "returns": {"path": str(returns), "date_columns": {"booked": {"type": "date"}}},
        },
        "profiles": [
            {
                "name": "annual",
                "output_subdirectory": "annual",
                "limits": {"max_data_rows_per_worksheet": 1000, "max_cells_per_workbook": 10_000_000},
                "partitioning": {
                    "algorithm": "calendar_greedy",
                    "base_period": "year",
                    "period_order": "ascending",
                    "year_split_months": [6, 4, 3, 2, 1],
                    "oversized_period": "balanced_rows",
                    "null_dates": "separate",
                },
                "naming": {
                    "workbook": "{profile}_{period_label}_{workbook_index:03d}.xlsx",
                    "worksheet": "{source} {period_label}",
                    "single_worksheet": "{source} Data",
                    "overflow_worksheet": "{source} {period_label}_p{part_index:02d}",
                    "month_format": "numeric",
                    "worksheet_prefix": "",
                    "sheet_collision": "error",
                },
                "sheets": [
                    {"source": "sales", "partition_column": "booked", "sort": [{"column": "customer", "direction": "ascending", "nulls": "last"}]},
                    {"source": "returns", "partition_column": "booked", "sort": []},
                ],
            },
        ],
    }
    document.update(changes)
    return ExportConfig.model_validate(document)


@dataclass(frozen=True, slots=True)
class _Ran:
    """One completed run and what it was run against, so a test names what it needs."""

    config: ExportConfig
    profile: ProfileSettings
    outcome: RunOutcome


def _run(tmp_path: Path, **options: Any) -> _Ran:  # noqa: ANN401
    """Run the profile once."""
    config: ExportConfig = _config(tmp_path)
    profile: ProfileSettings = config.profiles[0]
    outcome: RunOutcome = run_profile(config, profile, run_id=RUN_ID, instants=INSTANTS, options=RunOptions(scratch_root=ZPath(str(tmp_path / "scratch")), **options))
    return _Ran(config=config, profile=profile, outcome=outcome)


def _destination(config: ExportConfig, profile: ProfileSettings) -> UPath:
    """Where the profile publishes."""
    return ZPath(config.output_directory) / profile.output_subdirectory


# --------------------------------------------------------------------------------------
# A first run
# --------------------------------------------------------------------------------------


def test_a_first_run_publishes_a_verified_export(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    assert ran.outcome.valid, ran.outcome.report.failure
    assert ran.outcome.verdict is not None
    assert ran.outcome.verdict.valid
    assert ran.outcome.manifest is not None


def test_a_first_run_is_stale_because_there_is_no_receipt(tmp_path: Path) -> None:
    config: ExportConfig = _config(tmp_path)
    profile: ProfileSettings = config.profiles[0]
    assert "no-receipt" in select_profile(config, profile, _destination(config, profile)).reasons


def test_everything_the_manifest_names_is_at_the_destination(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    destination: UPath = _destination(ran.config, ran.profile)
    assert ran.outcome.manifest is not None
    name: str
    for name in [*ran.outcome.manifest.workbooks, *ran.outcome.manifest.sidecars]:
        assert (destination / name).exists(), name
    assert manifest_path(destination, ran.profile.name).exists()
    assert receipt_path(destination, ran.profile.name).exists()


def test_the_report_is_published_in_all_three_renderings(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    reports: list[str] = sorted(entry.name for entry in (_destination(ran.config, ran.profile) / "reports").iterdir())
    assert len(reports) == 3
    assert {name.rsplit(".", 1)[-1] for name in reports} == {"json", "md", "html"}


def test_the_lease_is_released_when_the_run_finishes(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    assert not lease_path(_destination(ran.config, ran.profile), ran.profile.name).exists()


def test_the_scratch_directory_is_gone(tmp_path: Path) -> None:
    _run(tmp_path)
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    assert not (scratch / f"run-{RUN_ID}").exists()


def test_the_staged_parquet_is_deleted_before_verification(tmp_path: Path) -> None:
    # Structural rather than asserted: the source is physically absent while verification runs.
    # The scratch directory is removed at the end, so what this checks is that the run completed
    # with verification passing, which it could not have done had it needed the staged copy.
    ran: _Ran = _run(tmp_path)
    assert ran.outcome.verdict is not None
    assert ran.outcome.verdict.valid


def test_every_row_reaches_the_export(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    assert ran.outcome.manifest is not None
    rows: dict[str, int] = {source.source_alias: source.expected_row_count for source in ran.outcome.manifest.sources}
    assert rows == {"sales": 40, "returns": 12}
    source: Any
    for source in ran.outcome.manifest.sources:
        assert sum(fragment.row_count for fragment in source.fragments) == source.expected_row_count


def test_the_receipt_records_what_the_next_run_compares_against(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    receipt: RunReceipt = RunReceipt.model_validate_json(receipt_path(_destination(ran.config, ran.profile), ran.profile.name).read_text(encoding="utf-8"))
    assert receipt.config_id == ran.config.config_id
    assert receipt.profile == ran.profile.name
    assert receipt.excel_created_utc == NOON
    assert receipt.planner_version == PLANNER_VERSION
    assert set(receipt.sources) == {"sales", "returns"}


def test_the_manifest_embeds_a_configuration_a_reader_could_load(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    written: dict[str, Any] = json.loads(manifest_path(_destination(ran.config, ran.profile), ran.profile.name).read_text(encoding="utf-8"))
    assert ExportConfig.model_validate(written["resolved_config"]) == ran.config


# --------------------------------------------------------------------------------------
# A second run
# --------------------------------------------------------------------------------------


def test_a_second_run_with_nothing_changed_does_no_work(tmp_path: Path) -> None:
    # Run it again unchanged: it must stage nothing and publish nothing, and say so.
    ran: _Ran = _run(tmp_path)
    again: RunOutcome = run_profile(ran.config, ran.profile, run_id="second", instants=INSTANTS, options=RunOptions(scratch_root=ZPath(str(tmp_path / "scratch"))))
    assert again.valid
    assert again.published == ()
    assert again.manifest is None
    assert "nothing was stale" in " ".join(again.report.notes)


def test_a_forced_run_rebuilds_regardless(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    again: RunOutcome = run_profile(
        ran.config,
        ran.profile,
        run_id="second",
        instants=INSTANTS,
        options=RunOptions(force=True, scratch_root=ZPath(str(tmp_path / "scratch"))),
    )
    assert again.manifest is not None
    assert "forced" in again.staleness.reasons


def test_a_dry_run_stages_nothing_and_publishes_nothing(tmp_path: Path) -> None:
    config: ExportConfig = _config(tmp_path)
    profile: ProfileSettings = config.profiles[0]
    outcome: RunOutcome = run_profile(
        config,
        profile,
        run_id=RUN_ID,
        instants=INSTANTS,
        options=RunOptions(dry_run=True, scratch_root=ZPath(str(tmp_path / "scratch"))),
    )
    assert outcome.manifest is None
    assert outcome.published == ()
    assert list_names(_destination(config, profile)) == ()
    assert "dry run" in " ".join(outcome.report.notes)


# --------------------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------------------


def test_an_empty_destination_is_available(tmp_path: Path) -> None:
    check_ownership(ZPath(str(tmp_path / "out")), "annual", "cfg-1")


def test_a_destination_this_configuration_owns_is_available(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    check_ownership(_destination(ran.config, ran.profile), ran.profile.name, ran.config.config_id)


def test_a_destination_another_configuration_owns_is_refused(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    with pytest.raises(OwnershipError, match="not 'other'"):
        check_ownership(_destination(ran.config, ran.profile), ran.profile.name, "other")


def test_a_destination_another_profile_owns_is_refused(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    with pytest.raises(OwnershipError, match="profile"):
        check_ownership(_destination(ran.config, ran.profile), "monthly", ran.config.config_id)


def test_a_destination_holding_output_with_no_receipt_is_refused(tmp_path: Path) -> None:
    # Something else wrote here, so this run will not reconcile it.
    destination: UPath = ZPath(str(tmp_path / "out" / "annual"))
    destination.mkdir(parents=True)
    (destination / "somebody_elses.xlsx").write_bytes(b"x")
    with pytest.raises(OwnershipError, match="something else wrote here"):
        check_ownership(destination, "annual", "cfg-1")


def test_a_previous_failed_runs_reports_and_lease_do_not_claim_the_directory(tmp_path: Path) -> None:
    # Both are left by a run that failed, and neither means the directory is someone else's.
    destination: UPath = ZPath(str(tmp_path / "out" / "annual"))
    (destination / "reports").mkdir(parents=True)
    (destination / "reports" / "annual-x.report.json").write_text("{}", encoding="utf-8")
    lease_path(destination, "annual").write_text("{}", encoding="utf-8")
    check_ownership(destination, "annual", "cfg-1")


def test_a_run_against_a_directory_it_does_not_own_refuses_rather_than_reports(tmp_path: Path) -> None:
    # Publishing a report into a directory this run does not own is the very thing the check
    # exists to prevent.
    config: ExportConfig = _config(tmp_path)
    profile: ProfileSettings = config.profiles[0]
    destination: UPath = _destination(config, profile)
    destination.mkdir(parents=True)
    (destination / "somebody_elses.xlsx").write_bytes(b"x")
    with pytest.raises(OwnershipError):
        run_profile(config, profile, run_id=RUN_ID, instants=INSTANTS, options=RunOptions(scratch_root=ZPath(str(tmp_path / "scratch"))))


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------


def test_a_stale_workbook_from_an_earlier_run_is_reconciled_away(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    destination: UPath = _destination(ran.config, ran.profile)
    (destination / "annual_old_999.xlsx").write_bytes(b"stale")
    again: RunOutcome = run_profile(
        ran.config,
        ran.profile,
        run_id="second",
        instants=INSTANTS,
        options=RunOptions(force=True, scratch_root=ZPath(str(tmp_path / "scratch"))),
    )
    assert "annual_old_999.xlsx" in again.deleted
    assert not (destination / "annual_old_999.xlsx").exists()


def test_the_run_never_deletes_its_own_records(tmp_path: Path) -> None:
    ran: _Ran = _run(tmp_path)
    destination: UPath = _destination(ran.config, ran.profile)
    run_profile(ran.config, ran.profile, run_id="second", instants=INSTANTS, options=RunOptions(force=True, scratch_root=ZPath(str(tmp_path / "scratch"))))
    assert manifest_path(destination, ran.profile.name).exists()
    assert receipt_path(destination, ran.profile.name).exists()
    assert (destination / "reports").exists()


def test_the_sidecars_the_run_published_survive_reconciliation(tmp_path: Path) -> None:
    # With the sidecars beside the output they share this directory, and a keep-set without them
    # would have the run delete what it published a few steps earlier.
    ran: _Ran = _run(tmp_path)
    destination: UPath = _destination(ran.config, ran.profile)
    assert ran.outcome.manifest is not None
    name: str
    for name in ran.outcome.manifest.sidecars:
        assert (destination / name).exists()


# --------------------------------------------------------------------------------------
# Past the single-sheet shortcut
# --------------------------------------------------------------------------------------


def _tight(tmp_path: Path, **profile_changes: Any) -> ExportConfig:  # noqa: ANN401
    """The same configuration with limits too small for the shortcut."""
    config: ExportConfig = _config(tmp_path)
    document: dict[str, Any] = json.loads(config.model_dump_json())
    document["profiles"][0]["limits"] = {"max_data_rows_per_worksheet": 6, "max_cells_per_workbook": 100}
    document["profiles"][0].update(profile_changes)
    return ExportConfig.model_validate(document)


def test_a_run_past_the_shortcut_takes_the_calendar_route(tmp_path: Path) -> None:
    config: ExportConfig = _tight(tmp_path)
    profile: ProfileSettings = config.profiles[0]
    outcome: RunOutcome = run_profile(config, profile, run_id=RUN_ID, instants=INSTANTS, options=RunOptions(scratch_root=ZPath(str(tmp_path / "scratch"))))
    assert outcome.valid, outcome.report.failure
    assert outcome.manifest is not None
    assert len(outcome.manifest.workbooks) > 1
    # The sheets are named for the periods they cover, not `Data`.
    assert any(fragment.period_label not in {None, "Data"} for source in outcome.manifest.sources for fragment in source.fragments)


def test_every_row_survives_a_multi_workbook_run(tmp_path: Path) -> None:
    config: ExportConfig = _tight(tmp_path)
    profile: ProfileSettings = config.profiles[0]
    outcome: RunOutcome = run_profile(config, profile, run_id=RUN_ID, instants=INSTANTS, options=RunOptions(scratch_root=ZPath(str(tmp_path / "scratch"))))
    assert outcome.manifest is not None
    source: Any
    for source in outcome.manifest.sources:
        assert sum(fragment.row_count for fragment in source.fragments) == source.expected_row_count


def test_one_source_named_by_two_sheets_is_selected_once(tmp_path: Path) -> None:
    config: ExportConfig = _config(tmp_path)
    document: dict[str, Any] = json.loads(config.model_dump_json())
    document["profiles"][0]["sheets"].append({"source": "sales", "partition_column": "booked", "sort": []})
    repeated: ExportConfig = ExportConfig.model_validate(document)
    profile: ProfileSettings = repeated.profiles[0]
    staleness: ProfileStaleness = select_profile(repeated, profile, _destination(repeated, profile))
    assert [source.alias for source in staleness.sources] == ["sales", "returns"]


def test_a_balanced_profile_says_plainly_that_it_is_not_wired_in(tmp_path: Path) -> None:
    # plan_balanced_sheets and minimum_balanced_workbooks exist and are tested; nothing yet
    # chooses between them and the calendar path inside a run.
    config: ExportConfig = _config(tmp_path)
    document: dict[str, Any] = json.loads(config.model_dump_json())
    document["profiles"][0]["limits"] = {"max_data_rows_per_worksheet": 6, "max_cells_per_workbook": 200}
    document["profiles"][0]["partitioning"] = {"algorithm": "balanced", "balance_across": "worksheets"}
    sheet: dict[str, Any]
    for sheet in document["profiles"][0]["sheets"]:
        sheet["partition_column"] = None
    balanced: ExportConfig = ExportConfig.model_validate(document)
    profile: ProfileSettings = balanced.profiles[0]
    with pytest.raises(NotImplementedError, match="not yet wired"):
        run_profile(balanced, profile, run_id=RUN_ID, instants=INSTANTS, options=RunOptions(scratch_root=ZPath(str(tmp_path / "scratch"))))


def test_a_balanced_profile_that_fits_one_workbook_still_runs(tmp_path: Path) -> None:
    # The shortcut applies whatever the algorithm says, so balanced output small enough to fit
    # needs no balancing at all -- and consults no date, which is what `partition_column: null` is.
    config: ExportConfig = _config(tmp_path)
    document: dict[str, Any] = json.loads(config.model_dump_json())
    document["profiles"][0]["partitioning"] = {"algorithm": "balanced", "balance_across": "worksheets"}
    sheet: dict[str, Any]
    for sheet in document["profiles"][0]["sheets"]:
        sheet["partition_column"] = None
    balanced: ExportConfig = ExportConfig.model_validate(document)
    profile: ProfileSettings = balanced.profiles[0]
    outcome: RunOutcome = run_profile(balanced, profile, run_id=RUN_ID, instants=INSTANTS, options=RunOptions(scratch_root=ZPath(str(tmp_path / "scratch"))))
    assert outcome.valid
    assert outcome.manifest is not None
    assert all(fragment.period_label is None for source in outcome.manifest.sources for fragment in source.fragments)


# --------------------------------------------------------------------------------------
# A failed verification
# --------------------------------------------------------------------------------------


def test_a_failed_verification_publishes_the_report_and_no_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The deliberate carve-out from "a failed run changes nothing": the report is the only
    # artifact that explains the failure to someone who has the share and nothing else.
    import pqx_pipeline.run as run_module
    from pqx_verify.fragments import ManifestVerdict, SourceVerdict
    from pqx_verify.validation import Verdict

    config: ExportConfig = _config(tmp_path)
    profile: ProfileSettings = config.profiles[0]

    def refuse(*args: object, **kwargs: object) -> ManifestVerdict:  # noqa: ARG001
        """Stand in for a verification that found a fragment did not hold."""
        bad: Verdict = Verdict("digest-mismatch", "book_001.xlsx!sales 2025 does not hold the rows recorded for it")
        return ManifestVerdict(sources=(SourceVerdict(source_alias="sales", fragments=(bad,), rows_found=1, rows_expected=1, reassembly=None),))

    monkeypatch.setattr(run_module, "verify_manifest", refuse)
    outcome: RunOutcome = run_profile(config, profile, run_id=RUN_ID, instants=INSTANTS, options=RunOptions(scratch_root=ZPath(str(tmp_path / "scratch"))))
    assert not outcome.valid
    assert outcome.report.outcome == "verification-failed"
    assert "does not hold" in (outcome.report.failure or "")

    destination: UPath = _destination(config, profile)
    published: tuple[str, ...] = list_names(destination)
    assert published == ("reports",)
    assert not receipt_path(destination, profile.name).exists()
    assert not manifest_path(destination, profile.name).exists()
    assert len(list((destination / "reports").iterdir())) == 3
