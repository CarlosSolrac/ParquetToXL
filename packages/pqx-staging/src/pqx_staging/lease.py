"""``<profile>.lock``: what keeps two runs of one profile from deleting each other's output.

**A lease, because ownership is not a lock.** Two concurrent runs of one configuration and profile
both pass the ownership check -- same ``config_id`` -- and both reconcile by listing, each deleting
what the other just wrote. So the lease is written before staging, and a non-expired foreign lease
refuses the run.

**This is not a mutex, and the docstrings here do not pretend otherwise.** Neither SMB nor Blob
offers compare-and-swap, so two runs starting in the same instant can both acquire. It closes the
realistic case -- someone launching the job twice -- not the theoretical one. A stale lease expires
on its own, so a killed run does not wedge the directory permanently.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, Field, JsonValue

from pqx_staging.errors import LeaseHeldError, LeaseNotHeldError

if TYPE_CHECKING:
    from upath import UPath

__all__ = ["DEFAULT_LEASE_TTL", "LEASE_VERSION", "Lease", "acquire_lease", "lease_path", "read_lease", "release_lease", "require_lease"]

LEASE_VERSION: Literal[1] = 1
"""The lease file's format version. A future version is refused rather than misread: a lease this
build cannot parse must not be treated as absent, or the run it belongs to loses its directory."""

DEFAULT_LEASE_TTL: Final[dt.timedelta] = dt.timedelta(hours=6)
"""How long a lease stays valid without being renewed.

Long enough that a real export of a large profile does not outlive its own lease, short enough that
a killed run does not wedge the directory for a working day. Nothing renews a lease mid-run today,
so this is a ceiling on run length rather than a heartbeat interval -- which is worth knowing
before raising it.
"""


class Lease(BaseModel, extra="forbid", frozen=True):
    """Who holds a profile's output directory, and since when.

    Attributes:
        lease_version: The file format's version.
        run_id: The run that acquired it.
        profile: The profile whose directory this is.
        config_id: The configuration the run belongs to, so a foreign lease can be described
            rather than merely reported.
        acquired_utc: When it was taken.
    """

    lease_version: Literal[1] = LEASE_VERSION
    run_id: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    config_id: str = Field(min_length=1)
    acquired_utc: dt.datetime

    def expired_at(self, now: dt.datetime, ttl: dt.timedelta = DEFAULT_LEASE_TTL) -> bool:
        """Return whether this lease has lapsed by ``now``.

        ``now`` is passed in rather than read here, so that a test of expiry does not depend on
        when it runs and so that one run compares every lease against a single instant.

        Args:
            now: The instant to judge against, timezone-aware.
            ttl: How long a lease stays valid.

        Returns:
            Whether the lease has lapsed.
        """
        return now - self.acquired_utc >= ttl


def lease_path(destination: UPath, profile: str) -> UPath:
    """Return where a profile's lease lives: ``<destination>/<profile>.lock``."""
    return destination / f"{profile}.lock"


def read_lease(destination: UPath, profile: str) -> Lease | None:
    """Return the lease on a profile's directory, or ``None`` when there is none.

    Args:
        destination: The profile's output directory.
        profile: The profile name.

    Returns:
        The lease, or ``None`` when no lease file exists.

    Raises:
        ValueError: A lease file exists and cannot be read as one. **Not** treated as absent: a
            lease this build cannot parse still belongs to a run that is probably still working,
            and overwriting it would take a directory out from under that run.
    """
    path: UPath = lease_path(destination, profile)
    if not path.exists():
        return None
    text: str = path.read_text(encoding="utf-8")
    # Typed as JsonValue rather than object, matching the sidecar store: narrowing a bare object
    # with isinstance leaves a dict of unknowns, and this way the keys and values are typed.
    loaded: JsonValue
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as error:
        message: str = f"{path} is not valid JSON: {error}"
        raise ValueError(message) from error
    if not isinstance(loaded, dict):
        message = f"{path} does not hold a JSON object"
        raise ValueError(message)
    version: JsonValue = loaded.get("lease_version")
    if version != LEASE_VERSION:
        message = f"{path} declares lease_version {version!r}; this build reads only {LEASE_VERSION}"
        raise ValueError(message)
    return Lease.model_validate(loaded)


