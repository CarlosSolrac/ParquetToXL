# Ticket schema

Every ticket carries a YAML front-matter block naming the two model configurations it
depends on. They are **separate namespaces on purpose**: the `claude:` settings need a
human to change them, the `qwen:` settings are applied programmatically by the dispatch
script. Nothing is shared between the blocks, including key names.

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
  record: last_served_model    # written to the attempt log every dispatch

qwen:
  applies: true
  model: qwen3.6:latest
  think: false
  num_ctx: 32768
  temperature: 0.2
  seed: 20260908
  keep_alive: -1
  max_prompt_tokens: 20000
---
```

## `qwen:` is always present

On a Claude-only ticket the block is still written, with `applies: false` and a mandatory
reason:

```yaml
qwen:
  applies: false
  reason: >
    Requires reading UPath's __new__/__init__ dispatch to thread storage_options through
    both. Exploratory, not single-shot. Claude implements directly.
```

An absent block is ambiguous — forgotten or deliberate, you cannot tell. An explicit
`applies: false` is unambiguous, and the mandatory `reason` forces the judgment to be
written down where it can be reviewed later, when ticket-splitting has improved and a
ticket once thought Claude-only may no longer be.

The dispatch script treats a missing `qwen.applies` key as a hard error.

## Field reference

### `claude:`

| Field | Meaning |
| --- | --- |
| `model` | Exact model ID, no date suffix. Currently `claude-opus-5`. |
| `effort` | `low`–`max`. The level required to **author** this ticket (write the stub and the frozen test), not to apply a patch or read a failure. |
| `on_mismatch` | `halt_and_ask` — detect via `get_session`, raise `AskUserQuestion`, wait, re-verify. Claude cannot change its own model or effort. |
| `verify_via` | How the check is made. `get_session` returns `session_context.model` and `session_context.effort_level`. |
| `record` | `last_served_model` is logged per attempt. It can diverge from `configured_model` when the runtime falls back mid-session, and a frozen test authored under a fallback is worth knowing about. |

`effort` is an authoring requirement, so **batch by phase**: author every frozen test in a
phase in one `max` sitting, then drop to `high` and dispatch and verify the whole phase.
One prompt per phase rather than two per ticket.

### `qwen:`

| Field | Meaning |
| --- | --- |
| `applies` | `true` or `false`. Always present. |
| `reason` | Required when `applies: false`. Why this is not a single-shot ticket. |
| `model` | Ollama tag, exactly as `ollama ps` reports it. |
| `think` | Reasoning on or off. Off for transcription tickets; on only where there is real algorithmic content. |
| `num_ctx` | Hard ceiling. Ollama truncates silently past it — never rely on the server default. |
| `temperature` | Low. Paired with `seed` so a failure is reproducible. |
| `seed` | Recorded per attempt. Without it you cannot distinguish a bad ticket from a bad roll. |
| `keep_alive` | `-1` keeps the model resident. The default 5-minute TTL expires during the gate runs between dispatches, forcing a 23 GB reload. |
| `max_prompt_tokens` | Pre-flight guard. The script refuses to dispatch above this, and hard-fails if the returned `prompt_eval_count` exceeds it. |

## What the dispatch script enforces

Three invariants, mechanically, because a rule that depends on Claude remembering it
across sessions is not a rule:

1. **Token budget** — refuse to dispatch over `max_prompt_tokens`; hard-fail if the
   returned `prompt_eval_count` came in higher than expected. This is what protects the
   frozen test from being silently truncated away.
2. **Claude model and effort** — compare the ticket against `get_session`, halt on
   mismatch. Only enforced during the authoring stage, not when applying a patch QWen
   already produced.
3. **Measurement** — log `prompt_eval_count`, `eval_count`, `seed`, and
   `last_served_model` for every attempt. These accumulate into the real budget table and
   retire the estimates the workflow was designed against.
