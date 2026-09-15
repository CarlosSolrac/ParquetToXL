"""Where a run does its work, how much room it needs, and how that room is given back.

**Configurable, never a hard-coded ``/tmp``.** The default is ``tempfile.gettempdir()``, which
honours ``TMPDIR``, and it is overridable in configuration and on the CLI -- because Spark may point
local storage elsewhere, and because on many Spark images ``/tmp`` is **tmpfs**. Where it is,
staged Parquet and written workbooks come out of the same memory the export is already competing
for, which turns the free-space precheck below into a memory precheck without saying so.

That last point is what gate 0e was to settle and has not been run: whether ``gettempdir()``
resolves to tmpfs on the Spark image. Until it does, the safe reading is that it might.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pqx_common.paths import ZPath

from pqx_staging.errors import InsufficientSpaceError

if TYPE_CHECKING:
    from upath import UPath

__all__ = ["DEFAULT_OUTPUT_ALLOWANCE", "FreeSpace", "default_scratch_root", "require_free_space", "required_bytes", "scratch_directory"]

DEFAULT_OUTPUT_ALLOWANCE: Final[int] = 2 * 1024 * 1024 * 1024
"""Room to leave for the workbooks a run writes, over and above the sources it stages.

A configured allowance rather than a computed one. What an export costs on disk depends on how
compressible the data is, which is exactly what is not known before writing it, so the honest
choice is a number an operator can raise rather than an estimate that reads as a measurement.
"""


@dataclass(frozen=True, slots=True)
class FreeSpace:
    """What the scratch root has, and what the run was asked to fit into it.

    Attributes:
        root: The scratch root the reading was taken at.
        free: Bytes available.
        required: Bytes the run needs -- every source it will stage, plus the output allowance.
    """

    root: UPath
    free: int
    required: int

    @property
    def sufficient(self) -> bool:
        """Whether the run fits."""
        return self.free >= self.required

    @property
    def shortfall(self) -> int:
        """Bytes missing, or zero when the run fits."""
        return max(0, self.required - self.free)


def default_scratch_root() -> UPath:
    """Return the platform's temporary directory, honouring ``TMPDIR``.

    Read through ``tempfile.gettempdir()`` rather than hard-coded, which is what "honouring
    ``TMPDIR``" means in practice.

    **``gettempdir()`` caches.** It resolves the directory once and returns that answer for the
    life of the process, so setting ``TMPDIR`` after anything has already called it -- including
    anything in a library -- has no effect. A process that needs to choose its scratch root at
    runtime should pass ``root`` explicitly rather than set the variable and hope, which is why
    every function here takes one.

    Returns:
        The default scratch root.
    """
    return ZPath(tempfile.gettempdir())


def required_bytes(sources: Sequence[UPath], *, output_allowance: int = DEFAULT_OUTPUT_ALLOWANCE) -> int:
    """Return the scratch space a run needs: every source it stages, plus room for its output.

    **The sum of every source the profile needs**, because fan-in stages them all at once rather
    than one at a time. Two profiles naming the same source stage it once and share it, so the
    caller deduplicates before calling -- doing it here would need to know which paths are the same
    file, which is a question about the store rather than about arithmetic.

    Args:
        sources: Every source to be staged, already deduplicated.
        output_allowance: Room to leave for the workbooks.

    Returns:
        The total bytes needed.

    Raises:
        FileNotFoundError: A source is not there. Sizing a run against a file that does not exist
            would pass the precheck and fail at stage-in, which names the wrong step.
    """
    return sum(source.stat().st_size for source in sources) + output_allowance


def require_free_space(root: UPath, required: int) -> FreeSpace:
    """Check the scratch root against what the run needs, before anything is copied.

    Before, deliberately. The alternative is an ``ENOSPC`` partway through writing a workbook,
    which leaves a truncated file in scratch, a half-staged source beside it, and an error naming
    the write rather than the capacity.

    Args:
        root: The scratch root, which is local: ``shutil.disk_usage`` has no remote equivalent, and
            scratch is local by definition.
        required: Bytes needed, from :func:`required_bytes`.

    Returns:
        The reading, which is sufficient.

    Raises:
        InsufficientSpaceError: The root cannot hold the run, with the shortfall named.
    """
    # Indexed rather than read as .free: shutil types the result as a private named tuple, and
    # naming that type here would reach into the standard library's internals to say "the third
    # field". Position two is the documented free-bytes slot.
    free: int = shutil.disk_usage(str(root))[2]
    space: FreeSpace = FreeSpace(root=root, free=free, required=required)
    if not space.sufficient:
        message: str = f"{root} has {space.free:,} bytes free and this run needs {space.required:,}; it is short by {space.shortfall:,}"
        raise InsufficientSpaceError(message)
    return space


@contextmanager
def scratch_directory(run_id: str, *, root: UPath | None = None, keep: bool = False) -> Generator[UPath]:
    """Create ``<root>/run-<run_id>/`` for the duration, then remove it.

    **Cleanup runs on failure as well as success**, which is the whole reason this is a context
    manager rather than a pair of calls: the failure path is the one where leftovers accumulate,
    and it is also the one a caller forgets.

    ``keep`` is for debugging a failed run. It leaves the directory and says nothing further; the
    caller is expected to have told the operator, because a scratch directory that silently
    survives is indistinguishable from one the cleanup missed.

    Args:
        run_id: Identifies this run's directory. Used as a path segment, so a caller passing
            something with a separator in it would escape the root -- refused here.
        root: The scratch root. Defaults to :func:`default_scratch_root`.
        keep: Leave the directory in place when the block exits.

    Yields:
        The run's scratch directory, which exists.

    Raises:
        ValueError: ``run_id`` is empty or holds a path separator.
    """
    if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        message: str = f"run_id {run_id!r} is not a single path segment"
        raise ValueError(message)
    base: UPath = default_scratch_root() if root is None else root
    directory: UPath = base / f"run-{run_id}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        if not keep:
            # A best-effort remove: the directory is scratch, and failing to clean it up must not
            # mask whatever the run was already failing with. Both guards are needed and neither
            # is redundant: `ignore_errors` swallows failures encountered while walking the tree,
            # and `suppress` covers `rmtree` itself refusing -- a path that is not a directory, a
            # filesystem that rejects the call. Without the second, a cleanup failure inside a
            # `finally` replaces the exception the caller was already raising.
            with suppress(OSError):
                shutil.rmtree(str(directory), ignore_errors=True)
