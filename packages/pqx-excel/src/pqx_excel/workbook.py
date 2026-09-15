"""Writing many worksheets into one workbook, over ``rustpy-xlsxwriter``'s ``FastExcel``.

Gate 0a settled the shape. ``FastExcel`` holds each sheet's data source without touching it and
consumes them all, in order, inside ``save()``; peak RSS stayed flat at about 1 MiB over
baseline from 1 to 32 sheets and from 400k to 6.4M rows, at a fixed cost of roughly 23 KiB per
sheet. So sheet count is free, and this module is a thin, well-guarded pass-through rather than
an assembler that buffers.

What gate 0a's harness did *not* show, because it generated rows synthetically, is measured in
``test_multi_sheet_workbook`` and stated on :func:`write_workbook`: deferring consumption to
``save()`` means every sheet's frame stays reachable until then. The writer adds nothing per
row, but it does not let a caller hold less than the data it hands over.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import polars as pl
import rustpy_xlsxwriter
from pqx_common.names import NameRegistry, PortableNameError, validate_worksheet_name

from pqx_excel.writer import EXCEL_MAX_COLUMNS, EXCEL_MAX_ROWS, reject_unnameable_columns

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from upath import UPath

__all__ = ["SheetNameError", "validate_sheet_name", "write_workbook"]

AUTOFIT: Final[bool] = False
"""Always off, and **not** for memory.

The single-sheet writer's docstring long gave a memory reason for this. Gate 0a measured the
cost of ``autofit=True`` at nothing detectable, so that reason is gone. The reason that remains
is determinism: autofit sizes each column by measuring the cells actually present, so the same
rows split differently across sheets produce different column widths, and a verified export
whose bytes depend on how it happened to be partitioned is a worse thing to explain than a
narrow column.
"""

DEDUPE_STRINGS: Final[bool] = False
"""Always off, and this one *is* for memory.

Gate 0a measured it at about 1.2 KiB per row, and measured that the cost is exactly per sheet
rather than shared across the workbook -- so it scales with the whole export. It buys a smaller
file by building a shared string table, which means buffering the sheet, which is the one thing
that would take this writer out of the constant-memory mode the rest of the design assumes.
"""


class SheetNameError(PortableNameError):
    """A worksheet name this writer will not put in a workbook.

    Subclasses ``PortableNameError`` so a caller validating names from several layers -- the
    planner's rendered names, this writer's check -- catches one family.
    """


def validate_sheet_name(name: str) -> str:
    """Check one sheet name against this project's rule and against the writer's own.

    Two checks, in this order and for different reasons.

    :func:`pqx_common.names.validate_worksheet_name` is the project's rule and runs first,
    because it produces the message that names the offending character. It is a strict superset
    of the library's check today: no name it accepts is one ``rustpy_xlsxwriter`` refuses, which
    ``test_workbook.py`` asserts over a character corpus rather than assuming.

    ``rustpy_xlsxwriter.validate_sheet_name`` then runs as a backstop, because "strict superset"
    is a property of the version installed rather than a promise. If a later release refuses
    something this project allows, the export fails here with the name in hand instead of
    somewhere inside ``save()`` with a whole workbook already staged.

    The library's own check is not sufficient on its own, which is why it is not the only one:
    measured, it accepts ``History``, a name wrapped in apostrophes, and names holding a tab, a
    newline or a NUL.

    Args:
        name: The fully expanded sheet name, ``worksheet_prefix`` included.

    Returns:
        ``name``, unchanged.

    Raises:
        PortableNameError: The project's rule refuses the name.
        SheetNameError: The library refuses a name the project's rule allowed.
    """
    validate_worksheet_name(name)
    if not rustpy_xlsxwriter.validate_sheet_name(name):
        message: str = f"rustpy-xlsxwriter refuses the sheet name {name!r}, which this project's own rule allowed; the library's rule has tightened and ours has not caught up"
        raise SheetNameError(message)
    return name


def _reject_oversized_sheet(name: str, frame: pl.DataFrame) -> None:
    """Refuse a frame larger than a worksheet holds.

    Capacity is the planner's arithmetic, so reaching this means the plan and the writer
    disagree -- and a disagreement that silently truncates is the expensive kind. Guarded on
    both axes by the same symmetry the single-sheet writer uses: the column ceiling is cheap to
    exercise, the row ceiling needs a frame of over a million rows, so its test drives this with
    the constant patched down rather than by building one.

    Args:
        name: The sheet the frame is destined for, for the message.
        frame: The frame about to be written.

    Raises:
        ValueError: The frame exceeds a worksheet's dimensions, header row included.
    """
    if frame.width > EXCEL_MAX_COLUMNS:
        message: str = f"sheet {name!r} has {frame.width} columns; a worksheet holds {EXCEL_MAX_COLUMNS}"
        raise ValueError(message)
    if frame.height + 1 > EXCEL_MAX_ROWS:
        message = f"sheet {name!r} has {frame.height} rows plus a header; a worksheet holds {EXCEL_MAX_ROWS}"
        raise ValueError(message)


def write_workbook(sheets: Iterable[tuple[str, pl.DataFrame]], path: UPath) -> tuple[str, ...]:
    """Write one workbook holding every given sheet, in the order given.

    **On memory.** ``FastExcel`` consumes nothing when a sheet is added and everything inside
    ``save()``. The writer therefore adds about 23 KiB per sheet and nothing per row -- but every
    frame handed over here stays reachable until ``save()`` returns, so peak memory is the sum
    of the frames, not one of them. Passing a lazy generator of ``(name, frame)`` pairs does not
    change that: the pairs are drained here, before ``save()``, precisely so that a name or a
    shape can be refused before any bytes are written.

    **On validation order.** Every sheet is validated before the first is added, so a workbook
    is never left half-written at the destination by a name the tenth sheet would have failed on.

    Args:
        sheets: ``(sheet name, converted frame)`` pairs, in final output order. Names must be
            fully expanded, prefix included. Frames must already have been through
            ``DataframeConversionToExcel``.
        path: Destination. Overwritten if it exists.

    Returns:
        The sheet names written, in order, as the caller's record of what the workbook holds.

    Raises:
        ValueError: ``sheets`` is empty, a frame has an unnameable column, or a frame exceeds a
            worksheet's dimensions.
        PortableNameError: A sheet name is invalid, or two names collide case-insensitively --
            which Excel treats as one sheet, so the second would overwrite the first.
    """
    planned: tuple[tuple[str, pl.DataFrame], ...] = tuple(sheets)
    if not planned:
        message: str = "a workbook needs at least one worksheet; Excel cannot open one with none"
        raise ValueError(message)

    registry: NameRegistry = NameRegistry(f"workbook {path.name!r}")
    name: str
    frame: pl.DataFrame
    for name, frame in planned:
        registry.claim(validate_sheet_name(name))
        reject_unnameable_columns(frame)
        _reject_oversized_sheet(name, frame)

    book: rustpy_xlsxwriter.FastExcel = rustpy_xlsxwriter.FastExcel(str(path), autofit=AUTOFIT)
    for name, frame in planned:
        # A zero-row frame is handed over whole rather than as a row iterator. iter_rows yields
        # nothing, so the writer never learns a column name and emits a sheet with no header at
        # all, which reads back as NoDataError. The same carve-out the single-sheet writer makes,
        # for the same measured reason.
        rows: Iterator[dict[str, Any]] | pl.DataFrame = frame if frame.height == 0 else frame.iter_rows(named=True)
        book = book.sheet(name, rows, dedupe_strings=DEDUPE_STRINGS)
    book.save()
    return tuple(name for name, _ in planned)
