### 1. Findings

Audit date: 2026-10-08. Production packages were reviewed against the supplied design-evaluation prompt, with decision records and specifications as context and package tests as evidence. Findings below concern design; supplied lint and complexity measurements were accepted without rerunning them. No finding warrants `block` severity.

[F1] D1 | fix | `packages/pqx-plan/src/pqx_plan/manifest.py:124`

Problem: `total_and_disjoint: bool = True` exposes a verification alternative for partial exports that do not exist. Setting it false skips whole-source digest reassembly at `packages/pqx-verify/src/pqx_verify/fragments.py:226–227`, and an absent reassembly verdict is accepted by `SourceVerdict.valid` at line 85.

Force: None for the alternative. The field explicitly describes a future filter/projection seam; the only false setter found is `packages/pqx-verify/tests/test_fragments.py:290`, which tests the bypass itself. Filters and projection remain out of scope, and no decision record supplies a present need for this branch.

Fix: Constrain the v1 field to `Literal[True]` and remove the opt-out branch from reassembly. Retain invalid verdicts for unsupported formats; introduce partial-export semantics when an actual implementation exists.

Guide: design — require a present force before introducing behavioral alternatives.

[F2] E | fix | `packages/pqx-pipeline/src/pqx_pipeline/cli.py:461`

Problem: The per-profile boundary catches an assortment of exceptions, including every `ValueError`, while expected pipeline failures such as `IngestError`, `BucketingError`, `CapacityError`, and `CalendarError` sit outside that catch. Consequently, an unreadable Parquet source escapes as a traceback and stops the profile loop, while an unexpected `ValueError` can be mislabeled as an ordinary refusal.

Force: The command already runs multiple profiles independently and distinguishes expected refusals from unexpected failures. `read_parquet` deliberately translates storage/parser failures to `IngestError` at `packages/pqx-pipeline/src/pqx_pipeline/ingest.py:79–83`, but that type derives directly from `Exception` at line 41. A bytecode-disabled probe confirmed that this error does not match the CLI's catch tuple.

Fix: Translate expected stage failures into a small application error hierarchy, preserving their causes, and handle those explicit types once per profile. Keep broad built-in exception catches out of that boundary so programming failures retain their traceback.

Guide: design — establish error ownership and classification across application boundaries.

[F3] T | fix | `packages/pqx-pipeline/src/pqx_pipeline/run.py:630`

Problem: `run_profile` calls the imported `verify_manifest` directly, leaving no supported way to supply a verification failure. Both failure-path tests replace that module global: `packages/pqx-pipeline/tests/test_run.py:440` and `packages/pqx-pipeline/tests/test_cli.py:596`.

Force: A second verification behavior already exists in those tests, and the important outcome is that a failed verification publishes only its report. The tests must know where the orchestrator imported its collaborator to exercise that outcome.

Fix: Inject the narrow verification collaborator at the application composition boundary and supply a small fake in the failure tests. Keep real workbook round trips as integration tests.

Guide: testing — expose seams for collaborators whose outcomes govern application behavior.

[F4] T | fix | `packages/pqx-staging/src/pqx_staging/scratch.py:126`

Problem: `require_free_space` reads `shutil.disk_usage` through a global before deciding whether to refuse the run. Deterministic shortfall and exact-boundary tests must monkeypatch that global at `packages/pqx-staging/tests/test_scratch.py:83`, `:89`, and `:97`.

Force: The suite already supplies another capacity reader through `_usage` at `packages/pqx-staging/tests/test_scratch.py:21–32`. A refusal must be testable without depending on the host's free space or replacing a standard-library global.

Fix: Extract a pure capacity guard accepting the existing `FreeSpace` value, leaving the actual disk reading in the shell; alternatively, inject the capacity-reading callable.

Guide: testing — separate environmental observations from decisions.

[F5] C | fix | `packages/pqx-pipeline/src/pqx_pipeline/cli.py:247`

Problem: Published-export inspection is implemented inside the CLI: `_schema_of` reconstructs schemas from sidecars, `_verify` loads the manifest and assembles verifier inputs at lines 269–276, and `_report` selects and loads the latest report at lines 298–308. These are application workflows rather than argument parsing, output formatting, or exit-code mapping.

Force: Verification and report retrieval are existing operations with their own persistence and selection rules. Their application behavior is currently embedded in command handlers instead of exposed beside the other pipeline operations.

Fix: Extract published-export verification and latest-report retrieval into application functions returning their existing named result types. Leave terminal rendering and exit-code mapping in the CLI.

Guide: cli — keep command handlers as adapters over application operations.

