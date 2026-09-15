"""Frozen tests for ordering a profile's sheets and packing them into workbooks."""

from __future__ import annotations

import pytest
from pqx_calendar.labels import CoverageSpan
from pqx_calendar.periods import PeriodKey
from pqx_plan.allocate import AllocatedWorkbook, allocate_workbooks, order_profile_sheets
from pqx_plan.capacity import CapacityError
from pqx_plan.partition import PlannedSheet

WIDTHS: dict[str, int] = {"sales": 4, "returns": 2}


def _year(alias: str, year: int, rows: int) -> PlannedSheet:
    """A period sheet covering one year."""
    return PlannedSheet(source_alias=alias, kind="period", rows=rows, coverage=CoverageSpan.of(PeriodKey("year", year)))


def _month(alias: str, year: int, month: int, rows: int) -> PlannedSheet:
    """A period sheet covering one month."""
    return PlannedSheet(source_alias=alias, kind="period", rows=rows, coverage=CoverageSpan.of(PeriodKey("month", year, month)))


def _undated(alias: str, rows: int) -> PlannedSheet:
    """An undated sheet."""
    return PlannedSheet(source_alias=alias, kind="undated", rows=rows)


def _balanced(alias: str, rows: int) -> PlannedSheet:
    """A balanced sheet, which covers no calendar."""
    return PlannedSheet(source_alias=alias, kind="period", rows=rows)


# --------------------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------------------


def test_sheets_are_ordered_by_period_first_then_source() -> None:
    # The ordering the spec implies rather than states: only this one makes a period span
    # workbooks, which is the consequence it does state.
    ordered: tuple[PlannedSheet, ...] = order_profile_sheets(
        [(0, [_year("sales", 2024, 10), _year("sales", 2025, 10)]), (1, [_year("returns", 2024, 5), _year("returns", 2025, 5)])],
        "ascending",
    )
    assert [(sheet.source_alias, sheet.coverage.first.year if sheet.coverage else None) for sheet in ordered] == [
        ("sales", 2024),
        ("returns", 2024),
        ("sales", 2025),
        ("returns", 2025),
    ]


def test_descending_order_reverses_the_periods_and_not_the_sources() -> None:
    ordered: tuple[PlannedSheet, ...] = order_profile_sheets(
        [(0, [_year("sales", 2025, 10), _year("sales", 2024, 10)]), (1, [_year("returns", 2025, 5), _year("returns", 2024, 5)])],
        "descending",
    )
    assert [(sheet.source_alias, sheet.coverage.first.year if sheet.coverage else None) for sheet in ordered] == [
        ("sales", 2025),
        ("returns", 2025),
        ("sales", 2024),
        ("returns", 2024),
    ]


def test_finer_periods_order_within_their_year() -> None:
    ordered: tuple[PlannedSheet, ...] = order_profile_sheets([(0, [_month("sales", 2025, 3, 1), _month("sales", 2025, 1, 1), _month("sales", 2024, 12, 1)])], "ascending")
    assert [(sheet.coverage.first.year, sheet.coverage.first.month) for sheet in ordered if sheet.coverage] == [(2024, 12), (2025, 1), (2025, 3)]


@pytest.mark.parametrize("period_order", ["ascending", "descending"])
def test_undated_sheets_sort_after_everything_either_way(period_order: str) -> None:
    ordered: tuple[PlannedSheet, ...] = order_profile_sheets(
        [(0, [_undated("sales", 3), _year("sales", 2025, 10)]), (1, [_year("returns", 2024, 5)])],
        period_order,  # type: ignore[arg-type]
    )
    assert ordered[-1].kind == "undated"


def test_balanced_sheets_order_by_source_then_slice() -> None:
    # They describe no calendar, so the only meaningful arrangement is the order they were sliced.
    ordered: tuple[PlannedSheet, ...] = order_profile_sheets([(0, [_balanced("sales", 4), _balanced("sales", 3)]), (1, [_balanced("returns", 2)])], "ascending")
    assert [(sheet.source_alias, sheet.rows) for sheet in ordered] == [("sales", 4), ("sales", 3), ("returns", 2)]


