"""Frozen tests for ``DataframeConversionToExcel``.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it: everything downstream assumes the test is the specification.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Literal

import polars as pl

from parquet_to_xl.conversion.base import ConvertedDataframe
from parquet_to_xl.conversion.to_excel import EXCEL_CELL_LIMIT, EXCEL_FALSE, EXCEL_TRUE, DataframeConversionToExcel


def _convert(df: pl.DataFrame) -> tuple[pl.DataFrame, bool]:
    """Convert through the public method, which is the only supported entry point."""
    result: ConvertedDataframe = DataframeConversionToExcel().metadata_of_converted_dataframe(df, [])
    return result.converted_dataframe, result.schema_or_data_changed


def test_every_integer_width_becomes_float64() -> None:
    df: pl.DataFrame = pl.DataFrame(
        {"a": pl.Series([1], dtype=pl.Int8), "b": pl.Series([1], dtype=pl.Int64), "c": pl.Series([1], dtype=pl.UInt8), "d": pl.Series([1], dtype=pl.UInt64)},
    )
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.dtypes == [pl.Float64] * 4
    assert changed is True


def test_float32_and_decimal_become_float64() -> None:
    df: pl.DataFrame = pl.DataFrame({"f": pl.Series([1.5], dtype=pl.Float32), "d": pl.Series([Decimal("1.2500")], dtype=pl.Decimal(18, 4))})
    converted: pl.DataFrame = _convert(df)[0]
    assert converted.dtypes == [pl.Float64, pl.Float64]
    assert converted.to_dicts() == [{"f": 1.5, "d": 1.25}]


def test_float64_is_left_alone_including_nan_and_infinities() -> None:
    df: pl.DataFrame = pl.DataFrame({"f": pl.Series([0.0, -0.0, float("nan"), float("inf"), float("-inf"), None], dtype=pl.Float64)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    values: list[float | None] = converted["f"].to_list()
    assert changed is False
    assert values[0] == 0.0
    assert math.isnan(values[2] or 0.0)
    assert values[3] == float("inf")
    assert values[4] == float("-inf")
    assert values[5] is None


def test_booleans_map_to_excels_signed_numeric_form() -> None:
    df: pl.DataFrame = pl.DataFrame({"b": pl.Series([True, False, None], dtype=pl.Boolean)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.dtypes == [pl.Float64]
    assert converted["b"].to_list() == [EXCEL_TRUE, EXCEL_FALSE, None]
    assert EXCEL_TRUE == -1.0
    assert changed is True


def test_long_strings_are_truncated_and_the_flag_says_so() -> None:
    df: pl.DataFrame = pl.DataFrame({"s": pl.Series(["x" * (EXCEL_CELL_LIMIT + 33), "short", None], dtype=pl.String)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    values: list[str | None] = converted["s"].to_list()
    assert len(values[0] or "") == EXCEL_CELL_LIMIT
    assert values[1] == "short"
    assert values[2] is None
    assert changed is True


def test_a_short_string_column_reports_no_change() -> None:
    df: pl.DataFrame = pl.DataFrame({"s": pl.Series(["a", "", None], dtype=pl.String)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted["s"].to_list() == ["a", "", None]
    assert changed is False


def test_binary_becomes_lowercase_hex() -> None:
    df: pl.DataFrame = pl.DataFrame({"x": pl.Series([b"\xff\xfe", b"", None], dtype=pl.Binary)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.dtypes == [pl.String]
    assert converted["x"].to_list() == ["fffe", "", None]
    assert changed is True


def test_binary_is_truncated_after_hex_encoding_not_before() -> None:
    # Hex doubles the length, so a blob of half the limit plus one crosses it only once
    # encoded. Truncating the bytes first would leave the value untouched and the flag False.
    df: pl.DataFrame = pl.DataFrame({"x": pl.Series([b"\xab" * (EXCEL_CELL_LIMIT // 2 + 1)], dtype=pl.Binary)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert len(converted["x"].to_list()[0] or "") == EXCEL_CELL_LIMIT
    assert changed is True


def test_categorical_becomes_its_string_labels() -> None:
    df: pl.DataFrame = pl.DataFrame({"c": pl.Series(["alpha", None, "beta"], dtype=pl.Categorical)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.dtypes == [pl.String]
    assert converted["c"].to_list() == ["alpha", None, "beta"]
    assert changed is True


def test_an_oversized_categorical_label_is_truncated_on_the_first_pass() -> None:
    # Regression, from a Codex review. Casting Categorical to String without truncating left
    # an oversized label for the *second* pass to cut, so the frame and the flag depended on
    # how many times the conversion ran. Idempotency outranks the spec's narrower wording.
    df: pl.DataFrame = pl.DataFrame({"c": pl.Series(["z" * (EXCEL_CELL_LIMIT + 1)], dtype=pl.Categorical)})
    once: pl.DataFrame
    first_changed: bool
    once, first_changed = _convert(df)
    twice: pl.DataFrame
    second_changed: bool
    twice, second_changed = _convert(once)
    assert len(once["c"].to_list()[0] or "") == EXCEL_CELL_LIMIT
    assert first_changed is True
    assert second_changed is False
    assert twice.to_dicts() == once.to_dicts()


def test_durations_become_float_seconds_keeping_sub_second_precision() -> None:
    # dt.total_seconds() returns Int64 and would render one microsecond as zero.
    df: pl.DataFrame = pl.DataFrame({"d": pl.Series([dt.timedelta(seconds=1), dt.timedelta(microseconds=1), dt.timedelta(days=-2), None], dtype=pl.Duration("us"))})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.dtypes == [pl.Float64]
    assert converted["d"].to_list() == [1.0, 1e-06, -172800.0, None]
    assert changed is True


def test_every_duration_unit_produces_the_same_seconds() -> None:
    # The physical int64 counts the column's own unit, so one divisor cannot serve all three.
    unit: Literal["ms", "us", "ns"]
    for unit in ("ms", "us", "ns"):
        df: pl.DataFrame = pl.DataFrame({"d": pl.Series([dt.timedelta(seconds=1)], dtype=pl.Duration(unit))})
        assert _convert(df)[0]["d"].to_list() == [1.0], unit


def test_date_time_datetime_and_null_are_untouched() -> None:
    df: pl.DataFrame = pl.DataFrame(
        {
            "d": pl.Series([dt.date(2020, 1, 1)], dtype=pl.Date),
            "t": pl.Series([dt.time(1, 2, 3)], dtype=pl.Time),
            "s": pl.Series([dt.datetime(2020, 1, 1, tzinfo=dt.UTC)], dtype=pl.Datetime("us", "UTC")),
            "n": pl.Series([None], dtype=pl.Null),
        },
    )
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.schema == df.schema
    assert changed is False


def test_column_order_and_names_are_preserved() -> None:
    df: pl.DataFrame = pl.DataFrame({"z": pl.Series([1], dtype=pl.Int8), "a": pl.Series(["x"], dtype=pl.String), "m": pl.Series([True], dtype=pl.Boolean)})
    assert _convert(df)[0].columns == ["z", "a", "m"]


def test_column_names_that_look_like_selectors_are_treated_literally() -> None:
    # Regression, from a Codex review. pl.col() reads a name wrapped in ^...$ as a regex and
    # "*" as every column, and all of these are legal Parquet column names. Selecting by
    # position is what keeps a frame's own columns addressable.
    df: pl.DataFrame = pl.DataFrame({"^a$": pl.Series([3], dtype=pl.Int64), "a": pl.Series([5], dtype=pl.Int64), "*": pl.Series([7], dtype=pl.Int64)})
    converted: pl.DataFrame = _convert(df)[0]
    assert converted.columns == ["^a$", "a", "*"]
    assert converted.to_dicts() == [{"^a$": 3.0, "a": 5.0, "*": 7.0}]


def test_a_lone_regex_shaped_column_name_survives() -> None:
    df: pl.DataFrame = pl.DataFrame({"^a$": pl.Series([3], dtype=pl.Int64)})
    converted: pl.DataFrame = _convert(df)[0]
    assert converted.columns == ["^a$"]
    assert converted.to_dicts() == [{"^a$": 3.0}]


def test_duplicate_looking_names_do_not_collide_across_dtypes() -> None:
    # Exercises the string and binary branches too, since each builds its own expression.
    df: pl.DataFrame = pl.DataFrame({"^s$": pl.Series(["x"], dtype=pl.String), "s": pl.Series([b"\xff"], dtype=pl.Binary)})
    converted: pl.DataFrame = _convert(df)[0]
    assert converted.to_dicts() == [{"^s$": "x", "s": "ff"}]


def test_the_input_frame_is_not_mutated() -> None:
    df: pl.DataFrame = pl.DataFrame({"b": pl.Series([True], dtype=pl.Boolean)})
    before: list[pl.DataType] = df.dtypes
    _convert(df)
    assert df.dtypes == before
    assert df["b"].to_list() == [True]


def test_conversion_is_idempotent_over_every_dtype() -> None:
    # Spec line 176: the headline test runs both operands through _convert, so a second
    # pass must be a no-op in both the frame and the flag.
    df: pl.DataFrame = pl.DataFrame(
        {
            "i": pl.Series([1, None], dtype=pl.Int64),
            "f": pl.Series([1.5, None], dtype=pl.Float32),
            "b": pl.Series([True, None], dtype=pl.Boolean),
            "s": pl.Series(["x" * (EXCEL_CELL_LIMIT + 5), None], dtype=pl.String),
            "y": pl.Series([b"\xff", None], dtype=pl.Binary),
            "c": pl.Series(["a", None], dtype=pl.Categorical),
            "u": pl.Series([dt.timedelta(seconds=2), None], dtype=pl.Duration("us")),
            "d": pl.Series([dt.date(2020, 1, 1), None], dtype=pl.Date),
        },
    )
    once: pl.DataFrame
    first_changed: bool
    once, first_changed = _convert(df)
    twice: pl.DataFrame
    second_changed: bool
    twice, second_changed = _convert(once)
    assert first_changed is True
    assert second_changed is False
    assert twice.schema == once.schema
    assert twice.to_dicts() == once.to_dicts()


def test_an_empty_frame_converts_without_error() -> None:
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(pl.DataFrame())
    assert converted.width == 0
    assert changed is False


def test_an_all_null_string_column_does_not_report_truncation() -> None:
    df: pl.DataFrame = pl.DataFrame({"s": pl.Series([None, None], dtype=pl.String)})
    assert _convert(df)[1] is False
