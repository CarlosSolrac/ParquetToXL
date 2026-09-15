"""Gate 0b: ToExcel conversion commutes with row subsetting, and digests are additive over a partition.

``docs/export-pipeline-spec.md`` verifies a partitioned export by arithmetic on recorded
digests rather than by reassembling the rows, and never opens the Parquet file to do it.
That rests on two claims which this module is the proof of:

1. ``convert(row_subset) == row_subset(convert(whole))``. Every ToExcel rule is element-wise,
   so it should hold -- but "should" is not a proof, and if it fails the whole
   ``Fragment.expected`` design is wrong rather than merely unproven.
2. ``whole_frame_digest == sum(fragment_digests) mod 2**128``, on **both** ``digest_hex`` and
   ``row_digest_hex``, which is the consequence the pipeline actually spends.

The subsets are not only contiguous slices. Partitioning by a calendar period selects rows by
predicate, so a fragment is an arbitrary subset of row positions; the contiguous case alone
would prove the easy half of what the pipeline does. Both shapes are driven here.

The last four tests are the preconditions, stated as failures. Additivity is not a property of
the hasher alone -- it holds for *total, disjoint* partitions carrying *every source column in
source order*. Each of those words is load-bearing, and a test that only shows the property
holding would not say so.

The splits are derived from ``xxh3``, keyed by trial and row index, rather than drawn from
``random``. Two reasons, and the second holds on its own. A property test that picks fresh
splits each run turns a real counterexample into a flake someone re-runs away, so a failure
here has to reproduce -- and ``random`` promises reproducibility across neither Python
versions nor builds, which is also why ``pqx_testing.generate`` hand-rolls its own generator
rather than calling it. The same hash the hasher under test uses is already a dependency here,
and Ruff's ``S311`` rejects the standard generators outright.
"""

from __future__ import annotations

import polars as pl
import xxhash
from pqx_frame.conversion.base import ConvertedDataframe
from pqx_frame.conversion.to_excel import DataframeConversionToExcel
from pqx_frame.hashing.binary_aggregate import MODULUS, BinaryAggregateHashedDataframe, DataFrameHasherBinaryAggregateHash
from pqx_testing.generate import ROW_COUNT, build_frame

SEED: str = "gate-0b/2026-09-15"
"""Keys every draw below, so a counterexample reproduces. See the module docstring."""

CONTIGUOUS_TRIALS: int = 200
SCATTERED_TRIALS: int = 60
"""Scattered splits cost a ``gather`` per fragment, so fewer of them buy the same coverage."""

MAX_FRAGMENTS: int = 12
"""Upper bound on fragments per trial. The partitioning spec's workbooks hold far fewer sheets."""


def _convert(df: pl.DataFrame) -> pl.DataFrame:
    """Return the ToExcel-converted frame, through the public surface.

    Args:
        df: The frame to convert.

    Returns:
        The converted frame.
    """
    result: ConvertedDataframe = DataframeConversionToExcel().metadata_of_converted_dataframe(df, [])
    return result.converted_dataframe


def _changed(df: pl.DataFrame) -> bool:
    """Return the conversion's ``schema_or_data_changed`` flag for one frame.

    Args:
        df: The frame to convert.

    Returns:
        Whether the conversion reported a schema change or a moved value.
    """
    return DataframeConversionToExcel().metadata_of_converted_dataframe(df, []).schema_or_data_changed


def _digest(df: pl.DataFrame) -> BinaryAggregateHashedDataframe:
    """Return the whole-frame binary-aggregate record.

    Args:
        df: The frame to digest.

    Returns:
        The record, carrying both ``digest_hex`` and ``row_digest_hex``.
    """
    return DataFrameHasherBinaryAggregateHash().hash_all(df)[1]


def _totals(fragments: list[pl.DataFrame]) -> tuple[str, str]:
    """Sum the fragment digests the way ``verify_manifest`` will.

    Args:
        fragments: The converted fragments, in any order -- the sum does not depend on it.

    Returns:
        The cell-and-row total and the row-hash total, each as 32 lowercase hex characters.
    """
    cells: int = 0
    rows: int = 0
    fragment: pl.DataFrame
    for fragment in fragments:
        record: BinaryAggregateHashedDataframe = _digest(fragment)
        assert record.row_digest_hex is not None, "a version 2 dataframe record always carries a row digest"
        cells = (cells + int(record.digest_hex, 16)) % MODULUS
        rows = (rows + int(record.row_digest_hex, 16)) % MODULUS
    return f"{cells:032x}", f"{rows:032x}"


