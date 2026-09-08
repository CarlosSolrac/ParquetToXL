# QWen dispatch workflow

How `requirements-spec.md` gets built: Claude Code orchestrates, a local QWen implements
one function body at a time, Codex reviews the diff. TDD throughout, as the spec mandates.

Ticket front-matter and the measured Ollama calibration are in `SCHEMA.md`. This file is
the plan those tickets execute.

## Roles

**Claude Code (orchestrator).** Owns `requirements-spec.md`, decomposition, stubs, frozen
tests, all integration and wiring, git, and the full gate run
(`pre-commit run --all-files` plus coverage). Anything cross-file is Claude's, never a
QWen ticket.

**QWen** (`qwen3.6:latest` via Ollama, single-shot). One function body per dispatch. Fresh
request every time — never a continued conversation, never a retry inside the same context.

**Codex.** Reviews the diff plus the ticket, never the repository. Two standing questions
beyond ordinary review: does the implementation satisfy the docstring contract or merely
the tests, and were any frozen tests weakened or made trivially true? That is the check
QWen is structurally least able to perform on itself.

## The two mechanisms that make it work

**Interface-first.** Claude writes the stub — full signature, Google docstring stating the
contract, `raise NotImplementedError`. QWen fills in the body only. It never invents a
name, a type, or a module path, so it cannot drift, and Codex reviews against a stable
interface.

**Frozen tests.** Claude writes the failing test. QWen receives it read-only and is told:
if a test looks wrong, stop and report — do not edit it. This is the most important rule in
the workflow. The classic small-model failure is deleting the assertion that will not pass,
and everything downstream assumes the test is the spec.

## The loop

1. Claude writes the stub and the frozen test; commits with the test red. The commit *is*
   the demonstrated failure the spec's TDD mandate calls for.
2. Dispatch to QWen, single-shot. It never calls a tool, never reads pytest output, never
   explores.
3. Claude applies the patch and runs the gates.
4. Gate failures come back as a **new** ticket with the truncated error — never as a
   continued conversation. A fresh window beats a polluted one.
5. Codex reviews the diff.
6. Claude commits.

**Two-strike rule.** If a ticket fails twice, do not attempt a third time. The ticket is
too big: split it into sub-issues of the original and label the parent `needs-split`. A
repository full of `needs-split` issues is the signal that ticket sizing is wrong, and it
is data, not failure.

## Why single-shot

Ollama is inference, not an agent harness — and building a loop on top of it would be a
mistake. Single-shot removes tool schemas and tool output from the window entirely, which
were the two largest and least predictable consumers. Measured cost of a full dispatch is
about 12% of a 32k window (see `SCHEMA.md`), so the design has room rather than running at
the edge.

## Issue tracking

GitHub Issues are the control plane; files are the payload.

Author each ticket through `.github/ISSUE_TEMPLATE/qwen-ticket.yml`. At dispatch, snapshot
the issue body to `.tickets/T-NNN.md` and give QWen **only that file**. An issue body is
mutable and grows a comment thread; a snapshot is byte-exact and tells you later precisely
what a failing attempt was shown.

Labels carry state: `ready`, `dispatched`, `strike-1`, `strike-2`, `needs-split`,
`agent:qwen`, `agent:claude`. Splits become sub-issues of the parent. Codex findings go to
the PR review, not the issue.

## Backlog

Derived from the test-module table at `requirements-spec.md` lines 307–319, which is
already close to a 1:1 ticket list — rows are split where one row carries several
independent assertion groups. Roughly 37 tickets; 22–25 are QWen-sized.

