"""One profile's run, in the order the order matters.

    select -> stage -> describe -> plan -> write -> drop staged Parquet -> VERIFY -> publish

**Verify before publish.** Nothing reaches the destination until every fragment has passed. It also
makes verification cheap, because it reads from local scratch rather than over SMB or Azure.

**The staged Parquet is deleted before verification**, not merely dropped from memory. That frees
scratch where the run needs it most, and makes verification's independence structural rather than
asserted: the source is physically absent while it runs.

**Publication order is load-bearing.** Ownership check, acquire lease, sidecars, workbooks,
reconcile delete, manifest, receipt, report, release lease. The receipt is the "this export
completed" marker, so nothing may precede it that a later step could invalidate. Sidecars go early
on purpose: they mark only *described*, so a failure after them costs the export rather than the
hashing.

**A failed run publishes its report and nothing else.** That is a deliberate carve-out from "a
failed run changes nothing", because the report is the only artifact that explains the failure to
someone who has the share and nothing else.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pqx_common.paths import ZPath
from pqx_plan.allocate import AllocatedWorkbook, allocate_workbooks, order_profile_sheets
from pqx_plan.capacity import SourceShape, fits_one_workbook
from pqx_plan.config import CalendarPartitioning, ExportConfig, Partitioning, ProfileSettings, SheetSettings
from pqx_plan.fingerprint import resolved_config_hash
from pqx_plan.manifest import RunManifest, SourceFragments
from pqx_plan.naming import NamedWorkbook, name_workbooks
from pqx_plan.partition import PlannedSheet, plan_calendar_sheets
from pqx_plan.receipt import RunReceipt, SourceStamp
from pqx_report.model import FragmentReport, Outcome, RunReport, SourceReport, WorkbookReport
from pqx_report.render import REPORT_SUFFIXES, render_html, render_json, render_markdown, report_filename
from pqx_sidecar.document import SidecarDocument
from pqx_sidecar.store import sidecar_path
from pqx_staging.lease import acquire_lease, release_lease, require_lease
from pqx_staging.scratch import DEFAULT_OUTPUT_ALLOWANCE, require_free_space, required_bytes, scratch_directory
from pqx_staging.transfer import copy_file, delete_names, list_names, publish_files
from pqx_verify.fragments import ManifestVerdict, verify_manifest

from pqx_pipeline.bucketing import ordered_by_bucket
from pqx_pipeline.ingest import Observation, build_sidecar, observe, read_parquet
from pqx_pipeline.locations import REPORTS_DIRECTORY, keep_set, manifest_path, receipt_path
from pqx_pipeline.staleness import ProfileStaleness, SourceStaleness, excel_stale, read_receipt, sidecar_stale
from pqx_pipeline.write import WrittenWorkbook, write_profile

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import polars as pl
    from pqx_calendar.columns import DateColumn
    from pqx_calendar.periods import Bucket, PeriodOrder
    from pqx_frame.metadata.dataframe import DataframeMetadata
    from upath import UPath

__all__ = [
    "PLANNER_VERSION",
    "OwnershipError",
    "Preview",
    "Produced",
    "RunInstants",
    "RunOptions",
    "RunOutcome",
    "check_ownership",
    "describe_sources",
    "observe_sources",
    "plan_profile",
    "preview_profile",
    "publish_reports",
    "run_profile",
    "select_profile",
    "sidecar_directory_for",
]

PLANNER_VERSION: str = "1"
"""This build's allocation algorithm. A change here invalidates an export every timestamp calls
current, which is why the receipt records it and selection compares it."""


class OwnershipError(Exception):
    """The output directory does not belong to this configuration and profile.

    Deletion is refused when this fires, so an unexpected receipt -- or none at all where files
    already exist -- stops the run instead of clearing the directory.
    """


@dataclass(frozen=True, slots=True)
class RunOptions:
    """The switches a run is invoked with.

    Attributes:
        force: Rebuild regardless of what selection says.
        dry_run: Report the selection and the delete set, and publish nothing.
        scratch_root: Where to work. ``None`` takes the platform's temporary directory.
        keep_scratch: Leave the scratch directory in place, for debugging a failed run.
        output_allowance: Bytes to leave free for the workbooks, over and above the sources.
    """

    force: bool = False
    dry_run: bool = False
    scratch_root: UPath | None = None
    keep_scratch: bool = False
    output_allowance: int = DEFAULT_OUTPUT_ALLOWANCE


@dataclass(frozen=True, slots=True)
class RunInstants:
    """Every instant a run stamps, passed in rather than read.

    Four, and they are not interchangeable. ``described`` is T2 and reaches every sidecar this run
    writes -- one instant for all of them, so sidecars written seconds apart do not give different
    answers to the same staleness question. ``exported`` is T4 and reaches the receipt.
    """

    started: dt.datetime
    described: dt.datetime
    exported: dt.datetime
    finished: dt.datetime

    @classmethod
    def at(cls, moment: dt.datetime) -> RunInstants:
        """Return instants that all name one moment, for a run short enough not to care."""
        return cls(started=moment, described=moment, exported=moment, finished=moment)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one run did.

    Attributes:
        report: The report, whatever the outcome. Always produced.
        staleness: What selection decided, and why.
        manifest: The manifest, when one was produced.
        verdict: What verification said, when it ran.
        published: The destination-relative names published, in order.
        deleted: What reconciliation removed.
    """

    report: RunReport
    staleness: ProfileStaleness
    manifest: RunManifest | None = None
    verdict: ManifestVerdict | None = None
    published: tuple[str, ...] = ()
    deleted: tuple[str, ...] = field(default=())

    @property
    def valid(self) -> bool:
        """Whether the run published a verified export."""
        return self.report.valid


