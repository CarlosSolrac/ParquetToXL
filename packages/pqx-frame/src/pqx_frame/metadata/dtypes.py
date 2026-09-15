"""A dtype vocabulary of this library's own, and conversion to and from Polars dtypes.

The sidecar has to say what a column holds in a form that outlives the Polars release that
wrote it. ``str(dtype)`` cannot: it is a display form rather than a serialization format, so
``getattr(pl, "Float64")`` resolves while ``getattr(pl, "Datetime(time_unit='us',
time_zone=None)")`` does not, and any change to Polars' repr would invalidate every file
already written.

Owning the vocabulary also means being free to leave things out. ``Categorical`` carries an
``ordering`` parameter that Polars deprecated in 1.32 -- it is always lexical now -- and a
format that mirrored Polars' dtype surface would have persisted a concept Polars has since
dropped. It is not modelled here.

This is the third vocabulary in the project with its own identity, after ``encode_value``'s
tag table and ``HashedDataframe``'s discriminated union, and it follows the same shape:
discriminated on ``kind``, extensible by adding a member. Its version rides on the sidecar's
``schema_version``, because the shape of a stored dtype is part of the shape of the document.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Annotated, Final, Literal

import polars as pl
from polars.datatypes import DataTypeClass
from pydantic import BaseModel, Field

type TimeUnit = Literal["ms", "us", "ns"]
"""Polars' three temporal resolutions, named rather than inherited."""

type SimpleKind = Literal[
    "int8",
    "int16",
    "int32",
    "int64",
    "int128",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "uint128",
    "float16",
    "float32",
    "float64",
    "boolean",
    "string",
    "binary",
    "date",
    "time",
    "null",
]
"""The dtypes that carry no parameters, so their name is the whole description.

Taken from what Polars actually exposes rather than from what the fixtures happen to use.
``Utf8`` is absent because it is an alias: a ``Utf8`` column reports ``String``."""

type PhysicalKind = Literal["uint8", "uint16", "uint32"]
"""The index widths a ``Categorical`` may use. Polars rejects every other width outright."""

PHYSICAL_POLARS_DTYPES: Final[dict[PhysicalKind, DataTypeClass]] = {"uint8": pl.UInt8, "uint16": pl.UInt16, "uint32": pl.UInt32}

_PHYSICAL_KIND_BY_POLARS: Final[dict[DataTypeClass, PhysicalKind]] = {width: kind for kind, width in PHYSICAL_POLARS_DTYPES.items()}

SIMPLE_POLARS_DTYPES: Final[dict[SimpleKind, pl.DataType]] = {
    "int8": pl.Int8(),
    "int16": pl.Int16(),
    "int32": pl.Int32(),
    "int64": pl.Int64(),
    "int128": pl.Int128(),
    "uint8": pl.UInt8(),
    "uint16": pl.UInt16(),
    "uint32": pl.UInt32(),
    "uint64": pl.UInt64(),
    "uint128": pl.UInt128(),
    "float16": pl.Float16(),
    "float32": pl.Float32(),
    "float64": pl.Float64(),
    "boolean": pl.Boolean(),
    "string": pl.String(),
    "binary": pl.Binary(),
    "date": pl.Date(),
    "time": pl.Time(),
    "null": pl.Null(),
}
"""Stored name to Polars dtype. The inverse is derived rather than written twice."""

_KIND_BY_POLARS_TYPE: Final[dict[type[pl.DataType], SimpleKind]] = {type(dtype): kind for kind, dtype in SIMPLE_POLARS_DTYPES.items()}


class ColumnDtypeBase(BaseModel, ABC, frozen=True):
    """What every stored dtype carries.

    ``kind`` is the discriminator. Subclasses narrow it to the literals they answer for, and
    ``ColumnDtype`` selects between them by it on reload. Abstract rather than a base with a
    raising body, so a member that forgets ``to_polars`` cannot be constructed at all -- the
    same shape as ``ExcelWriterBase`` and ``SidecarStoreBase``.
    """

    kind: str

    @abstractmethod
    def to_polars(self) -> pl.DataType:
        """Return the Polars dtype this describes."""


