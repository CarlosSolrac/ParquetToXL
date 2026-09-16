"""Frozen tests for conversion being independent of the order the rows arrive in.

Its own module because it is its own ticket. ``test_conversion_idempotency.py`` checks what a
*second* conversion does to a frame; this checks what the *first* one does to a frame whose rows
have been moved, which is a different question and does not fall out of that one.

Why it is load-bearing: the pipeline converts a source for Excel and arranges its rows into
calendar buckets, and those two steps can run in either order. Converting first is worth a whole
pass of a forty-million-row frame, because the conversion already happened once while the sidecar
was being written. That saving is only available if moving a row cannot change what conversion
makes of it -- and the manifest's fragment digests are taken over *slices* of the arranged frame,
so if it could, every published digest would shift and ``pqx verify`` would reject exports that
are perfectly good.

Conversion is per value and per column, so the property is expected to hold. It is asserted rather
than assumed because the cost of it being false is silent, and lands on already-published data.

Frames are compared through ``_same_values`` rather than ``DataFrame.equals`` or ``to_dicts()``
for the reasons ``test_conversion_idempotency.py`` sets out: ``equals`` is ``False`` for an
``Object`` column even against itself, and ``to_dicts()`` equality fails on any frame holding a
NaN. The frame below carries one on purpose.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest
from pqx_frame.conversion.base import ConvertedDataframe
from pqx_frame.conversion.to_excel import EXCEL_CELL_LIMIT, DataframeConversionToExcel
from pqx_frame.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash

PERMUTATION: list[int] = [3, 0, 2, 1]
"""A reordering that moves every row, so no position is left to pass by accident."""


def _frame() -> pl.DataFrame:
    """Four rows over every scalar dtype, carrying the values that move under conversion."""
    return pl.DataFrame(
        {
            "int64": pl.Series([-9223372036854775808, 0, 9223372036854775807, None], dtype=pl.Int64),
            "uint64": pl.Series([0, 1, 18446744073709551615, None], dtype=pl.UInt64),
            "float64": pl.Series([float("inf"), float("nan"), -0.0, None], dtype=pl.Float64),
            "boolean": pl.Series([True, False, True, None], dtype=pl.Boolean),
            "string": pl.Series(["x" * (EXCEL_CELL_LIMIT + 7), "", "plain", None], dtype=pl.String),
            "binary": pl.Series([b"\xff\xfe", b"", b"\x00", None], dtype=pl.Binary),
            "date": pl.Series([dt.date(1899, 12, 31), dt.date(2025, 6, 1), dt.date(9999, 12, 31), None], dtype=pl.Date),
            "time": pl.Series([dt.time(0, 0), dt.time(12, 0), dt.time(23, 59, 59, 999999), None], dtype=pl.Time),
            "datetime": pl.Series([dt.datetime(1970, 1, 1, tzinfo=dt.UTC), dt.datetime(2025, 6, 1, tzinfo=dt.UTC), dt.datetime(2262, 4, 11, tzinfo=dt.UTC), None], dtype=pl.Datetime("us", "UTC")),
            "duration": pl.Series([dt.timedelta(microseconds=1), dt.timedelta(0), dt.timedelta(days=-100000), None], dtype=pl.Duration("us")),
            "decimal": pl.Series([Decimal("1.2500"), Decimal("0.0000"), Decimal("-9.9900"), None], dtype=pl.Decimal(18, 4)),
            "categorical": pl.Series(["alpha", "", "beta", None], dtype=pl.Categorical),
            "null": pl.Series([None, None, None, None], dtype=pl.Null),
        },
    )


def _convert(df: pl.DataFrame) -> pl.DataFrame:
    result: ConvertedDataframe = DataframeConversionToExcel().metadata_of_converted_dataframe(df, [])
    return result.converted_dataframe


def _digest(df: pl.DataFrame) -> str:
    return DataFrameHasherBinaryAggregateHash().hash_dataframe(df).digest_hex


def _same_values(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    """Compare two frames cell by cell, treating NaN as equal to itself."""
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


def test_reordering_before_conversion_gives_what_reordering_after_it_gives() -> None:
    source: pl.DataFrame = _frame()
    converted_then_moved: pl.DataFrame = _convert(source)[PERMUTATION]
    moved_then_converted: pl.DataFrame = _convert(source[PERMUTATION])
    assert moved_then_converted.schema == converted_then_moved.schema
    assert _same_values(moved_then_converted, converted_then_moved)


def test_the_digest_of_the_whole_frame_survives_the_two_steps_swapping_places() -> None:
    # Stronger than value equality and closer to what the manifest records.
    source: pl.DataFrame = _frame()
    assert _digest(_convert(source)[PERMUTATION]) == _digest(_convert(source[PERMUTATION]))


@pytest.mark.parametrize(("offset", "length"), [(0, 1), (0, 2), (1, 2), (2, 2), (3, 1)])
def test_the_digest_of_a_slice_survives_the_two_steps_swapping_places(offset: int, length: int) -> None:
    # The property the manifest actually rests on: a fragment is a slice of the arranged frame,
    # and its digest is published. If a slice digest moved, verification would reject good exports.
    source: pl.DataFrame = _frame()
    assert _digest(_convert(source)[PERMUTATION].slice(offset, length)) == _digest(_convert(source[PERMUTATION]).slice(offset, length))
