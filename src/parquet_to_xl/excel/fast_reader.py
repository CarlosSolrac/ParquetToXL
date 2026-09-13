"""Reading a workbook back into a Polars frame, one thread per sheet."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import TYPE_CHECKING

import fastexcel
import polars as pl

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from polars.datatypes import DataTypeClass
    from upath import UPath

TIME_TEXT_FORMAT: str = "%H:%M:%S%.f"
"""How a ``Time`` cell is parsed back out of text.

Neither writer stores a ``Time`` as a time cell: ``rustpy-xlsxwriter`` writes ``str(time)``
and ``PolarsExcelWriter`` writes ``MICROSECOND_TIME_FORMAT``. The two renderings differ --
``str(time)`` omits the fractional part when it is zero, the other always emits six digits --
and ``%.f`` accepts both, as well as the empty cell a null becomes.
"""

_REPORTED_CELL_ERRORS: int = 5
"""How many dropped cells an error message names before summarising the rest.

A column of the wrong type produces one error per row, and a message listing a thousand of
them helps nobody. The count is always reported in full; only the detail is trimmed.
"""

FASTEXCEL_DTYPES: dict[DataTypeClass, fastexcel.DType] = {
    pl.Float64: "float",
    pl.String: "string",
    pl.Date: "date",
    pl.Datetime: "datetime",
    pl.Time: "string",
    pl.Null: "string",
}
"""What to ask ``fastexcel`` for, per Polars dtype in the target schema.

These six are exactly what ``DataframeConversionToExcel`` can produce, which is the schema
this reader is built to be given. Anything else is refused rather than guessed at: a dtype
that never survives a round trip cannot be recovered from the sheet, so accepting it would
promise something the file cannot deliver.

``Time`` maps to text on purpose -- both writers render it as text -- and is parsed back in
``_coerce``. ``fastexcel``'s own vocabulary is only ``null``, ``int``, ``float``, ``string``,
``boolean``, ``datetime``, ``date`` and ``duration``, which is coarser than Polars', so two
columns still arrive needing correction.

