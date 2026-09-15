"""Deciding what has to be rebuilt, over remote metadata only.

**Before staging**, and that is the whole point: one receipt read, one ``stat()`` per source, one
small sidecar read per source. Downloading a multi-gigabyte Parquet to discover nothing changed is
the thing this exists to avoid, and ``JsonSidecarStore`` reading over ``UPath`` is what makes it
possible -- so its remote support is load-bearing rather than vestigial.

**Selection picks profiles, not files.** Rebuilding a workbook re-reads its unchanged sources, so a
profile is rebuilt when *any* of its sources is stale, and then all of its sources are staged.
Per-file skipping would leave a workbook mixing old and new sheets.

**The two markers are independent, deliberately.** The sidecar marks *described*; the receipt marks
*exported*. A run that dies after publishing sidecars but before the receipt leaves the next run
with ``sidecar_stale`` false and ``excel_stale`` true, so it rebuilds only the export and does not
re-hash the Parquet. Neither marker can claim work the other did.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pqx_frame.conversion.to_excel import DataframeConversionToExcel
from pqx_frame.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash
from pqx_plan.receipt import RunReceipt
from pqx_report.model import StalenessSignal
from pqx_sidecar.document import SidecarDocument
from pqx_sidecar.store import get_sidecar_store

from pqx_pipeline.locations import receipt_path

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pqx_frame.hashing import HashedDataframe
    from pqx_frame.metadata.columns import DataframeColumnsMetadata
    from upath import UPath

__all__ = ["ProfileStaleness", "SourceStaleness", "excel_record", "excel_stale", "read_receipt", "sidecar_stale", "truncate_to_second"]

SUPPORTED_HASHER_VERSION: int = 2
"""The hasher algorithm version this build recomputes with. A v1 record aggregated without row
relationships, so its dataframe digest cannot be reproduced from the data."""


def truncate_to_second(moment: dt.datetime) -> dt.datetime:
    """Return an instant with sub-second precision removed.

    Every timestamp comparison here goes through this. Azure Blob reports last-modified at
    one-second granularity while a local filesystem reports finer, so the same file staged from one
    and described from the other differs in a way that is an artefact of the store rather than a
    change to the data -- and a rebuild that fires every run is indistinguishable from one that
    never fires, in that neither tells you anything.

    Args:
        moment: The instant to truncate.

    Returns:
        The same instant, with microseconds dropped.
    """
    return moment.replace(microsecond=0)


@dataclass(frozen=True, slots=True)
class SourceStaleness:
    """Whether one source needs re-describing, and why.

    Attributes:
        alias: The source alias.
        signals: Every condition that fired, in the order checked. Empty means fresh.
        described: The sidecar found, or ``None`` when there was none this build could read.
    """

    alias: str
    signals: tuple[StalenessSignal, ...] = ()
    described: SidecarDocument | None = None

    @property
    def stale(self) -> bool:
        """Whether this source must be re-described."""
        return bool(self.signals)


@dataclass(frozen=True, slots=True)
class ProfileStaleness:
    """Whether one profile needs re-exporting, and why.

    Attributes:
        profile: The profile name.
        signals: Every condition that fired at profile level.
        sources: Each source's own verdict, in the order the profile names them.
    """

    profile: str
    signals: tuple[StalenessSignal, ...] = ()
    sources: tuple[SourceStaleness, ...] = field(default=())

    @property
    def stale(self) -> bool:
        """Whether this profile must be rebuilt."""
        return bool(self.signals) or any(source.stale for source in self.sources)

    @property
    def reasons(self) -> tuple[StalenessSignal, ...]:
        """Return every distinct signal that fired, profile-level and per source, in order."""
        # A dict rather than a set, because order is the point: the signals are recorded in the
        # order they were checked, and a set would report them in whatever order it hashed them.
        seen: dict[StalenessSignal, None] = {}
        signal: StalenessSignal
        source: SourceStaleness
        for signal in self.signals:
            seen.setdefault(signal, None)
        for source in self.sources:
            for signal in source.signals:
                seen.setdefault(signal, None)
        return tuple(seen)


def sidecar_stale(alias: str, source_path: UPath, *, store: str = "json") -> SourceStaleness:
    """Decide whether one source needs re-describing, without opening it.

    Four conditions, and the last two are version drift rather than change. A bump to
    ``DataframeConversionToExcel`` or to the hasher leaves T1 untouched, so without them a run
    proceeds all the way to verification before ``unsupported-conversion`` fires -- the right
    answer, after staging, reading and writing everything. Rebuilding at selection is the same
    answer for far less work. Both are conservative: they re-describe once per release whether or
    not the digests would differ.

    Args:
        alias: The source alias, for the verdict.
        source_path: The **original** source location, not a scratch copy.
        store: Identifier of the sidecar store to read through.

    Returns:
        The verdict. A source that is itself missing is *not* reported here -- that is a wiring
        failure rather than staleness, and ``stat()`` raises.

    Raises:
        FileNotFoundError: The source is not there.
    """
    modified: dt.datetime = dt.datetime.fromtimestamp(source_path.stat().st_mtime, tz=dt.UTC)
    document: SidecarDocument | None = None
    try:
        document = get_sidecar_store(store).read(source_path)
    except (FileNotFoundError, ValueError):
        # Unreadable is treated exactly as missing: a sidecar this build cannot parse describes
        # the source no better than no sidecar at all, and a v2 file after the v3 bump reaches
        # here by design.
        return SourceStaleness(alias=alias, signals=("sidecar-missing-or-unreadable",))

    signals: list[StalenessSignal] = []
    if truncate_to_second(modified) != truncate_to_second(document.metadata.modified_utc):
        # Compared with != and not >. Restoring yesterday's Parquet over today's moves its mtime
        # backwards, and > calls that "not new" and leaves the stale description standing.
        signals.append("source-changed")
    if not _conversion_current(document):
        signals.append("conversion-version")
    if not _hasher_current(document):
        signals.append("hasher-version")
    return SourceStaleness(alias=alias, signals=tuple(signals), described=document)


def excel_record(document: SidecarDocument) -> DataframeColumnsMetadata | None:
    """Return a sidecar's ToExcel record, found by identifier rather than by position.

    Public because verification needs it too: the dtypes of the frame that was written live here,
    and ``verify_manifest`` takes them as an argument rather than going looking for the sidecar
    itself. Finding the record by identifier is the part worth having in one place -- a
    ``DataframeMetadata`` holds one record per conversion in a plain list, so position is whatever
    the caller happened to pass.

    Args:
        document: The sidecar.

    Returns:
        The record, or ``None`` when the sidecar carries none.
    """
    record: DataframeColumnsMetadata
    for record in document.metadata.column_metadata_of_conversions:
        if record.conversion is not None and record.conversion.identifier == DataframeConversionToExcel.identifier:
            return record
    return None


def _conversion_current(document: SidecarDocument) -> bool:
    """Return whether the sidecar's ToExcel record was written by this build's conversion.

    A sidecar with no ToExcel record at all counts as drifted rather than current: it was
    extracted without the conversion the export depends on, so it has to be re-described either
    way, and saying so here is cheaper than discovering it at verification.
    """
    record: DataframeColumnsMetadata | None = excel_record(document)
    if record is None or record.conversion is None:
        return False
    return record.conversion.version == DataframeConversionToExcel.version


def _hasher_current(document: SidecarDocument) -> bool:
    """Return whether the sidecar's dataframe digest was written by this build's hasher."""
    record: DataframeColumnsMetadata | None = excel_record(document)
    if record is None or not record.dataframe_hashes:
        return False
    stored: HashedDataframe = record.dataframe_hashes[0]
    return stored.identifier == DataFrameHasherBinaryAggregateHash.identifier and stored.version == SUPPORTED_HASHER_VERSION


def read_receipt(destination: UPath, profile: str) -> RunReceipt | None:
    """Return the receipt at a profile's destination, or ``None`` when there is none.

    Args:
        destination: The profile's output directory.
        profile: The profile name.

    Returns:
        The receipt, or ``None`` when the profile has never been exported.

    Raises:
        ValueError: A receipt exists and this build cannot read it. **Not** treated as absent: an
            unreadable receipt still marks the directory as owned, and reading it as "never
            exported" would let a run reconcile away another configuration's output.
    """
    path: UPath = receipt_path(destination, profile)
    if not path.exists():
        return None
    return RunReceipt.model_validate_json(path.read_text(encoding="utf-8"))


def excel_stale(
    profile: str,
    sources: Sequence[SourceStaleness],
    receipt: RunReceipt | None,
    *,
    config_modified_utc: dt.datetime,
    resolved_config_hash: str,
    planner_version: str,
    forced: bool = False,
) -> ProfileStaleness:
    """Decide whether one profile needs re-exporting.

    Args:
        profile: The profile name.
        sources: Each source's verdict from :func:`sidecar_stale`, in profile order.
        receipt: The profile's receipt, or ``None`` when it has never been exported.
        config_modified_utc: T3.
        resolved_config_hash: The fingerprint of the configuration as it stands now.
        planner_version: This build's planner version.
        forced: Whether ``--force`` was given.

    Returns:
        The verdict, carrying every condition that fired rather than the first.
    """
    signals: list[StalenessSignal] = []
    if forced:
        signals.append("forced")
    if receipt is None:
        signals.append("no-receipt")
        return ProfileStaleness(profile=profile, signals=tuple(signals), sources=tuple(sources))

    described: Mapping[str, SourceStaleness] = {source.alias: source for source in sources}
    if any(_described_after_export(described.get(alias), receipt.excel_created_utc) for alias in described):
        signals.append("described-after-export")
    if truncate_to_second(config_modified_utc) > truncate_to_second(receipt.excel_created_utc):
        signals.append("config-modified")
    if resolved_config_hash != receipt.resolved_config_hash:
        # The backstop for a forgotten T3 bump: exact, clock-free, and it fires when the timestamp
        # does not. Both signals are recorded, so a forgotten bump is visible rather than merely
        # compensated for.
        signals.append("resolved-config-hash")
    if planner_version != receipt.planner_version:
        # partitioning-spec.md promises determinism "for the same sources, resolved configuration
        # and planner version", so a changed allocation algorithm must invalidate an export that
        # every timestamp calls current.
        signals.append("planner-version")
    return ProfileStaleness(profile=profile, signals=tuple(signals), sources=tuple(sources))


def _described_after_export(source: SourceStaleness | None, exported: dt.datetime) -> bool:
    """Return whether a source's sidecar was written after the export that used it.

    A source with no readable sidecar is already stale on its own account, so it does not need to
    be counted here as well -- and it has no T2 to compare.
    """
    if source is None or source.described is None:
        return False
    return truncate_to_second(source.described.created_utc) > truncate_to_second(exported)
