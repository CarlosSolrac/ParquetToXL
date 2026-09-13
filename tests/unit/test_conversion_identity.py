"""Frozen tests for ``ConversionIdentity``: which conversion a metadata record describes.

A new module rather than additions to the conversion or metadata test files, because those
are frozen and this is new behaviour rather than a correction to theirs.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import polars as pl
import pytest

from parquet_to_xl.conversion.base import DataframeConversionBaseClass
from parquet_to_xl.conversion.none import DataframeConversionNone
from parquet_to_xl.conversion.to_excel import DataframeConversionToExcel
from parquet_to_xl.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash
from parquet_to_xl.metadata.builder import build_columns_metadata
from parquet_to_xl.metadata.columns import ConversionIdentity, DataframeColumnsMetadata
from parquet_to_xl.metadata.dataframe import DataframeMetadata
from parquet_to_xl.metadata.extract import extract_metadata_from_dataframe
from parquet_to_xl.paths import ZPath

if TYPE_CHECKING:
    from pathlib import Path

    from upath import UPath

FRAME: pl.DataFrame = pl.DataFrame({"n": [1, 2, 3]})


def test_a_source_frame_record_names_no_conversion() -> None:
    # The frame as it was read is not the output of any conversion, so there is nothing
    # to name. None is the answer, not a gap.
    built: DataframeColumnsMetadata = build_columns_metadata(FRAME, [])
    assert built.conversion is None


@pytest.mark.parametrize(
    ("conversion", "identifier", "version", "version_number"),
    [
        (DataframeConversionNone(), "none", "1.0", 1),
        (DataframeConversionToExcel(), "to-excel", "4.0", 4),
    ],
    ids=["none", "to-excel"],
)
def test_a_conversion_stamps_its_own_identity(conversion: DataframeConversionBaseClass, identifier: str, version: str, version_number: int) -> None:
    stamped: ConversionIdentity | None = conversion.metadata_of_converted_dataframe(FRAME, []).columns_metadata.conversion
    assert stamped == ConversionIdentity(identifier=identifier, version=version, version_number=version_number)


def test_the_identity_is_taken_from_the_class_not_repeated() -> None:
    # Bound to the ClassVars rather than to literals, so a version bump cannot leave the
    # recorded metadata claiming the old one.
    stamped: ConversionIdentity | None = DataframeConversionToExcel().metadata_of_converted_dataframe(FRAME, []).columns_metadata.conversion
    assert stamped is not None
    assert stamped.identifier == DataframeConversionToExcel.identifier
    assert stamped.version == DataframeConversionToExcel.version
    assert stamped.version_number == DataframeConversionToExcel.version_number


def test_an_extracted_record_can_be_found_by_identifier_rather_than_position(tmp_path: Path) -> None:
    # The reason the field exists. Before it, the only way to reach the ToExcel digest was
    # to index the list at the position the caller happened to pass the conversion in.
    source: pl.DataFrame = pl.DataFrame({"n": [1, 2, 3]})
    target: UPath = ZPath(str(tmp_path)) / "frame.parquet"
    source.write_parquet(str(target))

    recorded: DataframeMetadata | None = extract_metadata_from_dataframe(
        source,
        target,
        [],
        [DataFrameHasherBinaryAggregateHash()],
        [DataframeConversionNone(), DataframeConversionToExcel()],
    )
    assert recorded is not None

    found: list[DataframeColumnsMetadata] = [record for record in recorded.column_metadata_of_conversions if record.conversion is not None and record.conversion.identifier == DataframeConversionToExcel.identifier]
    assert len(found) == 1
    assert found[0] is recorded.column_metadata_of_conversions[1]
    assert recorded.source_columns_metadata.conversion is None


def test_the_identity_survives_a_json_round_trip() -> None:
    # The sidecar will persist these, so they have to survive real JSON, not just
    # model_dump() with Python objects still in it.
    original: DataframeColumnsMetadata = DataframeConversionToExcel().metadata_of_converted_dataframe(FRAME, []).columns_metadata
    reloaded: DataframeColumnsMetadata = DataframeColumnsMetadata.model_validate_json(original.model_dump_json())
    assert reloaded.conversion == original.conversion


def test_the_identity_is_frozen() -> None:
    identity: ConversionIdentity = ConversionIdentity(identifier="none", version="1.0", version_number=1)
    with pytest.raises(ValueError, match="frozen"):
        identity.identifier = "something-else"  # type: ignore[misc]
