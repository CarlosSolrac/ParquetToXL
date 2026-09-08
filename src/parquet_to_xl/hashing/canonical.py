"""Deterministic byte encoding of a single Polars scalar, used by every hasher."""

from __future__ import annotations

import datetime as dt
import math
import struct
from decimal import Decimal
from typing import Final

CANONICAL_NAN: Final[bytes] = struct.pack("<d", float("nan"))
"""The single payload every NaN collapses to, so a sign bit cannot change a digest."""

EPOCH_DATE: Final[dt.date] = dt.date(1970, 1, 1)
"""Day zero for the ``Date`` tag."""

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
    0x09   ``decimal.Decimal``          ``format(v.normalize(), "f").encode("utf-8")``
    0x0A   ``datetime.timedelta``       ``struct.pack("<q", microseconds)``
    ===== ============================ ================================================

    Tags 0x00 to 0x05 are exactly the post-conversion type set, so a frame that has been
    through ``DataframeConversionToExcel`` uses only those. Tags 0x06 to 0x0A exist because
    ``build_columns_metadata`` also hashes the *source* frame, where ``Int64``, ``Boolean``,
    ``Binary``, ``Decimal`` and ``Duration`` columns are still in their original dtypes.

    Three normalisations, each pinned by a test:

    - ``-0.0`` encodes identically to ``0.0``. IEEE-754 gives them different bit patterns
      but they are the same number.
    - Every NaN encodes as the canonical quiet NaN, ``struct.pack("<d", float("nan"))``.
      A negative or signalling NaN would otherwise produce a different digest for data that
      is equally "not a number".
    - A naive ``datetime`` is assumed to be UTC; an aware one is converted to UTC. This is
      what lets a frame read back from Excel, where ``calamine`` returns naive datetimes,
      hash equal to the frame that was written.

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
        micros: int = int(aware.astimezone(dt.UTC).timestamp() * MICROS_PER_SECOND)
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
        # normalize() collapses trailing zeros so 1.25 and 1.250 give one digest.
        return b"\x09" + format(value.normalize(), "f").encode("utf-8")
    raise TypeError(f"encode_value does not support {type(value).__name__}; nested dtypes are out of scope")
