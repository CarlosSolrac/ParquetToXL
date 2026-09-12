"""The identity conversion, which models storing the frame unchanged."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from parquet_to_xl.conversion.base import DataframeConversionBaseClass

if TYPE_CHECKING:
    import polars as pl


class DataframeConversionNone(DataframeConversionBaseClass):
    """Converts nothing, so the recorded metadata describes the source frame as it is.

    It exists so that the source frame travels the same code path as every other
    conversion: callers pass a list of conversions and get a list of results, without a
    special case for "no conversion". It is also the baseline the other conversions are
    read against -- a digest recorded here is the one an unmodified round trip must
    reproduce.
    """

    identifier: ClassVar[str] = "none"
    version: ClassVar[str] = "1.0"
    version_number: ClassVar[int] = 1
    description: ClassVar[str] = "Identity conversion; the frame is recorded exactly as read."

    def _convert(self, df: pl.DataFrame) -> tuple[pl.DataFrame, bool]:
        """Return the frame unchanged.

        Trivially idempotent, and returns the same object rather than a copy: nothing here
        mutates, and the caller is documented not to.

        Args:
            df: The frame to convert.

        Returns:
            The frame itself, and ``False``.
        """
        return df, False
