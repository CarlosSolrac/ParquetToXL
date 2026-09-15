"""Turning one source's bucket counts into the sheets that will hold them.

Works over counts, never over rows. The planner is told how many rows fall in each calendar bucket
and decides how many sheets that needs and what each covers; *which* rows go where follows from the
sort order and is the pipeline's job. That is what keeps every case here a handful of integers, and
what lets a forty-million-row export be planned without reading one.

Three shapes come out, and they are what the naming templates distinguish: a **period** sheet
covering one bucket or a merged run of them, an **overflow** fragment when a bucket's rows exceed a
sheet and are split by count, and an **undated** sheet, always last. The fourth, **single**, is the
whole-export shortcut and is decided in :mod:`pqx_plan.capacity`, not here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pqx_calendar.grids import year_grid_spans
from pqx_calendar.labels import CoverageSpan
from pqx_calendar.periods import UNDATED, BasePeriod, Bucket, PeriodKey, PeriodPrecision, UndatedBucket, order_buckets

from pqx_plan.capacity import CapacityError, SourceShape, balanced_split, minimum_sheets, sheet_capacity
from pqx_plan.config import CalendarPartitioning, ProfileLimits

__all__ = ["PlannedSheet", "SheetKind", "plan_balanced_sheets", "plan_calendar_sheets", "required_count_precision"]

type SheetKind = Literal["single", "period", "overflow", "undated"]
"""Which naming template renders a planned sheet, and therefore what it is.

