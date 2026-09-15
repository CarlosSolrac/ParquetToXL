"""The decoded calendar value, and the precision it carries with it."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final, Literal

MIN_YEAR: Final[int] = 1
"""The first Gregorian year v1 supports. Year zero does not exist and is refused."""

MAX_YEAR: Final[int] = 9999
"""The last Gregorian year v1 supports, matching ``datetime.date``'s own ceiling."""

type DatePrecision = Literal["month", "day"]
"""How finely a decoded value locates a point in the calendar.

``YYMM`` and ``YYYYMM`` locate a month and nothing finer. Dates, datetimes and the two
``DD`` integer formats locate a day.
"""


@dataclass(frozen=True, slots=True)
class CalendarPoint:
    """A point in the Gregorian calendar, at whichever precision its encoding supports.

    Precision is a property of the value rather than a flag beside it. The alternative --
    a ``datetime.date`` with the day set to 1 and the real precision recorded elsewhere --
    was rejected because the two travel separately and only one of them is checked: a
    month-precision value that reaches a ``year-month-day`` partitioner as a plain date
    partitions every row of March into "March 1st" without anything raising.

    Frozen and slotted rather than a Pydantic model: this is computed once per source row
    in the hot path of Phase E, it is never parsed from JSON, and it has no need of
    aliases, discriminators or JSON schema. The configuration models in ``columns.py``,
    which *are* parsed from JSON, are Pydantic.

    Ordering is by :attr:`sort_key` rather than by dataclass ``order=True``, which would
    compare ``day`` against ``None`` and raise.

    Attributes:
        year: Gregorian year, 1 through 9999.
        month: Month of that year, 1 through 12.
        day: Day of that month, or ``None`` for a month-precision value.
    """

    year: int
    month: int
    day: int | None = None

    def __post_init__(self) -> None:
        """Refuse a point the calendar does not contain.

        Validating here rather than at each call site means an invalid ``CalendarPoint``
        cannot be constructed at all, so nothing downstream -- keys, labels, grids -- has
        to re-check. ``datetime.date`` does the day-of-month work, including February 29th
        in the years that have one.

        Raises:
            ValueError: The year, month or day is outside the calendar.
        """
        if not MIN_YEAR <= self.year <= MAX_YEAR:
            year_message: str = f"year {self.year} is outside the supported range {MIN_YEAR}-{MAX_YEAR}"
            raise ValueError(year_message)
        if not 1 <= self.month <= 12:
            month_message: str = f"month {self.month} is not a calendar month"
            raise ValueError(month_message)
        if self.day is not None:
            try:
                dt.date(self.year, self.month, self.day)
            except ValueError as exc:
                day_message: str = f"{self.year:04d}-{self.month:02d}-{self.day} is not a calendar date"
                raise ValueError(day_message) from exc

    @property
    def precision(self) -> DatePrecision:
        """Return whether this point locates a day or only a month."""
        return "month" if self.day is None else "day"

    @property
    def sort_key(self) -> tuple[int, int, int]:
        """Return a tuple ordering points chronologically.

        A month-precision point sorts at day zero, ahead of every dated point in the same
        month. The two never mix within one column -- precision is fixed by the column's
        declared encoding -- so the choice only has to be total and stable, and sorting a
        month before the days it contains is the reading least likely to surprise.
        """
        return (self.year, self.month, 0 if self.day is None else self.day)

    def to_date(self) -> dt.date:
        """Return this point as a ``datetime.date``.

        Returns:
            The equivalent date.

        Raises:
            ValueError: This point carries month precision, so it names no day. Inventing
                one is exactly what ``partitioning-spec.md`` forbids.
        """
        if self.day is None:
            message: str = f"{self.year:04d}-{self.month:02d} carries month precision and names no day"
            raise ValueError(message)
        return dt.date(self.year, self.month, self.day)

    @classmethod
    def from_date(cls, value: dt.date) -> CalendarPoint:
        """Return the day-precision point naming the same calendar date.

        Args:
            value: The date to convert. A ``datetime`` is a ``date`` subclass, so passing
                one here silently discards its time; callers decode datetimes through
                :func:`pqx_calendar.decoding.decode_datetime`, which resolves the zone first.

        Returns:
            The equivalent point.
        """
        return cls(value.year, value.month, value.day)
