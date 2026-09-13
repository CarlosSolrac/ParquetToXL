"""Row relationships survive aggregation while whole rows may be reordered."""

from __future__ import annotations

import polars as pl
import pytest
import xxhash

from parquet_to_xl.hashing.binary_aggregate import MODULUS, BinaryAggregateHashedDataframe, DataFrameHasherBinaryAggregateHash
from parquet_to_xl.metadata.builder import build_columns_metadata
from parquet_to_xl.metadata.columns import DataframeColumnsMetadata


def test_swapping_values_between_rows_changes_only_the_frame_digest() -> None:
    before: pl.DataFrame = pl.DataFrame({"id": [1, 2], "amount": [10, 20]})
    after: pl.DataFrame = pl.DataFrame({"id": [1, 2], "amount": [20, 10]})
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    assert hasher.hash_column(before["amount"]) == hasher.hash_column(after["amount"])
    assert hasher.hash_dataframe(before) != hasher.hash_dataframe(after)


def test_column_order_is_bound_but_column_names_are_not() -> None:
    frame: pl.DataFrame = pl.DataFrame({"a": [1, 2], "b": [3, 4]})
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    assert hasher.hash_dataframe(frame) != hasher.hash_dataframe(frame.select("b", "a"))
    assert hasher.hash_dataframe(frame) == hasher.hash_dataframe(frame.rename({"a": "other"}))


def test_fixed_width_cell_hashes_define_the_row_and_final_sums() -> None:
    # These bytes follow the canonical integer/string/null tags, independently of the
    # implementation's series traversal. Every row concatenates exactly two 16-byte hashes.
    encoded_rows: list[tuple[bytes, bytes]] = [(b"\x061", b"\x02ab"), (b"\x062", b"\x02c"), (b"\x061", b"\x00")]
    rows: list[list[bytes]] = [[xxhash.xxh3_128_digest(value) for value in row] for row in encoded_rows]
    row_sum: int = sum(xxhash.xxh3_128_intdigest(b"".join(row)) for row in rows) % MODULUS
    cell_sum: int = sum(int.from_bytes(value, "big") for row in rows for value in row) % MODULUS
    frame: pl.DataFrame = pl.DataFrame({"id": [1, 2, 1], "text": ["ab", "c", None]})
    result: BinaryAggregateHashedDataframe = DataFrameHasherBinaryAggregateHash().hash_dataframe(frame)
    assert result.version == 2
    assert result.row_digest_hex == f"{row_sum:032x}"
    assert result.digest_hex == f"{(cell_sum + row_sum) % MODULUS:032x}"


def test_batch_boundaries_do_not_change_any_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    frame: pl.DataFrame = pl.DataFrame({"id": [1, 2, 3, 4, 5], "value": [None, "a", "b", "a", "c"]})
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    expected: tuple[tuple[BinaryAggregateHashedDataframe, ...], BinaryAggregateHashedDataframe] = hasher.hash_all(frame)
    monkeypatch.setattr("parquet_to_xl.hashing.binary_aggregate.HASH_BATCH_ROWS", 2)
    assert hasher.hash_all(frame) == expected
    columns: tuple[BinaryAggregateHashedDataframe, ...]
    whole: BinaryAggregateHashedDataframe
    columns, whole = hasher.hash_all(frame)
    assert list(columns) == [hasher.hash_column(frame[name]) for name in frame.columns]
    assert whole == hasher.hash_dataframe(frame.reverse())
    metadata: DataframeColumnsMetadata = build_columns_metadata(frame, [hasher])
    assert [column.hashes[0] for column in metadata.columns] == list(columns)
    assert metadata.dataframe_hashes == [whole]


def test_split_aggregates_add_to_the_whole_with_duplicate_and_null_rows() -> None:
    frame: pl.DataFrame = pl.DataFrame({"id": [1, 1, None, 2], "value": ["a", "a", None, "b"]})
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    whole: BinaryAggregateHashedDataframe = hasher.hash_dataframe(frame)
    first: BinaryAggregateHashedDataframe = hasher.hash_dataframe(frame[:2])
    second: BinaryAggregateHashedDataframe = hasher.hash_dataframe(frame[2:])
    assert int(whole.digest_hex, 16) == (int(first.digest_hex, 16) + int(second.digest_hex, 16)) % MODULUS
    assert hasher.hash_dataframe(frame.head(1)) != hasher.hash_dataframe(frame.head(2))


def test_empty_frame_and_empty_columns_have_zero_aggregates() -> None:
    hasher: DataFrameHasherBinaryAggregateHash = DataFrameHasherBinaryAggregateHash()
    frame: pl.DataFrame
    columns: tuple[BinaryAggregateHashedDataframe, ...]
    whole: BinaryAggregateHashedDataframe
    for frame in [pl.DataFrame(), pl.DataFrame(schema={"a": pl.Int64, "b": pl.String})]:
        columns, whole = hasher.hash_all(frame)
        assert all(column.digest_hex == "0" * 32 for column in columns)
        assert whole.digest_hex == whole.row_digest_hex == "0" * 32
