"""Frozen tests for the run report and its three renderings."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from pqx_report.model import REPORT_VERSION, FragmentReport, RunReport, SourceReport, WorkbookReport
from pqx_report.render import OUTCOME_HEADLINES, REPORT_SUFFIXES, render_html, render_json, render_markdown, report_filename
from pydantic import ValidationError

GENERATED: dt.datetime = dt.datetime(2026, 9, 15, 12, 5, 0, tzinfo=dt.UTC)
STARTED: dt.datetime = dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=dt.UTC)
FINISHED: dt.datetime = dt.datetime(2026, 9, 15, 12, 4, 30, tzinfo=dt.UTC)


def _fragment(verdict: str = "valid", **changes: Any) -> FragmentReport:  # noqa: ANN401
    """One fragment's outcome."""
    fields: dict[str, Any] = {"workbook": "annual_review_2025_001.xlsx", "sheet_name": "sales 2025", "rows": 100, "period_label": "2025", "verdict": verdict, "detail": "holds what it should"}
    fields.update(changes)
    return FragmentReport.model_validate(fields)


def _source(**changes: Any) -> SourceReport:  # noqa: ANN401
    """One source's contribution, verified."""
    fields: dict[str, Any] = {
        "alias": "sales",
        "path": "/data/sales.parquet",
        "rows": 100,
        "rebuilt_because": ("config-modified",),
        "fragments": (_fragment(),),
        "rows_expected": 100,
        "rows_found": 100,
        "reassembled": True,
    }
    fields.update(changes)
    return SourceReport.model_validate(fields)


def _report(**changes: Any) -> RunReport:  # noqa: ANN401
    """A published run, unless overridden."""
    fields: dict[str, Any] = {
        "outcome": "published",
        "profile": "annual_review",
        "run_id": "2026-09-15T12-00-00Z-annual_review",
        "generated_utc": GENERATED,
        "started_utc": STARTED,
        "finished_utc": FINISHED,
        "config_id": "0f7c1a94-3b52-4c8e-9a1d-6e2f0b5d7c33",
        "config_path": "/config/export-config.json",
        "destination": "az://exports/monthly/annual_review",
        "planner_version": "1",
        "resolved_config_hash": "2f1a" * 16,
        "sources": (_source(),),
        "workbooks": (WorkbookReport(filename="annual_review_2025_001.xlsx", sheets=1, rows=100, cells=1212),),
    }
    fields.update(changes)
    return RunReport.model_validate(fields)


# --------------------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------------------


def test_a_published_run_is_valid() -> None:
    assert _report().valid
    assert _report().report_version == REPORT_VERSION


@pytest.mark.parametrize("outcome", ["verification-failed", "planning-failed", "write-failed"])
def test_every_failure_outcome_is_not_valid(outcome: str) -> None:
    assert not _report(outcome=outcome, failure="something went wrong").valid


def test_the_report_totals_its_rows_and_cells() -> None:
    report: RunReport = _report(
        sources=(_source(alias="sales", rows=100), _source(alias="returns", rows=40)),
        workbooks=(WorkbookReport(filename="a.xlsx", sheets=1, rows=100, cells=1212), WorkbookReport(filename="b.xlsx", sheets=2, rows=40, cells=500)),
    )
    assert report.total_rows == 140
    assert report.total_cells == 1712


def test_failed_fragments_are_gathered_across_sources() -> None:
    report: RunReport = _report(
        sources=(
            _source(alias="sales", fragments=(_fragment(), _fragment("digest-mismatch", sheet_name="sales 2024"))),
            _source(alias="returns", fragments=(_fragment("columns-differ", sheet_name="returns 2025"),)),
        ),
    )
    assert [fragment.sheet_name for fragment in report.failed_fragments] == ["sales 2024", "returns 2025"]


def test_a_source_is_invalid_when_a_fragment_did_not_hold() -> None:
    assert not _source(fragments=(_fragment("digest-mismatch"),)).valid


def test_a_source_is_invalid_when_the_rows_do_not_add_up() -> None:
    assert not _source(rows_found=90, rows_expected=100).valid


