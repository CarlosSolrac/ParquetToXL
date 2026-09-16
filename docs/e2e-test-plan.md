# End-to-end exercise: plan, edge cases, and what to look at

Backlog item 7 says the pipeline has never been driven against anything but `tmp_path`. This is
the local half of closing that. `tools/e2e_exercise.py` writes thirteen Parquet files chosen to be
awkward, nine configurations over them, and runs each one end to end — leaving sidecars, manifests,
receipts, reports and workbooks in a tree that **survives the run** so a person can open them.

**It asserts nothing.** The 1,725 tests already assert what the code believes. This produces output
whose correctness a human can judge, which is a different job.

```bash
uv run python tools/e2e_exercise.py          # builds ./e2e, replacing any previous tree
```

The tree is gitignored. `e2e/RESULTS.json` records every profile's exit code, workbooks and output.

---

## Findings so far

Three things the exercise surfaced that the test suite did not.

### 1. Partitioning is bypassed when the export fits one workbook

[run.py:197](../packages/pqx-pipeline/src/pqx_pipeline/run.py#L197) takes a shortcut: if every
source fits inside one workbook, each becomes a single sheet named by `single_worksheet`, with no
period label — **whatever the partitioning says**. It is deliberate and commented.

It is also the most surprising thing here. An operator configuring monthly workbooks over a small
dataset gets one sheet called `source Data` and no indication the calendar settings were read at
all. The first draft of this very plan fell into it: seven of nine profiles claimed to demonstrate
period partitioning and none of them did, because the cell limits were generous. The profiles below
now carry deliberately small `max_cells_per_workbook` values for that reason — if you raise them,
the partitioning quietly stops happening.

### 2. A data-level refusal escapes the CLI as a traceback

`null_dates: "error"` is a documented configuration value meaning *refuse rather than bucket*. When
it fires, `pqx run` exits with a Python stack trace rather than one of its four documented exit
codes, and the message it worked to build — `row 0 has no 'booked', and null_dates is 'error'` — is
buried in it.

[cli.py:461](../packages/pqx-pipeline/src/pqx_pipeline/cli.py#L461) maps `OwnershipError`,
`StagingError`, `NotImplementedError` and `ValueError` to exit 3. `BucketingError` and
`DateDecodeError` subclass plain `Exception` and are not caught, so **every data-level refusal
escapes** — an unreadable date value as much as a null.

Not fixed here, because the right exit code is a judgement: `EXIT_BAD` (1) reads as "your data is
broken", `EXIT_REFUSED` (3) as "this run will not proceed". A bad date value and a null under an
error policy may not deserve the same one.

### 3. A `sidecar_location: directory` target is not created

The sidecar store writes straight into the configured directory and raises `FileNotFoundError` if
it does not exist. Nothing in `docs/export-config.schema.json` says it must pre-exist. The harness
creates it, as an operator would have to.

---

## The sources

Thirteen Parquet files under `e2e/sources/`.

| Source | Rows × cols | What it is for |
| --- | --- | --- |
| `tiny` | 3 × 3 | The simplest possible export |
| `empty` | 0 × 3 | Zero rows: `is_empty` on the shape, and what a plan does with nothing |
| `single_row` | 1 × 3 | A header plus one data row |
| `multi_year` | 600 × 4 | Five years at three-day spacing: per-year and per-month sheets |
| `undated` | 30 × 3 | A third of rows null-dated: the UNDATED bucket |
| `all_undated` | 12 × 3 | Every row undated: an export with no dated bucket at all |
| `int_yymm` | 6 × 3 | The two-digit-year window at its boundaries, and leading zeros |
| `int_yyyymmdd` | 4 × 3 | A leap day, and the two days a year boundary is made of |
| `zoned` | 3 × 3 | A named zone moving a row into the previous **year** |
| `wall_clock` | 6 × 2 | Naive timestamps read as a wall clock, no zone consulted |
| `overflow` | 2,500 × 4 | One period too big for one worksheet: overflow fragments |
| `dtypes` | 4 × 13 | Every scalar dtype at its boundary values |
| `odd_names` | 3 × 5 | Columns named `*` and `^date$`, and a non-ASCII header |

### Edge cases each one encodes

**`int_yymm`** carries the four values the window exists for: `6901` → 2069, `7001` → 1970,
`9901` → **1999 not 2099**, and `101` → January 2001 rather than a three-digit value. Under a 1970
window these span 99 years, and the workbook is labelled `1970~2069` — which is the check.

**`zoned`** holds `2026-01-01T02:00Z` and `2026-01-01T23:00Z`. Both are 2 January in UTC; in
America/Mexico City the first is still **2025**. They must land in different year sheets. The
manifest shows `zoned 2025~2026`, so they do.

**`dtypes`** carries `int64` min and max (and max−1, which collapses onto the same float), `uint64`
max, `inf`/`-inf`/`nan`/`-0.0`, a string seven characters longer than a cell can hold, an empty
string, empty and non-empty binary, `1899-12-31` and `9999-12-31`, a full-precision time, a
hundred-thousand-day negative duration, decimals, a categorical with an empty member, and an
all-null `Null` column. This is the one to open first.

**`odd_names`** exists because `pl.col("*")` is a wildcard and `pl.col("^date$")` is a regex, both
legal Parquet column names. A bug of exactly this kind was found in `bucketing.py` during review.

### Edge cases deliberately **not** covered

Stated so nobody assumes this is exhaustive:

- **Nothing runs against real infrastructure.** Local filesystem only. SMB, `abfs://` and the Spark
  image are backlog items 6 and 7 and need `gates/gate_0f_azure_round_trip.py` plus credentials.
- **No staleness sequence.** Every profile runs once, into a clean tree. A second run, a touched
  source, a modified configuration and a moved clock are all untested here.
- **No reconciliation.** Nothing exercises deleting what the manifest does not claim, which is the
  most safety-critical path in the system.
- **No verification pass.** `pqx verify` is never invoked against what was written.
- **No `balanced` partitioning** — not wired into a run (backlog item 2).
- **No concurrency**, so the lease is never contended.
- **No worksheet-name collisions**, and no names long enough to hit Excel's 31-character limit.

---

## The profiles

| Profile | Sources | What to check |
| --- | --- | --- |
| `by_year` | tiny, single_row, multi_year | Five year sheets. Note workbook 003 is `2024~2025` and 004 is `2025` — greedy merging putting 2025 data in two workbooks is expected, and worth confirming you agree |
| `by_month_descending` | multi_year | 60 monthly sheets, strictly newest-first from 2026-12 |
| `fan_in` | tiny, odd_names | Two sources in one workbook via the shortcut. Confirm `*`, `^date$` and `ünïcødé` survived as headers |
| `undated_last` | undated, all_undated | **The undated sheet must be last** under a descending order, not first |
| `tight_limits` | overflow | 2,500 rows against a 400-row worksheet: seven fragments `p01`–`p07` across four workbooks |
| `dtypes_showcase` | dtypes | **Open this first.** Every dtype at its boundary |
| `int_and_zoned_dates` | four date encodings | `int_yymm 1970~2069`, and `zoned` spanning 2025–2026 |
| `empty_source` | empty | A zero-row export still produces a workbook with a header and no rows |
| `null_dates_refused` | undated | Expected to refuse. It does — see finding 2 for how badly |

---

## What to look at, in order

1. **`e2e/destination/dtypes_showcase/`** — how each dtype rendered. `int64` extremes, the
   over-long string, `inf`/`nan`, and how booleans appear.
2. **`e2e/destination/undated_last/`** — that `Undated` is the last workbook, not the first.
3. **`e2e/destination/int_and_zoned_dates/`** — that the two same-UTC-day zoned rows are in
   different year sheets.
4. **`e2e/destination/tight_limits/`** — seven `p01`–`p07` fragments across four workbooks, and
   that the row counts sum to 2,500.
5. **`e2e/sidecars/`** — the dtypes recorded per column, and the digests.
6. **Any `*.manifest.json`** — `expected_whole`, and `total_and_disjoint`.
7. **`*/reports/*.report.html`** — what an operator would actually be handed.
