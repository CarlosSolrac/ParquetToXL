"""The arithmetic every partitioning algorithm is bounded by.

Two ceilings bind at once and they bind differently. ``max_data_rows_per_worksheet`` (``L``) is a
per-sheet row count; ``max_cells_per_workbook`` (``B``) is a budget summed across a workbook's
sheets. Fan-in is what makes the second non-trivial: with several sources in one workbook each
has its own exported column count ``C_s``, so a workbook's cost is a **sum across its sheets**,
not a single product, and there is no one ``C`` for the export.

Nothing here reads a frame. The planner works over :class:`SourceShape`, a four-field value
object, which is what keeps this package free of Polars and makes every number below testable
without building a dataset to match it.

``max_cells_per_workbook`` is a planning limit, not an Excel memory guarantee. Ten to thirty
million numeric values hold 80-240 MB of raw payload, and cell structures, strings and formatting
add costs Microsoft publishes no conversion for. It counts every exported position, nulls
included, plus one header row per sheet -- matching this project's ``value_count`` convention,
which also includes nulls.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from pqx_plan.config import ProfileLimits

__all__ = [
    "EXCEL_MAX_COLUMNS",
    "CapacityError",
    "SourceShape",
    "balanced_split",
    "balanced_workbook_cost",
    "fits_one_workbook",
    "minimum_balanced_workbooks",
    "minimum_sheets",
    "sheet_capacity",
    "sheet_cells",
    "workbook_cells",
]

EXCEL_MAX_COLUMNS: Final[int] = 16_384
"""A worksheet's column ceiling. A source wider than this cannot be exported at all."""

HEADER_ROWS: Final[int] = 1
"""V1 writes exactly one header row per sheet, with no index or title rows."""


class CapacityError(Exception):
    """A profile's limits cannot accommodate the data, or a shape cannot be exported at all.

    Always a refusal, never a silent adjustment: ``partitioning-spec.md`` forbids dropping rows or
    columns to meet a limit, so the only two outcomes here are a plan that fits and an error.
    """


@dataclass(frozen=True, slots=True)
class SourceShape:
    """What the planner needs to know about one source, and nothing more.

    Deliberately small. The planner does not open a Parquet file, hold a frame or know a dtype;
    it needs the exported width, the row count, and two names. Keeping it to that is what lets
    ``pqx-plan`` stay pure and what makes the capacity tests readable, because a case is four
    numbers rather than a fixture.

    Attributes:
        alias: The source alias, as declared and as interpolated into worksheet names.
        stem: The source file's basename without its final ``.parquet``, for ``{source_stem}``.
        columns: ``C_s``, the exported column count. V1 exports all source columns in source order.
        rows: The source's data row count, headers excluded.
    """

    alias: str
    stem: str
    columns: int
    rows: int

    def __post_init__(self) -> None:
        """Refuse a shape no worksheet could hold.

        Raises:
            CapacityError: The source has no columns, or more than a worksheet holds. A
                zero-column source would make every capacity expression divide by zero, and a
                too-wide one cannot be exported at any budget -- neither is a planning decision.
            ValueError: The row count is negative, which describes no dataset.
        """
        if self.rows < 0:
            message: str = f"source {self.alias!r} reports {self.rows} rows"
            raise ValueError(message)
        if self.columns < 1:
            narrow: str = f"source {self.alias!r} exports {self.columns} columns; a worksheet needs at least one"
            raise CapacityError(narrow)
        if self.columns > EXCEL_MAX_COLUMNS:
            wide: str = f"source {self.alias!r} exports {self.columns} columns; a worksheet holds {EXCEL_MAX_COLUMNS}"
            raise CapacityError(wide)

    @property
    def header_cells(self) -> int:
        """Return the cells one header row of this source occupies."""
        return self.columns * HEADER_ROWS

    @property
    def is_empty(self) -> bool:
        """Return whether this source contributes no data rows.

        An empty source still gets one header-only worksheet when its header fits the budget, so
        that the export's shape does not depend on whether a quarter happened to have sales.
        """
        return self.rows == 0