def test_ordering_nothing_yields_nothing() -> None:
    assert order_profile_sheets([], "ascending") == ()


# --------------------------------------------------------------------------------------
# Packing
# --------------------------------------------------------------------------------------


def test_sheets_that_fit_share_one_workbook() -> None:
    books: tuple[AllocatedWorkbook, ...] = allocate_workbooks([_year("sales", 2024, 10), _year("sales", 2025, 10)], WIDTHS, budget=1000)
    assert len(books) == 1
    assert books[0].index == 1
    assert len(books[0].sheets) == 2


def test_a_new_workbook_opens_when_the_next_sheet_will_not_fit() -> None:
    # 4 columns, 10 rows = 44 cells each. A budget of 80 holds one and not two.
    books: tuple[AllocatedWorkbook, ...] = allocate_workbooks([_year("sales", 2024, 10), _year("sales", 2025, 10)], WIDTHS, budget=80)
    assert [book.index for book in books] == [1, 2]
    assert all(len(book.sheets) == 1 for book in books)


def test_a_fitting_sheet_is_never_split_to_fill_remaining_capacity() -> None:
    # Leaves 36 cells unused in the first workbook, which is what preserves calendar boundaries.
    books: tuple[AllocatedWorkbook, ...] = allocate_workbooks([_year("sales", 2024, 10), _year("sales", 2025, 10)], WIDTHS, budget=80)
    assert books[0].cells(WIDTHS) == 44


def test_the_budget_is_the_only_thing_that_opens_a_workbook() -> None:
    # Year boundaries alone do not force one: three different years share a workbook here.
    books: tuple[AllocatedWorkbook, ...] = allocate_workbooks([_year("sales", 2023, 1), _year("sales", 2024, 1), _year("sales", 2025, 1)], WIDTHS, budget=1000)
    assert len(books) == 1


def test_an_oversized_year_may_span_several_workbooks() -> None:
    sheets: list[PlannedSheet] = [_month("sales", 2025, month, 10) for month in range(1, 7)]
    books: tuple[AllocatedWorkbook, ...] = allocate_workbooks(sheets, WIDTHS, budget=100)
    assert len(books) == 3
    assert sum(len(book.sheets) for book in books) == 6


def test_a_workbook_may_hold_only_some_sources_for_a_period() -> None:
    # The fan-in consequence, stated in the spec because it must not be inferred: ordered
    # allocation resolves a period too expensive for one workbook by spanning workbooks.
    sheets: tuple[PlannedSheet, ...] = order_profile_sheets([(0, [_year("sales", 2025, 20)]), (1, [_year("returns", 2025, 20)])], "ascending")
    books: tuple[AllocatedWorkbook, ...] = allocate_workbooks(sheets, WIDTHS, budget=90)
    assert len(books) == 2
    assert [sheet.source_alias for sheet in books[0].sheets] == ["sales"]
    assert [sheet.source_alias for sheet in books[1].sheets] == ["returns"]


def test_workbooks_are_indexed_from_one() -> None:
    books: tuple[AllocatedWorkbook, ...] = allocate_workbooks([_year("sales", year, 10) for year in range(2020, 2025)], WIDTHS, budget=80)
    assert [book.index for book in books] == [1, 2, 3, 4, 5]


def test_no_sheets_produce_no_workbooks() -> None:
    # An empty workbook is a file Excel cannot open, and there is nothing to name.
    assert allocate_workbooks([], WIDTHS, budget=1000) == ()


def test_a_sheet_too_large_for_an_empty_workbook_is_refused() -> None:
    # Planning used R_s, derived from this same budget, so reaching this means the sheets were
    # planned against different limits from the ones being allocated under.
    with pytest.raises(CapacityError, match="planned against different limits"):
        allocate_workbooks([_year("sales", 2025, 1000)], WIDTHS, budget=100)


def test_a_workbook_reports_its_cost_as_a_sum_across_sources() -> None:
    book: AllocatedWorkbook = AllocatedWorkbook(index=1, sheets=(_year("sales", 2025, 10), _year("returns", 2025, 10)))
    assert book.cells(WIDTHS) == 4 * 11 + 2 * 11
