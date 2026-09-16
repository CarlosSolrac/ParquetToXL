"""Frozen tests for putting rows into calendar buckets and into final output order."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from pqx_calendar.columns import DateColumn, DateDateColumn, DatetimeDateColumn, IntDateColumn, SourceWallClockTimezone, ZoneTimezone
from pqx_calendar.errors import BasePeriodUnsupportedError, DateDecodeError, UnknownTimezoneError
from pqx_calendar.periods import UNDATED, Bucket, PeriodKey, order_buckets
from pqx_pipeline.bucketing import BUCKET_KEY_COLUMN, SOURCE_ORDINAL_COLUMN, BucketingError, bucket_of_key, bucket_of_row, key_of_bucket, ordered_by_bucket
from pqx_plan.config import CalendarPartitioning, SortKey

if TYPE_CHECKING:
    from collections.abc import Sequence

DATE_COLUMN: DateDateColumn = DateDateColumn(type="date")


def _partitioning(**changes: Any) -> CalendarPartitioning:  # noqa: ANN401
    """A greedy calendar profile over years."""
    settings: dict[str, Any] = {
        "algorithm": "calendar_greedy",
        "base_period": "year",
        "period_order": "ascending",
        "year_split_months": [6, 4, 3, 2, 1],
        "oversized_period": "balanced_rows",
        "null_dates": "separate",
    }
    settings.update(changes)
    return CalendarPartitioning.model_validate(settings)


def _frame(dates: Sequence[dt.date | None], **extra: Sequence[Any]) -> pl.DataFrame:
    """A frame with an id, a date column and whatever else a case needs."""
    data: dict[str, Sequence[Any]] = {"id": list(range(len(dates))), "booked": list(dates), **extra}
    return pl.DataFrame(data)


# --------------------------------------------------------------------------------------
# One row
# --------------------------------------------------------------------------------------


def test_a_dated_row_lands_in_its_month_bucket() -> None:
    assert bucket_of_row(dt.date(2025, 3, 9), DATE_COLUMN, _partitioning(), column_name="booked", row_ordinal=0) == PeriodKey("month", 2025, 3)


def test_a_null_row_lands_in_the_undated_bucket() -> None:
    assert bucket_of_row(None, DATE_COLUMN, _partitioning(), column_name="booked", row_ordinal=0) is UNDATED


def test_a_null_row_is_refused_when_the_profile_says_so() -> None:
    # No date policy may discard rows, so the only alternatives are a bucket and a refusal.
    with pytest.raises(BucketingError, match="null_dates is 'error'"):
        bucket_of_row(None, DATE_COLUMN, _partitioning(null_dates="error"), column_name="booked", row_ordinal=7)


def test_an_unreadable_value_names_the_column_and_the_row() -> None:
    column: IntDateColumn = IntDateColumn(type="int", format="YYYYMMDD")
    caught: pytest.ExceptionInfo[DateDecodeError]
    with pytest.raises(DateDecodeError) as caught:
        bucket_of_row(20251345, column, _partitioning(), column_name="returned_on", row_ordinal=41)
    assert "returned_on" in str(caught.value)
    assert "41" in str(caught.value)


def test_a_daily_grid_keys_rows_by_day() -> None:
    assert bucket_of_row(dt.date(2025, 3, 9), DATE_COLUMN, _partitioning(base_period="year-month-day"), column_name="booked", row_ordinal=0) == PeriodKey("day", 2025, 3, 9)


# --------------------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------------------


def test_buckets_come_out_in_period_order_with_undated_last() -> None:
    frame: pl.DataFrame = _frame([dt.date(2025, 3, 1), None, dt.date(2024, 7, 2), dt.date(2025, 1, 9)])
    ordered: pl.DataFrame
    counts: dict[Bucket, int]
    ordered, counts = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked")
    assert ordered["id"].to_list() == [2, 3, 0, 1]
    assert list(counts)[-1] is UNDATED


def test_descending_order_reverses_the_dated_buckets_and_not_the_undated_one() -> None:
    frame: pl.DataFrame = _frame([dt.date(2024, 7, 2), None, dt.date(2025, 3, 1)])
    ordered: pl.DataFrame
    counts: dict[Bucket, int]
    ordered, counts = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(period_order="descending"), column_name="booked")
    assert ordered["id"].to_list() == [2, 0, 1]
    assert list(counts)[-1] is UNDATED


def test_months_of_one_year_arrive_contiguous_and_in_order() -> None:
    # The key is taken at month precision even under a year base, so a year that later turns out
    # to need subdividing already has its months arranged for slicing.
    frame: pl.DataFrame = _frame([dt.date(2025, 6, 1), dt.date(2025, 1, 1), dt.date(2025, 11, 1), dt.date(2025, 3, 1)])
    ordered: pl.DataFrame
    _: dict[Bucket, int]
    ordered, _ = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked")
    assert [value.month for value in ordered["booked"].to_list()] == [1, 3, 6, 11]


def test_source_order_is_preserved_inside_a_bucket_when_no_sort_is_requested() -> None:
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 5), dt.date(2025, 1, 2), dt.date(2025, 1, 9)])
    ordered: pl.DataFrame
    _: dict[Bucket, int]
    ordered, _ = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked")
    assert ordered["id"].to_list() == [0, 1, 2]


def test_the_requested_sort_applies_inside_each_bucket_not_across_them() -> None:
    # A calendar export sorted by customer is sorted by customer inside each sheet, not globally
    # across all years.
    frame: pl.DataFrame = _frame(
        [dt.date(2025, 1, 1), dt.date(2025, 1, 2), dt.date(2024, 5, 1), dt.date(2024, 5, 2)],
        customer=["zed", "amy", "zed", "amy"],
    )
    ordered: pl.DataFrame
    _: dict[Bucket, int]
    ordered, _ = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked", sort=[SortKey(column="customer", direction="ascending", nulls="last")])
    assert ordered["customer"].to_list() == ["amy", "zed", "amy", "zed"]
    assert [value.year for value in ordered["booked"].to_list()] == [2024, 2024, 2025, 2025]


def test_a_descending_sort_key_is_honoured() -> None:
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 1)] * 3, customer=["amy", "zed", "mel"])
    ordered: pl.DataFrame
    _: dict[Bucket, int]
    ordered, _ = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked", sort=[SortKey(column="customer", direction="descending", nulls="last")])
    assert ordered["customer"].to_list() == ["zed", "mel", "amy"]


def test_null_placement_is_honoured() -> None:
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 1)] * 3, customer=["amy", None, "zed"])
    first: pl.DataFrame
    _: dict[Bucket, int]
    first, _ = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked", sort=[SortKey(column="customer", direction="ascending", nulls="first")])
    last: pl.DataFrame
    last, _ = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked", sort=[SortKey(column="customer", direction="ascending", nulls="last")])
    assert first["customer"].to_list()[0] is None
    assert last["customer"].to_list()[-1] is None


def test_the_source_ordinal_breaks_ties_so_a_rerun_repeats_the_arrangement() -> None:
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 1)] * 4, customer=["amy", "amy", "amy", "amy"])
    ordered: pl.DataFrame
    _: dict[Bucket, int]
    ordered, _ = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked", sort=[SortKey(column="customer", direction="ascending", nulls="last")])
    assert ordered["id"].to_list() == [0, 1, 2, 3]


def test_no_hidden_date_sort_is_injected_into_the_requested_row_sort() -> None:
    # Inside one bucket the dates vary, and the requested sort is the only thing arranging them.
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 9), dt.date(2025, 1, 2)], customer=["amy", "zed"])
    ordered: pl.DataFrame
    _: dict[Bucket, int]
    ordered, _ = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked", sort=[SortKey(column="customer", direction="ascending", nulls="last")])
    assert ordered["customer"].to_list() == ["amy", "zed"]
    assert [value.day for value in ordered["booked"].to_list()] == [9, 2]


# --------------------------------------------------------------------------------------
# Counts
# --------------------------------------------------------------------------------------


def test_the_counts_are_keyed_at_the_precision_the_planner_expects() -> None:
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 1), dt.date(2025, 3, 1), dt.date(2025, 3, 2)])
    counts: dict[Bucket, int]
    _: pl.DataFrame
    _, counts = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked")
    assert counts == {PeriodKey("month", 2025, 1): 1, PeriodKey("month", 2025, 3): 2}


def test_the_counts_add_up_to_every_row() -> None:
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 1), None, dt.date(2024, 3, 1), None, dt.date(2025, 1, 2)])
    counts: dict[Bucket, int]
    _: pl.DataFrame
    _, counts = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked")
    assert sum(counts.values()) == 5
    assert counts[UNDATED] == 2


def test_an_empty_frame_buckets_into_nothing() -> None:
    frame: pl.DataFrame = pl.DataFrame({"id": pl.Series([], dtype=pl.Int64), "booked": pl.Series([], dtype=pl.Date)})
    ordered: pl.DataFrame
    counts: dict[Bucket, int]
    ordered, counts = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked")
    assert counts == {}
    assert ordered.height == 0


# --------------------------------------------------------------------------------------
# Temporary keys
# --------------------------------------------------------------------------------------


def test_the_temporary_keys_never_reach_the_returned_frame() -> None:
    # An extra column changes both the digest the sidecar recorded and the additive identity
    # verification relies on.
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 1), None])
    ordered: pl.DataFrame
    _: dict[Bucket, int]
    ordered, _ = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked")
    assert ordered.columns == frame.columns
    assert BUCKET_KEY_COLUMN not in ordered.columns
    assert SOURCE_ORDINAL_COLUMN not in ordered.columns


def test_a_source_column_that_collides_with_a_temporary_key_is_accommodated() -> None:
    # Checked against the real columns rather than assumed to be free.
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 2), dt.date(2025, 1, 1)], **{BUCKET_KEY_COLUMN: ["a", "b"], SOURCE_ORDINAL_COLUMN: ["c", "d"]})
    ordered: pl.DataFrame
    _: dict[Bucket, int]
    ordered, _ = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked")
    assert ordered.columns == frame.columns
    assert ordered[BUCKET_KEY_COLUMN].to_list() == ["a", "b"]


# --------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------


def test_a_partition_column_the_frame_does_not_have_is_refused() -> None:
    with pytest.raises(BucketingError, match="no column 'missing'"):
        ordered_by_bucket(_frame([dt.date(2025, 1, 1)]), DATE_COLUMN, _partitioning(), column_name="missing")


def test_a_month_only_column_under_a_daily_grid_is_refused_before_a_row_is_read() -> None:
    column: DateColumn = IntDateColumn(type="int", format="YYYYMM")
    frame: pl.DataFrame = pl.DataFrame({"id": [1], "booked": [202501]})
    with pytest.raises(BasePeriodUnsupportedError):
        ordered_by_bucket(frame, column, _partitioning(base_period="year-month-day"), column_name="booked")


def test_a_null_under_the_error_policy_is_refused() -> None:
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 1), None])
    with pytest.raises(BucketingError, match="null_dates is 'error'"):
        ordered_by_bucket(frame, DATE_COLUMN, _partitioning(null_dates="error"), column_name="booked")


# --------------------------------------------------------------------------------------
# The packed bucket key
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bucket",
    [PeriodKey("year", 2025), PeriodKey("month", 2025, 3), PeriodKey("day", 2025, 3, 9), PeriodKey("month", 1, 1), PeriodKey("day", 9999, 12, 31), PeriodKey("day", 2024, 2, 29)],
)
def test_a_bucket_survives_being_packed_into_a_key_and_unpacked(bucket: PeriodKey) -> None:
    # Precision is recoverable without being stored: an absent month or day is 0, and a present
    # one is 1-12 or 1-31, so the zeros say which fields the key carries.
    assert bucket_of_key(key_of_bucket(bucket)) == bucket


def test_the_packed_key_orders_dated_buckets_the_way_order_buckets_does() -> None:
    # Numeric order on the key IS lexicographic order on (year, month, day), because month never
    # reaches 100 and day never reaches 100. That equivalence is what lets the frame sort on the
    # key alone instead of on a rank derived from order_buckets.
    buckets: list[PeriodKey] = [PeriodKey("month", 2025, 3), PeriodKey("month", 2024, 12), PeriodKey("month", 2025, 1), PeriodKey("month", 2024, 1)]
    assert tuple(sorted(buckets, key=key_of_bucket)) == order_buckets(buckets, "ascending")
    assert tuple(sorted(buckets, key=key_of_bucket, reverse=True)) == order_buckets(buckets, "descending")


def test_the_key_of_a_year_bucket_leaves_the_month_and_day_digits_zero() -> None:
    # Spelled out rather than round-tripped, because the staged Parquet carries this number and a
    # human reading 20250000 should be able to tell what it says.
    assert key_of_bucket(PeriodKey("year", 2025)) == 20250000
    assert key_of_bucket(PeriodKey("month", 2025, 3)) == 20250300
    assert key_of_bucket(PeriodKey("day", 2025, 3, 9)) == 20250309


# --------------------------------------------------------------------------------------
# Repeated values, and which row a refusal names
# --------------------------------------------------------------------------------------


def test_a_value_repeated_across_the_frame_is_counted_once_per_row() -> None:
    # Decoding is per distinct value; counting is per row. Conflating them would under-count
    # every source whose dates repeat, which is every source.
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 1), dt.date(2024, 5, 1), dt.date(2025, 1, 1), dt.date(2025, 1, 1), dt.date(2024, 5, 1)])
    counts: dict[Bucket, int]
    _: pl.DataFrame
    _, counts = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked")
    assert counts == {PeriodKey("month", 2024, 5): 2, PeriodKey("month", 2025, 1): 3}


def test_a_refusal_names_the_first_row_holding_the_offending_value_not_a_later_one() -> None:
    # The scalar loop stopped at the first bad row. Decoding distinct values loses the ordinal, so
    # this pins that it is recovered rather than replaced by whichever row the grouping surfaced.
    column: IntDateColumn = IntDateColumn(type="int", format="YYYYMMDD")
    frame: pl.DataFrame = pl.DataFrame({"id": [0, 1, 2, 3], "booked": [20250101, 20251345, 20250102, 20251345]})
    caught: pytest.ExceptionInfo[DateDecodeError]
    with pytest.raises(DateDecodeError) as caught:
        ordered_by_bucket(frame, column, _partitioning(), column_name="booked")
    assert "row 1" in str(caught.value)


def test_an_unreadable_value_before_a_null_is_the_refusal_that_wins() -> None:
    # Both are refusals under null_dates 'error'. The scalar loop raised for whichever came first
    # in the frame, and that is what the message must still describe.
    column: IntDateColumn = IntDateColumn(type="int", format="YYYYMMDD")
    frame: pl.DataFrame = pl.DataFrame({"id": [0, 1, 2], "booked": [20251345, None, 20250101]})
    with pytest.raises(DateDecodeError):
        ordered_by_bucket(frame, column, _partitioning(null_dates="error"), column_name="booked")


def test_a_null_before_an_unreadable_value_is_the_refusal_that_wins() -> None:
    column: IntDateColumn = IntDateColumn(type="int", format="YYYYMMDD")
    frame: pl.DataFrame = pl.DataFrame({"id": [0, 1, 2], "booked": [None, 20251345, 20250101]})
    with pytest.raises(BucketingError, match="row 0"):
        ordered_by_bucket(frame, column, _partitioning(null_dates="error"), column_name="booked")


# --------------------------------------------------------------------------------------
# The frame's order agrees with the plan's order
# --------------------------------------------------------------------------------------


def _bucket_runs(frame: pl.DataFrame, column: DateColumn, partitioning: CalendarPartitioning) -> list[Bucket]:
    """The buckets the frame's rows fall into, in row order, with consecutive repeats collapsed."""
    runs: list[Bucket] = []
    value: object
    bucket: Bucket
    for value in frame["booked"].to_list():
        bucket = bucket_of_row(value, column, partitioning, column_name="booked", row_ordinal=0)
        if not runs or runs[-1] != bucket:
            runs.append(bucket)
    return runs


