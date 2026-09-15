# ParquetToXL — Export pipeline specification

**Status:** design, unbuilt.

This document covers the decomposition into installable libraries, how a run decides what work
is out of date, how files move between remote storage and local scratch, how an export is
verified, and how it is published. Partitioning — capacity, calendar algorithms, date
interpretation, naming — is in `partitioning-spec.md`. The library that already exists, and the
contracts this depends on, are in `library-spec.md`.

## Why

`ParquetToXL` today answers one question: given a Polars DataFrame, what is its metadata, what
does it hash to, and does a written workbook still hold that data? It is finished against its
own spec — 428 tests, 100% statement and branch coverage, Ruff plus pyright plus mypy strict.

It is not yet a tool. Two gaps:

- **Nothing reads Parquet.** `extract_metadata_from_dataframe(df, path, ...)` takes the frame and
  the path as separate arguments and never checks them against each other. The caller supplies
  the frame, so "load a Parquet file and create its sidecar" is not a callable thing.
- **`validate_workbook` cannot validate a partitioned source.** It compares one workbook against
  one sidecar's whole-frame digest. Once a source's rows are split across sheets, nothing
  matches; the headline integration test does the reassembly by hand.

## Runtime

An isolated PySpark environment on a **Linux** host, **invoked manually**. There is no watcher
process and no polling loop, but every run begins by selecting only work that is out of date.

**Spark is the execution environment only.** No `pyspark` import, no `spark.read`; Polars does
the work. `paths.py` already carries the rules that matter: Spark does not use fsspec, so
storage options govern driver-side Python I/O only, and a path must never be pickled to an
executor because `UPath.__reduce__` would pickle its credentials with it.

**All I/O is staged through local scratch.** A source is copied from its real location — an SMB
share, Azure Blob or ADLS, or a POSIX mount — into a run-scoped scratch directory. Every read
and every write happens there. Verified output is then published. No reader or writer ever opens
a remote path.

## Selection — the four timestamps

| | Timestamp | Where it lives | Written by |
| --- | --- | --- | --- |
| **T1** | `source_modified_utc` | the Parquet file's own mtime | the source store |
| **T2** | `sidecar_created_utc` | **new** field on `SidecarDocument` (schema v3) | the pipeline |
| **T3** | `config_modified_utc` | **new** required field in the export configuration | the author, or the web editor |
| **T4** | `excel_created_utc` | **new** run receipt at the destination | the pipeline |

The sidecar already records T1 as observed: `extract_metadata_from_dataframe` does
`dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)` and stores it as
`DataframeMetadata.modified_utc`. That is the anchor for the source check.

```text
sidecar_stale(s) <=> sidecar missing or unreadable
                  or T1(s) != sidecar.metadata.modified_utc        # source changed since described
                  or sidecar conversion/hasher versions != current # digests computed under other rules

excel_stale(p)   <=> receipt missing                               # never exported
                  or any(sidecar_stale(s) for s in p.sources)      # data about to be re-described
                  or any(T2(s) > T4 for s in p.sources)            # re-described after the export
                  or T3 > T4                                       # config changed after the export
                  or resolved_config_hash != receipt.config_hash   # backstop: a forgotten T3 bump
                  or receipt.planner_version != planner_version    # allocation rules changed
```

### Why T1 is compared to a recording of itself

Not `mtime(source) > mtime(sidecar_file)`. Comparing the two files' own timestamps is worse in
three specific ways:

- **Two clocks.** With `sidecar_location` pointing at a different store from the source, one
  timestamp comes from Azure's servers and the other from an SMB file server. Minutes of skew is
  ordinary, and it makes the answer depend on where the sidecar was put.
- **Publishing rewrites the sidecar's own mtime.** Every publish stamps it "now", so the file's
  timestamp records when it was copied, not what it describes.
- **`>` misses a reverted source.** Restore yesterday's Parquet over today's and its mtime moves
  *backwards*. `>` calls that "not new" and the stale export survives. `!=` catches it.

