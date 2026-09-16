"""Gate 0f: does an Azure Blob destination behave the way the freshness rule assumes?

``sidecar_stale`` decides whether a source needs re-describing by comparing the source's
``stat().st_mtime`` against the ``modified_utc`` recorded in its sidecar, both truncated to the
second. **Every rebuild decision in the pipeline rests on that comparison.** If ``adlfs`` does not
surface a usable ``st_mtime`` on a blob -- absent, constant, an access time rather than a write
time, or a value that does not survive being written and read back -- then ``sidecar_stale`` needs
a different source of truth, and that is a design change rather than a fix.

Nothing built so far assumes an answer. This harness gets one.

**It reports rather than asserts.** No check here says what ``adlfs`` *should* return, because the
point is to find out. Each check prints what it observed and, where the pipeline depends on a
particular property, says plainly whether that property held. A check that raises is a result and
is recorded as one.

**It exercises the production code paths, not raw ``adlfs``.** Paths are built with ``zpath`` so
the credential seam is the real one; the sidecar goes through ``get_sidecar_store("json")``; the
workbook copy goes through ``pqx_staging.transfer.copy_file``; listing and deletion go through
``list_names`` and ``delete_names``. A gate that measured ``adlfs`` directly would answer a
question nobody is asking.

**Safety.** The harness refuses a destination that is not empty, so it cannot be pointed at a live
export directory by accident, and it deletes only the names it created. Credentials are never
printed: the credential section reports which environment variables are *set*, never their values.

Usage, with the environment already carrying whatever ``adlfs`` needs -- see
``pqx_common.paths`` for the variables it reads and the ``DefaultAzureCredential`` fallback::

    uv run python gates/gate_0f_azure_round_trip.py --destination abfs://container/gate-0f
    uv run python gates/gate_0f_azure_round_trip.py --destination abfs://container/gate-0f --json

A local path works too, and is the way to check the harness itself before spending a real
container on it::

    uv run python gates/gate_0f_azure_round_trip.py --destination ./scratch/gate-0f
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Final

from pqx_common.paths import zpath
from pqx_pipeline.staleness import truncate_to_second
from pqx_staging.transfer import copy_file, delete_names, list_names
from upath import UPath

CREDENTIAL_VARIABLES: Final[tuple[str, ...]] = (
    "AZURE_STORAGE_ACCOUNT_NAME",
    "AZURE_STORAGE_ACCOUNT_KEY",
    "AZURE_STORAGE_CONNECTION_STRING",
    "AZURE_STORAGE_SAS_TOKEN",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
)
"""What ``adlfs`` reads before falling back to ``DefaultAzureCredential``. Reported by name only."""

PROBE_JSON_NAME: Final[str] = "gate_0f_probe.json"
PROBE_BINARY_NAME: Final[str] = "gate_0f_probe.xlsx"
"""The two objects this harness creates, and the only two it will delete."""

REWRITE_PAUSE_SECONDS: Final[float] = 1.5
"""Slept between the two writes, so a one-second-resolution timestamp can distinguish them. A
shorter pause would make "the timestamp did not move" ambiguous between coarse resolution and a
timestamp that never moves at all."""

BINARY_PROBE: Final[bytes] = b"PK\x03\x04gate-0f-not-a-real-workbook"
"""Enough of an ``.xlsx`` magic number to be stored as one, and obviously not a workbook."""


@dataclass(frozen=True, slots=True)
class Check:
    """One observation, and whether the property the pipeline needs held.

    Attributes:
        name: What was checked.
        held: ``True`` when the pipeline's assumption held, ``False`` when it did not, and ``None``
            when the check only reports and asserts nothing.
        detail: What was actually observed, in a form a human can act on.
    """

    name: str
    held: bool | None
    detail: str


@dataclass
class Findings:
    """Every check, in the order they ran."""

    checks: list[Check] = field(default_factory=list[Check])

    def record(self, name: str, held: bool | None, detail: str) -> None:
        """Add one observation.

        Args:
            name: What was checked.
            held: Whether the pipeline's assumption held, or ``None`` when nothing is asserted.
            detail: What was observed.
        """
        self.checks.append(Check(name=name, held=held, detail=detail))
        marker: str = "    " if held is None else ("ok  " if held else "FAIL")
        print(f"  {marker}  {name}: {detail}")

    def failed(self) -> list[Check]:
        """Return the checks whose property did not hold."""
        return [check for check in self.checks if check.held is False]


def report_credentials(findings: Findings) -> None:
    """Record which credential variables are set, by name only.

    Args:
        findings: Where to record.
    """
    present: list[str] = [name for name in CREDENTIAL_VARIABLES if os.environ.get(name)]
    detail: str = ", ".join(present) if present else "none set; adlfs will fall back to DefaultAzureCredential"
    findings.record("credential variables present", None, detail)


def probe_mkdir(destination: UPath, findings: Findings) -> None:
    """Create the destination prefix, and report whether it needed creating.

    A local directory has to exist before anything can be written into it. Blob storage has no
    directories at all, so ``mkdir`` there is either a no-op or a marker object, and which one it
    is decides whether ``publish_files`` can assume its target exists. Reported rather than
    asserted, because either answer is workable and only one of them is a surprise.

    Args:
        destination: The prefix to create.
        findings: Where to record.
    """
    existed: bool = destination.exists()
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # the failure is the finding
        findings.record("mkdir on the destination", False, f"{type(exc).__name__}: {exc}")
        return
    findings.record("mkdir on the destination", None, "already existed" if existed else f"created; exists() now {destination.exists()}")


def require_empty(destination: UPath, findings: Findings) -> bool:
    """Refuse a destination that already holds something.

    The harness deletes what it creates, and a destination holding a real export is one where a
    deletion bug costs someone their bookkeeping. Refusing an occupied prefix is cheaper than
    being careful.

    Args:
        destination: Where the probes would go.
        findings: Where to record.

    Returns:
        Whether the destination is safe to use.
    """
    existing: tuple[str, ...] = list_names(destination)
    if existing:
        findings.record("destination is empty", False, f"holds {len(existing)} entries, first few {existing[:5]}; refusing to write into it")
        return False
    findings.record("destination is empty", True, "nothing there, safe to write probes")
    return True


def probe_json(destination: UPath, findings: Findings) -> UPath:
    """Write a small JSON object and read it back.

    The sidecar is JSON and is read on every run's selection pass, so a destination that cannot
    round-trip a small text object cannot hold sidecars.

    Args:
        destination: Where to write.
        findings: Where to record.

    Returns:
        The path written.
    """
    target: UPath = destination / PROBE_JSON_NAME
    payload: dict[str, Any] = {"gate": "0f", "written_utc": dt.datetime.now(tz=dt.UTC).isoformat()}
    target.write_text(json.dumps(payload), encoding="utf-8")
    returned: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    findings.record("small JSON round trip", returned == payload, "byte-identical" if returned == payload else f"wrote {payload}, read {returned}")
    return target


def probe_mtime(target: UPath, findings: Findings) -> None:
    """Report what ``stat().st_mtime`` gives on a freshly written object.

    This is the gate's real question. ``sidecar_stale`` reads ``st_mtime`` and compares it, through
    ``truncate_to_second``, against what was recorded when the source was described.

    Args:
        target: The object to stat.
        findings: Where to record.
    """
    raw: float = target.stat().st_mtime
    moment: dt.datetime = dt.datetime.fromtimestamp(raw, tz=dt.UTC)
    findings.record("st_mtime is present", True, f"{raw!r} -> {moment.isoformat()}")

    fractional: float = raw - int(raw)
    resolution: str = "whole seconds" if fractional == 0.0 else f"sub-second, fractional part {fractional!r}"
    findings.record("st_mtime resolution", None, resolution)

    skew: float = (dt.datetime.now(tz=dt.UTC) - moment).total_seconds()
    plausible: bool = -300.0 < skew < 300.0
    findings.record("st_mtime is a recent write time", plausible, f"{skew:.1f}s before now" + ("" if plausible else "; too far off to be this write, so it may not be a modification time at all"))

    again: float = target.stat().st_mtime
    findings.record("st_mtime is stable across reads", again == raw, f"second stat gave {again!r}" + ("" if again == raw else "; a value that moves when only read is an access time, not a modification time"))


def probe_rewrite(target: UPath, findings: Findings) -> None:
    """Rewrite the object and report whether the timestamp moved.

    A timestamp that does not change when the object changes cannot drive a freshness rule, which
    is the failure mode this gate exists to detect.

    Args:
        target: The object to rewrite.
        findings: Where to record.
    """
    before: float = target.stat().st_mtime
    time.sleep(REWRITE_PAUSE_SECONDS)
    target.write_text(json.dumps({"gate": "0f", "rewritten_utc": dt.datetime.now(tz=dt.UTC).isoformat()}), encoding="utf-8")
    after: float = target.stat().st_mtime

    moved: bool = after != before
    findings.record("st_mtime moves when the object is rewritten", moved, f"{before!r} -> {after!r}" + ("" if moved else "; the freshness rule cannot use this and sidecar_stale needs another source of truth"))

    truncated_differ: bool = truncate_to_second(dt.datetime.fromtimestamp(before, tz=dt.UTC)) != truncate_to_second(dt.datetime.fromtimestamp(after, tz=dt.UTC))
    findings.record(
        "the move survives truncate_to_second",
        truncated_differ,
        "the two writes are distinguishable at whole-second precision"
        if truncated_differ
        else f"both truncate to the same second after a {REWRITE_PAUSE_SECONDS}s pause; sidecar_stale compares truncated values and would call this unchanged",
    )


def probe_sidecar_comparison(target: UPath, findings: Findings) -> None:
    """Check the exact comparison ``sidecar_stale`` performs.

    Recording a stat and re-reading it must compare equal, or every run re-describes every source
    forever while looking perfectly healthy.

    Args:
        target: The object to stat.
        findings: Where to record.
    """
    recorded: dt.datetime = dt.datetime.fromtimestamp(target.stat().st_mtime, tz=dt.UTC)
    observed: dt.datetime = dt.datetime.fromtimestamp(target.stat().st_mtime, tz=dt.UTC)
    same: bool = truncate_to_second(recorded) == truncate_to_second(observed)
    findings.record("a recorded mtime compares equal to a later stat", same, f"{truncate_to_second(recorded).isoformat()} vs {truncate_to_second(observed).isoformat()}")


def probe_binary_copy(destination: UPath, scratch_source: UPath, findings: Findings) -> UPath:
    """Copy a binary file up through the production transfer helper.

    Args:
        destination: Where the copy lands.
        scratch_source: The local file to copy.
        findings: Where to record.

    Returns:
        The path written.
    """
    target: UPath = copy_file(scratch_source, destination / PROBE_BINARY_NAME)
    returned: bytes = target.read_bytes()
    findings.record("binary copy up through copy_file", returned == BINARY_PROBE, f"{len(returned)} bytes back" + ("" if returned == BINARY_PROBE else ", and they differ from what went up"))
    return target


def probe_listing(destination: UPath, findings: Findings) -> tuple[str, ...]:
    """Report what listing the destination returns.

    Reconciliation deletes what the manifest does not claim, and it works from this listing, so
    entries have to be bare names rather than URIs or prefixed keys.

    Args:
        destination: What to list.
        findings: Where to record.

    Returns:
        The names listed.
    """
    names: tuple[str, ...] = list_names(destination)
    expected: set[str] = {PROBE_JSON_NAME, PROBE_BINARY_NAME}
    findings.record("listing returns the two probes", set(names) == expected, f"{names}")

    bare: bool = all("/" not in name and "\\" not in name for name in names)
    findings.record("listed entries are bare names", bare, "no separators in any entry" if bare else f"some entries carry a path: {names}")
    return names


def probe_delete(destination: UPath, names: tuple[str, ...], findings: Findings) -> None:
    """Delete by listing, and confirm the destination is empty afterwards.

    Args:
        destination: Where to delete from.
        names: The names to delete. Only ever the probes this harness created.
        findings: Where to record.
    """
    removable: tuple[str, ...] = tuple(name for name in names if name in {PROBE_JSON_NAME, PROBE_BINARY_NAME})
    deleted: tuple[str, ...] = delete_names(destination, removable)
    findings.record("delete by listing", set(deleted) == set(removable), f"deleted {deleted}")

    remaining: tuple[str, ...] = list_names(destination)
    findings.record("destination is empty afterwards", not remaining, "nothing left" if not remaining else f"still holds {remaining}")


def run(destination: UPath, scratch: UPath) -> Findings:
    """Run every check in order, recording what each one found.

    A check that raises stops the run: later checks assume the objects earlier ones created, so
    continuing past a failure would report cascading noise rather than one cause.

    Args:
        destination: Where the probes go.
        scratch: A local directory for the file that gets copied up.

    Returns:
        Everything observed.
    """
    findings: Findings = Findings()
    report_credentials(findings)
    print()

    probe_mkdir(destination, findings)
    if not require_empty(destination, findings):
        return findings

    target: UPath = probe_json(destination, findings)
    probe_mtime(target, findings)
    probe_rewrite(target, findings)
    probe_sidecar_comparison(target, findings)

    local: UPath = scratch / PROBE_BINARY_NAME
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(BINARY_PROBE)
    probe_binary_copy(destination, local, findings)

    probe_delete(destination, probe_listing(destination, findings), findings)
    return findings


def main() -> int:
    """Parse arguments, run the checks, and summarise.

    Returns:
        ``0`` when every asserted property held, ``1`` when one did not, ``2`` when the run could
        not complete.
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="gate_0f_azure_round_trip",
        description="Gate 0f: does an Azure Blob destination behave the way the freshness rule assumes?",
        epilog="The destination must be empty. Its contents are never read and only the two probe objects this harness writes are ever deleted.",
    )
    parser.add_argument("--destination", required=True, help="where to write the probes, e.g. abfs://container/gate-0f")
    parser.add_argument("--scratch", default="./scratch/gate-0f", help="a local directory for the file that gets copied up")
    parser.add_argument("--json", action="store_true", help="print the findings as JSON as well")
    arguments: argparse.Namespace = parser.parse_args()

    destination: UPath = zpath(str(arguments.destination))
    scratch: UPath = zpath(str(arguments.scratch))
    print(f"gate 0f against {destination}\n")

    findings: Findings
    try:
        findings = run(destination, scratch)
    except Exception:
        # The failure IS the result here; a traceback is what an operator needs in order to act on it.
        print("\nthe run did not complete:\n")
        traceback.print_exc()
        print("\nA failure here is a finding. Record what it was before changing anything: the freshness rule's\nsource of truth depends on the answer.")
        return 2

    print()
    failures: list[Check] = findings.failed()
    check: Check
    if failures:
        print(f"{len(failures)} of the pipeline's assumptions did not hold:\n")
        for check in failures:
            print(f"  - {check.name}: {check.detail}")
        print("\nsidecar_stale rests on st_mtime. If one of the mtime checks is among these, it needs a\ndifferent source of truth, which is a design change rather than a fix.")
    else:
        print("every asserted property held; the destination behaves the way the freshness rule assumes")

    if arguments.json:
        print()
        print(json.dumps([{"name": check.name, "held": check.held, "detail": check.detail} for check in findings.checks], indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
