"""The command line: eight verbs over one profile's lifecycle.

    pqx --config export.json <verb> [--profile NAME ...] [--force] [--dry-run]
        [--scratch-root PATH] [--keep-scratch]

**Every verb has a distinct job**, which is the design rule the whole module follows. Offering
``plan`` and ``write`` as "``run``, but stop here" would put a second statement of the pipeline
next to the first; instead they call the very code ``run`` calls, through
:func:`pqx_pipeline.run.preview_profile`, so what they show is what a run would do rather than what
a second implementation believes it would do.

The verbs divide cleanly into three groups:

- **Destination-side inspectors** -- ``status``, ``verify`` and ``report`` -- which read the output
  directory, open no Parquet, and stage nothing. ``verify`` in particular is the promise
  ``pqx-verify`` makes made available at a terminal: it checks a published export against the
  published manifest, with the sources unreachable and the scratch directory long gone.
- **Source-side previews** -- ``plan`` and ``write`` -- which stage, describe, arrange and plan,
  and publish nothing. Their sidecars go into scratch rather than to the configured location, so
  running one does not quietly tell the *next* run that a source has been described.
- **Writers** -- ``sidecar``, ``run`` and ``publish``. ``sidecar`` does the one half of a run that
  stands alone, because a sidecar marks *described* and never *exported*. ``run`` honours selection
  and is the verb a scheduler invokes; ``publish`` is ``run --force`` under a name that says what
  an operator means by it.

**Exit codes are three-valued on purpose.** ``0`` the command did its job and the answer was good,
``1`` it ran and the answer was bad -- verification failed, a run did not publish -- and ``3`` it
refused before doing anything, because the configuration would not load or the destination belongs
to someone else. ``2`` is argparse's own usage error. A script can therefore tell "your export is
broken" from "your invocation is broken" without reading the text. **Staleness is not a failure**:
``status`` exits ``0`` whether or not there is work to do, because "there is work to do" is the
answer it was asked for.

Output goes to an injected stream rather than through ``print``, which makes every verb testable by
reading a string instead of by capturing a file descriptor.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import TYPE_CHECKING, Final

from pqx_common.paths import ZPath
from pqx_plan.config import ExportConfig
from pqx_plan.manifest import RunManifest
from pqx_plan.receipt import RunReceipt
from pqx_plan.semantics import ConfigSemanticError, validate_config
from pqx_report.model import RunReport
from pqx_report.render import render_markdown
from pqx_sidecar.document import SidecarDocument
from pqx_staging.errors import StagingError
from pqx_verify.fragments import ManifestVerdict, verify_manifest
from pydantic import ValidationError

from pqx_pipeline.locations import REPORT_JSON_GLOB, manifest_path, reports_directory
from pqx_pipeline.run import OwnershipError, Preview, RunInstants, RunOptions, RunOutcome, describe_sources, preview_profile, run_profile, select_profile
from pqx_pipeline.staleness import ProfileStaleness, SourceStaleness, excel_record, read_receipt

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from typing import TextIO

    import polars as pl
    from pqx_frame.metadata.columns import DataframeColumnsMetadata
    from pqx_plan.config import ProfileSettings
    from pqx_plan.naming import NamedSheet, NamedWorkbook
    from pqx_verify.fragments import SourceVerdict
    from pqx_verify.validation import Verdict
    from upath import UPath

    from pqx_pipeline.write import WrittenWorkbook

__all__ = ["EXIT_BAD", "EXIT_OK", "EXIT_REFUSED", "EXIT_USAGE", "VERBS", "Invocation", "build_parser", "load_config", "main", "run_identifier"]

EXIT_OK: Final[int] = 0
EXIT_BAD: Final[int] = 1
EXIT_USAGE: Final[int] = 2
EXIT_REFUSED: Final[int] = 3
"""``2`` is argparse's, and is listed here so nothing else claims it."""