Reading the recorded value has none of these problems: one clock, the source store's, and the
comparison is against what was actually described, wherever the sidecar now lives. Compare
truncated to whole seconds — Azure Blob reports last-modified at one-second granularity while a
local filesystem reports finer.

**Only T3 versus T4 crosses a clock boundary.** T2 and T4 are both written by the pipeline, on
the same host, in the same run. T3 comes from an author's workstation or the web editor, and it
is also the one a human can simply forget to bump after editing `naming.workbook`. Hence
`resolved_config_hash`: exact, clock-free, and it fires when the timestamp does not. Both signals
are implemented, and the report names which one triggered, so a forgotten bump is visible rather
than merely compensated for.

### Version drift

Two of the conditions above are version drift, and both are easy to miss. A bump to
`DataframeConversionToExcel` or to the hasher leaves T1 untouched, so without the version check a
run proceeds all the way to verification before `unsupported-conversion` fires — the right answer,
after staging, reading and writing everything. Rebuilding at selection is the same answer for far
less work. Likewise `partitioning-spec.md` promises determinism "for the same sources, resolved
configuration and planner version", so a changed allocation algorithm must invalidate an export
that every timestamp calls current.

Both are conservative: they rebuild once per release whether or not the output would differ. A
third, smaller one to expect rather than be surprised by: `resolved_config_hash` is taken over the
serialized model, so adding a field to the configuration — even one with a default — changes every
hash and rebuilds everything once. Blunt, correct, and cheaper than deciding which fields are
digest-relevant.

### How selection runs

**Before staging**, over remote metadata only: one receipt read, one `stat()` per source, one
small sidecar read per source. Downloading a multi-gigabyte Parquet to discover nothing changed is
the thing this exists to avoid. `JsonSidecarStore` reading over `UPath` is what makes it possible,
so its remote support is load-bearing rather than vestigial.

**Selection picks profiles, not files.** Rebuilding a workbook re-reads its unchanged sources, so a
profile is rebuilt when **any** of its sources is stale, and then all of its sources are staged.
Per-file skipping would leave a workbook mixing old and new sheets.

**The two markers are independent, deliberately.** The sidecar marks *described*; the receipt marks
*exported*. A run that dies after publishing sidecars but before the receipt leaves the next run
with `sidecar_stale` false and `excel_stale` true, so it rebuilds only the export and does not
re-hash the Parquet. Neither marker can claim work the other did.

### The trap

`extract_metadata_from_dataframe` stats whatever path it is handed, and its docstring is explicit
that "the pairing is the caller's responsibility" — frame and path are never checked against each
other. That is exactly what lets the pipeline pass a frame read from scratch together with the
**original remote path**, giving `full_path` the real URI and `modified_utc` the real source time.

Hand it the *scratch* path instead and T1 silently becomes the stage-in time, which poisons the
freshness rule permanently while looking perfectly healthy. This gets its own test.

`--force` rebuilds regardless. `--dry-run` prints the selection and, per profile, which condition
fired, without staging anything.

## Exclusive output locations

**No two configurations may export to the same place**, and neither may two profiles within one
configuration. A run can see only its own configuration and the destination, so the runtime guard
is an ownership marker — and the receipt already is one:

- The configuration carries a stable `config_id`. The **web editor is the authority** that
  allocates it and verifies uniqueness across configurations. Until that exists, hand-written
  configurations set it and the destination check below is what actually catches a collision — at
  publish time rather than at authoring time.
- Each profile writes to `output_directory/output_subdirectory`, and its receipt there names
  `config_id` and `profile`. Publishing into a directory whose receipt names a **different**
  `config_id` or `profile` is refused, before anything is staged.
- Two profiles in one configuration resolving to the same directory is a validation error,
  catchable statically without touching the destination.

This exclusivity is what makes **reconcile-by-listing** safe.

## Staging, verification order, and publication