def sheet_cells(columns: int, data_rows: int) -> int:
    """Return the cells one sheet occupies: ``C_s * (data_rows + 1)``.

    Args:
        columns: ``C_s``.
        data_rows: The sheet's data rows, header excluded.

    Returns:
        The cell count, header row included.
    """
    return columns * (data_rows + HEADER_ROWS)


def workbook_cells(sheets: Iterable[tuple[int, int]]) -> int:
    """Return a workbook's cost: the sum of :func:`sheet_cells` over its sheets.

    A sum, not a product. With fan-in each sheet may come from a source of a different width, so
    there is no single ``C`` for the workbook.

    Args:
        sheets: ``(columns, data rows)`` per sheet.

    Returns:
        The total cells, every header row included.
    """
    return sum(sheet_cells(columns, rows) for columns, rows in sheets)


def sheet_capacity(shape: SourceShape, limits: ProfileLimits) -> int:
    """Return ``R_s``, the most data rows of this source one sheet may hold.

    ``R_s = min(L, floor(B / C_s) - 1)``. The ``- 1`` is the header row, which the budget must pay
    for before a single data row fits.

    This is the per-sheet cap for every mode **except** balanced-workbooks, which uses ``L``
    directly; that exception is documented where it arises, in
    :func:`balanced_workbook_cost`.

    Args:
        shape: The source.
        limits: The profile's ceilings.

    Returns:
        The capacity, always at least one row.

    Raises:
        CapacityError: The budget cannot pay for a header and one data row of this source. Stated
            as a refusal because the alternative -- a sheet of zero data rows -- would loop
            forever producing empty sheets.
    """
    from_budget: int = limits.max_cells_per_workbook // shape.columns - HEADER_ROWS
    capacity: int = min(limits.max_data_rows_per_worksheet, from_budget)
    if capacity < 1:
        message: str = f"a cell budget of {limits.max_cells_per_workbook} cannot hold a header and one data row of source {shape.alias!r}, which is {shape.columns} columns wide; at least {shape.columns * (HEADER_ROWS + 1)} cells are needed"
        raise CapacityError(message)
    return capacity


