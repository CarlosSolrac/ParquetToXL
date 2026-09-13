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
import pytest

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


def test_non_finite_floats_become_null() -> None:
    # Contract change, approved: NaN and the infinities are blanked, because no writer
    # measured here can store them -- the chosen one emits an empty cell and xlsxwriter
    # refuses outright. The digest has to model the loss to survive a round trip.
    df: pl.DataFrame = pl.DataFrame({"f": pl.Series([0.0, -0.0, float("nan"), float("inf"), float("-inf"), None], dtype=pl.Float64)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    values: list[float | None] = converted["f"].to_list()
    assert converted.schema == df.schema
    assert values == [0.0, -0.0, None, None, None, None]
    assert not any(value is not None and math.isnan(value) for value in values)
    assert changed is True


def test_finite_floats_are_untouched_and_report_no_change() -> None:
    df: pl.DataFrame = pl.DataFrame({"f": pl.Series([0.0, -0.0, 1.5, -2.5, None], dtype=pl.Float64)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted["f"].to_list() == [0.0, -0.0, 1.5, -2.5, None]
    assert changed is False


def test_float32_non_finites_are_blanked_too() -> None:
    df: pl.DataFrame = pl.DataFrame({"f": pl.Series([1.5, float("nan"), float("inf")], dtype=pl.Float32)})
    converted: pl.DataFrame = _convert(df)[0]
    assert converted.dtypes == [pl.Float64]
    assert converted["f"].to_list() == [1.5, None, None]


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
    df: pl.DataFrame = pl.DataFrame({"s": pl.Series(["a", "bb", None], dtype=pl.String)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted["s"].to_list() == ["a", "bb", None]
    assert changed is False


def test_an_empty_string_becomes_null() -> None:
    # Contract change, approved: a worksheet cannot tell an empty string cell from an empty
    # cell, and the two readers disagree about which it returns -- fastexcel says None,
    # python-calamine says ''. The distinction cannot survive, so it is dropped here.
    df: pl.DataFrame = pl.DataFrame({"s": pl.Series(["a", "", None], dtype=pl.String)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.schema == df.schema
    assert converted["s"].to_list() == ["a", None, None]
    assert changed is True


def test_an_empty_categorical_label_becomes_null() -> None:
    df: pl.DataFrame = pl.DataFrame({"c": pl.Series(["alpha", "", None], dtype=pl.Categorical)})
    assert _convert(df)[0]["c"].to_list() == ["alpha", None, None]


def test_binary_becomes_lowercase_hex_and_empty_bytes_become_null() -> None:
    df: pl.DataFrame = pl.DataFrame({"x": pl.Series([b"\xff\xfe", b"", None], dtype=pl.Binary)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.dtypes == [pl.String]
    # b"" hex-encodes to "", which is then blanked by the same rule that blanks "".
    assert converted["x"].to_list() == ["fffe", None, None]
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


def test_enum_becomes_its_string_labels() -> None:
    # Enum is dictionary-encoded text like Categorical and converges on the same rule. Before
    # this, ToExcel left the column an Enum, which fast_excel_reader refuses outright -- so a
    # sidecar describing an Enum column could not be validated at all.
    df: pl.DataFrame = pl.DataFrame({"e": pl.Series(["alpha", None, "beta"], dtype=pl.Enum(["alpha", "beta"]))})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.dtypes == [pl.String]
    assert converted["e"].to_list() == ["alpha", None, "beta"]
    assert changed is True


def test_an_empty_enum_label_becomes_null() -> None:
    df: pl.DataFrame = pl.DataFrame({"e": pl.Series(["alpha", "", None], dtype=pl.Enum(["alpha", ""]))})
    assert _convert(df)[0]["e"].to_list() == ["alpha", None, None]


def test_an_oversized_enum_label_is_truncated_on_the_first_pass() -> None:
    # Same idempotency trap Categorical had: an uncut label would pass through the first call
    # and be cut by the second, making the frame depend on how many times the conversion ran.
    label: str = "z" * (EXCEL_CELL_LIMIT + 1)
    df: pl.DataFrame = pl.DataFrame({"e": pl.Series([label], dtype=pl.Enum([label]))})
    once: pl.DataFrame
    first_changed: bool
    once, first_changed = _convert(df)
    twice: pl.DataFrame
    second_changed: bool
    twice, second_changed = _convert(once)
    assert first_changed is True
    assert len(once["e"][0]) == EXCEL_CELL_LIMIT
    assert once.equals(twice)
    assert second_changed is False


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


def test_datetimes_lose_their_sub_second_component() -> None:
    # Contract change, approved. Measured: 23:47:16.854775 reads back from a workbook as
    # 23:47:16, so the model has to drop the same digits or no digest can survive a trip.
    df: pl.DataFrame = pl.DataFrame({"t": pl.Series([dt.datetime(2262, 4, 11, 23, 47, 16, 854775, tzinfo=dt.UTC), None], dtype=pl.Datetime("us", "UTC"))})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.schema == {"t": pl.Datetime("us")}
    assert converted["t"].to_list() == [dt.datetime(2262, 4, 11, 23, 47, 16, tzinfo=dt.UTC).replace(tzinfo=None), None]
    assert changed is True


def test_a_whole_second_zoned_datetime_reports_a_schema_change() -> None:
    df: pl.DataFrame = pl.DataFrame({"t": pl.Series([dt.datetime(2020, 1, 1, 12, 30, 5, tzinfo=dt.UTC), None], dtype=pl.Datetime("us", "UTC"))})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted["t"].to_list() == [dt.datetime(2020, 1, 1, 12, 30, 5, tzinfo=dt.UTC).replace(tzinfo=None), None]
    assert changed is True


def test_a_naive_datetime_is_truncated_and_stays_naive() -> None:
    # Both naive values are derived from an aware one rather than constructed bare, which is
    # what keeps this inside DTZ001 without a waiver.
    sub_second: dt.datetime = dt.datetime(2020, 1, 1, 0, 0, 0, 500000, tzinfo=dt.UTC).replace(tzinfo=None)
    df: pl.DataFrame = pl.DataFrame({"t": pl.Series([sub_second], dtype=pl.Datetime("us"))})
    converted: pl.DataFrame = _convert(df)[0]
    assert converted.schema == df.schema
    assert converted["t"].to_list() == [sub_second.replace(microsecond=0)]


def test_a_nanosecond_datetime_at_its_range_floor_does_not_wrap() -> None:
    # Regression, from a Codex review. Truncating a Datetime("ns") near the bottom of its
    # range produced a value that range cannot hold: 1677-09-21 00:12:43.145225 wrapped
    # forward to 2262-04-11, and a second pass moved it again, so idempotency broke too.
    floor: dt.datetime = dt.datetime(1677, 9, 21, 0, 12, 43, 145225, tzinfo=dt.UTC)
    df: pl.DataFrame = pl.DataFrame({"t": pl.Series([floor], dtype=pl.Datetime("ns", "UTC"))})
    once: pl.DataFrame
    first_changed: bool
    once, first_changed = _convert(df)
    assert first_changed is True
    assert once["t"].to_list() == [dt.datetime(1677, 9, 21, 0, 12, 43, tzinfo=dt.UTC).replace(tzinfo=None)]
    twice: pl.DataFrame
    second_changed: bool
    twice, second_changed = _convert(once)
    assert twice["t"].to_list() == once["t"].to_list()
    assert second_changed is False


@pytest.mark.parametrize("unit", ["ms", "us", "ns"])
def test_every_datetime_unit_normalises_to_microseconds(unit: Literal["ms", "us", "ns"]) -> None:
    # Excel stores whole seconds, so no nanosecond column is representable. Normalising the
    # unit is what makes the truncated value expressible for every input unit.
    df: pl.DataFrame = pl.DataFrame({"t": pl.Series([dt.datetime(2020, 1, 1, 12, 0, 0, 500000, tzinfo=dt.UTC)], dtype=pl.Datetime(unit, "UTC"))})
    converted: pl.DataFrame = _convert(df)[0]
    assert converted.dtypes == [pl.Datetime("us")]
    assert converted["t"].to_list() == [dt.datetime(2020, 1, 1, 12, 0, 0, tzinfo=dt.UTC).replace(tzinfo=None)]


def test_times_are_truncated_to_microseconds() -> None:
    # Regression, from a Codex review. Polars stores Time as nanoseconds, but every Python
    # boundary the value crosses is a datetime.time, which resolves only to microseconds --
    # so the digest, which reads the physical int64, saw digits the workbook never held.
    df: pl.DataFrame = pl.DataFrame({"t": pl.Series([123456789, None], dtype=pl.Int64).cast(pl.Time)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.dtypes == [pl.Time]
    assert converted.select(pl.nth(0).cast(pl.Int64)).to_series().to_list() == [123456000, None]
    assert changed is True


def test_a_microsecond_time_reports_no_change() -> None:
    df: pl.DataFrame = pl.DataFrame({"t": pl.Series([123456000, None], dtype=pl.Int64).cast(pl.Time)})
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted.select(pl.nth(0).cast(pl.Int64)).to_series().to_list() == [123456000, None]
    assert changed is False


def test_a_microsecond_time_and_a_date_are_untouched() -> None:
    # Date is left alone entirely, and a Time already at microsecond precision survives --
    # the writer renders it as text, and the reader restores it from the schema.
    df: pl.DataFrame = pl.DataFrame(
        {"t": pl.Series([dt.time(23, 59, 59, 999999)], dtype=pl.Time), "d": pl.Series([dt.date(2020, 1, 1)], dtype=pl.Date)},
    )
    converted: pl.DataFrame
    changed: bool
    converted, changed = _convert(df)
    assert converted["t"].to_list() == [dt.time(23, 59, 59, 999999)]
    assert converted["d"].to_list() == [dt.date(2020, 1, 1)]
    assert changed is False


def test_date_time_naive_datetime_and_null_are_untouched() -> None:
    df: pl.DataFrame = pl.DataFrame(
        {
            "d": pl.Series([dt.date(2020, 1, 1)], dtype=pl.Date),
            "t": pl.Series([dt.time(1, 2, 3)], dtype=pl.Time),
            "s": pl.Series([dt.datetime(2020, 1, 1, tzinfo=dt.UTC).replace(tzinfo=None)], dtype=pl.Datetime("us")),
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