def _draw(below: int, *key: object) -> int:
    """Return a value in ``range(below)`` determined by ``key``.

    Args:
        below: Exclusive upper bound, at least 1.
        key: Whatever identifies this draw -- trial number, row position, purpose.

    Returns:
        The same value on every machine, Python version and run, for the same key.
    """
    return xxhash.xxh3_64_intdigest("/".join([SEED, *map(str, key)]).encode()) % below


def _contiguous_split(trial: int, height: int, parts: int) -> list[list[int]]:
    """Cut ``0..height`` into consecutive runs of row positions.

    Cut points are drawn and then deduplicated, so the result may hold fewer than ``parts``
    runs; it never holds an empty one, and it is total and disjoint either way, which is all
    the tests below ask of it.

    Args:
        trial: Identifies this draw.
        height: Number of rows to cover, at least two.
        parts: How many runs to aim for.

    Returns:
        One list of row positions per run, in ascending order.
    """
    cuts: list[int] = sorted({1 + _draw(height - 1, trial, "cut", index) for index in range(parts - 1)})
    bounds: list[int] = [0, *cuts, height]
    return [list(range(bounds[edge], bounds[edge + 1])) for edge in range(len(bounds) - 1)]


def _scattered_split(trial: int, height: int, parts: int) -> list[list[int]]:
    """Assign every row position to one of ``parts`` groups.

    This is the shape partitioning by a calendar period actually produces: a fragment is the
    rows matching a predicate, which are scattered through the frame rather than adjacent.

    Empty groups are kept rather than dropped, but at these sizes none is ever drawn -- 1000
    rows across at most twelve groups leaves the smallest observed group at 69 rows. An empty
    fragment is a real case (a period with no rows) and is covered deliberately instead, by
    ``test_additivity_survives_an_empty_fragment_in_the_partition``.

    Args:
        trial: Identifies this draw.
        height: Number of rows to cover.
        parts: How many groups to produce.

    Returns:
        One list of row positions per group, in ascending order.
    """
    groups: list[list[int]] = [[] for _ in range(parts)]
    position: int
    for position in range(height):
        groups[_draw(parts, trial, "group", position)].append(position)
    return groups


def _splits(height: int, trials: int, *, scattered: bool) -> list[list[list[int]]]:
    """Derive ``trials`` partitions of ``height`` row positions.

    Args:
        height: Number of rows to cover.
        trials: How many partitions to derive.
        scattered: Derive scattered groups rather than consecutive runs.

    Returns:
        One partition per trial, each a list of row-position lists.
    """
    drawn: list[list[list[int]]] = []
    trial: int
    for trial in range(trials):
        parts: int = 1 + _draw(MAX_FRAGMENTS, trial, "parts")
        drawn.append(_scattered_split(trial, height, parts) if scattered else _contiguous_split(trial, height, parts))
    return drawn


def test_conversion_commutes_with_contiguous_row_subsetting() -> None:
    whole: pl.DataFrame = build_frame(delta=False)
    converted: pl.DataFrame = _convert(whole)
    partition: list[list[int]]
    for partition in _splits(whole.height, CONTIGUOUS_TRIALS, scattered=False):
        positions: list[int]
        for positions in partition:
            assert _convert(whole[positions]).equals(converted[positions])


def test_conversion_commutes_with_scattered_row_subsetting() -> None:
    whole: pl.DataFrame = build_frame(delta=False)
    converted: pl.DataFrame = _convert(whole)
    partition: list[list[int]]
    for partition in _splits(whole.height, SCATTERED_TRIALS, scattered=True):
        positions: list[int]
        for positions in partition:
            assert _convert(whole[positions]).equals(converted[positions])


def test_a_fragment_carries_the_whole_frames_converted_schema() -> None:
    # Fragment verification reads each sheet at the schema recorded once for the source. If a
    # short or edge-free fragment converted to a different schema, that read would be wrong for
    # exactly the fragments least likely to be looked at.
    whole: pl.DataFrame = build_frame(delta=False)
    converted: pl.DataFrame = _convert(whole)
    partition: list[list[int]]
    for partition in _splits(whole.height, SCATTERED_TRIALS, scattered=True):
        positions: list[int]
        for positions in partition:
            assert _convert(whole[positions]).schema == converted.schema