@pytest.mark.parametrize("period_order", ["ascending", "descending"])
def test_the_rows_arrive_in_exactly_the_bucket_order_the_counts_promise(period_order: str) -> None:
    # _slices cuts sheets with a positional cursor and trusts frame order to match plan order. The
    # row sort and the counts dict are now stated separately, so a drift between them would
    # misassign rows to sheets with nothing raising. This is the assertion that catches it.
    frame: pl.DataFrame = _frame([dt.date(2025, 3, 1), None, dt.date(2024, 7, 2), dt.date(2025, 1, 9), None, dt.date(2024, 7, 30)])
    partitioning: CalendarPartitioning = _partitioning(period_order=period_order)
    ordered: pl.DataFrame
    counts: dict[Bucket, int]
    ordered, counts = ordered_by_bucket(frame, DATE_COLUMN, partitioning, column_name="booked")
    assert _bucket_runs(ordered, DATE_COLUMN, partitioning) == list(counts)


def test_the_rows_arrive_in_the_promised_order_with_no_undated_bucket_at_all() -> None:
    frame: pl.DataFrame = _frame([dt.date(2025, 3, 1), dt.date(2024, 7, 2), dt.date(2025, 1, 9)])
    ordered: pl.DataFrame
    counts: dict[Bucket, int]
    ordered, counts = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked")
    assert _bucket_runs(ordered, DATE_COLUMN, _partitioning()) == list(counts)


