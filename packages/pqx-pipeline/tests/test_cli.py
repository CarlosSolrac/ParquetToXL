"""Frozen tests for the command line: what each verb does, and what it refuses to do.

Every test drives ``main`` with a real configuration file over real Parquet, and reads the answer
off the stream and the exit code. Nothing is mocked: a verb that claims to publish nothing is
tested by looking at the destination afterwards.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from pqx_common.paths import ZPath
from pqx_pipeline.cli import EXIT_BAD, EXIT_OK, EXIT_REFUSED, VERBS, build_parser, load_config, main, run_identifier
from pqx_pipeline.locations import manifest_path, receipt_path, reports_directory
from pqx_sidecar.store import sidecar_path

if TYPE_CHECKING:
    from pathlib import Path

    from pqx_plan.config import ExportConfig
    from upath import UPath

NOON: dt.datetime = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
"""Fixed, so a run identifier and a report filename are the same in every run of these tests."""


def _source(path: UPath, rows: int, *, start: dt.date = dt.date(2024, 1, 1)) -> None:
    """A Parquet file whose dates span two years."""
    pl.DataFrame(
        {
            "id": pl.Series(list(range(rows)), dtype=pl.Int64),
            "customer": pl.Series([f"c{index % 7}" for index in range(rows)], dtype=pl.String),
            "booked": pl.Series([start + dt.timedelta(days=index * 5) for index in range(rows)], dtype=pl.Date),
        },
    ).write_parquet(str(path))


def _document(tmp_path: Path) -> dict[str, Any]:
    """A two-source, one-profile configuration over real files under ``tmp_path``."""
    sales: UPath = ZPath(str(tmp_path / "sales.parquet"))
    returns: UPath = ZPath(str(tmp_path / "returns.parquet"))
    _source(sales, 40)
    _source(returns, 12, start=dt.date(2025, 2, 1))
    return {
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


def _configured(tmp_path: Path, **changes: Any) -> Path:  # noqa: ANN401
    """Write a configuration file and return where it is."""
    document: dict[str, Any] = _document(tmp_path)
    document.update(changes)
    path: Path = tmp_path / "export.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class _Invoked:
    """One completed ``main`` call: what it printed and what it returned."""

    code: int
    output: str

    def __init__(self, code: int, output: str) -> None:
        """Store the result.

        Args:
            code: The exit code.
            output: Everything written to the stream.
        """
        self.code = code
        self.output = output


def _invoke(tmp_path: Path, *arguments: str, config: Path | None = None, now: dt.datetime = NOON) -> _Invoked:
    """Run one command line against a configuration under ``tmp_path``."""
    where: Path = config if config is not None else _configured(tmp_path)
    stream: io.StringIO = io.StringIO()
    code: int = main(["--config", str(where), *arguments, "--scratch-root", str(tmp_path / "scratch")], stream=stream, now=now)
    return _Invoked(code, stream.getvalue())


def _destination(tmp_path: Path) -> UPath:
    """Where the one profile publishes."""
    return ZPath(str(tmp_path / "out" / "annual"))


# --------------------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------------------


def test_every_verb_the_specification_names_is_offered() -> None:
    assert list(VERBS) == ["status", "sidecar", "plan", "write", "verify", "report", "publish", "run"]


def test_an_unknown_verb_is_a_usage_error_rather_than_a_refusal(tmp_path: Path) -> None:
    # argparse exits 2 itself. The point of the assertion is that the CLI does not catch it and
    # turn a misspelled verb into something a script would read as "your data is broken".
    caught: pytest.ExceptionInfo[SystemExit]
    with pytest.raises(SystemExit) as caught:
        main(["--config", str(_configured(tmp_path)), "no-such-verb"], stream=io.StringIO())
    assert caught.value.code == 2


def test_the_configuration_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["status"])


def test_a_run_identifier_carries_no_colons() -> None:
    # It becomes a scratch path segment, and a segment carrying 12:00:00 is refused on Windows.
    identifier: str = run_identifier("annual", NOON)
    assert identifier == "2026-09-15T12-00-00Z-annual"
    assert ":" not in identifier


def test_a_run_identifier_is_stamped_in_utc_whatever_zone_it_is_given() -> None:
    # Mexico is UTC-6; the same instant must name the same run whichever clock recorded it.
    local: dt.datetime = NOON.astimezone(dt.timezone(-dt.timedelta(hours=6)))
    assert run_identifier("annual", local) == run_identifier("annual", NOON)


# --------------------------------------------------------------------------------------
# Loading a configuration
# --------------------------------------------------------------------------------------


def test_a_missing_configuration_is_refused_before_anything_is_staged(tmp_path: Path) -> None:
    stream: io.StringIO = io.StringIO()
    code: int = main(["--config", str(tmp_path / "absent.json"), "status"], stream=stream, now=NOON)
    assert code == EXIT_REFUSED
    assert "pqx:" in stream.getvalue()


def test_a_document_that_is_not_a_configuration_is_refused(tmp_path: Path) -> None:
    path: Path = tmp_path / "export.json"
    path.write_text('{"config_version": 1}', encoding="utf-8")
    assert _invoke(tmp_path, "status", config=path).code == EXIT_REFUSED


def test_a_configuration_that_builds_but_cannot_be_run_is_refused(tmp_path: Path) -> None:
    # A sheet naming an undeclared source: valid against the models, impossible to run.
    document: dict[str, Any] = _document(tmp_path)
    document["profiles"][0]["sheets"][0]["source"] = "not-declared"
    path: Path = tmp_path / "export.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    invoked: _Invoked = _invoke(tmp_path, "status", config=path)
    assert invoked.code == EXIT_REFUSED
    assert "not-declared" in invoked.output


def test_loading_returns_the_validated_configuration(tmp_path: Path) -> None:
    config: ExportConfig = load_config(ZPath(str(_configured(tmp_path))))
    assert [profile.name for profile in config.profiles] == ["annual"]


# --------------------------------------------------------------------------------------
# Choosing profiles
# --------------------------------------------------------------------------------------


def test_every_profile_is_acted_on_by_default(tmp_path: Path) -> None:
    assert "annual" in _invoke(tmp_path, "status").output


def test_a_named_profile_is_acted_on_alone(tmp_path: Path) -> None:
    invoked: _Invoked = _invoke(tmp_path, "status", "--profile", "annual")
    assert invoked.code == EXIT_OK
    assert "annual" in invoked.output


def test_an_unknown_profile_is_refused_rather_than_skipped(tmp_path: Path) -> None:
    # A typo that silently acts on nothing looks exactly like a profile with no work to do.
    invoked: _Invoked = _invoke(tmp_path, "status", "--profile", "quarterly")
    assert invoked.code == EXIT_REFUSED
    assert "quarterly" in invoked.output


# --------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------


def test_status_says_a_never_exported_profile_is_stale_and_why(tmp_path: Path) -> None:
    invoked: _Invoked = _invoke(tmp_path, "status")
    assert invoked.code == EXIT_OK
    assert "stale" in invoked.output
    assert "no-receipt" in invoked.output
    assert "never" in invoked.output


def test_status_names_each_source_and_its_own_signals(tmp_path: Path) -> None:
    output: str = _invoke(tmp_path, "status").output
    assert "source sales" in output
    assert "source returns" in output
    assert "sidecar-missing-or-unreadable" in output


def test_status_stages_nothing_and_publishes_nothing(tmp_path: Path) -> None:
    _invoke(tmp_path, "status")
    assert not _destination(tmp_path).exists()
    assert not sidecar_path(ZPath(str(tmp_path / "sales.parquet"))).exists()


def test_status_says_fresh_after_a_run_and_still_exits_zero(tmp_path: Path) -> None:
    # Staleness is information, not failure: a script asks status what to do, not whether it broke.
    config: Path = _configured(tmp_path)
    assert _invoke(tmp_path, "run", config=config).code == EXIT_OK
    invoked: _Invoked = _invoke(tmp_path, "status", config=config)
    assert invoked.code == EXIT_OK
    assert "fresh" in invoked.output
    assert "2026-09-15T12:00:00" in invoked.output


def test_status_under_force_reports_the_forced_signal(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    assert "forced" in _invoke(tmp_path, "status", "--force", config=config).output


# --------------------------------------------------------------------------------------
# sidecar
# --------------------------------------------------------------------------------------


def test_sidecar_describes_the_sources_and_publishes_nothing(tmp_path: Path) -> None:
    invoked: _Invoked = _invoke(tmp_path, "sidecar")
    assert invoked.code == EXIT_OK
    assert sidecar_path(ZPath(str(tmp_path / "sales.parquet"))).exists()
    assert sidecar_path(ZPath(str(tmp_path / "returns.parquet"))).exists()
    assert not _destination(tmp_path).exists()


def test_sidecar_leaves_the_export_stale_and_the_hashing_done(tmp_path: Path) -> None:
    # This is the state a run killed after its sidecars leaves behind. The next run must rebuild
    # the export only -- so the source's own signals are gone while the profile stays stale.
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "sidecar", config=config)
    output: str = _invoke(tmp_path, "status", config=config).output
    assert "stale" in output
    assert "no-receipt" in output
    assert "sidecar-missing-or-unreadable" not in output


def test_sidecar_under_dry_run_describes_nothing(tmp_path: Path) -> None:
    invoked: _Invoked = _invoke(tmp_path, "sidecar", "--dry-run")
    assert invoked.code == EXIT_OK
    assert "would describe" in invoked.output
    assert not sidecar_path(ZPath(str(tmp_path / "sales.parquet"))).exists()


# --------------------------------------------------------------------------------------
# plan and write
# --------------------------------------------------------------------------------------


def test_plan_names_the_workbooks_and_sheets_without_producing_them(tmp_path: Path) -> None:
    invoked: _Invoked = _invoke(tmp_path, "plan")
    assert invoked.code == EXIT_OK
    assert ".xlsx" in invoked.output
    assert "sales" in invoked.output
    assert not _destination(tmp_path).exists()


def test_plan_does_not_describe_the_sources_where_the_next_run_would_look(tmp_path: Path) -> None:
    # A preview's sidecars go into scratch. Writing one beside the source would tell the next run
    # the source had been described, which is a claim a command that publishes nothing must not make.
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "plan", config=config)
    assert not sidecar_path(ZPath(str(tmp_path / "sales.parquet"))).exists()
    assert "sidecar-missing-or-unreadable" in _invoke(tmp_path, "status", config=config).output


def test_plan_reports_what_selection_says(tmp_path: Path) -> None:
    assert "selection: stale" in _invoke(tmp_path, "plan").output


def test_write_reports_rows_and_cells_that_plan_cannot_know(tmp_path: Path) -> None:
    # Planning knows how many rows it intends; only writing knows what the file cost.
    planned: str = _invoke(tmp_path, "plan").output
    written: str = _invoke(tmp_path, "write").output
    assert "cells" not in planned
    assert "cells" in written


def test_write_publishes_nothing(tmp_path: Path) -> None:
    assert _invoke(tmp_path, "write").code == EXIT_OK
    assert not _destination(tmp_path).exists()


def test_write_keeps_the_workbooks_where_they_can_be_opened_when_asked(tmp_path: Path) -> None:
    invoked: _Invoked = _invoke(tmp_path, "write", "--keep-scratch")
    assert "scratch kept at" in invoked.output
    kept: UPath = ZPath(str(tmp_path / "scratch")) / f"run-{run_identifier('annual', NOON)}"
    assert sorted(entry.name for entry in kept.glob("*.xlsx"))


def test_write_removes_its_scratch_by_default(tmp_path: Path) -> None:
    _invoke(tmp_path, "write")
    assert not (ZPath(str(tmp_path / "scratch")) / f"run-{run_identifier('annual', NOON)}").exists()


@pytest.mark.parametrize("verb", ["plan", "write"])
def test_a_preview_under_dry_run_stages_nothing(verb: str, tmp_path: Path) -> None:
    invoked: _Invoked = _invoke(tmp_path, verb, "--dry-run")
    assert invoked.code == EXIT_OK
    assert "nothing staged" in invoked.output


# --------------------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------------------


def test_verify_checks_a_published_export_against_its_manifest(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    invoked: _Invoked = _invoke(tmp_path, "verify", config=config)
    assert invoked.code == EXIT_OK
    assert "verdict: valid" in invoked.output
    assert "sales: valid" in invoked.output


def test_verify_opens_no_parquet(tmp_path: Path) -> None:
    # The sources are deleted before verifying, which is the strongest form of the assertion:
    # a verify that needed them could not pass.
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    (tmp_path / "sales.parquet").unlink()
    (tmp_path / "returns.parquet").unlink()
    assert _invoke(tmp_path, "verify", config=config).code == EXIT_OK


def test_verify_refuses_where_nothing_has_been_published(tmp_path: Path) -> None:
    invoked: _Invoked = _invoke(tmp_path, "verify")
    assert invoked.code == EXIT_REFUSED
    assert "nothing published" in invoked.output


def test_verify_reports_a_mutated_workbook_as_bad_rather_than_as_a_refusal(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    destination: UPath = _destination(tmp_path)
    workbook: UPath = next(iter(sorted(destination.glob("*.xlsx"))))
    # Truncating is a mutation the reader cannot get past, which is what makes it a judgement
    # about the export rather than about the invocation.
    workbook.write_bytes(b"not a workbook")
    invoked: _Invoked = _invoke(tmp_path, "verify", config=config)
    assert invoked.code == EXIT_BAD
    assert "INVALID" in invoked.output


def test_verify_refuses_a_sidecar_that_does_not_describe_what_was_written(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    destination: UPath = _destination(tmp_path)
    published: UPath = destination / "sales.parquet.json"
    document: dict[str, Any] = json.loads(published.read_text(encoding="utf-8"))
    document["metadata"]["column_metadata_of_conversions"] = []
    published.write_text(json.dumps(document), encoding="utf-8")
    invoked: _Invoked = _invoke(tmp_path, "verify", config=config)
    assert invoked.code == EXIT_REFUSED
    assert "ToExcel" in invoked.output


# --------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------


def test_report_prints_the_most_recent_published_report(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    invoked: _Invoked = _invoke(tmp_path, "report", config=config)
    assert invoked.code == EXIT_OK
    assert "annual" in invoked.output
    assert "#" in invoked.output


def test_report_reads_the_newest_of_several(tmp_path: Path) -> None:
    # Lexicographic order over a fixed-width UTC stamp is chronological order, which is why the
    # filename carries the stamp at all.
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    later: dt.datetime = NOON + dt.timedelta(hours=1)
    _invoke(tmp_path, "publish", config=config, now=later)
    assert len(list(reports_directory(_destination(tmp_path)).glob("*.report.json"))) == 2
    assert "13:00" in _invoke(tmp_path, "report", config=config).output


def test_report_refuses_where_no_report_has_been_published(tmp_path: Path) -> None:
    invoked: _Invoked = _invoke(tmp_path, "report")
    assert invoked.code == EXIT_REFUSED
    assert "no reports" in invoked.output


def test_report_refuses_where_the_directory_exists_but_holds_no_report_for_this_profile(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    reports: UPath = reports_directory(_destination(tmp_path))
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "quarterly-20260915T120000Z.report.json").write_text("{}", encoding="utf-8")
    assert _invoke(tmp_path, "report", config=config).code == EXIT_REFUSED


def test_report_exits_bad_for_a_run_that_did_not_publish(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    reports: UPath = reports_directory(_destination(tmp_path))
    published: UPath = next(iter(sorted(reports.glob("*.report.json"))))
    document: dict[str, Any] = json.loads(published.read_text(encoding="utf-8"))
    document["outcome"] = "verification-failed"
    document["failure"] = "one fragment did not hold what the manifest records"
    published.write_text(json.dumps(document), encoding="utf-8")
    assert _invoke(tmp_path, "report", config=config).code == EXIT_BAD


# --------------------------------------------------------------------------------------
# run and publish
# --------------------------------------------------------------------------------------


def test_run_publishes_a_verified_export(tmp_path: Path) -> None:
    invoked: _Invoked = _invoke(tmp_path, "run")
    assert invoked.code == EXIT_OK
    assert "outcome: published" in invoked.output
    destination: UPath = _destination(tmp_path)
    assert manifest_path(destination, "annual").exists()
    assert receipt_path(destination, "annual").exists()
    assert sorted(destination.glob("*.xlsx"))


def test_run_names_what_it_published(tmp_path: Path) -> None:
    assert "published " in _invoke(tmp_path, "run").output


def test_a_second_run_stages_nothing_and_says_so(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    invoked: _Invoked = _invoke(tmp_path, "run", config=config)
    assert invoked.code == EXIT_OK
    assert "nothing was stale" in invoked.output


def test_publish_rebuilds_what_run_would_have_skipped(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    invoked: _Invoked = _invoke(tmp_path, "publish", config=config, now=NOON + dt.timedelta(hours=1))
    assert invoked.code == EXIT_OK
    assert "nothing was stale" not in invoked.output
    assert "published " in invoked.output


def test_run_under_force_matches_publish(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    invoked: _Invoked = _invoke(tmp_path, "run", "--force", config=config, now=NOON + dt.timedelta(hours=1))
    assert "nothing was stale" not in invoked.output


def test_run_under_dry_run_publishes_nothing(tmp_path: Path) -> None:
    invoked: _Invoked = _invoke(tmp_path, "run", "--dry-run")
    assert invoked.code == EXIT_OK
    assert "dry run" in invoked.output
    assert not _destination(tmp_path).exists()


def test_run_reconciles_and_names_what_it_deleted(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    stray: UPath = _destination(tmp_path) / "annual_old_001.xlsx"
    stray.write_bytes(b"left over from an earlier naming scheme")
    invoked: _Invoked = _invoke(tmp_path, "publish", config=config, now=NOON + dt.timedelta(hours=1))
    assert "deleted annual_old_001.xlsx" in invoked.output
    assert not stray.exists()


def test_a_destination_owned_by_another_configuration_is_refused(tmp_path: Path) -> None:
    config: Path = _configured(tmp_path)
    _invoke(tmp_path, "run", config=config)
    other: Path = _configured(tmp_path, config_id="cfg-2")
    other_path: Path = tmp_path / "other.json"
    other_path.write_text(other.read_text(encoding="utf-8"), encoding="utf-8")
    invoked: _Invoked = _invoke(tmp_path, "publish", config=other_path)
    assert invoked.code == EXIT_REFUSED
    assert "cfg-2" in invoked.output


def test_a_profile_that_cannot_be_planned_is_refused_without_stopping_the_others(tmp_path: Path) -> None:
    # `balanced` is planned and tested but not yet wired into a run. The refusal is per profile,
    # so a configuration with one such profile still exports the rest.
    document: dict[str, Any] = _document(tmp_path)
    document["profiles"][0]["limits"] = {"max_data_rows_per_worksheet": 6, "max_cells_per_workbook": 200}
    document["profiles"][0]["partitioning"] = {"algorithm": "balanced", "balance_across": "worksheets"}
    sheet: dict[str, Any]
    for sheet in document["profiles"][0]["sheets"]:
        sheet["partition_column"] = None
    path: Path = tmp_path / "export.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    invoked: _Invoked = _invoke(tmp_path, "run", config=path)
    assert invoked.code == EXIT_REFUSED
    assert "not yet wired" in invoked.output


def test_the_worst_profile_decides_the_exit_code(tmp_path: Path) -> None:
    # Two profiles, one of which has never been exported: `verify` refuses that one and passes
    # the other, and the command as a whole must report the refusal.
    document: dict[str, Any] = _document(tmp_path)
    second: dict[str, Any] = json.loads(json.dumps(document["profiles"][0]))
    second["name"] = "monthly"
    second["output_subdirectory"] = "monthly"
    document["profiles"].append(second)
    path: Path = tmp_path / "export.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert _invoke(tmp_path, "run", "--profile", "annual", config=path).code == EXIT_OK
    invoked: _Invoked = _invoke(tmp_path, "verify", config=path)
    assert invoked.code == EXIT_REFUSED
    assert "verdict: valid" in invoked.output


def test_the_default_scratch_root_is_used_when_none_is_given(tmp_path: Path) -> None:
    # The only test that omits --scratch-root, so the platform temporary directory is exercised.
    stream: io.StringIO = io.StringIO()
    code: int = main(["--config", str(_configured(tmp_path)), "plan"], stream=stream, now=NOON)
    assert code == EXIT_OK
    assert ".xlsx" in stream.getvalue()


def test_the_clock_is_read_when_no_instant_is_given(tmp_path: Path) -> None:
    # `now` is injected everywhere else so that a run identifier is assertable; this is the one
    # place the real clock is exercised, and all it has to do is produce a usable run.
    stream: io.StringIO = io.StringIO()
    assert main(["--config", str(_configured(tmp_path)), "status"], stream=stream) == EXIT_OK


def test_output_goes_to_standard_output_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(_configured(tmp_path)), "status"], now=NOON) == EXIT_OK
    assert "annual" in capsys.readouterr().out


def test_run_names_the_failure_when_verification_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A failed run publishes its report and no data, and the command must say what went wrong
    # rather than only that something did.
    import pqx_pipeline.run as run_module
    from pqx_verify.fragments import ManifestVerdict, SourceVerdict
    from pqx_verify.validation import Verdict

    def refuse(*args: object, **kwargs: object) -> ManifestVerdict:  # noqa: ARG001
        """Stand in for a verification that found a fragment did not hold."""
        bad: Verdict = Verdict("digest-mismatch", "annual_2025_001.xlsx!sales 2025 does not hold the rows recorded for it")
        return ManifestVerdict(sources=(SourceVerdict(source_alias="sales", fragments=(bad,), rows_found=1, rows_expected=1, reassembly=None),))

    monkeypatch.setattr(run_module, "verify_manifest", refuse)
    invoked: _Invoked = _invoke(tmp_path, "run")
    assert invoked.code == EXIT_BAD
    assert "outcome: verification-failed" in invoked.output
    assert "failure: annual_2025_001.xlsx!sales 2025" in invoked.output
    assert not sorted(_destination(tmp_path).glob("*.xlsx"))


def test_verify_reports_a_source_whose_fragments_no_longer_add_up(tmp_path: Path) -> None:
    # A fragment dropped from the manifest fails the row count and the reassembly while every
    # fragment that remains is individually valid -- so the reassembly verdict is the only thing
    # that names the problem.
    document: dict[str, Any] = _document(tmp_path)
    document["profiles"][0]["limits"] = {"max_data_rows_per_worksheet": 6, "max_cells_per_workbook": 10_000}
    config: Path = tmp_path / "export.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    _invoke(tmp_path, "run", config=config)
    published: UPath = manifest_path(_destination(tmp_path), "annual")
    manifest: dict[str, Any] = json.loads(published.read_text(encoding="utf-8"))
    source: dict[str, Any] = next(entry for entry in manifest["sources"] if len(entry["fragments"]) > 1)
    source["fragments"].pop()
    published.write_text(json.dumps(manifest), encoding="utf-8")
    invoked: _Invoked = _invoke(tmp_path, "verify", config=config)
    assert invoked.code == EXIT_BAD
    assert "do not reassemble" in invoked.output
