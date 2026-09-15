"""Rendering a :class:`RunReport` as JSON, Markdown and HTML.

**Stdlib only, and no template engine.** A template dependency here would be inherited by
everything that reports, to save string building that a report this shape does not need. The cost
is that escaping is explicit rather than automatic, which is why every interpolation into HTML goes
through :func:`html.escape` and why there is a test that a source alias holding ``<script>`` comes
out as text.

All three renderings describe the same report and none of them consults a clock: the instant is a
field, so the same report rendered twice is the same bytes.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import datetime as dt

    from pqx_report.model import FragmentReport, RunReport

__all__ = ["OUTCOME_HEADLINES", "REPORT_SUFFIXES", "render_html", "render_json", "render_markdown", "report_filename"]

REPORT_SUFFIXES: Final[dict[str, str]] = {"json": ".report.json", "md": ".report.md", "html": ".report.html"}
"""The three files one run leaves in ``reports/``, keyed by the renderer that writes each."""

OUTCOME_HEADLINES: Final[dict[str, str]] = {
    "published": "Published",
    "verification-failed": "Verification failed - nothing published",
    "planning-failed": "Planning failed - nothing written",
    "write-failed": "Write failed - nothing published",
}
"""What each outcome says in one line, at the top, before any detail."""

STYLE: Final[str] = (
    "body{font:14px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:60rem;padding:0 1rem;color:#1a1a1a}"
    "h1{font-size:1.5rem;margin-bottom:.25rem}h2{font-size:1.1rem;margin-top:2rem}"
    "table{border-collapse:collapse;width:100%;margin:.5rem 0}"
    "th,td{border:1px solid #ddd;padding:.35rem .6rem;text-align:left;vertical-align:top}"
    "th{background:#f5f5f5;font-weight:600}code{background:#f5f5f5;padding:.1rem .3rem;border-radius:3px}"
    "dl{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem;margin:0}dt{font-weight:600}dd{margin:0}"
    ".failed{color:#a11}.ok{color:#161}"
)
"""Inlined rather than linked: a report is copied to a share and opened from there, where a
stylesheet beside it would be one more file for reconciliation to reason about."""


def report_filename(profile: str, generated_utc: dt.datetime, suffix: str) -> str:
    """Return the basename one rendering takes under ``reports/``.

    ``<profile>-<utc>.report.<ext>``, with the instant spelled without colons: a name carrying
    ``12:00:00`` is refused on Windows and SMB, which is exactly where these land.

    Args:
        profile: The profile name.
        generated_utc: The instant the report describes itself as generated at, in UTC.
        suffix: One of :data:`REPORT_SUFFIXES`' keys.

    Returns:
        The basename.

    Raises:
        KeyError: ``suffix`` names no rendering.
    """
    stamp: str = generated_utc.strftime("%Y%m%dT%H%M%SZ")
    return f"{profile}-{stamp}{REPORT_SUFFIXES[suffix]}"


def render_json(report: RunReport) -> str:
    """Return the report as JSON, indented for reading.

    Indented rather than compact for the same reason the sidecar is: a report is read by people at
    least as often as by programs, and the cost is whitespace.

    Args:
        report: The report.

    Returns:
        JSON text, newline-terminated.
    """
    return report.model_dump_json(indent=2) + "\n"


def _duration(report: RunReport) -> str:
    """Return how long the run took, to the second."""
    seconds: int = int((report.finished_utc - report.started_utc).total_seconds())
    minutes: int
    remainder: int
    minutes, remainder = divmod(max(0, seconds), 60)
    return f"{minutes}m {remainder}s" if minutes else f"{remainder}s"


def _source_rows(report: RunReport) -> list[tuple[str, ...]]:
    """Return the Sources table's rows, in manifest order."""
    return [(source.alias, f"{source.rows:,}", str(len(source.fragments)), ", ".join(source.rebuilt_because) or "-", "yes" if source.valid else "no") for source in report.sources]


def _workbook_rows(report: RunReport) -> list[tuple[str, ...]]:
    """Return the Workbooks table's rows, in output order."""
    return [(workbook.filename, str(workbook.sheets), f"{workbook.rows:,}", f"{workbook.cells:,}") for workbook in report.workbooks]


def _failure_rows(failed: tuple[FragmentReport, ...]) -> list[tuple[str, ...]]:
    """Return the failed-fragment table's rows."""
    return [(fragment.workbook, fragment.sheet_name, fragment.verdict, fragment.detail) for fragment in failed]


