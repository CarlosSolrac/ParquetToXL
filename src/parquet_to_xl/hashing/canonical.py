"""Deterministic byte encoding of a single Polars scalar, used by every hasher."""

from __future__ import annotations


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
    raise NotImplementedError
