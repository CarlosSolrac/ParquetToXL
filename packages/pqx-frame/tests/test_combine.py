"""Frozen tests for combining fragment digests back into a whole-source digest.

Gate 0b proved the additive identity by doing the arithmetic inline. This is the same property as
a call, and these tests are what keep it a property rather than a coincidence.
"""

from __future__ import annotations

import polars as pl
import pytest
from pqx_frame.hashing.binary_aggregate import BinaryAggregateHashedDataframe, DataFrameHasherBinaryAggregateHash

HASHER: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()


def _frame(rows: int = 40) -> pl.DataFrame:
    """A mixed-dtype frame wide enough that column order matters."""
    return pl.DataFrame(
        {
            "i": pl.Series(list(range(rows)), dtype=pl.Int64),
            "s": pl.Series([f"row {index}" for index in range(rows)], dtype=pl.String),
            "f": pl.Series([index / 3 for index in range(rows)], dtype=pl.Float64),
            "b": pl.Series([index % 2 == 0 for index in range(rows)], dtype=pl.Boolean),
            "n": pl.Series([None if index % 5 == 0 else index for index in range(rows)], dtype=pl.Int64),
        },
    )


def test_contiguous_parts_combine_to_the_whole() -> None:
    frame: pl.DataFrame = _frame()
    whole: BinaryAggregateHashedDataframe = HASHER.hash_dataframe(frame)
    parts: list[BinaryAggregateHashedDataframe] = [HASHER.hash_dataframe(frame.slice(start, 13)) for start in range(0, 40, 13)]
    combined: BinaryAggregateHashedDataframe = DataFrameHasherBinaryAggregateHash.combine(parts)
    assert combined.digest_hex == whole.digest_hex
    assert combined.row_digest_hex == whole.row_digest_hex


def test_scattered_parts_combine_to_the_whole() -> None:
    # Not only contiguous slices: any total, disjoint partition works, because every aggregate is
    # a modular sum over cells and rows.
    frame: pl.DataFrame = _frame()
    whole: BinaryAggregateHashedDataframe = HASHER.hash_dataframe(frame)
    even: BinaryAggregateHashedDataframe = HASHER.hash_dataframe(frame.filter(pl.col("i") % 2 == 0))
    odd: BinaryAggregateHashedDataframe = HASHER.hash_dataframe(frame.filter(pl.col("i") % 2 == 1))
    combined: BinaryAggregateHashedDataframe = DataFrameHasherBinaryAggregateHash.combine([even, odd])
    assert combined.digest_hex == whole.digest_hex
    assert combined.row_digest_hex == whole.row_digest_hex


def test_the_order_of_the_parts_does_not_matter() -> None:
    frame: pl.DataFrame = _frame()
    parts: list[BinaryAggregateHashedDataframe] = [HASHER.hash_dataframe(frame.slice(start, 13)) for start in range(0, 40, 13)]
    forward: BinaryAggregateHashedDataframe = DataFrameHasherBinaryAggregateHash.combine(parts)
    backward: BinaryAggregateHashedDataframe = DataFrameHasherBinaryAggregateHash.combine(list(reversed(parts)))
    assert forward == backward


def test_one_part_combines_to_itself() -> None:
    whole: BinaryAggregateHashedDataframe = HASHER.hash_dataframe(_frame())
    assert DataFrameHasherBinaryAggregateHash.combine([whole]) == whole


def test_an_empty_fragment_contributes_nothing() -> None:
    # A source with no rows in a period contributes no sheet; a header-only one contributes a
    # fragment that must not change the sum.
    frame: pl.DataFrame = _frame()
    whole: BinaryAggregateHashedDataframe = HASHER.hash_dataframe(frame)
    empty: BinaryAggregateHashedDataframe = HASHER.hash_dataframe(frame.clear())
    assert DataFrameHasherBinaryAggregateHash.combine([whole, empty]).digest_hex == whole.digest_hex


