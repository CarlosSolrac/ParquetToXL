"""Frozen tests for the neutral dtype vocabulary.

The sidecar has to record what a column holds in a form that outlives the version of Polars
that wrote it. ``str(dtype)`` could not do that: it is a display form, so a validator needed
a lookup table and any change to Polars' repr would have invalidated every file already
written. These models are the replacement, and the property that matters is the one every
test below turns on -- a Polars dtype in, real JSON bytes, the same Polars dtype out.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import polars as pl
import pytest
from polars import datatypes as polars_datatypes
from pydantic import TypeAdapter, ValidationError

from parquet_to_xl.metadata.dtypes import (
    CategoricalDtype,
    ColumnDtype,
    ColumnDtypeBase,
    DatetimeDtype,
    DecimalDtype,
    DurationDtype,
    ExtensionDtype,
    dtype_from_polars,
)

if TYPE_CHECKING:
    from pathlib import Path

ADAPTER: TypeAdapter[ColumnDtype] = TypeAdapter(ColumnDtype)
"""Validates the union the way the sidecar's models do, by ``kind``."""

EVERY_SCALAR_DTYPE: list[pl.DataType] = [
    pl.Int8(),
    pl.Int16(),
    pl.Int32(),
    pl.Int64(),
    pl.Int128(),
    pl.UInt8(),
    pl.UInt16(),
    pl.UInt32(),
    pl.UInt64(),
    pl.UInt128(),
    pl.Float16(),
    pl.Float32(),
    pl.Float64(),
    pl.Boolean(),
    pl.String(),
    pl.Binary(),
    pl.Date(),
    pl.Time(),
    pl.Null(),
    pl.Categorical(),
    pl.Categorical(pl.Categories("x", physical=pl.UInt8)),
    pl.Categorical(pl.Categories("x", namespace="ns", physical=pl.UInt16)),
    pl.Enum([]),
    pl.Enum(["a", "b"]),
    pl.Enum(["b", "a"]),
    pl.Extension("t.int", pl.Int64),
    pl.Extension("t.zoned", pl.Datetime("ns", "UTC"), "meta"),
    pl.Datetime("ms"),
    pl.Datetime("us"),
    pl.Datetime("ns"),
    pl.Datetime("us", "UTC"),
    pl.Datetime("ns", "Asia/Kolkata"),
    pl.Datetime("ms", "America/Chicago"),
    pl.Duration("ms"),
    pl.Duration("us"),
    pl.Duration("ns"),
    pl.Decimal(18, 4),
    pl.Decimal(38, 0),
    pl.Decimal(1, 1),
]
"""Every scalar dtype the project supports, with each parameter varied independently.

Taken from what Polars exposes, not from what the fixtures use. An earlier revision of this
table was built from the fixture frame's nineteen columns and so silently dropped ``Int128``,
``UInt128``, ``Float16``, ``Enum`` and ``Extension`` from the vocabulary, and flattened every
parameterised ``Categorical`` onto the default one.

The parameterised families are the whole reason this module exists, so each gets more than
one row: a bug that hardcoded ``us``, dropped the zone, or ignored a category set would pass
a table that listed each family once.
"""


@pytest.mark.parametrize("dtype", EVERY_SCALAR_DTYPE, ids=str)
def test_every_scalar_dtype_survives_a_json_round_trip(dtype: pl.DataType) -> None:
    # The whole contract in one assertion: out through JSON bytes and back to the same dtype.
    described: ColumnDtype = dtype_from_polars(dtype)
    reloaded: ColumnDtype = ADAPTER.validate_json(ADAPTER.dump_json(described))
    assert reloaded == described
    assert reloaded.to_polars() == dtype


@pytest.mark.parametrize("dtype", EVERY_SCALAR_DTYPE, ids=str)
def test_the_union_reloads_as_the_right_class(dtype: pl.DataType) -> None:
    # Discriminated on kind, like HashedDataframe. A union that matched structurally would
    # reload a datetime as whichever member happened to accept its fields.
    described: ColumnDtype = dtype_from_polars(dtype)
    reloaded: ColumnDtype = ADAPTER.validate_json(ADAPTER.dump_json(described))
    assert type(reloaded) is type(described)


