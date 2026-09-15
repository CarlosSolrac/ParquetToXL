"""Turning one registered column's raw value into a :class:`CalendarPoint`.

Every refusal here is a refusal by design. ``partitioning-spec.md`` lists what must be
rejected -- floats, booleans, strings, implicit coercions, negatives, year zero, excess
digits, invalid months, impossible dates, naive input under a named zone -- because each
of them has a plausible-looking wrong answer available, and producing it would yield an
export that answers a different question than the one asked.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pqx_calendar.columns import CalendarTimezone, DateColumn, DateDateColumn, DatetimeDateColumn, IntDateColumn, SourceWallClockTimezone
from pqx_calendar.errors import DateDecodeError, UnknownTimezoneError
from pqx_calendar.points import MAX_YEAR, MIN_YEAR, CalendarPoint


def resolve_two_digit_year(two_digits: int, window_start: int) -> int:
    """Return the unique year ending in ``two_digits`` inside the window starting at ``window_start``.

    The window is the inclusive hundred years ``window_start`` through ``window_start + 99``,
    so exactly one year in it ends in any given pair of digits. With a window starting in
    1970, ``69`` means 2069 and ``70`` means 1970 -- never the current year, never a platform
    default, because either would make the same file decode differently next January.

    Args:
        two_digits: The encoded year, 0 through 99.
        window_start: The first year of the hundred-year window.

    Returns:
        The resolved four-digit year.
    """
    return window_start + (two_digits - window_start) % 100


def decode_date(value: object) -> CalendarPoint:
    """Return the calendar point a Parquet ``date`` value already names.

    ``datetime`` is a subclass of ``date``, so an ``isinstance`` check alone would accept a
    timestamp here and silently drop its time and zone. A column holding datetimes must be
    registered as ``datetime`` and go through :func:`decode_datetime`, which is where the
    zone question gets an answer.

    Args:
        value: The raw cell value.

    Returns:
        The day-precision point.

    Raises:
        DateDecodeError: ``value`` is not a plain ``datetime.date``.
    """
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise DateDecodeError("expected a date; a date column may not hold any other type", value)
    return CalendarPoint.from_date(value)


def decode_datetime(column: DatetimeDateColumn, value: object) -> CalendarPoint:
    """Return the calendar date a Parquet ``datetime`` value falls on, under the column's zone policy.

    Under ``source_wall_clock`` the timestamp's own wall clock is the answer and its zone is
    irrelevant. Under a named zone the instant is converted first, which is exactly where a
    year boundary can move: ``2026-01-01T02:00+00:00`` is still 2025 in Mexico City.

    Args:
        column: The registered column, carrying the zone policy.
        value: The raw cell value.

    Returns:
        The day-precision point.

    Raises:
        DateDecodeError: ``value`` is not a ``datetime``, or it is naive and the column names
            a zone. Assigning a zone to a naive timestamp needs a DST ambiguity policy that
            v1 does not have, so it is refused rather than guessed.
        UnknownTimezoneError: This machine's timezone database has no such zone.
    """
    if not isinstance(value, dt.datetime):
        raise DateDecodeError("expected a datetime; a datetime column may not hold any other type", value)
    policy: CalendarTimezone = column.calendar_timezone
    if isinstance(policy, SourceWallClockTimezone):
        return CalendarPoint(value.year, value.month, value.day)
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise DateDecodeError(f"is naive, so it names no instant to convert into {policy.zone!r}; a naive column requires mode 'source_wall_clock'", value)
    try:
        zone: ZoneInfo = ZoneInfo(policy.zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        message: str = f"{policy.zone!r} is not a zone in this machine's timezone database"
        raise UnknownTimezoneError(message) from exc
    return CalendarPoint.from_date(value.astimezone(zone).date())


def decode_int(column: IntDateColumn, value: object) -> CalendarPoint:
    """Return the calendar point an integer encodes under the column's declared format.

    Nonnegative values are zero-padded to the format's width before parsing, so ``101`` in
    ``YYMM`` is January 2001 under a 1970 window rather than a three-digit value nothing
    could read. Padding is not truncation: a value wider than the format is an error, not a
    field to be trimmed.

    Args:
        column: The registered column, carrying the format and any window.
        value: The raw cell value.

    Returns:
        The point, at month precision for ``YYMM`` and ``YYYYMM`` and day precision otherwise.

    Raises:
        DateDecodeError: The value is not an integer, is negative, has more digits than the
            format, or spells a year, month or day the calendar does not contain.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise DateDecodeError(f"expected an integer in format {column.format}; floats, booleans, strings and implicit coercions are refused", value)
    if value < 0:
        raise DateDecodeError(f"is negative; format {column.format} encodes no negative dates", value)
    digits: str = f"{value:0{column.width}d}"
    if len(digits) > column.width:
        raise DateDecodeError(f"has {len(digits)} digits; format {column.format} has {column.width}", value)

    year_digits: int = 2 if column.format in {"YYMM", "YYMMDD"} else 4
    year: int = int(digits[:year_digits])
    if column.two_digit_year_window_start is not None:
        year = resolve_two_digit_year(year, column.two_digit_year_window_start)
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise DateDecodeError(f"spells year {year:04d}, outside the supported range {MIN_YEAR}-{MAX_YEAR}", value)

    month: int = int(digits[year_digits : year_digits + 2])
    if not 1 <= month <= 12:
        raise DateDecodeError(f"spells month {month:02d}, which is not a calendar month", value)

    day: int | None = int(digits[year_digits + 2 :]) if column.precision == "day" else None
    try:
        return CalendarPoint(year, month, day)
    except ValueError as exc:
        raise DateDecodeError(f"spells day {day} of {year:04d}-{month:02d}, which is not a calendar date", value) from exc


def decode(column: DateColumn, value: object) -> CalendarPoint:
    """Return the calendar point a value encodes, dispatching on the column's declared type.

    The final branch is a fall-through rather than an ``assert_never``: the union has three
    members, two are tested above, and an unreachable ``else`` would be an uncoverable line
    under this project's 100% branch gate.

    Args:
        column: The registered column.
        value: The raw cell value, which must not be null -- null handling is the
            partitioner's ``null_dates`` policy, not a decoding question.

    Returns:
        The decoded point.

    Raises:
        DateDecodeError: The value cannot be interpreted under this column's declaration.
        UnknownTimezoneError: The column names a zone this machine does not have.
    """
    if isinstance(column, DateDateColumn):
        return decode_date(value)
    if isinstance(column, DatetimeDateColumn):
        return decode_datetime(column, value)
    return decode_int(column, value)


def decode_cell(column: DateColumn, value: object, *, column_name: str, row_ordinal: int) -> CalendarPoint:
    """Decode one cell, naming the column and source row ordinal in any refusal.

    The entry point Phase E calls. ``column_name`` and ``row_ordinal`` are required keyword
    arguments rather than optional ones because ``partitioning-spec.md`` requires an invalid
    date to name both, and a default would let a caller drop the half of the message that
    makes the error actionable in a file with millions of rows.

    Args:
        column: The registered column.
        value: The raw, non-null cell value.
        column_name: The exact source column name the value came from.
        row_ordinal: The zero-based source row ordinal.

    Returns:
        The decoded point.

    Raises:
        DateDecodeError: The value cannot be interpreted, now naming where it sits.
        UnknownTimezoneError: The column names a zone this machine does not have. Not
            located, because it is a property of the column and the machine rather than of
            any one row; reporting it per row would print it once per million.
    """
    try:
        return decode(column, value)
    except DateDecodeError as exc:
        raise exc.located(column_name=column_name, row_ordinal=row_ordinal) from exc