```text
  az:// | //share | /mnt         -- stage in -->  <scratch>/run-<id>/in/sales.parquet
                                                       |  read -> extract -> sidecar
                                                       |  plan -> write
                                                       v
                                                  <scratch>/run-<id>/out/*.xlsx
                                                  <scratch>/run-<id>/sales.parquet.json
                                    delete staged Parquet --+
                                                       v    |
                                                  VERIFY locally
                                                       |
                                    valid -------------+------- invalid -> report, publish no data
                                                       v
  destination  <-- ownership check, lease, sidecars, workbooks, reconcile,
                   manifest, receipt, report, release
```

**Verify before publish.** Nothing reaches the destination until every fragment has passed. It
also makes verification cheap, because it reads from local scratch rather than over SMB or Azure.

**The staged Parquet is deleted before verification**, not merely dropped from memory. That frees
scratch space where the run needs it most, and makes verification's independence structural rather
than asserted: the source is physically absent while it runs.

**Publication order is load-bearing.** Ownership check, acquire lease, sidecars, workbooks,
reconcile delete, manifest, receipt, report, release lease. The receipt is the "this export
completed" marker, so nothing may precede it that a later step could invalidate. Sidecars go early
on purpose: they mark only *described*, so a failure after them costs the export rather than the
hashing.

**A lease, because ownership is not a lock.** Two concurrent runs of one configuration and profile
both pass the ownership check — same `config_id` — and both reconcile by listing, each deleting
what the other just wrote. So `<profile>.lock` carries `run_id` and an acquisition timestamp, is
written before staging, and a non-expired foreign lease refuses the run. A stale lease expires on
its own, so a killed run does not wedge the directory permanently.

This is **not** a mutex. Neither SMB nor Blob offers compare-and-swap, so two runs starting in the
same instant can both acquire. It closes the realistic case — someone launching the job twice —
not the theoretical one.

**Reconcile by listing, not by diff.** Because the directory belongs to exactly one profile, the
delete set is `listing - (manifest.workbooks + {manifest, receipt, lock} + reports/)`. This
recovers from a crashed run, a missing manifest, and a template that changed two runs ago — none
of which a previous-manifest diff would clean up. **The keep-set must include the bookkeeping**, or
the run deletes its own records on the way out.

Deleting from a shared folder is hard to undo, so four guards:

- the delete set is computed and shown during planning, before any work is done;
- `--dry-run` lists it and publishes nothing;
- deletion is refused if the ownership check did not pass, so an unexpected receipt — or none at
  all where files already exist — stops the run instead of clearing the directory;
- deletion is refused if the lease is not held.

**Reports are timestamped, kept for both outcomes, and never reconciled away.** They live in a
`reports/` subdirectory as `<profile>-<utc>.report.{json,html,md}`, which the keep-set excludes
wholesale. A failed run therefore *does* write to the destination — but only there, never a
workbook, never a delete. That is a deliberate carve-out from "a failed run changes nothing",
because the report is the only artifact that explains the failure to someone who has the share and
nothing else. The directory grows without bound; retention is an operator concern and out of scope.

### Scratch

**Configurable, never a hard-coded `/tmp`.** Default `tempfile.gettempdir()`, honouring `TMPDIR`,
overridable in configuration and on the CLI, because Spark may point local storage elsewhere and
because on many Spark images `/tmp` is **tmpfs** — RAM-backed, so staged Parquet and written
`.xlsx` come out of the same memory the export is already competing for.

Before staging, check `shutil.disk_usage(scratch_root)` against **the sum of every source the
profile needs** — fan-in stages them all at once — plus a configured output allowance, failing with
a clear message rather than an `ENOSPC` midway through a workbook. Two profiles naming the same
source stage it once and share it. Cleanup runs on failure as well as success, via a context
manager, with `--keep-scratch` for debugging.

### Portable names

Enforced by `partitioning-spec.md`, on the Linux host that generates them, because the destination
may be SMB, Azure or POSIX and none of them will object at generation time. CI is `ubuntu-latest`
only, so it must be an explicit validator rather than a trusted filesystem error.

## The additive property verification rests on