[F6] C | fix | `packages/pqx-pipeline/src/pqx_pipeline/cli.py:440`

Problem: Configuration errors and profile refusals are written to the same output stream as successful command results, at lines 440 and 462. That stream defaults to stdout at line 431, so callers cannot redirect report data separately from diagnostics.

Force: `report` already emits report content, and a multi-profile invocation can produce both successful output and refusals. One injected stream cannot preserve separate data and diagnostic channels.

Fix: Add a separately injectable diagnostic stream defaulting to stderr, and route refusals and failure messages to it while retaining result output on stdout.

Guide: cli — design stdout, stderr, and exit codes as separate parts of the command interface.

[F7] D3 | fix | `packages/pqx-excel/src/pqx_excel/writer.py:63`

Problem: `ExcelWriterBase` contains only an identifier and an abstract method, with no concrete behavior to share. The same nominal-inheritance coupling recurs in `SidecarStoreBase` at `packages/pqx-sidecar/src/pqx_sidecar/store.py:45–63`.

Force: Interchangeable collaborators justify interfaces: there are two real Excel writers and test subclasses for both registries. They do not justify behavior-free ABCs. The library specification prescribes these interfaces but supplies no reason that structural conformance is insufficient.

Fix: Use `Protocol` interfaces for writers and sidecar stores, retaining configuration-driven factories. Keep the conversion and hasher ABCs, which do carry shared concrete behavior.

Guide: design — choose Protocols for collaborator contracts and ABCs for shared implementation.

### 2. Questions

1. **Where should selection read a configured-directory sidecar?** `packages/pqx-pipeline/src/pqx_pipeline/run.py:551` passes only the original source path to `sidecar_stale`; `packages/pqx-pipeline/src/pqx_pipeline/staleness.py:145` consequently reads beside that source. Writing instead honors `sidecar_directory_for(config)` at `run.py:611` and `:276–285`. This disagrees with the configured-location behavior described in `docs/decisions/2026-09-15-phases-e-h.md:53–69`. A directory-location write/select acceptance case would settle the intended contract. This is a static code/document disagreement, not a reproduced end-to-end result.

2. **How should descending greedy groups construct chronological coverage?** `packages/pqx-plan/src/pqx_plan/partition.py:225` uses the first and last groups' positional endpoints, although the input can already be descending at lines 303–308. An in-memory probe with ten rows each in January 2024 and January 2025, descending yearly greedy partitioning, and capacity 100 raised `CoverageError`. `docs/partitioning-spec.md:400–401` requires the earlier endpoint first regardless of sheet order. A descending greedy acceptance case and chronological endpoint normalization would settle the discrepancy; this appears to be a local correctness issue rather than a reason to restructure the planner.

3. **Is schema inference part of the public reader contract?** `packages/pqx-excel/src/pqx_excel/fast_reader.py:283` makes the schema optional, and line 345 explicitly selects inference when it is absent. `packages/pqx-excel/tests/test_fast_excel_reader.py:179–186` pins that behavior. `docs/library-spec.md:798` instead says the reader never infers. Decide whether inference is supported for simple inputs or whether supplying the schema is mandatory.

4. **Should the pipeline use a raising metadata core?** `packages/pqx-frame/src/pqx_frame/metadata/extract.py:96–102` logs failures and returns `None`, as explicitly required for the batch API by `docs/library-spec.md:524`. `packages/pqx-pipeline/src/pqx_pipeline/ingest.py:133–135` turns that `None` into a fatal `IngestError`, losing the original cause from the exception chain. The legacy batch behavior is justified; deciding whether the pipeline should call a raising core would settle ownership of this failure and its diagnostic detail.

5. **Should incompatible recorded hashes become a reassembly verdict?** `packages/pqx-verify/src/pqx_verify/fragments.py:142–146` returns `unsupported-hasher` for an unsupported fragment, but `_reassembly` at line 230 subsequently calls `combine`, whose mixed-version refusal escapes as `ValueError` at `packages/pqx-frame/src/pqx_frame/hashing/binary_aggregate.py:169`. An in-memory audit probe with versions 1 and 99 reproduced the exception without opening workbook files. `docs/export-pipeline-spec.md:440–443` describes workbook judgments as verdicts; an explicit policy for internally inconsistent manifest records would settle the boundary.

### 3. Strengths

- **D1/D4 — Functional planning core.** `packages/pqx-plan/src/pqx_plan/partition.py:274–317` plans over counts, a source shape, and settings. It needs no source files, Polars frames, clock, or writer. Reporting likewise converts results to text, for example `packages/pqx-report/src/pqx_report/render.py:70–82`.

