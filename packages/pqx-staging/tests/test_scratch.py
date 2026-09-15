"""Frozen tests for the scratch lifecycle and the free-space precheck."""

from __future__ import annotations

import shutil
import tempfile
from typing import TYPE_CHECKING

import pytest
from pqx_common.paths import ZPath
from pqx_staging.errors import InsufficientSpaceError
from pqx_staging.scratch import DEFAULT_OUTPUT_ALLOWANCE, FreeSpace, default_scratch_root, require_free_space, required_bytes, scratch_directory

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from upath import UPath


def _usage(total: int, used: int, free: int) -> Callable[[object], tuple[int, int, int]]:
    """Return a stand-in for ``shutil.disk_usage``.

    So the precheck's refusal is reached by injecting a reading rather than by finding a
    filesystem that is genuinely full.
    """

    def reading(_path: object) -> tuple[int, int, int]:
        """Report a fixed reading."""
        return (total, used, free)

    return reading


def _file(root: UPath, name: str, size: int) -> UPath:
    """A file of a known size."""
    path: UPath = root / name
    path.write_bytes(b"x" * size)
    return path


def test_the_default_root_is_the_platform_temporary_directory() -> None:
    assert str(default_scratch_root()) == tempfile.gettempdir()


def test_tmpdir_is_honoured_only_before_gettempdir_has_resolved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # gettempdir() caches its answer for the life of the process, so setting TMPDIR afterwards
    # does nothing. Pinned because the opposite is easy to assume: a caller that needs to choose
    # its scratch root at runtime must pass `root` rather than set the variable and hope.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)
    assert str(default_scratch_root()) == str(tmp_path)


def test_the_requirement_sums_every_source_plus_the_allowance(tmp_path: Path) -> None:
    # Fan-in stages them all at once rather than one at a time.
    root: UPath = ZPath(str(tmp_path))
    sources: list[UPath] = [_file(root, "a.parquet", 100), _file(root, "b.parquet", 250)]
    assert required_bytes(sources, output_allowance=1000) == 1350


def test_the_requirement_uses_a_configured_allowance_by_default(tmp_path: Path) -> None:
    root: UPath = ZPath(str(tmp_path))
    assert required_bytes([_file(root, "a.parquet", 10)]) == 10 + DEFAULT_OUTPUT_ALLOWANCE


def test_sizing_a_run_against_a_missing_source_is_refused(tmp_path: Path) -> None:
    # Would pass the precheck and fail at stage-in, which names the wrong step.
    with pytest.raises(FileNotFoundError):
        required_bytes([ZPath(str(tmp_path / "absent.parquet"))])


def test_a_run_that_fits_passes_the_precheck(tmp_path: Path) -> None:
    space: FreeSpace = require_free_space(ZPath(str(tmp_path)), 1)
    assert space.sufficient
    assert space.shortfall == 0
    assert space.free >= 1


def test_a_run_that_does_not_fit_is_refused_before_anything_is_copied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The alternative is an ENOSPC partway through writing a workbook, which leaves a truncated
    # file in scratch and an error naming the write rather than the capacity.
    monkeypatch.setattr(shutil, "disk_usage", _usage(1000, 900, 100))
    with pytest.raises(InsufficientSpaceError, match="short by 900"):
        require_free_space(ZPath(str(tmp_path)), 1000)


def test_the_shortfall_is_named_in_the_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", _usage(1000, 999, 1))
    caught: pytest.ExceptionInfo[InsufficientSpaceError]
    with pytest.raises(InsufficientSpaceError) as caught:
        require_free_space(ZPath(str(tmp_path)), 5_000_000)
    assert "4,999,999" in str(caught.value)


def test_exactly_enough_space_is_enough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", _usage(1000, 500, 500))
    assert require_free_space(ZPath(str(tmp_path)), 500).sufficient


def test_a_scratch_directory_exists_inside_the_block_and_not_after(tmp_path: Path) -> None:
    root: UPath = ZPath(str(tmp_path))
    seen: UPath
    directory: UPath
    with scratch_directory("abc", root=root) as directory:
        seen = directory
        assert directory.exists()
        assert directory.name == "run-abc"
        (directory / "work.xlsx").write_bytes(b"x")
    assert not seen.exists()


def test_cleanup_runs_on_failure_as_well_as_success(tmp_path: Path) -> None:
    # The whole reason this is a context manager: the failure path is where leftovers accumulate,
    # and it is also the one a caller forgets.
    root: UPath = ZPath(str(tmp_path))
    seen: list[UPath] = []

    def fail_inside() -> None:
        """Do some work in scratch, then fail the way a real run does."""
        directory: UPath
        with scratch_directory("abc", root=root) as directory:
            seen.append(directory)
            (directory / "half-written.xlsx").write_bytes(b"x")
            message: str = "boom"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="boom"):
        fail_inside()
    assert not seen[0].exists()


def test_keeping_the_directory_leaves_it_for_debugging(tmp_path: Path) -> None:
    root: UPath = ZPath(str(tmp_path))
    seen: UPath
    directory: UPath
    with scratch_directory("abc", root=root, keep=True) as directory:
        seen = directory
        (directory / "work.xlsx").write_bytes(b"x")
    assert seen.exists()
    assert (seen / "work.xlsx").exists()


def test_a_cleanup_that_fails_does_not_mask_the_runs_own_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root: UPath = ZPath(str(tmp_path))

    def refuse(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        """A filesystem that refuses the call outright, which `ignore_errors` does not cover."""
        message: str = "cannot remove"
        raise OSError(message)

    monkeypatch.setattr(shutil, "rmtree", refuse)

    def fail_inside() -> None:
        """Fail inside the block, so the cleanup runs while an exception is already in flight."""
        with scratch_directory("abc", root=root):
            message: str = "boom"
            raise RuntimeError(message)

    # The caller must see its own error, not the cleanup's. Without the suppress in the finally,
    # this OSError would replace the RuntimeError the run was already raising.
    with pytest.raises(RuntimeError, match="boom"):
        fail_inside()


@pytest.mark.parametrize("run_id", ["", "a/b", "a\\b", ".", ".."])
def test_a_run_id_that_is_not_one_path_segment_is_refused(run_id: str, tmp_path: Path) -> None:
    # A caller passing something with a separator would escape the scratch root.
    def enter() -> None:
        """Open the block, which is where the refusal happens."""
        with scratch_directory(run_id, root=ZPath(str(tmp_path))):
            pass

    with pytest.raises(ValueError, match="single path segment"):
        enter()


def test_a_reused_run_id_does_not_fail(tmp_path: Path) -> None:
    # A retry under the same run_id reuses the directory rather than refusing.
    root: UPath = ZPath(str(tmp_path))
    with scratch_directory("abc", root=root, keep=True):
        pass
    directory: UPath
    with scratch_directory("abc", root=root) as directory:
        assert directory.exists()


def test_the_default_root_is_used_when_none_is_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)
    directory: UPath
    with scratch_directory("abc") as directory:
        assert str(directory).startswith(str(tmp_path))