``Null`` maps to text rather than to ``fastexcel``'s ``null``, which looks like the obvious
choice and is a trap: asking for ``null`` discards whatever the cell held **and reports no
error**, so a value written into a column the model says is empty vanishes silently and the
digest does not move. Measured -- an edited cell produced a digest identical to the empty
original. Reading as text keeps the content long enough for
``_reject_content_in_null_columns`` to see it.
"""


def _fastexcel_dtypes(schema: Mapping[str, pl.DataType]) -> fastexcel.DTypeMap:
    """Translate a Polars target schema into what ``fastexcel`` should read.

    Args:
        schema: The dtypes the caller wants back.

    Returns:
        The per-column dtype request, in ``fastexcel``'s vocabulary.

    Raises:
        ValueError: A column's dtype is not one this reader maps.
    """
    requested: fastexcel.DTypeMap = {}
    unmapped: list[str] = []
    name: str
    dtype: pl.DataType
    for name, dtype in schema.items():
        mapped: fastexcel.DType | None = FASTEXCEL_DTYPES.get(dtype.base_type())
        if mapped is None:
            unmapped.append(f"{name}={dtype}")
        else:
            requested[name] = mapped
    if unmapped:
        message: str = f"fast_excel_reader cannot read these dtypes: {', '.join(unmapped)}; it reads the output of DataframeConversionToExcel, which is {sorted(str(known) for known in FASTEXCEL_DTYPES)}"
        raise ValueError(message)
    return requested


def _coerce(frame: pl.DataFrame, schema: Mapping[str, pl.DataType]) -> pl.DataFrame:
    """Bring a sheet's columns to the dtypes the caller asked for.

    ``fastexcel``'s vocabulary is coarser than Polars', so two columns arrive close but not
    equal even when every dtype was requested. Measured over the fixture frame, then 19 columns wide:
    a ``Datetime`` comes back at millisecond precision where the model is microsecond, and a
    ``Time`` comes back as the text its writer stored. Everything else already matches.

    A ``Null`` column is the third case, and it is deliberate rather than inherited: it is
    requested as text so its content can be checked, and collapsed here once it has been.

    Columns are addressed by position. ``pl.col(name)`` reads ``^...$`` as a regular
    expression and ``*`` as every column, and both are legal Parquet column names, so
    selecting by name here would silently return a different column's values.

    Args:
        frame: One sheet, as ``fastexcel`` returned it.
        schema: The dtypes the caller wants. Columns absent from it are left alone.

    Returns:
        The frame with every column it could place at its target dtype.
    """
    corrections: list[pl.Expr] = []
    index: int
    name: str
    for index, name in enumerate(frame.columns):
        target: pl.DataType | None = schema.get(name)
        if target is None or frame.schema[name] == target:
            continue
        if isinstance(target, pl.Time) and isinstance(frame.schema[name], pl.String):
            corrections.append(pl.nth(index).str.to_time(TIME_TEXT_FORMAT).alias(name))
        elif isinstance(target, pl.Null):
            # Read as text so its content could be checked; only now collapse it.
            corrections.append(pl.nth(index).cast(pl.Null).alias(name))
        else:
            corrections.append(pl.nth(index).cast(target).alias(name))
    if not corrections:
        return frame
    return frame.with_columns(corrections)


def _reject_dropped_cells(sheet_name: str, columns: list[str], errors: fastexcel.CellErrors) -> None:
    """Refuse a sheet holding cells the requested dtype could not represent.

    A cell that does not parse as its column's dtype is turned into null rather than being
    reported, and null is also what an empty cell returns -- so a value that does not fit
    its column disappears without trace. That matters here because these frames are hashed:
    a cell edited from empty to something uncoercible reads back as empty, and the digest
    does not move. Setting ``dtype_coercion="strict"`` does not change it; measured, it
    returns the same null.

    What does see it is ``to_arrow_with_errors``, which records the position of every value
    the parse dropped. This turns those positions into an error naming the first few, since
    a whole bad column would otherwise produce a message as long as the column.

    Args:
        sheet_name: The sheet the errors came from, named in the message.
        columns: The sheet's column names, used to turn a column index into a name.
        errors: What ``to_arrow_with_errors`` reported.

    Raises:
        ValueError: Always. Callers check for errors before calling.
    """
    described: list[str] = []
    error: fastexcel.CellError
    for error in errors.errors[:_REPORTED_CELL_ERRORS]:
        row: int
        column: int
        # Both indices are relative to the data, so they address `columns` directly: the
        # frame and the errors come from the same sheet.
        row, column = error.offset_position
        described.append(f"{columns[column]!r} row {row}: {error.detail}")
    remaining: int = len(errors.errors) - len(described)
    tail: str = f", and {remaining} more" if remaining > 0 else ""
    message: str = (
        f"sheet {sheet_name!r} holds {len(errors.errors)} cell(s) the requested dtypes cannot represent, which would read back as null and so be indistinguishable from an empty cell: {'; '.join(described)}{tail}"
    )
    raise ValueError(message)


def _reject_content_in_null_columns(sheet_name: str, frame: pl.DataFrame, schema: Mapping[str, pl.DataType]) -> None:
    """Refuse a sheet whose ``Null`` column is not actually empty.

    ``_reject_dropped_cells`` cannot catch this one. Asking ``fastexcel`` for its ``null``
    dtype discards whatever a cell held and reports **no** cell error, so the value is gone
    before there is anything to report -- measured, an edited cell read back as ``None`` and
    produced a digest identical to the untouched original. A ``Null`` column is therefore
    requested as text, and this is what makes the difference visible.

    Args:
        sheet_name: The sheet the values came from, named in the message.
        frame: The sheet as read, with ``Null`` columns still text.
        schema: The dtypes the caller asked for.

    Raises:
        ValueError: A column the schema calls ``Null`` holds a value.
    """
    populated: list[str] = []
    index: int
    name: str
    for index, name in enumerate(frame.columns):
        if not isinstance(schema.get(name), pl.Null):
            continue
        present: pl.Series = frame.select(pl.nth(index).is_not_null().alias("present"))["present"]
        if present.any():
            populated.append(f"{name!r} row {present.arg_true().to_list()[0]}")
    if populated:
        message: str = f"sheet {sheet_name!r} holds values in column(s) the schema says are empty: {', '.join(populated)}"
        raise ValueError(message)


def _read_sheet(path: UPath, name: str, dtypes: fastexcel.DTypeMap | None, schema: Mapping[str, pl.DataType] | None) -> pl.DataFrame:
    """Read one sheet, on whichever thread is running this job.

    A workbook handle is opened here rather than shared with the other jobs. That is not
    caution: ``fastexcel``'s reader is a Rust object behind a ``RefCell``, and a second
    thread calling ``load_sheet`` on the same handle raises ``RuntimeError: Already
    borrowed``. Opening per job is what makes the pool usable at all, and re-parsing the zip
    directory per sheet is cheap next to parsing the sheet.

    Args:
        path: The workbook.
        name: The sheet to read.
        dtypes: What to ask ``fastexcel`` for, or ``None`` to let it infer.
        schema: The caller's target schema, used to correct what inference cannot express.

    Returns:
        The sheet as a frame, at its target dtypes.

    Raises:
        ValueError: A cell could not be represented at its requested dtype, or a column the
            schema calls ``Null`` holds a value.
    """
    sheet: fastexcel.ExcelSheet = fastexcel.read_excel(str(path)).load_sheet(name, header_row=0, dtypes=dtypes)
    # to_arrow_with_errors rather than to_polars: same single parse, but it also reports the
    # cells that parse dropped, which to_polars discards. See _reject_dropped_cells.
    batch: fastexcel.ArrowRecordBatch
    errors: fastexcel.CellErrors | None
    batch, errors = sheet.to_arrow_with_errors()
    frame: pl.DataFrame = pl.DataFrame(batch)
    if errors is not None:
        _reject_dropped_cells(name, frame.columns, errors)
    if schema is None:
        return frame
    _reject_content_in_null_columns(name, frame, schema)
    return _coerce(frame, schema)


def _restore_extent(frame: pl.DataFrame, sheet_count: int, expected_rows: int) -> pl.DataFrame:
    """Restore trailing all-null rows the sheet could not record.

    Only a *trailing* run is ever missing, so appending the shortfall puts the rows back
    where they were -- but that reasoning holds for one sheet at a time. Across several
    sheets the shortfall cannot be placed: rows missing from the end of a non-final sheet
    belong before the sheets that follow, and nothing in the file says which sheet lost
    them. Appending to the concatenated frame would silently reorder the data, so a
    multi-sheet shortfall is refused rather than guessed at.

    Args:
        frame: The concatenated sheets.
        sheet_count: How many sheets were concatenated.
        expected_rows: The row count the frame should have.

    Returns:
        ``frame``, extended with all-null rows if it was short and the shortfall can be
        placed.

    Raises:
        ValueError: The sheets hold more rows than expected, which means the workbook and
            the metadata describe different data rather than that a row went missing; or
            rows are missing from a multi-sheet read, where they cannot be located.
    """
    if frame.height > expected_rows:
        message: str = f"the workbook holds more rows than expected: {frame.height} read, {expected_rows} expected"
        raise ValueError(message)
    if frame.height == expected_rows:
        return frame
    if sheet_count > 1:
        message = (
            f"{expected_rows - frame.height} row(s) are missing from a {sheet_count}-sheet read, and which sheet lost them is not recorded in the file; "
            f"read the sheets separately with their own expected_rows, or omit expected_rows to accept the workbook as it stands"
        )
        raise ValueError(message)
    missing: int = expected_rows - frame.height
    padding: pl.DataFrame = pl.DataFrame({name: [None] * missing for name in frame.columns}, schema=dict(frame.schema))
    return pl.concat([frame, padding], how="vertical")


def fast_excel_reader(
    path: UPath,
    *,
    sheet_names: Sequence[str] | None = None,
    schema: Mapping[str, pl.DataType] | None = None,
    expected_rows: int | None = None,
    max_workers: int | None = None,
) -> pl.DataFrame:
    """Read one workbook into one frame, reading its sheets in parallel.

    Every sheet is expected to hold the same columns; the sheets are read independently and
    concatenated in order, which is what lets a frame written across several sheets be
    reassembled.

    Reading goes through ``fastexcel`` directly rather than through ``pl.read_excel``, which
    wraps it. That is not a preference. ``pl.read_excel`` builds a Polars projection from the
    header text, so a column named ``*`` or ``^a$`` -- both legal Parquet names that both
    writers store correctly -- makes it raise ``DuplicateError``. Measured: a sheet with
    headers ``*`` and ``b`` fails, and so does ``^a$`` beside ``a``. ``fastexcel`` returns
    both correctly, because it never treats a header as an expression.

    Args:
        path: The workbook to read.
        sheet_names: Sheets to read, in the order given. All sheets in workbook order when
            omitted.
        schema: The dtypes the result should have, normally those of the
            ``DataframeConversionToExcel``-converted frame that was written. **Supply this.**
            Without it ``fastexcel`` infers per sheet, and inference is measured to fail on
            real data -- it overflows on ``Int64``'s maximum and downcasts integral-looking
            floats -- as well as to let two sheets of one frame disagree about a column.
            It is also what makes a cell that does not fit its column visible rather than
            silently null; see ``_reject_dropped_cells``.
        expected_rows: The row count the sheets should add up to, from
            ``DataframeColumnMetadata.value_count``. A row whose cells are all empty emits no
            ``<row>`` element, so a *trailing* run of all-null rows leaves no trace in the
            file and is restored here. Only trailing rows are affected: measured, interior
            and leading all-null rows are held in place by the row indices around them.
            Restoring is only well defined for a single sheet; see ``_restore_extent``.
        max_workers: Threads to read sheets with. ``None`` lets ``ThreadPoolExecutor``
            choose. The result does not depend on this, and a test pins that.

    Returns:
        One frame, the sheets concatenated in the order they were read, padded to
        ``expected_rows`` if that was given.

    Raises:
        KeyError: ``sheet_names`` names a sheet the workbook does not have.
        ValueError: ``schema`` holds a dtype this reader does not map, a cell cannot be
            represented at its requested dtype, a column the schema calls ``Null`` holds a
            value, the sheets hold more rows than ``expected_rows`` allows, or rows are
            missing from a multi-sheet read.
    """
    available: list[str] = fastexcel.read_excel(str(path)).sheet_names
    targets: list[str] = list(available) if sheet_names is None else list(sheet_names)
    absent: list[str] = [name for name in targets if name not in available]
    if absent:
        message: str = f"workbook {path.name} has no sheet named {absent}; it has {available}"
        raise KeyError(message)

    dtypes: fastexcel.DTypeMap | None = None if schema is None else _fastexcel_dtypes(schema)
    pool: ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        frames: list[pl.DataFrame] = list(pool.map(partial(_read_sheet, path, dtypes=dtypes, schema=schema), targets))

    combined: pl.DataFrame = pl.concat(frames, how="vertical_relaxed")
    if expected_rows is None:
        return combined
    return _restore_extent(combined, len(frames), expected_rows)