def _markdown_table(headings: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    """Return a Markdown table, or a single line saying there is nothing in it."""
    if not rows:
        return ["_None._", ""]
    lines: list[str] = ["| " + " | ".join(headings) + " |", "| " + " | ".join("---" for _ in headings) + " |"]
    lines.extend("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows)
    lines.append("")
    return lines


def render_markdown(report: RunReport) -> str:
    """Return the report as Markdown.

    Leads with the outcome and, when the run failed, with what failed -- a reader who opens this
    because something went wrong should not have to scroll past a summary of what went right.

    Args:
        report: The report.

    Returns:
        Markdown text, newline-terminated.
    """
    lines: list[str] = [
        f"# {report.profile} - {OUTCOME_HEADLINES[report.outcome]}",
        "",
        f"- **Run** `{report.run_id}`",
        f"- **Generated** {report.generated_utc.isoformat()}",
        f"- **Took** {_duration(report)}",
        f"- **Destination** `{report.destination}`",
        f"- **Configuration** `{report.config_path}` (`{report.config_id}`)",
        f"- **Resolved config hash** `{report.resolved_config_hash}`",
        f"- **Planner** `{report.planner_version}`",
        "",
    ]
    if report.failure:
        lines += ["## What failed", "", report.failure, ""]
    failed: tuple[FragmentReport, ...] = report.failed_fragments
    if failed:
        lines += ["## Fragments that did not hold", ""]
        lines += _markdown_table(("Workbook", "Sheet", "Verdict", "Detail"), _failure_rows(failed))
    lines += ["## Sources", ""]
    lines += _markdown_table(("Source", "Rows", "Fragments", "Rebuilt because", "Verified"), _source_rows(report))
    lines += ["## Workbooks", ""]
    lines += _markdown_table(("Workbook", "Sheets", "Rows", "Cells"), _workbook_rows(report))
    lines += [f"**Total** {report.total_rows:,} rows in {report.total_cells:,} cells across {len(report.workbooks)} workbook(s).", ""]
    if report.deleted:
        lines += ["## Removed by reconciliation", "", *[f"- `{name}`" for name in report.deleted], ""]
    if report.notes:
        lines += ["## Notes", "", *[f"- {note}" for note in report.notes], ""]
    return "\n".join(lines) + "\n"


def _html_table(headings: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    """Return an HTML table, or a paragraph saying there is nothing in it.

    Every cell goes through ``escape``. The values here are source aliases, sheet names and file
    paths -- all of them user-supplied, none of them trusted to be free of ``<``.
    """
    if not rows:
        return "<p><em>None.</em></p>"
    head: str = "".join(f"<th>{escape(heading)}</th>" for heading in headings)
    body: str = "".join("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(report: RunReport) -> str:
    """Return the report as a self-contained HTML page.

    Self-contained on purpose. It is opened from a share by someone who has that share and nothing
    else, so it carries its own styles and links to nothing.

    Args:
        report: The report.

    Returns:
        HTML text, newline-terminated.
    """
    headline: str = OUTCOME_HEADLINES[report.outcome]
    status: str = "ok" if report.valid else "failed"
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{escape(report.profile)} - {escape(headline)}</title>",
        f"<style>{STYLE}</style></head><body>",
        f'<h1 class="{status}">{escape(report.profile)} - {escape(headline)}</h1>',
        "<dl>",
        f"<dt>Run</dt><dd><code>{escape(report.run_id)}</code></dd>",
        f"<dt>Generated</dt><dd>{escape(report.generated_utc.isoformat())}</dd>",
        f"<dt>Took</dt><dd>{escape(_duration(report))}</dd>",
        f"<dt>Destination</dt><dd><code>{escape(report.destination)}</code></dd>",
        f"<dt>Configuration</dt><dd><code>{escape(report.config_path)}</code> (<code>{escape(report.config_id)}</code>)</dd>",
        f"<dt>Resolved config hash</dt><dd><code>{escape(report.resolved_config_hash)}</code></dd>",
        f"<dt>Planner</dt><dd><code>{escape(report.planner_version)}</code></dd>",
        "</dl>",
    ]
    if report.failure:
        parts.append(f'<h2>What failed</h2><p class="failed">{escape(report.failure)}</p>')
    failed: tuple[FragmentReport, ...] = report.failed_fragments
    if failed:
        parts.append("<h2>Fragments that did not hold</h2>")
        parts.append(_html_table(("Workbook", "Sheet", "Verdict", "Detail"), _failure_rows(failed)))
    parts.append("<h2>Sources</h2>")
    parts.append(_html_table(("Source", "Rows", "Fragments", "Rebuilt because", "Verified"), _source_rows(report)))
    parts.append("<h2>Workbooks</h2>")
    parts.append(_html_table(("Workbook", "Sheets", "Rows", "Cells"), _workbook_rows(report)))
    parts.append(f"<p><strong>Total</strong> {report.total_rows:,} rows in {report.total_cells:,} cells across {len(report.workbooks)} workbook(s).</p>")
    if report.deleted:
        parts.append("<h2>Removed by reconciliation</h2><ul>" + "".join(f"<li><code>{escape(name)}</code></li>" for name in report.deleted) + "</ul>")
    if report.notes:
        parts.append("<h2>Notes</h2><ul>" + "".join(f"<li>{escape(note)}</li>" for note in report.notes) + "</ul>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"
