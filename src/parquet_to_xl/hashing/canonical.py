"""Deterministic byte encoding of Polars values, used by every hasher.

``encode_value`` encodes one scalar. ``encode_series`` encodes a whole column and is what
hashers should call: some Polars dtypes carry more precision than the Python scalar they
convert to, and going through ``Series.to_list()`` silently discards it.
"""

from __future__ import annotations

import datetime as dt
import math
import struct
from collections.abc import Iterator
from decimal import Decimal
from typing import Final

import polars as pl

CANONICAL_NAN: Final[bytes] = struct.pack("<d", float("nan"))
"""The single payload every NaN collapses to, so a sign bit cannot change a digest."""

EPOCH_DATE: Final[dt.date] = dt.date(1970, 1, 1)
"""Day zero for the ``Date`` tag."""

EPOCH_DATETIME: Final[dt.datetime] = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
"""Instant zero for the ``Datetime`` tag, subtracted with integer timedelta arithmetic."""

NULL_TAG: Final[bytes] = b"\x00"
TIME_TAG: Final[bytes] = b"\x05"
DATETIME_NANOS_TAG: Final[bytes] = b"\x0b"
DURATION_NANOS_TAG: Final[bytes] = b"\x0c"

NANOS_PER_MICROSECOND: Final[int] = 1000
MICROS_PER_SECOND: Final[int] = 1_000_000
SECONDS_PER_HOUR: Final[int] = 3600
SECONDS_PER_MINUTE: Final[int] = 60


