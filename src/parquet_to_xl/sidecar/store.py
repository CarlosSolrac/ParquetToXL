"""The sidecar store registry, and the JSON store registered in it."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from parquet_to_xl.sidecar.document import SIDECAR_SCHEMA_VERSION, SidecarDocument

if TYPE_CHECKING:
    from upath import UPath

    from parquet_to_xl.metadata.dataframe import DataframeMetadata

type JsonValue = dict[str, object] | list[object] | str | int | float | bool | None
"""Everything ``json.loads`` can return.

Declared so the result can be narrowed by ``isinstance`` into a genuinely typed mapping.
``json.loads`` is annotated ``Any``, and an ``object`` declaration narrows only to
``dict[Unknown, Unknown]``, whose ``get`` is itself unknown under pyright strict.
"""

SIDECAR_SUFFIX: str = ".json"
"""Appended to the source file's whole name, so ``sales.parquet`` gains ``.parquet.json``."""


def sidecar_path(source_path: UPath) -> UPath:
    """Return where the sidecar for ``source_path`` belongs.

    The suffix is appended to the whole name rather than replacing the existing one, so
    ``sales.parquet`` gains ``sales.parquet.json``. Replacing it would make ``sales.parquet``
    and ``sales.csv`` claim the same sidecar, and would make a genuine ``sales.json`` sitting
    in the directory indistinguishable from one of ours.

    Args:
        source_path: The file the metadata describes. Not read, and need not exist.

    Returns:
        The sidecar location, in the same directory and with the same storage options.
    """
    return source_path.with_name(source_path.name + SIDECAR_SUFFIX)


class SidecarStoreBase(ABC):
    """One way of persisting a metadata record beside the file it describes."""

    identifier: ClassVar[str]

    @abstractmethod
    def write(self, metadata: DataframeMetadata, source_path: UPath) -> UPath:
        """Persist ``metadata`` beside ``source_path`` and return where it was written."""

    @abstractmethod
    def read(self, source_path: UPath) -> SidecarDocument:
        """Load the document stored beside ``source_path``."""


SIDECAR_STORES: dict[str, type[SidecarStoreBase]] = {}
"""Identifier to store class. Populated by ``register_sidecar_store`` at import time."""


def register_sidecar_store(cls: type[SidecarStoreBase]) -> type[SidecarStoreBase]:
    """Register a store class under its own ``identifier``.

    Args:
        cls: The store class. Its ``identifier`` must not already be registered.

    Returns:
        ``cls`` unchanged, so this works as a decorator.

    Raises:
        ValueError: Another class is already registered under that identifier. Silently
            replacing it would make the winner depend on import order.
    """
    if cls.identifier in SIDECAR_STORES:
        message: str = f"a sidecar store is already registered as {cls.identifier!r}"
        raise ValueError(message)
    SIDECAR_STORES[cls.identifier] = cls
    return cls


def get_sidecar_store(identifier: str) -> SidecarStoreBase:
    """Return a new instance of the store registered under ``identifier``.

    Args:
        identifier: The registered name, e.g. ``"json"``.

    Returns:
        A fresh instance. Stores hold no state, so instances are interchangeable.

    Raises:
        KeyError: No store is registered under that identifier. The message lists what is,
            because the usual cause is a typo in configuration.
    """
    if identifier not in SIDECAR_STORES:
        message: str = f"unknown sidecar store {identifier!r}; registered: {sorted(SIDECAR_STORES)}"
        raise KeyError(message)
    return SIDECAR_STORES[identifier]()


@register_sidecar_store
class JsonSidecarStore(SidecarStoreBase):
    """Stores the document as indented UTF-8 JSON.

    Indented rather than compact because a sidecar is read by people as often as by
    programs -- a digest mismatch is diagnosed by looking at the two files -- and the cost
    is whitespace in a file already dominated by hex digests.

    Reads and writes go through ``UPath``, so this store works against any filesystem
    ``fsspec`` serves. That makes it the one component here that is not restricted to local
    paths: the Excel writers and reader all hand ``str(path)`` to a library that opens it as
    a local filename.
    """

    identifier: ClassVar[str] = "json"

    def write(self, metadata: DataframeMetadata, source_path: UPath) -> UPath:
        """Persist ``metadata`` beside ``source_path`` and return where it was written.

        Args:
            metadata: The record to store.
            source_path: The file it describes. Not read; only its name is used.

        Returns:
            The path written, which is ``sidecar_path(source_path)``.
        """
        target: UPath = sidecar_path(source_path)
        # Serialised in full before the destination is opened, so a failure to serialise
        # cannot leave a truncated file where a good one was.
        text: str = SidecarDocument(metadata=metadata).model_dump_json(indent=2)
        target.write_text(text, encoding="utf-8")
        return target

    def read(self, source_path: UPath) -> SidecarDocument:
        """Load the document stored beside ``source_path``.

        The version is checked before the body is validated. A future format would otherwise
        fail with whatever field happened to move first, which names the wrong problem.

        Args:
            source_path: The file the sidecar describes.

        Returns:
            The stored document.

        Raises:
            FileNotFoundError: No sidecar exists beside ``source_path``.
            ValueError: The file is not valid JSON, does not hold an object, or declares a
                schema version this build does not read.
        """
        target: UPath = sidecar_path(source_path)
        text: str = target.read_text(encoding="utf-8")
        loaded: JsonValue
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as error:
            message: str = f"{target} is not valid JSON: {error}"
            raise ValueError(message) from error
        if not isinstance(loaded, dict):
            message = f"{target} does not hold a JSON object"
            raise ValueError(message)
        version: object = loaded.get("schema_version")
        if version != SIDECAR_SCHEMA_VERSION:
            message = f"{target} declares schema_version {version!r}; this build reads only {SIDECAR_SCHEMA_VERSION}"
            raise ValueError(message)
        return SidecarDocument.model_validate(loaded)
