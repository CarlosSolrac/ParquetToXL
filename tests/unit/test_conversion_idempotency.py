"""Frozen tests for the idempotency contract and ``schema_or_data_changed``.

Its own module because it is its own ticket. The per-dtype rules in
``test_conversion_to_excel.py`` each check one cast; this checks the property that holds
*across* them, which will not fall out of those tests by accident.

Why it is load-bearing: spec line 176 runs **both** operands of the headline round-trip test
through ``_convert`` -- the frame read from Parquet and the frame read back from a workbook --
so that it compares like with like. If a second conversion moved a value, the two operands
would be at different numbers of passes and the comparison would be meaningless.

Frames are compared through ``_same_values``, not ``DataFrame.equals`` and not a plain
``to_dicts()`` comparison. Both of the obvious forms report identical frames as different
here, for two unrelated reasons, and both were measured rather than assumed:

- ``equals`` returns ``False`` for an ``Object`` column even against itself, and a frame read
  back from a worksheet acquires ``Object`` columns wherever the sheet mixed types.
- ``to_dicts()`` equality fails the moment a frame holds a NaN, because NaN never equals
  itself. Every frame below carries one deliberately, so this is not a corner case here.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest

from parquet_to_xl.conversion.base import ConvertedDataframe
from parquet_to_xl.conversion.to_excel import EXCEL_CELL_LIMIT, DataframeConversionToExcel
from parquet_to_xl.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash


def _every_dtype_frame() -> pl.DataFrame:
    """One column per scalar Polars dtype, carrying the values that move under conversion."""
    return pl.DataFrame(
        {
            "int8": pl.Series([-128, 127, None], dtype=pl.Int8),
            "int16": pl.Series([-32768, 32767, None], dtype=pl.Int16),
            "int32": pl.Series([-2147483648, 2147483647, None], dtype=pl.Int32),
            "int64": pl.Series([-9223372036854775808, 9223372036854775807, None], dtype=pl.Int64),
            "uint8": pl.Series([0, 255, None], dtype=pl.UInt8),
            "uint16": pl.Series([0, 65535, None], dtype=pl.UInt16),
            "uint32": pl.Series([0, 4294967295, None], dtype=pl.UInt32),
            "uint64": pl.Series([0, 18446744073709551615, None], dtype=pl.UInt64),
            "float32": pl.Series([1.5, float("nan"), None], dtype=pl.Float32),
            "float64": pl.Series([float("inf"), -0.0, None], dtype=pl.Float64),
            "boolean": pl.Series([True, False, None], dtype=pl.Boolean),
            "string": pl.Series(["x" * (EXCEL_CELL_LIMIT + 7), "", None], dtype=pl.String),
            "binary": pl.Series([b"\xff\xfe", b"", None], dtype=pl.Binary),
            "date": pl.Series([dt.date(1899, 12, 31), dt.date(9999, 12, 31), None], dtype=pl.Date),
            "time": pl.Series([dt.time(0, 0), dt.time(23, 59, 59, 999999), None], dtype=pl.Time),
            "datetime": pl.Series([dt.datetime(1970, 1, 1, tzinfo=dt.UTC), dt.datetime(2262, 4, 11, tzinfo=dt.UTC), None], dtype=pl.Datetime("us", "UTC")),
            "duration": pl.Series([dt.timedelta(microseconds=1), dt.timedelta(days=-100000), None], dtype=pl.Duration("us")),
            "decimal": pl.Series([Decimal("1.2500"), Decimal("-9.9900"), None], dtype=pl.Decimal(18, 4)),
            "categorical": pl.Series(["alpha", "", None], dtype=pl.Categorical),
            "null": pl.Series([None, None, None], dtype=pl.Null),
        },
    )


def _convert(df: pl.DataFrame) -> tuple[pl.DataFrame, bool]:
    result: ConvertedDataframe = DataframeConversionToExcel().metadata_of_converted_dataframe(df, [])
    return result.converted_dataframe, result.schema_or_data_changed


def _digest(df: pl.DataFrame) -> str:
    return DataFrameHasherBinaryAggregateHash().hash_dataframe(df).digest_hex


def _same_values(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    """Compare two frames cell by cell, treating NaN as equal to itself.

    A plain ``to_dicts()`` comparison reports two identical frames as different the moment
    either holds a NaN, because NaN never equals itself. Every frame here carries one on
    purpose, so the naive form would fail on data that did not move.
    """
    if left.columns != right.columns or left.height != right.height:
        return False
    name: str
    for name in left.columns:
        a: list[object] = left[name].to_list()
        b: list[object] = right[name].to_list()
        index: int
        for index in range(len(a)):
            first: object = a[index]
            second: object = b[index]
            if isinstance(first, float) and isinstance(second, float) and first != first and second != second:
                continue
            if first != second:
                return False
    return True


def test_a_second_conversion_moves_nothing_for_any_dtype() -> None:
    once: pl.DataFrame
    first_changed: bool
    once, first_changed = _convert(_every_dtype_frame())
    twice: pl.DataFrame
    second_changed: bool
    twice, second_changed = _convert(once)
    assert first_changed is True
    assert second_changed is False
    assert twice.schema == once.schema
    assert _same_values(twice, once)


def test_the_digest_is_stable_from_the_first_conversion_onward() -> None:
    # The property the headline test actually rests on. Stronger than frame equality, and it
    # is the value that gets recorded and compared against a workbook.
    current: pl.DataFrame = _convert(_every_dtype_frame())[0]
    settled: str = _digest(current)
    pass_number: int
    for pass_number in range(2, 6):
        current = _convert(current)[0]
        assert _digest(current) == settled, f"digest moved on pass {pass_number}"


def test_the_flag_is_false_on_every_pass_after_the_first() -> None:
    current: pl.DataFrame = _convert(_every_dtype_frame())[0]
    changed: bool
    pass_number: int
    for pass_number in range(2, 6):
        current, changed = _convert(current)
        assert changed is False, f"pass {pass_number} claimed a change"


def test_the_flag_is_true_when_only_the_schema_moves() -> None:
    # No string anywhere, so truncation cannot be what sets it.
    df: pl.DataFrame = pl.DataFrame({"i": pl.Series([1, 2], dtype=pl.Int64)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert changed is True
    assert converted.schema != df.schema


def test_the_flag_is_true_when_only_the_data_moves() -> None:
    # String to String: the schema is identical, so truncation is the only thing that can
    # set the flag. This is the half of "schema or data" that a schema check alone misses.
    df: pl.DataFrame = pl.DataFrame({"s": pl.Series(["x" * (EXCEL_CELL_LIMIT + 1)], dtype=pl.String)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.schema == df.schema
    assert changed is True


def test_an_already_excel_safe_frame_reports_no_change_and_is_returned_intact() -> None:
    df: pl.DataFrame = pl.DataFrame(
        {
            "f": pl.Series([1.5, None], dtype=pl.Float64),
            "s": pl.Series(["short", None], dtype=pl.String),
            "d": pl.Series([dt.date(2020, 1, 1), None], dtype=pl.Date),
            "t": pl.Series([dt.time(12, 0), None], dtype=pl.Time),
        },
    )
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert changed is False
    assert converted.schema == df.schema
    assert _same_values(converted, df)


def test_the_flag_and_the_frame_agree_about_whether_anything_happened() -> None:
    # A flag that disagrees with the frame is worse than either being wrong alone, because
    # schema_or_data_changed is what a caller uses to decide whether to rewrite a file.
    case: pl.DataFrame
    for case in (_every_dtype_frame(), pl.DataFrame({"f": pl.Series([1.0], dtype=pl.Float64)}), pl.DataFrame()):
        converted: pl.DataFrame
        changed: bool
        converted, changed = _convert(case)
        untouched: bool = converted.schema == case.schema and _same_values(converted, case)
        assert changed is not untouched


def test_metadata_from_a_second_conversion_equals_the_first() -> None:
    # The public surface, not just _convert: the recorded digests are what the round-trip
    # test compares, so they are what has to settle after one pass.
    conversion: DataframeConversionToExcel = DataframeConversionToExcel()
    hashers: list[DataFrameHasherBinaryAggregateHash] = [DataFrameHasherBinaryAggregateHash()]
    first: ConvertedDataframe = conversion.metadata_of_converted_dataframe(_every_dtype_frame(), hashers)
    second: ConvertedDataframe = conversion.metadata_of_converted_dataframe(first.converted_dataframe, hashers)
    assert second.columns_metadata == first.columns_metadata
    assert second.schema_or_data_changed is False


def test_column_order_and_names_survive_repeated_conversion() -> None:
    original: pl.DataFrame = _every_dtype_frame()
    current: pl.DataFrame = original
    _pass: int
    for _pass in range(3):
        current = _convert(current)[0]
    assert current.columns == original.columns


def test_a_nested_column_fails_loudly_rather_than_being_recorded() -> None:
    # Nested dtypes are out of scope per the spec. _convert passes one through and reports
    # no change, which is only safe because the public method then refuses. That refusal
    # used to be Polars raising from the extremes; it is now build_columns_metadata saying
    # so outright, which holds whether or not a hasher is supplied. Pinned so the failure
    # stays loud rather than becoming a silently recorded row no consumer could interpret.
    nested: pl.DataFrame = pl.DataFrame({"x": pl.Series("x", [[1, 2]], dtype=pl.List(pl.Int64))})
    with pytest.raises(TypeError, match="nested dtypes are out of scope"):
        DataframeConversionToExcel().metadata_of_converted_dataframe(nested, [])


def test_a_nested_column_also_fails_when_a_hasher_reaches_it() -> None:
    nested: pl.DataFrame = pl.DataFrame({"x": pl.Series("x", [[1, 2]], dtype=pl.List(pl.Int64))})
    with pytest.raises((TypeError, pl.exceptions.InvalidOperationError)):
        DataframeConversionToExcel().metadata_of_converted_dataframe(nested, [DataFrameHasherBinaryAggregateHash()])


def test_repeated_conversion_of_an_empty_frame_is_stable() -> None:
    current: pl.DataFrame = pl.DataFrame()
    changed: bool
    _pass: int
    for _pass in range(3):
        current, changed = _convert(current)
        assert changed is False
        assert current.width == 0