def check_ownership(destination: UPath, profile: str, config_id: str) -> None:
    """Refuse a destination that belongs to something else.

    A run can see only its own configuration and the destination, so the runtime guard is an
    ownership marker -- and the receipt already is one. Two cases refuse: a receipt naming a
    different configuration or profile, and **no receipt at all where files already exist**, which
    is a directory something else wrote and this run would otherwise reconcile away.

    ``reports/`` and the lease do not count as files: a previous failed run leaves both and neither
    means the directory is someone else's.

    Args:
        destination: The profile's output directory.
        profile: The profile name.
        config_id: This configuration's stable identity.

    Raises:
        OwnershipError: The directory belongs to another configuration or profile, or holds
            output no receipt accounts for.
    """
    receipt: RunReceipt | None = read_receipt(destination, profile)
    if receipt is not None:
        if receipt.config_id != config_id or receipt.profile != profile:
            message: str = f"{destination} holds a receipt for configuration {receipt.config_id!r} profile {receipt.profile!r}, not {config_id!r} profile {profile!r}"
            raise OwnershipError(message)
        return
    unexplained: tuple[str, ...] = tuple(name for name in list_names(destination) if name != REPORTS_DIRECTORY and name != f"{profile}.lock")
    if unexplained:
        message = f"{destination} holds {list(unexplained)} and no receipt for profile {profile!r}; something else wrote here, so this run will not reconcile it"
        raise OwnershipError(message)


def plan_profile(profile: ProfileSettings, observations: Mapping[str, Observation], counts: Mapping[str, dict[Bucket, int]]) -> tuple[NamedWorkbook, ...]:
    """Turn bucket counts into named workbooks, taking the single-sheet shortcut where it applies."""
    partitioning: Partitioning = profile.partitioning
    shapes: list[SourceShape] = [observations[sheet.source].shape for sheet in profile.sheets]
    per_source: list[tuple[int, tuple[PlannedSheet, ...]]] = []
    position: int
    sheet: SheetSettings
    if fits_one_workbook(shapes, profile.limits):
        # `single_worksheet` is used only when the entire export takes the shortcut, not for every
        # workbook that happens to hold one sheet.
        for position, sheet in enumerate(profile.sheets):
            per_source.append((position, (PlannedSheet(source_alias=sheet.source, kind="single", rows=observations[sheet.source].shape.rows),)))
    else:
        if not isinstance(partitioning, CalendarPartitioning):
            message: str = f"profile {profile.name!r} uses 'balanced', which is planned but not yet wired into a run"
            raise NotImplementedError(message)
        for position, sheet in enumerate(profile.sheets):
            per_source.append((position, plan_calendar_sheets(observations[sheet.source].shape, counts[sheet.source], partitioning, profile.limits)))

    order: PeriodOrder = partitioning.period_order if isinstance(partitioning, CalendarPartitioning) else "ascending"
    ordered: tuple[PlannedSheet, ...] = order_profile_sheets(per_source, order)
    widths: dict[str, int] = {alias: seen.shape.columns for alias, seen in observations.items()}
    books: tuple[AllocatedWorkbook, ...] = allocate_workbooks(ordered, widths, profile.limits.max_cells_per_workbook)
    return name_workbooks(books, profile.naming, profile=profile.name, stems={alias: seen.shape.stem for alias, seen in observations.items()})