def run_identifier(profile: str, started: dt.datetime) -> str:
    """Return the identifier one invocation records and names its scratch directory with.

    ``<utc>-<profile>``, with the instant spelled without colons: it becomes a path segment, and a
    segment carrying ``12:00:00`` is refused on Windows and on SMB.

    Args:
        profile: The profile being run.
        started: When the invocation started, in UTC.

    Returns:
        The identifier.
    """
    return f"{started.astimezone(dt.UTC):%Y-%m-%dT%H-%M-%SZ}-{profile}"


def load_config(path: UPath) -> ExportConfig:
    """Read, validate and semantically check a configuration file.

    Both halves, because they refuse different things: the models refuse a document Pydantic cannot
    build, and :func:`validate_config` refuses one that builds and then means something impossible
    -- a sheet naming a source that is not declared, two profiles claiming one directory, a
    template reaching out of the destination.

    Args:
        path: Where the configuration lives.

    Returns:
        The validated configuration.

    Raises:
        FileNotFoundError: There is no file there.
        ValidationError: The document is not a configuration.
        ConfigSemanticError: It is a configuration that cannot be run.
    """
    return validate_config(ExportConfig.model_validate_json(path.read_text(encoding="utf-8")))


class Invocation:
    """One resolved command line: what to run, over what, with which switches.

    Not a dataclass, because it is assembled once and read everywhere; a class with an explicit
    ``__init__`` keeps the annotations where the declaration checker wants them and costs nothing.
    """

    config: ExportConfig
    config_path: UPath
    profiles: tuple[ProfileSettings, ...]
    options: RunOptions
    stream: TextIO
    now: dt.datetime

    def __init__(self, *, config: ExportConfig, config_path: UPath, profiles: tuple[ProfileSettings, ...], options: RunOptions, stream: TextIO, now: dt.datetime) -> None:
        """Store the resolved invocation.

        Args:
            config: The validated configuration.
            config_path: Where it was read from.
            profiles: The profiles this invocation acts on, in configuration order.
            options: The switches given.
            stream: Where output goes.
            now: The instant every timestamp in this invocation derives from.
        """
        self.config = config
        self.config_path = config_path
        self.profiles = profiles
        self.options = options
        self.stream = stream
        self.now = now

    def say(self, text: str) -> None:
        """Write one line of output.

        Args:
            text: The line, without its newline.
        """
        self.stream.write(f"{text}\n")

    def destination(self, profile: ProfileSettings) -> UPath:
        """Return one profile's output directory.

        Args:
            profile: The profile.

        Returns:
            The directory a run would publish into.
        """
        return ZPath(self.config.output_directory) / profile.output_subdirectory

    def instants(self) -> RunInstants:
        """Return the instants this invocation stamps.

        One moment for all four: a command-line run is short enough that four readings of the clock
        would differ only in ways that make two runs' records incomparable for no gain.

        Returns:
            The instants.
        """
        return RunInstants.at(self.now)


def _status(invocation: Invocation, profile: ProfileSettings) -> int:
    """Report what selection decides for one profile, over remote metadata only."""
    destination: UPath = invocation.destination(profile)
    staleness: ProfileStaleness = select_profile(invocation.config, profile, destination, forced=invocation.options.force)
    receipt: RunReceipt | None = read_receipt(destination, profile.name)
    invocation.say(f"profile {profile.name!r} -> {destination}")
    invocation.say(f"  last exported: {'never' if receipt is None else receipt.excel_created_utc.isoformat()}")
    invocation.say(f"  verdict: {'stale' if staleness.stale else 'fresh'}")
    if staleness.stale:
        invocation.say(f"  because: {', '.join(staleness.reasons)}")
    source: SourceStaleness
    for source in staleness.sources:
        invocation.say(f"    source {source.alias}: {', '.join(source.signals) if source.signals else 'fresh'}")
    return EXIT_OK