class SimpleDtype(ColumnDtypeBase, frozen=True):
    """A dtype whose name is its whole description."""

    kind: SimpleKind

    def to_polars(self) -> pl.DataType:
        """Return the Polars dtype this describes."""
        return SIMPLE_POLARS_DTYPES[self.kind]


class DatetimeDtype(ColumnDtypeBase, frozen=True):
    """A timestamp, with its resolution and optional zone.

    ``time_unit`` has no default: a missing one is a damaged file rather than a request for
    microseconds, and silently picking a resolution would move every digest taken from the
    column. ``time_zone`` does default, because ``None`` is what a naive column genuinely is.
    """

    kind: Literal["datetime"] = "datetime"
    time_unit: TimeUnit
    time_zone: str | None = None

    def to_polars(self) -> pl.DataType:
        """Return the Polars dtype this describes."""
        return pl.Datetime(self.time_unit, self.time_zone)


class DurationDtype(ColumnDtypeBase, frozen=True):
    """An elapsed time, with its resolution."""

    kind: Literal["duration"] = "duration"
    time_unit: TimeUnit

    def to_polars(self) -> pl.DataType:
        """Return the Polars dtype this describes."""
        return pl.Duration(self.time_unit)


class DecimalDtype(ColumnDtypeBase, frozen=True):
    """A fixed-point decimal.

    Both parameters are required and both are integers. Polars accepts ``precision=None`` at
    construction but normalizes it to 38 on any real column, so a stored ``None`` could only
    ever describe a column that does not exist.
    """

    kind: Literal["decimal"] = "decimal"
    precision: int
    scale: int

    def to_polars(self) -> pl.DataType:
        """Return the Polars dtype this describes."""
        return pl.Decimal(self.precision, self.scale)


class CategoricalDtype(ColumnDtypeBase, frozen=True):
    """Dictionary-encoded text, with the category set it draws on.

    ``Categorical`` looked parameterless because a default one prints as ``Categorical``, but
    it names an entry in a process-global category registry and an index width, and both
    survive a Parquet round trip. A column built on ``Categories("x", physical=UInt8)`` is a
    different dtype from a default one, so flattening the two would have made ``to_polars()``
    return something the source frame never held.

    The deprecated ``ordering`` is still not modelled; it is always lexical since Polars 1.32.
    """

    kind: Literal["categorical"] = "categorical"
    name: str = ""
    namespace: str = ""
    physical: PhysicalKind = "uint32"

    def to_polars(self) -> pl.DataType:
        """Return the Polars dtype this describes."""
        return pl.Categorical(pl.Categories(self.name, namespace=self.namespace, physical=PHYSICAL_POLARS_DTYPES[self.physical]))


class EnumDtype(ColumnDtypeBase, frozen=True):
    """Text drawn from a closed, ordered set of categories.

    The categories are the dtype, not data: two ``Enum`` columns with different category
    lists are different dtypes, and the order is part of the identity.
    """

    kind: Literal["enum"] = "enum"
    categories: list[str]

    def to_polars(self) -> pl.DataType:
        """Return the Polars dtype this describes."""
        return pl.Enum(self.categories)