def observe_sources(
    profile: ProfileSettings,
    config: ExportConfig,
    staged: Mapping[str, UPath],
    instants: RunInstants,
    *,
    sidecar_directory: UPath | None,
) -> tuple[dict[str, Observation], dict[str, dict[Bucket, int]], dict[str, UPath]]:
    """Read, describe and bucket every source the profile names.

    Returns the observations, the bucket counts the planner works over, and where each sidecar was
    written. The frame carried by each observation is already in final output order, so the
    writer's slices line up with the plan without either of them re-deriving anything.

    ``sidecar_directory`` is passed in rather than read off the configuration, and ``None`` means
    beside the source. A run passes the configured location, because a sidecar written there is
    what the *next* run's selection reads. A preview passes its scratch directory instead, which is
    what makes ``plan`` and ``write`` leave nothing behind: they describe the sources exactly as a
    run would, into a directory that is deleted when they are done.

    Args:
        profile: The profile being run.
        config: The validated configuration.
        staged: Each source's local copy, by alias.
        instants: The run's instants; ``described`` is stamped into every sidecar.
        sidecar_directory: Where sidecars are written, or ``None`` for beside the source.

    Returns:
        The observations, the bucket counts, and where each sidecar was written, each by alias.
    """
    directory: UPath | None = sidecar_directory
    observations: dict[str, Observation] = {}
    counts: dict[str, dict[Bucket, int]] = {}
    sidecars: dict[str, UPath] = {}
    sheet: SheetSettings
    for sheet in profile.sheets:
        original: UPath = ZPath(config.sources[sheet.source].path)
        frame: pl.DataFrame = read_parquet(staged[sheet.source])
        metadata: DataframeMetadata = build_sidecar(frame, original_path=original, created_utc=instants.described, sidecar_directory=directory)
        sidecars[sheet.source] = sidecar_path(original if directory is None else directory / original.name)

        bucket_counts: dict[Bucket, int] = {}
        if isinstance(profile.partitioning, CalendarPartitioning) and sheet.partition_column is not None:
            column: DateColumn = config.sources[sheet.source].date_columns[sheet.partition_column]
            frame, bucket_counts = ordered_by_bucket(frame, column, profile.partitioning, column_name=sheet.partition_column, sort=sheet.sort)
        counts[sheet.source] = bucket_counts
        observations[sheet.source] = observe(sheet.source, frame, original_path=original, metadata=metadata)
    return observations, counts, sidecars


def sidecar_directory_for(config: ExportConfig) -> UPath | None:
    """Return where this configuration puts sidecars, or ``None`` for beside the source.

    Args:
        config: The validated configuration.

    Returns:
        The directory, or ``None`` when ``sidecar_location`` is ``beside_source``.
    """
    return None if config.sidecar_location.kind == "beside_source" else ZPath(config.sidecar_location.path)


@dataclass(frozen=True, slots=True)
class Produced:
    """Everything one profile's sources turned into, short of publishing.

    Attributes:
        observations: Each source as read, described and arranged, by alias.
        sidecars: Where each source's sidecar was written, by alias.
        named: The workbooks the planner chose, every name already rendered.
        written: The workbooks actually written into scratch. Empty when only planning.
        sources: The manifest entries the write produced. Empty when only planning.
    """

    observations: dict[str, Observation]
    sidecars: dict[str, UPath]
    named: tuple[NamedWorkbook, ...]
    written: tuple[WrittenWorkbook, ...] = ()
    sources: tuple[SourceFragments, ...] = ()