def test_a_source_is_invalid_when_the_reassembly_failed() -> None:
    assert not _source(reassembled=False).valid


def test_a_source_is_valid_when_the_reassembly_was_skipped() -> None:
    # None means the fragments do not cover the source, so the sum was never expected to reach it.
    assert _source(reassembled=None).valid


def test_a_report_is_frozen() -> None:
    with pytest.raises(ValidationError):
        _report().outcome = "published"  # type: ignore[misc]


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        _report(unexpected="value")


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValidationError):
        _report(generated_utc=dt.datetime(2026, 9, 15, 12, 0))  # noqa: DTZ001


def test_a_verdict_this_build_does_not_know_still_loads() -> None:
    # A report is read long after the run; refusing to load one because it names a verdict a
    # later build introduced would lose exactly the record that explains an upgrade.
    assert _fragment("some-future-verdict").verdict == "some-future-verdict"


# --------------------------------------------------------------------------------------
# Filenames
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("suffix", "expected"), [("json", ".report.json"), ("md", ".report.md"), ("html", ".report.html")])
def test_each_rendering_has_its_own_name(suffix: str, expected: str) -> None:
    assert report_filename("annual_review", GENERATED, suffix) == f"annual_review-20260915T120500Z{expected}"


def test_a_report_filename_carries_no_colons() -> None:
    # These land on a Windows or SMB share, where a name holding 12:00:00 is refused.
    name: str = report_filename("annual_review", GENERATED, "json")
    assert ":" not in name
    assert set(REPORT_SUFFIXES) == {"json", "md", "html"}


def test_an_unknown_rendering_is_refused() -> None:
    with pytest.raises(KeyError):
        report_filename("annual_review", GENERATED, "pdf")


# --------------------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------------------


def test_the_json_rendering_round_trips() -> None:
    report: RunReport = _report()
    loaded: dict[str, Any] = json.loads(render_json(report))
    assert RunReport.model_validate(loaded) == report


def test_the_json_rendering_is_indented_and_newline_terminated() -> None:
    text: str = render_json(_report())
    assert text.endswith("\n")
    assert "\n  " in text


def test_the_same_report_renders_the_same_bytes_twice() -> None:
    # Nothing here reads a clock: the instant is a field.
    report: RunReport = _report()
    assert render_json(report) == render_json(report)
    assert render_markdown(report) == render_markdown(report)
    assert render_html(report) == render_html(report)


# --------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------


def test_the_markdown_leads_with_the_outcome() -> None:
    assert render_markdown(_report()).startswith("# annual_review - Published")


def test_the_markdown_leads_with_the_failure_when_there_is_one() -> None:
    # A reader who opens this because something went wrong should not scroll past what went right.
    text: str = render_markdown(_report(outcome="verification-failed", failure="one fragment did not hold"))
    assert text.index("## What failed") < text.index("## Sources")
    assert "one fragment did not hold" in text


def test_the_markdown_lists_failed_fragments_before_the_summary() -> None:
    report: RunReport = _report(outcome="verification-failed", failure="x", sources=(_source(fragments=(_fragment("digest-mismatch"),)),))
    text: str = render_markdown(report)
    assert text.index("## Fragments that did not hold") < text.index("## Sources")
    assert "digest-mismatch" in text


def test_a_clean_run_has_no_failure_sections() -> None:
    text: str = render_markdown(_report())
    assert "## What failed" not in text
    assert "## Fragments that did not hold" not in text


def test_the_markdown_names_why_each_source_was_rebuilt() -> None:
    # The spec asks for this by name: a run that rebuilt only because of the hash is one whose
    # author forgot to bump the timestamp, and that is worth seeing.
    assert "resolved-config-hash" in render_markdown(_report(sources=(_source(rebuilt_because=("resolved-config-hash",)),)))


def test_a_source_that_was_not_rebuilt_shows_a_dash() -> None:
    assert "| - |" in render_markdown(_report(sources=(_source(rebuilt_because=()),)))


