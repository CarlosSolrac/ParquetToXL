"""The persisted document: a schema version wrapped around one metadata record."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

from parquet_to_xl.metadata.dataframe import DataframeMetadata

SIDECAR_SCHEMA_VERSION: Final = 2
"""The schema this build writes, and the only one it reads.

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
    metadata: DataframeMetadata