def minimum_sheets(rows: int, capacity: int) -> int:
    """Return the fewest sheets that hold ``rows`` at ``capacity`` rows each.

    Args:
        rows: The data rows to place.
        capacity: The per-sheet row cap.

    Returns:
        The sheet count. Zero rows still needs one sheet, because an empty source gets a
        header-only worksheet rather than no worksheet -- the export's shape must not depend on
        whether a period happened to have data.

    Raises:
        ValueError: ``capacity`` is not positive, which would place no rows per sheet.
    """
    if capacity < 1:
        message: str = f"a sheet capacity of {capacity} places no rows"
        raise ValueError(message)
    return max(1, -(-rows // capacity))


def balanced_split(total: int, parts: int) -> tuple[int, ...]:
    """Divide ``total`` rows into ``parts`` outputs differing by at most one row.

    ``total mod parts`` outputs of ``floor(total / parts) + 1``, followed by the smaller ones --
    larger first, as ``partitioning-spec.md`` specifies. Rows are never shuffled to equalise
    them: this decides *how many* rows each output takes, and the sorted order decides which.

    Args:
        total: The rows to divide.
        parts: How many outputs to divide them into.

    Returns:
        The row count of each output, in order, summing to ``total``.

    Raises:
        ValueError: ``parts`` is not positive, or ``total`` is negative.
    """
    if parts < 1:
        parts_message: str = f"cannot divide rows into {parts} outputs"
        raise ValueError(parts_message)
    if total < 0:
        total_message: str = f"cannot divide {total} rows"
        raise ValueError(total_message)
    base: int
    remainder: int
    base, remainder = divmod(total, parts)
    return (base + 1,) * remainder + (base,) * (parts - remainder)


def fits_one_workbook(shapes: Sequence[SourceShape], limits: ProfileLimits) -> bool:
    """Return whether the single-sheet shortcut applies: one sheet per source, one workbook.

    Both conditions must hold, and the budget is the one that usually decides. 500,000 rows by 100
    columns fits Excel's row limit and holds over 50 million cells with headers, so it must split
    under a 10-million budget; 2 million rows by 3 columns needs several sheets on rows alone yet
    fits one workbook on cells. **The workbook budget takes priority over the shortcut.**

    An **empty** source is exempt from the row test and not from the budget. It needs no data-row
    capacity, so a budget that could not pay for a header plus one data row of it is not a reason
    to refuse: ``partitioning-spec.md`` asks for one header-only worksheet whenever its header
    fits, and the ``workbook_cells`` sum is what checks that it does.

    Args:
        shapes: Every source the profile draws from.
        limits: The profile's ceilings.

    Returns:
        Whether every source fits one sheet and those sheets together fit one workbook.

    Raises:
        CapacityError: The budget cannot pay for a header and one data row of some **nonempty**
            source.
    """
    if any(shape.rows > sheet_capacity(shape, limits) for shape in shapes if not shape.is_empty):
        return False
    return workbook_cells((shape.columns, shape.rows) for shape in shapes) <= limits.max_cells_per_workbook


def balanced_workbook_cost(shapes: Sequence[SourceShape], workbooks: int, limits: ProfileLimits) -> int:
    """Return the cell cost of the largest workbook when rows are spread across ``workbooks``.

    For one source contributing ``n > 0`` rows to a workbook the cost is
    ``C_s * (n + ceil(n / L))``: the rows, plus one header for each of the sheets they are spread
    over. **This mode uses ``L``, not ``R_s``, as the per-sheet cap** -- the documented exception.
    It is cheaper, because fewer sheets means fewer repeated headers, and it stays safe because
    the workbook budget still binds through this very formula.

    The largest workbook takes the larger balanced share of every source, so this is what
    feasibility is decided on.

    Args:
        shapes: Every source the profile draws from.
        workbooks: The candidate workbook count ``K``.
        limits: The profile's ceilings.

    Returns:
        The cell cost of the largest workbook under that ``K``.

    Raises:
        ValueError: ``workbooks`` is not positive.
    """
    if workbooks < 1:
        message: str = f"cannot spread rows across {workbooks} workbooks"
        raise ValueError(message)
    limit: int = limits.max_data_rows_per_worksheet
    shape: SourceShape
    total: int = 0
    for shape in shapes:
        share: int = -(-shape.rows // workbooks)
        # A source with no rows in this workbook contributes no sheet, and therefore no header.
        # An empty source overall is handled by the caller, which gives it one header-only sheet.
        total += shape.columns * (share + -(-share // limit)) if share > 0 else 0
    return total


def minimum_balanced_workbooks(shapes: Sequence[SourceShape], limits: ProfileLimits) -> int:
    """Return the fewest workbooks whose largest one fits the cell budget.

    Feasibility is monotonic in ``K``, so this scans upward from one and stops at the first that
    fits. The scan terminates because each source's share falls to a single row, at which point
    the cost is fixed at ``sum(C_s * 2)``; if even that exceeds the budget no ``K`` works, and
    that is an error rather than an endless search.

    Args:
        shapes: Every source the profile draws from.
        limits: The profile's ceilings.

    Returns:
        The smallest feasible workbook count.

    Raises:
        CapacityError: No workbook count fits, because one row of every source plus their headers
            already exceeds the budget.
    """
    floor_cost: int = sum(shape.columns * (1 + HEADER_ROWS) for shape in shapes if shape.rows > 0)
    if floor_cost > limits.max_cells_per_workbook:
        message: str = f"a cell budget of {limits.max_cells_per_workbook} cannot hold one row of every source with their headers, which needs {floor_cost}; no number of workbooks helps"
        raise CapacityError(message)
    candidate: int = 1
    while balanced_workbook_cost(shapes, candidate, limits) > limits.max_cells_per_workbook:
        candidate += 1
    return candidate
