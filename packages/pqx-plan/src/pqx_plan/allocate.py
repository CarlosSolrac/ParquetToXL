"""Ordering a profile's sheets across its sources, and packing them into workbooks.

Two steps, and the first is the one the specs leave implicit. ``plan_calendar_sheets`` plans one
source at a time, because capacity is per source: ``R_s`` depends on ``C_s``, so one source's 2025
may fit a sheet while another's is split into quarters. Something then has to decide the order the
profile's sheets take *across* sources, and that order is what allocation consumes.

The packing itself is deliberately simple: append the next whole sheet if it fits the remaining
budget, otherwise open the next workbook. It is deterministic ordered packing, not a claim of
optimal packing, and it never splits a fitting sheet to fill a workbook's remaining capacity --
which may leave capacity unused, and is what preserves meaningful calendar boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pqx_calendar.periods import PeriodOrder

from pqx_plan.capacity import CapacityError, sheet_cells
from pqx_plan.partition import PlannedSheet

__all__ = ["AllocatedWorkbook", "allocate_workbooks", "order_profile_sheets"]


@dataclass(frozen=True, slots=True)
class AllocatedWorkbook:
    """One workbook and the sheets it holds, in order.

    Attributes:
        index: One-based workbook index within the profile. Used in filenames even when there is
            only one workbook, so a filename rule does not change when the dataset grows.
        sheets: The sheets, in the order they will be written.
    """

    index: int
    sheets: tuple[PlannedSheet, ...]

    def cells(self, widths: Mapping[str, int]) -> int:
        """Return this workbook's cell cost, summed across its sheets.

        Args:
            widths: ``C_s`` per source alias.

        Returns:
            The total cells, every header row included.
        """
        return sum(sheet_cells(widths[sheet.source_alias], sheet.rows) for sheet in self.sheets)


def order_profile_sheets(per_source: Sequence[tuple[int, Sequence[PlannedSheet]]], period_order: PeriodOrder) -> tuple[PlannedSheet, ...]:
    """Interleave each source's planned sheets into the profile's single output order.

    **Period first, then source.** ``partitioning-spec.md`` does not state this ordering outright,
    but it states the consequence that only this ordering produces: that a calendar bucket whose
    combined cost across the profile's sources exceeds the budget makes *the period* span
    workbooks, and that "a workbook may hold only some of the profile's sources for a period". That
    is only true if a period's sheets from every source are adjacent. Grouping by source instead
    would make each *source* span workbooks and put no two sources' 2025 near each other.

    Sheets are ordered by the start of their coverage, in the profile's ``period_order``, then by
    the position of their sheet entry in the profile, then by their order within that source. The
    undated bucket sorts after everything, whichever direction the dated sheets run.

    Balanced sheets carry no coverage, so they fall back to source order and then slice order,
    which is the only meaningful arrangement for slices of a sorted sequence.

    Args:
        per_source: ``(sheet entry position, that source's planned sheets)`` pairs, in the order
            the sheet entries appear in the profile.
        period_order: The profile's direction for dated sheets.

    Returns:
        Every sheet, in final output order.
    """
    descending: bool = period_order == "descending"
    ranked: list[tuple[tuple[int, int, int, int, int, int], PlannedSheet]] = []
    position: int
    sheets: Sequence[PlannedSheet]
    for position, sheets in per_source:
        index: int
        sheet: PlannedSheet
        for index, sheet in enumerate(sheets):
            if sheet.coverage is None:
                # Undated sheets sort last; balanced sheets sort first, by source then slice.
                ranked.append(((1 if sheet.kind == "undated" else 0, 0, 0, 0, position, index), sheet))
                continue
            year: int
            month: int
            day: int
            year, month, day = sheet.coverage.first.sort_key
            sign: int = -1 if descending else 1
            ranked.append(((0, sign * year, sign * month, sign * day, position, index), sheet))
    ranked.sort(key=lambda entry: entry[0])
    return tuple(sheet for _, sheet in ranked)


def allocate_workbooks(sheets: Sequence[PlannedSheet], widths: Mapping[str, int], budget: int) -> tuple[AllocatedWorkbook, ...]:
    """Pack sheets into workbooks in order, opening a new one whenever the next will not fit.

    Year boundaries alone do not force a new workbook, and an oversized year may span several.
    **The budget is what triggers another workbook**, and nothing else.

    Args:
        sheets: Every sheet the profile produces, in final output order.
        widths: ``C_s`` per source alias.
        budget: ``max_cells_per_workbook``.

    Returns:
        The workbooks, indexed from one. An empty sheet list produces no workbooks: there is
        nothing to name, and an empty workbook would be a file Excel cannot open.

    Raises:
        CapacityError: One sheet does not fit an empty workbook. Planning used ``R_s``, which is
            derived from this same budget, so reaching this means the sheets were planned against
            different limits from the ones being allocated under.
    """
    workbooks: list[AllocatedWorkbook] = []
    current: list[PlannedSheet] = []
    used: int = 0
    sheet: PlannedSheet
    for sheet in sheets:
        cost: int = sheet_cells(widths[sheet.source_alias], sheet.rows)
        if cost > budget:
            message: str = f"a sheet of {sheet.rows} rows from source {sheet.source_alias!r} costs {cost} cells, over the whole workbook budget of {budget}; it was planned against different limits"
            raise CapacityError(message)
        if current and used + cost > budget:
            workbooks.append(AllocatedWorkbook(index=len(workbooks) + 1, sheets=tuple(current)))
            current, used = [], 0
        current.append(sheet)
        used += cost
    if current:
        workbooks.append(AllocatedWorkbook(index=len(workbooks) + 1, sheets=tuple(current)))
    return tuple(workbooks)
