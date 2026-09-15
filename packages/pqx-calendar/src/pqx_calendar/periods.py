"""Base periods, the bucket key a decoded point falls into, and the order buckets take."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from pqx_calendar.columns import DateColumn, IntDateColumn
from pqx_calendar.errors import BasePeriodUnsupportedError
from pqx_calendar.points import CalendarPoint, DatePrecision

type BasePeriod = Literal["year", "year-month", "year-month-day"]
"""The grid every sheet in a calendar profile is bucketed against."""

type PeriodPrecision = Literal["year", "month", "day"]
"""How finely a bucket or a coverage span locates itself.

Distinct from :data:`~pqx_calendar.points.DatePrecision`, which has no ``year`` member: a
*value* is never merely a year -- every encoding v1 reads carries at least a month -- while a
*bucket* very often is.
"""

type PeriodOrder = Literal["ascending", "descending"]
"""The direction dated buckets take. ``Undated`` ignores it; see :func:`order_buckets`."""

PRECISION_OF_BASE_PERIOD: Final[dict[BasePeriod, PeriodPrecision]] = {"year": "year", "year-month": "month", "year-month-day": "day"}
"""What each base period's buckets locate."""

BASE_PERIOD_OF_PRECISION: Final[dict[PeriodPrecision, BasePeriod]] = {"year": "year", "month": "year-month", "day": "year-month-day"}
"""The inverse, for turning a split grid's precision back into a base period."""


@dataclass(frozen=True, slots=True)
class PeriodKey:
    """The bucket a decoded point falls into on a given grid.

    Carries its own precision rather than a reference to the base period that produced it,
    because an oversized year is subdivided into month-precision buckets that no longer match
    the profile's ``base_period``. A key that claimed to be a year while spanning January
    through June would render the wrong label.

    Attributes:
        precision: Whether this bucket names a year, a month or a day.
        year: Gregorian year.
        month: Month, or ``None`` at year precision.
        day: Day, or ``None`` above day precision.
    """

    precision: PeriodPrecision
    year: int
    month: int | None = None
    day: int | None = None

    def __post_init__(self) -> None:
        """Refuse a key whose fields contradict its declared precision.

        Raises:
            ValueError: A field required by the precision is missing, or one it does not
                have is present.
        """
        wants_month: bool = self.precision in {"month", "day"}
        wants_day: bool = self.precision == "day"
        if wants_month != (self.month is not None):
            month_message: str = f"precision {self.precision!r} and month={self.month!r} disagree"
            raise ValueError(month_message)
        if wants_day != (self.day is not None):
            day_message: str = f"precision {self.precision!r} and day={self.day!r} disagree"
            raise ValueError(day_message)
        CalendarPoint(self.year, 1 if self.month is None else self.month, self.day)

    @property
    def sort_key(self) -> tuple[int, int, int]:
        """Return a tuple ordering keys of one precision chronologically."""
        return (self.year, self.month or 0, self.day or 0)


@dataclass(frozen=True, slots=True)
class UndatedBucket:
    """The bucket holding rows whose partition column is null.

    A distinct type rather than ``None``, so that a bucket variable is never also the
    "no bucket yet" value and a forgotten null check fails at the type checker rather than
    at the point where a sheet named ``None`` reaches Excel.
    """


UNDATED: Final[UndatedBucket] = UndatedBucket()
"""The single undated bucket. Compare with ``is``; equality also holds, the dataclass being frozen and field-free."""

type Bucket = PeriodKey | UndatedBucket
"""Either a calendar bucket or the undated one."""


def period_key(point: CalendarPoint, base_period: BasePeriod) -> PeriodKey:
    """Return the bucket a decoded point falls into on the given grid.

    Args:
        point: The decoded value.
        base_period: The profile's grid.

    Returns:
        The bucket key, at the base period's precision.

    Raises:
        BasePeriodUnsupportedError: ``base_period`` is ``year-month-day`` and the point
            carries only month precision, so the day would have to be invented.
    """
    precision: PeriodPrecision = PRECISION_OF_BASE_PERIOD[base_period]
    if precision == "day" and point.day is None:
        message: str = f"base period {base_period!r} needs a day, and {point.year:04d}-{point.month:02d} carries month precision"
        raise BasePeriodUnsupportedError(message)
    if precision == "year":
        return PeriodKey("year", point.year)
    if precision == "month":
        return PeriodKey("month", point.year, point.month)
    return PeriodKey("day", point.year, point.month, point.day)


def column_precision(column: DateColumn) -> DatePrecision:
    """Return the finest precision a registered column's encoding can express.

    Args:
        column: The registered column.

    Returns:
        ``"month"`` for the two month-only integer formats, ``"day"`` for everything else.
    """
    return column.precision if isinstance(column, IntDateColumn) else "day"


def require_base_period_supported(column: DateColumn, base_period: BasePeriod) -> None:
    """Refuse, before any row is read, a grid the column's encoding cannot express.

    The same rule :func:`period_key` enforces per value, hoisted to configuration time. A
    profile pairing ``YYYYMM`` with ``year-month-day`` is wrong for every row it will ever
    see, and finding that out on row one of forty million is a worse way to learn it.

    Args:
        column: The registered column.
        base_period: The profile's grid.

    Only an integer column can fail this: ``date`` and ``datetime`` encodings always carry a
    day, which is why the check tests the class rather than calling :func:`column_precision`.
    Routing through that helper would leave a branch for "a date column with month precision"
    that no input can reach.

    Raises:
        BasePeriodUnsupportedError: The column carries month precision and the grid needs a day.
    """
    if base_period == "year-month-day" and isinstance(column, IntDateColumn) and column.precision == "month":
        message: str = f"format {column.format!r} carries month precision, so base period 'year-month-day' cannot be derived from it without inventing a day"
        raise BasePeriodUnsupportedError(message)


def order_buckets(buckets: Iterable[Bucket], period_order: PeriodOrder) -> tuple[Bucket, ...]:
    """Return the buckets in output order, with ``Undated`` last however the rest are ordered.

    "Last regardless of ``period_order``" is the whole reason this is a function rather than
    a ``sorted`` call at each call site. A sort key that merely ranked ``Undated`` high would
    put it *first* under ``descending``, which is the one arrangement the spec forbids.

    Duplicate keys are collapsed: two rows in the same month name one bucket, and callers
    pass the keys they derived per row.

    Args:
        buckets: The buckets present in the data, in any order, possibly with repeats.
        period_order: The direction the dated buckets take.

    Returns:
        The distinct dated buckets in the requested order, then the undated one if present.
    """
    dated: set[PeriodKey] = set()
    has_undated: bool = False
    bucket: Bucket
    for bucket in buckets:
        if isinstance(bucket, PeriodKey):
            dated.add(bucket)
        else:
            has_undated = True
    # Sorted into its own list first: assigning straight into a `list[Bucket]` pushes that
    # wider element type back into `sorted`, and the key function then reads as one that must
    # also handle the undated bucket, which has no calendar position to sort on.
    chronological: list[PeriodKey] = sorted(dated, key=lambda item: item.sort_key, reverse=period_order == "descending")
    ordered: list[Bucket] = list(chronological)
    if has_undated:
        ordered.append(UNDATED)
    return tuple(ordered)
