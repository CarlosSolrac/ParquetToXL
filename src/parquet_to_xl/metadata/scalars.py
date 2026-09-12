"""The scalar value type and the dtype flag helpers that column metadata records."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl

type ColumnScalar = float | int | str | bool | bytes | dt.datetime | dt.date | dt.time | dt.timedelta | Decimal
"""Every Python type a scalar Polars column yields when a value is read out of it.

Deliberately excludes the nested dtypes (``List``, ``Struct``, ``Array``, ``Object``), which
the spec puts out of scope. ``None`` is not a member: a missing value is modelled as
``ColumnScalar | None`` at each use site, so that nullability stays visible in the signature.
"""


def as_column_scalar(value: object) -> ColumnScalar | None:
    """Return a value as a ``ColumnScalar``, refusing anything outside that set.

    Polars declares ``Series.min()`` as returning its ``PythonLiteral``, which also admits
    an ndarray and a list -- the nested dtypes this project puts out of scope. Narrowing by
    an explicit runtime check rather than a cast means such a value fails loudly here rather
    than being recorded as something no consumer of the metadata could interpret.

    In practice Polars usually refuses first: ``min()`` on a ``List`` column raises
    ``InvalidOperationError`` before this is reached. The check is the non-matching half of
    a narrowing the type checkers require, and raising is better than silently reporting
    ``None``, which would claim a column has no extreme when it has one.

    Args:
        value: A value read out of a column, typically an extreme.

    Returns:
        The value unchanged, or ``None`` when there was no value to report.

    Raises:
        TypeError: The value is not one of the ``ColumnScalar`` types.
    """
    if value is None:
        return None
    if isinstance(value, bool | int | float | str | bytes | dt.datetime | dt.date | dt.time | dt.timedelta | Decimal):
        return value
    raise TypeError(f"value of type {type(value).__name__} is not a ColumnScalar; nested dtypes are out of scope")


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

    Polars offers no predicate for this, so it is derived. ``Categorical`` counts as text:
    it is dictionary-encoded text, its values read back as ``str``, and the ToExcel
    conversion maps it to ``String`` precisely because it is text.

    Args:
        dtype: The column's Polars dtype.

    Returns:
        True for ``String`` and ``Categorical``.
    """
    return isinstance(dtype, pl.String | pl.Categorical)


def is_boolean(dtype: pl.DataType) -> bool:
    """Return whether the dtype is boolean.

    Polars offers no predicate for this, so it is derived.

    Args:
        dtype: The column's Polars dtype.

    Returns:
        True for ``Boolean`` only.
    """
    return isinstance(dtype, pl.Boolean)
