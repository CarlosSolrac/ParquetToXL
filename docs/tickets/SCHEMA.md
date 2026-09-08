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
  think: false                 # MUST be explicit — omitting it turns thinking ON
  num_ctx: 32768
  temperature: 0.2
  presence_penalty: 0          # model default is 1.5; hostile to code, see below
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
| `think` | Reasoning on or off. **Always send it explicitly** — omitting the field leaves thinking on. Default off for every ticket, including the algorithmic ones; see the calibration below. |
| `presence_penalty` | Pinned to 0. The model ships with 1.5, which penalizes tokens for having appeared before — actively wrong for code, which repeats names and annotations by design. |
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

## Measured calibration

Run `tools/probe_ollama.py` against the serving host; these are the results that replaced
the workflow's original estimates. Ollama 0.33.3, `qwen3.6:latest`, seed 20260908, one run
per condition on a small sample ticket.

| `think` | eval_count | thinking chars | content chars |
| --- | --- | --- | --- |
| omitted | 5,836 | 20,297 | 516 |
| `false` | **669** | 0 | 2,740 |
| `true` | 6,256 | 22,354 | 427 |

Four things follow, and they are why the defaults above look the way they do.

**Omitting `think` does not disable it.** The field must be sent as `false` on every
request. This is the most likely way for the workflow to silently start costing nine times
what it should.

**Thinking is not worth it here.** Nine times the tokens for a shorter answer, and the
`think: true` run built its illegal-character set as `[]:*?\` — dropping the `/` the
contract explicitly listed, which the non-thinking run got right. The original plan
enabled thinking for tickets with real algorithmic content; that is reversed. Default off
everywhere, and turn it on for one ticket only after that ticket has demonstrably failed
without it.

Caveat: one run per condition on a deliberately easy task. The token ratio is solid; the
quality comparison is a signal, not a finding.

**Tickets are far cheaper than estimated.** 313 prompt tokens for ~1,400 characters, about
4.5 chars per token. A ticket at the 400-line context cap is roughly 3,300 tokens; with a
~700-token completion that is about 4,000 of 32,768 — twelve percent. The original 6–8k
prompt estimate was around double the truth, and the 400-line cap is far tighter than the
window requires. `max_prompt_tokens` stays at 20,000 as a guard against a ticket that has
grown without anyone noticing, not because the budget is close.

**32,768 is a configuration choice, not the model's limit.** `/api/show` reports
`qwen35moe.context_length: 262144`. Ollama loaded the model with a 32k window; raising it
costs VRAM for the KV cache. Worth knowing before treating the context ceiling as the
thing that constrains ticket design — on this evidence it is not.