def test_the_parameters_are_recorded_not_defaulted() -> None:
    zoned: ColumnDtype = dtype_from_polars(pl.Datetime("ns", "Asia/Kolkata"))
    assert isinstance(zoned, DatetimeDtype)
    assert (zoned.time_unit, zoned.time_zone) == ("ns", "Asia/Kolkata")

    naive: ColumnDtype = dtype_from_polars(pl.Datetime("ms"))
    assert isinstance(naive, DatetimeDtype)
    assert (naive.time_unit, naive.time_zone) == ("ms", None)

    elapsed: ColumnDtype = dtype_from_polars(pl.Duration("ns"))
    assert isinstance(elapsed, DurationDtype)
    assert elapsed.time_unit == "ns"

    fixed: ColumnDtype = dtype_from_polars(pl.Decimal(18, 4))
    assert isinstance(fixed, DecimalDtype)
    assert (fixed.precision, fixed.scale) == (18, 4)


def test_a_parameterised_categorical_is_not_flattened_onto_the_default() -> None:
    # A Categorical names an entry in a global category registry and an index width, and both
    # survive a Parquet round trip, so the two are genuinely different dtypes. Treating
    # Categorical as parameterless made to_polars() return a dtype the source never held.
    narrow: pl.DataType = pl.Categorical(pl.Categories("x", physical=pl.UInt8))
    assert narrow != pl.Categorical()
    assert dtype_from_polars(narrow) != dtype_from_polars(pl.Categorical())
    assert dtype_from_polars(narrow).to_polars() == narrow


def test_enum_carries_its_categories_in_order() -> None:
    # The categories are the dtype rather than data, and their order is part of its identity.
    assert dtype_from_polars(pl.Enum(["a", "b"])) != dtype_from_polars(pl.Enum(["b", "a"]))
    assert dtype_from_polars(pl.Enum(["a", "b"])).to_polars() == pl.Enum(["a", "b"])
    assert dtype_from_polars(pl.Enum([])).to_polars() == pl.Enum([])


def test_enum_and_categorical_are_different_kinds() -> None:
    # Unrelated classes in Polars, and dictionary-encoded text in different ways.
    assert dtype_from_polars(pl.Enum(["a"])).kind != dtype_from_polars(pl.Categorical()).kind


def test_the_vocabulary_covers_every_non_nested_dtype_polars_exposes() -> None:
    # The guard against the mistake that produced this test: a vocabulary enumerated from the
    # fixtures rather than from Polars. Utf8 is excluded as an alias -- a Utf8 column reports
    # String -- and the abstract bases and nested types are out of scope.
    # Utf8 is an alias -- a Utf8 column reports String. BaseExtension is no longer skipped:
    # dispatch matches it, and an Extension sample is an instance of it.
    skip: set[str] = {
        "DataType",
        "DataTypeClass",
        "NestedType",
        "TemporalType",
        "NumericType",
        "IntegerType",
        "SignedIntegerType",
        "UnsignedIntegerType",
        "FloatType",
        "DecimalType",
        "List",
        "Array",
        "Struct",
        "Object",
        "Field",
        "Unknown",
        "Utf8",
    }
    name: str
    for name in dir(polars_datatypes):
        candidate: object = getattr(polars_datatypes, name, None)
        if not isinstance(candidate, type) or not issubclass(candidate, pl.DataType) or name.startswith("_") or name in skip:
            continue
        assert any(isinstance(sample, candidate) for sample in EVERY_SCALAR_DTYPE), f"{name} is not covered by EVERY_SCALAR_DTYPE"


def test_an_extension_describes_its_storage_recursively() -> None:
    # An extension is a user-defined type over a storage dtype, so the storage is what a
    # consumer actually needs; it is described with the same vocabulary rather than a second.
    described: ColumnDtype = dtype_from_polars(pl.Extension("t.zoned", pl.Datetime("ns", "UTC"), "meta"))
    assert isinstance(described, ExtensionDtype)
    assert described.name == "t.zoned"
    assert described.metadata == "meta"
    assert described.storage.to_polars() == pl.Datetime("ns", "UTC")


class _RegisteredExtension(pl.BaseExtension):
    """A registered extension type, which is what Polars hands back from Parquet."""


