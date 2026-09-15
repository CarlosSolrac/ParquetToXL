"""Frozen tests for turning a raw cell value into a calendar point.

The largest matrix in the package. Every refusal here has a plausible-looking wrong answer
available -- a truncated integer, an invented day, a naive timestamp read as UTC -- and each
of those would produce an export that looks right and answers a different question.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from pqx_calendar.columns import DateColumn, DateDateColumn, DatetimeDateColumn, IntDateColumn, SourceWallClockTimezone, ZoneTimezone
from pqx_calendar.decoding import decode, decode_cell, decode_date, decode_datetime, decode_int, resolve_two_digit_year
from pqx_calendar.errors import DateDecodeError, UnknownTimezoneError
from pqx_calendar.points import CalendarPoint

DATE_COLUMN: DateDateColumn = DateDateColumn(type="date")
WALL_CLOCK: DatetimeDateColumn = DatetimeDateColumn(type="datetime", calendar_timezone=SourceWallClockTimezone(mode="source_wall_clock"))
MEXICO: DatetimeDateColumn = DatetimeDateColumn(type="datetime", calendar_timezone=ZoneTimezone(mode="zone", zone="America/Mexico_City"))
YYMM_1970: IntDateColumn = IntDateColumn(type="int", format="YYMM", two_digit_year_window_start=1970)
YYYYMM: IntDateColumn = IntDateColumn(type="int", format="YYYYMM")
YYMMDD_1970: IntDateColumn = IntDateColumn(type="int", format="YYMMDD", two_digit_year_window_start=1970)
YYYYMMDD: IntDateColumn = IntDateColumn(type="int", format="YYYYMMDD")


class _OffsetlessZone(dt.tzinfo):
    """A ``tzinfo`` that admits to knowing no offset, so its datetimes are naive in effect."""

    def utcoffset(self, dt: dt.datetime | None) -> dt.timedelta | None:  # noqa: ARG002
        """Return no offset at all."""
        return None

    def tzname(self, dt: dt.datetime | None) -> str | None:  # noqa: ARG002
        """Return no name either."""
        return None

    def dst(self, dt: dt.datetime | None) -> dt.timedelta | None:  # noqa: ARG002
        """Return no daylight-saving offset."""
        return None


# --------------------------------------------------------------------------------------
# The two-digit year window
# --------------------------------------------------------------------------------------


# 1999, not 2099: the window is 1970-2069 inclusive, and 2099 is outside it. The digits do
# not simply take the later century -- they take the one century that puts them in range.
@pytest.mark.parametrize(("two_digits", "expected"), [(69, 2069), (70, 1970), (71, 1971), (0, 2000), (99, 1999), (1, 2001)])
def test_the_1970_window_resolves_each_pair_of_digits(two_digits: int, expected: int) -> None:
    # partitioning-spec.md, verbatim: "With a window starting in 1970, 69 means 2069 and
    # 70 means 1970." Never the current year, never a platform default.
    assert resolve_two_digit_year(two_digits, 1970) == expected


@pytest.mark.parametrize("window_start", [1, 1900, 1970, 2000, 9900])
@pytest.mark.parametrize("two_digits", [0, 1, 50, 98, 99])
def test_every_resolved_year_lands_inside_its_window(window_start: int, two_digits: int) -> None:
    resolved: int = resolve_two_digit_year(two_digits, window_start)
    assert window_start <= resolved <= window_start + 99
    assert resolved % 100 == two_digits


def test_a_window_starting_on_a_century_maps_the_digits_straight_through() -> None:
    assert resolve_two_digit_year(25, 2000) == 2025
    assert resolve_two_digit_year(0, 2000) == 2000
    assert resolve_two_digit_year(99, 2000) == 2099


# --------------------------------------------------------------------------------------
# date columns
# --------------------------------------------------------------------------------------


def test_a_date_value_is_used_as_the_calendar_date_it_already_is() -> None:
    assert decode_date(dt.date(2025, 1, 31)) == CalendarPoint(2025, 1, 31)


def test_a_datetime_is_refused_by_a_date_column() -> None:
    # datetime is a subclass of date, so an isinstance check alone would accept this and
    # silently drop the time and the zone question with it.
    with pytest.raises(DateDecodeError, match="expected a date"):
        decode_date(dt.datetime(2025, 1, 31, 12, 0, tzinfo=dt.UTC))


@pytest.mark.parametrize("value", [20250131, "2025-01-31", 1.5, None, True])
def test_a_non_date_is_refused_by_a_date_column(value: object) -> None:
    with pytest.raises(DateDecodeError, match="expected a date"):
        decode_date(value)


# --------------------------------------------------------------------------------------
# datetime columns
# --------------------------------------------------------------------------------------


def test_source_wall_clock_uses_the_local_calendar_date_of_a_naive_value() -> None:
    assert decode_datetime(WALL_CLOCK, dt.datetime(2025, 1, 31, 23, 59)) == CalendarPoint(2025, 1, 31)  # noqa: DTZ001


def test_source_wall_clock_ignores_the_zone_of_an_aware_value() -> None:
    # The wall clock is the answer; which zone wrote it is not consulted. Consistent with
    # the existing ToExcel policy.
    aware: dt.datetime = dt.datetime(2026, 1, 1, 2, 0, tzinfo=dt.UTC)
    assert decode_datetime(WALL_CLOCK, aware) == CalendarPoint(2026, 1, 1)


def test_a_named_zone_moves_the_year_boundary() -> None:
    # 2026-01-01T02:00Z is still 2025 in Mexico City (UTC-6). This is the case the whole
    # `zone` mode exists for, and the one a source_wall_clock reading gets wrong.
    aware: dt.datetime = dt.datetime(2026, 1, 1, 2, 0, tzinfo=dt.UTC)
    assert decode_datetime(MEXICO, aware) == CalendarPoint(2025, 12, 31)


def test_a_named_zone_moves_the_day_boundary_the_other_way() -> None:
    aware: dt.datetime = dt.datetime(2025, 6, 30, 20, 0, tzinfo=ZoneInfo("America/Mexico_City"))
    assert decode_datetime(DatetimeDateColumn(type="datetime", calendar_timezone=ZoneTimezone(mode="zone", zone="UTC")), aware) == CalendarPoint(2025, 7, 1)


def test_a_naive_value_is_refused_under_a_named_zone() -> None:
    # Assigning a zone to a naive timestamp needs a DST ambiguity policy v1 does not have.
    with pytest.raises(DateDecodeError, match="is naive"):
        decode_datetime(MEXICO, dt.datetime(2025, 1, 31, 12, 0))  # noqa: DTZ001


def test_a_zone_that_knows_no_offset_is_refused_under_a_named_zone() -> None:
    with pytest.raises(DateDecodeError, match="is naive"):
        decode_datetime(MEXICO, dt.datetime(2025, 1, 31, 12, 0, tzinfo=_OffsetlessZone()))


def test_an_unknown_zone_is_reported_as_a_timezone_problem_not_a_data_problem() -> None:
    column: DatetimeDateColumn = DatetimeDateColumn(type="datetime", calendar_timezone=ZoneTimezone(mode="zone", zone="Mars/Olympus_Mons"))
    with pytest.raises(UnknownTimezoneError, match="timezone database"):
        decode_datetime(column, dt.datetime(2025, 1, 31, 12, 0, tzinfo=dt.UTC))


def test_a_zone_name_that_is_not_even_a_path_is_reported_the_same_way() -> None:
    # ZoneInfo raises ValueError rather than ZoneInfoNotFoundError for an absolute path.
    column: DatetimeDateColumn = DatetimeDateColumn(type="datetime", calendar_timezone=ZoneTimezone(mode="zone", zone="/etc/localtime"))
    with pytest.raises(UnknownTimezoneError, match="timezone database"):
        decode_datetime(column, dt.datetime(2025, 1, 31, 12, 0, tzinfo=dt.UTC))


@pytest.mark.parametrize("value", [dt.date(2025, 1, 31), 20250131, "2025-01-31T00:00:00", None])
def test_a_non_datetime_is_refused_by_a_datetime_column(value: object) -> None:
    with pytest.raises(DateDecodeError, match="expected a datetime"):
        decode_datetime(WALL_CLOCK, value)


# --------------------------------------------------------------------------------------
# integer columns
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        (YYMM_1970, 2501, CalendarPoint(2025, 1)),
        (YYMM_1970, 6912, CalendarPoint(2069, 12)),
        (YYMM_1970, 7001, CalendarPoint(1970, 1)),
        (YYYYMM, 202501, CalendarPoint(2025, 1)),
        (YYYYMM, 999912, CalendarPoint(9999, 12)),
        (YYMMDD_1970, 250131, CalendarPoint(2025, 1, 31)),
        (YYMMDD_1970, 700101, CalendarPoint(1970, 1, 1)),
        (YYYYMMDD, 20250131, CalendarPoint(2025, 1, 31)),
        (YYYYMMDD, 20240229, CalendarPoint(2024, 2, 29)),
    ],
)
def test_each_integer_format_decodes_its_own_encoding(column: IntDateColumn, value: int, expected: CalendarPoint) -> None:
    assert decode_int(column, value) == expected


def test_a_short_value_is_zero_padded_to_the_format_width() -> None:
    # partitioning-spec.md, verbatim: "101 in YYMM means January 2001 for that window."
    assert decode_int(YYMM_1970, 101) == CalendarPoint(2001, 1)


@pytest.mark.parametrize(("column", "value", "expected"), [(YYYYMM, 101, CalendarPoint(1, 1)), (YYYYMMDD, 10101, CalendarPoint(1, 1, 1)), (YYMMDD_1970, 10101, CalendarPoint(2001, 1, 1))])
def test_zero_padding_reaches_the_earliest_supported_years(column: IntDateColumn, value: int, expected: CalendarPoint) -> None:
    assert decode_int(column, value) == expected


def test_the_century_window_inverts_numeric_order() -> None:
    # The case a reader will not expect, and the reason the manifest records the decoded key
    # beside the raw value: 6912 is the smaller number and the later date.
    december_2069: CalendarPoint = decode_int(YYMM_1970, 6912)
    january_1970: CalendarPoint = decode_int(YYMM_1970, 7001)
    assert december_2069.sort_key > january_1970.sort_key


@pytest.mark.parametrize("value", [True, False])
def test_a_boolean_is_refused_even_though_bool_subclasses_int(value: bool) -> None:
    with pytest.raises(DateDecodeError, match="expected an integer"):
        decode_int(YYYYMM, value)


@pytest.mark.parametrize("value", [202501.0, "202501", None, dt.date(2025, 1, 1)])
def test_a_non_integer_is_refused_by_an_integer_column(value: object) -> None:
    with pytest.raises(DateDecodeError, match="expected an integer"):
        decode_int(YYYYMM, value)


@pytest.mark.parametrize("value", [-1, -202501])
def test_a_negative_integer_is_refused(value: int) -> None:
    with pytest.raises(DateDecodeError, match="is negative"):
        decode_int(YYYYMM, value)


@pytest.mark.parametrize(("column", "value"), [(YYMM_1970, 12345), (YYYYMM, 2025011), (YYMMDD_1970, 2501311), (YYYYMMDD, 202501311)])
def test_a_value_wider_than_the_format_is_refused_rather_than_trimmed(column: IntDateColumn, value: int) -> None:
    # Padding is not truncation. Trimming would decode a corrupt value into a valid-looking date.
    with pytest.raises(DateDecodeError, match="digits"):
        decode_int(column, value)


@pytest.mark.parametrize(("column", "value"), [(YYYYMM, 1), (YYYYMM, 12), (YYYYMMDD, 101)])
def test_year_zero_is_refused(column: IntDateColumn, value: int) -> None:
    with pytest.raises(DateDecodeError, match="outside the supported range"):
        decode_int(column, value)


@pytest.mark.parametrize(("column", "value"), [(YYMM_1970, 2500), (YYMM_1970, 2513), (YYYYMM, 202500), (YYYYMM, 202513), (YYYYMMDD, 20259901), (YYMMDD_1970, 250031)])
def test_a_month_outside_one_through_twelve_is_refused(column: IntDateColumn, value: int) -> None:
    with pytest.raises(DateDecodeError, match="not a calendar month"):
        decode_int(column, value)


@pytest.mark.parametrize(("column", "value"), [(YYYYMMDD, 20250229), (YYYYMMDD, 20250431), (YYYYMMDD, 20250100), (YYMMDD_1970, 250230), (YYYYMMDD, 20250132)])
def test_an_impossible_day_is_refused(column: IntDateColumn, value: int) -> None:
    with pytest.raises(DateDecodeError, match="not a calendar date"):
        decode_int(column, value)


def test_a_month_only_format_never_reports_a_day() -> None:
    assert decode_int(YYMM_1970, 2502).day is None
    assert decode_int(YYYYMM, 202502).day is None


# --------------------------------------------------------------------------------------
# dispatch and the located error
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        (DATE_COLUMN, dt.date(2025, 1, 31), CalendarPoint(2025, 1, 31)),
        (WALL_CLOCK, dt.datetime(2025, 1, 31, 8, 0), CalendarPoint(2025, 1, 31)),  # noqa: DTZ001
        (YYYYMM, 202501, CalendarPoint(2025, 1)),
    ],
)
def test_decode_dispatches_on_the_declared_type(column: DateColumn, value: object, expected: CalendarPoint) -> None:
    assert decode(column, value) == expected


def test_decode_cell_returns_the_point_when_the_value_is_good() -> None:
    assert decode_cell(YYYYMM, 202501, column_name="invoice_month", row_ordinal=7) == CalendarPoint(2025, 1)


def test_decode_cell_names_the_column_and_the_row_ordinal() -> None:
    # partitioning-spec.md requires all three: the column, the ordinal and the value.
    caught: pytest.ExceptionInfo[DateDecodeError]
    with pytest.raises(DateDecodeError) as caught:
        decode_cell(YYYYMM, 202513, column_name="invoice_month", row_ordinal=41_000_000)
    assert caught.value.column_name == "invoice_month"
    assert caught.value.row_ordinal == 41_000_000
    assert caught.value.value == 202513
    assert "invoice_month" in str(caught.value)
    assert "41000000" in str(caught.value)
    assert "202513" in str(caught.value)


def test_a_bare_decode_error_names_only_what_it_knows() -> None:
    bare: DateDecodeError = DateDecodeError("unreadable", 7)
    assert bare.column_name is None
    assert bare.row_ordinal is None
    assert str(bare) == "7: unreadable"


def test_locating_an_error_leaves_the_original_message_intact() -> None:
    # The bare error may already be an except clause's argument further up; rewriting its
    # message underneath that handler would change what a log line said after it was written.
    bare: DateDecodeError = DateDecodeError("unreadable", 7)
    located: DateDecodeError = bare.located(column_name="c", row_ordinal=3)
    assert str(bare) == "7: unreadable"
    assert str(located) == "7 in column 'c' at source row 3: unreadable"


def test_an_unknown_zone_is_not_turned_into_a_per_row_error() -> None:
    # It is a property of the column and the machine, not of any one row; locating it would
    # print the same line once per million rows.
    column: DatetimeDateColumn = DatetimeDateColumn(type="datetime", calendar_timezone=ZoneTimezone(mode="zone", zone="Mars/Olympus_Mons"))
    with pytest.raises(UnknownTimezoneError):
        decode_cell(column, dt.datetime(2025, 1, 31, tzinfo=dt.UTC), column_name="c", row_ordinal=0)