def test_a_missing_fragment_changes_the_digest() -> None:
    # The whole point: a dropped fragment must not combine to the value the manifest expects.
    frame: pl.DataFrame = _frame()
    whole: BinaryAggregateHashedDataframe = HASHER.hash_dataframe(frame)
    parts: list[BinaryAggregateHashedDataframe] = [HASHER.hash_dataframe(frame.slice(start, 13)) for start in range(0, 40, 13)]
    assert DataFrameHasherBinaryAggregateHash.combine(parts[:-1]).digest_hex != whole.digest_hex


def test_a_moved_cell_changes_the_row_digest_even_when_the_cell_sum_survives() -> None:
    # Why the row digest exists, and why combine sums it too. Swapping one value between two rows
    # of the same column leaves every column total untouched.
    frame: pl.DataFrame = _frame(4)
    mutated: pl.DataFrame = frame.with_columns(pl.Series("s", ["row 1", "row 0", "row 2", "row 3"], dtype=pl.String))
    assert HASHER.hash_dataframe(frame).row_digest_hex != HASHER.hash_dataframe(mutated).row_digest_hex


def test_column_scope_records_combine_without_a_row_digest() -> None:
    frame: pl.DataFrame = _frame()
    first: BinaryAggregateHashedDataframe = HASHER.hash_column(frame["i"].slice(0, 20))
    second: BinaryAggregateHashedDataframe = HASHER.hash_column(frame["i"].slice(20, 20))
    combined: BinaryAggregateHashedDataframe = DataFrameHasherBinaryAggregateHash.combine([first, second])
    assert combined.scope == "column"
    assert combined.row_digest_hex is None
    assert combined.digest_hex == HASHER.hash_column(frame["i"]).digest_hex


def test_combining_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one record"):
        DataFrameHasherBinaryAggregateHash.combine([])


def test_records_of_different_scopes_are_refused() -> None:
    frame: pl.DataFrame = _frame()
    with pytest.raises(ValueError, match="not terms in one sum"):
        DataFrameHasherBinaryAggregateHash.combine([HASHER.hash_dataframe(frame), HASHER.hash_column(frame["i"])])


def test_records_of_different_versions_are_refused() -> None:
    # Version skew is a live failure mode here: library-spec.md already grew an
    # `unsupported-hasher` verdict for it.
    current: BinaryAggregateHashedDataframe = HASHER.hash_dataframe(_frame())
    legacy: BinaryAggregateHashedDataframe = current.model_copy(update={"version": 1})
    with pytest.raises(ValueError, match="not terms in one sum"):
        DataFrameHasherBinaryAggregateHash.combine([current, legacy])


def test_a_partial_row_digest_is_refused() -> None:
    # A sum over some of the rows would silently describe fewer than it claims.
    current: BinaryAggregateHashedDataframe = HASHER.hash_dataframe(_frame())
    stripped: BinaryAggregateHashedDataframe = current.model_copy(update={"row_digest_hex": None})
    with pytest.raises(ValueError, match="carry a row digest"):
        DataFrameHasherBinaryAggregateHash.combine([current, stripped])


def test_the_combined_record_keeps_the_hasher_identity() -> None:
    combined: BinaryAggregateHashedDataframe = DataFrameHasherBinaryAggregateHash.combine([HASHER.hash_dataframe(_frame())])
    assert combined.identifier == "binary-aggregate-xxh3-128"
    assert combined.version == 2
    assert combined.bit_width == 128


@pytest.mark.parametrize("parts", [2, 3, 5, 7, 40])
def test_the_identity_holds_at_every_split_count(parts: int) -> None:
    frame: pl.DataFrame = _frame()
    whole: BinaryAggregateHashedDataframe = HASHER.hash_dataframe(frame)
    size: int = -(-frame.height // parts)
    fragments: list[BinaryAggregateHashedDataframe] = [HASHER.hash_dataframe(frame.slice(start, size)) for start in range(0, frame.height, size)]
    combined: BinaryAggregateHashedDataframe = DataFrameHasherBinaryAggregateHash.combine(fragments)
    assert (combined.digest_hex, combined.row_digest_hex) == (whole.digest_hex, whole.row_digest_hex)