def acquire_lease(destination: UPath, profile: str, *, run_id: str, config_id: str, now: dt.datetime, ttl: dt.timedelta = DEFAULT_LEASE_TTL) -> Lease:
    """Take the lease on a profile's directory, refusing an unexpired foreign one.

    Re-acquiring a lease this run already holds is allowed and refreshes it -- a run that staged,
    failed and is being retried under the same ``run_id`` is not a second run.

    Args:
        destination: The profile's output directory, created if missing.
        profile: The profile name.
        run_id: This run.
        config_id: The configuration this run belongs to.
        now: The instant to stamp and to judge any existing lease against.
        ttl: How long a lease stays valid.

    Returns:
        The lease now held.

    Raises:
        LeaseHeldError: Another run holds an unexpired lease.
        ValueError: An existing lease file cannot be read.
    """
    existing: Lease | None = read_lease(destination, profile)
    if existing is not None and existing.run_id != run_id and not existing.expired_at(now, ttl):
        message: str = f"run {existing.run_id!r} of configuration {existing.config_id!r} has held {profile!r} since {existing.acquired_utc.isoformat()}, and that lease has not expired"
        raise LeaseHeldError(message)
    lease: Lease = Lease(run_id=run_id, profile=profile, config_id=config_id, acquired_utc=now)
    destination.mkdir(parents=True, exist_ok=True)
    lease_path(destination, profile).write_text(lease.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return lease


def require_lease(destination: UPath, profile: str, *, run_id: str, now: dt.datetime, ttl: dt.timedelta = DEFAULT_LEASE_TTL) -> Lease:
    """Return this run's lease, refusing to proceed without it.

    Called before deletion. Reconciliation removes everything the manifest does not claim, and
    doing that without the lease is how two concurrent runs delete each other's output.

    Args:
        destination: The profile's output directory.
        profile: The profile name.
        run_id: The run that should hold it.
        now: The instant to judge expiry against.
        ttl: How long a lease stays valid.

    Returns:
        The lease, which this run holds and which has not expired.

    Raises:
        LeaseNotHeldError: There is no lease, it belongs to another run, or this run's own lease
            has expired -- which means long enough has passed that another run may have taken it
            and started deleting.
        ValueError: The lease file cannot be read.
    """
    existing: Lease | None = read_lease(destination, profile)
    if existing is None:
        message: str = f"no lease on {profile!r} at {destination}; deletion is refused without one"
        raise LeaseNotHeldError(message)
    if existing.run_id != run_id:
        message = f"the lease on {profile!r} belongs to run {existing.run_id!r}, not {run_id!r}"
        raise LeaseNotHeldError(message)
    if existing.expired_at(now, ttl):
        message = f"run {run_id!r} holds an expired lease on {profile!r}, taken at {existing.acquired_utc.isoformat()}; another run may already have taken it"
        raise LeaseNotHeldError(message)
    return existing


def release_lease(destination: UPath, profile: str, *, run_id: str) -> bool:
    """Remove this run's lease, leaving another run's alone.

    Args:
        destination: The profile's output directory.
        profile: The profile name.
        run_id: The run releasing it.

    Returns:
        Whether a lease was removed. ``False`` when there was none, or when the one there belongs
        to another run -- releasing that would be taking the directory out from under it, and a
        run that has lost its own lease has bigger problems than tidying up after itself.
    """
    existing: Lease | None = read_lease(destination, profile)
    if existing is None or existing.run_id != run_id:
        return False
    lease_path(destination, profile).unlink(missing_ok=True)
    return True
