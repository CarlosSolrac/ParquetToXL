"""Frozen tests for the arithmetic every partitioning algorithm is bounded by.

Each case is four numbers rather than a fixture, which is the point of planning over
:class:`SourceShape` instead of over a frame.
"""

from __future__ import annotations

import pytest
from pqx_plan.capacity import (
    EXCEL_MAX_COLUMNS,
    CapacityError,
    SourceShape,
    balanced_split,
    balanced_workbook_cost,
    fits_one_workbook,
    minimum_balanced_workbooks,
    minimum_sheets,
    sheet_capacity,
    sheet_cells,
    workbook_cells,
)
from pqx_plan.config import ProfileLimits


def _limits(rows: int = 1_048_575, cells: int = 10_000_000) -> ProfileLimits:
    """The profile ceilings, with the spec's initial cell budget as the default."""
    return ProfileLimits(max_data_rows_per_worksheet=rows, max_cells_per_workbook=cells)


# --------------------------------------------------------------------------------------
# SourceShape
# --------------------------------------------------------------------------------------


def test_a_shape_reports_its_header_cost_and_emptiness() -> None:
    shape: SourceShape = SourceShape(alias="sales", stem="sales", columns=12, rows=0)
    assert shape.header_cells == 12
    assert shape.is_empty
    assert not SourceShape(alias="sales", stem="sales", columns=12, rows=1).is_empty


@pytest.mark.parametrize("columns", [0, -1])
def test_a_source_with_no_columns_is_refused(columns: int) -> None:
    # Would make every capacity expression divide by zero.
    with pytest.raises(CapacityError, match="at least one"):
        SourceShape(alias="sales", stem="sales", columns=columns, rows=10)


def test_a_source_wider_than_a_worksheet_is_refused() -> None:
    # Not a planning decision: no budget makes this exportable.
    with pytest.raises(CapacityError, match="a worksheet holds"):
        SourceShape(alias="sales", stem="sales", columns=EXCEL_MAX_COLUMNS + 1, rows=1)


def test_the_widest_exportable_source_is_accepted() -> None:
    assert SourceShape(alias="sales", stem="sales", columns=EXCEL_MAX_COLUMNS, rows=1).columns == EXCEL_MAX_COLUMNS


def test_a_negative_row_count_is_refused() -> None:
    with pytest.raises(ValueError, match="reports -1 rows"):
        SourceShape(alias="sales", stem="sales", columns=3, rows=-1)


# --------------------------------------------------------------------------------------
# Cell cost
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("columns", "rows", "expected"), [(3, 0, 3), (3, 1, 6), (12, 100, 1212), (1, 1_000_000, 1_000_001)])
def test_a_sheet_costs_its_rows_plus_one_header(columns: int, rows: int, expected: int) -> None:
    assert sheet_cells(columns, rows) == expected


def test_a_workbook_costs_the_sum_across_its_sheets_not_a_product() -> None:
    # The fan-in case: each sheet may come from a source of a different width, so there is no
    # single C for the workbook.
    assert workbook_cells([(3, 10), (12, 5), (1, 0)]) == 3 * 11 + 12 * 6 + 1 * 1


def test_a_workbook_of_no_sheets_costs_nothing() -> None:
    assert workbook_cells([]) == 0


# --------------------------------------------------------------------------------------
# R_s
# --------------------------------------------------------------------------------------


def test_the_row_limit_binds_when_the_budget_is_generous() -> None:
    shape: SourceShape = SourceShape(alias="s", stem="s", columns=3, rows=0)
    assert sheet_capacity(shape, _limits(rows=1000, cells=10_000_000)) == 1000


def test_the_budget_binds_when_the_source_is_wide() -> None:
    # floor(10_000_000 / 100) - 1 = 99_999, below the row limit.
    shape: SourceShape = SourceShape(alias="s", stem="s", columns=100, rows=0)
    assert sheet_capacity(shape, _limits(rows=1_048_575, cells=10_000_000)) == 99_999


def test_the_header_is_paid_for_before_the_first_data_row() -> None:
    # A budget of exactly two rows' worth of a 3-column source leaves room for one data row.
    shape: SourceShape = SourceShape(alias="s", stem="s", columns=3, rows=0)
    assert sheet_capacity(shape, _limits(cells=6)) == 1


def test_a_budget_too_small_for_a_header_and_one_row_is_refused() -> None:
    # Stated as a refusal because a sheet of zero data rows would loop forever producing
    # empty sheets.
    shape: SourceShape = SourceShape(alias="wide", stem="wide", columns=3, rows=10)
    with pytest.raises(CapacityError, match="at least 6 cells are needed"):
        sheet_capacity(shape, _limits(cells=5))