def _produce(config: ExportConfig, profile: ProfileSettings, scratch: UPath, *, instants: RunInstants, sidecars_into: UPath | None, write: bool, output_allowance: int) -> Produced:
    """Stage, describe, arrange, plan and optionally write one profile, entirely inside ``scratch``.

    The shared body of a run and of a preview. ``plan`` and ``write`` are only worth offering if
    they show what ``run`` would actually do, and a second implementation of the same sequence
    would be a second thing to keep true.

    Args:
        config: The validated configuration.
        profile: The profile to produce.
        scratch: The working directory. Everything this function writes lands here or under
            ``sidecars_into``; nothing reaches the destination.
        instants: The run's instants.
        sidecars_into: Where sidecars go, or ``None`` for beside the source.
        write: Whether to write the workbooks. ``False`` stops after planning, which is what makes
            a plan cost one read of each source rather than a whole export.
        output_allowance: Bytes to leave free for the workbooks, over and above the sources.

    Returns:
        What was produced. ``written`` and ``sources`` are empty unless ``write``.
    """
    originals: dict[str, UPath] = {sheet.source: ZPath(config.sources[sheet.source].path) for sheet in profile.sheets}
    require_free_space(scratch, required_bytes(list(originals.values()), output_allowance=output_allowance))
    staged: dict[str, UPath] = {alias: copy_file(original, scratch / original.name) for alias, original in originals.items()}

    observations: dict[str, Observation]
    counts: dict[str, dict[Bucket, int]]
    sidecars: dict[str, UPath]
    observations, counts, sidecars = observe_sources(profile, config, staged, instants, sidecar_directory=sidecars_into)
    named: tuple[NamedWorkbook, ...] = plan_profile(profile, observations, counts)
    if not write:
        return Produced(observations=observations, sidecars=sidecars, named=named)

    written: tuple[WrittenWorkbook, ...]
    sources: tuple[SourceFragments, ...]
    written, sources = write_profile(named, observations, scratch, sidecar_names={alias: path.name for alias, path in sidecars.items()})

    # Before verification, not merely dropped from memory: it frees scratch where the run needs it
    # most, and makes verification's independence structural rather than asserted.
    path: UPath
    for path in staged.values():
        path.unlink(missing_ok=True)
    return Produced(observations=observations, sidecars=sidecars, named=named, written=written, sources=sources)


@dataclass(frozen=True, slots=True)
class Preview:
    """What a run would do, having done everything except reach the destination.

    Attributes:
        staleness: What selection decided, and why.
        produced: The plan, and the workbooks when the preview wrote them.
        destination: Where a run would have published.
        scratch: Where the work was done. It exists after the call only under ``keep_scratch``,
            which is the whole reason a preview offers the switch.
    """

    staleness: ProfileStaleness
    produced: Produced
    destination: UPath
    scratch: UPath


def preview_profile(config: ExportConfig, profile: ProfileSettings, *, run_id: str, instants: RunInstants, options: RunOptions | None = None, write: bool = False) -> Preview:
    """Do everything a run does short of verifying and publishing, and return what it would be.

    Sidecars go into scratch rather than to their configured location, so a preview leaves the
    *next* run's selection exactly as it found it: describing a source into a directory that is
    about to be deleted is not a claim that the source has been described.

    Args:
        config: The validated configuration.
        profile: The profile to preview.
        run_id: Identifies this invocation; used as a scratch path segment.
        instants: The instants to stamp.
        options: The switches it was invoked with.
        write: Whether to write the workbooks into scratch as well as plan them.

    Returns:
        The preview.
    """
    chosen: RunOptions = options or RunOptions()
    destination: UPath = ZPath(config.output_directory) / profile.output_subdirectory
    staleness: ProfileStaleness = select_profile(config, profile, destination, forced=chosen.force)
    scratch: UPath
    with scratch_directory(run_id, root=chosen.scratch_root, keep=chosen.keep_scratch) as scratch:
        produced: Produced = _produce(config, profile, scratch, instants=instants, sidecars_into=scratch, write=write, output_allowance=chosen.output_allowance)
        return Preview(staleness=staleness, produced=produced, destination=destination, scratch=scratch)


