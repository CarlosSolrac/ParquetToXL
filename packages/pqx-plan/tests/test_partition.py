"""Frozen tests for turning bucket counts into planned sheets.

Every case is a handful of integers. That is the point of planning over counts rather than rows:
a forty-million-row export and a forty-row one exercise the same arithmetic.
"""

from __future__ import annotations

from typing import Any

import pytest
from pqx_calendar.labels import period_label
from pqx_calendar.periods import UNDATED, Bucket, PeriodKey
from pqx_plan.capacity import CapacityError, SourceShape
from pqx_plan.config import CalendarPartitioning, ProfileLimits
from pqx_plan.partition import PlannedSheet, plan_balanced_sheets, plan_calendar_sheets, required_count_precision

SHAPE: SourceShape = SourceShape(alias="sales", stem="sales", columns=4, rows=0)


def _partitioning(**changes: Any) -> CalendarPartitioning:  # noqa: ANN401
    """A calendar profile, greedy over years by default."""
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


def _limits(rows: int = 100) -> ProfileLimits:
    """Ceilings where the row limit binds, so cases are stated in rows."""
    return ProfileLimits(max_data_rows_per_worksheet=rows, max_cells_per_workbook=10_000_000)


def _labels(sheets: tuple[PlannedSheet, ...]) -> list[str]:
    """Render each planned sheet's coverage, for comparison against the spec's own labels."""
    return [period_label(sheet.coverage, "numeric") if sheet.coverage is not None else "Undated" for sheet in sheets]


def _months(year: int, rows_per_month: int, months: range) -> dict[Bucket, int]:
    """Month-precision counts for one year."""
    return {PeriodKey("month", year, month): rows_per_month for month in months}


# --------------------------------------------------------------------------------------
# PlannedSheet
# --------------------------------------------------------------------------------------


def test_a_planned_sheet_records_what_it_holds() -> None:
    sheet: PlannedSheet = PlannedSheet(source_alias="sales", kind="period", rows=10)
    assert sheet.rows == 10
    assert sheet.part_index is None
    assert sheet.parts == 1


def test_a_sheet_with_negative_rows_is_refused() -> None:
    with pytest.raises(ValueError, match="holds -1 rows"):
        PlannedSheet(source_alias="sales", kind="period", rows=-1)


def test_an_overflow_sheet_without_a_fragment_index_is_refused() -> None:
    with pytest.raises(ValueError, match="disagree"):
        PlannedSheet(source_alias="sales", kind="overflow", rows=10)


def test_a_non_overflow_sheet_with_a_fragment_index_is_refused() -> None:
    with pytest.raises(ValueError, match="disagree"):
        PlannedSheet(source_alias="sales", kind="period", rows=10, part_index=1)


def test_a_sheet_split_into_no_fragments_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot be split into 0"):
        PlannedSheet(source_alias="sales", kind="period", rows=10, parts=0)


# --------------------------------------------------------------------------------------
# The counts contract
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("base_period", "expected"), [("year", "month"), ("year-month", "month"), ("year-month-day", "day")])
def test_the_required_count_precision_is_not_the_base_period(base_period: str, expected: str) -> None:
    # A year base asks for month counts, because subdividing an oversized year needs the months
    # inside it and a count keyed by year cannot produce one.
    assert required_count_precision(_partitioning(base_period=base_period)) == expected


def test_counts_at_the_wrong_precision_are_refused() -> None:
    counts: dict[Bucket, int] = {PeriodKey("year", 2025): 10}
    with pytest.raises(ValueError, match="needs counts at 'month' precision"):
        plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits())


def test_day_counts_are_refused_under_a_month_base() -> None:
    counts: dict[Bucket, int] = {PeriodKey("day", 2025, 1, 15): 10}
    with pytest.raises(ValueError, match="needs counts at 'month' precision"):
        plan_calendar_sheets(SHAPE, counts, _partitioning(base_period="year-month"), _limits())


