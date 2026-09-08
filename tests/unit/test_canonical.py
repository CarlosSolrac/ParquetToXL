"""Frozen tests for ``encode_value``.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it: everything downstream assumes the test is the specification.
"""

from __future__ import annotations

import datetime as dt
import struct
import zoneinfo
from decimal import Context, Decimal, localcontext

import pytest

from parquet_to_xl.hashing.canonical import encode_value


def test_null_is_a_bare_sentinel() -> None:
    assert encode_value(None) == b"\x00"


def test_each_type_gets_its_own_tag() -> None:
    tags: list[bytes] = [
        encode_value(None)[:1],
        encode_value(1.5)[:1],
        encode_value("x")[:1],
        encode_value(dt.date(2020, 1, 1))[:1],
        encode_value(dt.datetime(2020, 1, 1, tzinfo=dt.UTC))[:1],
        encode_value(dt.time(1, 2, 3))[:1],
        encode_value(7)[:1],
        encode_value(True)[:1],
        encode_value(b"x")[:1],
        encode_value(Decimal("1.25"))[:1],
        encode_value(dt.timedelta(seconds=5))[:1],
    ]
    assert tags == [bytes([n]) for n in range(0x00, 0x0B)]
    assert len(set(tags)) == len(tags)


def test_float_zero_does_not_collide_with_the_empty_string() -> None:
    # The reason the tag byte exists at all.
    assert encode_value(0.0) != encode_value("")


def test_date_and_datetime_do_not_collide() -> None:
    # Both pack to 8 bytes via struct, so without a tag they would contribute the same
    # term to hash_dataframe's pooled sum.
    assert encode_value(dt.date(1970, 1, 2)) != encode_value(dt.datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=dt.UTC))


def test_negative_zero_normalises_to_zero() -> None:
    assert encode_value(-0.0) == encode_value(0.0)


def test_every_nan_encodes_as_the_canonical_quiet_nan() -> None:
    canonical: bytes = b"\x01" + struct.pack("<d", float("nan"))
    assert encode_value(float("nan")) == canonical
    assert encode_value(float("-nan")) == canonical


def test_infinities_are_preserved_and_distinct() -> None:
    assert encode_value(float("inf")) != encode_value(float("-inf"))
    assert encode_value(float("inf")) == b"\x01" + struct.pack("<d", float("inf"))


def test_bool_is_encoded_as_bool_not_as_int() -> None:
    # bool subclasses int, so an implementation that tests int first silently encodes
    # every boolean as an integer.
    assert encode_value(True) != encode_value(1)
    assert encode_value(False) != encode_value(0)
    assert encode_value(True)[:1] == b"\x07"


def test_datetime_is_encoded_as_datetime_not_as_date() -> None:
    # datetime subclasses date, so an implementation that tests date first silently
    # discards the time of day.
    assert encode_value(dt.datetime(2020, 1, 1, 12, 30, tzinfo=dt.UTC))[:1] == b"\x04"
    assert encode_value(dt.date(2020, 1, 1))[:1] == b"\x03"


def test_naive_datetime_is_assumed_utc() -> None:
    # calamine returns Excel datetimes naive; this is what lets a frame read back from a
    # workbook hash equal to the frame that was written.
    aware: dt.datetime = dt.datetime(2020, 1, 1, 12, 0, tzinfo=dt.UTC)
    naive: dt.datetime = aware.replace(tzinfo=None)
    assert naive.tzinfo is None
    assert encode_value(naive) == encode_value(aware)


