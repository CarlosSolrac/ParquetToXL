# Ticket schema

Every ticket carries a YAML front-matter block naming the orchestrator configuration it
depends on.

```yaml
---
ticket: T-014
title: ToExcel conversion — boolean mapping
phase: 3
depends_on: [T-011]

claude:
  model: claude-opus-5
  effort: max                  # level required to AUTHOR this ticket
  on_mismatch: halt_and_ask    # AskUserQuestion, then re-verify before proceeding
  verify_via: get_session      # session_context.model + session_context.effort_level
---
```

## Field reference

| Field | Meaning |
| --- | --- |
| `ticket` | Identifier, `T-NNN`. |
| `title` | One line. The unit of work, not the phase. |
| `phase` | 0–7, matching the backlog table in `WORKFLOW.md`. |
| `depends_on` | Ticket IDs that must land first. Empty list if independent. |
| `claude.model` | Exact model ID, no date suffix. Currently `claude-opus-5`. |
| `claude.effort` | `low`–`max`. The level required to **author** this ticket — write the stub and the frozen test — not to implement against one that already exists, or to read a gate failure. |
| `claude.on_mismatch` | `halt_and_ask` — raise `AskUserQuestion`, wait, re-verify. Claude cannot change its own model or effort. |
| `claude.verify_via` | How the check is made. `get_session` returns `session_context.model` and `session_context.effort_level`. |

`effort` is an authoring requirement, so **batch by phase**: author every frozen test in a
phase in one sitting at that level, then drop to `high` and implement and verify the whole
phase. One level change per phase rather than two per ticket.

## `get_session` availability is not guaranteed

It is a Claude Code Remote MCP tool, confirmed working in a cloud session; whether a local
terminal session has that server attached is unverified. Check for it before relying on it.

Without it the loop is open rather than closed: setting the level correctly falls to the
operator before the session starts, and the ticket can only record what it required, not
confirm what was in effect. Say which mode applied, plainly. A session that reports a
verification it did not perform is worse than one that admits the gap, because a later
failure then gets attributed to the wrong cause.

## Definition of ready

- Stub committed with full signature, docstring contract, and `raise NotImplementedError`.
- Frozen test committed and demonstrated red.
- The frozen test demonstrated **satisfiable** — an implementation exists that passes it.
  A test that cannot be made green poisons everything built on top of it.
- The contract is pasted text in the ticket, not a pointer to `requirements-spec.md`.

## Definition of done

- The narrow gates pass: `pytest` on the one test file, `ruff check`, `pyright`, `mypy`,
  and `tools/check_declarations.py` on the changed paths.
- No new suppression. `# noqa`, `# type: ignore`, `# pragma: no cover` and friends need
  explicit authorization; fix the cause first. Three were avoided in Phase 1 by doing so —
  `inspect.isabstract` instead of `type: ignore[abstract]`, an explicit permutation instead
  of tripping `S311`, and deriving a naive datetime from an aware one instead of a `DTZ001`
  waiver.
- Codex has reviewed the diff.

## An earlier version dispatched to a local model

This schema previously carried a second `qwen:` block configuring a local `qwen3.6` served
by Ollama, which implemented each body single-shot. That was removed once measured; see
"Why there is no third model" in `WORKFLOW.md` for the numbers. The removed machinery,
including the measured Ollama calibration, is in git history.
