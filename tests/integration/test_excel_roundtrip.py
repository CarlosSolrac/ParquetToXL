"""The round trip, end to end, over every scalar dtype at once.

The unit tests check one cast or one writer behaviour at a time. This checks the property
those exist to support: convert a real frame, write it, read it back, and get the same
digest. It is the strongest statement this project makes and the reason the conversion
models losses rather than merely reshaping dtypes.

Not yet the headline test the spec describes. That one additionally splits a frame across
two 500-row workbooks, reads them back in the wrong order and concatenates, and is a phase 7
unit. This covers the single-workbook half of it.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest

from parquet_to_xl.conversion.to_excel import DataframeConversionToExcel
from parquet_to_xl.excel.writer import ExcelWriteConfig, get_excel_writer
from parquet_to_xl.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash
from parquet_to_xl.paths import ZPath

if TYPE_CHECKING:
    from pathlib import Path

    from upath import UPath

FIXTURE_STEMS: list[str] = ["parquet_a", "parquet_b"]


def _converted(source_path: Path) -> pl.DataFrame:
    source: pl.DataFrame = pl.read_parquet(source_path)
    return DataframeConversionToExcel().metadata_of_converted_dataframe(source, []).converted_dataframe


def _write_and_read(frame: pl.DataFrame, target: UPath) -> pl.DataFrame:
    """Write with the configured default writer, read back with the frame's own schema."""
    get_excel_writer(ExcelWriteConfig().writer).write(frame, target, {})
    return pl.read_excel(str(target), schema_overrides=dict(frame.schema), raise_if_empty=False)


def _digest(frame: pl.DataFrame) -> str:
    return DataFrameHasherBinaryAggregateHash().hash_dataframe(frame).digest_hex


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
        return True
    try:
        return bool(left == right)
    except Exception:
        return False


@pytest.mark.parametrize("stem", FIXTURE_STEMS)
def test_the_digest_survives_a_real_workbook(stem: str, fixture_files: dict[str, Path], tmp_path: Path) -> None:
    # The whole point. If this fails, the conversion is not modelling what the file does.
    frame: pl.DataFrame = _converted(fixture_files[stem])
    target: UPath = ZPath(str(tmp_path / f"{stem}.xlsx"))
    assert _digest(_write_and_read(frame, target)) == _digest(frame)


@pytest.mark.parametrize("stem", FIXTURE_STEMS)
def test_every_column_returns_with_its_dtype_and_values(stem: str, fixture_files: dict[str, Path], tmp_path: Path) -> None:
    frame: pl.DataFrame = _converted(fixture_files[stem])
    target: UPath = ZPath(str(tmp_path / f"{stem}-values.xlsx"))
    back: pl.DataFrame = _write_and_read(frame, target)
    assert back.shape == frame.shape
    assert back.columns == frame.columns
    assert dict(back.schema) == dict(frame.schema)
    name: str
    for name in frame.columns:
        want: list[Any] = frame[name].to_list()
        got: list[Any] = back[name].to_list()
        differing: list[int] = [index for index in range(len(want)) if not _same(want[index], got[index])]
        assert not differing, f"{name}: {len(differing)} cells differ, first at row {differing[0]}"


def test_the_fixture_frames_are_distinguishable_after_a_round_trip(fixture_files: dict[str, Path], tmp_path: Path) -> None:
    # parquet_b differs from parquet_a by one cell per column. A digest that survives the
    # trip but cannot tell them apart would be worthless, so both halves are asserted.
    digests: list[str] = []
    stem: str
    for stem in FIXTURE_STEMS:
        frame: pl.DataFrame = _converted(fixture_files[stem])
        digests.append(_digest(_write_and_read(frame, ZPath(str(tmp_path / f"{stem}-distinct.xlsx")))))
    assert digests[0] != digests[1]


@pytest.mark.parametrize("stem", FIXTURE_STEMS)
def test_the_conversion_settles_before_the_workbook_is_written(stem: str, fixture_files: dict[str, Path], tmp_path: Path) -> None:
    # The headline test runs both operands through _convert, so a frame already converted
    # must convert to itself. Asserted here on real data rather than a constructed frame.
    conversion: DataframeConversionToExcel = DataframeConversionToExcel()
    once: pl.DataFrame = _converted(fixture_files[stem])
    twice: pl.DataFrame = conversion.metadata_of_converted_dataframe(once, []).converted_dataframe
    assert _digest(twice) == _digest(once)
    assert conversion.metadata_of_converted_dataframe(once, []).schema_or_data_changed is False
    target: UPath = ZPath(str(tmp_path / f"{stem}-settled.xlsx"))
    assert _digest(_write_and_read(twice, target)) == _digest(once)
