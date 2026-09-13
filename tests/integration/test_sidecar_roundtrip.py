"""A metadata record survives being written to a sidecar and read back.

The point is narrower than it looks. The record that matters is the ToExcel one: it carries
the digest a workbook written from this Parquet file must reproduce, and
``test_excel_hash_roundtrip.py`` proves that digest is correct *in memory*. This module
proves the file boundary does not damage it, which is what makes the digest usable at all --
a digest that has to be recomputed from the data every time compares nothing.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import polars as pl
import pytest

from parquet_to_xl.conversion.none import DataframeConversionNone
from parquet_to_xl.conversion.to_excel import DataframeConversionToExcel
from parquet_to_xl.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash
from parquet_to_xl.metadata.extract import extract_metadata_from_dataframe
from parquet_to_xl.paths import ZPath
from parquet_to_xl.sidecar.store import get_sidecar_store, sidecar_path

if TYPE_CHECKING:
    from pathlib import Path

    from upath import UPath

    from parquet_to_xl.metadata.columns import DataframeColumnsMetadata
    from parquet_to_xl.metadata.dataframe import DataframeMetadata
    from parquet_to_xl.sidecar.store import SidecarStoreBase

FIXTURE_STEMS: list[str] = ["parquet_a", "parquet_b"]

TIMEZONES: list[str] = ["UTC", "America/Chicago", "Asia/Kolkata"]
"""Three zones, one of them UTC, so the per-zone mapping is exercised rather than trivial."""


def _recorded(path: UPath) -> DataframeMetadata:
    frame: pl.DataFrame = pl.read_parquet(str(path))
    metadata: DataframeMetadata | None = extract_metadata_from_dataframe(
        frame,
        path,
        TIMEZONES,
        [DataFrameHasherBinaryAggregateHash()],
        [DataframeConversionNone(), DataframeConversionToExcel()],
    )
    assert metadata is not None
    return metadata


def _to_excel_record(metadata: DataframeMetadata) -> DataframeColumnsMetadata:
    """Find the ToExcel record by identifier, which is the reason ConversionIdentity exists."""
    found: list[DataframeColumnsMetadata] = [record for record in metadata.column_metadata_of_conversions if record.conversion is not None and record.conversion.identifier == DataframeConversionToExcel.identifier]
    assert len(found) == 1
    return found[0]


@pytest.mark.parametrize("stem", FIXTURE_STEMS)
def test_a_real_record_reloads_exactly(stem: str, fixture_files: dict[str, Path], tmp_path: Path) -> None:
    # Exact equality over a record built from 19 dtypes and 1000 rows, not a hand-made one.
    # This is only assertable because the column extremes were removed: while min_value and
    # max_value were typed as a union containing str, binary and temporal values reloaded as
    # strings and non-UTF-8 bytes did not serialise at all.
    source: UPath = ZPath(str(fixture_files[stem]))
    original: DataframeMetadata = _recorded(source)

    target: UPath = ZPath(str(tmp_path / source.name))
    store: SidecarStoreBase = get_sidecar_store("json")
    store.write(original, target)

    assert store.read(target).metadata == original


@pytest.mark.parametrize("stem", FIXTURE_STEMS)
def test_the_excel_digest_survives_the_file_boundary(stem: str, fixture_files: dict[str, Path], tmp_path: Path) -> None:
    source: UPath = ZPath(str(fixture_files[stem]))
    original: DataframeMetadata = _recorded(source)
    target: UPath = ZPath(str(tmp_path / source.name))
    get_sidecar_store("json").write(original, target)

    reloaded: DataframeMetadata = get_sidecar_store("json").read(target).metadata
    before: DataframeColumnsMetadata = _to_excel_record(original)
    after: DataframeColumnsMetadata = _to_excel_record(reloaded)

    assert after.dataframe_hashes == before.dataframe_hashes
    assert after.dataframe_hashes[0].digest_hex == before.dataframe_hashes[0].digest_hex
    # Per-column digests too, since the sidecar is also how a single changed column is found.
    assert [record.hashes for record in after.columns] == [record.hashes for record in before.columns]
    assert after.conversion is not None
    assert after.conversion.version == DataframeConversionToExcel.version


def test_the_two_fixtures_produce_different_sidecars(fixture_files: dict[str, Path], tmp_path: Path) -> None:
    # parquet_b differs from parquet_a by exactly one cell per column, so if the sidecars
    # matched, the digests they carry would not be detecting anything.
    texts: list[str] = []
    stem: str
    for stem in FIXTURE_STEMS:
        source: UPath = ZPath(str(fixture_files[stem]))
        target: UPath = ZPath(str(tmp_path / f"{stem}.parquet"))
        get_sidecar_store("json").write(_recorded(source), target)
        texts.append(sidecar_path(target).read_text(encoding="utf-8"))
    assert texts[0] != texts[1]


def test_the_modification_time_reloads_aware_and_names_the_same_instant(fixture_files: dict[str, Path], tmp_path: Path) -> None:
    # Measured: a per-zone datetime reloads carrying a fixed offset rather than the ZoneInfo
    # it was built with, so the tzinfo objects are not equal even though the instants are.
    # Instants are what the field means, and what model equality compares.
    source: UPath = ZPath(str(fixture_files["parquet_a"]))
    original: DataframeMetadata = _recorded(source)
    target: UPath = ZPath(str(tmp_path / source.name))
    get_sidecar_store("json").write(original, target)
    reloaded: DataframeMetadata = get_sidecar_store("json").read(target).metadata

    assert reloaded.modified_utc.tzinfo is not None
    assert reloaded.modified_utc == original.modified_utc
    assert sorted(reloaded.modified_in_timezones) == sorted(TIMEZONES)
    name: str
    for name in TIMEZONES:
        assert reloaded.modified_in_timezones[name] == original.modified_in_timezones[name]