# --------------------------------------------------------------------------------------
# Sheet counts and balanced division
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("rows", "capacity", "expected"), [(0, 10, 1), (1, 10, 1), (10, 10, 1), (11, 10, 2), (20, 10, 2), (21, 10, 3)])
def test_the_fewest_sheets_that_hold_the_rows(rows: int, capacity: int, expected: int) -> None:
    assert minimum_sheets(rows, capacity) == expected


def test_an_empty_source_still_needs_one_sheet() -> None:
    # The export's shape must not depend on whether a period happened to have data.
    assert minimum_sheets(0, 1000) == 1


@pytest.mark.parametrize("capacity", [0, -1])
def test_a_sheet_capacity_that_places_no_rows_is_refused(capacity: int) -> None:
    with pytest.raises(ValueError, match="places no rows"):
        minimum_sheets(10, capacity)


@pytest.mark.parametrize(
    ("total", "parts", "expected"),
    [(10, 3, (4, 3, 3)), (9, 3, (3, 3, 3)), (10, 1, (10,)), (0, 3, (0, 0, 0)), (2, 3, (1, 1, 0)), (7, 2, (4, 3)), (100, 7, (15, 15, 14, 14, 14, 14, 14))],
)
def test_rows_divide_with_the_larger_outputs_first(total: int, parts: int, expected: tuple[int, ...]) -> None:
    # partitioning-spec.md: `total mod parts` outputs of floor(total/parts) + 1, then the smaller.
    assert balanced_split(total, parts) == expected


@pytest.mark.parametrize(("total", "parts"), [(10, 3), (9, 3), (0, 5), (1_048_575, 17)])
def test_a_balanced_split_conserves_every_row_and_differs_by_at_most_one(total: int, parts: int) -> None:
    shares: tuple[int, ...] = balanced_split(total, parts)
    assert sum(shares) == total
    assert len(shares) == parts
    assert max(shares) - min(shares) <= 1


@pytest.mark.parametrize("parts", [0, -1])
def test_dividing_into_no_outputs_is_refused(parts: int) -> None:
    with pytest.raises(ValueError, match="cannot divide rows into"):
        balanced_split(10, parts)


def test_dividing_a_negative_row_count_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot divide -1 rows"):
        balanced_split(-1, 3)


# --------------------------------------------------------------------------------------
# The single-sheet shortcut
# --------------------------------------------------------------------------------------


def test_small_data_takes_the_shortcut() -> None:
    shapes: list[SourceShape] = [SourceShape(alias="sales", stem="sales", columns=12, rows=1000), SourceShape(alias="returns", stem="returns", columns=4, rows=50)]
    assert fits_one_workbook(shapes, _limits())


def test_the_budget_overrides_the_shortcut_even_when_rows_fit() -> None:
    # partitioning-spec.md's own example: 500,000 rows by 100 columns fits Excel's row limit and
    # holds over 50 million cells with headers, so it must split under a 10-million budget.
    shapes: list[SourceShape] = [SourceShape(alias="wide", stem="wide", columns=100, rows=500_000)]
    assert not fits_one_workbook(shapes, _limits(cells=10_000_000))


def test_many_rows_of_a_narrow_source_still_fit_one_workbook() -> None:
    # The converse example: 2 million rows by 3 columns needs several sheets on rows alone yet
    # fits one workbook on cells -- so the shortcut does not apply, on the row test.
    shapes: list[SourceShape] = [SourceShape(alias="narrow", stem="narrow", columns=3, rows=2_000_000)]
    assert workbook_cells([(3, 2_000_000)]) < 10_000_000
    assert not fits_one_workbook(shapes, _limits(rows=1_048_575, cells=10_000_000))


def test_an_empty_source_takes_the_shortcut_when_its_header_fits() -> None:
    shapes: list[SourceShape] = [SourceShape(alias="sales", stem="sales", columns=12, rows=0)]
    assert fits_one_workbook(shapes, _limits(cells=12))


def test_the_shortcut_is_refused_when_the_budget_cannot_hold_a_header_and_a_row() -> None:
    shapes: list[SourceShape] = [SourceShape(alias="sales", stem="sales", columns=12, rows=5)]
    with pytest.raises(CapacityError):
        fits_one_workbook(shapes, _limits(cells=11))