def test_empty_tables_say_so_rather_than_rendering_a_header() -> None:
    text: str = render_markdown(_report(sources=(), workbooks=()))
    assert text.count("_None._") == 2


def test_a_pipe_in_a_value_does_not_break_the_table() -> None:
    assert r"a\|b" in render_markdown(_report(sources=(_source(alias="a|b"),)))


def test_reconciliation_deletions_are_listed() -> None:
    # Deleting from a shared folder is hard to undo, so the record of what went is part of the point.
    assert "old_001.xlsx" in render_markdown(_report(deleted=("old_001.xlsx",)))


def test_notes_are_listed_when_present() -> None:
    assert "took over a stale lease" in render_markdown(_report(notes=("took over a stale lease",)))


def test_a_run_with_nothing_to_note_has_no_notes_section() -> None:
    assert "## Notes" not in render_markdown(_report())


@pytest.mark.parametrize(
    ("finished", "expected"),
    [
        (dt.datetime(2026, 9, 15, 12, 0, 45, tzinfo=dt.UTC), "45s"),
        (dt.datetime(2026, 9, 15, 12, 4, 30, tzinfo=dt.UTC), "4m 30s"),
        (dt.datetime(2026, 9, 15, 11, 59, 0, tzinfo=dt.UTC), "0s"),
    ],
)
def test_the_duration_reads_as_minutes_and_seconds(finished: dt.datetime, expected: str) -> None:
    # The last case is a clock that went backwards mid-run; it reports zero rather than a
    # negative duration, which would be a distraction from whatever else went wrong.
    assert f"**Took** {expected}" in render_markdown(_report(finished_utc=finished))


# --------------------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------------------


def test_the_html_is_a_self_contained_page() -> None:
    # Opened from a share by someone who has that share and nothing else.
    text: str = render_html(_report())
    assert text.startswith("<!doctype html>")
    assert "<style>" in text
    assert "<link" not in text
    assert "<script" not in text


def test_the_html_escapes_every_user_supplied_value() -> None:
    hostile: str = "<script>alert(1)</script>"
    text: str = render_html(_report(sources=(_source(alias=hostile),), destination=hostile, profile=hostile))
    assert hostile not in text
    assert "&lt;script&gt;" in text


def test_the_html_marks_a_failed_run() -> None:
    assert 'class="failed"' in render_html(_report(outcome="write-failed", failure="the writer refused a sheet name"))
    assert 'class="ok"' in render_html(_report())


def test_the_html_shows_the_failure_and_the_failed_fragments() -> None:
    report: RunReport = _report(outcome="verification-failed", failure="one fragment did not hold", sources=(_source(fragments=(_fragment("digest-mismatch"),)),))
    text: str = render_html(report)
    assert "What failed" in text
    assert "Fragments that did not hold" in text
    assert "digest-mismatch" in text


def test_a_clean_html_run_has_no_failure_sections() -> None:
    text: str = render_html(_report())
    assert "What failed" not in text
    assert "Fragments that did not hold" not in text


def test_the_html_says_none_for_an_empty_table() -> None:
    assert render_html(_report(sources=(), workbooks=())).count("<em>None.</em>") == 2


def test_the_html_lists_deletions_and_notes() -> None:
    text: str = render_html(_report(deleted=("old_001.xlsx",), notes=("took over a stale lease",)))
    assert "Removed by reconciliation" in text
    assert "old_001.xlsx" in text
    assert "took over a stale lease" in text


def test_a_clean_html_run_lists_neither() -> None:
    text: str = render_html(_report())
    assert "Removed by reconciliation" not in text
    assert "Notes" not in text


@pytest.mark.parametrize("outcome", list(OUTCOME_HEADLINES))
def test_every_outcome_renders_in_all_three_formats(outcome: str) -> None:
    report: RunReport = _report(outcome=outcome, failure=None if outcome == "published" else "something went wrong")
    assert OUTCOME_HEADLINES[outcome] in render_markdown(report)
    assert OUTCOME_HEADLINES[outcome] in render_html(report)
    assert json.loads(render_json(report))["outcome"] == outcome
