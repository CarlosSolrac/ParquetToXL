"""Frozen tests for the ``date_columns`` configuration models and their one conditional rule."""

from __future__ import annotations

from typing import Any

import pytest
from pqx_calendar.columns import DateColumn, DateDateColumn, DatetimeDateColumn, IntDateColumn, SourceWallClockTimezone, ZoneTimezone
from pydantic import TypeAdapter, ValidationError

ADAPTER: TypeAdapter[DateColumn] = TypeAdapter(DateColumn)


def test_a_date_column_needs_nothing_beyond_its_type() -> None:
    column: DateColumn = ADAPTER.validate_python({"type": "date"})
    assert isinstance(column, DateDateColumn)


def test_a_datetime_column_carries_a_wall_clock_policy() -> None:
    column: DateColumn = ADAPTER.validate_python({"type": "datetime", "calendar_timezone": {"mode": "source_wall_clock"}})
    assert isinstance(column, DatetimeDateColumn)
    assert isinstance(column.calendar_timezone, SourceWallClockTimezone)


def test_a_datetime_column_carries_a_named_zone() -> None:
    column: DateColumn = ADAPTER.validate_python({"type": "datetime", "calendar_timezone": {"mode": "zone", "zone": "America/Mexico_City"}})
    assert isinstance(column, DatetimeDateColumn)
    assert isinstance(column.calendar_timezone, ZoneTimezone)
    assert column.calendar_timezone.zone == "America/Mexico_City"


def test_a_datetime_column_requires_a_zone_policy() -> None:
    with pytest.raises(ValidationError, match="calendar_timezone"):
        ADAPTER.validate_python({"type": "datetime"})


def test_the_wall_clock_sentinel_is_a_mode_not_a_zone_name() -> None:
    # The original put the sentinel in the same string field as real zone names, which makes
    # it collide with the IANA namespace. A discriminated union has no such collision.
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"type": "datetime", "calendar_timezone": {"mode": "zone", "zone": "source_wall_clock", "extra": 1}})


def test_an_empty_zone_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"type": "datetime", "calendar_timezone": {"mode": "zone", "zone": ""}})


@pytest.mark.parametrize(("fmt", "width", "precision"), [("YYMM", 4, "month"), ("YYYYMM", 6, "month"), ("YYMMDD", 6, "day"), ("YYYYMMDD", 8, "day")])
def test_each_integer_format_reports_its_width_and_precision(fmt: str, width: int, precision: str) -> None:
    payload: dict[str, Any] = {"type": "int", "format": fmt}
    if fmt in {"YYMM", "YYMMDD"}:
        payload["two_digit_year_window_start"] = 1970
    column: DateColumn = ADAPTER.validate_python(payload)
    assert isinstance(column, IntDateColumn)
    assert column.width == width
    assert column.precision == precision


@pytest.mark.parametrize("fmt", ["YYMM", "YYMMDD"])
def test_a_two_digit_format_requires_a_window(fmt: str) -> None:
    with pytest.raises(ValidationError, match="two_digit_year_window_start is required"):
        ADAPTER.validate_python({"type": "int", "format": fmt})


@pytest.mark.parametrize("fmt", ["YYYYMM", "YYYYMMDD"])
def test_a_four_digit_format_forbids_a_window(fmt: str) -> None:
    # Forbidden, not merely unused: a setting that silently does nothing is the kind of
    # configuration bug that survives for years.
    with pytest.raises(ValidationError, match="must be absent"):
        ADAPTER.validate_python({"type": "int", "format": fmt, "two_digit_year_window_start": 1970})


@pytest.mark.parametrize("window", [0, -1, 9901])
def test_a_window_outside_the_supported_span_is_refused(window: int) -> None:
    # 9900 is the last start whose hundred years stay inside year 9999.
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"type": "int", "format": "YYMM", "two_digit_year_window_start": window})


@pytest.mark.parametrize("window", [1, 9900])
def test_the_window_endpoints_are_accepted(window: int) -> None:
    column: DateColumn = ADAPTER.validate_python({"type": "int", "format": "YYMM", "two_digit_year_window_start": window})
    assert isinstance(column, IntDateColumn)
    assert column.two_digit_year_window_start == window


@pytest.mark.parametrize("fmt", ["YYYY", "MMYY", "yymm", "YYYYMMDDHH"])
def test_an_unlisted_integer_format_is_refused(fmt: str) -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"type": "int", "format": fmt})


@pytest.mark.parametrize("declared", ["float", "string", "bool", "timestamp"])
def test_a_type_outside_the_three_kinds_is_refused(declared: str) -> None:
    # partitioning-spec.md: reject floats, booleans, strings and implicit coercions.
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"type": declared})


def test_a_field_belonging_to_another_kind_is_refused() -> None:
    # The Pydantic counterpart of the schema's unevaluatedProperties: false. A `format` on a
    # date column means the author believed something about that column that is not true.
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"type": "date", "format": "YYMM"})


def test_every_column_model_is_frozen() -> None:
    column: DateColumn = ADAPTER.validate_python({"type": "date"})
    with pytest.raises(ValidationError):
        column.type = "int"  # type: ignore[misc]
