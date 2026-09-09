"""What a round trip through an Excel workbook does to a dataframe."""

from __future__ import annotations

from typing import ClassVar, Literal

import polars as pl

from parquet_to_xl.conversion.base import DataframeConversionBaseClass
from parquet_to_xl.metadata import scalars

EXCEL_CELL_LIMIT: int = 32767
"""Excel's maximum characters per cell.

Enforcing it is this project's rule, not the format's: of the three writers measured, only
``rustpy-xlsxwriter`` refuses an oversized cell, and it refuses by raising. Truncating here
means the writer never sees one.
"""

EXCEL_TRUE: float = -1.0
EXCEL_FALSE: float = 0.0
"""Excel's numeric booleans. ``TRUE`` is all bits set, which reads as -1, not 1."""

EXCEL_TIME_UNIT: Literal["us"] = "us"
"""Datetimes are normalised to this before truncation; see the ``Datetime`` branch."""

NANOSECONDS_PER_MICROSECOND: int = 1000

WHOLE_SECOND: str = "1s"
"""Datetimes are truncated to this. Measured: a workbook stores no sub-second component, so
``23:47:16.854775`` reads back as ``23:47:16``. Modelling that here is what lets a digest
taken before writing match one taken after."""

MICROSECONDS_PER_SECOND: int = 1000000
DURATION_DIVISORS: dict[str, int] = {"ms": 1000, "us": MICROSECONDS_PER_SECOND, "ns": 1000000000}
"""Ticks per second for each Polars duration unit.

A ``Duration`` column's physical ``int64`` counts its own unit, so one divisor cannot serve
all three. ``dt.total_seconds()`` is not usable at all here: it returns ``Int64`` and
truncates, turning one microsecond into zero.
"""