def test_aware_datetimes_are_converted_to_utc() -> None:
    new_york: dt.datetime = dt.datetime(2020, 1, 1, 7, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York"))
    utc: dt.datetime = dt.datetime(2020, 1, 1, 12, 0, tzinfo=dt.UTC)
    assert encode_value(new_york) == encode_value(utc)


def test_temporal_payloads_use_the_documented_epochs() -> None:
    assert encode_value(dt.date(2020, 1, 1)) == b"\x03" + struct.pack("<q", (dt.date(2020, 1, 1) - dt.date(1970, 1, 1)).days)
    assert encode_value(dt.time(1, 2, 3, 4)) == b"\x05" + struct.pack("<q", ((1 * 3600 + 2 * 60 + 3) * 1_000_000 + 4) * 1000)
    assert encode_value(dt.timedelta(seconds=5)) == b"\x0a" + struct.pack("<q", 5_000_000)


def test_integers_cover_the_full_signed_and_unsigned_range() -> None:
    # UInt64's maximum does not fit a signed 8-byte pack, which is why integers are
    # encoded as their decimal text rather than packed.
    assert encode_value(2**64 - 1) == b"\x06" + b"18446744073709551615"
    assert encode_value(-(2**63)) == b"\x06" + b"-9223372036854775808"
    assert encode_value(0) != encode_value(-0.0)


def test_decimal_ignores_trailing_zeros() -> None:
    # Two decimals that are numerically equal must produce one digest.
    assert encode_value(Decimal("1.25")) == encode_value(Decimal("1.250"))
    assert encode_value(Decimal("100")) == encode_value(Decimal("1E+2"))
    assert encode_value(Decimal("1.25")) != encode_value(Decimal("1.26"))


def test_strings_and_bytes_are_encoded_verbatim_after_the_tag() -> None:
    assert encode_value("héllo") == b"\x02" + "héllo".encode()
    assert encode_value(b"\x01\x02") == b"\x08\x01\x02"


def test_unsupported_types_raise_rather_than_hashing_to_something_plausible() -> None:
    # A silent fallback would let a nested column produce a digest that looked fine.
    with pytest.raises(TypeError):
        encode_value([1, 2, 3])
    with pytest.raises(TypeError):
        encode_value({"a": 1})


# ---- precision regressions, found in review -----------------------------------------
# These pin two silent collisions: values that differ in the data hashing to one digest.
# Both were reachable with types and ranges Polars represents, not theoretical edges.


def test_high_precision_decimals_do_not_collide() -> None:
    # normalize() rounds to the ambient 28-digit context, inventing digits the data never
    # had: both of these previously encoded as ...567900.
    left: Decimal = Decimal("123456789012345678901234567890")
    right: Decimal = Decimal("123456789012345678901234567891")
    assert left != right
    assert encode_value(left) != encode_value(right)


def test_decimal_encoding_ignores_the_ambient_context() -> None:
    # A digest must not change because unrelated code touched a process-global.
    value: Decimal = Decimal("1.2345678901234567890123456789012345")
    baseline: bytes = encode_value(value)
    ctx: Context
    with localcontext() as ctx:
        ctx.prec = 50
        assert encode_value(value) == baseline
    with localcontext() as ctx:
        ctx.prec = 6
        assert encode_value(value) == baseline


def test_negative_zero_decimal_encodes_as_zero() -> None:
    # Decimal("-0") == Decimal("0"), so they are one value and must be one digest --
    # the same rule the float path applies to -0.0.
    assert encode_value(Decimal("-0")) == encode_value(Decimal("0"))
    assert encode_value(Decimal("-0.00")) == encode_value(Decimal("0"))


def test_far_future_datetimes_keep_microsecond_precision() -> None:
    # float64's mantissa runs out around 2255. Polars holds year 3000 without complaint and
    # the fixture spec calls for far-future dates, so this is reachable data.
    earlier: dt.datetime = dt.datetime(2300, 1, 1, 0, 0, 0, 1, tzinfo=dt.UTC)
    later: dt.datetime = dt.datetime(2300, 1, 1, 0, 0, 0, 2, tzinfo=dt.UTC)
    assert encode_value(earlier) != encode_value(later)


def test_datetime_payload_is_exact_integer_microseconds() -> None:
    moment: dt.datetime = dt.datetime(2300, 1, 1, 0, 0, 0, 1, tzinfo=dt.UTC)
    expected: int = (moment - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)) // dt.timedelta(microseconds=1)
    assert encode_value(moment) == b"" + struct.pack("<q", expected)