# --------------------------------------------------------------------------------------
# Timestamps finer than Python can represent
# --------------------------------------------------------------------------------------


WALL_CLOCK: DatetimeDateColumn = DatetimeDateColumn(type="datetime", calendar_timezone=SourceWallClockTimezone(mode="source_wall_clock"))


def _nanoseconds(values: Sequence[int]) -> pl.DataFrame:
    """A frame whose partition column is a nanosecond-resolution timestamp."""
    return pl.DataFrame({"id": list(range(len(values))), "booked": pl.Series(values, dtype=pl.Int64).cast(pl.Datetime("ns"))})


def test_a_timestamp_finer_than_a_microsecond_still_lands_in_its_calendar_bucket() -> None:
    # Decoding reads the value through Python, which has no nanoseconds, and that is harmless --
    # truncation cannot move a date. Matching the decoded answer back onto the frame through that
    # same Python value is not harmless: it matches nothing, and the row would go silently undated.
    frame: pl.DataFrame = _nanoseconds([1735689600000000001])
    counts: dict[Bucket, int]
    _: pl.DataFrame
    _, counts = ordered_by_bucket(frame, WALL_CLOCK, _partitioning(null_dates="error"), column_name="booked")
    assert counts == {PeriodKey("month", 2025, 1): 1}


