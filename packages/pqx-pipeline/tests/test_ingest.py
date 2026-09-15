"""Frozen tests for reading a staged Parquet file and describing it.

The headline one is ``test_the_original_path_supplies_the_modification_time``: the spec calls this
trap out by name and asks for a test, because falling into it fails silently and permanently.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import polars as pl
import pytest
from pqx_common.paths import ZPath
from pqx_frame.metadata.dataframe import DataframeMetadata
from pqx_pipeline.ingest import IngestError, Observation, build_sidecar, observe, read_parquet, source_stem
from pqx_sidecar.store import get_sidecar_store, sidecar_path

if TYPE_CHECKING:
    from pathlib import Path

    from upath import UPath

CREATED: dt.datetime = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)


def _frame(rows: int = 6) -> pl.DataFrame:
    """A small frame of the dtypes an export carries."""
    return pl.DataFrame(
        {
            "id": pl.Series(list(range(rows)), dtype=pl.Int64),
            "name": pl.Series([f"row {index}" for index in range(rows)], dtype=pl.String),
            "booked": pl.Series([dt.date(2025, 1, 1) + dt.timedelta(days=index) for index in range(rows)], dtype=pl.Date),
        },
    )


def _written(tmp_path: Path, name: str, rows: int = 6) -> UPath:
    """A real Parquet file on disk."""
    path: UPath = ZPath(str(tmp_path / name))
    _frame(rows).write_parquet(str(path))
    return path


@pytest.mark.parametrize(("name", "expected"), [("sales.parquet", "sales"), ("sales.2025.parquet", "sales.2025"), ("sales", "sales"), ("sales.parquet.bak", "sales.parquet.bak")])
def test_only_the_final_parquet_extension_is_stripped(name: str, expected: str, tmp_path: Path) -> None:
    assert source_stem(ZPath(str(tmp_path / name))) == expected


def test_a_parquet_file_reads_back(tmp_path: Path) -> None:
    source: UPath = _written(tmp_path, "sales.parquet")
    assert read_parquet(source).equals(_frame())


def test_a_file_that_is_not_parquet_is_refused_by_name(tmp_path: Path) -> None:
    broken: UPath = ZPath(str(tmp_path / "sales.parquet"))
    broken.write_bytes(b"not parquet at all")
    with pytest.raises(IngestError, match="could not read"):
        read_parquet(broken)


def test_a_missing_file_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="could not read"):
        read_parquet(ZPath(str(tmp_path / "absent.parquet")))


def test_describing_a_frame_writes_a_sidecar_beside_its_source(tmp_path: Path) -> None:
    source: UPath = _written(tmp_path, "sales.parquet")
    build_sidecar(read_parquet(source), original_path=source, created_utc=CREATED)
    assert sidecar_path(source).exists()
    assert get_sidecar_store("json").read(source).created_utc == CREATED


def test_the_original_path_supplies_the_modification_time(tmp_path: Path) -> None:
    # The trap, and the reason build_sidecar takes both paths as separate required arguments.
    # extract_metadata_from_dataframe stats whatever path it is handed and never checks it against
    # the frame. Hand it the scratch copy and T1 silently becomes the stage-in time: nothing fails,
    # nothing warns, and every later run compares the source's real mtime against a staging time
    # that will never match, so the source is stale forever while looking perfectly healthy.
    source: UPath = _written(tmp_path, "sales.parquet")
    staged: UPath = ZPath(str(tmp_path / "scratch" / "sales.parquet"))
    staged.parent.mkdir(parents=True)
    staged.write_bytes(source.read_bytes())
    # Make the two mtimes unmistakably different, the way staging a week-old source does.
    import os

    old: float = source.stat().st_mtime - 7 * 24 * 3600
    os.utime(str(source), (old, old))

    recorded: DataframeMetadata = build_sidecar(read_parquet(staged), original_path=source, created_utc=CREATED)
    assert recorded.modified_utc == dt.datetime.fromtimestamp(old, tz=dt.UTC)
    assert recorded.modified_utc != dt.datetime.fromtimestamp(staged.stat().st_mtime, tz=dt.UTC)
    assert str(source) in str(recorded.full_path)
    assert "scratch" not in str(recorded.full_path)


def test_extraction_returning_nothing_becomes_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # extract_metadata_from_dataframe returns None rather than raising, because it is a per-file
    # boundary for batch jobs. Here the run IS the file, so a None must not become a silently
    # empty description.
    import pqx_pipeline.ingest as ingest_module

    def describe_nothing(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        """Stand in for an extraction that logged its reason and gave up."""
        return

    monkeypatch.setattr(ingest_module, "extract_metadata_from_dataframe", describe_nothing)
    source: UPath = _written(tmp_path, "sales.parquet")
    with pytest.raises(IngestError, match="could not describe"):
        build_sidecar(_frame(), original_path=source, created_utc=CREATED)


def test_observing_measures_the_converted_frame(tmp_path: Path) -> None:
    # C_s is the exported column count, so measuring the unconverted frame would size every sheet
    # against a shape that is never written.
    source: UPath = _written(tmp_path, "sales.parquet", rows=9)
    frame: pl.DataFrame = read_parquet(source)
    recorded: DataframeMetadata = build_sidecar(frame, original_path=source, created_utc=CREATED)
    seen: Observation = observe("sales", frame, original_path=source, metadata=recorded)
    assert seen.shape.alias == "sales"
    assert seen.shape.stem == "sales"
    assert seen.shape.rows == 9
    assert seen.shape.columns == seen.frame.width
    assert seen.frame.height == 9


def test_an_observation_carries_the_frame_the_digest_will_be_taken_over(tmp_path: Path) -> None:
    # Converted here, before planning, so the digest recorded per fragment is taken over a frame
    # already in memory rather than by converting twice.
    from pqx_frame.conversion.to_excel import DataframeConversionToExcel

    source: UPath = _written(tmp_path, "sales.parquet")
    frame: pl.DataFrame = read_parquet(source)
    recorded: DataframeMetadata = build_sidecar(frame, original_path=source, created_utc=CREATED)
    expected: pl.DataFrame = DataframeConversionToExcel().metadata_of_converted_dataframe(frame, []).converted_dataframe
    assert observe("sales", frame, original_path=source, metadata=recorded).frame.equals(expected)


def test_an_empty_source_observes_as_zero_rows(tmp_path: Path) -> None:
    source: UPath = _written(tmp_path, "sales.parquet", rows=0)
    frame: pl.DataFrame = read_parquet(source)
    recorded: DataframeMetadata = build_sidecar(frame, original_path=source, created_utc=CREATED)
    seen: Observation = observe("sales", frame, original_path=source, metadata=recorded)
    assert seen.shape.rows == 0
    assert seen.shape.is_empty
    assert seen.shape.columns > 0


def test_a_sidecar_can_be_written_somewhere_other_than_beside_its_source(tmp_path: Path) -> None:
    # sidecar_location: directory. What is described and where the description goes are separate:
    # deriving the location from whatever path was described would mean describing the sidecar's
    # own directory to move it there.
    source: UPath = _written(tmp_path, "sales.parquet")
    elsewhere: UPath = ZPath(str(tmp_path / "sidecars"))
    elsewhere.mkdir(parents=True)
    recorded: DataframeMetadata = build_sidecar(read_parquet(source), original_path=source, created_utc=CREATED, sidecar_directory=elsewhere)
    assert sidecar_path(elsewhere / "sales.parquet").exists()
    assert not sidecar_path(source).exists()
    # Still describing the source, not the place the sidecar landed.
    assert str(source) in str(recorded.full_path)
    assert "sidecars" not in str(recorded.full_path)