def describe_sources(config: ExportConfig, profile: ProfileSettings, *, run_id: str, instants: RunInstants, options: RunOptions | None = None) -> dict[str, UPath]:
    """Re-describe every source a profile names, and publish nothing.

    The one half of a run that stands alone. A sidecar marks *described*, never *exported*, so
    writing one costs the next run its hashing and nothing else -- which is exactly the state a run
    killed after its sidecars leaves behind, and exactly what this reproduces on purpose.

    Nothing reaches the destination, so this cannot make a directory a later run would refuse to
    reconcile.

    Args:
        config: The validated configuration.
        profile: The profile whose sources to describe.
        run_id: Identifies this invocation; used as a scratch path segment.
        instants: The instants to stamp; ``described`` is T2.
        options: The switches it was invoked with.

    Returns:
        Where each source's sidecar was written, by alias.
    """
    chosen: RunOptions = options or RunOptions()
    directory: UPath | None = sidecar_directory_for(config)
    originals: dict[str, UPath] = {sheet.source: ZPath(config.sources[sheet.source].path) for sheet in profile.sheets}
    written: dict[str, UPath] = {}
    scratch: UPath
    with scratch_directory(run_id, root=chosen.scratch_root, keep=chosen.keep_scratch) as scratch:
        require_free_space(scratch, required_bytes(list(originals.values()), output_allowance=chosen.output_allowance))
        alias: str
        original: UPath
        for alias, original in originals.items():
            # One at a time, deleted as we go: describing needs one source in hand, not all of
            # them, and a profile over several large sources should not need room for every one.
            staged: UPath = copy_file(original, scratch / original.name)
            build_sidecar(read_parquet(staged), original_path=original, created_utc=instants.described, sidecar_directory=directory)
            written[alias] = sidecar_path(original if directory is None else directory / original.name)
            staged.unlink(missing_ok=True)
    return written


def _report(
    profile: ProfileSettings,
    config: ExportConfig,
    staleness: ProfileStaleness,
    instants: RunInstants,
    *,
    run_id: str,
    destination: UPath,
    outcome: Outcome,
    config_hash: str,
    observations: Mapping[str, Observation] | None = None,
    sources: Sequence[SourceFragments] = (),
    verdict: ManifestVerdict | None = None,
    written: Sequence[WrittenWorkbook] = (),
    deleted: Sequence[str] = (),
    failure: str | None = None,
    notes: Sequence[str] = (),
) -> RunReport:
    """Assemble the report, which is produced whatever the outcome."""
    by_alias: Mapping[str, Observation] = observations or {}
    verdicts: Mapping[str, object] = {source.source_alias: source for source in (verdict.sources if verdict is not None else ())}
    source_reports: list[SourceReport] = []
    fragments: SourceFragments
    for fragments in sources:
        checked: object = verdicts.get(fragments.source_alias)
        source_reports.append(
            SourceReport(
                alias=fragments.source_alias,
                path=fragments.source_path,
                rows=fragments.expected_row_count,
                rebuilt_because=staleness.reasons,
                fragments=tuple(
                    FragmentReport(
                        workbook=fragment.workbook,
                        sheet_name=fragment.sheet_name,
                        rows=fragment.row_count,
                        period_label=fragment.period_label,
                        verdict=getattr(checked, "fragments", ())[index].kind if checked is not None else "not-verified",
                        detail=getattr(checked, "fragments", ())[index].detail if checked is not None else "verification did not run",
                    )
                    for index, fragment in enumerate(fragments.fragments)
                ),
                rows_expected=fragments.expected_row_count,
                rows_found=sum(fragment.row_count for fragment in fragments.fragments),
                reassembled=None if checked is None else getattr(getattr(checked, "reassembly", None), "valid", None),
            ),
        )
    if not source_reports:
        source_reports = [SourceReport(alias=alias, path=str(seen.metadata.full_path), rows=seen.shape.rows, rebuilt_because=staleness.reasons) for alias, seen in by_alias.items()]
    return RunReport(
        outcome=outcome,
        profile=profile.name,
        run_id=run_id,
        generated_utc=instants.finished,
        started_utc=instants.started,
        finished_utc=instants.finished,
        config_id=config.config_id,
        config_path=str(config.output_directory),
        destination=str(destination),
        planner_version=PLANNER_VERSION,
        resolved_config_hash=config_hash,
        sources=tuple(source_reports),
        workbooks=tuple(WorkbookReport(filename=book.filename, sheets=book.sheets, rows=book.rows, cells=book.cells) for book in written),
        deleted=tuple(deleted),
        failure=failure,
        notes=tuple(notes),
    )


