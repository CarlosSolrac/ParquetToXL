"""Frozen tests for the metadata models.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it: everything downstream assumes the test is the specification.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from parquet_to_xl.hashing.binary_aggregate import BinaryAggregateHashedDataframe
from parquet_to_xl.metadata.column import DataframeColumnMetadata
from parquet_to_xl.metadata.columns import DataframeColumnsMetadata
from parquet_to_xl.metadata.dataframe import DataframeMetadata


def _column(**overrides: object) -> DataframeColumnMetadata:
    fields: dict[str, object] = {
        "name": "n",
        "dtype": {"kind": "int64"},
        "is_numeric": True,
        "is_float": False,
        "is_integer": True,
        "is_decimal": False,
        "is_text": False,
        "is_boolean": False,
        "hashes": [BinaryAggregateHashedDataframe(scope="column", digest_hex="a" * 32)],
        "value_count": 4,
        "null_count": 1,
    }
    fields.update(overrides)
    return DataframeColumnMetadata.model_validate(fields)


def _wrapper() -> DataframeColumnsMetadata:
    return DataframeColumnsMetadata(
        columns=[_column()],
        dataframe_hashes=[BinaryAggregateHashedDataframe(scope="dataframe", digest_hex="b" * 32)],
    )


def test_column_metadata_is_frozen() -> None:
    column: DataframeColumnMetadata = _column()
    attribute: str = "name"
    with pytest.raises(ValidationError):
        setattr(column, attribute, "other")


def test_column_metadata_round_trips() -> None:
    column: DataframeColumnMetadata = _column()
    assert DataframeColumnMetadata.model_validate(column.model_dump()) == column


def test_counts_must_be_integers() -> None:
    with pytest.raises(ValidationError):
        _column(value_count="four")


def test_columns_metadata_preserves_order_and_round_trips() -> None:
    first: DataframeColumnMetadata = _column(name="a")
    second: DataframeColumnMetadata = _column(name="b")
    wrapper: DataframeColumnsMetadata = DataframeColumnsMetadata(columns=[first, second], dataframe_hashes=[])
    assert [column.name for column in wrapper.columns] == ["a", "b"]
    assert DataframeColumnsMetadata.model_validate(wrapper.model_dump()) == wrapper


def test_dataframe_metadata_composes_and_round_trips() -> None:
    moment: dt.datetime = dt.datetime(2020, 1, 1, 12, 0, tzinfo=dt.UTC)
    metadata: DataframeMetadata = DataframeMetadata(
        file_name="a.parquet",
        full_path="/data/a.parquet",
        modified_utc=moment,
        modified_in_timezones={"UTC": moment},
        source_columns_metadata=_wrapper(),
        column_metadata_of_conversions=[_wrapper(), _wrapper()],
    )
    assert metadata.modified_utc.tzinfo is not None
    assert len(metadata.column_metadata_of_conversions) == 2
    assert DataframeMetadata.model_validate(metadata.model_dump()) == metadata


def test_dataframe_metadata_is_frozen() -> None:
    moment: dt.datetime = dt.datetime(2020, 1, 1, 12, 0, tzinfo=dt.UTC)
    metadata: DataframeMetadata = DataframeMetadata(
        file_name="a.parquet",
        full_path="/data/a.parquet",
        modified_utc=moment,
        modified_in_timezones={},
        source_columns_metadata=_wrapper(),
        column_metadata_of_conversions=[],
    )
    attribute: str = "file_name"
    with pytest.raises(ValidationError):
        setattr(metadata, attribute, "b.parquet")