def test_two_timestamps_inside_one_microsecond_are_two_distinct_values() -> None:
    # They collapse to one value when rendered through Python, so a mapping built from rendered
    # values carries the same key twice and Polars refuses it.
    frame: pl.DataFrame = _nanoseconds([1735689600000000001, 1735689600000000002])
    counts: dict[Bucket, int]
    ordered: pl.DataFrame
    ordered, counts = ordered_by_bucket(frame, WALL_CLOCK, _partitioning(null_dates="error"), column_name="booked")
    assert counts == {PeriodKey("month", 2025, 1): 2}
    assert ordered["id"].to_list() == [0, 1]


# --------------------------------------------------------------------------------------
# Column names Polars would otherwise read as patterns
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("column_name", ["*", "^date$", "booked"])
def test_a_partition_column_named_like_a_pattern_is_still_one_literal_column(column_name: str) -> None:
    # Parquet puts no such restriction on a column name, and nothing upstream rejects one: the
    # names module governs worksheet and file names, not source columns. Selecting by expression
    # would read "*" as every column and "^date$" as a regex, so both must be selected literally.
    frame: pl.DataFrame = pl.DataFrame({"id": [0, 1, 2], column_name: [dt.date(2025, 1, 1), None, dt.date(2024, 5, 1)]})
    ordered: pl.DataFrame
    counts: dict[Bucket, int]
    ordered, counts = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name=column_name)
    assert counts == {PeriodKey("month", 2024, 5): 1, PeriodKey("month", 2025, 1): 1, UNDATED: 1}
    assert ordered["id"].to_list() == [2, 0, 1]
    assert ordered.columns == frame.columns