The binary-aggregate hash is **additive mod 2^128** (`MODULUS` in `binary_aggregate.py`). Every
partitioning algorithm produces total, disjoint row partitions, and every sheet carries all source
columns in source order. Therefore:

```text
whole_frame_digest == (sum of fragment_digests) mod 2^128
```

So validating the union is **arithmetic on recorded digests** — no reassembly, no re-reading, and
the Parquet file is never opened. Sorting is free, because the hash is order-independent.

Two conditions, enforced rather than assumed:

1. `convert(row_subset) == row_subset(convert(whole))`. Every ToExcel rule is element-wise, so
   this should hold, but it is proved by a property test over random splits of the 1000-row,
   23-dtype fixture before anything depends on it. If it fails, verification must read every
   fragment back and reassemble instead — a different and much more expensive design.
2. Temporary partition keys and source ordinals never reach exported data. An extra column breaks
   both the additivity and the sidecar comparison.

## The run manifest and receipt

Three options were live for where expected digests come from. **Chosen: computed at write time.**
The converted frame is already in memory when a fragment is written, so the digest is free, and
verification then needs only the manifest, the sidecar and the workbooks. Rejected: recomputing
from the source at verify time, which reopens the file that verification exists to have unloaded;
and storing nothing and checking only the total, which cannot localize a mismatch to a workbook
and sheet.

Paths in the manifest are **destination-relative**, never absolute. The same manifest then serves
verification (join the scratch root) and reconciliation (join the destination), and stays valid if
the destination is moved or remapped.

```python
class Fragment(BaseModel, frozen=True):
    workbook: str                                # destination-relative
    sheet_name: str                              # final, after prefix and collision resolution
    source_alias: str
    period_label: str | None                     # None for balanced output
    part_index: int | None                       # set only for an overflow fragment
    row_count: int
    column_names: list[str]                      # as written, pre-normalization
    expected: BinaryAggregateHashedDataframe

class SourceFragments(BaseModel, frozen=True):
    source_alias: str
    source_path: str
    sidecar_path: str
    conversion: ConversionIdentity
    total_and_disjoint: bool                     # always True in v1; the seam for future filters
    expected_whole: BinaryAggregateHashedDataframe
    expected_row_count: int
    fragments: list[Fragment]

class RunManifest(BaseModel, frozen=True):
    manifest_version: int
    run_id: str
    config_path: str
    profile: str
    planner_version: str
    resolved_config: ExportConfig
    output_directory: str
    started_utc: datetime
    sources: list[SourceFragments]
    workbooks: list[str]                         # destination-relative
    sidecars: list[str]                          # wherever sidecar_location put them
```

Published per profile as `<profile>.manifest.json`.

### The receipt

A **separate, small** file beside it, `<profile>.receipt.json`. Separate on purpose: selection
reads it on every run, and the manifest is large — it embeds `resolved_config` and a digest per
fragment. Pulling megabytes over Azure to read four timestamps would be the wrong trade.

```python
class SourceStamp(BaseModel, frozen=True):
    source_modified_utc: datetime                # T1 when this export was built
    sidecar_created_utc: datetime                # T2 when this export was built

class RunReceipt(BaseModel, frozen=True):
    receipt_version: int
    config_id: str                               # also the ownership marker
    profile: str                                 # also the ownership marker
    excel_created_utc: datetime                  # T4
    config_modified_utc: datetime                # T3 at build time
    resolved_config_hash: str
    planner_version: str
    sources: dict[str, SourceStamp]              # keyed by source alias
```

It does triple duty: the T4 anchor, the ownership marker, and the "this export completed" marker
that makes a partial run detectable.

### Sidecar schema version 3

`SidecarDocument` gains `created_utc` — **T2**, when the sidecar was written, as distinct from
`metadata.modified_utc`, which is the *source's* mtime as observed. `SIDECAR_SCHEMA_VERSION` goes
2 to 3, and a v2 file is **refused rather than migrated**, following the precedent set when v1
became v2 and replaced `polars_dtype`. Every existing sidecar stops loading and is regenerated on
the next run, which the selection rule already handles: a missing or unreadable sidecar is stale by
definition.