def encode_value(value: object) -> bytes:
    r"""Return deterministic bytes for one Polars scalar.

    Every result is ``tag_byte + payload``. The tag is what stops two different values with
    identical payloads contributing the same term to a digest -- a float ``0.0`` and an
    empty string, or a ``Date(1)`` and a ``Datetime(1)``, all of which would otherwise
    collide once ``hash_dataframe`` pools every value of every column into one sum.

    The tag is a property of the value, not of the column, which is what keeps this
    compatible with the "no column binding" decision.

    ===== ============================ ================================================
    Tag    Value                        Payload
    ===== ============================ ================================================
    0x00   ``None``                     empty
    0x01   ``float``                    ``struct.pack("<d", v)``
    0x02   ``str``                      ``v.encode("utf-8")``
    0x03   ``datetime.date``            ``struct.pack("<q", days_since_epoch)``
    0x04   ``datetime.datetime``        ``struct.pack("<q", micros_since_epoch)``
    0x05   ``datetime.time``            ``struct.pack("<q", nanos_since_midnight)``
    0x06   ``int``                      ``str(v).encode("utf-8")``
    0x07   ``bool``                     ``b"\x01"`` if true else ``b"\x00"``
    0x08   ``bytes``                    the bytes themselves
    0x09   ``decimal.Decimal``          plain decimal text, trailing zeros stripped
    0x0A   ``datetime.timedelta``       ``struct.pack("<q", microseconds)``
    0x0B   ``Datetime("ns")`` column     ``struct.pack("<q", nanos_since_epoch)``
    0x0C   ``Duration("ns")`` column     ``struct.pack("<q", nanoseconds)``
    ===== ============================ ================================================

    Tags 0x00 to 0x05 are exactly the post-conversion type set, so a frame that has been
    through ``DataframeConversionToExcel`` uses only those. Tags 0x06 to 0x0A exist because
    ``build_columns_metadata`` also hashes the *source* frame, where ``Int64``, ``Boolean``,
    ``Binary``, ``Decimal`` and ``Duration`` columns are still in their original dtypes.
    Tags 0x0B and 0x0C are emitted only by ``encode_series``, which never converts a
    nanosecond column to a Python scalar; ``encode_value`` cannot produce them because by
    the time it sees a value the precision is already gone.

    Four normalisations, each pinned by a test:

    - ``-0.0`` encodes identically to ``0.0``. IEEE-754 gives them different bit patterns
      but they are the same number.
    - Every NaN encodes as the canonical quiet NaN, ``struct.pack("<d", float("nan"))``.
      A negative or signalling NaN would otherwise produce a different digest for data that
      is equally "not a number".
    - A naive ``datetime`` is assumed to be UTC; an aware one is converted to UTC. This is
      what lets a frame read back from Excel, where ``calamine`` returns naive datetimes,
      hash equal to the frame that was written.
    - A ``Decimal`` drops insignificant trailing zeros, so ``1.25`` and ``1.250`` agree, but
      keeps every significant digit. Both are done textually rather than through
      ``normalize()``, which rounds to the ambient context precision and would make the
      digest depend on a global that no caller here controls.

    Two payloads are computed with integer arithmetic rather than the obvious float route,
    because the obvious route loses precision inside the range Polars can represent:
    ``Datetime`` subtracts ``EPOCH_DATETIME`` as a ``timedelta`` instead of scaling
    ``timestamp()``, and ``Decimal`` formats rather than normalises.

    Two dispatch orders are load-bearing, because Python's type hierarchy works against
    the table above: ``bool`` is a subclass of ``int``, so it must be tested first or every
    boolean encodes as an integer; and ``datetime`` is a subclass of ``date``, so it must be
    tested first or every timestamp loses its time of day.

    Args:
        value: One scalar read out of a Polars column, or ``None`` for a null.

    Returns:
        The tagged encoding.

    Raises:
        TypeError: The value is not one of the types in the table. Nested dtypes are out of
            scope, and a silent fallback would let an unhashable column produce a digest
            that looked fine.
    """
    if value is None:
        return b"\x00"
    # bool before int, and datetime before date: both are subclasses, and testing the
    # parent first silently mis-encodes every value of the child type.
    if isinstance(value, bool):
        return b"\x07" + (b"\x01" if value else b"\x00")
    if isinstance(value, float):
        if math.isnan(value):
            return b"\x01" + CANONICAL_NAN
        # Catches -0.0 as well, since it compares equal to 0.0 while packing differently.
        normalised: float = 0.0 if value == 0.0 else value
        return b"\x01" + struct.pack("<d", normalised)
    if isinstance(value, int):
        # Decimal text rather than a packed integer: UInt64's maximum does not fit "<q".
        return b"\x06" + str(value).encode("utf-8")
    if isinstance(value, str):
        return b"\x02" + value.encode("utf-8")
    if isinstance(value, bytes):
        return b"\x08" + value
    if isinstance(value, dt.datetime):
        aware: dt.datetime = value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value
        # Integer timedelta arithmetic, not float timestamp(): a float64 mantissa runs
        # out around 2255, and two datetimes a microsecond apart in the year 2300 --
        # which polars holds happily, and which the fixture spec asks for as a
        # far-future edge case -- would otherwise encode identically.
        micros: int = (aware.astimezone(dt.UTC) - EPOCH_DATETIME) // dt.timedelta(microseconds=1)
        return b"\x04" + struct.pack("<q", micros)
    if isinstance(value, dt.date):
        return b"\x03" + struct.pack("<q", (value - EPOCH_DATE).days)
    if isinstance(value, dt.time):
        seconds: int = value.hour * SECONDS_PER_HOUR + value.minute * SECONDS_PER_MINUTE + value.second
        nanos: int = (seconds * MICROS_PER_SECOND + value.microsecond) * NANOS_PER_MICROSECOND
        return b"\x05" + struct.pack("<q", nanos)
    if isinstance(value, dt.timedelta):
        return b"\x0a" + struct.pack("<q", value // dt.timedelta(microseconds=1))
    if isinstance(value, Decimal):
        # format() never consults the ambient decimal context, so no significant digit
        # is rounded away and the encoding cannot change because an unrelated caller
        # altered getcontext(). normalize() does both, and silently.
        text: str = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        # Decimal("-0") equals Decimal("0"), so they encode alike, as -0.0 does.
        return b"\x09" + ("0" if text == "-0" else text).encode("utf-8")
    raise TypeError(f"encode_value does not support {type(value).__name__}; nested dtypes are out of scope")


def _encode_physical(column: pl.Series, tag: bytes) -> Iterator[bytes]:
    """Yield ``tag + int64`` for each value of a column, reading its physical storage.

    Args:
        column: Column whose physical representation carries the full precision.
        tag: Type tag to prefix each payload with.

    Yields:
        The encoding of each value, with nulls encoded as the null sentinel.
    """
    stored: int | None
    for stored in column.to_physical().to_list():
        yield NULL_TAG if stored is None else tag + struct.pack("<q", stored)


def encode_series(column: pl.Series) -> Iterator[bytes]:
    """Yield the canonical encoding of every value in a column.

    This is what a hasher should call, not ``encode_value`` over ``Series.to_list()``.
    Three Polars dtypes store more precision than the Python scalar they convert to, and
    the conversion is silent -- the values simply come back equal:

    - ``Time`` is always nanoseconds since midnight, but ``datetime.time`` resolves only to
      microseconds, so every ``Time`` column loses its bottom three digits.
    - ``Datetime("ns")`` and ``Duration("ns")`` lose the same three digits to
      ``datetime.datetime`` and ``datetime.timedelta``.

    For those three the physical ``int64`` is encoded directly. ``Time`` keeps tag 0x05 and
    its nanosecond payload, which is byte-identical to what ``encode_value`` produces for a
    microsecond-granular time -- so no existing digest changes. The two nanosecond variants
    get their own tags rather than being folded into 0x04 and 0x0A, because those payloads
    are microseconds and widening them to nanoseconds would overflow ``int64`` before the
    far-future dates the fixtures call for.

    Every other dtype goes through ``encode_value``, which is lossless for them.

    Args:
        column: The column to encode.

    Yields:
        The encoding of each value, in column order.
    """
    dtype: pl.DataType = column.dtype
    if isinstance(dtype, pl.Time):
        yield from _encode_physical(column, TIME_TAG)
        return
    if isinstance(dtype, pl.Datetime) and dtype.time_unit == "ns":
        yield from _encode_physical(column, DATETIME_NANOS_TAG)
        return
    if isinstance(dtype, pl.Duration) and dtype.time_unit == "ns":
        yield from _encode_physical(column, DURATION_NANOS_TAG)
        return
    scalar: object
    for scalar in column.to_list():
        yield encode_value(scalar)
