"""``RunReport``: what one run of one profile did, and why.

The only artifact a failed run publishes. ``export-pipeline-spec.md`` makes that a deliberate
carve-out from "a failed run changes nothing", because the report is the only thing that explains a
failure to someone who has the share and nothing else -- so it has to stand on its own, without the
manifest, the receipt or the logs beside it.

**The timestamp is injected, never read from a clock here.** A model that stamped itself would make
every rendering of one run a different document, and would make a test of the renderers depend on
when it ran. ``pqx-pipeline`` passes the instant in, once.
"""

from __future__ import annotations

from typing import Literal

from pqx_frame.timestamps import UtcDatetime
from pydantic import BaseModel, Field, NonNegativeInt

__all__ = [
    "REPORT_VERSION",
    "FragmentReport",
    "Outcome",
    "RunReport",
    "SourceReport",
    "StalenessSignal",
    "WorkbookReport",
]

REPORT_VERSION: Literal[1] = 1
"""Independent of the manifest, receipt, sidecar, conversion and hasher versions.

A report is read by people and by whatever collects them; its shape can move for presentation
reasons that say nothing about the digest contract, and tying it to that contract would make a
heading change look like a version skew.
"""

type Outcome = Literal["published", "verification-failed", "planning-failed", "write-failed"]
"""How the run ended.

Four rather than two, because "it failed" is the least useful thing a report can say. The three
failures reach the destination at different points and call for different responses: a planning
failure means the configuration and the data disagree, a write failure means the export could not
be produced, and a verification failure means it was produced and does not hold what it should --
which is the one that says nothing was published rather than that nothing was attempted.
"""

type StalenessSignal = Literal[
    "no-receipt",
    "sidecar-missing-or-unreadable",
    "source-changed",
    "described-after-export",
    "config-modified",
    "resolved-config-hash",
    "conversion-version",
    "hasher-version",
    "planner-version",
    "forced",
]
"""Why a source or profile was rebuilt.

Recorded per source because the spec asks for it by name: both the ``config_modified_utc`` check
and the ``resolved_config_hash`` backstop are implemented, "and the report names which one
triggered, so a forgotten bump is visible rather than merely compensated for". A run that rebuilt
only because of the hash is a run whose author forgot to bump the timestamp, and that is worth
seeing rather than silently absorbing.
"""


class _ReportModel(BaseModel, extra="forbid", frozen=True):
    """Shared configuration for every model here.

    Frozen because a report describes a run that is over; a mutable one could be edited between
    being written as JSON and being rendered as HTML, and the two would disagree.

    Repeated as class keywords on every subclass rather than inherited: Pydantic carries the config
    down, but a type checker reads a non-frozen subclass of a frozen base as an error.
    """


class FragmentReport(_ReportModel, extra="forbid", frozen=True):
    """One sheet's outcome, as the report states it."""

    workbook: str
    sheet_name: str
    rows: NonNegativeInt
    period_label: str | None
    verdict: str
    """The verdict kind, as ``pqx-verify`` named it. A string rather than the ``VerdictKind``
    literal, so that a report written by a later build still loads here: a report is read long
    after the run, and refusing to load one because it names a verdict this build does not have
    would lose exactly the record that explains an upgrade."""

    detail: str


class SourceReport(_ReportModel, extra="forbid", frozen=True):
    """One source's contribution, and what verification said about it as a whole."""

    alias: str
    path: str
    rows: NonNegativeInt
    rebuilt_because: tuple[StalenessSignal, ...] = ()
    """Empty when the source was not rebuilt. Several may fire at once, and all are recorded --
    a run that rebuilt for four reasons is a different story from one that rebuilt for one."""

    fragments: tuple[FragmentReport, ...] = ()
    rows_expected: NonNegativeInt = 0
    rows_found: NonNegativeInt = 0
    reassembled: bool | None = None
    """``None`` when the reassembly check was skipped because the fragments do not cover the source."""

    @property
    def valid(self) -> bool:
        """Whether every fragment held, the rows added up, and the reassembly held."""
        return all(fragment.verdict == "valid" for fragment in self.fragments) and self.rows_found == self.rows_expected and self.reassembled is not False


class WorkbookReport(_ReportModel, extra="forbid", frozen=True):
    """One written workbook, named as it was published."""

    filename: str
    sheets: NonNegativeInt
    rows: NonNegativeInt
    cells: NonNegativeInt


class RunReport(_ReportModel, extra="forbid", frozen=True):
    """Everything one run of one profile did.

    Ordered so the head of the file answers the first question a reader has -- did it work, which
    profile, when -- before the detail that only matters once the answer is "no".
    """

    report_version: Literal[1] = 1
    outcome: Outcome
    profile: str
    run_id: str
    generated_utc: UtcDatetime
    """Injected by the caller. Nothing here reads a clock."""

    started_utc: UtcDatetime
    finished_utc: UtcDatetime
    config_id: str
    config_path: str
    destination: str
    planner_version: str
    resolved_config_hash: str
    sources: tuple[SourceReport, ...] = ()
    workbooks: tuple[WorkbookReport, ...] = ()
    deleted: tuple[str, ...] = ()
    """What reconciliation removed. Listed because deleting from a shared folder is hard to undo,
    so the record of what went is part of what the report is for."""

    failure: str | None = None
    """Set for every outcome but ``published``. A failure with no explanation is the one thing a
    report must never be."""

    notes: tuple[str, ...] = Field(default=())
    """Anything that did not stop the run but a reader should know -- a stale lease taken over, a
    source with no rows in any period."""

    @property
    def valid(self) -> bool:
        """Whether the run published a verified export."""
        return self.outcome == "published"

    @property
    def total_rows(self) -> int:
        """Return every data row written, across sources."""
        return sum(source.rows for source in self.sources)

    @property
    def total_cells(self) -> int:
        """Return every cell written, headers included, across workbooks."""
        return sum(workbook.cells for workbook in self.workbooks)

    @property
    def failed_fragments(self) -> tuple[FragmentReport, ...]:
        """Return every fragment whose verdict was not ``valid``, for a renderer to lead with."""
        return tuple(fragment for source in self.sources for fragment in source.fragments if fragment.verdict != "valid")
