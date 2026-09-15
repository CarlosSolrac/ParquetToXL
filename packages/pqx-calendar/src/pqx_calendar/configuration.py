"""The base every configuration model in this project derives from.

Here, in the lowest layer that has one, because two packages mirror parts of
``docs/export-config.schema.json`` -- ``pqx-calendar`` owns ``dateColumn`` and ``pqx-plan`` owns the
rest -- and the rules about how those models *serialise* have to be the same in both. ``pqx-plan``
already depends on this package, so one statement costs no new edge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, model_serializer

if TYPE_CHECKING:
    from pydantic import SerializerFunctionWrapHandler

__all__ = ["ConfigurationModel"]


class ConfigurationModel(BaseModel, extra="forbid", frozen=True, serialize_by_alias=True):
    """Shared configuration for every model mirroring the export-configuration schema.

    ``extra="forbid"`` mirrors the schema's ``additionalProperties: false`` and
    ``unevaluatedProperties: false``, which it sets on every object: an unknown field is rejected
    rather than ignored, because a misspelled key that takes effect as a default is invisible.

    ``frozen`` because the resolved configuration is hashed, and a mutable field would let the hash
    and the value disagree.

    ``serialize_by_alias`` because one field has an alias it cannot be spelled without -- ``$schema``
    is not a Python identifier -- and without it a dump writes the *field* name, which validation
    then refuses under ``extra="forbid"``. The model would not round-trip through its own
    serialisation, and every caller would have to remember ``by_alias=True`` forever.

    All three are repeated as class keywords on every subclass rather than left to inheritance:
    Pydantic carries the config down, but a type checker reads a non-frozen subclass of a frozen
    base as an error, and the keywords are the only thing that tells it otherwise.
    """

    omit_when_absent: ClassVar[tuple[str, ...]] = ()
    """Serialised keys the schema **forbids** when the value is absent, rather than merely allowing
    a null.

    Two fields are like this, and neither is the same as an ordinary optional. ``$schema`` is typed
    as a string, so a null fails it. ``two_digit_year_window_start`` is forbidden outright beside a
    four-digit format -- which is the entire point of that rule, since a setting that silently does
    nothing is the configuration bug that survives for years.

    ``scratch_root`` is the counterexample and stays: its null *is* the value, written down rather
    than left out so that "no scratch root" and "someone forgot" are different documents.

    Without this, a configuration written back out by the models is one the schema refuses. That
    matters because ``RunManifest.resolved_config`` embeds a configuration and a web editor reads
    it back, and it is how this was found: the manifest dumps the whole thing in one call.
    """

    @model_serializer(mode="wrap")
    def _omit_keys_the_schema_forbids_when_absent(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Drop each of :attr:`omit_when_absent` whose value is ``None``.

        Args:
            handler: Pydantic's own serialisation, which this wraps.

        Returns:
            The serialised model, without the keys its schema would refuse.
        """
        dumped: dict[str, Any] = handler(self)
        key: str
        for key in self.omit_when_absent:
            if dumped.get(key) is None:
                dumped.pop(key, None)
        return dumped