def test_digests_are_additive_over_contiguous_partitions() -> None:
    whole: pl.DataFrame = build_frame(delta=False)
    expected: BinaryAggregateHashedDataframe = _digest(_convert(whole))
    partition: list[list[int]]
    for partition in _splits(whole.height, CONTIGUOUS_TRIALS, scattered=False):
        assert _totals([_convert(whole[positions]) for positions in partition]) == (expected.digest_hex, expected.row_digest_hex)


def test_digests_are_additive_over_scattered_partitions() -> None:
    whole: pl.DataFrame = build_frame(delta=False)
    expected: BinaryAggregateHashedDataframe = _digest(_convert(whole))
    partition: list[list[int]]
    for partition in _splits(whole.height, SCATTERED_TRIALS, scattered=True):
        assert _totals([_convert(whole[positions]) for positions in partition]) == (expected.digest_hex, expected.row_digest_hex)


def test_an_empty_fragment_contributes_the_additive_identity() -> None:
    # A period with no rows, or an overflow part that ends up empty, must be summable without
    # a special case in verify_manifest.
    whole: pl.DataFrame = build_frame(delta=False)
    empty: pl.DataFrame = _convert(whole.slice(0, 0))
    record: BinaryAggregateHashedDataframe = _digest(empty)
    assert record.digest_hex == "0" * 32
    assert record.row_digest_hex == "0" * 32


def test_additivity_survives_an_empty_fragment_in_the_partition() -> None:
    # A period the calendar declares but no row falls into still gets a fragment, and
    # verify_manifest sums it with the rest rather than special-casing it away.
    whole: pl.DataFrame = _convert(build_frame(delta=False))
    expected: BinaryAggregateHashedDataframe = _digest(whole)
    assert _totals([whole.slice(0, 700), whole.slice(0, 0), whole.slice(700, ROW_COUNT - 700)]) == (expected.digest_hex, expected.row_digest_hex)


def test_the_changed_flag_is_the_disjunction_of_the_fragments_not_each_ones_value() -> None:
    # The one thing about the conversion that does **not** commute, pinned so nobody records it
    # per fragment and reads it as the source's. ``schema_or_data_changed`` answers "did
    # anything move in *this* frame"; a fragment holding no NaN truthfully says no while the
    # whole frame says yes. The disjunction is what carries over, because the schema half is
    # subset-independent and the value half is an ``any`` over rows.
    frame: pl.DataFrame = pl.DataFrame({"f": pl.Series([float("nan"), 1.0, 2.0], dtype=pl.Float64)})
    assert _changed(frame) is True
    assert _changed(frame.slice(1, 2)) is False
    assert _changed(frame.slice(0, 1)) is True
    assert _changed(frame) is (_changed(frame.slice(0, 1)) or _changed(frame.slice(1, 2)))


def test_additivity_fails_when_fragments_overlap() -> None:
    whole: pl.DataFrame = _convert(build_frame(delta=False))
    assert _totals([whole.slice(0, 501), whole.slice(500, ROW_COUNT - 500)]) != (_digest(whole).digest_hex, _digest(whole).row_digest_hex)


def test_additivity_fails_when_a_fragment_is_missing() -> None:
    whole: pl.DataFrame = _convert(build_frame(delta=False))
    assert _totals([whole.slice(0, 400), whole.slice(400, 400)]) != (_digest(whole).digest_hex, _digest(whole).row_digest_hex)


def test_additivity_fails_when_a_fragment_reorders_its_columns() -> None:
    # Why "every sheet carries all source columns in source order" is a rule and not a habit.
    # The cell-hash halves still agree -- a sum does not care about order -- but the row hash
    # concatenates a row's cell digests in column order, so it moves, and the combined digest
    # moves with it.
    whole: pl.DataFrame = _convert(build_frame(delta=False))
    reordered: pl.DataFrame = whole.select(sorted(whole.columns, reverse=True))
    assert _totals([whole.slice(0, 500), reordered.slice(500, 500)]) != (_digest(whole).digest_hex, _digest(whole).row_digest_hex)


def test_additivity_fails_when_a_fragment_carries_a_partition_key() -> None:
    # The spec's second enforced condition: a temporary partition key or source ordinal must
    # never reach exported data. One extra column changes every row hash in that fragment.
    whole: pl.DataFrame = _convert(build_frame(delta=False))
    keyed: pl.DataFrame = whole.slice(500, 500).with_columns(pl.lit(1, dtype=pl.Int64).alias("__period_key"))
    assert _totals([whole.slice(0, 500), keyed]) != (_digest(whole).digest_hex, _digest(whole).row_digest_hex)
