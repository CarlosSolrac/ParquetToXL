"""Frozen tests for ``ZPath``.

The spec's assertions for this unit are: ``ZPath`` is a ``UPath``, a string round-trips,
``storage_options`` are accepted and stored, and it forwards to UPath without breaking
local-filesystem operations.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

from pathlib import Path

from upath import UPath

from parquet_to_xl.paths import ZPath


def test_a_constructed_path_is_a_upath() -> None:
    made: UPath = ZPath("memory://bucket/file.txt")
    assert isinstance(made, UPath)


def test_a_local_path_round_trips_through_str(tmp_path: Path) -> None:
    original: UPath = ZPath(str(tmp_path / "a.parquet"))
    again: UPath = ZPath(str(original))
    assert str(again) == str(original)


def test_storage_options_are_accepted_and_stored() -> None:
    # The whole reason the seam exists. UPath 0.3.10 carries these natively, which is why
    # ZPath does not need to intercept them.
    made: UPath = ZPath("memory://bucket/file.txt", anon=True)
    assert dict(made.storage_options) == {"anon": True}


def test_no_storage_options_yields_an_empty_mapping() -> None:
    made: UPath = ZPath("memory://bucket/file.txt")
    assert dict(made.storage_options) == {}


def test_local_filesystem_operations_still_work(tmp_path: Path) -> None:
    target: UPath = ZPath(str(tmp_path / "written.txt"))
    target.write_text("content")
    assert target.exists()
    assert target.read_text() == "content"
    assert target.name == "written.txt"


def test_stat_exposes_a_modification_time(tmp_path: Path) -> None:
    # extract_metadata_from_dataframe reads st_mtime off this, so it is part of the contract.
    target: UPath = ZPath(str(tmp_path / "stamped.txt"))
    target.write_text("x")
    assert isinstance(target.stat().st_mtime, float)


def test_joining_and_parents_keep_working(tmp_path: Path) -> None:
    base: UPath = ZPath(str(tmp_path))
    joined: UPath = base / "child.parquet"
    assert joined.name == "child.parquet"
    assert str(joined.parent) == str(base)


def test_a_non_local_protocol_is_dispatched_by_upath() -> None:
    # The registry picks the concrete class per protocol; ZPath must not interfere with it.
    made: UPath = ZPath("memory://bucket/file.txt")
    assert made.protocol == "memory"