@pytest.fixture
def registered_extension() -> Iterator[type[pl.BaseExtension]]:
    """Register an extension type for one test, and take it out again.

    Registration is process-global, so leaving it in place would let this test change what a
    later one sees.
    """
    pl.register_extension_type("test.registered", _RegisteredExtension)
    yield _RegisteredExtension
    pl.unregister_extension_type("test.registered")


def test_a_registered_extension_subclass_is_described_like_any_other(registered_extension: type[pl.BaseExtension]) -> None:
    # A registered type returns from Parquet as its own subclass, which is a BaseExtension but
    # not a pl.Extension. Dispatching on the concrete class refused exactly the extensions
    # someone had cared enough to register.
    subclass: pl.DataType = registered_extension("test.registered", pl.Int64())
    assert isinstance(subclass, pl.BaseExtension)
    assert not isinstance(subclass, pl.Extension)

    described: ColumnDtype = dtype_from_polars(subclass)
    assert isinstance(described, ExtensionDtype)
    assert described.name == "test.registered"
    # Reconstructs as a generic pl.Extension, which Polars still considers equal: extensions
    # compare by name and storage, not by Python class.
    assert described.to_polars() == subclass


def test_an_extension_wrapping_a_nested_dtype_is_refused() -> None:
    # The one way a nested dtype can present itself as scalar: Polars allows this and reports
    # is_nested() as False for it, so only describing the storage recursively catches it.
    wrapper: pl.DataType = pl.Extension("t.list", pl.List(pl.Int64()))
    assert wrapper.is_nested() is False
    with pytest.raises(TypeError, match="nested dtypes are out of scope"):
        dtype_from_polars(wrapper)


def test_categorical_is_its_own_kind_rather_than_string() -> None:
    # ToExcel maps Categorical to String, but the source record describes the frame as read,
    # where the two are different dtypes -- and is_text already distinguishes them.
    assert dtype_from_polars(pl.Categorical()) != dtype_from_polars(pl.String())
    assert dtype_from_polars(pl.Categorical()).to_polars() == pl.Categorical()


def test_categorical_ordering_is_deliberately_not_modelled() -> None:
    # Polars deprecated the ordering parameter in 1.32 -- it is always lexical now. Declining
    # to carry it is the point of having our own vocabulary: a format that mirrored Polars'
    # dtype surface would have persisted a concept Polars has since dropped.
    assert "ordering" not in CategoricalDtype.model_fields


def test_a_nested_dtype_is_refused() -> None:
    # Out of scope per the spec, and refused here rather than described as something else.
    with pytest.raises(TypeError, match="nested dtypes are out of scope"):
        dtype_from_polars(pl.List(pl.Int64()))


def test_an_unknown_kind_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"kind": "int256"})


def test_a_datetime_without_its_unit_is_refused() -> None:
    # time_unit has no default on purpose: a missing one is a damaged file, not a request
    # for microseconds, and silently picking a resolution would move every digest.
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"kind": "datetime", "time_zone": "UTC"})


def test_the_models_are_frozen() -> None:
    described: ColumnDtype = dtype_from_polars(pl.Datetime("us", "UTC"))
    attribute: str = "time_unit"
    with pytest.raises(ValidationError):
        setattr(described, attribute, "ns")


def test_the_base_class_is_abstract() -> None:
    # A member that did not answer to_polars would otherwise reach the union and fail only
    # when something tried to read the column it describes.
    with pytest.raises(TypeError):
        ColumnDtypeBase(kind="int8")  # type: ignore[abstract]


def test_the_vocabulary_covers_every_dtype_the_fixtures_use(fixture_files: dict[str, Path]) -> None:
    # Guards the table against a Polars upgrade that adds a scalar dtype the fixtures pick up
    # while this module quietly stops describing it.
    #
    # Takes the path from the session fixture rather than naming it: tests/fixtures/data is
    # gitignored and built on demand, so a hardcoded path makes this module pass only when
    # some other test has already run and generated it.
    frame: pl.DataFrame = pl.read_parquet(fixture_files["parquet_a"])
    dtype: pl.DataType
    for dtype in frame.schema.values():
        assert dtype_from_polars(dtype).to_polars() == dtype
