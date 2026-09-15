"""The resolved configuration's fingerprint: the backstop for a forgotten ``config_modified_utc``.

``config_modified_utc`` (T3) is the only one of the pipeline's four timestamps a human maintains,
and the only one that can be wrong while looking right: edit a naming template, forget to bump T3,
and every staleness comparison says the export is current while it would now produce different
filenames. ``resolved_config_hash`` is what catches that -- exact, clock-free, and independent of
anyone remembering anything.

It is deliberately **blunt**. The hash covers the whole serialised configuration rather than a
chosen subset of digest-relevant fields, so adding a field rebuilds everything once. That is
cheaper than maintaining a list of which fields matter, and much cheaper than being wrong about
one: a field wrongly excluded produces a stale export that every check calls current.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

from pqx_plan.config import ExportConfig

__all__ = ["FINGERPRINT_ALGORITHM", "canonical_json", "resolved_config_hash"]

FINGERPRINT_ALGORITHM: Final[str] = "sha256"
"""Hex-encoded SHA-256, 64 characters.

Not the dataframe hasher. That one is versioned, tuned for speed over large frames, and part of
the digest contract the sidecar and the manifest share; this is a fingerprint of a small JSON
document, where a stable, universally available algorithm matters more than throughput and where
sharing the data hasher's version would make a hasher upgrade look like a configuration change.
"""


def canonical_json(document: dict[str, Any]) -> str:
    """Return a JSON document in the one spelling this project hashes.

    Stability is the whole point, so every choice here removes a way for the same configuration to
    produce different text:

    - ``sort_keys`` makes key order irrelevant, so reordering fields in the file does not rebuild.
    - Compact separators make whitespace irrelevant, so reformatting does not rebuild.
    - ``ensure_ascii=False`` keeps a non-ASCII value one character rather than an escape, so the
      text does not depend on which writer produced it.

    Args:
        document: A JSON-serialisable mapping, already in ``mode="json"`` form.

    Returns:
        The canonical text.
    """
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def resolved_config_hash(config: ExportConfig) -> str:
    """Return the fingerprint of a resolved configuration.

    Args:
        config: The configuration, after structural and semantic validation.

    Returns:
        64 hexadecimal characters.
    """
    document: dict[str, Any] = config.model_dump(mode="json", by_alias=True)
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
