"""Frozen tests for bucket keys, the month-precision refusal, and where ``Undated`` sits."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pqx_calendar.columns import DateColumn, DateDateColumn, DatetimeDateColumn, IntDateColumn, SourceWallClockTimezone
from pqx_calendar.errors import BasePeriodUnsupportedError
from pqx_calendar.periods import UNDATED, BasePeriod, Bucket, PeriodKey, PeriodOrder, UndatedBucket, column_precision, order_buckets, period_key, require_base_period_supported
from pqx_calendar.points import CalendarPoint

DATE_COLUMN: DateDateColumn = DateDateColumn(type="date")
DATETIME_COLUMN: DatetimeDateColumn = DatetimeDateColumn(type="datetime", calendar_timezone=SourceWallClockTimezone(mode="source_wall_clock"))
YYMM: IntDateColumn = IntDateColumn(type="int", format="YYMM", two_digit_year_window_start=1970)
YYYYMM: IntDateColumn = IntDateColumn(type="int", format="YYYYMM")
YYYYMMDD: IntDateColumn = IntDateColumn(type="int", format="YYYYMMDD")


@pytest.mark.parametrize(
    ("base_period", "expected"),
    [("year", PeriodKey("year", 2025)), ("year-month", PeriodKey("month", 2025, 6)), ("year-month-day", PeriodKey("day", 2025, 6, 15))],
)
def test_a_day_precision_point_buckets_on_every_grid(base_period: BasePeriod, expected: PeriodKey) -> None:
    assert period_key(CalendarPoint(2025, 6, 15), base_period) == expected


@pytest.mark.parametrize(("base_period", "expected"), [("year", PeriodKey("year", 2025)), ("year-month", PeriodKey("month", 2025, 6))])
def test_a_month_precision_point_buckets_on_the_two_coarser_grids(base_period: BasePeriod, expected: PeriodKey) -> None:
    assert period_key(CalendarPoint(2025, 6), base_period) == expected


def test_a_month_precision_point_refuses_a_daily_grid() -> None:
    # "Do not invent a day." A synthesised first-of-the-month would put every row of June
    # into a sheet named for June 1st, and nothing would raise.
    with pytest.raises(BasePeriodUnsupportedError, match="month precision"):
        period_key(CalendarPoint(2025, 6), "year-month-day")


@pytest.mark.parametrize(("key", "expected"), [(PeriodKey("year", 2025), (2025, 0, 0)), (PeriodKey("month", 2025, 6), (2025, 6, 0)), (PeriodKey("day", 2025, 6, 15), (2025, 6, 15))])
def test_a_key_sorts_by_its_calendar_position(key: PeriodKey, expected: tuple[int, int, int]) -> None:
    assert key.sort_key == expected


@pytest.mark.parametrize(
    "construct",
    [
        lambda: PeriodKey("year", 2025, 6),
        lambda: PeriodKey("month", 2025),
        lambda: PeriodKey("month", 2025, 6, 15),
        lambda: PeriodKey("day", 2025, 6),
        lambda: PeriodKey("day", 2025, None, 15),
        lambda: PeriodKey("year", 2025, None, 15),
    ],
)
def test_a_key_whose_fields_contradict_its_precision_is_refused(construct: Callable[[], PeriodKey]) -> None:
    with pytest.raises(ValueError, match="disagree"):
        construct()


def test_a_key_naming_a_date_the_calendar_lacks_is_refused() -> None:
    with pytest.raises(ValueError, match="not a calendar date"):
        PeriodKey("day", 2025, 2, 29)


@pytest.mark.parametrize(("column", "expected"), [(DATE_COLUMN, "day"), (DATETIME_COLUMN, "day"), (YYMM, "month"), (YYYYMM, "month"), (YYYYMMDD, "day")])
def test_each_column_reports_the_finest_precision_it_can_express(column: DateColumn, expected: str) -> None:
    assert column_precision(column) == expected


@pytest.mark.parametrize("column", [YYMM, YYYYMM])
def test_a_month_only_column_refuses_a_daily_grid_at_configuration_time(column: IntDateColumn) -> None:
    # The same rule period_key enforces per value, hoisted so that a profile wrong for every
    # row it will ever see is refused before row one of forty million.
    with pytest.raises(BasePeriodUnsupportedError, match="month precision"):
        require_base_period_supported(column, "year-month-day")


@pytest.mark.parametrize("column", [DATE_COLUMN, DATETIME_COLUMN, YYYYMMDD])
def test_a_day_capable_column_accepts_a_daily_grid(column: DateColumn) -> None:
    require_base_period_supported(column, "year-month-day")


@pytest.mark.parametrize("base_period", ["year", "year-month"])
@pytest.mark.parametrize("column", [DATE_COLUMN, DATETIME_COLUMN, YYMM, YYYYMM, YYYYMMDD])
def test_every_column_accepts_the_two_coarser_grids(column: DateColumn, base_period: BasePeriod) -> None:
    require_base_period_supported(column, base_period)


def test_buckets_come_back_in_ascending_order() -> None:
    keys: list[Bucket] = [PeriodKey("year", 2025), PeriodKey("year", 2023), PeriodKey("year", 2024)]
    assert order_buckets(keys, "ascending") == (PeriodKey("year", 2023), PeriodKey("year", 2024), PeriodKey("year", 2025))


def test_buckets_come_back_in_descending_order() -> None:
    keys: list[Bucket] = [PeriodKey("year", 2023), PeriodKey("year", 2025), PeriodKey("year", 2024)]
    assert order_buckets(keys, "descending") == (PeriodKey("year", 2025), PeriodKey("year", 2024), PeriodKey("year", 2023))


@pytest.mark.parametrize("period_order", ["ascending", "descending"])
def test_undated_is_last_whichever_way_the_dated_buckets_run(period_order: PeriodOrder) -> None:
    # The whole reason this is a function rather than a sorted() call at each call site: a
    # sort key that merely ranked Undated high would put it first under descending.
    keys: list[Bucket] = [UNDATED, PeriodKey("year", 2024), PeriodKey("year", 2025)]
    ordered: tuple[Bucket, ...] = order_buckets(keys, period_order)
    assert ordered[-1] is UNDATED
    assert len(ordered) == 3


def test_undated_alone_is_still_a_bucket() -> None:
    assert order_buckets([UNDATED], "ascending") == (UNDATED,)


def test_no_undated_bucket_appears_when_no_row_is_null() -> None:
    assert order_buckets([PeriodKey("year", 2025)], "ascending") == (PeriodKey("year", 2025),)


def test_ordering_nothing_yields_nothing() -> None:
    assert order_buckets([], "ascending") == ()


def test_repeated_keys_collapse_to_one_bucket() -> None:
    # Callers derive a key per row, so a month with a million rows arrives a million times.
    repeated: PeriodKey = PeriodKey("month", 2025, 6)
    keys: list[Bucket] = [repeated, repeated, repeated, PeriodKey("month", 2025, 5), UNDATED, UNDATED]
    assert order_buckets(keys, "ascending") == (PeriodKey("month", 2025, 5), PeriodKey("month", 2025, 6), UNDATED)


def test_the_undated_bucket_is_a_distinct_type_rather_than_none() -> None:
    # So that a bucket variable is never also the "no bucket yet" value, and a forgotten
    # null check fails at the type checker rather than where a sheet named None reaches Excel.
    assert isinstance(UNDATED, UndatedBucket)
    assert UNDATED is not None
    assert UndatedBucket() == UNDATED
    assert {UNDATED, UndatedBucket()} == {UNDATED}
