"""Frozen tests for staging in, publishing, listing and deleting.

Run against a real temporary directory and against ``memory://``, which is what
``export-pipeline-spec.md`` asks for: the error branches are reached by injecting failures rather
than by finding a filesystem that produces them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pqx_common.paths import ZPath
from pqx_staging.errors import TransferError
from pqx_staging.transfer import copy_file, delete_names, list_names, publish_files, stage_sources

if TYPE_CHECKING:
    from pathlib import Path

    from upath import UPath


@pytest.fixture
def remote() -> UPath:
    """A clean in-memory store, standing in for the source side without touching real storage."""
    root: UPath = ZPath("memory://sources")
    if root.exists():
        entry: UPath
        for entry in root.iterdir():
            entry.unlink()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _remote_file(root: UPath, name: str, payload: bytes) -> UPath:
    """A file in the in-memory store."""
    path: UPath = root / name
    path.write_bytes(payload)
    return path


# --------------------------------------------------------------------------------------
# copy_file
# --------------------------------------------------------------------------------------


def test_a_copy_reproduces_the_bytes(remote: UPath, tmp_path: Path) -> None:
    source: UPath = _remote_file(remote, "sales.parquet", b"payload" * 1000)
    target: UPath = copy_file(source, ZPath(str(tmp_path / "sales.parquet")))
    assert target.read_bytes() == source.read_bytes()


def test_a_copy_creates_the_parent_directory(remote: UPath, tmp_path: Path) -> None:
    source: UPath = _remote_file(remote, "sales.parquet", b"x")
    target: UPath = copy_file(source, ZPath(str(tmp_path / "nested" / "deeper" / "sales.parquet")))
    assert target.exists()


def test_a_copy_larger_than_one_chunk_is_streamed_whole(remote: UPath, tmp_path: Path) -> None:
    # Streamed rather than read into memory: a staged Parquet is the largest thing this project
    # touches, and on a tmpfs scratch root reading it whole competes with the export itself.
    payload: bytes = bytes(range(256)) * 40_000
    source: UPath = _remote_file(remote, "big.parquet", payload)
    assert copy_file(source, ZPath(str(tmp_path / "big.parquet"))).read_bytes() == payload


def test_a_failed_copy_leaves_nothing_behind(remote: UPath, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A partial file is worse than no file: readable, plausibly sized, and reconciliation keeps it
    # because the manifest names it.
    import shutil as shutil_module

    source: UPath = _remote_file(remote, "sales.parquet", b"payload")
    target: UPath = ZPath(str(tmp_path / "sales.parquet"))

    def fail_midway(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        """Write something, then fail, the way a truncated transfer does."""
        target.write_bytes(b"par")
        message: str = "connection reset"
        raise OSError(message)

    monkeypatch.setattr(shutil_module, "copyfileobj", fail_midway)
    with pytest.raises(TransferError, match="could not copy"):
        copy_file(source, target)
    assert not target.exists()


def test_a_failed_copy_whose_cleanup_also_fails_still_reports_the_copy(remote: UPath, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The cleanup must not replace the error that explains why the copy failed.
    import shutil as shutil_module

    source: UPath = _remote_file(remote, "sales.parquet", b"payload")
    target: UPath = ZPath(str(tmp_path / "sales.parquet"))

    def fail(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        """Fail the transfer."""
        message: str = "connection reset"
        raise OSError(message)

    def refuse_unlink(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        """Fail the cleanup too."""
        message: str = "permission denied"
        raise OSError(message)

    monkeypatch.setattr(shutil_module, "copyfileobj", fail)
    monkeypatch.setattr(type(target), "unlink", refuse_unlink)
    with pytest.raises(TransferError, match="connection reset"):
        copy_file(source, target)


def test_a_missing_source_is_reported_as_a_transfer_failure(tmp_path: Path) -> None:
    with pytest.raises(TransferError, match="could not copy"):
        copy_file(ZPath(str(tmp_path / "absent.parquet")), ZPath(str(tmp_path / "out.parquet")))


# --------------------------------------------------------------------------------------
# stage_sources
# --------------------------------------------------------------------------------------


def test_every_source_is_staged(remote: UPath, tmp_path: Path) -> None:
    sources: list[UPath] = [_remote_file(remote, "sales.parquet", b"a"), _remote_file(remote, "returns.parquet", b"bb")]
    staged: dict[UPath, UPath] = stage_sources(sources, ZPath(str(tmp_path)))
    assert sorted(path.name for path in staged.values()) == ["returns.parquet", "sales.parquet"]
    assert all(path.exists() for path in staged.values())


def test_a_source_named_twice_is_staged_once(remote: UPath, tmp_path: Path) -> None:
    # Two profiles naming the same source stage it once and share it.
    source: UPath = _remote_file(remote, "sales.parquet", b"a")
    staged: dict[UPath, UPath] = stage_sources([source, source, source], ZPath(str(tmp_path)))
    assert len(staged) == 1


def test_two_sources_sharing_a_basename_are_refused(remote: UPath, tmp_path: Path) -> None:
    # Silently renaming one would make the staged copy's name disagree with the manifest's
    # record of it.
    other: UPath = ZPath("memory://elsewhere")
    other.mkdir(parents=True, exist_ok=True)
    first: UPath = _remote_file(remote, "sales.parquet", b"a")
    second: UPath = _remote_file(other, "sales.parquet", b"b")
    with pytest.raises(TransferError, match="share the basename"):
        stage_sources([first, second], ZPath(str(tmp_path)))


def test_staging_nothing_stages_nothing(tmp_path: Path) -> None:
    assert stage_sources([], ZPath(str(tmp_path))) == {}


# --------------------------------------------------------------------------------------
# publish, list, delete
# --------------------------------------------------------------------------------------


def test_publishing_writes_each_file_under_the_name_given(tmp_path: Path) -> None:
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    scratch.mkdir(parents=True)
    (scratch / "a.xlsx").write_bytes(b"one")
    (scratch / "b.xlsx").write_bytes(b"two")
    destination: UPath = ZPath(str(tmp_path / "out"))
    written: tuple[str, ...] = publish_files([(scratch / "a.xlsx", "annual_001.xlsx"), (scratch / "b.xlsx", "annual_002.xlsx")], destination)
    assert written == ("annual_001.xlsx", "annual_002.xlsx")
    assert (destination / "annual_001.xlsx").read_bytes() == b"one"


def test_publishing_into_a_subdirectory_creates_it(tmp_path: Path) -> None:
    # reports/ is the one that does.
    scratch: UPath = ZPath(str(tmp_path / "scratch"))
    scratch.mkdir(parents=True)
    (scratch / "r.json").write_bytes(b"{}")
    destination: UPath = ZPath(str(tmp_path / "out"))
    publish_files([(scratch / "r.json", "reports/annual-20260915T120000Z.report.json")], destination)
    assert (destination / "reports" / "annual-20260915T120000Z.report.json").exists()


def test_listing_a_destination_that_has_never_been_written_to_is_not_an_error(tmp_path: Path) -> None:
    # That is the first run, not a failure.
    assert list_names(ZPath(str(tmp_path / "absent"))) == ()


def test_a_listing_is_sorted(tmp_path: Path) -> None:
    # So a delete set computed from it is stable, which makes a --dry-run listing comparable
    # with the run that follows it.
    destination: UPath = ZPath(str(tmp_path))
    name: str
    for name in ["c.xlsx", "a.xlsx", "b.xlsx"]:
        (destination / name).write_bytes(b"x")
    assert list_names(destination) == ("a.xlsx", "b.xlsx", "c.xlsx")


def test_a_listing_includes_directories(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    (destination / "reports").mkdir()
    (destination / "a.xlsx").write_bytes(b"x")
    assert list_names(destination) == ("a.xlsx", "reports")


def test_deleting_removes_the_named_entries(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    name: str
    for name in ["a.xlsx", "b.xlsx", "keep.xlsx"]:
        (destination / name).write_bytes(b"x")
    assert delete_names(destination, ["a.xlsx", "b.xlsx"]) == ("a.xlsx", "b.xlsx")
    assert list_names(destination) == ("keep.xlsx",)


def test_deleting_a_directory_removes_it_whole(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    (destination / "stale").mkdir()
    (destination / "stale" / "old.xlsx").write_bytes(b"x")
    assert delete_names(destination, ["stale"]) == ("stale",)
    assert list_names(destination) == ()


def test_deleting_something_already_gone_is_not_an_error(tmp_path: Path) -> None:
    # Reconciliation runs after a crash that may have removed things itself.
    assert delete_names(ZPath(str(tmp_path)), ["absent.xlsx"]) == ()


@pytest.mark.parametrize("name", ["", "..", ".", "a/b", "a\\b", "../escape.xlsx"])
def test_a_name_that_is_not_a_single_entry_is_refused(name: str, tmp_path: Path) -> None:
    # Deleting outside the profile's own directory is the one mistake this package must not be
    # able to make.
    with pytest.raises(ValueError, match="not a single entry"):
        delete_names(ZPath(str(tmp_path)), [name])


def test_a_refused_name_stops_before_deleting_anything_after_it(tmp_path: Path) -> None:
    destination: UPath = ZPath(str(tmp_path))
    (destination / "keep.xlsx").write_bytes(b"x")
    with pytest.raises(ValueError, match="not a single entry"):
        delete_names(destination, ["..", "keep.xlsx"])
    assert (destination / "keep.xlsx").exists()
