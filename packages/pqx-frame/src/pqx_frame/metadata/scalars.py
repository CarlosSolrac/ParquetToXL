"""The dtype flag helpers that column metadata records."""

from __future__ import annotations

import polars as pl


def is_nested(dtype: pl.DataType) -> bool:
    """Return whether the dtype is one of the nested dtypes this project puts out of scope.

    Args:
        dtype: The column's Polars dtype.

    Returns:
        True for ``List``, ``Array``, ``Struct`` and ``Object``.
    """
    return isinstance(dtype, pl.List | pl.Array | pl.Struct | pl.Object)


def is_numeric(dtype: pl.DataType) -> bool:
    """Return whether the dtype holds numbers.

    Delegates to Polars. Note two results that surprise people, both pinned by tests:
    ``Decimal`` is numeric, and ``Boolean`` is not.

    Args:
        dtype: The column's Polars dtype.

    Returns:
        True for the integer, unsigned-integer, float and decimal dtypes.
    """
    return dtype.is_numeric()


def is_float(dtype: pl.DataType) -> bool:
    """Return whether the dtype is a floating-point dtype.

    Args:
        dtype: The column's Polars dtype.

    Returns:
        True for ``Float32`` and ``Float64`` only. ``Decimal`` is not a float.
    """
    return dtype.is_float()


def is_integer(dtype: pl.DataType) -> bool:
    """Return whether the dtype is an integer dtype.

    Args:
        dtype: The column's Polars dtype.

    Returns:
        True for the signed and unsigned integer dtypes. ``Boolean`` is not an integer here,
        whatever Python's type hierarchy says about ``bool``.
    """
    return dtype.is_integer()


def is_decimal(dtype: pl.DataType) -> bool:
    """Return whether the dtype is a fixed-point decimal.

    Args:
        dtype: The column's Polars dtype.

    Returns:
        True for ``Decimal`` only.
    """
    return dtype.is_decimal()


def is_text(dtype: pl.DataType) -> bool:
    """Return whether the dtype holds text.

    Polars offers no predicate for this, so it is derived. ``Categorical`` and ``Enum`` both
    count: each is dictionary-encoded text, their values read back as ``str``, and the ToExcel
    conversion maps them to ``String`` precisely because they are text.

    This predicate is load-bearing rather than descriptive. ``DataframeConversionToExcel``
    routes on it, so a text dtype missing here is passed through unconverted and then refused
    by ``fast_excel_reader``, which is what an omitted ``Enum`` did: it produced a sidecar
    describing a column that could not be validated at all.

    Args:
        dtype: The column's Polars dtype.

    Returns:
        True for ``String``, ``Categorical`` and ``Enum``.
    """
    return isinstance(dtype, pl.String | pl.Categorical | pl.Enum)


def is_boolean(dtype: pl.DataType) -> bool:
    """Return whether the dtype is boolean.

    Polars offers no predicate for this, so it is derived.

    Args:
        dtype: The column's Polars dtype.

    Returns:
        True for ``Boolean`` only.
    """
    return isinstance(dtype, pl.Boolean)