- **D1 — State and invariants belong together.** `NameRegistry` owns its mutable namespace and case-insensitive collision rule at `packages/pqx-common/src/pqx_common/names.py:249–300`. Calendar values validate their own precision and dates at `packages/pqx-calendar/src/pqx_calendar/points.py:51–73`.

- **D1 — Existing alternatives are explicit.** Partitioning configuration uses a discriminated union at `packages/pqx-plan/src/pqx_plan/config.py:224`, and date-column interpretation uses the same approach in `packages/pqx-calendar/src/pqx_calendar/columns.py:147`. These alternatives correspond to current behavior.

- **D3 — Shared-behavior inheritance is justified.** `packages/pqx-frame/src/pqx_frame/conversion/base.py:70–77` centralizes conversion, metadata construction, and identity stamping. The hasher base supplies `hash_all` at `packages/pqx-frame/src/pqx_frame/hashing/base.py:38–50`; its test implementations demonstrate actual substitutability.

- **E — Errors gain actionable context.** `packages/pqx-calendar/src/pqx_calendar/decoding.py:189–192` enriches a decoding failure with required column and row information and preserves the original cause.

- **A/N — Named results use domain vocabulary.** `PlannedSheet`, `AllocatedWorkbook`, and `NamedWorkbook` describe distinct stages rather than anonymous positional records; see `packages/pqx-plan/src/pqx_plan/naming.py:55`. The observed vocabulary remains consistent across planning, naming, manifests, and verification.

- **T — Tests target consequential behavior.** `packages/pqx-plan/tests/test_partition.py:147–152` exercises the public pure planner with small inputs. `packages/pqx-frame/tests/test_conversion_row_order.py:106` checks the conversion/arrangement digest invariant; `packages/pqx-frame/tests/test_row_partition_additivity.py:266–286` tests overlap, omission, reordered columns, and leaked temporary columns.

- **L — Library logging has a clear configuration seam.** Metadata extraction obtains a module-named logger at `packages/pqx-frame/src/pqx_frame/metadata/extract.py:24`. Process-wide handler and renderer setup is isolated in `packages/pqx-common/src/pqx_common/logging.py:12–59`; entry-point use was not established as consistently as the pure-core boundaries above.

The recorded choices to remain eager, share the run/preview body, keep the configuration corpus in the package, and distinguish refusal exit code 3 were not treated as defects. Their decision records explain present constraints; module size and supplied complexity measurements alone did not generate findings.

Audit checks and limits:

- Read-only source/context inspection used `Get-Content`, `Get-ChildItem`, and `rg`.
- `git diff --check` and `git diff --exit-code` passed before this report was created. The existing untracked `.VSCodeCounter/` and `e2e/` directories were left alone.
- `.venv/Scripts/python.exe -B -c ...` probes confirmed the expected-ingest-error classification gap (exit 0), descending greedy coverage failure (exit 1), and mixed-hasher reassembly refusal (exit 1). These were targeted probes, not a package test run.
- Lint, pyright, mypy, and the package test suites were not run. No claim about their current pass/fail status is made. Production code was not modified; this report is the sole intended addition.

### 4. Tally

Counts include findings only, not questions. “Met well?” describes the observed design and does not imply that every clause passed an automated check.

| Criterion | block | fix | nit | met well? |
|---|---:|---:|---:|---|
| D1 | 0 | 1 | 0 | Mostly; strong value objects and pure decisions |
| D2 | 0 | 0 | 0 | Mostly; package responsibilities are recognizable |
| D3 | 0 | 1 | 0 | Mixed; justified behavioral ABCs, unnecessary interface ABCs |
| D4 | 0 | 0 | 0 | Yes in the planning and reporting cores |
| E | 0 | 1 | 0 | Mixed; good local context, inconsistent application boundary |
| A | 0 | 0 | 0 | Mostly; named domain results are strong |
| N | 0 | 0 | 0 | Yes; domain vocabulary is consistent |
| L | 0 | 0 | 0 | Partly; module logging and isolated setup are present |
| C | 0 | 2 | 0 | Partly; workflows and diagnostic routing need separation |
| T | 0 | 2 | 0 | Mostly; strong invariant tests with two missing seams |

### 5. Verdict

The most important design weakness is the speculative partial-export switch, which makes the project's whole-source reassembly guarantee optional without a current use case. The strongest design is the functional planning core, expressed through validated values and tested through public behavior. The weakness grows as more verification paths must distinguish partial exports, while the current pure core provides a sound basis for growth.
