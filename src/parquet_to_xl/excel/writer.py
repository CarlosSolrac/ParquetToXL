"""The Excel writer registry, and the two writers registered in it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

import polars as pl
import rustpy_xlsxwriter
import xlsxwriter
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from typing import Any

    from upath import UPath

DEFAULT_SHEET_NAME: str = "Sheet1"
"""Used when ``options`` carries no ``sheet_name``. Both writers honour the same key."""

EXCEL_MAX_COLUMNS: int = 16384
EXCEL_MAX_ROWS: int = 1048576
"""A worksheet's hard dimensions, header row included in the row count."""

MICROSECOND_TIME_FORMAT: str = "%H:%M:%S%.6f"
"""How ``PolarsExcelWriter`` renders a ``Time``; see the note on its ``write``."""

SHEET_NAME_OPTION: str = "sheet_name"
"""The only option key either writer accepts. See ``ExcelWriteConfig.options``."""

XLSXWRITER_WORKBOOK_OPTIONS: dict[str, bool] = {
    "remove_timezone": True,
    "nan_inf_to_errors": True,
    "strings_to_formulas": False,
    "strings_to_urls": False,
}
"""Workbook options ``PolarsExcelWriter`` cannot do without.

The first two are forced: xlsxwriter raises ``TypeError`` on a timezone-aware ``Datetime``
and again on ``NaN`` or infinity, and a ToExcel-converted frame still carries both.

The last two prevent silent data loss, and both were found by review rather than by reading
the documentation. At their defaults xlsxwriter reinterprets strings by their content:

- ``strings_to_formulas`` -- a cell whose text begins with ``=`` becomes a live formula.
  Measured, the string ``=SUM(1+1)`` read back as the float ``0.0``.
- ``strings_to_urls`` -- a cell that looks like a URL becomes a hyperlink, and one longer
  than Excel's 2,079-character link limit is then **dropped entirely** with only a warning.
  Measured, a 2,120-character URL survived the conversion and vanished from the workbook.

Both are ordinary values in a string column holding user data, and neither the default
writer nor DuckDB reinterprets them.
"""


class ExcelWriterBase(ABC):
    """One way of getting a dataframe into a workbook.

    Writers are addressed by ``identifier`` rather than imported, so the choice of writer
    can come from configuration. ``ExcelWriteConfig`` is what carries that choice.
    """

    identifier: ClassVar[str]

    @abstractmethod
    def write(self, df: pl.DataFrame, path: UPath, options: Mapping[str, object]) -> None:
        """Write the frame to ``path`` as a single-sheet workbook.

        Implementations expect a frame that has already been through
        ``DataframeConversionToExcel``. They are not required to make an unconverted frame
        writable, and neither of the two registered here can: both refuse dtypes that only
        the conversion removes.

        Args:
            df: The frame to write, already converted.
            path: Destination. Overwritten if it exists.
            options: Writer options. Only ``sheet_name`` is accepted; an unknown key is an
                error rather than a silent no-op, because a typo in configuration would
                otherwise be invisible.

        Raises:
            ValueError: ``options`` holds a key the writer does not accept.
        """


EXCEL_WRITERS: dict[str, type[ExcelWriterBase]] = {}
"""Identifier to writer class. Populated by ``register_excel_writer`` at import time."""


def register_excel_writer(cls: type[ExcelWriterBase]) -> type[ExcelWriterBase]:
    """Register a writer class under its own ``identifier``.

    Args:
        cls: The writer class. Its ``identifier`` must not already be registered.

    Returns:
        ``cls`` unchanged, so this works as a decorator.

    Raises:
        ValueError: Another class is already registered under that identifier. Silently
            replacing it would make the winner depend on import order.
    """
    if cls.identifier in EXCEL_WRITERS:
        message: str = f"an excel writer is already registered as {cls.identifier!r}"
        raise ValueError(message)
    EXCEL_WRITERS[cls.identifier] = cls
    return cls


def get_excel_writer(identifier: str) -> ExcelWriterBase:
    """Return a new instance of the writer registered under ``identifier``.

    Args:
        identifier: The registered name, e.g. ``"rustpy-xlsxwriter"``.

    Returns:
        A fresh instance. Writers hold no state, so instances are interchangeable.

    Raises:
        KeyError: No writer is registered under that identifier. The message lists what is,
            because the usual cause is a typo in configuration.
    """
    if identifier not in EXCEL_WRITERS:
        message: str = f"unknown excel writer {identifier!r}; registered: {sorted(EXCEL_WRITERS)}"
        raise KeyError(message)
    return EXCEL_WRITERS[identifier]()


def _reject_unnameable_columns(df: pl.DataFrame) -> None:
    """Refuse a frame with an empty column name, whichever writer is in use.

    Neither writer can carry one, and both corrupt it differently rather than failing:
    ``polars-xlsxwriter`` renames it to the generated ``Column1``, and
    ``rustpy-xlsxwriter`` renames it to ``1`` *and drops the row*. A silently renamed column
    breaks schema-driven reading, which is how every workbook here is read back.

    Args:
        df: The frame about to be written.

    Raises:
        ValueError: One or more columns have an empty name.
    """
    positions: list[int] = [index for index, name in enumerate(df.columns) if not name]
    if positions:
        message: str = f"no excel writer here preserves an empty column name; columns at positions {positions} have one"
        raise ValueError(message)