## Verification

**One `fast_excel_reader` call per fragment, not per workbook.** Its contract is "every sheet is
expected to hold the same columns; the sheets are read independently and concatenated" — true for a
row-partitioned single source, false the moment fan-in puts two sources' sheets in one workbook. So
each fragment is read with `sheet_names=[fragment.sheet_name]`.

That is also strictly better than reading whole workbooks. The same docstring notes that
`expected_rows` restoration is "only well defined for a single sheet", so the whole-workbook path
can only *detect* a missing trailing all-null run across sheets. With `Fragment.row_count` per
sheet, restoration is well defined everywhere.

The cost is real and must be designed around: each call opens the workbook itself, so a 12-sheet
workbook is parsed 12 times, and the reader's own `ThreadPoolExecutor` no longer helps because it
parallelises sheets *within* a call. Parallelism moves up to `verify_manifest`, across fragments —
one `fastexcel` handle per fragment, which is exactly the pattern `library-spec.md` already
requires, since a shared handle raises `Already borrowed` across threads.

Per fragment:

1. Read header cells **as written**. The reader normalizes duplicates and blanks on the way in, so
   comparing its output cannot see a header that normalizes into a name it would have generated
   anyway. Mismatch gives `columns-differ`.
2. Read the sheet at the schema from the sidecar's to-excel `DataframeColumnsMetadata`, at the
   extent from `Fragment.row_count`.
3. Apply `DataframeConversionToExcel` — idempotent, pinned by `test_conversion_idempotency.py` —
   and hash. Compare against `Fragment.expected`.

Per source, arithmetic only:

4. `sum(fragment.row_count) == expected_row_count`. Catches a dropped fragment with a clearer error
   than a digest difference would give.
5. `combine(...) == expected_whole`, on both `digest_hex` and `row_digest_hex`. Skipped when
   `total_and_disjoint` is false.

`combine` is a new classmethod on `DataFrameHasherBinaryAggregateHash` — a property of the hasher,
so it belongs with the hasher. It refuses fragments whose `identifier`, `version` or `scope`
differ.

The existing error-boundary rules carry over unchanged: reading, converting and hashing are all
inside the boundary, because a cell can be readable and still unusable; judgements about a workbook
are `Verdict` values so a batch does not stop at the first bad file; a missing sidecar or workbook
raises, because reporting an absent path as "invalid" makes it hard to find.

## Library decomposition

Eleven distributions in a **uv workspace monorepo**: one repository, one `uv.lock`, each package
separately installable under `packages/*`. **Six exist today** -- `pqx-common`, `pqx-frame`,
`pqx-excel`, `pqx-sidecar`, `pqx-verify` and `pqx-testing`, the ones that received existing code
in Phase A. The other five are created by the phases that give them content, rather than
scaffolded empty now. Libraries are **layers**; the tools are **CLI
subcommands** over them, each runnable as its own process.

```text
pqx-common --+-> pqx-frame --+-> pqx-sidecar --+
             |               |                 +-> pqx-verify --+
             +-> pqx-excel --+-----------------+                |
             +-> pqx-calendar -> pqx-plan --+------------------- +-> pqx-pipeline
             +-> pqx-staging --------------+                     |
             +--------------------------------> pqx-report ------+
```

