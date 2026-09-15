"""The errors this package raises."""

from __future__ import annotations


class StagingError(Exception):
    """Base class for every refusal this package makes."""


class InsufficientSpaceError(StagingError):
    """The scratch root cannot hold what the run needs.

    Raised before anything is copied, deliberately. The alternative is an ``ENOSPC`` partway
    through writing a workbook, which leaves a truncated file in scratch, a half-staged source
    beside it, and an error naming the write rather than the capacity.
    """


class LeaseHeldError(StagingError):
    """Another run holds an unexpired lease on this profile's output directory.

    Not a lock. Neither SMB nor Blob offers compare-and-swap, so two runs starting in the same
    instant can both acquire. This closes the realistic case -- someone launching the job twice --
    not the theoretical one.
    """


class LeaseNotHeldError(StagingError):
    """An operation that requires the lease was attempted without it.

    Deletion is the one that matters: reconciliation removes everything the manifest does not
    claim, and doing that without the lease is how two concurrent runs delete each other's output.
    """


class TransferError(StagingError):
    """A file could not be copied, and what was partially written has been cleaned up."""