# --------------------------------------------------------------------------------------
# Timestamps, and the zone that decides which day they fall on
# --------------------------------------------------------------------------------------


def _zoned(zone: str) -> DatetimeDateColumn:
    """A datetime column read against a named zone."""
    return DatetimeDateColumn(type="datetime", calendar_timezone=ZoneTimezone(mode="zone", zone=zone))


def _instants(moments: Sequence[str], *, stored: str = "UTC") -> pl.DataFrame:
    """A frame of tz-aware timestamps, given as ISO strings in the stored zone."""
    parsed: list[dt.datetime] = [dt.datetime.fromisoformat(moment) for moment in moments]
    return pl.DataFrame({"id": list(range(len(moments))), "booked": pl.Series(parsed, dtype=pl.Datetime("us", time_zone=stored))})


def test_two_instants_in_one_stored_day_can_fall_on_different_days_in_the_named_zone() -> None:
    # Mexico City is UTC-6. Both rows are 2026-01-02 in UTC; locally one is the 1st and one is the
    # 2nd. Anything that groups by the stored day before consulting the zone merges them and
    # answers a different question than the one asked.
    frame: pl.DataFrame = _instants(["2026-01-02T01:00:00+00:00", "2026-01-02T23:00:00+00:00"])
    counts: dict[Bucket, int]
    _: pl.DataFrame
    _, counts = ordered_by_bucket(frame, _zoned("America/Mexico_City"), _partitioning(base_period="year-month-day"), column_name="booked")
    assert counts == {PeriodKey("day", 2026, 1, 1): 1, PeriodKey("day", 2026, 1, 2): 1}