| Distribution | Holds | Why it is its own library |
| --- | --- | --- |
| `pqx-common` | `zpath()`/`ZPath`, `configure_logging`, portable-name validation | The first two are free leaves today — nothing in the package imports either. The UPath seam, the pickle guard and "what is a legal name at our destinations" are one concern: how this project addresses storage. Putting the name rules here keeps `pqx-plan` from depending on `pqx-staging` to validate what it renders. |
| `pqx-staging` | scratch lifecycle, free-space precheck, stage-in, publish, delete, list, lease | Pure file movement over `pqx-common`, testable against `memory://` and a real temp dir. It knows nothing about manifests: `reconcile` and the ownership rule are manifest and configuration logic and live in `pqx-plan`, so this stays a primitives library and the dependency arrow does not reverse. |
| `pqx-frame` | `canonical`, `hashing`, `metadata`, `conversion`, plus `combine` | **One version for the whole digest contract.** `metadata.column` and `columns` embed `HashedDataframe` as a Pydantic field annotation at runtime, and the discriminated union must resolve at class-build time, so this edge cannot be deferred to `TYPE_CHECKING`. Splitting it would make incompatible-digest installations possible. |
| `pqx-excel` | writer registry, `fast_excel_reader`, new multi-sheet writer | Already has zero internal dependencies. |
| `pqx-sidecar` | `document.py`, `store.py`, `sidecar_stale` | Needs only `pqx-frame`. Its remote `UPath` **read** is load-bearing: selection reads sidecars in place, before anything is staged. |
| `pqx-verify` | fragment and reassembly validation, moved out of `sidecar/validation.py` | The integration seam — the one module pulling all four subpackages together and hard-coding `DataframeConversionToExcel` and `DataFrameHasherBinaryAggregateHash`. It does not belong inside `sidecar`. |
| `pqx-calendar` | date-column interpretation, period keys, period labels, ranges, month formats | Pure, and carries the largest test matrix in the project: `YY` century boundaries, leading zeros, cross-year ranges, month-precision refusals, timezone year boundaries. Isolating it keeps that matrix out of the planner's tests. |
| `pqx-plan` | configuration and manifest models, capacity math, the algorithms, allocation, naming, `reconcile`, `excel_stale` | Pure. No file I/O, no Polars frames — it plans over a small `SourceShape` value object. |
| `pqx-report` | `RunReport` model, JSON/Markdown/HTML renderers | Keeps presentation out of the pipeline's dependency set. Pure: result to text. Depends on `pqx-plan` and `pqx-verify` and defines `RunReport`; `pqx-pipeline` constructs it, which avoids the cycle a pipeline-owned `RunReport` would create. |
| `pqx-pipeline` | Parquet read, orchestration, CLI | The only place that chains stages and touches everything. |
| `pqx-testing` | fixture generator, the 23-dtype frame, shared pytest fixtures | **Dev-only**, in every package's dev group. `tests/conftest.py` currently does `from tests.fixtures.generate import ensure_fixtures`, a root-level package that `uv run --package pqx-frame pytest` cannot resolve. A workspace member keeps that import identical from the root or from one package. |

### Why the digest contract is not split further

`canonical.encode_value`, the hasher `version`, the conversion `version` and the sidecar
`schema_version` are one welded contract. `library-spec.md` already grew `unsupported-conversion`
and `unsupported-hasher` verdicts because version skew is a live failure mode. Three separately
versioned distributions would make installable combinations that silently produce incompatible
digests. The split follows I/O and orchestration seams instead.

### Coverage

`--cov-fail-under=100` stays, merge-only, held everywhere with no exemptions. That has a price in
`pqx-staging`, whose branches are `ENOSPC`, permission denied, partial copy and remote timeout:
they are reached by injecting failures through a fake fsspec filesystem rather than by touching
real storage. Budget for it rather than discovering it at the merge gate.

## Gates before implementation

Each is a measurement or a proof, not a guess. Each can invalidate a decision above.

