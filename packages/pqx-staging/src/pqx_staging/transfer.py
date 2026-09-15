"""Moving files between a store and scratch, and between scratch and a destination.

Copies are **streamed**, not read into memory. A staged Parquet file is the largest thing this
project touches, and ``read_bytes()`` on one would put the whole file in the memory the export is
already competing for -- which on a tmpfs scratch root is the same memory again.

Every copy is **cleaned up on failure**. A partial file left at a destination is worse than no
file: it is readable, it is the right size to look plausible, and reconciliation will keep it
because the manifest names it.

There is **no atomic rename**. ``adlfs`` implements a move as a copy followed by a delete, so a
publish cannot be made atomic at this layer, and nothing here pretends otherwise. What protects
the destination is the order publication happens in and the lease, both of which live above this
package. That is also what gate 0f was to confirm and has not been run.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Final

from pqx_staging.errors import TransferError

if TYPE_CHECKING:
    from typing import IO

    from upath import UPath

__all__ = ["COPY_CHUNK_BYTES", "copy_file", "delete_names", "list_names", "publish_files", "stage_sources"]

COPY_CHUNK_BYTES: Final[int] = 4 * 1024 * 1024
"""Bytes moved per read/write cycle. Large enough that a remote round trip is amortised, small
enough that a dozen concurrent copies do not add up to a memory problem."""


def copy_file(source: UPath, target: UPath) -> UPath:
    """Copy one file, streaming, and remove what was written if it fails.

    Args:
        source: The file to read.
        target: Where to write it. Its parent is created if missing.

    Returns:
        ``target``.

    Raises:
        TransferError: The copy failed. Whatever had been written to ``target`` is removed first,
            so the failure leaves nothing behind that looks like a complete file.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    reader: IO[bytes]
    writer: IO[bytes]
    try:
        with source.open("rb") as reader, target.open("wb") as writer:
            shutil.copyfileobj(reader, writer, COPY_CHUNK_BYTES)
    except (OSError, ValueError) as error:
        # Best-effort: the copy already failed, and failing to clean up must not replace the
        # error that explains why with one about the cleanup.
        with suppress(OSError):
            target.unlink(missing_ok=True)
        message: str = f"could not copy {source} to {target}: {error}"
        raise TransferError(message) from error
    return target


def stage_sources(sources: Iterable[UPath], scratch: UPath) -> dict[UPath, UPath]:
    """Copy every source into scratch, staging a repeated one only once.

    **Two profiles naming the same source stage it once and share it.** Deduplication is by path
    rather than by content: two paths that are the same file under different spellings are a
    question about the store, and answering it here would mean stat-ing everything twice to learn
    something the caller usually already knows.

    A basename collision between two different sources is refused rather than resolved. Silently
    renaming one would make the staged copy's name disagree with the manifest's record of it.

    Args:
        sources: The source files, in any order.
        scratch: The run's scratch directory.

    Returns:
        Each distinct source mapped to its staged copy, in first-seen order.

    Raises:
        TransferError: A copy failed, or two different sources share a basename.
    """
    staged: dict[UPath, UPath] = {}
    claimed: dict[str, UPath] = {}
    source: UPath
    for source in sources:
        if source in staged:
            continue
        owner: UPath | None = claimed.get(source.name)
        if owner is not None:
            message: str = f"sources {owner} and {source} share the basename {source.name!r}; staging both would make one overwrite the other"
            raise TransferError(message)
        claimed[source.name] = source
        staged[source] = copy_file(source, scratch / source.name)
    return staged


def publish_files(files: Sequence[tuple[UPath, str]], destination: UPath) -> tuple[str, ...]:
    """Copy each local file to the destination under the name given.

    Names are destination-relative, so a caller may publish into a subdirectory -- ``reports/`` is
    the one that does. The subdirectory is created as needed.

    Args:
        files: ``(local path, destination-relative name)`` pairs, in publication order.
        destination: The profile's output directory.

    Returns:
        The names written, in order.

    Raises:
        TransferError: A copy failed. Everything published before it stays: this layer moves
            files, and deciding whether a partial publication should be unwound is a question
            about the manifest and the lease, which live above it.
    """
    local: UPath
    name: str
    for local, name in files:
        copy_file(local, destination / name)
    return tuple(name for _, name in files)


def list_names(destination: UPath) -> tuple[str, ...]:
    """Return the names directly in the destination, sorted, directories included.

    Sorted so a delete set computed from it is stable, which is what makes a ``--dry-run`` listing
    comparable with the run that follows it.

    Args:
        destination: The directory to list.

    Returns:
        The names, or an empty tuple when the directory does not exist -- a destination that has
        never been written to is not an error, it is the first run.
    """
    if not destination.exists():
        return ()
    return tuple(sorted(entry.name for entry in destination.iterdir()))


def delete_names(destination: UPath, names: Iterable[str]) -> tuple[str, ...]:
    """Delete the named entries from the destination and return what went.

    Refuses a name that is not a single path segment. Reconciliation's delete set is computed from
    a listing, so a ``..`` here would have to have come from somewhere other than that listing --
    and deleting outside the profile's own directory is the one mistake this package must not be
    able to make.

    Args:
        destination: The profile's output directory.
        names: The entries to remove.

    Returns:
        The names actually removed, in the order given. A name that was already gone is not
        reported, and is not an error: reconciliation races with nothing, but it does run after a
        crash that may have removed things itself.

    Raises:
        ValueError: A name is not a single path segment.
    """
    removed: list[str] = []
    name: str
    for name in names:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            message: str = f"{name!r} is not a single entry in {destination}"
            raise ValueError(message)
        target: UPath = destination / name
        if not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(str(target), ignore_errors=True)
        else:
            target.unlink()
        removed.append(name)
    return tuple(removed)