def _reject_unwritable_by_polars(df: pl.DataFrame) -> None:
    """Refuse a frame ``DataFrame.write_excel`` would drop rather than write.

    Every check here exists because the underlying call **fails soft**: it warns, discards
    data, and still returns, so without this the writer would report success and leave an
    empty or truncated workbook where a real one used to be.

    Two of the three were measured on this frame shape:

    - Over 16,384 columns produced a workbook that read back as ``(0, 0)``.
    - Column names differing only by case -- ``a`` and ``A``, both legal in Parquet -- lost
      one column and every row.

    The row ceiling is **not** measured -- doing so needs a frame of over a million rows --
    and is guarded by symmetry with the column ceiling, which is the same limit on the other
    axis and which the default writer also refuses. Its test drives the guard with the
    constant patched down rather than by building such a frame.

    The default writer needs none of this. It raises on the oversized frame rather than
    emitting an empty one, and writes case-colliding names correctly.

    Args:
        df: The frame about to be written.

    Raises:
        ValueError: The frame exceeds a worksheet's dimensions, or its effective header
            names are not unique once case folding and name generation are applied.
    """
    if df.width > EXCEL_MAX_COLUMNS:
        message: str = f"polars-xlsxwriter cannot write {df.width} columns; a worksheet holds {EXCEL_MAX_COLUMNS}"
        raise ValueError(message)
    if df.height + 1 > EXCEL_MAX_ROWS:
        message = f"polars-xlsxwriter cannot write {df.height} rows plus a header; a worksheet holds {EXCEL_MAX_ROWS}"
        raise ValueError(message)
    seen: dict[str, str] = {}
    collisions: list[str] = []
    name: str
    for name in df.columns:
        folded: str = name.casefold()
        if folded in seen:
            collisions.append(f"{seen[folded]!r} and {name!r}")
        seen[folded] = name
    if collisions:
        message = f"polars-xlsxwriter cannot write column names that differ only by case: {', '.join(collisions)}; the rustpy-xlsxwriter writer can"
        raise ValueError(message)


def _sheet_name(options: Mapping[str, object]) -> str:
    """Read the sheet name out of ``options``, rejecting anything else.

    Args:
        options: The caller's writer options.

    Returns:
        The requested sheet name, or ``DEFAULT_SHEET_NAME``.

    Raises:
        ValueError: An unrecognised key is present, or ``sheet_name`` is not a string.
    """
    unknown: set[str] = set(options) - {SHEET_NAME_OPTION}
    if unknown:
        message: str = f"unknown excel writer options: {sorted(unknown)}; only {SHEET_NAME_OPTION!r} is accepted"
        raise ValueError(message)
    name: object = options.get(SHEET_NAME_OPTION, DEFAULT_SHEET_NAME)
    if not isinstance(name, str):
        message = f"{SHEET_NAME_OPTION!r} must be a string, got {type(name).__name__}"
        raise ValueError(message)
    return name


@register_excel_writer
class RustpyExcelWriter(ExcelWriterBase):
    """Writes through ``rustpy-xlsxwriter``, Rust bindings over the ``rust_xlsxwriter`` crate.

    The default writer. Chosen on measured behaviour rather than only speed: it is the only
    one of the three writers measured that refuses an oversized cell instead of guessing --
    DuckDB writes the too-long string anyway and xlsxwriter silently truncates it -- and the
    only one that preserves NUL, CRLF and surrounding whitespace in strings while also not
    shifting timestamps into the writing machine's local zone.

    The speed picture is worth stating plainly, because "fastest" is only true at one end:
    it beats DuckDB below roughly 15-20k rows and is about 25% slower at 50k. Both are far
    ahead of ``polars.write_excel``, which was five times slower than DuckDB at 50k rows.

    Rows are streamed with ``iter_rows`` rather than materialised with ``to_dicts``, and
    ``autofit`` is off. Autofit measures every cell to size columns and its own
    documentation says to disable it on large data; ``dedupe_strings`` is left off because
    enabling it buffers the whole sheet to build a shared string table, which takes the
    writer out of constant-memory mode. Streaming plus those two defaults is what keeps that
    mode meaningful. A generator and a list were verified to produce identical workbooks.

    One inherited quirk matters downstream: this writer renders ``Time`` as ``str(time)``
    rather than as a time cell, so a ``Time`` column comes back as text. The reader is what
    puts that right, by coercing to the converted frame's schema.
    """

    identifier: ClassVar[str] = "rustpy-xlsxwriter"

    def write(self, df: pl.DataFrame, path: UPath, options: Mapping[str, object]) -> None:
        """Write the frame as one worksheet.

        Args:
            df: The converted frame.
            path: Destination.
            options: Only ``sheet_name`` is accepted.

        Raises:
            ValueError: ``options`` holds an unknown key.
            RuntimeError: A cell exceeds Excel's 32,767-character limit. The conversion
                truncates strings precisely so this cannot happen for a converted frame.
        """
        sheet: str = _sheet_name(options)
        _reject_unnameable_columns(df)
        if df.height == 0:
            # A zero-row frame still has columns, and streaming would lose them: iter_rows
            # yields nothing, so the writer never learns a single column name and emits a
            # sheet with no header row at all. Reading that back raises NoDataError. Handing
            # over the frame itself carries the schema; the resulting sheet XML was measured
            # byte-identical to the header row the streaming path writes.
            rustpy_xlsxwriter.write_worksheet(df, str(path), sheet, autofit=False)
            return
        rows: Iterator[dict[str, Any]] = df.iter_rows(named=True)
        rustpy_xlsxwriter.write_worksheet(rows, str(path), sheet, autofit=False)