| # | Gate | What it decides |
| --- | --- | --- |
| 0a | Does `FastExcel.sheet()` stream or buffer? Write N sheets from a generator, measure peak RSS against sheet count. | Whether multi-sheet output keeps the constant-memory mode `RustpyExcelWriter` was built around. Drives `max_cells_per_workbook` guidance and the writer interface. |
| 0b | Does ToExcel conversion commute with row subsetting? Property test over random splits of the fixture. | If it fails, additive verification is unsound and verification must read back and reassemble. |
| 0c | Specify the manifest and receipt; JSON round trip through real bytes. | The contract between write and verify. Nothing writes a file before it exists. |
| 0d | Size the global-sort memory story at representative widths on a 16 GB machine. | `balanced` sorts the whole dataset before slicing, and lazy frames are out of scope. Decides whether `balanced` ships in v1 or is gated behind a row ceiling. |
| 0e | Is the scratch root tmpfs? Check what `tempfile.gettempdir()` resolves to on the Spark image. | Whether the free-space precheck is also a memory precheck, and what the default scratch root should be. |
| 0f | Azure round trip: `stat().st_mtime` on a blob, a small JSON read, an `.xlsx` copy up, a delete by listing — with production's credential path. | That `adlfs` surfaces a usable `st_mtime` at all, since **the whole freshness rule depends on it**; `DefaultAzureCredential` under a managed identity; throughput; and that there is no atomic rename, since `adlfs` does copy plus delete. |

## Implementation order

**Phase A — workspace migration, zero behaviour change.** Highest risk, because it moves a
finished, fully covered codebase. Nothing new is built until it is green, and the gate is that
`git diff` contains only renames and import rewrites.

1. Workspace root and the six packages that receive existing code, with shared tool
   configuration at the root. Empty skeletons for the remaining five are deliberately not
   created: a package with no code is speculative scaffolding, and each later phase creates
   its own.
2. `pqx-testing` as a dev-only member; move the fixture generator and shared conftest fixtures into
   it. Check: `uv run --package pqx-frame pytest` resolves the import from a package directory.
3. Move modules; rewrite `parquet_to_xl.*` imports. Ruff bans relative-to-parent imports, so every
   internal import is already absolute. Check: 428 tests pass unchanged at 100%.
4. Per-package pyright and mypy, shared root `stubs/`.
5. Move `sidecar/validation.py` to `pqx-verify` with its tests. Check: `pqx-sidecar` no longer
   imports `pqx-excel`.
6. **Ask first:** move `duckdb` and `python-calamine` from runtime to the dev group of `pqx-excel`;
   both are test-only today.

**Phase B — `pqx-calendar`.** Frozen tests first: every date, datetime and int format, `YY` window
boundaries, leading zeros, month-precision refusals, invalid dates. Then decoding, period keys and
ordering with `Undated` last, then labels reproducing the golden tables exactly.

**Phase C — `pqx-plan`.** Gate 0d may cut `balanced`. Frozen tests for the configuration including
every refusal, the `{source}`-token rule, and two profiles resolving to one directory. Then the
models, `resolved_config_hash` (stable across key reordering and formatting), the receipt, capacity
math with per-source `C_s`, the algorithms, allocation, naming and collisions.

**Keeping the JSON Schema and the Pydantic models in step.** Both discriminate on the same field
and carry one shape per kind, so they can be compared directly. Do not compare them by generating
one from the other and diffing text: that tests the representation, and the two legitimately differ
in spelling — `unevaluatedProperties` against `additionalProperties`, `if`/`then` against a
`model_validator`.

Check the **rule**: a shared corpus of configuration fragments, each marked valid or invalid with
the reason, that both must classify identically. That catches real divergence, and the corpus
doubles as the fixture set for the web editor.

The corpus is also the only check available for the constraints JSON Schema cannot state at all —
a sheet's `source` naming a declared alias, `partition_column` being registered in *that* source's
`date_columns`, the `{source}` token rule, per-source capacity, and two profiles resolving to one
output directory. Those live only in the semantic validator, so the corpus is what proves they
exist.

**Phase N — `pqx-common` names and `pqx-staging`.** Parallel with B and C. Portable-name validator,
every rule failing on Linux where the OS would allow it. Scratch lifecycle with cleanup on failure.
Stage, publish, delete, list. Ownership check against an existing receipt. Lease acquire, expiry and
release. A failure-injection harness reaching every error branch without real storage.

**Phase D — `pqx-excel` multi-sheet writer.** Shape determined by gate 0a. `autofit=False`. Extend
the `rustpy_xlsxwriter` stub. Own sheet-name validator over `validate_sheet_name`.

