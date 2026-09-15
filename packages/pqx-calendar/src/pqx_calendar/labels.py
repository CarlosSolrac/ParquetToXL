"""Rendering a coverage span as the label that becomes part of a sheet or workbook name.

Reproduces the two tables in ``partitioning-spec.md`` exactly. The choice between ``numeric``
and ``abbreviated`` changes labels only and never a partition boundary, so nothing in this
module may be consulted while bucketing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from pqx_calendar.errors import CalendarError
from pqx_calendar.periods import UNDATED, Bucket, PeriodKey, PeriodPrecision, UndatedBucket

type MonthFormat = Literal["numeric", "abbreviated"]
"""How a month renders inside a label."""

MONTH_ABBREVIATIONS: Final[tuple[str, ...]] = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
"""The fixed English abbreviations, spelled out rather than derived from ``%b``.

``strftime`` reads the computer's locale, so the same export would produce ``Sep`` on one
machine and ``sept.`` on another -- a difference that changes a filename, and therefore
changes whether a re-run overwrites the previous run's output or sits beside it.
"""

RANGE_SEPARATOR: Final[str] = "~"
"""The literal character between range endpoints. No backslash is stored before it, in JSON or in a name."""

UNDATED_LABEL: Final[str] = "Undated"
"""The label of the null-date bucket."""

UNSPLIT_LABEL: Final[str] = "Data"
"""The label balanced and unsplit output uses, where no calendar coverage is being described."""

UNDATED_SUFFIX: Final[str] = f"_{UNDATED_LABEL}"
"""Appended to a workbook's coverage label when the workbook also holds undated rows."""


class CoverageError(CalendarError):
    """A coverage span was assembled from endpoints that cannot describe one span."""


@dataclass(frozen=True, slots=True)
class CoverageSpan:
    """The calendar coverage a sheet or workbook represents, as inclusive endpoints.

    A span rather than a single key because a greedy merge produces sheets covering several
    base periods, and a year split produces sheets covering several months. A single bucket is
    the degenerate case where both endpoints are equal, which is deliberate: it makes
    "one month" and "a range of months" one code path, so they cannot drift apart.

    Attributes:
        first: The earliest bucket covered.
        last: The latest bucket covered.
    """

    first: PeriodKey
    last: PeriodKey

    def __post_init__(self) -> None:
        """Refuse endpoints of different precision, or a span running backwards.

        Ranges show the earlier endpoint first even when sheets are ordered descending, so a
        caller under ``period_order: descending`` must not hand its endpoints over in the
        order it happens to hold them.

        Raises:
            CoverageError: The endpoints disagree on precision, or ``last`` precedes ``first``.
        """
        if self.first.precision != self.last.precision:
            precision_message: str = f"span endpoints disagree on precision: {self.first.precision!r} and {self.last.precision!r}"
            raise CoverageError(precision_message)
        if self.last.sort_key < self.first.sort_key:
            order_message: str = f"span runs backwards: {self.last} precedes {self.first}; ranges name the earlier endpoint first"
            raise CoverageError(order_message)

    @property
    def precision(self) -> PeriodPrecision:
        """Return the precision both endpoints share."""
        return self.first.precision

    @property
    def is_single(self) -> bool:
        """Return whether the span covers exactly one bucket, which is labelled without a range."""
        return self.first == self.last

    @classmethod
    def of(cls, key: PeriodKey) -> CoverageSpan:
        """Return the span covering exactly one bucket.

        Args:
            key: The bucket.

        Returns:
            A span whose endpoints are both ``key``.
        """
        return cls(key, key)

    @classmethod
    def over(cls, keys: Iterable[PeriodKey]) -> CoverageSpan:
        """Return the span from the earliest through the latest of the given buckets.

        Args:
            keys: The buckets covered, in any order.

        Returns:
            The enclosing span.

        Raises:
            CoverageError: ``keys`` is empty, so there is no coverage to describe, or the
                buckets disagree on precision.
        """
        material: tuple[PeriodKey, ...] = tuple(keys)
        if not material:
            message: str = "a coverage span needs at least one bucket"
            raise CoverageError(message)
        return cls(min(material, key=lambda key: key.sort_key), max(material, key=lambda key: key.sort_key))


def render_month(month: int, month_format: MonthFormat) -> str:
    """Return one month as it appears inside a label.

    Args:
        month: The month, 1 through 12.
        month_format: The profile's naming choice.

    Returns:
        A zero-padded two-digit number, or the fixed English abbreviation.
    """
    return f"{month:02d}" if month_format == "numeric" else MONTH_ABBREVIATIONS[month - 1]


