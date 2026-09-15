"""Frozen tests for the calendar grids an oversized year is tried against."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pqx_calendar.grids import YEAR_SPLIT_CHOICES, YearSplitMonthsError, validate_year_split_months, year_grid, year_grid_spans
from pqx_calendar.periods import PeriodKey


@pytest.mark.parametrize("values", [[6, 4, 3, 2, 1], [6, 3, 1], [1], [6, 1], [4, 2, 1], [3, 1], [2, 1], [6, 4, 1]])
def test_a_strictly_descending_list_ending_in_one_is_accepted(values: list[int]) -> None:
    assert validate_year_split_months(values) == tuple(values)


def test_the_valid_lists_are_exactly_the_descending_subsets_ending_in_one() -> None:
    # partitioning-spec.md: "sixteen in all". Enumerating them here proves the validator
    # accepts the whole set rather than an arbitrary subset of it.
    subsets: list[tuple[int, ...]] = []
    mask: int
    for mask in range(16):
        chosen: tuple[int, ...] = tuple(value for index, value in enumerate((6, 4, 3, 2)) if mask >> index & 1)
        subsets.append((*chosen, 1))
    assert len(subsets) == 16
    candidate: tuple[int, ...]
    for candidate in subsets:
        assert validate_year_split_months(candidate) == candidate


@pytest.mark.parametrize("values", [[5, 1], [12, 6, 1], [0, 1], [7, 1], [-1]])
def test_a_grid_outside_the_supported_set_is_refused(values: list[int]) -> None:
    with pytest.raises(YearSplitMonthsError, match="supported grids"):
        validate_year_split_months(values)


@pytest.mark.parametrize("values", [[1, 6], [3, 3, 1], [1, 1], [2, 4, 1], [6, 6, 1]])
def test_a_list_that_is_not_strictly_descending_is_refused(values: list[int]) -> None:
    with pytest.raises(YearSplitMonthsError, match="strictly descending"):
        validate_year_split_months(values)


@pytest.mark.parametrize("values", [[6, 3], [3], [6, 4, 3, 2]])
def test_a_list_not_ending_in_one_is_refused(values: list[int]) -> None:
    # The monthly grid is the last one tried before oversized_period takes over, so a list
    # stopping at 3 would silently escalate a merely large quarter to a row split.
    with pytest.raises(YearSplitMonthsError, match="must end in 1"):
        validate_year_split_months(values)


def test_an_empty_list_is_refused() -> None:
    with pytest.raises(YearSplitMonthsError, match="must end in 1"):
        validate_year_split_months([])


@pytest.mark.parametrize(
    ("months_per_bucket", "expected"),
    [
        (6, ((1, 6), (7, 12))),
        (4, ((1, 4), (5, 8), (9, 12))),
        (3, ((1, 3), (4, 6), (7, 9), (10, 12))),
        (2, ((1, 2), (3, 4), (5, 6), (7, 8), (9, 10), (11, 12))),
        (1, ((1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 6), (7, 7), (8, 8), (9, 9), (10, 10), (11, 11), (12, 12))),
    ],
)
def test_each_grid_divides_the_year_at_its_calendar_boundaries(months_per_bucket: int, expected: tuple[tuple[int, int], ...]) -> None:
    spans: tuple[tuple[PeriodKey, PeriodKey], ...] = year_grid_spans(2025, months_per_bucket)
    assert tuple((first.month, last.month) for first, last in spans) == expected


@pytest.mark.parametrize("months_per_bucket", YEAR_SPLIT_CHOICES)
def test_every_grid_starts_in_january_and_covers_the_whole_year(months_per_bucket: int) -> None:
    spans: tuple[tuple[PeriodKey, PeriodKey], ...] = year_grid_spans(2025, months_per_bucket)
    assert spans[0][0] == PeriodKey("month", 2025, 1)
    assert spans[-1][1] == PeriodKey("month", 2025, 12)
    covered: list[int] = [month for first, last in spans for month in range(first.month or 0, (last.month or 0) + 1)]
    assert covered == list(range(1, 13))


@pytest.mark.parametrize("months_per_bucket", YEAR_SPLIT_CHOICES)
def test_the_bucket_starts_are_the_first_endpoint_of_each_span(months_per_bucket: int) -> None:
    assert year_grid(2025, months_per_bucket) == tuple(first for first, _ in year_grid_spans(2025, months_per_bucket))


def test_the_monthly_grid_makes_each_span_a_single_month() -> None:
    # Which is what lets "one month" and "a range of months" be one code path in labelling.
    assert all(first == last for first, last in year_grid_spans(2025, 1))


def test_the_grids_are_alternatives_rather_than_nested_subdivisions() -> None:
    # Four-month periods do not nest inside semesters: 1~4 straddles the 1~6 boundary.
    semesters: tuple[tuple[PeriodKey, PeriodKey], ...] = year_grid_spans(2025, 6)
    thirds: tuple[tuple[PeriodKey, PeriodKey], ...] = year_grid_spans(2025, 4)
    assert (thirds[1][0].month, thirds[1][1].month) == (5, 8)
    assert (semesters[0][1].month, semesters[1][0].month) == (6, 7)


@pytest.mark.parametrize("months_per_bucket", [5, 7, 12, 0, -1])
def test_an_unsupported_grid_size_is_refused(months_per_bucket: int) -> None:
    with pytest.raises(YearSplitMonthsError, match="not a supported grid"):
        year_grid_spans(2025, months_per_bucket)


def test_the_preference_order_is_coarsest_first() -> None:
    # A caller tries each grid against the whole year and takes the first where every
    # nonempty bucket fits, so the declared order is the order it walks.
    assert YEAR_SPLIT_CHOICES == (6, 4, 3, 2, 1)


def test_a_tuple_is_accepted_as_readily_as_a_list() -> None:
    values: Sequence[int] = (6, 1)
    assert validate_year_split_months(values) == (6, 1)
