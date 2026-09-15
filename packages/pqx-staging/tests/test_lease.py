"""Frozen tests for the profile lease.

Every instant is passed in rather than read, so nothing here depends on when it runs.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import pytest
from pqx_common.paths import ZPath
from pqx_staging.errors import LeaseHeldError, LeaseNotHeldError
from pqx_staging.lease import DEFAULT_LEASE_TTL, LEASE_VERSION, Lease, acquire_lease, lease_path, read_lease, release_lease, require_lease
from pydantic import ValidationError

if TYPE_CHECKING:
    from pathlib import Path

    from upath import UPath

NOON: dt.datetime = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
LATER: dt.datetime = NOON + dt.timedelta(hours=1)
NEXT_DAY: dt.datetime = NOON + dt.timedelta(days=1)


def _destination(tmp_path: Path) -> UPath:
    """A profile's output directory."""
    return ZPath(str(tmp_path / "out"))


def test_a_directory_with_no_lease_reports_none(tmp_path: Path) -> None:
    assert read_lease(_destination(tmp_path), "annual_review") is None


def test_acquiring_writes_the_lease_where_it_is_expected(tmp_path: Path) -> None:
    destination: UPath = _destination(tmp_path)
    lease: Lease = acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=NOON)
    assert lease_path(destination, "annual_review").name == "annual_review.lock"
    assert lease_path(destination, "annual_review").exists()
    assert lease.run_id == "run-1"
    assert lease.acquired_utc == NOON
    assert lease.lease_version == LEASE_VERSION


def test_an_acquired_lease_reads_back(tmp_path: Path) -> None:
    destination: UPath = _destination(tmp_path)
    written: Lease = acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=NOON)
    assert read_lease(destination, "annual_review") == written


def test_another_run_is_refused_while_the_lease_holds(tmp_path: Path) -> None:
    # Two concurrent runs of one configuration both pass the ownership check and both reconcile
    # by listing, each deleting what the other just wrote.
    destination: UPath = _destination(tmp_path)
    acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=NOON)
    with pytest.raises(LeaseHeldError, match="run-1"):
        acquire_lease(destination, "annual_review", run_id="run-2", config_id="cfg-1", now=LATER)


def test_a_lapsed_lease_can_be_taken_over(tmp_path: Path) -> None:
    # A killed run must not wedge the directory permanently.
    destination: UPath = _destination(tmp_path)
    acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=NOON)
    taken: Lease = acquire_lease(destination, "annual_review", run_id="run-2", config_id="cfg-1", now=NEXT_DAY)
    assert taken.run_id == "run-2"


def test_the_same_run_may_reacquire_its_own_lease(tmp_path: Path) -> None:
    # A run that staged, failed and is being retried under the same run_id is not a second run.
    destination: UPath = _destination(tmp_path)
    acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=NOON)
    refreshed: Lease = acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=LATER)
    assert refreshed.acquired_utc == LATER


def test_two_profiles_hold_their_own_leases(tmp_path: Path) -> None:
    destination: UPath = _destination(tmp_path)
    acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=NOON)
    acquire_lease(destination, "monthly", run_id="run-2", config_id="cfg-1", now=NOON)
    assert read_lease(destination, "annual_review") is not None
    assert read_lease(destination, "monthly") is not None


@pytest.mark.parametrize(("elapsed", "expired"), [(dt.timedelta(0), False), (DEFAULT_LEASE_TTL - dt.timedelta(seconds=1), False), (DEFAULT_LEASE_TTL, True), (dt.timedelta(days=2), True)])
def test_expiry_is_judged_against_an_instant_passed_in(elapsed: dt.timedelta, expired: bool) -> None:
    lease: Lease = Lease(run_id="run-1", profile="annual_review", config_id="cfg-1", acquired_utc=NOON)
    assert lease.expired_at(NOON + elapsed) is expired