class ExtensionDtype(ColumnDtypeBase, frozen=True):
    """A user-defined type wrapping a storage dtype.

    ``storage`` is itself a ``ColumnDtype``, which is what keeps nested types out: Polars
    allows ``Extension("t", List(Int64))`` and reports ``is_nested()`` as ``False`` for it, so
    an extension is the one way a nested dtype can present itself as scalar. Describing the
    storage recursively means such a column is refused by the same rule as a bare ``List``.

    Reachable through the documented entry point: an unregistered extension degrades to its
    storage dtype on a Parquet round trip today, but survives intact under
    ``POLARS_UNKNOWN_EXTENSION_TYPE_BEHAVIOR=load_as_extension``, which Polars 2.0 makes the
    default.
    """

    kind: Literal["extension"] = "extension"
    name: str
    storage: ColumnDtype
    metadata: str | None = None

    def to_polars(self) -> pl.DataType:
        """Return the Polars dtype this describes."""
        # A generic pl.Extension even when a subclass was registered under this name: Polars
        # compares extensions by name and storage rather than by Python class, so the result
        # equals the registered subclass it came from.
        return pl.Extension(self.name, self.storage.to_polars(), self.metadata)


type ColumnDtype = Annotated[SimpleDtype | DatetimeDtype | DurationDtype | DecimalDtype | CategoricalDtype | EnumDtype | ExtensionDtype, Field(discriminator="kind")]
"""Every dtype a scalar column can have, selected by ``kind`` on reload.

Extensible without touching consumers: a new parameterised dtype is a new member here and
nowhere else, exactly as a second hasher is a new member of ``HashedDataframe``.
"""


def dtype_from_polars(dtype: pl.DataType) -> ColumnDtype:
    """Describe a Polars dtype in this library's vocabulary.

    Args:
        dtype: The column's Polars dtype.

    Returns:
        The stored description, which ``to_polars()`` turns back into ``dtype``.

    Raises:
        TypeError: The dtype is nested, or is one this vocabulary does not name.
    """
    if isinstance(dtype, pl.Datetime):
        return DatetimeDtype(time_unit=dtype.time_unit, time_zone=dtype.time_zone)
    if isinstance(dtype, pl.Duration):
        return DurationDtype(time_unit=dtype.time_unit)
    if isinstance(dtype, pl.Decimal):
        return DecimalDtype(precision=dtype.precision, scale=dtype.scale)
    if isinstance(dtype, pl.BaseExtension):
        # Matched on the base, not on pl.Extension: a registered extension type comes back
        # from Parquet as its own registered subclass, which is a BaseExtension but not a
        # pl.Extension, and matching the concrete class would refuse exactly the extensions
        # someone cared enough about to register.
        #
        # Before the table: an extension's exact type is never in it, and its storage dtype
        # carries the description that matters. ext_storage() hands back whatever was passed
        # to the constructor, so a parameterless storage arrives as the class rather than an
        # instance and has to be constructed before it can be described.
        declared: pl.DataType | DataTypeClass = dtype.ext_storage()
        storage: pl.DataType = declared() if isinstance(declared, type) else declared
        return ExtensionDtype(name=dtype.ext_name(), storage=dtype_from_polars(storage), metadata=dtype.ext_metadata())
    if isinstance(dtype, pl.Enum):
        return EnumDtype(categories=dtype.categories.to_list())
    if isinstance(dtype, pl.Categorical):
        # Keyed rather than searched: Polars accepts only the three widths in the table --
        # it raises on anything else -- so there is no reachable miss to guard against, and a
        # guard for one could not be tested. A KeyError naming the width is the right failure
        # if a future Polars widens the set.
        # physical() is declared as "a dtype or a dtype class" and returns the class in
        # practice; normalised so the lookup is correct either way.
        declared_width: pl.DataType | DataTypeClass = dtype.categories.physical()
        width: DataTypeClass = declared_width if isinstance(declared_width, DataTypeClass) else type(declared_width)
        return CategoricalDtype(
            name=dtype.categories.name(),
            namespace=dtype.categories.namespace(),
            physical=_PHYSICAL_KIND_BY_POLARS[width],
        )
    kind: SimpleKind | None = _KIND_BY_POLARS_TYPE.get(type(dtype))
    if kind is None:
        message: str = f"no stored dtype for {dtype}; nested dtypes are out of scope and every other scalar dtype should be named here"
        raise TypeError(message)
    return SimpleDtype(kind=kind)


ExtensionDtype.model_rebuild()