def test_month_counts_roll_up_into_year_buckets() -> None:
    counts: dict[Bucket, int] = {PeriodKey("month", 2025, 1): 10, PeriodKey("month", 2025, 7): 20, PeriodKey("month", 2024, 3): 5}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(algorithm="calendar_periods"), _limits())
    assert [(sheet.rows, label) for sheet, label in zip(planned, _labels(planned), strict=True)] == [(5, "2024"), (30, "2025")]


# --------------------------------------------------------------------------------------
# calendar_periods
# --------------------------------------------------------------------------------------


def test_each_base_period_gets_its_own_sheet() -> None:
    counts: dict[Bucket, int] = {PeriodKey("month", 2023, 1): 30, PeriodKey("month", 2024, 1): 40, PeriodKey("month", 2025, 1): 50}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(algorithm="calendar_periods"), _limits())
    assert _labels(planned) == ["2023", "2024", "2025"]
    assert [sheet.rows for sheet in planned] == [30, 40, 50]


def test_a_month_base_keeps_each_month_separate() -> None:
    counts: dict[Bucket, int] = _months(2025, 10, range(1, 4))
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(algorithm="calendar_periods", base_period="year-month"), _limits())
    assert _labels(planned) == ["2025-01", "2025-02", "2025-03"]


def test_a_day_base_keeps_each_day_separate() -> None:
    counts: dict[Bucket, int] = {PeriodKey("day", 2025, 1, 30): 5, PeriodKey("day", 2025, 1, 31): 6}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(algorithm="calendar_periods", base_period="year-month-day"), _limits())
    assert _labels(planned) == ["2025-01-30", "2025-01-31"]


def test_missing_periods_create_no_empty_sheets() -> None:
    counts: dict[Bucket, int] = {PeriodKey("month", 2021, 1): 10, PeriodKey("month", 2025, 1): 10}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(algorithm="calendar_periods"), _limits())
    assert _labels(planned) == ["2021", "2025"]


# --------------------------------------------------------------------------------------
# calendar_greedy
# --------------------------------------------------------------------------------------


def test_successive_periods_merge_while_they_fit() -> None:
    counts: dict[Bucket, int] = {PeriodKey("month", 2023, 1): 30, PeriodKey("month", 2024, 1): 40, PeriodKey("month", 2025, 1): 50}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits(rows=100))
    # 30 + 40 = 70 fits; adding 50 would be 120, so the run is emitted and 2025 starts a new one.
    assert _labels(planned) == ["2023~2024", "2025"]
    assert [sheet.rows for sheet in planned] == [70, 50]


def test_a_merged_label_describes_coverage_not_a_guarantee_of_every_year() -> None:
    # 2023 and 2025 have rows and 2024 does not; the merged sheet is still labelled 2023~2025.
    counts: dict[Bucket, int] = {PeriodKey("month", 2023, 1): 10, PeriodKey("month", 2025, 1): 10}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits())
    assert _labels(planned) == ["2023~2025"]


def test_older_sheets_are_never_backfilled() -> None:
    # 60 then 50 then 30: the first run closes at 60 because 110 would not fit, and the 30 joins
    # the 50 rather than returning to fill the first sheet's remaining 40.
    counts: dict[Bucket, int] = {PeriodKey("month", 2023, 1): 60, PeriodKey("month", 2024, 1): 50, PeriodKey("month", 2025, 1): 30}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits(rows=100))
    assert [sheet.rows for sheet in planned] == [60, 80]
    assert _labels(planned) == ["2023", "2024~2025"]


def test_a_single_period_is_labelled_without_a_range() -> None:
    counts: dict[Bucket, int] = {PeriodKey("month", 2025, 1): 10}
    assert _labels(plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits())) == ["2025"]


# --------------------------------------------------------------------------------------
# Oversized years and the calendar grids
# --------------------------------------------------------------------------------------


def test_an_oversized_year_takes_the_first_grid_whose_every_bucket_fits() -> None:
    # 12 months of 25 = 300. Semesters are 150 each, too big. Four-month periods are 100 each,
    # exactly the capacity, so that grid wins -- and quarters are never tried.
    counts: dict[Bucket, int] = _months(2025, 25, range(1, 13))
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits(rows=100))
    assert _labels(planned) == ["2025-01~04", "2025-05~08", "2025-09~12"]
    assert [sheet.rows for sheet in planned] == [100, 100, 100]