def _sidecar(invocation: Invocation, profile: ProfileSettings) -> int:
    """Re-describe one profile's sources, and publish nothing."""
    if invocation.options.dry_run:
        invocation.say(f"profile {profile.name!r}: would describe {', '.join(sorted({sheet.source for sheet in profile.sheets}))}")
        return EXIT_OK
    written: dict[str, UPath] = describe_sources(invocation.config, profile, run_id=run_identifier(profile.name, invocation.now), instants=invocation.instants(), options=invocation.options)
    invocation.say(f"profile {profile.name!r}: described {len(written)} source(s)")
    alias: str
    path: UPath
    for alias, path in written.items():
        invocation.say(f"  {alias} -> {path}")
    return EXIT_OK


def _plan(invocation: Invocation, profile: ProfileSettings) -> int:
    """Show the workbooks and sheets a run would produce, and produce none of them."""
    return _preview(invocation, profile, write=False)


def _write(invocation: Invocation, profile: ProfileSettings) -> int:
    """Write the workbooks into scratch without publishing, for inspection under ``--keep-scratch``."""
    return _preview(invocation, profile, write=True)


def _preview(invocation: Invocation, profile: ProfileSettings, *, write: bool) -> int:
    """Run the shared body of ``plan`` and ``write`` and report what it produced."""
    if invocation.options.dry_run:
        invocation.say(f"profile {profile.name!r}: would {'write' if write else 'plan'} into scratch; nothing staged")
        return EXIT_OK
    preview: Preview = preview_profile(invocation.config, profile, run_id=run_identifier(profile.name, invocation.now), instants=invocation.instants(), options=invocation.options, write=write)
    invocation.say(f"profile {profile.name!r} -> {preview.destination}")
    invocation.say(f"  selection: {'stale' if preview.staleness.stale else 'fresh'}{'' if not preview.staleness.stale else ' because ' + ', '.join(preview.staleness.reasons)}")
    by_filename: Mapping[str, WrittenWorkbook] = {book.filename: book for book in preview.produced.written}
    workbook: NamedWorkbook
    for workbook in preview.produced.named:
        actual: WrittenWorkbook | None = by_filename.get(workbook.filename)
        suffix: str = "" if actual is None else f"  [{actual.rows} rows, {actual.cells} cells]"
        invocation.say(f"  {workbook.filename}{suffix}")
        sheet: NamedSheet
        for sheet in workbook.sheets:
            invocation.say(f"    {sheet.name}  ({sheet.sheet.source_alias}, {sheet.sheet.rows} rows)")
    if invocation.options.keep_scratch:
        invocation.say(f"  scratch kept at {preview.scratch}")
    return EXIT_OK


def _schema_of(sidecar: UPath) -> dict[str, pl.DataType]:
    """Return the dtypes of the frame that was written, read back from a published sidecar.

    Raises:
        ValueError: The sidecar carries no ToExcel record, so it does not describe what was
            written and the fragments cannot be read back as the manifest describes them.
    """
    document: SidecarDocument = SidecarDocument.model_validate_json(sidecar.read_text(encoding="utf-8"))
    record: DataframeColumnsMetadata | None = excel_record(document)
    if record is None:
        message: str = f"{sidecar} carries no ToExcel record, so it does not describe the frame that was written"
        raise ValueError(message)
    return {column.name: column.dtype.to_polars() for column in record.columns}


def _verify(invocation: Invocation, profile: ProfileSettings) -> int:
    """Check a published export against the manifest published beside it.

    No Parquet is opened and nothing is staged: the manifest records what every sheet should hash
    to, and the sidecars record the dtypes to read them back with, which is the whole point of
    having written both.
    """
    destination: UPath = invocation.destination(profile)
    path: UPath = manifest_path(destination, profile.name)
    if not path.exists():
        invocation.say(f"profile {profile.name!r}: no manifest at {path}; there is nothing published to check")
        return EXIT_REFUSED
    manifest: RunManifest = RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
    schemas: dict[str, dict[str, pl.DataType]] = {source.source_alias: _schema_of(destination / source.sidecar_path) for source in manifest.sources}
    verdict: ManifestVerdict = verify_manifest(destination, manifest, schemas)

    invocation.say(f"profile {profile.name!r} -> {destination}")
    checked: SourceVerdict
    for checked in verdict.sources:
        invocation.say(f"  {checked.source_alias}: {'valid' if checked.valid else 'INVALID'}  ({checked.rows_found}/{checked.rows_expected} rows, {len(checked.fragments)} fragment(s))")
        failure: Verdict
        for failure in (entry for entry in checked.fragments if not entry.valid):
            invocation.say(f"    {failure.kind}: {failure.detail}")
        if checked.reassembly is not None and not checked.reassembly.valid:
            invocation.say(f"    {checked.reassembly.kind}: {checked.reassembly.detail}")
    invocation.say(f"  verdict: {'valid' if verdict.valid else 'INVALID'}")
    return EXIT_OK if verdict.valid else EXIT_BAD