| Phase | Tickets | Owner |
| --- | --- | --- |
| 0 · Prereq | deps + `[build-system]` + src layout + delete `main.py`; **typing/stubs audit**; `configure_logging`; golden style module + `conftest.py` | Claude (logging → QWen) |
| 1 · Canonical + hashing | dtype flag helpers + `ColumnScalar`; `encode_value` null/float; `encode_value` string; `encode_value` temporal; hash Pydantic models + union round-trip; hasher ABC; `hash_column`; `hash_dataframe` | QWen — all 8 |
| 2 · Metadata models | `DataframeColumnMetadata`; `DataframeColumnsMetadata`; `DataframeMetadata`; builder/flags; builder/stats; builder/hash wiring | QWen |
| 3 · Conversions | `ConvertedDataframe` + base + `None`; ToExcel numeric; ToExcel boolean; ToExcel string truncation; ToExcel binary→hex; ToExcel categorical + duration; **ToExcel idempotency + `schema_or_data_changed`** | QWen |
| 4 · Extract | happy path; tz dict + `modified_utc`; failure → `None` + `logger.exception` | QWen |
| 5 · Excel | writer registry; `ExcelWriteConfig`; `PolarsExcelWriter.write`; `fast_excel_reader` | QWen (reader → Claude) |
| 6 · Fixtures | per-dtype column builders (3–4); edge-case row table; `parquet_b` one-cell delta; `ensure_fixtures()` orchestration; Excel file generation | Mixed |
| 7 · Integration | `ZPath`; headline round-trip test; reader parallelization benchmark | Claude |

### Reserved for Claude, with reasons

- **`ZPath`** — the spec itself flags it (lines 109–110): threading `storage_options`
  through UPath's `__new__` *and* `__init__` without breaking its protocol handlers.
  Requires reading UPath internals. Not single-shot.
- **`fast_excel_reader`** — `ThreadPoolExecutor` plus an open empirical question the spec
  leaves unresolved (line 285): whether per-sheet extraction on one workbook handle
  actually parallelizes. Needs benchmarking.
- **`tests/fixtures/generate.py`** — 19 dtypes × 1000 rows × edge-case tables ×
  byte-identical regeneration. The largest single unit in the project.
- **The integration test** — whole-system view by definition.

### Sequencing notes

Do the Pydantic models early. They are declarative, the spec gives exact fields including
the `digest_hex` pattern constraint, and they are the type foundation every later stub
imports. Cheap wins that also calibrate ticket sizing against real behaviour before
anything hard.

Give ToExcel idempotency its own ticket. Spec line 136: `_convert` must be idempotent on
already-converted frames, because the headline test runs both operands through it. Subtle,
load-bearing, and it will not fall out of the per-dtype tickets by accident.

## Phase 0 blockers

Resolve before dispatching anything.

**The typing audit is the big one.** Seven runtime dependencies land under `pyright`
strict with `reportMissingTypeStubs = true` and `mypy --strict`. If any of them —
`python-calamine` is the likeliest — ships no `py.typed`, then *every* QWen ticket that
imports it fails on an error that has nothing to do with the ticket, and QWen will spend
its window flailing at it. Audit all seven, populate `stubs/` where needed, and confirm a
trivial module passes both checkers before the first dispatch.

**Warm pyright once.** The PyPI wrapper downloads a Node runtime on first run; do it
outside the ticket loop.

**Reconcile the coverage gate.** `requirements-spec.md` line 332 says "≥ 90% stmt / 85%
branch on changed code"; `pyproject.toml` says `fail_under = 90` with `branch = true`
globally. Those are different gates, and a global 90 fails on a partially-built repo.
Recommend making it merge-only.

## Commands QWen's dispatch must never run

Not applicable under single-shot dispatch, but they belong in any ticket's "done when"
block, which Claude executes:

- `pre-commit run --all-files --show-diff-on-failure` — dumps the whole repo diff
- `pytest --cov` — a global gate that fails on a partial repo
- any recursive `grep` or `find`

Narrow targets only: `uv run pytest -x -q --no-header --tb=short <one test file>`,
`uv run ruff check <one path>`, `uv run pyright <one path>`.

## Open

- The `think: true` run dropped `/` from an illegal-character set the contract listed
  (`SCHEMA.md`, calibration). One sample. Re-run the probe with two or three different
  seeds before treating "thinking hurts correctness" as established rather than "thinking
  costs 9× and did not help here."
- Whether to raise Ollama's `num_ctx` above 32768. The model supports 262,144; the ceiling
  is a VRAM tradeoff, not a limit. On current evidence tickets use ~12% of 32k, so there
  is no pressure to.
