"""Frozen tests for ``DataframeConversionNone`` and the conversion base class.

The base class is exercised here rather than in a module of its own: ``None`` is the
identity conversion, so it is the vehicle that shows the template method wiring without a
cast table getting in the way.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import ClassVar

import polars as pl
import pytest

from parquet_to_xl.conversion.base import ConvertedDataframe, DataframeConversionBaseClass
from parquet_to_xl.conversion.none import DataframeConversionNone
from parquet_to_xl.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash


def _frame() -> pl.DataFrame:
    return pl.DataFrame({"i": [3, 1, None], "s": ["b", None, "a"]}, schema={"i": pl.Int64, "s": pl.String})


def test_the_base_class_cannot_be_instantiated() -> None:
    assert inspect.isabstract(DataframeConversionBaseClass)


def test_converted_dataframe_is_frozen() -> None:
    result: ConvertedDataframe = DataframeConversionNone().metadata_of_converted_dataframe(_frame(), [])
    assert dataclasses.is_dataclass(result)
    # Written through setattr with the name in a variable. A direct assignment is a static
    # error as well as a runtime one, so expressing it inline would need a suppression, and
    # a literal name here would trip B010.
    field_name: str = "schema_or_data_changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(result, field_name, True)


def test_none_returns_the_frame_unchanged() -> None:
    df: pl.DataFrame = _frame()
    result: ConvertedDataframe = DataframeConversionNone().metadata_of_converted_dataframe(df, [])
    assert result.converted_dataframe.equals(df)
    assert result.schema_or_data_changed is False


def test_none_reports_no_change_through_the_public_method() -> None:
    result: ConvertedDataframe = DataframeConversionNone().metadata_of_converted_dataframe(_frame(), [])
    assert result.schema_or_data_changed is False
    assert result.converted_dataframe.equals(_frame())


def test_metadata_mirrors_the_input_columns() -> None:
    df: pl.DataFrame = _frame()
    result: ConvertedDataframe = DataframeConversionNone().metadata_of_converted_dataframe(df, [])
    assert [column.name for column in result.columns_metadata.columns] == df.columns
    assert [column.polars_dtype for column in result.columns_metadata.columns] == [str(dtype) for dtype in df.dtypes]


def test_metadata_is_built_from_the_converted_frame_not_the_input() -> None:
    # The base class owns this wiring so a subclass cannot get it wrong. A conversion that
    # changes dtypes must have its metadata describe the result, which is what makes a
    # recorded digest comparable to one taken from the written file.
    class _WidenToFloat(DataframeConversionBaseClass):
        identifier: ClassVar[str] = "test-widen"
        version: ClassVar[str] = "1.0"
        version_number: ClassVar[int] = 1
        description: ClassVar[str] = "Casts every column to Float64."

        def _convert(self, df: pl.DataFrame) -> tuple[pl.DataFrame, bool]:
            return df.select(pl.col("i").cast(pl.Float64)), True

    result: ConvertedDataframe = _WidenToFloat().metadata_of_converted_dataframe(_frame(), [])
    assert [column.polars_dtype for column in result.columns_metadata.columns] == ["Float64"]
    assert result.schema_or_data_changed is True


def test_one_hash_per_hasher_reaches_the_metadata() -> None:
    result: ConvertedDataframe = DataframeConversionNone().metadata_of_converted_dataframe(_frame(), [DataFrameHasherBinaryAggregateHash()])
    assert len(result.columns_metadata.dataframe_hashes) == 1
    assert all(len(column.hashes) == 1 for column in result.columns_metadata.columns)


def test_the_conversion_identifies_itself() -> None:
    assert DataframeConversionNone.identifier == "none"
    assert DataframeConversionNone.version_number == 1
    assert DataframeConversionNone.description
