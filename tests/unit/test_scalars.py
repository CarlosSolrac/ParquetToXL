"""Frozen tests for the dtype flag helpers.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it: everything downstream assumes the test is the specification.
"""

from __future__ import annotations

import polars as pl
import pytest

from parquet_to_xl.metadata import scalars

# dtype, numeric, float, integer, decimal, text, boolean.
# Every row was measured against polars 1.44 rather than assumed. The two entries people
# get wrong are Decimal, which is numeric but neither float nor integer, and Boolean,
# which is not numeric despite bool subclassing int in Python.
FLAG_TABLE: list[tuple[pl.DataType, bool, bool, bool, bool, bool, bool]] = [
    (pl.Int8(), True, False, True, False, False, False),
    (pl.Int64(), True, False, True, False, False, False),
    (pl.UInt8(), True, False, True, False, False, False),
    (pl.UInt64(), True, False, True, False, False, False),
    (pl.Float32(), True, True, False, False, False, False),
    (pl.Float64(), True, True, False, False, False, False),
    (pl.Decimal(10, 2), True, False, False, True, False, False),
    (pl.Boolean(), False, False, False, False, False, True),
    (pl.String(), False, False, False, False, True, False),
    (pl.Categorical(), False, False, False, False, True, False),
    (pl.Enum(["a", "b"]), False, False, False, False, True, False),
    (pl.Binary(), False, False, False, False, False, False),
    (pl.Date(), False, False, False, False, False, False),
    (pl.Time(), False, False, False, False, False, False),
    (pl.Datetime("us", "UTC"), False, False, False, False, False, False),
    (pl.Duration(), False, False, False, False, False, False),
    (pl.Null(), False, False, False, False, False, False),
]


@pytest.mark.parametrize(("dtype", "numeric", "flt", "integer", "decimal", "text", "boolean"), FLAG_TABLE)
def test_flags_match_the_measured_table(dtype: pl.DataType, numeric: bool, flt: bool, integer: bool, decimal: bool, text: bool, boolean: bool) -> None:
    assert scalars.is_numeric(dtype) is numeric
    assert scalars.is_float(dtype) is flt
    assert scalars.is_integer(dtype) is integer
    assert scalars.is_decimal(dtype) is decimal
    assert scalars.is_text(dtype) is text
    assert scalars.is_boolean(dtype) is boolean


def test_decimal_is_numeric_but_not_float_or_integer() -> None:
    # Called out on its own because it is the single most surprising row above, and a
    # plausible-looking implementation that tests is_float first would get it wrong.
    dtype: pl.DataType = pl.Decimal(10, 2)
    assert scalars.is_numeric(dtype) is True
    assert scalars.is_decimal(dtype) is True
    assert scalars.is_float(dtype) is False
    assert scalars.is_integer(dtype) is False


def test_boolean_is_not_numeric_and_not_an_integer() -> None:
    # Python's bool subclasses int; the dtype flags must not inherit that confusion.
    dtype: pl.DataType = pl.Boolean()
    assert scalars.is_boolean(dtype) is True
    assert scalars.is_numeric(dtype) is False
    assert scalars.is_integer(dtype) is False


def test_enum_counts_as_text() -> None:
    # Added after an Enum column was found to break the Excel path end to end: ToExcel routes
    # on is_text, so leaving Enum out meant the conversion passed it through unchanged and
    # fast_excel_reader then refused the dtype its own writer had produced.
    assert scalars.is_text(pl.Enum(["a", "b"])) is True
    assert scalars.is_text(pl.Enum([])) is True


def test_categorical_counts_as_text() -> None:
    # A Categorical is dictionary-encoded text: its values read back as str and the ToExcel
    # conversion maps it to String. A consumer filtering on is_text must not miss it.
    assert scalars.is_text(pl.Categorical()) is True
    assert scalars.is_text(pl.String()) is True
    assert scalars.is_text(pl.Binary()) is False


@pytest.mark.parametrize(
    "dtype",
    [pl.List(pl.Int64()), pl.Array(pl.Int64(), 2), pl.Struct({"a": pl.Int64()}), pl.Object()],
    ids=["list", "array", "struct", "object"],
)
def test_every_nested_dtype_is_recognised(dtype: pl.DataType) -> None:
    # All four named individually: build_columns_metadata refuses whatever this returns True
    # for, and the only nested dtype reached by any other test is List, so dropping one of
    # the other three from the union would otherwise go unnoticed.
    assert scalars.is_nested(dtype) is True


@pytest.mark.parametrize("dtype", [row[0] for row in FLAG_TABLE], ids=lambda dtype: str(dtype))
def test_no_scalar_dtype_is_nested(dtype: pl.DataType) -> None:
    assert scalars.is_nested(dtype) is False