class DataframeConversionToExcel(DataframeConversionBaseClass):
    """Models the value and dtype damage of storing a frame in a worksheet.

    A worksheet cell holds a float, a string, a boolean, or a date-like value, so every
    other dtype has to become one of those. The mapping is:

    ===================================== ==============================================
    Source dtype                          Becomes
    ===================================== ==============================================
    ``Int*``, ``UInt*``, ``Decimal``      ``Float64``
    ``Float32``, ``Float64``              ``Float64``; ``NaN`` and ``+/-inf`` become null
    ``Boolean``                           ``Float64``, -1.0 / 0.0, null preserved
    ``String``, ``Categorical``           ``String`` cut to ``EXCEL_CELL_LIMIT``; ``""`` becomes null
    ``Binary``                            lowercase hex ``String``, then the same rule
    ``Duration``                          ``Float64`` seconds
    ``Datetime``                          microseconds, truncated to whole seconds
    ``Time``                              truncated to microseconds
    ``Date``, ``Null``                    unchanged
    ===================================== ==============================================

    Three of those rules destroy information rather than reshaping it, and all three exist
    because a measured round trip destroys it first. Modelling the loss is the whole point:
    a digest taken before writing has to match one taken after, so this conversion has to
    lose exactly what the file loses, no more and no less.

    - **An empty string becomes null.** A worksheet cannot tell an empty string cell from an
      empty cell, so both read back the same way. Which way depends on the reader --
      ``fastexcel`` says ``None`` and ``python-calamine`` says ``''`` -- which is itself a
      reason the distinction cannot be preserved.
    - **NaN and the infinities become null.** No writer measured here can store them:
      xlsxwriter refuses outright without ``nan_inf_to_errors``, and the chosen writer emits
      an empty cell.
    - **Datetimes lose their sub-second component**, and any nanosecond column is first
      normalised to microseconds. Measured, ``23:47:16.854775`` reads back as ``23:47:16``;
      and a nanosecond datetime at the bottom of its range cannot even hold its own truncated
      value, wrapping forward by centuries.
    - **Times lose precision below a microsecond.** Polars stores nanoseconds, but every
      Python boundary the value crosses is a ``datetime.time``, which does not.

    One departure from the spec's cast table is not about loss: ``Categorical`` is truncated
    as well as cast, though the table lists truncation for ``String`` and ``Binary`` only.
    Leaving it out breaks idempotency, because an oversized label survives the first pass and
    is cut by the second. Idempotency is load-bearing for the headline test, so it wins.

    The measurements behind all of this are in
    ``tests/fixtures/excel-round-trip-findings.md``.
    """

    identifier: ClassVar[str] = "to-excel"
    version: ClassVar[str] = "2.0"
    version_number: ClassVar[int] = 2
    description: ClassVar[str] = "Models the dtype and value damage of a round trip through an Excel worksheet."

    def _convert(self, df: pl.DataFrame) -> tuple[pl.DataFrame, bool]:
        """Apply the cast table and report whether anything moved.

        Idempotent by construction: every rule maps into a set the rules then leave alone, so
        a second pass finds nothing to do. ``Boolean`` becomes ``Float64`` and there are then
        no booleans; ``Binary`` becomes ``String`` and is then only cut; a string already at
        the limit is not over it, and one already blanked is null rather than empty; a
        blanked ``NaN`` is null, and null is not non-finite; a datetime already truncated to
        the second truncates to itself. Each flag predicate tests the same condition its rule
        acts on, so the second pass reports ``False`` for the frame and for the flag alike.

        Args:
            df: The frame to convert. Never mutated.

        Returns:
            The converted frame, and ``True`` when the schema changed or any value moved.
        """
        expressions: list[pl.Expr] = []
        values_moved: bool = False
        index: int
        name: str
        for index, name in enumerate(df.columns):
            dtype: pl.DataType = df.schema[name]
            # Selected by position, never by name. ``pl.col(name)`` treats a name wrapped in
            # ^...$ as a regex and "*" as every column, both of which are legal column names
            # in a Parquet file: ``pl.col("^a$")`` next to a column called "a" silently
            # returns that other column's values, a lone such column disappears, and "*"
            # raises DuplicateError.
            column: pl.Expr = pl.nth(index)
            if scalars.is_boolean(dtype):
                # Ordered before the numeric branch: Polars reports Boolean as not numeric,
                # but relying on that here would make this depend on a Polars detail.
                expressions.append(pl.when(column).then(pl.lit(EXCEL_TRUE)).when(column.not_()).then(pl.lit(EXCEL_FALSE)).otherwise(pl.lit(None)).cast(pl.Float64).alias(name))
            elif scalars.is_float(dtype):
                # Ordered before the general numeric branch: only a float source can carry a
                # NaN or an infinity, and only those need blanking.
                widened: pl.Expr = column.cast(pl.Float64)
                values_moved = values_moved or _any_true(df, ~widened.is_finite() & widened.is_not_null())
                expressions.append(pl.when(widened.is_finite()).then(widened).otherwise(pl.lit(None)).cast(pl.Float64).alias(name))
            elif scalars.is_numeric(dtype):
                expressions.append(column.cast(pl.Float64).alias(name))
            elif isinstance(dtype, pl.Binary) or scalars.is_text(dtype):
                # Binary, String and Categorical converge on the same rule: become text, cut
                # to the cell limit, then blank an empty result. Categorical is truncated
                # even though the spec lists truncation for String and Binary only -- leaving
                # it out breaks idempotency, because an oversized label would pass through on
                # the first call and be cut on the second.
                text: pl.Expr = column.bin.encode("hex") if isinstance(dtype, pl.Binary) else column.cast(pl.String)
                cut: pl.Expr = text.str.slice(0, EXCEL_CELL_LIMIT)
                values_moved = values_moved or _any_true(df, text.str.len_chars() > EXCEL_CELL_LIMIT) or _any_true(df, cut.str.len_chars() == 0)
                expressions.append(pl.when(cut.str.len_chars() == 0).then(pl.lit(None)).otherwise(cut).alias(name))
            elif isinstance(dtype, pl.Duration):
                expressions.append((column.cast(pl.Int64) / DURATION_DIVISORS[dtype.time_unit]).cast(pl.Float64).alias(name))
            elif isinstance(dtype, pl.Datetime):
                # Normalised to microseconds before truncating, not merely truncated. A
                # Datetime("ns") at the bottom of its range truncates to a value that range
                # cannot hold: 1677-09-21 00:12:43.145225 wrapped forward to 2262-04-11 and
                # a second pass moved it again, so idempotency broke as well as the value.
                # Excel stores whole seconds, so no nanosecond column is representable anyway.
                whole: pl.Expr = column.dt.cast_time_unit(EXCEL_TIME_UNIT).dt.truncate(WHOLE_SECOND)
                values_moved = values_moved or _any_true(df, whole != column)
                expressions.append(whole.alias(name))
            elif isinstance(dtype, pl.Time):
                # Polars holds Time as nanoseconds, but every Python boundary it crosses --
                # to_list, iter_rows, the writer -- is a datetime.time, which resolves only
                # to microseconds. The digest reads the physical int64, so without this the
                # recorded value and the written one would disagree by the bottom 3 digits.
                microsecond: pl.Expr = (column.cast(pl.Int64) // NANOSECONDS_PER_MICROSECOND * NANOSECONDS_PER_MICROSECOND).cast(pl.Time)
                values_moved = values_moved or _any_true(df, microsecond.cast(pl.Int64) != column.cast(pl.Int64))
                expressions.append(microsecond.alias(name))
            else:
                expressions.append(column)
        converted: pl.DataFrame = df.select(expressions)
        return converted, converted.schema != df.schema or values_moved


def _any_true(df: pl.DataFrame, predicate: pl.Expr) -> bool:
    """Return whether a boolean expression holds for at least one row.

    Args:
        df: The frame the expression is evaluated against.
        predicate: An expression producing a ``Boolean`` column.

    Returns:
        True when at least one row satisfies it. An empty frame, and a column whose
        predicate is null throughout, both give False rather than null.
    """
    return bool(df.select(predicate.any()).item())