def test_semesters_win_when_they_fit() -> None:
    counts: dict[Bucket, int] = _months(2025, 10, range(1, 13))
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits(rows=100))
    assert _labels(planned) == ["2025-01~06", "2025-07~12"]


def test_the_grids_are_alternatives_not_recursive_subdivisions() -> None:
    # Four-month periods straddle the semester boundary: 05~08 exists, so the year was divided
    # by four from January rather than by splitting a semester.
    counts: dict[Bucket, int] = _months(2025, 25, range(1, 13))
    assert "2025-05~08" in _labels(plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits(rows=100)))


def test_a_profile_may_omit_intermediate_grids() -> None:
    # With [6, 3, 1] the four-month grid is unavailable, so 300 rows fall through to quarters.
    counts: dict[Bucket, int] = _months(2025, 25, range(1, 13))
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(year_split_months=[6, 3, 1]), _limits(rows=100))
    assert _labels(planned) == ["2025-01~03", "2025-04~06", "2025-07~09", "2025-10~12"]


def test_an_empty_bucket_in_a_grid_produces_no_sheet() -> None:
    # 120 rows in January through March, capacity 100. Semesters, four-month periods and quarters
    # all put all three months in one bucket and overflow; bimonthly is the first grid that fits.
    # Every bucket from May onward is empty and contributes no sheet at all.
    counts: dict[Bucket, int] = _months(2025, 40, range(1, 4))
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits(rows=100))
    assert _labels(planned) == ["2025-01~02", "2025-03~04"]
    assert [sheet.rows for sheet in planned] == [80, 40]


def test_subdivided_months_of_one_year_do_not_merge_back_together() -> None:
    # Sealed: merging them would undo the split that was needed to make them fit.
    counts: dict[Bucket, int] = _months(2025, 25, range(1, 13))
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits(rows=100))
    assert len(planned) == 3


def test_fragments_of_an_oversized_year_do_not_merge_into_a_neighbouring_year() -> None:
    counts: dict[Bucket, int] = {PeriodKey("month", 2024, 1): 10, **_months(2025, 25, range(1, 13)), PeriodKey("month", 2026, 1): 10}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits(rows=100))
    assert _labels(planned) == ["2024", "2025-01~04", "2025-05~08", "2025-09~12", "2026"]


def test_a_year_whose_months_are_still_too_large_falls_through_to_the_overflow_rule() -> None:
    # partitioning-spec.md: emit the fitting months and apply oversized_period to each oversized
    # month. January is split by rows; the rest are emitted whole.
    counts: dict[Bucket, int] = {PeriodKey("month", 2025, 1): 250, PeriodKey("month", 2025, 2): 30}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits(rows=100))
    assert _labels(planned) == ["2025-01", "2025-01", "2025-01", "2025-02"]
    assert [sheet.rows for sheet in planned] == [84, 83, 83, 30]
    assert [sheet.part_index for sheet in planned] == [1, 2, 3, None]


def test_a_month_base_never_subdivides_and_goes_straight_to_the_overflow_rule() -> None:
    # There is no grid between a month and a day that year_split_months describes.
    counts: dict[Bucket, int] = {PeriodKey("month", 2025, 1): 250}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(base_period="year-month"), _limits(rows=100))
    assert [sheet.kind for sheet in planned] == ["overflow", "overflow", "overflow"]
    assert [sheet.rows for sheet in planned] == [84, 83, 83]


# --------------------------------------------------------------------------------------
# Overflow
# --------------------------------------------------------------------------------------


def test_an_oversized_bucket_splits_evenly_with_the_larger_fragments_first() -> None:
    counts: dict[Bucket, int] = {PeriodKey("month", 2025, 1): 250}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(base_period="year-month"), _limits(rows=100))
    assert sum(sheet.rows for sheet in planned) == 250
    assert all(sheet.parts == 3 for sheet in planned)
    assert [sheet.part_index for sheet in planned] == [1, 2, 3]


