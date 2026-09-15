"""The run receipt: the T4 anchor, the ownership marker, and the "this export completed" marker.

A separate, small file beside the manifest, published as ``<profile>.receipt.json``. Separate on
purpose: selection reads it on **every** run, over remote metadata, before anything is staged,
while the manifest embeds the resolved configuration and a digest per fragment. Pulling megabytes
over Azure to read four timestamps would be the wrong trade, and keeping them in one file would
make it one.

It does three jobs at once, and each of the three is why one of its fields is here:

- **The T4 anchor.** ``excel_created_utc`` is what ``sidecar_created_utc > T4`` and
  ``config_modified_utc > T4`` are compared against.
- **The ownership marker.** ``config_id`` and ``profile`` are what makes publishing into a
  directory whose receipt names a different configuration a refusal rather than a collision, and
  that exclusivity is what makes reconcile-by-listing safe.
- **The completion marker.** Written last, after the workbooks and the manifest, so a run that
  died partway leaves no receipt and the next run rebuilds the export. The sidecars, written
  early, still mark *described*, so that next run does not re-hash the Parquet.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel

from pqx_plan.timestamps import UtcDatetime

type ReceiptVersion = Literal[1]
"""The receipt schema versions this build reads. A closed vocabulary, so a ``Literal``.

Versioned independently of the manifest, though the two ship together, because selection reads
the receipt alone: a build that changed only the manifest layout must not make every receipt
unreadable and every export stale.
"""

RECEIPT_VERSION: Final[ReceiptVersion] = 1
"""The version this build writes. Annotated with the alias so the two cannot drift apart."""


class SourceStamp(BaseModel, frozen=True):
    """What one source's timestamps were when this export was built.

    Recorded per source rather than per run because a profile's sources change independently:
    one stale source rebuilds the profile, and this is what says which one it was.
    """

    source_modified_utc: UtcDatetime
    """T1 as it stood at build time -- the source store's own mtime, as the sidecar observed it.

    Compared with ``!=`` and not ``>``. Restoring yesterday's Parquet over today's moves its
    mtime *backwards*, and ``>`` calls that "not new" and leaves the stale export standing."""

    sidecar_created_utc: UtcDatetime
    """T2 as it stood at build time: when the sidecar describing this source was written."""


class RunReceipt(BaseModel, frozen=True):
    """One profile's completed export, as the next run's selection reads it."""

    receipt_version: ReceiptVersion = RECEIPT_VERSION
    """First, so it is readable at the head of the file, and a ``Literal`` so a future version is
    refused by validation rather than by whichever field happened to move."""

    config_id: str
    """Also the ownership marker. Stable across renaming or moving the configuration file, which
    is why it is not the path."""

    profile: str
    """Also the ownership marker: two profiles of one configuration may not share a directory."""

    excel_created_utc: UtcDatetime
    """T4."""

    config_modified_utc: UtcDatetime
    """T3 as it stood at build time. The only one of the four that crosses a clock boundary --
    T2 and T4 are both written by the pipeline, on one host, in one run -- and the only one a
    human can forget to bump, which is what ``resolved_config_hash`` exists to catch."""

    resolved_config_hash: str
    """The backstop for a forgotten T3 bump: exact, clock-free, and taken over the serialised
    model, so adding a configuration field rebuilds everything once. Blunt, correct, and cheaper
    than deciding which fields are digest-relevant."""

    planner_version: str
    """A changed allocation algorithm invalidates an export every timestamp calls current."""

    sources: dict[str, SourceStamp]
    """Keyed by source alias. A source the profile now names but this map does not is a source
    that did not exist at build time, which makes the export stale."""
