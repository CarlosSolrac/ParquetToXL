"""Frozen tests for putting rows into calendar buckets and into final output order."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from pqx_calendar.columns import DateColumn, DateDateColumn, IntDateColumn
from pqx_calendar.errors import BasePeriodUnsupportedError, DateDecodeError
from pqx_calendar.periods import UNDATED, Bucket, PeriodKey
from pqx_pipeline.bucketing import BUCKET_RANK_COLUMN, SOURCE_ORDINAL_COLUMN, BucketingError, bucket_of_row, ordered_by_bucket
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
    assert BUCKET_RANK_COLUMN not in ordered.columns
    assert SOURCE_ORDINAL_COLUMN not in ordered.columns


def test_a_source_column_that_collides_with_a_temporary_key_is_accommodated() -> None:
    # Checked against the real columns rather than assumed to be free.
    frame: pl.DataFrame = _frame([dt.date(2025, 1, 2), dt.date(2025, 1, 1)], **{BUCKET_RANK_COLUMN: ["a", "b"], SOURCE_ORDINAL_COLUMN: ["c", "d"]})
    ordered: pl.DataFrame
    _: dict[Bucket, int]
    ordered, _ = ordered_by_bucket(frame, DATE_COLUMN, _partitioning(), column_name="booked")
    assert ordered.columns == frame.columns
    assert ordered[BUCKET_RANK_COLUMN].to_list() == ["a", "b"]


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