def test_an_oversized_bucket_is_refused_when_the_profile_says_error() -> None:
    counts: dict[Bucket, int] = {PeriodKey("month", 2025, 1): 250}
    with pytest.raises(CapacityError, match="oversized_period is 'error'"):
        plan_calendar_sheets(SHAPE, counts, _partitioning(base_period="year-month", oversized_period="error"), _limits(rows=100))


def test_no_row_is_lost_when_a_bucket_is_split() -> None:
    counts: dict[Bucket, int] = {PeriodKey("month", 2025, 1): 1_000}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(base_period="year-month"), _limits(rows=7))
    assert sum(sheet.rows for sheet in planned) == 1_000
    assert max(sheet.rows for sheet in planned) - min(sheet.rows for sheet in planned) <= 1


# --------------------------------------------------------------------------------------
# Undated
# --------------------------------------------------------------------------------------


def test_the_undated_bucket_comes_last_under_ascending_order() -> None:
    counts: dict[Bucket, int] = {UNDATED: 5, PeriodKey("month", 2024, 1): 10, PeriodKey("month", 2025, 1): 10}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(algorithm="calendar_periods"), _limits())
    assert planned[-1].kind == "undated"
    assert _labels(planned) == ["2024", "2025", "Undated"]


def test_the_undated_bucket_comes_last_under_descending_order_too() -> None:
    # The arrangement a sort key ranking Undated high would get wrong.
    counts: dict[Bucket, int] = {UNDATED: 5, PeriodKey("month", 2024, 1): 10, PeriodKey("month", 2025, 1): 10}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(algorithm="calendar_periods", period_order="descending"), _limits())
    assert _labels(planned) == ["2025", "2024", "Undated"]


def test_no_undated_sheet_appears_when_no_row_is_null() -> None:
    counts: dict[Bucket, int] = {PeriodKey("month", 2025, 1): 10}
    assert all(sheet.kind != "undated" for sheet in plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits()))


def test_an_oversized_undated_bucket_is_split_and_stays_undated() -> None:
    # No date policy may discard rows, and the undated template is what names these.
    counts: dict[Bucket, int] = {UNDATED: 250}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits(rows=100))
    assert [sheet.kind for sheet in planned] == ["undated", "undated", "undated"]
    assert sum(sheet.rows for sheet in planned) == 250
    assert all(sheet.part_index is None for sheet in planned)


def test_an_undated_only_source_produces_only_undated_sheets() -> None:
    counts: dict[Bucket, int] = {UNDATED: 10}
    planned: tuple[PlannedSheet, ...] = plan_calendar_sheets(SHAPE, counts, _partitioning(), _limits())
    assert len(planned) == 1
    assert planned[0].kind == "undated"
    assert planned[0].coverage is None


def test_a_source_with_no_rows_at_all_plans_no_calendar_sheets() -> None:
    # The header-only worksheet an empty source gets is the shortcut's business, not the
    # calendar's: there is no bucket to name.
    assert plan_calendar_sheets(SHAPE, {}, _partitioning(), _limits()) == ()


# --------------------------------------------------------------------------------------
# Balanced
# --------------------------------------------------------------------------------------


def test_balanced_sheets_divide_the_rows_evenly_and_carry_no_coverage() -> None:
    shape: SourceShape = SourceShape(alias="sales", stem="sales", columns=4, rows=10)
    planned: tuple[PlannedSheet, ...] = plan_balanced_sheets(shape, 3)
    assert [sheet.rows for sheet in planned] == [4, 3, 3]
    assert all(sheet.coverage is None for sheet in planned)
    assert all(sheet.kind == "period" for sheet in planned)


def test_balanced_sheets_conserve_every_row() -> None:
    shape: SourceShape = SourceShape(alias="sales", stem="sales", columns=4, rows=1_048_575)
    assert sum(sheet.rows for sheet in plan_balanced_sheets(shape, 17)) == 1_048_575


def test_balanced_into_no_sheets_is_refused() -> None:
    shape: SourceShape = SourceShape(alias="sales", stem="sales", columns=4, rows=10)
    with pytest.raises(ValueError, match="cannot divide rows into"):
        plan_balanced_sheets(shape, 0)