def test_the_named_zone_can_move_an_instant_into_the_previous_year() -> None:
    # The case decode_datetime's own docstring names: 2026-01-01T02:00+00:00 is still 2025 in
    # Mexico City, so it belongs in the 2025 workbook.
    frame: pl.DataFrame = _instants(["2026-01-01T02:00:00+00:00"])
    counts: dict[Bucket, int]
    _: pl.DataFrame
    _, counts = ordered_by_bucket(frame, _zoned("America/Mexico_City"), _partitioning(), column_name="booked")
    assert counts == {PeriodKey("month", 2025, 12): 1}


def test_a_zone_offset_of_a_half_hour_still_splits_the_day_where_it_actually_falls() -> None:
    # Kolkata is UTC+5:30, so its midnight is 18:30 the previous day in UTC. A collapse that
    # assumed whole-hour offsets would put both of these on the same day.
    frame: pl.DataFrame = _instants(["2026-03-01T18:00:00+00:00", "2026-03-01T19:00:00+00:00"])
    counts: dict[Bucket, int]
    _: pl.DataFrame
    _, counts = ordered_by_bucket(frame, _zoned("Asia/Kolkata"), _partitioning(base_period="year-month-day"), column_name="booked")
    assert counts == {PeriodKey("day", 2026, 3, 1): 1, PeriodKey("day", 2026, 3, 2): 1}


def test_instants_either_side_of_a_daylight_saving_shift_are_read_in_local_time() -> None:
    # New York springs forward at 07:00 UTC on 2026-03-08. Both instants are the 8th locally.
    frame: pl.DataFrame = _instants(["2026-03-08T06:00:00+00:00", "2026-03-08T08:00:00+00:00"])
    counts: dict[Bucket, int]
    _: pl.DataFrame
    _, counts = ordered_by_bucket(frame, _zoned("America/New_York"), _partitioning(base_period="year-month-day"), column_name="booked")
    assert counts == {PeriodKey("day", 2026, 3, 8): 2}