def publish_reports(report: RunReport, destination: UPath, scratch: UPath) -> tuple[str, ...]:
    """Write the three renderings into ``reports/`` and return their destination-relative names.

    The one thing a failed run publishes. ``reports/`` is excluded from the keep-set wholesale, so
    nothing a later run reconciles will remove them.

    Args:
        report: The report to render.
        destination: The profile's output directory.
        scratch: Where to render before copying.

    Returns:
        The names written, destination-relative.
    """
    renderings: dict[str, str] = {"json": render_json(report), "md": render_markdown(report), "html": render_html(report)}
    staged: list[tuple[UPath, str]] = []
    suffix: str
    text: str
    for suffix, text in renderings.items():
        name: str = report_filename(report.profile, report.generated_utc, suffix)
        local: UPath = scratch / name
        local.write_text(text, encoding="utf-8")
        staged.append((local, f"{REPORTS_DIRECTORY}/{name}"))
    assert set(REPORT_SUFFIXES) == set(renderings)  # noqa: S101
    return publish_files(staged, destination)


def select_profile(config: ExportConfig, profile: ProfileSettings, destination: UPath, *, forced: bool = False) -> ProfileStaleness:
    """Decide whether a profile needs rebuilding, over remote metadata only.

    Args:
        config: The validated configuration.
        profile: The profile to consider.
        destination: Its output directory.
        forced: Whether ``--force`` was given.

    Returns:
        The verdict, carrying every condition that fired.
    """
    sheet: SheetSettings
    seen: set[str] = set()
    sources: list[SourceStaleness] = []
    for sheet in profile.sheets:
        if sheet.source in seen:
            continue
        seen.add(sheet.source)
        sources.append(sidecar_stale(sheet.source, ZPath(config.sources[sheet.source].path)))
    return excel_stale(
        profile.name,
        sources,
        read_receipt(destination, profile.name),
        config_modified_utc=config.config_modified_utc,
        resolved_config_hash=resolved_config_hash(config),
        planner_version=PLANNER_VERSION,
        forced=forced,
    )


def run_profile(config: ExportConfig, profile: ProfileSettings, *, run_id: str, instants: RunInstants, options: RunOptions | None = None) -> RunOutcome:
    """Run one profile end to end, publishing only a verified export.

    Args:
        config: The validated configuration.
        profile: The profile to run.
        run_id: Identifies this run. Used as a scratch path segment and recorded everywhere.
        instants: The four instants this run stamps.
        options: The switches it was invoked with.

    Returns:
        What the run did. A failure is a value here rather than an exception, because the report is
        part of the outcome and a caller that got an exception would have nothing to publish.

    Raises:
        OwnershipError: The destination belongs to something else. Raised rather than reported,
            because publishing a report into a directory this run does not own is the very thing
            the check exists to prevent.
    """
    chosen: RunOptions = options or RunOptions()
    destination: UPath = ZPath(config.output_directory) / profile.output_subdirectory
    config_hash: str = resolved_config_hash(config)
    staleness: ProfileStaleness = select_profile(config, profile, destination, forced=chosen.force)

    if not staleness.stale:
        return RunOutcome(
            report=_report(profile, config, staleness, instants, run_id=run_id, destination=destination, outcome="published", config_hash=config_hash, notes=("nothing was stale; no work was done",)),
            staleness=staleness,
        )
    check_ownership(destination, profile.name, config.config_id)
    if chosen.dry_run:
        return RunOutcome(
            report=_report(
                profile,
                config,
                staleness,
                instants,
                run_id=run_id,
                destination=destination,
                outcome="published",
                config_hash=config_hash,
                notes=("dry run: nothing was staged, written, published or deleted", f"would rebuild because {', '.join(staleness.reasons)}"),
            ),
            staleness=staleness,
        )

    scratch: UPath
    with scratch_directory(run_id, root=chosen.scratch_root, keep=chosen.keep_scratch) as scratch:
        produced: Produced = _produce(config, profile, scratch, instants=instants, sidecars_into=sidecar_directory_for(config), write=True, output_allowance=chosen.output_allowance)
        observations: Mapping[str, Observation] = produced.observations
        sidecars: Mapping[str, UPath] = produced.sidecars
        written: tuple[WrittenWorkbook, ...] = produced.written
        sources: tuple[SourceFragments, ...] = produced.sources

        manifest: RunManifest = RunManifest(
            run_id=run_id,
            config_path=str(config.output_directory),
            profile=profile.name,
            planner_version=PLANNER_VERSION,
            resolved_config=config,
            output_directory=str(destination),
            started_utc=instants.started,
            sources=list(sources),
            workbooks=[book.filename for book in written],
            sidecars=[path.name for path in sidecars.values()],
        )
        schemas: dict[str, dict[str, object]] = {alias: dict(seen.frame.schema) for alias, seen in observations.items()}
        verdict: ManifestVerdict = verify_manifest(scratch, manifest, schemas)  # type: ignore[arg-type]

        if not verdict.valid:
            failed: str = "; ".join(entry.detail for entry in verdict.failures[:3]) or "a source did not reassemble"
            report: RunReport = _report(
                profile,
                config,
                staleness,
                instants,
                run_id=run_id,
                destination=destination,
                outcome="verification-failed",
                config_hash=config_hash,
                observations=observations,
                sources=sources,
                verdict=verdict,
                written=written,
                failure=failed,
            )
            publish_reports(report, destination, scratch)
            return RunOutcome(report=report, staleness=staleness, manifest=manifest, verdict=verdict)

        published: tuple[str, ...]
        deleted: tuple[str, ...]
        published, deleted = _publish(config, profile, destination, manifest, written, sidecars, run_id=run_id, instants=instants)
        report = _report(
            profile,
            config,
            staleness,
            instants,
            run_id=run_id,
            destination=destination,
            outcome="published",
            config_hash=config_hash,
            observations=observations,
            sources=sources,
            verdict=verdict,
            written=written,
            deleted=deleted,
        )
        published += publish_reports(report, destination, scratch)
        return RunOutcome(report=report, staleness=staleness, manifest=manifest, verdict=verdict, published=published, deleted=deleted)