def test_a_shorter_ttl_expires_sooner() -> None:
    lease: Lease = Lease(run_id="run-1", profile="annual_review", config_id="cfg-1", acquired_utc=NOON)
    assert lease.expired_at(LATER, ttl=dt.timedelta(minutes=30))
    assert not lease.expired_at(LATER, ttl=dt.timedelta(hours=2))


# --------------------------------------------------------------------------------------
# Unreadable leases
# --------------------------------------------------------------------------------------


def test_a_lease_that_is_not_json_is_refused_rather_than_treated_as_absent(tmp_path: Path) -> None:
    # A lease this build cannot parse still belongs to a run that is probably still working, and
    # overwriting it would take a directory out from under that run.
    destination: UPath = _destination(tmp_path)
    destination.mkdir(parents=True)
    lease_path(destination, "annual_review").write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        read_lease(destination, "annual_review")


def test_a_lease_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    destination: UPath = _destination(tmp_path)
    destination.mkdir(parents=True)
    lease_path(destination, "annual_review").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="does not hold a JSON object"):
        read_lease(destination, "annual_review")


def test_a_lease_of_a_future_version_is_refused_by_name(tmp_path: Path) -> None:
    destination: UPath = _destination(tmp_path)
    destination.mkdir(parents=True)
    lease_path(destination, "annual_review").write_text('{"lease_version": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="reads only 1"):
        read_lease(destination, "annual_review")


# --------------------------------------------------------------------------------------
# require_lease
# --------------------------------------------------------------------------------------


def test_deletion_is_refused_without_a_lease(tmp_path: Path) -> None:
    with pytest.raises(LeaseNotHeldError, match="no lease"):
        require_lease(_destination(tmp_path), "annual_review", run_id="run-1", now=NOON)


def test_deletion_is_refused_under_another_runs_lease(tmp_path: Path) -> None:
    destination: UPath = _destination(tmp_path)
    acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=NOON)
    with pytest.raises(LeaseNotHeldError, match="belongs to run 'run-1'"):
        require_lease(destination, "annual_review", run_id="run-2", now=LATER)


def test_deletion_is_refused_under_an_expired_lease_of_this_runs_own(tmp_path: Path) -> None:
    # Long enough has passed that another run may have taken it and started deleting.
    destination: UPath = _destination(tmp_path)
    acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=NOON)
    with pytest.raises(LeaseNotHeldError, match="expired lease"):
        require_lease(destination, "annual_review", run_id="run-1", now=NEXT_DAY)


def test_a_held_lease_permits_deletion(tmp_path: Path) -> None:
    destination: UPath = _destination(tmp_path)
    acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=NOON)
    assert require_lease(destination, "annual_review", run_id="run-1", now=LATER).run_id == "run-1"


# --------------------------------------------------------------------------------------
# release
# --------------------------------------------------------------------------------------


def test_releasing_removes_this_runs_lease(tmp_path: Path) -> None:
    destination: UPath = _destination(tmp_path)
    acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=NOON)
    assert release_lease(destination, "annual_review", run_id="run-1")
    assert read_lease(destination, "annual_review") is None


def test_releasing_leaves_another_runs_lease_alone(tmp_path: Path) -> None:
    # A run that has lost its own lease has bigger problems than tidying up after itself.
    destination: UPath = _destination(tmp_path)
    acquire_lease(destination, "annual_review", run_id="run-1", config_id="cfg-1", now=NOON)
    assert not release_lease(destination, "annual_review", run_id="run-2")
    assert read_lease(destination, "annual_review") is not None


def test_releasing_nothing_reports_that_nothing_went(tmp_path: Path) -> None:
    assert not release_lease(_destination(tmp_path), "annual_review", run_id="run-1")


def test_a_lease_is_frozen_and_refuses_unknown_fields() -> None:
    lease: Lease = Lease(run_id="run-1", profile="annual_review", config_id="cfg-1", acquired_utc=NOON)
    with pytest.raises(ValidationError):
        lease.run_id = "run-2"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Lease(run_id="run-1", profile="p", config_id="c", acquired_utc=NOON, extra="x")  # type: ignore[call-arg]