def _report(invocation: Invocation, profile: ProfileSettings) -> int:
    """Print the most recent published report for one profile.

    The JSON rendering is the one read back rather than the Markdown, because it is the report
    itself; the Markdown is then re-rendered from it, so what appears here is produced by the same
    renderer a run published and cannot drift from it.
    """
    directory: UPath = reports_directory(invocation.destination(profile))
    if not directory.exists():
        invocation.say(f"profile {profile.name!r}: no reports at {directory}")
        return EXIT_REFUSED
    # Sorted by name, which sorts by the UTC stamp the filename carries: the stamp is fixed-width
    # and zero-padded, so lexicographic order is chronological order without parsing anything.
    found: list[UPath] = sorted(directory.glob(REPORT_JSON_GLOB.format(profile=profile.name)), key=lambda entry: entry.name)
    if not found:
        invocation.say(f"profile {profile.name!r}: no reports at {directory}")
        return EXIT_REFUSED
    report: RunReport = RunReport.model_validate_json(found[-1].read_text(encoding="utf-8"))
    invocation.say(render_markdown(report))
    return EXIT_OK if report.valid else EXIT_BAD


def _run(invocation: Invocation, profile: ProfileSettings) -> int:
    """Run one profile end to end, honouring selection."""
    outcome: RunOutcome = run_profile(invocation.config, profile, run_id=run_identifier(profile.name, invocation.now), instants=invocation.instants(), options=invocation.options)
    invocation.say(f"profile {profile.name!r} -> {invocation.destination(profile)}")
    invocation.say(f"  outcome: {outcome.report.outcome}")
    note: str
    for note in outcome.report.notes:
        invocation.say(f"  note: {note}")
    if outcome.report.failure is not None:
        invocation.say(f"  failure: {outcome.report.failure}")
    name: str
    for name in outcome.published:
        invocation.say(f"  published {name}")
    for name in outcome.deleted:
        invocation.say(f"  deleted {name}")
    return EXIT_OK if outcome.valid else EXIT_BAD


def _publish(invocation: Invocation, profile: ProfileSettings) -> int:
    """Run one profile end to end, regardless of what selection says.

    ``run`` is what a scheduler invokes and skips a profile nothing has changed under; this is what
    an operator invokes when they want the export rebuilt now and are not interested in being told
    it was unnecessary.
    """
    forced: Invocation = Invocation(
        config=invocation.config,
        config_path=invocation.config_path,
        profiles=invocation.profiles,
        options=RunOptions(
            force=True,
            dry_run=invocation.options.dry_run,
            scratch_root=invocation.options.scratch_root,
            keep_scratch=invocation.options.keep_scratch,
            output_allowance=invocation.options.output_allowance,
        ),
        stream=invocation.stream,
        now=invocation.now,
    )
    return _run(forced, profile)


type Verb = Callable[[Invocation, ProfileSettings], int]
"""One command, applied to one profile, returning that profile's exit code."""