def test_the_sum_across_sources_is_what_decides() -> None:
    # Each source alone fits; together they do not.
    shapes: list[SourceShape] = [SourceShape(alias="a", stem="a", columns=10, rows=400), SourceShape(alias="b", stem="b", columns=10, rows=400)]
    assert fits_one_workbook([shapes[0]], _limits(rows=1000, cells=5000))
    assert not fits_one_workbook(shapes, _limits(rows=1000, cells=5000))


# --------------------------------------------------------------------------------------
# Balanced workbooks
# --------------------------------------------------------------------------------------


def test_the_balanced_workbook_cost_charges_one_header_per_sheet() -> None:
    # C_s * (n + ceil(n / L)). With L=10 and n=25 that is three sheets, so three headers.
    shapes: list[SourceShape] = [SourceShape(alias="a", stem="a", columns=4, rows=25)]
    assert balanced_workbook_cost(shapes, 1, _limits(rows=10, cells=10_000_000)) == 4 * (25 + 3)


def test_the_balanced_workbook_cost_uses_the_row_limit_not_the_budget_capacity() -> None:
    # The documented exception: this mode caps sheets at L, which is cheaper because fewer
    # sheets means fewer repeated headers. The workbook budget still binds through the sum.
    shapes: list[SourceShape] = [SourceShape(alias="a", stem="a", columns=4, rows=20)]
    assert balanced_workbook_cost(shapes, 1, _limits(rows=20, cells=10_000_000)) == 4 * (20 + 1)


def test_spreading_across_workbooks_takes_the_larger_share() -> None:
    # 25 rows over 2 workbooks: the larger holds 13.
    shapes: list[SourceShape] = [SourceShape(alias="a", stem="a", columns=4, rows=25)]
    assert balanced_workbook_cost(shapes, 2, _limits(rows=100, cells=10_000_000)) == 4 * (13 + 1)


def test_a_source_with_no_rows_contributes_no_header_to_a_balanced_workbook() -> None:
    shapes: list[SourceShape] = [SourceShape(alias="a", stem="a", columns=4, rows=10), SourceShape(alias="b", stem="b", columns=99, rows=0)]
    assert balanced_workbook_cost(shapes, 1, _limits(rows=100, cells=10_000_000)) == 4 * 11


@pytest.mark.parametrize("workbooks", [0, -1])
def test_spreading_across_no_workbooks_is_refused(workbooks: int) -> None:
    with pytest.raises(ValueError, match="cannot spread rows across"):
        balanced_workbook_cost([SourceShape(alias="a", stem="a", columns=4, rows=10)], workbooks, _limits())


def test_one_workbook_is_enough_when_everything_fits() -> None:
    shapes: list[SourceShape] = [SourceShape(alias="a", stem="a", columns=4, rows=10)]
    assert minimum_balanced_workbooks(shapes, _limits()) == 1


def test_the_workbook_count_grows_until_the_largest_one_fits() -> None:
    # 4 columns, 100 rows, L=100, budget 208 = 4*(50+1)+4 -> two workbooks of 50 rows each.
    shapes: list[SourceShape] = [SourceShape(alias="a", stem="a", columns=4, rows=100)]
    assert minimum_balanced_workbooks(shapes, _limits(rows=100, cells=4 * 51)) == 2


def test_feasibility_is_monotonic_in_the_workbook_count() -> None:
    # Which is what makes a scan upward from one correct, and what lets it stop at the first fit.
    shapes: list[SourceShape] = [SourceShape(alias="a", stem="a", columns=4, rows=1000)]
    limits: ProfileLimits = _limits(rows=1000, cells=10_000)
    costs: list[int] = [balanced_workbook_cost(shapes, k, limits) for k in range(1, 40)]
    assert costs == sorted(costs, reverse=True)


def test_no_workbook_count_helps_when_one_row_each_already_overflows() -> None:
    # The scan's termination condition, stated as an error rather than an endless search.
    shapes: list[SourceShape] = [SourceShape(alias="a", stem="a", columns=100, rows=10), SourceShape(alias="b", stem="b", columns=100, rows=10)]
    with pytest.raises(CapacityError, match="no number of workbooks helps"):
        minimum_balanced_workbooks(shapes, _limits(cells=399))


def test_the_floor_cost_ignores_sources_with_no_rows() -> None:
    shapes: list[SourceShape] = [SourceShape(alias="a", stem="a", columns=100, rows=10), SourceShape(alias="b", stem="b", columns=100, rows=0)]
    assert minimum_balanced_workbooks(shapes, _limits(cells=400)) >= 1