@register_excel_writer
class PolarsExcelWriter(ExcelWriterBase):
    """Writes through ``polars.DataFrame.write_excel``, which drives ``xlsxwriter``.

    **This writer is not round-trip safe, and must not be used where a digest has to
    survive.** Two of its losses cannot be guarded around, only avoided, and both were
    measured on ordinary already-converted data that the default writer handles exactly:

    - **Float64 loses precision.** ``1.2345678901234567`` reads back as
      ``1.234567890123457``; xlsxwriter serializes numbers with too few significant digits.
      That is ordinary data, not an edge case.
    - **1900-01-01 shifts back a day.** A converted ``1900-01-01 12:00`` UTC reads back as
      ``1899-12-31 12:00`` -- Excel's inherited Lotus 1-2-3 leap-year bug, which DuckDB shows
      too and the default writer does not.

    Fixing either would mean not calling ``write_excel`` at all, at which point this stops
    being the Polars writer. So it stays registered for what it is genuinely good for: it is
    the only pure-Python writer here, so it is the fallback when the Rust wheel is
    unavailable on a platform, and a second implementation that fails *differently* is a
    sharper sanity check than one. It is also the slowest of the three measured, about five
    times DuckDB at 50k rows.

    What it does guarantee is a readable, correctly shaped workbook: headers, column order,
    row order, string content, and -- because ``Time`` is rendered as text here -- microsecond
    times. It refuses, rather than corrupts, the inputs it cannot represent.

    ``autofit`` is off and no formatting is applied, for the same reason as the default.
    """

    identifier: ClassVar[str] = "polars-xlsxwriter"

    def write(self, df: pl.DataFrame, path: UPath, options: Mapping[str, object]) -> None:
        """Write the frame as one worksheet.

        Args:
            df: The converted frame.
            path: Destination.
            options: Only ``sheet_name`` is accepted.

        Raises:
            ValueError: ``options`` holds an unknown key.
            TypeError: The frame holds a dtype xlsxwriter refuses even with the workbook
                options applied -- ``Binary`` is the one the conversion does not remove.
        """
        name: str = _sheet_name(options)
        _reject_unnameable_columns(df)
        _reject_unwritable_by_polars(df)
        # Time is rendered as text before handing over. Left as a Time, write_excel emits a
        # numeric time cell whose fraction is lost: measured, 23:59:59.999999 read back as
        # 00:00. The default writer already stores Time as text, so this makes the two agree
        # and keeps a converted frame recoverable at the precision the model promises.
        textual: pl.DataFrame = df.with_columns(
            [pl.nth(index).dt.to_string(MICROSECOND_TIME_FORMAT).alias(column) for index, column in enumerate(df.columns) if isinstance(df.schema[column], pl.Time)],
        )
        workbook: xlsxwriter.Workbook = xlsxwriter.Workbook(str(path), XLSXWRITER_WORKBOOK_OPTIONS)
        # write_excel never touches `path` itself: xlsxwriter buffers everything in memory and
        # only writes to disk inside `close()`. Closing unconditionally -- e.g. in a `finally`
        # -- would still persist an empty or partial workbook to `path` after a failure here.
        # Measured with an invalid sheet name: write_excel raised, but the finally still ran and
        # replaced an existing, readable workbook with an empty one. Closing only on success
        # leaves the destination untouched if writing fails.
        textual.write_excel(workbook=workbook, worksheet=name, include_header=True, autofit=False)
        workbook.close()


class ExcelWriteConfig(BaseModel):
    """Which writer to use, and what to tell it.

    Exists so a caller names a writer instead of importing one, which is what lets the
    choice come from a configuration file.
    """

    writer: str = RustpyExcelWriter.identifier
    """Defaults to ``rustpy-xlsxwriter``. Bound to the class attribute rather than repeated
    as a literal, so the default cannot drift from the identifier it names."""

    options: dict[str, object] = Field(default_factory=dict)
    """Passed to the writer's ``write``. Only ``sheet_name`` is accepted by either writer."""