VERBS: Final[dict[str, Verb]] = {
    "status": _status,
    "sidecar": _sidecar,
    "plan": _plan,
    "write": _write,
    "verify": _verify,
    "report": _report,
    "publish": _publish,
    "run": _run,
}
"""In lifecycle order rather than alphabetical, because that is the order they are learned in."""


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser.

    ``argparse`` rather than a third-party framework: eight verbs sharing five switches is what it
    is for, it ships typed with the standard library, and a dependency here would be one every
    package in the workspace inherits through ``pqx-pipeline``.

    Returns:
        The parser. Every verb takes the same switches, and the ones a verb cannot act on are
        reported rather than silently ignored -- ``--force`` under ``plan`` still changes what
        selection is asked, which is worth showing.
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(prog="pqx", description="Convert Parquet sources into verified Excel workbooks.")
    parser.add_argument("--config", required=True, help="the export configuration to read")
    parser.add_argument("--profile", action="append", default=None, metavar="NAME", help="act on this profile only; repeatable, and every profile by default")
    parser.add_argument("--force", action="store_true", help="treat the profile as stale regardless of what selection says")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen and change nothing")
    parser.add_argument("--scratch-root", default=None, metavar="PATH", help="work here instead of the platform temporary directory")
    parser.add_argument("--keep-scratch", action="store_true", help="leave the scratch directory in place, for inspecting a failed run")
    parser.add_argument("verb", choices=list(VERBS), help="what to do")
    return parser


def _chosen_profiles(config: ExportConfig, wanted: Sequence[str] | None) -> tuple[ProfileSettings, ...]:
    """Return the profiles to act on, in configuration order.

    Raises:
        KeyError: A named profile is not in the configuration. Refused rather than skipped: a
            typo that silently acts on nothing looks exactly like a profile with no work to do.
    """
    if wanted is None:
        return tuple(config.profiles)
    known: Mapping[str, ProfileSettings] = {profile.name: profile for profile in config.profiles}
    missing: list[str] = sorted({name for name in wanted if name not in known})
    if missing:
        message: str = f"the configuration has no profile(s) {missing}; it has {sorted(known)}"
        raise KeyError(message)
    return tuple(profile for profile in config.profiles if profile.name in set(wanted))


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None, now: dt.datetime | None = None) -> int:
    """Run one command line and return its exit code.

    Args:
        argv: The arguments, without the program name. ``None`` takes ``sys.argv``.
        stream: Where output goes. ``None`` takes standard output.
        now: The instant to stamp. ``None`` reads the clock, which is the only place this module
            does; injecting it is what lets a test assert on a run identifier.

    Returns:
        The exit code: the worst any profile produced.
    """
    parsed: argparse.Namespace = build_parser().parse_args(argv)
    out: TextIO = stream if stream is not None else sys.stdout
    moment: dt.datetime = now if now is not None else dt.datetime.now(tz=dt.UTC)
    config_path: UPath = ZPath(str(parsed.config))

    config: ExportConfig
    profiles: tuple[ProfileSettings, ...]
    try:
        config = load_config(config_path)
        profiles = _chosen_profiles(config, parsed.profile)
    except (FileNotFoundError, KeyError, ValidationError, ConfigSemanticError) as refusal:
        out.write(f"pqx: {refusal}\n")
        return EXIT_REFUSED

    invocation: Invocation = Invocation(
        config=config,
        config_path=config_path,
        profiles=profiles,
        options=RunOptions(force=bool(parsed.force), dry_run=bool(parsed.dry_run), scratch_root=None if parsed.scratch_root is None else ZPath(str(parsed.scratch_root)), keep_scratch=bool(parsed.keep_scratch)),
        stream=out,
        now=moment,
    )
    verb: Verb = VERBS[str(parsed.verb)]

    worst: int = EXIT_OK
    profile: ProfileSettings
    for profile in profiles:
        # Each profile is refused on its own account. One destination owned by something else must
        # not stop the profiles that would have succeeded, or fixing a configuration becomes a
        # sequence of runs each revealing one more problem.
        try:
            worst = max(worst, verb(invocation, profile))
        except (OwnershipError, StagingError, NotImplementedError, ValueError) as refusal:
            invocation.say(f"pqx: profile {profile.name!r}: {refusal}")
            worst = max(worst, EXIT_REFUSED)
    return worst


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