def test_wall_clock_mode_reads_the_stored_reading_and_ignores_the_zone_entirely() -> None:
    # The same instant as the test above, stored in Mexico City rather than UTC. Its reading there
    # is 2025-12-31T20:00, and wall-clock mode takes that reading at face value -- which is the
    # same answer the named zone reached, but reached without consulting any zone.
    frame: pl.DataFrame = _instants(["2026-01-01T02:00:00+00:00"], stored="America/Mexico_City")
    counts: dict[Bucket, int]
    _: pl.DataFrame
    _, counts = ordered_by_bucket(frame, WALL_CLOCK, _partitioning(), column_name="booked")
    assert counts == {PeriodKey("month", 2025, 12): 1}


def test_a_naive_column_under_a_named_zone_is_refused() -> None:
    # Assigning a zone to a naive timestamp needs a DST ambiguity policy v1 does not have.
    naive: dt.datetime = dt.datetime.fromisoformat("2026-01-01T00:00:00")
    frame: pl.DataFrame = pl.DataFrame({"id": [0], "booked": pl.Series([naive], dtype=pl.Datetime("us"))})
    with pytest.raises(DateDecodeError, match="naive"):
        ordered_by_bucket(frame, _zoned("America/Mexico_City"), _partitioning(), column_name="booked")


def test_a_zone_this_machine_does_not_have_is_refused_once_rather_than_per_row() -> None:
    frame: pl.DataFrame = _instants(["2026-01-01T02:00:00+00:00", "2026-02-01T02:00:00+00:00"])
    with pytest.raises(UnknownTimezoneError):
        ordered_by_bucket(frame, _zoned("Not/AZone"), _partitioning(), column_name="booked")


def test_a_date_column_registered_as_a_datetime_is_refused() -> None:
    frame: pl.DataFrame = _frame([dt.date(2026, 1, 1)])
    with pytest.raises(DateDecodeError, match="expected a datetime"):
        ordered_by_bucket(frame, WALL_CLOCK, _partitioning(), column_name="booked")


def _oracle_counts(frame: pl.DataFrame, column: DateColumn, partitioning: CalendarPartitioning) -> dict[Bucket, int]:
    """Row counts per bucket, taken one row at a time through the scalar decoder."""
    tally: dict[Bucket, int] = {}
    value: object
    bucket: Bucket
    for value in frame["booked"].to_list():
        bucket = bucket_of_row(value, column, partitioning, column_name="booked", row_ordinal=0)
        tally[bucket] = tally.get(bucket, 0) + 1
    return tally


def test_the_decoders_timezone_database_is_the_one_that_decides_the_day() -> None:
    # Polars converts zones through its own bundled database and decode_cell converts through this
    # machine's. They do not always agree: for Africa/Casablanca in October 2026 they differ by an
    # hour, which is enough to move a row to the wrong day. Whichever database this host carries,
    # the decoder is the authority -- so the expectation is taken from it rather than written down,
    # and what is asserted is that no grouping quietly substituted the other one.
    frame: pl.DataFrame = _instants(["2026-10-01T23:30:00+00:00", "2026-10-02T00:30:00+00:00"])
    column: DateColumn = _zoned("Africa/Casablanca")
    partitioning: CalendarPartitioning = _partitioning(base_period="year-month-day")
    counts: dict[Bucket, int]
    _: pl.DataFrame
    _, counts = ordered_by_bucket(frame, column, partitioning, column_name="booked")
    assert counts == _oracle_counts(frame, column, partitioning)


def test_a_naive_timestamp_column_reads_its_own_fields_with_no_zone_involved() -> None:
    # The one shape where a day can be derived without consulting any timezone database at all:
    # the stored reading is the answer, and truncating it is field arithmetic.
    moments: list[dt.datetime] = [dt.datetime.fromisoformat("2026-01-01T23:30:00"), dt.datetime.fromisoformat("2026-01-02T00:30:00"), dt.datetime.fromisoformat("2026-01-02T09:00:00")]
    frame: pl.DataFrame = pl.DataFrame({"id": [0, 1, 2], "booked": pl.Series(moments, dtype=pl.Datetime("us"))})
    counts: dict[Bucket, int]
    _: pl.DataFrame
    _, counts = ordered_by_bucket(frame, WALL_CLOCK, _partitioning(base_period="year-month-day"), column_name="booked")
    assert counts == {PeriodKey("day", 2026, 1, 1): 1, PeriodKey("day", 2026, 1, 2): 2}
