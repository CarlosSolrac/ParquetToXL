"""The persisted document: a schema version wrapped around one metadata record."""

from __future__ import annotations

from typing import Final

from pqx_frame.metadata.dataframe import DataframeMetadata
from pqx_frame.timestamps import UtcDatetime
from pydantic import BaseModel

SIDECAR_SCHEMA_VERSION: Final = 3
"""The schema this build writes, and the only one it reads.

Version 3 adds ``created_utc`` -- **T2**, when the sidecar was written -- as distinct from
``metadata.modified_utc``, which is the *source's* mtime as observed. A version 2 file is refused
rather than migrated, following the precedent v1 set: there is no honest value to migrate to,
since when a v2 sidecar was written is exactly what it does not record, and inventing one would
put a fabricated instant into the staleness comparison it exists to drive. Every existing sidecar
stops loading and is regenerated on the next run, which the selection rule already handles: a
missing or unreadable sidecar is stale by definition.

Version 2 replaced the column's ``polars_dtype`` string with the structured ``dtype`` of
``metadata.dtypes``. A version 1 file is refused rather than read: its dtype field is a
different shape under the same name, so reading it would need a parser for the display form
this version exists to stop relying on.

Separate from every other version number in the project, and deliberately so. The hashers'
``version`` is welded to the ``encode_value`` tag table, where a change invalidates stored
digests; a conversion's ``version`` names its data rules. This one describes the shape of a
file. Folding it into either would mean a digest version bump every time the file layout
moved.
"""


class SidecarDocument(BaseModel, frozen=True):
    """One metadata record, as it is stored on disk.

    A thin wrapper rather than a reshaped copy of ``DataframeMetadata``: every field of that
    model survives a JSON round trip unchanged, so there is nothing to project. That is only
    true since the column extremes were removed -- they were typed as a union containing
    ``str``, which made Pydantic reload binary, temporal and decimal values as strings and
    fail outright on non-UTF-8 bytes.

    ``schema_version`` is first so it is readable at the head of the file, and is checked
    before the rest is validated, so a future format fails with a version error rather than
    an obscure complaint about a field that moved.
    """

    schema_version: int = SIDECAR_SCHEMA_VERSION
    created_utc: UtcDatetime
    """**T2**: when this sidecar was written, on the pipeline's clock.

    Required, with no default. The alternative -- defaulting to now -- would make a document
    constructed for any other reason stamp itself with an instant nothing measured, and that
    instant feeds ``excel_stale``: ``T2(s) > T4`` is what says a source was re-described after the
    export. A fabricated T2 there is a rebuild that never happens or one that never stops.

    Distinct from ``metadata.modified_utc``, which is the source's own mtime as observed. Two
    clocks, and they answer different questions: that one says when the data changed, this one
    says when we last looked.
    """

    metadata: DataframeMetadata
