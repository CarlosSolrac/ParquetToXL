"""What a round trip through an Excel workbook does to a dataframe."""

from __future__ import annotations

from typing import ClassVar

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
    ``Int*``, ``UInt*``, ``Float32``      ``Float64``
    ``Decimal``                           ``Float64``
    ``Boolean``                           ``Float64``, -1.0 / 0.0, null preserved
    ``String``                            ``String`` truncated to ``EXCEL_CELL_LIMIT``
    ``Binary``                            lowercase hex ``String``, then truncated
    ``Categorical``                       ``String`` (the labels)
    ``Duration``                          ``Float64`` seconds
    ``Date``, ``Datetime``, ``Time``      unchanged
    ``Null``                              unchanged
    ===================================== ==============================================

    Two limits of this model are worth stating, because they are places where a measured
    round trip loses information that this conversion does not reproduce:

    - A null and an empty string are distinct here but identical after a real round trip:
      every null of every dtype reads back from a worksheet as ``''``. A digest taken
      before writing therefore will not match one taken after, for any frame containing
      nulls.
    - ``NaN`` and ``+/-inf`` survive this conversion as themselves, but the chosen writer
      turns them into empty cells. ``encode_value`` canonicalises NaN, but after a real
      round trip there is nothing left to canonicalise.

    Both are recorded in ``tests/fixtures/excel-round-trip-findings.md`` with the
    measurements behind them. Neither is modelled here, because the spec's cast table does
    not cover them and inventing a rule would make this conversion disagree with the
    written contract rather than with reality.

    One deliberate departure from the spec's cast table: ``Categorical`` is truncated too,
    though the table lists truncation for ``String`` and ``Binary`` only. Leaving it out
    breaks the idempotency contract, because an oversized label survives the first pass as a
    ``String`` and is then cut by the second. Idempotency is load-bearing for the headline
    round-trip test, so it wins over the narrower reading.
    """

    identifier: ClassVar[str] = "to-excel"
    version: ClassVar[str] = "1.0"
    version_number: ClassVar[int] = 1
    description: ClassVar[str] = "Models the dtype and value damage of a round trip through an Excel worksheet."

    def _convert(self, df: pl.DataFrame) -> tuple[pl.DataFrame, bool]:
        """Apply the cast table and report whether anything moved.

        Idempotent by construction: every rule maps into the set of dtypes the rules leave
        alone, so a second pass finds nothing to do. ``Boolean`` becomes ``Float64`` and
        there are then no booleans; ``Binary`` becomes ``String`` and is then only
        truncated; a string already cut to the limit is not over it. The flag is computed
        from the same two facts, so the second pass reports ``False``.

        Args:
            df: The frame to convert. Never mutated.

        Returns:
            The converted frame, and ``True`` when the schema changed or a string was cut.
        """
        expressions: list[pl.Expr] = []
        truncated: bool = False
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
            elif scalars.is_numeric(dtype):
                expressions.append(column.cast(pl.Float64).alias(name))
            elif isinstance(dtype, pl.Binary):
                hexed: pl.Expr = column.bin.encode("hex")
                truncated = truncated or _over_limit(df, hexed)
                expressions.append(hexed.str.slice(0, EXCEL_CELL_LIMIT).alias(name))
            elif scalars.is_text(dtype):
                # String and Categorical share this branch, and Categorical has to be
                # truncated here even though the spec lists truncation only for String and
                # Binary. Leaving it out breaks idempotency: an oversized label would pass
                # through as String on the first call and be cut on the second, so the frame
                # and the flag would both depend on how many times the conversion ran. The
                # cast is a no-op for a column that is already String.
                text: pl.Expr = column.cast(pl.String)
                truncated = truncated or _over_limit(df, text)
                expressions.append(text.str.slice(0, EXCEL_CELL_LIMIT).alias(name))
            elif isinstance(dtype, pl.Duration):
                expressions.append((column.cast(pl.Int64) / DURATION_DIVISORS[dtype.time_unit]).cast(pl.Float64).alias(name))
            else:
                expressions.append(column)
        converted: pl.DataFrame = df.select(expressions)
        return converted, converted.schema != df.schema or truncated


def _over_limit(df: pl.DataFrame, text: pl.Expr) -> bool:
    """Return whether any value of a text expression exceeds Excel's cell limit.

    Args:
        df: The frame the expression is evaluated against.
        text: An expression producing a ``String`` column.

    Returns:
        True when at least one value is longer than ``EXCEL_CELL_LIMIT``. An empty or
        all-null column is False rather than null.
    """
    return bool(df.select((text.str.len_chars() > EXCEL_CELL_LIMIT).any()).item())
