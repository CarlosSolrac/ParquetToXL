"""The conversion interface and the record one conversion produces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from parquet_to_xl.metadata.builder import build_columns_metadata
from parquet_to_xl.metadata.columns import ConversionIdentity, DataframeColumnsMetadata

if TYPE_CHECKING:
    from collections.abc import Sequence

    import polars as pl

    from parquet_to_xl.hashing.base import DataFrameHasherBaseClass


@dataclass(frozen=True)
class ConvertedDataframe:
    """One conversion's output: the frame, its metadata, and whether anything moved.

    A plain frozen dataclass rather than a Pydantic model, deliberately: it holds a live
    ``pl.DataFrame``, which has no schema Pydantic could validate and which would be copied
    by any attempt to.
    """

    columns_metadata: DataframeColumnsMetadata
    converted_dataframe: pl.DataFrame
    schema_or_data_changed: bool


class DataframeConversionBaseClass(ABC):
    """A model of what some target format does to a dataframe.

    A conversion is not a writer. It answers "what would this data look like after a round
    trip through that format", so the metadata and digests recorded for it describe what the
    format will actually hold rather than what was read from Parquet.

    ``metadata_of_converted_dataframe`` is concrete and is the whole public surface:
    subclasses supply ``_convert`` and inherit the wiring. That keeps the guarantee that
    metadata is always built from the *converted* frame, which a subclass could otherwise
    get wrong.

    The four class variables identify the conversion in recorded metadata. ``version`` is
    the human-readable label and ``version_number`` the machine-comparable one; both change
    together whenever ``_convert`` starts producing different values for the same input,
    for the same reason the hash encoding is versioned.
    """

    identifier: ClassVar[str]
    version: ClassVar[str]
    version_number: ClassVar[int]
    description: ClassVar[str]

    def metadata_of_converted_dataframe(self, df: pl.DataFrame, hashers: Sequence[DataFrameHasherBaseClass]) -> ConvertedDataframe:
        """Convert the frame and describe the result.

        Args:
            df: The frame to convert. Never mutated.
            hashers: The hashers to digest the converted frame with, in order.

        Returns:
            The converted frame, its metadata, and whether the conversion changed the
            schema or any value.
        """
        converted: pl.DataFrame
        changed: bool
        converted, changed = self._convert(df)
        built: DataframeColumnsMetadata = build_columns_metadata(converted, hashers)
        # Stamped here rather than inside the builder, which also serves the source frame
        # and has no conversion to name. Copied rather than rebuilt field by field: a field
        # added to DataframeColumnsMetadata later would be silently dropped by a rebuild.
        identity: ConversionIdentity = ConversionIdentity(identifier=type(self).identifier, version=type(self).version, version_number=type(self).version_number)
        described: DataframeColumnsMetadata = built.model_copy(update={"conversion": identity})
        return ConvertedDataframe(described, converted, changed)

    @abstractmethod
    def _convert(self, df: pl.DataFrame) -> tuple[pl.DataFrame, bool]:
        """Return the converted frame and whether it differs from the input.

        Implementations must be idempotent: converting an already-converted frame returns
        an equal frame and reports ``False``. The headline round-trip test relies on it,
        because it passes both the source frame and a frame read back from a workbook
        through this method so it compares like with like.

        Args:
            df: The frame to convert. Never mutated.

        Returns:
            The converted frame, and ``True`` when the schema or any value changed.
        """
