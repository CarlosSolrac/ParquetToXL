# Implementation workflow

How `requirements-spec.md` gets built: Claude Code implements one unit at a time against a
frozen test, Codex reviews the diff. TDD throughout, as the spec mandates.

Ticket front-matter is in `SCHEMA.md`. This file is the plan those tickets execute.

## Roles

**Claude Code.** Owns `requirements-spec.md`, decomposition, stubs, frozen tests,
implementation, integration, git, and the full gate run (`pre-commit run --all-files` plus
coverage).

**Codex.** Reviews the diff plus the ticket, never the repository. Two standing questions
beyond ordinary review: does the implementation satisfy the docstring contract or merely
the tests, and were any frozen tests weakened or made trivially true? Those are the checks
an author is structurally least able to perform on their own work, which is why this role
stays with a second pair of eyes rather than being folded into the first.

## Why there is no third model

An earlier version of this workflow dispatched each unit to a local `qwen3.6` over Ollama,
single-shot, with Claude authoring the stub and frozen test. It was removed after one
ticket went through end to end and the numbers were measured rather than assumed.

**The contract has to be at least as precise as the code.** For a tool-less model to fill a
body single-shot it needs a docstring that names every value to configure. On T-001 that
was 32 lines of contract for 23 lines of body; across Phase 1 it was roughly 238 lines of
contract for about 40 lines of implementation. Writing a specification that exact *is*
writing the code, in prose, and usually at greater length.

**The dispatch overhead is per-ticket, not amortised.** Each ticket needs a snapshot
inlining the stub, the frozen test and the golden style module; the completion has to be
read back, applied, and gated; a failure needs a fresh ticket carrying the diagnostics.
T-001 took two attempts to go green and still needed a lint fix applied by hand.

**Verifying the frozen test duplicates the work.** Confirming a frozen test is satisfiable
means writing an implementation that passes it. Once that exists, dispatching the same body
to another model produces code that is already in hand.

None of this says the local model was bad — it produced correct, house-style code on the
second attempt. It says the split put the expensive half of the work on the orchestrator
and the cheap half on the implementer. The mechanisms that made it work are worth keeping;
the dispatch is not.

The machinery itself is still in the tree and is now unused: `tools/dispatch_ticket.py`,
`tools/probe_ollama.py`, `docs/tickets/calibration-2026-09-08.json`, and the `.tickets/T-001`
records. It is left in place deliberately rather than deleted — the calibration and the two
attempt logs are the evidence behind the decision above, and deleting the evidence would
make the reasoning unauditable. Nothing imports any of it.

## The two mechanisms worth keeping

**Interface-first.** Write the stub — full signature, Google docstring stating the contract,
`raise NotImplementedError` — before the body. The contract is then a written artifact that
Codex can review against, rather than something reconstructed from the implementation.

**Frozen tests.** Write the failing test first and commit it red; the commit *is* the
demonstrated failure the spec's TDD mandate calls for. Do not edit a test to make an
implementation pass. If a test looks wrong, stop and say why.

**Prove a frozen test is satisfiable, not merely red.** A test that cannot be made green
poisons everything built on it. Before relying on one, confirm an implementation exists that
passes it. This is cheap when the implementation follows immediately.

## The loop

1. Write the stub and the frozen test; commit with the test red.
2. Implement the body.
3. Run the gates on narrow targets.
4. Codex reviews the diff.
5. Commit.

## Verify assumptions against a live interpreter

The most expensive mistakes in this project have all been assumptions frozen into a test
before anyone checked them. Every one of these was found by running code, not by reading:

- `tzdata` was missing, so `zoneinfo` could not resolve any IANA key including `"UTC"`, and
  reading a value out of a `Datetime("us","UTC")` column raised. Linux CI carries a system
  tz database, so this would have been green in CI and broken locally.
- The spec's own module layout could not be imported: the `HashedDataframe` alias in
  `base.py` needed a class from `binary_aggregate.py`, which needs `base.py`.
- `encode_value` was specified for six tags but had to hash source frames containing five
  more dtypes.
- Polars reports `Decimal` as numeric and `Boolean` as not numeric, and offers no `is_text`
  predicate at all.

Check the fact before writing the assertion.

## Issue tracking

GitHub Issues are the control plane; files are the payload. Author each ticket through
`.github/ISSUE_TEMPLATE/`. Labels carry state: `ready`, `in-progress`, `needs-split`.

**Two-strike rule.** If a unit resists implementation twice, do not force a third attempt.
It is too big: split it into sub-issues of the original and label the parent `needs-split`.
A repository full of `needs-split` issues is the signal that sizing is wrong, and it is
data, not failure.

## Backlog

Derived from the test-module table at `requirements-spec.md`, which is already close to a
1:1 ticket list — rows are split where one row carries several independent assertion groups.

