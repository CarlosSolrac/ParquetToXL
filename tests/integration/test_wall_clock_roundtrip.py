"""Excel conversion preserves local clock readings, including DST folds."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from upath import UPath

from parquet_to_xl.conversion.base import ConvertedDataframe
from parquet_to_xl.conversion.to_excel import DataframeConversionToExcel
from parquet_to_xl.excel.writer import RustpyExcelWriter
from parquet_to_xl.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash
from parquet_to_xl.paths import zpath


@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Asia/Kolkata"])
def test_timezone_is_removed_without_shifting_wall_clock(zone: str, tmp_path: Path) -> None:
    values: list[dt.datetime | None] = [
        dt.datetime(2026, 11, 1, 1, 30, 15, 123456, tzinfo=ZoneInfo(zone), fold=0),
        dt.datetime(2026, 11, 1, 1, 30, 15, 123456, tzinfo=ZoneInfo(zone), fold=1),
        None,
    ]
    source: pl.DataFrame = pl.DataFrame({"time": values, "id": [1.0, 2.0, 3.0]})
    converter: DataframeConversionToExcel = DataframeConversionToExcel()
    result: ConvertedDataframe = converter.metadata_of_converted_dataframe(source, [])
    expected: list[dt.datetime | None] = [value.replace(tzinfo=None, microsecond=0) if value is not None else None for value in values]
    assert result.converted_dataframe["time"].dtype == pl.Datetime("us")
    assert result.converted_dataframe["time"].to_list() == expected
    assert result.schema_or_data_changed
    again: ConvertedDataframe = converter.metadata_of_converted_dataframe(result.converted_dataframe, [])
    assert not again.schema_or_data_changed
    assert again.converted_dataframe.equals(result.converted_dataframe)
    assert source["time"].to_list() == values
    target: UPath = zpath(tmp_path / "wall-clock.xlsx")
    RustpyExcelWriter().write(result.converted_dataframe, target, {})
    back: pl.DataFrame = pl.read_excel(str(target), schema_overrides=dict(result.converted_dataframe.schema))
    assert back["time"].to_list() == expected
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    assert hasher.hash_dataframe(back) == hasher.hash_dataframe(result.converted_dataframe)