def _publish(
    config: ExportConfig,
    profile: ProfileSettings,
    destination: UPath,
    manifest: RunManifest,
    written: Sequence[WrittenWorkbook],
    sidecars: Mapping[str, UPath],
    *,
    run_id: str,
    instants: RunInstants,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Publish a verified export in the order the order matters, and return what went where.

    Ownership check (already done), acquire lease, sidecars, workbooks, reconcile delete, manifest,
    receipt, release lease. The receipt is the "this export completed" marker, so nothing precedes
    it that a later step could invalidate. Deletion happens under the lease and only after the
    workbooks are in place, so a run that dies mid-publish leaves too much rather than too little.
    """
    acquire_lease(destination, profile.name, run_id=run_id, config_id=config.config_id, now=instants.started)
    try:
        published: list[str] = list(publish_files([(path, path.name) for path in sidecars.values()], destination))
        published.extend(publish_files([(book.path, book.filename) for book in written], destination))

        require_lease(destination, profile.name, run_id=run_id, now=instants.exported)
        keep: frozenset[str] = keep_set(profile.name, workbooks=tuple(manifest.workbooks), sidecars=tuple(manifest.sidecars))
        deleted: tuple[str, ...] = delete_names(destination, [name for name in list_names(destination) if name not in keep])

        manifest_path(destination, profile.name).write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        published.append(manifest_path(destination, profile.name).name)

        receipt: RunReceipt = RunReceipt(
            config_id=config.config_id,
            profile=profile.name,
            excel_created_utc=instants.exported,
            config_modified_utc=config.config_modified_utc,
            resolved_config_hash=resolved_config_hash(config),
            planner_version=PLANNER_VERSION,
            sources={alias: SourceStamp(source_modified_utc=_described_at(alias, sidecars), sidecar_created_utc=instants.described) for alias in sidecars},
        )
        receipt_path(destination, profile.name).write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
        published.append(receipt_path(destination, profile.name).name)
        return tuple(published), deleted
    finally:
        release_lease(destination, profile.name, run_id=run_id)


def _described_at(alias: str, sidecars: Mapping[str, UPath]) -> dt.datetime:
    """Return T1 for one source, read back from the sidecar this run just wrote.

    Read back rather than carried along, so the receipt records the value that was actually
    persisted. If those two ever disagree, the persisted one is what the next run's selection will
    compare against.
    """
    return SidecarDocument.model_validate_json(sidecars[alias].read_text(encoding="utf-8")).metadata.modified_utc
