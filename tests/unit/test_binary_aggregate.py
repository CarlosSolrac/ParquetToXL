"""Frozen tests for the hash record models and the additive xxh3-128 hasher.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it: everything downstream assumes the test is the specification.
"""

from __future__ import annotations

import inspect

import polars as pl
import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from parquet_to_xl.hashing import HashedDataframe
from parquet_to_xl.hashing.base import DataFrameHasherBaseClass
from parquet_to_xl.hashing.binary_aggregate import MODULUS, BinaryAggregateHashedDataframe, DataFrameHasherBinaryAggregateHash


def _frame() -> pl.DataFrame:
    return pl.DataFrame({"n": [1.0, 2.0, 3.0, 4.0], "s": ["a", "b", "c", "d"], "i": [10, 20, 30, 40]})


# ---- the record model -------------------------------------------------------------


def test_digest_hex_must_be_32_lowercase_hex_characters() -> None:
    good: BinaryAggregateHashedDataframe = BinaryAggregateHashedDataframe(scope="column", digest_hex="0" * 32)
    assert good.digest_hex == "0" * 32
    bad: str
    for bad in ["A" * 32, "0" * 31, "0" * 33, "z" * 32]:
        with pytest.raises(ValidationError):
            BinaryAggregateHashedDataframe(scope="column", digest_hex=bad)


def test_record_defaults_and_immutability() -> None:
    record: BinaryAggregateHashedDataframe = BinaryAggregateHashedDataframe(scope="dataframe", digest_hex="a" * 32)
    assert record.identifier == "binary-aggregate-xxh3-128"
    assert record.version == 1
    assert record.bit_width == 128
    assert BinaryAggregateHashedDataframe.model_config.get("frozen") is True
    # The attribute name is held in a variable deliberately. Now that frozen is declared on
    # the subclass, both checkers reject a direct assignment statically, but the point here
    # is that pydantic also refuses it at runtime -- which is what protects a digest record
    # that has already been written into metadata.
    attribute: str = "version"
    with pytest.raises(ValidationError):
        setattr(record, attribute, 2)


def test_discriminated_union_round_trips_by_identifier() -> None:
    adapter: TypeAdapter[BinaryAggregateHashedDataframe] = TypeAdapter(HashedDataframe)
    original: BinaryAggregateHashedDataframe = BinaryAggregateHashedDataframe(scope="column", digest_hex="b" * 32)
    dumped: dict[str, object] = original.model_dump()
    assert dumped["identifier"] == "binary-aggregate-xxh3-128"
    assert adapter.validate_python(dumped) == original


def test_union_works_as_a_field_type_so_consumers_need_not_change() -> None:
    # DataframeColumnMetadata will declare hashes: list[HashedDataframe]; this pins that
    # the one-member union is usable that way and widens without touching consumers.
    class Holder(BaseModel, frozen=True):
        hashes: list[HashedDataframe]

    holder: Holder = Holder(hashes=[BinaryAggregateHashedDataframe(scope="column", digest_hex="c" * 32)])
    assert Holder.model_validate(holder.model_dump()) == holder


# ---- the hasher -------------------------------------------------------------------


def test_column_digest_is_lowercase_hex_and_scoped() -> None:
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    result: BinaryAggregateHashedDataframe = hasher.hash_column(pl.Series("n", [1.0, 2.0]))
    assert result.scope == "column"
    assert len(result.digest_hex) == 32
    assert result.digest_hex == result.digest_hex.lower()
    assert int(result.digest_hex, 16) < MODULUS


def test_empty_column_digests_to_zero_rather_than_failing() -> None:
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    assert hasher.hash_column(pl.Series("n", [], dtype=pl.Float64)).digest_hex == "0" * 32


def test_row_order_does_not_change_the_digest() -> None:
    # The property the whole design exists for: the headline test reassembles two
    # workbooks in the wrong order and still has to match.
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    values: list[float] = [1.0, 2.0, 3.0, 4.0, 5.0]
    shuffled: list[float] = [3.0, 5.0, 1.0, 4.0, 2.0]
    assert sorted(shuffled) == sorted(values)
    assert shuffled != values
    assert hasher.hash_column(pl.Series("n", values)).digest_hex == hasher.hash_column(pl.Series("n", shuffled)).digest_hex


def test_one_changed_cell_changes_the_digest() -> None:
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    before: str = hasher.hash_column(pl.Series("n", [1.0, 2.0, 3.0])).digest_hex
    after: str = hasher.hash_column(pl.Series("n", [1.0, 2.0, 3.5])).digest_hex
    assert before != after


def test_nulls_contribute_a_term_rather_than_being_skipped() -> None:
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    with_null: str = hasher.hash_column(pl.Series("n", [1.0, None])).digest_hex
    without: str = hasher.hash_column(pl.Series("n", [1.0])).digest_hex
    assert with_null != without


def test_dataframe_digest_is_the_modular_sum_of_the_column_digests() -> None:
    # The identity every later phase leans on. Asserted directly rather than left to fall
    # out of the per-column tests by accident.
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    df: pl.DataFrame = _frame()
    total: int = 0
    name: str
    for name in df.columns:
        total = (total + int(hasher.hash_column(df[name]).digest_hex, 16)) % MODULUS
    whole: BinaryAggregateHashedDataframe = hasher.hash_dataframe(df)
    assert whole.scope == "dataframe"
    assert int(whole.digest_hex, 16) == total


def test_concatenating_two_halves_reproduces_the_whole_frame_digest() -> None:
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    df: pl.DataFrame = _frame()
    halves: pl.DataFrame = pl.concat([df[2:], df[:2]], how="vertical")
    assert hasher.hash_dataframe(halves).digest_hex == hasher.hash_dataframe(df).digest_hex


def test_the_hasher_satisfies_the_abstract_interface() -> None:
    # inspect.isabstract rather than asserting that instantiating the base raises: both
    # type checkers reject that call outright, and a test should not need a suppression to
    # state something the checkers already prove.
    assert isinstance(DataFrameHasherBinaryAggregateHash(), DataFrameHasherBaseClass)
    assert inspect.isabstract(DataFrameHasherBaseClass)
    assert not inspect.isabstract(DataFrameHasherBinaryAggregateHash)