| Phase | Tickets | Status |
| --- | --- | --- |
| 0 · Prereq | deps + `[build-system]` + src layout + delete `main.py`; typing/stubs audit; `configure_logging`; golden style module + `conftest.py` | done |
| 1 · Canonical + hashing | dtype flag helpers + `ColumnScalar`; `encode_value`; hash Pydantic models + union round-trip; hasher ABC; `hash_column`; `hash_dataframe` | done |
| 2 · Metadata models | `DataframeColumnMetadata`; `DataframeColumnsMetadata`; `DataframeMetadata`; builder/flags; builder/stats; builder/hash wiring | done |
| 3 · Conversions | `ConvertedDataframe` + base + `None`; ToExcel numeric; boolean; string truncation; binary→hex; categorical + duration; **idempotency + `schema_or_data_changed`** | done |
| 4 · Extract | happy path; tz dict + `modified_utc`; failure → `None` + `logger.exception` | done |
| 5 · Excel | writer registry; `ExcelWriteConfig`; `RustpyExcelWriter` (default) + `PolarsExcelWriter`; `fast_excel_reader` | done |
| 6 · Fixtures | per-dtype column builders; edge-case row table; `parquet_b` one-cell delta; `ensure_fixtures()`; Excel file generation | done |
| 7 · Integration | `ZPath`; headline round-trip test; reader parallelization benchmark | done |
| 8 · Sidecar | remove the unread statistics; `SidecarDocument`; store registry + `JsonSidecarStore`; round-trip integration test | done |

### What the phases actually cost, in hindsight

Worth recording, because the sequencing notes below were written before any of it was built
and two of them turned out to be wrong:

- **`ZPath` is a phase 4 prerequisite, not a phase 7 unit.** `extract` takes one, and so do
  the phase 5 writer and reader. It was built during phase 4. Its open question resolved the
  opposite way from the guess: the problem was not threading `storage_options` through
  `__new__` and `__init__`, it was that the spec's subclass cannot be constructed at all.
- **The expensive part of phase 3 was not the cast table.** The per-dtype rules were
  mechanical, as predicted. What cost time was discovering, by measurement, that the table
  did not model what a workbook actually does -- and the amendments that followed.
- **Excel writers fail soft.** Seven rounds of review on one module, each finding a case
  where a writer warned, discarded data, and returned successfully. That is the single
  biggest source of defects in this project so far, and none of it was predictable from the
  documentation.

### Sequencing notes

Do the Pydantic models early. They are declarative, the spec gives exact fields including
the `digest_hex` pattern constraint, and they are the type foundation every later stub
imports.

Give ToExcel idempotency its own ticket. Spec line 136: `_convert` must be idempotent on
already-converted frames, because the headline test runs both operands through it. Subtle,
load-bearing, and it will not fall out of the per-dtype tickets by accident.

`ZPath`, `fast_excel_reader`, `tests/fixtures/generate.py` and the integration test are the
four largest units. The first two carry open questions — threading `storage_options`
through UPath's `__new__` *and* `__init__`, and whether per-sheet extraction on one workbook
handle actually parallelizes — so budget exploration time for them rather than sizing them
like the rest.

## Gate discipline

Never run these during the loop:

- `pre-commit run --all-files --show-diff-on-failure` — dumps the whole repo diff
- `pytest --cov` as a gate — the threshold is merge-only, applied by CI
- any recursive `grep` or `find`

Narrow targets only:

```
uv run pytest -x -q --no-header --tb=short <one test file>
uv run ruff check <one path>
uv run pyright <one path>
uv run python tools/check_declarations.py <one path>
```

## Claude effort per phase

`claude-opus-5` throughout. Effort is the lever, and the value below is what each phase
requires to be **authored** — writing the stub and the frozen test — not to implement
against one that already exists or to read a gate failure.

| Phase | Effort | Why |
| --- | --- | --- |
| 0 · Prereq | `xhigh` | Infrastructure. Typing audit, layout, lockfile. |
| 1 · Canonical + hashing | `max` | Tag design and the mod-2^128 identity; the frozen tests here are the ones later phases lean on. |
| 2 · Metadata models | `high` | Declarative, exact fields given in the spec. |
| 3 · Conversions | `xhigh`, `max` for idempotency | Per-dtype rules are mechanical; idempotency is load-bearing for the headline test. |
| 4 · Extract | `high` | Straightforward once the models exist. |
| 5 · Excel | `high`; `max` for `fast_excel_reader` | The reader carries an unresolved empirical question. |
| 6 · Fixtures | `xhigh` | Large, cross-cutting, byte-identical regeneration. |
| 7 · Integration | `max` | ZPath internals and the whole-system round-trip. |

**Batch by phase.** Author every frozen test in a phase in one sitting at that phase's
level, then drop to `high` to implement and verify the whole phase.

Claude cannot change its own model or effort, and cannot reliably read them either:
`get_session` is a Claude Code Remote MCP tool that a local terminal session may not have
attached. Check for it; if it is absent, say so plainly rather than implying a verification
that did not happen, and treat the level as operator-asserted. Setting it correctly falls
to the operator before the session starts.