def render_year(year: int) -> str:
    """Return one year as it appears inside a label.

    Zero-padded to four digits. Every year in the spec's tables is already four digits, so
    this is invisible there; it matters for the years below 1000 that ``MIN_YEAR`` admits,
    where an unpadded ``5`` would sort after ``1999`` in any directory listing.

    Args:
        year: The year, 1 through 9999.

    Returns:
        The four-digit year.
    """
    return f"{year:04d}"


def period_label(coverage: Bucket | CoverageSpan, month_format: MonthFormat) -> str:
    """Return the ``period_label`` token for the coverage a sheet or workbook represents.

    Reproduces ``partitioning-spec.md``'s table:

    ======================================  ==========================  ==========================
    Period                                  ``numeric``                 ``abbreviated``
    ======================================  ==========================  ==========================
    One year                                ``2025``                    ``2025``
    Multiple years                          ``2022~2024``               ``2022~2024``
    One month                               ``2025-01``                 ``2025-Jan``
    Month range within one year             ``2025-01~06``              ``2025-Jan~Jun``
    Month range crossing a year boundary    ``2025-11~2026-02``         ``2025-Nov~2026-Feb``
    One day                                 ``2025-01-31``              ``2025-01-31``
    Day range                               ``2025-01-31~2025-02-02``   ``2025-01-31~2025-02-02``
    ======================================  ==========================  ==========================

    A day label carries no month name in either format, which is the table's own answer and
    not an omission: ``2025-Jan-31`` appears nowhere in it.

    Args:
        coverage: One bucket, the undated bucket, or a span of buckets.
        month_format: The profile's naming choice.

    Returns:
        The label.
    """
    if isinstance(coverage, UndatedBucket):
        return UNDATED_LABEL
    span: CoverageSpan = CoverageSpan.of(coverage) if isinstance(coverage, PeriodKey) else coverage
    if span.precision == "year":
        return render_year(span.first.year) if span.is_single else f"{render_year(span.first.year)}{RANGE_SEPARATOR}{render_year(span.last.year)}"
    if span.precision == "month":
        return _month_label(span, month_format)
    return _day_label(span)


def _month_label(span: CoverageSpan, month_format: MonthFormat) -> str:
    """Return the label of a month-precision span.

    A range inside one year names that year once and both months after it; a range crossing a
    year boundary names both years, because ``2025-11~02`` would read as a span running
    backwards inside 2025.

    The ``or 1`` fallbacks are unreachable: ``PeriodKey`` refuses a month-precision key with no
    month, and the caller has already checked the precision. They are there so the expression
    is total for the type checker without a ``raise`` that no input could trigger.
    """
    first_month: str = render_month(span.first.month or 1, month_format)
    if span.is_single:
        return f"{render_year(span.first.year)}-{first_month}"
    last_month: str = render_month(span.last.month or 1, month_format)
    if span.first.year == span.last.year:
        return f"{render_year(span.first.year)}-{first_month}{RANGE_SEPARATOR}{last_month}"
    return f"{render_year(span.first.year)}-{first_month}{RANGE_SEPARATOR}{render_year(span.last.year)}-{last_month}"


def _day_label(span: CoverageSpan) -> str:
    """Return the label of a day-precision span, which both month formats spell identically."""
    first_day: str = f"{render_year(span.first.year)}-{span.first.month or 1:02d}-{span.first.day or 1:02d}"
    if span.is_single:
        return first_day
    last_day: str = f"{render_year(span.last.year)}-{span.last.month or 1:02d}-{span.last.day or 1:02d}"
    return f"{first_day}{RANGE_SEPARATOR}{last_day}"


def workbook_period_label(buckets: Iterable[Bucket], month_format: MonthFormat) -> str:
    """Return the ``period_label`` token for a workbook holding the given buckets.

    A workbook may hold only some of a profile's sources for a period, and a period may span
    workbooks, so this summarises earliest through latest coverage rather than asserting a
    single period. Exact coverage belongs in the manifest; this is a name.

    Args:
        buckets: Every bucket the workbook holds, in any order.
        month_format: The profile's naming choice.

    Returns:
        The coverage label, with ``_Undated`` appended when the workbook also holds undated
        rows, or exactly ``Undated`` when that is all it holds.

    Raises:
        CoverageError: ``buckets`` is empty, or the dated buckets disagree on precision.
    """
    material: tuple[Bucket, ...] = tuple(buckets)
    dated: tuple[PeriodKey, ...] = tuple(bucket for bucket in material if isinstance(bucket, PeriodKey))
    has_undated: bool = any(bucket is UNDATED for bucket in material)
    if not dated:
        if not has_undated:
            message: str = "a workbook label needs at least one bucket"
            raise CoverageError(message)
        return UNDATED_LABEL
    label: str = period_label(CoverageSpan.over(dated), month_format)
    return f"{label}{UNDATED_SUFFIX}" if has_undated else label