``single`` is used only when the *entire* export takes the shortcut, not for every workbook that
happens to hold one sheet. Balanced output uses ``period`` with no coverage, because its sheets
describe a slice of a sorted order rather than a stretch of calendar.
"""


@dataclass(frozen=True, slots=True)
class PlannedSheet:
    """One worksheet's worth of one source: how many rows, and what it covers.

    Attributes:
        source_alias: Whose rows these are. Fan-in puts several sources in one workbook.
        kind: Which template renders the name.
        rows: The data rows this sheet holds, header excluded.
        coverage: The calendar stretch covered, or ``None`` for balanced and undated sheets, which
            describe no calendar.
        part_index: One-based fragment index within the bucket this sheet came from, for
            ``overflow`` sheets; ``None`` for every other kind.
        parts: How many fragments that bucket was split into, so a renderer can pad the index.
    """

    source_alias: str
    kind: SheetKind
    rows: int
    coverage: CoverageSpan | None = None
    part_index: int | None = None
    parts: int = 1

    def __post_init__(self) -> None:
        """Refuse a sheet whose fields contradict its kind.

        Raises:
            ValueError: The row count is negative, a fragment index is present on a non-overflow
                sheet or missing from an overflow one, or a period sheet carries no coverage while
                claiming one.
        """
        if self.rows < 0:
            rows_message: str = f"sheet for {self.source_alias!r} holds {self.rows} rows"
            raise ValueError(rows_message)
        if (self.kind == "overflow") != (self.part_index is not None):
            part_message: str = f"kind {self.kind!r} and part_index {self.part_index!r} disagree"
            raise ValueError(part_message)
        if self.parts < 1:
            parts_message: str = f"a bucket cannot be split into {self.parts} fragments"
            raise ValueError(parts_message)


@dataclass(frozen=True, slots=True)
class _Group:
    """One stretch of calendar and the rows in it, before sheets are decided.

    ``sealed`` marks a group produced by subdividing an oversized year. Such a group never merges
    with anything: ``partitioning-spec.md`` forbids merging fragments of an oversized year into a
    neighbouring year, and merging them back together inside their own year would simply undo the
    split that was needed to make them fit.
    """

    coverage: CoverageSpan
    rows: int
    sealed: bool = False


def _year_groups(year: int, counts: Mapping[Bucket, int], capacity: int, grids: Sequence[int]) -> list[_Group]:
    """Return the sealed groups an oversized year becomes under the first grid that fits it.

    The grids are **alternatives evaluated against the entire year**, not recursive subdivisions:
    four-month periods do not nest inside semesters, so each is tried against the whole year and
    the first where every nonempty bucket fits wins.

    When none fits -- even monthly buckets are too large -- the spec does not keep the year whole:
    it emits the fitting months and applies ``oversized_period`` to each oversized month. So the
    finest declared grid is used anyway, and the oversized groups it produces are left for the
    caller to fragment.

    Args:
        year: The oversized year.
        counts: Row count per month-precision bucket.
        capacity: ``R_s`` for this source.
        grids: The profile's ``year_split_months``, coarsest first, ending in ``1``.

    Returns:
        One sealed group per nonempty bucket of the winning grid, in calendar order.
    """
    candidate: int
    for candidate in grids:
        groups: list[_Group] = _grid_groups(year, candidate, counts)
        if all(group.rows <= capacity for group in groups):
            return groups
    return _grid_groups(year, grids[-1], counts)


def _grid_groups(year: int, months_per_bucket: int, counts: Mapping[Bucket, int]) -> list[_Group]:
    """Return one sealed group per nonempty bucket of one grid over one year."""
    groups: list[_Group] = []
    first: PeriodKey
    last: PeriodKey
    for first, last in year_grid_spans(year, months_per_bucket):
        rows: int = sum(counts.get(PeriodKey("month", year, month), 0) for month in range(first.month or 1, (last.month or 1) + 1))
        if rows > 0:
            groups.append(_Group(CoverageSpan(first, last), rows, sealed=True))
    return groups


def required_count_precision(partitioning: CalendarPartitioning) -> PeriodPrecision:
    """Return the precision the caller must supply bucket counts at.

    Not the base period, and the difference is the whole reason this function exists. Subdividing
    an oversized **year** needs the row count of each *month* in it, and a count keyed by year
    cannot produce one. So a year base asks for month-precision counts and aggregates them up
    itself, keeping the finer numbers available for the moment a year turns out not to fit.

    A month base needs month counts and never subdivides -- there is no grid between a month and a
    day that ``year_split_months`` describes. A day base needs day counts, for the same reason.

    Args:
        partitioning: The profile's calendar settings.

    Returns:
        The precision every dated key in ``counts`` must carry.
    """
    return "day" if partitioning.base_period == "year-month-day" else "month"


def _base_key(key: PeriodKey, base_period: BasePeriod) -> PeriodKey:
    """Return the base-period bucket a finer key rolls up into."""
    return PeriodKey("year", key.year) if base_period == "year" else key


def _base_totals(counts: Mapping[Bucket, int], partitioning: CalendarPartitioning) -> dict[Bucket, int]:
    """Roll fine-grained counts up to the profile's base period, keeping the undated bucket apart.

    Args:
        counts: Row count per bucket, at :func:`required_count_precision`.
        partitioning: The profile's calendar settings.

    Returns:
        Row count per base-period bucket, plus the undated bucket if present.

    Raises:
        ValueError: A dated key does not carry the required precision, which would silently roll
            up into the wrong bucket or fail to subdivide later.
    """
    required: PeriodPrecision = required_count_precision(partitioning)
    totals: dict[Bucket, int] = {}
    bucket: Bucket
    rows: int
    for bucket, rows in counts.items():
        if isinstance(bucket, UndatedBucket):
            totals[UNDATED] = totals.get(UNDATED, 0) + rows
            continue
        if bucket.precision != required:
            message: str = f"base period {partitioning.base_period!r} needs counts at {required!r} precision, and {bucket} carries {bucket.precision!r}"
            raise ValueError(message)
        key: PeriodKey = _base_key(bucket, partitioning.base_period)
        totals[key] = totals.get(key, 0) + rows
    return totals


def _grouped(dated: Sequence[PeriodKey], counts: Mapping[Bucket, int], totals: Mapping[Bucket, int], capacity: int, partitioning: CalendarPartitioning) -> list[_Group]:
    """Return one group per base bucket, subdividing an oversized *year* into calendar grids.

    Only a year escalates. A month or day base has no coarser grid to subdivide, so an oversized
    bucket there goes straight to ``oversized_period`` -- as does a year whose own monthly buckets
    are still too large, which :func:`_year_groups` hands back oversized on purpose.
    """
    groups: list[_Group] = []
    bucket: PeriodKey
    for bucket in dated:
        rows: int = totals.get(bucket, 0)
        if partitioning.base_period == "year" and rows > capacity:
            groups.extend(_year_groups(bucket.year, counts, capacity, partitioning.year_split_months))
        else:
            groups.append(_Group(CoverageSpan.of(bucket), rows))
    return groups


def _merged(groups: Sequence[_Group], capacity: int) -> list[_Group]:
    """Merge successive whole groups while they fit, as ``calendar_greedy`` specifies.

    Before adding a group that would exceed capacity, the accumulated sheet is emitted. A sealed or
    oversized group flushes the accumulator and then stands alone, because fragments of an
    oversized year may not merge into a neighbouring year and an oversized bucket cannot merge with
    anything.

    No backfilling: an earlier run with room left is never revisited. Doing so would place a later
    period inside a sheet whose label claims an earlier contiguous range.
    """
    merged: list[_Group] = []
    run: list[_Group] = []

    def flush() -> None:
        """Emit the accumulated run, if any, as one group spanning all of it."""
        if run:
            merged.append(_Group(CoverageSpan(run[0].coverage.first, run[-1].coverage.last), sum(entry.rows for entry in run)))
            run.clear()

    group: _Group
    for group in groups:
        if group.sealed or group.rows > capacity:
            flush()
            merged.append(group)
            continue
        if run and sum(entry.rows for entry in run) + group.rows > capacity:
            flush()
        run.append(group)
    flush()
    return merged


def _fragments(alias: str, kind: SheetKind, rows: int, coverage: CoverageSpan | None, capacity: int, oversized_period: str, where: str) -> list[PlannedSheet]:
    """Return the sheets one group needs, splitting it only when it does not fit.

    Args:
        alias: The source.
        kind: The kind a fitting group takes. An oversized dated group becomes ``overflow``; an
            oversized undated one stays ``undated``, because the undated template is what names it
            and no date policy may discard its rows.
        rows: The group's row count.
        coverage: The group's calendar stretch, or ``None`` for the undated bucket.
        capacity: ``R_s`` for this source.
        oversized_period: The profile's policy, ``balanced_rows`` or ``error``.
        where: How to name this group in a refusal.

    Returns:
        One sheet, or several fragments in order, largest share first.

    Raises:
        CapacityError: The group is oversized and the profile refuses to split it.
    """
    if rows <= capacity:
        return [PlannedSheet(source_alias=alias, kind=kind, rows=rows, coverage=coverage)]
    if oversized_period == "error":
        message: str = f"source {alias!r} has {rows} rows in {where}, over the sheet capacity of {capacity}, and oversized_period is 'error'"
        raise CapacityError(message)
    parts: int = minimum_sheets(rows, capacity)
    fragment_kind: SheetKind = "undated" if kind == "undated" else "overflow"
    return [
        PlannedSheet(source_alias=alias, kind=fragment_kind, rows=share, coverage=coverage, part_index=None if fragment_kind == "undated" else index + 1, parts=parts)
        for index, share in enumerate(balanced_split(rows, parts))
    ]


def plan_calendar_sheets(shape: SourceShape, counts: Mapping[Bucket, int], partitioning: CalendarPartitioning, limits: ProfileLimits) -> tuple[PlannedSheet, ...]:
    """Return the sheets one source needs under a calendar algorithm, in final order.

    ``calendar_greedy`` merges successive whole base periods while they fit; ``calendar_periods``
    keeps each separate. Both subdivide an oversized year the same way, and both place the undated
    bucket last whatever ``period_order`` says.

    A bucket with no rows is simply absent from ``counts``. Missing periods do not create empty
    sheets, and a merged range label describes coverage rather than promising that every
    intervening period has rows.

    Args:
        shape: The source.
        counts: Row count per bucket, keyed at :func:`required_count_precision` -- **month
            precision for a year base**, not year, because subdividing an oversized year needs the
            months inside it. Plus the undated bucket, if any rows are null.
        partitioning: The profile's calendar settings.
        limits: The profile's ceilings.

    Returns:
        The planned sheets, in output order.

    Raises:
        CapacityError: A bucket is oversized and ``oversized_period`` is ``error``, or the budget
            cannot pay for a header and one data row of this source.
        ValueError: A dated key in ``counts`` does not carry the required precision.
    """
    capacity: int = sheet_capacity(shape, limits)
    totals: dict[Bucket, int] = _base_totals(counts, partitioning)
    ordered: tuple[Bucket, ...] = order_buckets(totals, partitioning.period_order)
    dated: list[PeriodKey] = [bucket for bucket in ordered if isinstance(bucket, PeriodKey)]

    groups: list[_Group] = _grouped(dated, counts, totals, capacity, partitioning)
    if partitioning.algorithm == "calendar_greedy":
        groups = _merged(groups, capacity)

    planned: list[PlannedSheet] = []
    group: _Group
    for group in groups:
        planned.extend(_fragments(shape.alias, "period", group.rows, group.coverage, capacity, partitioning.oversized_period, f"the period {group.coverage}"))

    if any(isinstance(entry, UndatedBucket) for entry in ordered):
        planned.extend(_fragments(shape.alias, "undated", totals[UNDATED], None, capacity, partitioning.oversized_period, "the undated bucket"))
    return tuple(planned)


def plan_balanced_sheets(shape: SourceShape, sheets: int) -> tuple[PlannedSheet, ...]:
    """Return one source's sheets under a balanced algorithm: even slices of a sorted order.

    Balanced modes consult no date at all, so these sheets carry no coverage and the ``Data`` label
    renders them. Calendar boundaries are not preserved by either balanced mode.

    Args:
        shape: The source.
        sheets: How many sheets to divide this source's rows into.

    Returns:
        The planned sheets, largest share first, summing to the source's row count.

    Raises:
        ValueError: ``sheets`` is not positive.
    """
    return tuple(PlannedSheet(source_alias=shape.alias, kind="period", rows=rows) for rows in balanced_split(shape.rows, sheets))
