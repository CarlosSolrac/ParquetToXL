"""Frozen tests for the fixture generator.

Everything built on the fixtures assumes these invariants, and until now they were only ever
checked by hand: the generator is the one substantial module in this repository that the
coverage gate does not see, because coverage is scoped to ``src/``.

Read-only for an implementer. If one of these looks wrong, stop and report it rather than
editing it.
"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING, Any

import polars as pl

from tests.fixtures.generate import (
    DELTA_ROW,
    EDGE_ROW_COUNT,
    PART_ROW_COUNT,
    ROW_COUNT,
    SLICES,
    build_frame,
    padded,
)

if TYPE_CHECKING:
    from pathlib import Path

WRITERS: list[str] = ["duckdb", "polars", "rustpy"]
STEMS: list[str] = ["parquet_a", "parquet_b"]


def _same(left: object, right: object) -> bool:
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
        return True
    try:
        return bool(left == right)
    except Exception:
        return False


def test_padded_returns_exactly_the_edge_row_count() -> None:
    assert len(padded(["only"])) == EDGE_ROW_COUNT
    assert padded(["a", "b"])[:4] == ["a", "b", "a", "b"]


def test_padded_silently_drops_anything_past_the_cap() -> None:
    # Pinned because it is a trap, not a feature: a column that declares more edge cases
    # than EDGE_ROW_COUNT loses the tail with no error at all, and the fixture would quietly
    # stop testing what its source says it tests. The guard against that is the next test.
    oversized: list[int] = list(range(EDGE_ROW_COUNT + 5))
    kept: list[int] = padded(oversized)
    assert len(kept) == EDGE_ROW_COUNT
    assert set(oversized) - set(kept) == set(range(EDGE_ROW_COUNT, EDGE_ROW_COUNT + 5))


def test_the_widest_column_still_has_headroom_under_the_cap() -> None:
    # An early warning for the trap above. Each edge row is ``edges[i % len(edges)]``, so
    # while the list is shorter than the cap the distinct count equals its length. A distinct
    # count that reaches EDGE_ROW_COUNT means the list has hit the ceiling and the next case
    # added will vanish. If this fails, raise EDGE_ROW_COUNT rather than deleting a case.
    frame: pl.DataFrame = build_frame(delta=False)
    name: str
    for name in frame.columns:
        distinct: int = frame[name].head(EDGE_ROW_COUNT).n_unique()
        assert distinct < EDGE_ROW_COUNT, f"{name} has filled every edge slot; raise EDGE_ROW_COUNT before adding another case"


def test_the_frame_has_one_column_per_scalar_dtype_in_sorted_order() -> None:
    frame: pl.DataFrame = build_frame(delta=False)
    assert frame.height == ROW_COUNT
    assert frame.width == 19
    assert frame.columns == sorted(frame.columns)


def test_every_scalar_dtype_is_represented_exactly_once() -> None:
    frame: pl.DataFrame = build_frame(delta=False)
    kinds: set[str] = {str(dtype) for dtype in frame.dtypes}
    assert "Boolean" in kinds
    assert "Binary" in kinds
    assert "Categorical(ordering='physical')" in kinds or "Categorical" in kinds
    assert any(kind.startswith("Datetime") for kind in kinds)
    assert any(kind.startswith("Duration") for kind in kinds)
    assert any(kind.startswith("Decimal") for kind in kinds)
    assert {"Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64", "Float32", "Float64", "String", "Date", "Time"} <= kinds


def test_the_two_frames_differ_at_exactly_one_row_in_every_column() -> None:
    # The whole point of parquet_b: a digest that misses a single changed cell is detectable
    # per column, not merely for the frame.
    left: pl.DataFrame = build_frame(delta=False)
    right: pl.DataFrame = build_frame(delta=True)
    name: str
    for name in left.columns:
        a: list[Any] = left[name].to_list()
        b: list[Any] = right[name].to_list()
        differing: list[int] = [index for index in range(len(a)) if not _same(a[index], b[index])]
        assert differing == [DELTA_ROW], f"{name} differs at {differing}, expected only row {DELTA_ROW}"


def test_the_frames_are_identical_apart_from_the_delta_row() -> None:
    left: pl.DataFrame = build_frame(delta=False)
    right: pl.DataFrame = build_frame(delta=True)
    assert left.schema == right.schema
    assert left.shape == right.shape


def test_building_the_same_frame_twice_gives_the_same_values() -> None:
    # The generator seeds its own arithmetic rather than using ``random``, so a rebuild in
    # the same process must not drift. The stronger byte-level claim is the next test.
    first: pl.DataFrame = build_frame(delta=False)
    second: pl.DataFrame = build_frame(delta=False)
    assert first.schema == second.schema
    name: str
    for name in first.columns:
        a: list[Any] = first[name].to_list()
        b: list[Any] = second[name].to_list()
        assert all(_same(a[index], b[index]) for index in range(len(a))), name


def test_parquet_regeneration_is_byte_identical(tmp_path: Path) -> None:
    # Claimed in the module docstring, and the reason the fixtures can be gitignored: a
    # rebuild has to produce the same file, not merely an equivalent one.
    first_path: Path = tmp_path / "first.parquet"
    second_path: Path = tmp_path / "second.parquet"
    build_frame(delta=False).write_parquet(first_path)
    build_frame(delta=False).write_parquet(second_path)
    assert hashlib.sha256(first_path.read_bytes()).hexdigest() == hashlib.sha256(second_path.read_bytes()).hexdigest()


def test_the_slice_table_partitions_the_frame_exactly() -> None:
    assert SLICES["full"] == (ROW_COUNT, 0)
    assert SLICES["part1"] == (PART_ROW_COUNT, 0)
    assert SLICES["part2"] == (PART_ROW_COUNT, PART_ROW_COUNT)
    assert SLICES["part1"][0] + SLICES["part2"][0] == ROW_COUNT


def test_every_fixture_file_is_present(fixture_files: dict[str, Path]) -> None:
    expected: set[str] = set(STEMS)
    stem: str
    label: str
    writer: str
    for stem in STEMS:
        for label in SLICES:
            for writer in WRITERS:
                expected.add(f"{stem}_{label}_{writer}")
    assert set(fixture_files) == expected
    path: Path
    for path in fixture_files.values():
        assert path.exists()
        assert path.stat().st_size > 0


def test_regenerating_reuses_the_files_already_on_disk(fixture_files: dict[str, Path]) -> None:
    # ensure_fixtures is called on every session; rewriting twenty files each time would
    # make the suite slower and the workbooks churn, since two of the three writers stamp a
    # creation time into them.
    from tests.fixtures.generate import ensure_fixtures

    before: dict[str, float] = {name: path.stat().st_mtime_ns for name, path in fixture_files.items()}
    again: dict[str, Path] = ensure_fixtures()
    name: str
    for name, path in again.items():
        assert path.stat().st_mtime_ns == before[name], f"{name} was rewritten"
