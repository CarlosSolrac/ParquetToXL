"""Where a profile's bookkeeping lives in its output directory.

One place, because four things join these names: the run that writes them, the selection that reads
them next time, reconciliation's keep-set, and the lease. A name spelled twice is a name that can
differ once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from upath import UPath

__all__ = ["MANIFEST_SUFFIX", "RECEIPT_SUFFIX", "REPORTS_DIRECTORY", "keep_set", "manifest_path", "receipt_path", "reports_directory"]

MANIFEST_SUFFIX: str = ".manifest.json"
RECEIPT_SUFFIX: str = ".receipt.json"
"""Both keyed by profile name, matching ``<profile>.lock`` which ``pqx-staging`` already writes."""

REPORTS_DIRECTORY: str = "reports"
"""Excluded from reconciliation wholesale, so a failed run's report survives the next run."""


def manifest_path(destination: UPath, profile: str) -> UPath:
    """Return where a profile's run manifest lives."""
    return destination / f"{profile}{MANIFEST_SUFFIX}"


def receipt_path(destination: UPath, profile: str) -> UPath:
    """Return where a profile's run receipt lives.

    The receipt does triple duty: the T4 anchor, the ownership marker, and the "this export
    completed" marker. It is written last for that reason -- nothing may precede it that a later
    step could invalidate.
    """
    return destination / f"{profile}{RECEIPT_SUFFIX}"


def reports_directory(destination: UPath) -> UPath:
    """Return where reports accumulate, for successes and failures alike."""
    return destination / REPORTS_DIRECTORY


def keep_set(profile: str, *, workbooks: tuple[str, ...], sidecars: tuple[str, ...]) -> frozenset[str]:
    """Return the names reconciliation must not delete.

    ``listing - keep_set`` is the delete set. **The keep-set must include the bookkeeping**, or the
    run deletes its own records on the way out -- and ``sidecars`` is in it for the same reason:
    with the sidecars published beside the output they share this directory, and a keep-set without
    them would have the run delete what it published a few steps earlier.

    Args:
        profile: The profile that owns this directory.
        workbooks: Destination-relative workbook names from the manifest.
        sidecars: Destination-relative sidecar names from the manifest.

    Returns:
        The names to keep, including ``reports/`` and the three bookkeeping files. Only the first
        segment of a nested name is kept, since the delete set is computed from a listing of the
        directory itself rather than a walk.
    """
    return frozenset(
        {
            *(name.split("/", 1)[0] for name in workbooks),
            *(name.split("/", 1)[0] for name in sidecars),
            f"{profile}{MANIFEST_SUFFIX}",
            f"{profile}{RECEIPT_SUFFIX}",
            f"{profile}.lock",
            REPORTS_DIRECTORY,
        },
    )
