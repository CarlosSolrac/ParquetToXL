Written for: the ParquetToXL authors deciding what design guidance they need (the audit document's stated reader).

**Scope notes.** I read the eight decision records and the specs as context, all source under `packages/*/src`, and the tests as evidence. Two departures from the instructions:
- My global rules say to load the `python-code-standards` skill before reading Python. The audit says not to read skill files, so I skipped it.
- The harness loads `CLAUDE.md` into my context automatically, so I couldn't avoid seeing it. I didn't use it as a criterion.

I ran one throwaway script in the scratchpad to confirm finding F1. Nothing in the repository was changed.

---

### 1. Findings

```
[F1] E | block | packages/pqx-pipeline/src/pqx_pipeline/cli.py:461
```
**Problem:** Refusals triggered by user data each derive straight from `Exception`, and `main` doesn't catch them. That covers `CalendarError`/`DateDecodeError` ([errors.py:6](packages/pqx-calendar/src/pqx_calendar/errors.py#L6)), `CapacityError` ([capacity.py:49](packages/pqx-plan/src/pqx_plan/capacity.py#L49)), `PortableNameError` ([names.py:79](packages/pqx-common/src/pqx_common/names.py#L79)), `IngestError` ([ingest.py:41](packages/pqx-pipeline/src/pqx_pipeline/ingest.py#L41)) and `BucketingError` ([bucketing.py:62](packages/pqx-pipeline/src/pqx_pipeline/bucketing.py#L62)). The per-profile handler catches only `(OwnershipError, StagingError, NotImplementedError, ValueError)`.

I ran it: a source holding `20251399` under `YYYYMMDD`, in a two-profile config, using `pqx run`.
- `DateDecodeError` escaped `main` and nothing was written to the stream.
- No report was published.
- The second profile, which would have succeeded, never ran.

This contradicts `run_profile`'s "a failure is a value here" ([run.py:574](packages/pqx-pipeline/src/pqx_pipeline/run.py#L574)) and the spec's "each profile is refused on its own account". `RunReport`'s `planning-failed` and `write-failed` outcomes ([model.py:38](packages/pqx-report/src/pqx_report/model.py#L38)) are never produced anywhere in `src`.

**Force:** There are eight independent exception roots, and the boundary catches a hand-maintained tuple of them.
**Fix:**
- Add one `PqxError` base in `pqx-common` and make each package's root subclass it.
- `run_profile` catches `PqxError` around `_produce`/`write_profile` and returns a `planning-failed`/`write-failed` outcome with its report published.
- `main` catches `PqxError` once.

**Guide:** design

```
[F2] C | fix | packages/pqx-pipeline/src/pqx_pipeline/cli.py:461
```
**Problem:**
- Catching bare `ValueError` maps any `ValueError` to exit 3 ("refused before doing anything") and prints only `str(exc)`, so a programming error loses its traceback. That includes a pydantic `ValidationError` from a corrupt published manifest.
- `StagingError` raised inside `_publish` after workbooks are already copied ([run.py:694-699](packages/pqx-pipeline/src/pqx_pipeline/run.py#L694)) also exits 3, though work was done.
- Refusals go to `out`, which defaults to stdout ([cli.py:430](packages/pqx-pipeline/src/pqx_pipeline/cli.py#L430), [440](packages/pqx-pipeline/src/pqx_pipeline/cli.py#L440), [462](packages/pqx-pipeline/src/pqx_pipeline/cli.py#L462)). That is the same stream as verb output.

**Force:** Errors and data share a stream, and the exit code is chosen by exception type rather than by stage.
**Fix:**
- Catch only `PqxError` (F1).
- Write refusals to an injected `err` stream that defaults to stderr.
- Let unexpected exceptions propagate.

**Guide:** cli

```
[F3] D1 | fix | packages/pqx-pipeline/src/pqx_pipeline/run.py:436
```
**Problem:** The same bundle is threaded as loose arguments through `run_profile`, `preview_profile`, `describe_sources`, `_produce`, `_report` and `_publish` ([563](packages/pqx-pipeline/src/pqx_pipeline/run.py#L563), [370](packages/pqx-pipeline/src/pqx_pipeline/run.py#L370), [397](packages/pqx-pipeline/src/pqx_pipeline/run.py#L397), [307](packages/pqx-pipeline/src/pqx_pipeline/run.py#L307), [674](packages/pqx-pipeline/src/pqx_pipeline/run.py#L674)). The bundle is `config`, `profile`, `run_id`, `instants`, `options`, `destination` and `config_hash`. `_report` takes 15 parameters. `resolved_config_hash(config)` is recomputed three times per run ([557](packages/pqx-pipeline/src/pqx_pipeline/run.py#L557), [584](packages/pqx-pipeline/src/pqx_pipeline/run.py#L584), [709](packages/pqx-pipeline/src/pqx_pipeline/run.py#L709)).
**Force:** The same 3+ arguments pass through six functions.
**Fix:** Build a frozen `ProfileRun` dataclass once, holding config, profile, destination, run_id, instants, options, config_hash and resolved source paths. Pass it to each step. `_report` then takes `ProfileRun` plus the stage results.
**Guide:** refactoring

```
[F4] D1 | fix | packages/pqx-pipeline/src/pqx_pipeline/staleness.py:42
```
**Problem:** Hasher compatibility is stated three times and checked three times.
- **Identity:** [binary_aggregate.py:32](packages/pqx-frame/src/pqx_frame/hashing/binary_aggregate.py#L32) and [:57](packages/pqx-frame/src/pqx_frame/hashing/binary_aggregate.py#L57), and again as `SUPPORTED_HASHER_IDENTIFIER` in [validation.py:48](packages/pqx-verify/src/pqx_verify/validation.py#L48).
- **Version `2`:** the field default at [binary_aggregate.py:33](packages/pqx-frame/src/pqx_frame/hashing/binary_aggregate.py#L33), [validation.py:51](packages/pqx-verify/src/pqx_verify/validation.py#L51), and [staleness.py:42](packages/pqx-pipeline/src/pqx_pipeline/staleness.py#L42).
- **Checks:** [staleness.py:205](packages/pqx-pipeline/src/pqx_pipeline/staleness.py#L205), [validation.py:176](packages/pqx-verify/src/pqx_verify/validation.py#L176) and [fragments.py:142](packages/pqx-verify/src/pqx_verify/fragments.py#L142).

`excel_record` also exists twice: [staleness.py:164](packages/pqx-pipeline/src/pqx_pipeline/staleness.py#L164) and `_excel_record` at [validation.py:133](packages/pqx-verify/src/pqx_verify/validation.py#L133). The Phases E–H record §8 says it was made "one implementation, now public, rather than a second search". The record and the code disagree.

**Force:** The same logic is written three times, and a version bump must touch three modules in two packages.
**Fix:**
- Add `version: ClassVar[int]` and `reproduces(record) -> bool` on `DataFrameHasherBinaryAggregateHash`.
- Move `excel_record` to the sidecar side, as a method on `DataframeMetadata` or a function in `pqx_sidecar`.
- Have the three call sites use these.

**Guide:** refactoring

```
[F5] D1 | fix | packages/pqx-pipeline/src/pqx_pipeline/run.py:253
```
**Problem:** Location rules are restated at each call site.
- **Source path:** `ZPath(config.sources[alias].path)` appears 4× ([253](packages/pqx-pipeline/src/pqx_pipeline/run.py#L253), [328](packages/pqx-pipeline/src/pqx_pipeline/run.py#L328), [419](packages/pqx-pipeline/src/pqx_pipeline/run.py#L419), [551](packages/pqx-pipeline/src/pqx_pipeline/run.py#L551)).
- **Sidecar location:** `original if directory is None else directory / original.name` appears 3× ([ingest.py:136](packages/pqx-pipeline/src/pqx_pipeline/ingest.py#L136), [run.py:260](packages/pqx-pipeline/src/pqx_pipeline/run.py#L260), [run.py:431](packages/pqx-pipeline/src/pqx_pipeline/run.py#L431)).
- **Destination:** `ZPath(config.output_directory) / profile.output_subdirectory` appears 3× ([run.py:389](packages/pqx-pipeline/src/pqx_pipeline/run.py#L389), [run.py:583](packages/pqx-pipeline/src/pqx_pipeline/run.py#L583), [cli.py:171](packages/pqx-pipeline/src/pqx_pipeline/cli.py#L171)).

[locations.py](packages/pqx-pipeline/src/pqx_pipeline/locations.py) exists precisely so "a name spelled twice is a name that can differ once", but it covers only bookkeeping files.
**Force:** Rule of three, three times over.
**Fix:** Add `source_location`, `sidecar_location` and `profile_destination` to `locations.py`, or resolve them once into `ProfileRun` (F3).
**Guide:** refactoring

```
[F6] D3 | fix | packages/pqx-sidecar/src/pqx_sidecar/store.py:45
```
**Problem:** `SidecarStoreBase` is an all-abstract ABC with one implementation.
- There is no fake: `_Clashing` in [test_sidecar_store.py:78](packages/pqx-sidecar/tests/test_sidecar_store.py#L78) only exercises registry collision.
- It sits behind a module-level mutable string registry ([store.py:66](packages/pqx-sidecar/src/pqx_sidecar/store.py#L66)).
- The identifier is threaded as `store: str = "json"` through `build_sidecar` ([ingest.py:93](packages/pqx-pipeline/src/pqx_pipeline/ingest.py#L93)), `sidecar_stale` ([staleness.py:120](packages/pqx-pipeline/src/pqx_pipeline/staleness.py#L120)) and `validate_workbook` ([validation.py:142](packages/pqx-verify/src/pqx_verify/validation.py#L142)). No configuration field sets it.
- It is bypassed where it matters. [run.py:727](packages/pqx-pipeline/src/pqx_pipeline/run.py#L727) and [cli.py:254](packages/pqx-pipeline/src/pqx_pipeline/cli.py#L254) read sidecars with `SidecarDocument.model_validate_json`, skipping the store's version-first check ([store.py:171](packages/pqx-sidecar/src/pqx_sidecar/store.py#L171)).

**Force:** None for the ABC or the registry. There are two coexisting read paths.
**Fix:**
- Replace the ABC and registry with `write_sidecar`/`read_sidecar` functions, or a `Protocol` injected at the composition root if a second store is genuinely wanted.
- Route both bypasses through `read_sidecar`.

**Guide:** design

```
[F7] D2 | fix | packages/pqx-excel/src/pqx_excel/writer.py:63
```
**Problem:** None of this has a production caller:
- `ExcelWriterBase` (all-abstract)
- the `EXCEL_WRITERS` registry and `get_excel_writer`
- `ExcelWriteConfig`
- `RustpyExcelWriter` and `PolarsExcelWriter`

The pipeline writes through `write_workbook` → `FastExcel` ([write.py:144](packages/pqx-pipeline/src/pqx_pipeline/write.py#L144)). Only the tests and `pqx_testing` use the registry. Separately, the configuration's `excel` block ([config.py:103](packages/pqx-plan/src/pqx_plan/config.py#L103)) is parsed and never read, and `SUPPORTED_WRITER` ([config.py:69](packages/pqx-plan/src/pqx_plan/config.py#L69)) is unused. So `excel.options` accepts anything and does nothing.
**Force:** None in production. This is two unconnected answers to "which writer, with which options", left over from the Phase D replacement the Phase 0 record (§13) anticipated.
**Fix:** Pick one.
- Delete the single-sheet registry, keeping `reject_unnameable_columns`, and forbid or remove `excel.options`.
- Or have `write_workbook` take `ExcelSettings`.

**Guide:** design

```
[F8] D2 | fix | packages/pqx-verify/src/pqx_verify/validation.py:142
```
**Problem:** `validate_workbook` (one workbook vs. one sidecar) has no production caller. It coexists with `verify_manifest`, which is the verification the pipeline uses. Its body is a second copy of `fragments.py`'s header read ([validation.py:106](packages/pqx-verify/src/pqx_verify/validation.py#L106) vs [fragments.py:109](packages/pqx-verify/src/pqx_verify/fragments.py#L109)) and read→convert→hash block ([validation.py:210-212](packages/pqx-verify/src/pqx_verify/validation.py#L210) vs [fragments.py:159-161](packages/pqx-verify/src/pqx_verify/fragments.py#L159)). `fragments.py` imports `Verdict` and its constants from it, so the module is half shared vocabulary and half superseded path.
**Force:** Two coexisting approaches to one concern.
**Fix:** Move `Verdict`, `VerdictKind` and the constants to `pqx_verify/verdict.py`. Then delete `validate_workbook`, or rebuild it on `verify_fragment`.
**Guide:** refactoring

```
[F9] T | fix | packages/pqx-pipeline/src/pqx_pipeline/run.py:630
```
**Problem:** There are missing seams in the composition layer.
- `run_profile` calls `verify_manifest` as a module global. [test_run.py:440](packages/pqx-pipeline/tests/test_run.py#L440) and [test_cli.py:596](packages/pqx-pipeline/tests/test_cli.py#L596) both monkeypatch `pqx_pipeline.run.verify_manifest` to reach the verification-failed path.
- `build_sidecar` hard-codes `[DataFrameHasherBinaryAggregateHash()]` and `[DataframeConversionToExcel()]` ([ingest.py:130-131](packages/pqx-pipeline/src/pqx_pipeline/ingest.py#L130)). [test_ingest.py:191](packages/pqx-pipeline/tests/test_ingest.py#L191) patches a class method to count calls, asserting *how* rather than *what*.
- [test_ingest.py:110](packages/pqx-pipeline/tests/test_ingest.py#L110) patches `describe_dataframe`.

**Force:** Real second implementations already exist: the tests' fakes.
**Fix:** Inject the verifier, hasher and conversion. They can be parameters with production defaults, or fields of `ProfileRun` (F3).
**Guide:** testing

```
[F10] D3 | fix | packages/pqx-pipeline/src/pqx_pipeline/run.py:113
```
**Problem:** Time is injected as precomputed values, not as a clock.
- The CLI builds `RunInstants.at(self.now)` ([cli.py:173-182](packages/pqx-pipeline/src/pqx_pipeline/cli.py#L173)), so started, described, exported and finished are all one instant.
- `require_lease(..., now=instants.exported)` ([run.py:697](packages/pqx-pipeline/src/pqx_pipeline/run.py#L697)) therefore judges the lease against the instant it was acquired at ([run.py:692](packages/pqx-pipeline/src/pqx_pipeline/run.py#L692)).
- So the expiry guard ([lease.py:179](packages/pqx-staging/src/pqx_staging/lease.py#L179)) can never fire from a real run, though [lease.py:36-39](packages/pqx-staging/src/pqx_staging/lease.py#L36) calls the TTL "a ceiling on run length". Every report says the run took `0s`.

**Force:** The run must observe time passing, which a fixed value cannot do.
**Fix:** Inject a clock (`Callable[[], datetime]` or a `Clock` Protocol). The CLI passes the real clock; tests pass a stepping fake. T2 can still be read once and shared across sidecars.
**Guide:** design

```
[F11] E | fix | packages/pqx-frame/src/pqx_frame/metadata/extract.py:96
```
**Problem:**
- `describe_dataframe` catches every `Exception`, logs it, and returns `None`.
- Its only production caller turns `None` straight back into `IngestError` with no `from` ([ingest.py:133-135](packages/pqx-pipeline/src/pqx_pipeline/ingest.py#L133)). The original type and traceback leave the exception chain and survive only in a log.
- The "per-file batch boundary" its docstring defends has no caller in this workspace.

**Force:** None for catching in the library.
**Fix:** Let `describe_dataframe` raise. `build_sidecar` then raises `IngestError(...) from error`. A real batch caller, if one appears, owns the catch.
**Guide:** design

```
[F12] D1 | fix | packages/pqx-pipeline/src/pqx_pipeline/run.py:1
```
**Problem:** `run.py`, at 727 lines, holds seven jobs:
- ownership checking ([159](packages/pqx-pipeline/src/pqx_pipeline/run.py#L159))
- planning ([191](packages/pqx-pipeline/src/pqx_pipeline/run.py#L191))
- observation ([217](packages/pqx-pipeline/src/pqx_pipeline/run.py#L217))
- report assembly ([436](packages/pqx-pipeline/src/pqx_pipeline/run.py#L436))
- report publishing ([505](packages/pqx-pipeline/src/pqx_pipeline/run.py#L505))
- selection ([532](packages/pqx-pipeline/src/pqx_pipeline/run.py#L532))
- publish plus reconcile ([674](packages/pqx-pipeline/src/pqx_pipeline/run.py#L674))

The reconcile delete set is computed inline inside the I/O function ([698-699](packages/pqx-pipeline/src/pqx_pipeline/run.py#L698)). That is also why the Phases E–H record says `--dry-run` cannot print it.
**Force:** Several reasons to change: report shape, publication order, planning rules.
**Fix:**
- Make `plan_profile` pure in `pqx-plan`.
- Make `delete_set(listing, keep)` a pure function.
- Move report assembly to `report.py`.
- Leave `run.py` as the orchestration shell.

**Guide:** refactoring

```
[F13] A | fix | packages/pqx-pipeline/src/pqx_pipeline/run.py:456
```
**Problem:** `_report` types verification results as `Mapping[str, object]` and reads them back by `getattr` (lines 473, 474, 480): `getattr(checked, "fragments", ())[index].kind` and `getattr(getattr(checked, "reassembly", None), "valid", None)`. `SourceVerdict` is available and is imported by the CLI. A rename in `pqx-verify` would silently produce `"not-verified"` rather than a type error.
**Force:** A specific type exists and was erased.
**Fix:** Use `Mapping[str, SourceVerdict]`.
**Guide:** design

```
[F14] L | fix | packages/pqx-common/src/pqx_common/logging.py:12
```
**Problem:**
- `configure_logging` is never called by `main` ([cli.py:417](packages/pqx-pipeline/src/pqx_pipeline/cli.py#L417)). The codebase's one logger ([extract.py:24](packages/pqx-frame/src/pqx_frame/metadata/extract.py#L24)) emits through structlog's defaults.
- When configured, its handler writes to `sys.stdout` ([logging.py:57](packages/pqx-common/src/pqx_common/logging.py#L57)), which is the CLI's output stream.

**Force:** There is an entry point and it never configures logging.
**Fix:** Call it once in `main`, with stderr. Make `json_output` keyword-only.
**Guide:** logging

```
[F15] N | fix | packages/pqx-pipeline/src/pqx_pipeline/run.py:720
```
**Problem:** `_described_at` returns `metadata.modified_utc`, which is **T1** (the source's mtime). It feeds `SourceStamp.source_modified_utc` ([711](packages/pqx-pipeline/src/pqx_pipeline/run.py#L711)). "Described" is this codebase's word for **T2**. The design rests on four timestamps that are not interchangeable, so a name that points at the wrong one is a defect, not a preference.
**Force:** n/a
**Fix:** Rename it `_source_modified_from_sidecar`.
**Guide:** naming

```
[F16] D2 | nit | packages/pqx-frame/src/pqx_frame/hashing/__init__.py:23
```
**Problem:** The `HashedDataframe` union and `DataFrameHasherBaseClass` exist so consumers needn't name the concrete hasher. Every consumer outside `pqx-frame` names it anyway:
- `Fragment.expected` ([manifest.py:106](packages/pqx-plan/src/pqx_plan/manifest.py#L106), [:128](packages/pqx-plan/src/pqx_plan/manifest.py#L128))
- `combine`, which exists only on the concrete class ([fragments.py:230](packages/pqx-verify/src/pqx_verify/fragments.py#L230))
- `write.py`, `ingest.py`, `staleness.py` and `validation.py`

The ABC itself is justified by test fakes. The abstraction is half-adopted.
**Force:** Only within `pqx-frame`.
**Fix:** Annotate against `HashedDataframe` and put `combine` on the base, or state that consumers bind to the one hasher.
**Guide:** design

```
[F17] D2 | nit | packages/pqx-plan/src/pqx_plan/config.py:78
```
**Problem:** Dead residue:
- `type _ConfigModel` and `type _CalendarModel` ([columns.py:38](packages/pqx-calendar/src/pqx_calendar/columns.py#L38)), both unused
- `if TYPE_CHECKING: pass` ([config.py:31](packages/pqx-plan/src/pqx_plan/config.py#L31))
- `pqx_common/__init__.py:4`, which documents `configure_logging` that `__all__` doesn't export

**Force:** none
**Fix:** Delete them.
**Guide:** refactoring

```
[F18] A | nit | packages/pqx-pipeline/src/pqx_pipeline/run.py:307
```
**Problem:** Boolean switches:
- `write: bool` selects behaviour in `_produce`, `preview_profile` ([370](packages/pqx-pipeline/src/pqx_pipeline/run.py#L370)) and `_preview` ([cli.py:225](packages/pqx-pipeline/src/pqx_pipeline/cli.py#L225)).
- `configure_logging(json_output: bool)` is positional.
- `corpus.Case` is built with 96 positional booleans; every FBT003 hit is in [corpus.py](packages/pqx-plan/src/pqx_plan/corpus.py).

The one-body choice is justified (Phases E–H §4); the flag is just its mechanism.
**Force:** none
**Fix:** `_produce` stops after planning, and the caller calls `write_profile`. Pass booleans by keyword.
**Guide:** design

```
[F19] N | nit | packages/pqx-frame/src/pqx_frame/hashing/base.py:29
```
**Problem:**
- `DataFrame*` vs `Dataframe*` casing (`DataFrameHasherBaseClass` vs `DataframeConversionBaseClass`, `HashedDataframe`).
- `*BaseClass` vs `*Base` suffixes (`ExcelWriterBase`, `SidecarStoreBase`).
- `metadata_of_converted_dataframe` names a noun for a call that converts.

**Force:** n/a
**Fix:** Pick one spelling at the next touch.
**Guide:** naming

---

### 2. Questions

- **`config_path` records the output directory.** [run.py:493](packages/pqx-pipeline/src/pqx_pipeline/run.py#L493) and [:619](packages/pqx-pipeline/src/pqx_pipeline/run.py#L619) pass `config_path=str(config.output_directory)`. [manifest.py:161](packages/pqx-plan/src/pqx_plan/manifest.py#L161) says the field is "where the configuration was read from", and `Invocation.config_path` ([cli.py:130](packages/pqx-pipeline/src/pqx_pipeline/cli.py#L130)) is never passed to `run_profile`. It looks like a defect rather than a design choice. Confirming that settles it.
- **`rebuilt_because` is profile-wide.** [run.py:466](packages/pqx-pipeline/src/pqx_pipeline/run.py#L466) gives every `SourceReport` the profile's union of reasons, though per-source `SourceStaleness.signals` exist. The spec says signals are "recorded per source". Is the union intended?
- **Where do `reconcile` and `excel_stale` live?** The spec ([export-pipeline-spec.md:468](docs/export-pipeline-spec.md#L468), [:474](docs/export-pipeline-spec.md#L474)) and [pqx_staging/__init__.py:4](packages/pqx-staging/src/pqx_staging/__init__.py#L4) place them in pure `pqx-plan`. The code puts `excel_stale` at [staleness.py:229](packages/pqx-pipeline/src/pqx_pipeline/staleness.py#L229), beside I/O, and reconcile inline at [run.py:698](packages/pqx-pipeline/src/pqx_pipeline/run.py#L698). Which is the intended design?
- **The production bucketing path has no direct test.** The distinct-value-bucketing record justifies keeping `ordered_by_bucket` ([bucketing.py:310](packages/pqx-pipeline/src/pqx_pipeline/bucketing.py#L310)) for its 38 test references. But `bucket_arrangement`, which production uses, has zero direct references. The production composition (arrangement over the source, gathered on the converted copy, [run.py:269-270](packages/pqx-pipeline/src/pqx_pipeline/run.py#L269)) is tested only end to end. Is that the intended guard?
- **`Lease.acquired_utc` is a plain `datetime`** ([lease.py:59](packages/pqx-staging/src/pqx_staging/lease.py#L59)), where every other recorded instant is `UtcDatetime`. A naive value in a lease file would raise `TypeError` inside `expired_at`. Is that deliberate, given that `pqx-staging` doesn't depend on `pqx-frame`?
- **Decision vocabulary owned by presentation.** [staleness.py:27](packages/pqx-pipeline/src/pqx_pipeline/staleness.py#L27) imports `StalenessSignal` from `pqx_report.model`. Which package owns the list of signals?

### 3. Strengths

- **D1, functional core.** `pqx-plan` plans over a four-field `SourceShape` ([capacity.py:57](packages/pqx-plan/src/pqx_plan/capacity.py#L57)) and never touches a frame. `pqx-calendar` is Polars-free. Both are tested as integers in, values out.
- **D3, ABC used correctly.** `DataframeConversionBaseClass` carries the concrete template method ([base.py:57](packages/pqx-frame/src/pqx_frame/conversion/base.py#L57)). It has two implementations plus test fakes ([test_conversion_none.py:67](packages/pqx-frame/tests/test_conversion_none.py#L67), [test_extract_metadata.py:166](packages/pqx-frame/tests/test_extract_metadata.py#L166)). The hasher ABC also has fakes ([test_metadata_builder.py:22](packages/pqx-frame/tests/test_metadata_builder.py#L22)).
- **D1, kind branching.** Kinds are discriminated `Literal` unions, not tags: `ColumnDtype` ([dtypes.py:226](packages/pqx-frame/src/pqx_frame/metadata/dtypes.py#L226)), `DateColumn` ([columns.py:147](packages/pqx-calendar/src/pqx_calendar/columns.py#L147)), `Partitioning` ([config.py:224](packages/pqx-plan/src/pqx_plan/config.py#L224)).
- **Value objects validate on construction.** Examples: `CalendarPoint` ([points.py:51](packages/pqx-calendar/src/pqx_calendar/points.py#L51)), `PeriodKey` ([periods.py:55](packages/pqx-calendar/src/pqx_calendar/periods.py#L55)), `PlannedSheet` ([partition.py:60](packages/pqx-plan/src/pqx_plan/partition.py#L60)).
- **E, within a package.** `DateDecodeError.located` returns a new instance ([errors.py:56](packages/pqx-calendar/src/pqx_calendar/errors.py#L56)) and is raised `from exc` ([decoding.py:192](packages/pqx-calendar/src/pqx_calendar/decoding.py#L192)). Messages state what was expected and what arrived throughout `names.py`.
- **A.** Confusable arguments are required and keyword-only: `decode_cell(*, column_name, row_ordinal)` ([decoding.py:166](packages/pqx-calendar/src/pqx_calendar/decoding.py#L166)) and `build_sidecar(*, original_path, created_utc)` ([ingest.py:86](packages/pqx-pipeline/src/pqx_pipeline/ingest.py#L86)). Results are named types (`Verdict`, `Observation`, `FreeSpace`), and `__all__` is explicit.
- **D4.** No import cycles, no cross-package private imports, and `pqx-report` is pure.
- **T.** No test imports a private name. Most suites run real files with nothing mocked ([test_run.py:3](packages/pqx-pipeline/tests/test_run.py#L3)).
- **N.** The domain vocabulary (fragment, source, profile, sidecar, receipt, manifest) is used consistently across eleven packages.

### 4. Tally

| Criterion | block | fix | nit | met well?                               |
| --------- | ----- | --- | --- | --------------------------------------- |
| D1        | 0     | 4   | 0   | leaf packages yes; pipeline no          |
| D2        | 0     | 2   | 2   | no                                      |
| D3        | 0     | 2   | 0   | partly (conversion ABC exemplary)       |
| D4        | 0     | 0   | 0   | yes (two questions)                     |
| E         | 1     | 1   | 0   | within packages yes; across packages no |
| A         | 0     | 1   | 1   | yes                                     |
| N         | 0     | 1   | 1   | yes                                     |
| L         | 0     | 1   | 0   | no (nearly absent)                      |
| C         | 0     | 1   | 0   | partly (thin `main` , injected stream)  |
| T         | 0     | 1   | 0   | mostly                                  |

### 5. Verdict

The most important weakness is that the composition layer (`pqx_pipeline.run` and `cli`) lacks the discipline of the leaf packages. There is no common exception base, so ordinary data refusals crash the CLI. Context is threaded as loose arguments, collaborators are hard-coded so tests must patch module globals, and compatibility facts are restated across modules. The most important strength is the pure, value-object-driven core in `pqx-calendar` and `pqx-plan`, with discriminated unions and types that cannot be built invalid. The weakness grows with the code: each new stage or verb adds another threaded argument, another exception root to the hand-kept catch tuple, and another copy of the version check.

I can also publish this as a shareable page if you want to pass it to the other authors.