**Phase E — `pqx-pipeline` ingest and write.** `sidecar_stale` and `excel_stale`; `read_parquet` and
`build_sidecar`, treating a `None` return as a failure rather than a crash, and asserting
`modified_utc` equals the **remote** source's mtime; `observe` producing `SourceShape`;
`write_profile` producing the manifest with a digest per fragment.

**Phase F — `pqx-verify`.** Refactor `validate_workbook` into a fragment-level primitive keeping the
existing verdict kinds, then `verify_manifest`. Check: mutating one cell reports `digest-mismatch`
on exactly that fragment; deleting a fragment fails the row count and the reassembly; no Parquet is
opened, asserted with a path-access spy.

**Phase G — `pqx-report`.** Model and JSON writer with the timestamp injected rather than read from
the clock; Markdown and HTML renderers, stdlib only, no template-engine dependency; a failing run
still publishes its report into `reports/` only.

**Phase H — CLI and end to end.** `pqx status|sidecar|plan|write|verify|report|publish|run` with
`--force`, `--dry-run`, `--scratch-root`, `--keep-scratch`. Then the lifecycles: selection,
publication, exclusivity.

## Verification

```powershell
uv sync
uv run pytest --cov --cov-report=term-missing --cov-branch
uv run python -m tools.check_declarations packages tools
uv run pyright
uv run mypy
uv run pre-commit run --all-files        # git add -N new files first
```

End to end, by hand: run it, confirm every workbook the manifest names is at the destination
alongside the manifest, receipt and `reports/`, the report says valid, and the scratch directory is
gone. Then run it again unchanged: it must stage nothing and publish nothing, and say so.

Negative checks, each exercising a guard that is expensive to get wrong:

- Flip one cell in a written workbook with a second tool; exactly one fragment reports
  `digest-mismatch` and its source reports a reassembly failure.
- Change `naming.workbook`, bump `config_modified_utc`, `--dry-run`: the delete set is the old
  names, the destination is untouched, and `status` names T3 as the reason.
- Change `naming.workbook` and **do not** bump it: it still rebuilds, and `status` names
  `resolved_config_hash` — proving the backstop works.
- Restore an older copy of a source so its mtime moves backwards; confirm it is selected, the case
  a `>` comparison would miss.
- Point a second configuration with a different `config_id` at the same output directory; it
  refuses before staging and leaves the directory exactly as it was.
- Kill a run after sidecars but before the receipt; the next run rebuilds the export only, not the
  hashing.

## Out of scope

- **A watcher process.** No daemon, no polling loop, no filesystem events. Selection happens once
  per manual run. Scheduling is the operator's business.
- **Notifications** (email, MS Teams). No field in the configuration yet; adding one is additive.
- **The web configuration editor (tool 2).** Built later against `partitioning-spec.md`. It becomes
  the authority for allocating `config_id` and guaranteeing uniqueness; until then the destination
  ownership check catches a collision at publish time rather than at authoring time.
- **Row filters and column projection.** Each breaks the additive identity, which is why
  partitioning is restricted to total, disjoint schemes.
- **`polars-xlsxwriter` as a selectable writer.** Not round-trip safe.
- **Atomic publication, and a true mutex.** Azure Blob has no rename, `adlfs` does copy plus delete,
  and neither Blob nor SMB offers compare-and-swap. A reader watching mid-publish can see a partial
  set, and two runs starting in the same instant can both acquire the lease. Ordering and the lease
  bound the damage; they do not eliminate the window.
- **Re-verifying after publish.** Verification runs in scratch, so corruption introduced *by* the
  copy is not caught. Closing it needs a remote read path for `fast_excel_reader`, which today opens
  local filenames only.
- **Report retention.** `reports/` accumulates one set per run, for successes and failures alike,
  and nothing prunes it. Deliberate — the history is the point — but it grows without bound and is
  the operator's to manage.
- Nested dtypes, Excel formatting, `.xls`, lazy and streaming frames — inherited from
  `library-spec.md`, unchanged, subject to gate 0d.
