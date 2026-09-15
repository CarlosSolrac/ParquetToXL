"""The calendar grids an oversized year is tried against, and the rule for declaring them."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise
from typing import Final

from pqx_calendar.errors import CalendarError
from pqx_calendar.periods import PeriodKey

MONTHS_IN_YEAR: Final[int] = 12
"""Why only ``{6, 4, 3, 2, 1}`` are candidates: every other size leaves a ragged last bucket."""

YEAR_SPLIT_CHOICES: Final[tuple[int, ...]] = (6, 4, 3, 2, 1)
"""The grids, in the only order a valid ``year_split_months`` may list them in.

Six is two semesters, four is three four-month periods, three is four quarters, two is six
bimonthly buckets and one is twelve months. The four-month option is named by its size rather
than by a word, because "trimester" is used for three months as often as for four and a
terminology argument must not be able to change which rows land in which sheet.
"""


class YearSplitMonthsError(CalendarError):
    """A profile's ``year_split_months`` is not a valid preference list.

    The JSON Schema constrains the member values and their uniqueness; ordering is a semantic
    rule, so it is checked here. The valid arrays are exactly the descending subsets of
    ``{6, 4, 3, 2}`` followed by ``1`` -- sixteen in all.
    """


def validate_year_split_months(values: Sequence[int]) -> tuple[int, ...]:
    """Check a declared preference list of calendar grids and return it as a tuple.

    Args:
        values: The declared grid sizes, coarsest first.

    Returns:
        The same values, as an immutable tuple.

    Raises:
        YearSplitMonthsError: A value is not a supported grid, the list is not strictly
            descending, or it does not end in ``1``. Ending in ``1`` is required because the
            monthly grid is the last one that can be tried before ``oversized_period`` takes
            over, and a list stopping at ``3`` would silently escalate a merely large quarter
            to a row split.
    """
    unsupported: tuple[int, ...] = tuple(value for value in values if value not in YEAR_SPLIT_CHOICES)
    if unsupported:
        member_message: str = f"year_split_months {list(values)} contains {list(unsupported)}; the supported grids are {list(YEAR_SPLIT_CHOICES)}"
        raise YearSplitMonthsError(member_message)
    if any(earlier <= later for earlier, later in pairwise(values)):
        order_message: str = f"year_split_months {list(values)} is not strictly descending"
        raise YearSplitMonthsError(order_message)
    if not values or values[-1] != 1:
        end_message: str = f"year_split_months {list(values)} must end in 1, the last grid tried before oversized_period applies"
        raise YearSplitMonthsError(end_message)
    return tuple(values)


def year_grid(year: int, months_per_bucket: int) -> tuple[PeriodKey, ...]:
    """Return one year's buckets under a given grid, as the month-precision key of each bucket's start.

    Every grid starts in January. These are alternatives evaluated against the whole year, not
    recursive subdivisions: four-month periods do not nest inside semesters, so a caller tries
    each grid against the entire year and takes the first where every nonempty bucket fits.

    Args:
        year: The year being subdivided.
        months_per_bucket: One of :data:`YEAR_SPLIT_CHOICES`.

    Returns:
        The bucket starts, January first. A bucket's span is ``months_per_bucket`` months from
        its start; :func:`year_grid_spans` returns both endpoints.

    Raises:
        YearSplitMonthsError: ``months_per_bucket`` is not a supported grid.
    """
    return tuple(span[0] for span in year_grid_spans(year, months_per_bucket))


def year_grid_spans(year: int, months_per_bucket: int) -> tuple[tuple[PeriodKey, PeriodKey], ...]:
    """Return one year's buckets under a given grid, as ``(first month, last month)`` pairs.

    Args:
        year: The year being subdivided.
        months_per_bucket: One of :data:`YEAR_SPLIT_CHOICES`.

    Returns:
        The inclusive month-precision endpoints of each bucket, January first. A single-month
        grid returns pairs whose endpoints are equal, which is what makes a one-month label
        fall out of the same code path as a range.

    Raises:
        YearSplitMonthsError: ``months_per_bucket`` is not a supported grid.
    """
    if months_per_bucket not in YEAR_SPLIT_CHOICES:
        message: str = f"{months_per_bucket} is not a supported grid; the supported grids are {list(YEAR_SPLIT_CHOICES)}"
        raise YearSplitMonthsError(message)
    return tuple((PeriodKey("month", year, start), PeriodKey("month", year, start + months_per_bucket - 1)) for start in range(1, MONTHS_IN_YEAR + 1, months_per_bucket))
