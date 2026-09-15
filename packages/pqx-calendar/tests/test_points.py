"""Frozen tests for the decoded calendar value and the precision it carries."""

from __future__ import annotations

import datetime as dt

import pytest
from pqx_calendar.points import MAX_YEAR, MIN_YEAR, CalendarPoint


def test_a_day_precision_point_reports_its_day() -> None:
    point: CalendarPoint = CalendarPoint(2025, 1, 31)
    assert point.precision == "day"
    assert point.sort_key == (2025, 1, 31)
    assert point.to_date() == dt.date(2025, 1, 31)


def test_a_month_precision_point_names_no_day() -> None:
    point: CalendarPoint = CalendarPoint(2025, 1)
    assert point.precision == "month"
    assert point.day is None
    assert point.sort_key == (2025, 1, 0)


def test_a_month_precision_point_refuses_to_invent_a_day() -> None:
    # partitioning-spec.md: "Do not invent a day." A synthesised first-of-the-month would
    # partition and sort plausibly and wrongly, which is worse than an exception.
    with pytest.raises(ValueError, match="month precision"):
        CalendarPoint(2025, 1).to_date()


def test_from_date_keeps_the_calendar_date() -> None:
    assert CalendarPoint.from_date(dt.date(1999, 12, 31)) == CalendarPoint(1999, 12, 31)


@pytest.mark.parametrize("year", [0, -1, MAX_YEAR + 1])
def test_a_year_outside_the_supported_range_is_refused(year: int) -> None:
    with pytest.raises(ValueError, match="outside the supported range"):
        CalendarPoint(year, 1, 1)


@pytest.mark.parametrize("year", [MIN_YEAR, MAX_YEAR])
def test_the_range_endpoints_themselves_are_accepted(year: int) -> None:
    assert CalendarPoint(year, 1, 1).year == year


@pytest.mark.parametrize("month", [0, 13, -1])
def test_a_month_outside_one_through_twelve_is_refused(month: int) -> None:
    with pytest.raises(ValueError, match="not a calendar month"):
        CalendarPoint(2025, month, 1)


@pytest.mark.parametrize(("year", "month", "day"), [(2025, 2, 29), (2025, 4, 31), (2025, 1, 32), (2025, 1, 0)])
def test_a_day_the_month_does_not_have_is_refused(year: int, month: int, day: int) -> None:
    with pytest.raises(ValueError, match="not a calendar date"):
        CalendarPoint(year, month, day)


def test_february_29th_is_accepted_in_a_leap_year() -> None:
    # 2024 is a leap year and 2025 is not; the check delegates to datetime.date rather than
    # reimplementing the rule, so the century cases come along for free.
    assert CalendarPoint(2024, 2, 29).to_date() == dt.date(2024, 2, 29)


def test_a_point_is_frozen_and_hashable() -> None:
    point: CalendarPoint = CalendarPoint(2025, 6, 15)
    assert {point, CalendarPoint(2025, 6, 15)} == {point}
    with pytest.raises(AttributeError):
        point.year = 2026  # type: ignore[misc]
