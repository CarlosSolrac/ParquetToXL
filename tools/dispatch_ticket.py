"""Dispatch one ticket to QWen, single-shot, enforcing the invariants in docs/tickets/SCHEMA.md.

QWen gets exactly one completion. It never calls a tool, never reads pytest output, and never
continues a conversation: Claude applies the returned patch and runs the gates, and a gate
failure comes back as a new ticket rather than a follow-up turn. This script is the whole
QWen side of that loop.

It enforces three invariants mechanically, because a rule that depends on Claude remembering
it across sessions is not a rule:

1. Token budget. Refuse to dispatch a prompt estimated above ``qwen.max_prompt_tokens``, and
   fail after the fact when the server's ``prompt_eval_count`` came in above it. This is what
   protects a frozen test from being silently truncated out of the window.
2. Orchestrator model and effort. Compare the ticket's ``claude:`` block against what is
   actually in force and halt on a mismatch. See the limitation below.
3. Measurement. Append ``prompt_eval_count``, ``eval_count``, ``seed`` and
   ``last_served_model`` to the attempt log on every dispatch, whether or not it succeeded.

What this script cannot verify
------------------------------
``get_session`` is an MCP tool available to Claude, not to a subprocess, so this script can
never call it. It takes what is in force as ``--claude-model`` and ``--claude-effort``, and
takes ``--verified-via`` to record how the caller came by them: ``get_session`` when Claude
read them from the session, ``operator-asserted`` when a human set the level by hand and said
so. The log records that provenance and a ``verified`` flag, so a later failure can be read
correctly instead of being silently attributed to a session that may never have been running
at the required level. The script halts on a mismatch either way; putting the question to the
operator is Claude's job, not the script's.

Facts verified against the serving host rather than assumed
-----------------------------------------------------------
Ollama 0.33.3 serving ``qwen3.6:latest``:

- ``/api/show`` reports ``presence_penalty 1.5`` as the model's own default. SCHEMA.md pins it
  to 0 and this script refuses a ticket that says otherwise.
- ``think`` is sent explicitly on every request. Omitting the field leaves thinking ON, at
  roughly nine times the tokens for a shorter answer.
- ``/api/tokenize`` does not exist (HTTP 404), so the pre-flight guard estimates from
  characters and the response's ``prompt_eval_count`` is the authoritative check.
- The server accepts and silently discards unknown keys inside ``options``. A misspelled
  ``presence_penalty`` would be dropped with no error anywhere and the model would run at its
  1.5 default, so option names are checked here, client-side.

Usage:
    uv run python tools/dispatch_ticket.py .tickets/T-014.md --claude-model claude-opus-5 --claude-effort max --verified-via operator-asserted

Exit status is 0 on a clean dispatch, and a distinct non-zero code per failure mode.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

DEFAULT_HOST: Final[str] = "http://localhost:11434"
DEFAULT_LOG: Final[Path] = Path(".tickets/attempts.jsonl")
DISPATCH_TIMEOUT: Final[float] = 900.0
FRONT_MATTER_FENCE: Final[str] = "---"

# Chars per token for the pre-flight estimate, which is all there is: Ollama 0.33.3 serves no
# tokenizer endpoint. SCHEMA.md's calibration gives 4.5 from the ticket body alone, but a
# measured dispatch of a 1,160-character body evaluated 315 prompt tokens -- a real ratio of
# 3.68 once the chat template wrapping the message is counted. 3.5 sits deliberately below
# that, because the two ways this guard can be wrong are not symmetric: refusing a ticket
# that would have fitted costs a re-check, while passing one that does not fit risks the
# window truncating a frozen test, which is the whole failure the guard exists to prevent.
CHARS_PER_TOKEN: Final[float] = 3.5

# Verified by sending a deliberate typo to /api/chat: the server returned 200 and ignored the
# key. An unknown option name is therefore silent, which would leave presence_penalty at the
# model's hostile 1.5 default with nothing in any log to show for it.
OLLAMA_OPTION_KEYS: Final[frozenset[str]] = frozenset({"num_ctx", "temperature", "seed", "presence_penalty"})

EXIT_OK: Final[int] = 0
EXIT_TICKET_INVALID: Final[int] = 2
EXIT_NOT_FOR_QWEN: Final[int] = 3
EXIT_CLAUDE_MISMATCH: Final[int] = 4
EXIT_PROMPT_TOO_LARGE: Final[int] = 5
EXIT_DISPATCH_FAILED: Final[int] = 6
EXIT_OVER_BUDGET: Final[int] = 7
EXIT_TRUNCATED: Final[int] = 8

_INTEGER: Final[re.Pattern[str]] = re.compile(r"^-?\d+$")
_DECIMAL: Final[re.Pattern[str]] = re.compile(r"^-?\d+\.\d+$")
_BOOLEANS: Final[Mapping[str, bool]] = {"true": True, "false": False}

# YAML 1.1 reads every one of these as a boolean, and the issue form's dropdowns emit exactly
# this vocabulary: "yes"/"no" for qwen_applies, "on"/"off" for qwen_think. Refusing them
# outright is why this reader exists instead of a general YAML parser.
_BOOLEAN_LOOKALIKES: Final[frozenset[str]] = frozenset({"yes", "no", "on", "off", "y", "n"})

_QUOTES: Final[frozenset[str]] = frozenset({"'", '"'})


class TicketError(Exception):
    """A ticket is missing, malformed, or violates a rule SCHEMA.md states."""


class DispatchError(Exception):
    """The single-shot request to Ollama could not be completed."""


@dataclass(frozen=True)
class ClaudeSettings:
    """The orchestrator configuration a ticket requires in order to be authored."""

    model: str
    effort: str
    on_mismatch: str
    verify_via: str
    record: str


@dataclass(frozen=True)
class QwenSettings:
    """The inference configuration the dispatch applies programmatically."""

    model: str
    think: bool
    num_ctx: int
    temperature: float
    presence_penalty: float
    seed: int
    keep_alive: int
    max_prompt_tokens: int


@dataclass(frozen=True)
class Ticket:
    """A ticket snapshot: its front matter, and the body that is the entire QWen prompt.

    ``qwen`` is ``None`` exactly when the ticket declares ``qwen.applies: false``, in which
    case ``skip_reason`` carries the reason SCHEMA.md makes mandatory in that case.
    """

    identifier: str
    title: str
    phase: int
    path: Path
    body: str
    claude: ClaudeSettings
    qwen: QwenSettings | None
    skip_reason: str | None


@dataclass(frozen=True)
class ClaudeCheck:
    """The comparison of a ticket's required orchestrator settings against those in force."""

    required_model: str
    required_effort: str
    asserted_model: str
    asserted_effort: str
    verified_via: str
    verified: bool
    matches: bool


def _strip_comment(raw: str) -> str:
    """Return the scalar with any trailing comment removed, leaving quoted scalars untouched."""
    if raw.lstrip()[:1] in _QUOTES:
        return raw
    position: int = raw.find(" #")
    return raw if position < 0 else raw[:position]


def _parse_scalar(raw: str, key: str, source: Path) -> object:
    """Convert one front-matter scalar to a Python value.

    Args:
        raw: The text to the right of the colon, comments included.
        key: The field name, used only to make an error message navigable.
        source: The ticket file, used only for error messages.

    Returns:
        A ``bool``, ``int``, ``float``, ``str``, or a ``list`` for inline ``[a, b]`` syntax.

    Raises:
        TicketError: The value is one of YAML 1.1's boolean look-alikes. These are refused
            rather than coerced, because ``off`` silently becoming ``False`` is precisely the
            failure this reader exists to prevent.
    """
    value: str = _strip_comment(raw).strip()
    if value.startswith("[") and value.endswith("]"):
        inner: str = value[1:-1].strip()
        return [_parse_scalar(item, key, source) for item in inner.split(",")] if inner else []
    if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
        return value[1:-1]
    if value in _BOOLEANS:
        return _BOOLEANS[value]
    lowered: str = value.lower()
    if lowered in _BOOLEAN_LOOKALIKES or lowered in _BOOLEANS:
        raise TicketError(
            f"{source}: {key} is {value!r}. Write booleans as exactly 'true' or 'false'; YAML 1.1 would read yes/no/on/off as booleans too, and this reader refuses them so a dropdown label can never be mistaken for one."
        )
    if _INTEGER.match(value):
        return int(value)
    if _DECIMAL.match(value):
        return float(value)
    return value


def _split_front_matter(text: str, source: Path) -> tuple[list[str], str]:
    """Separate the fenced front matter from the ticket body.

    Args:
        text: Full contents of the ticket snapshot.
        source: The ticket file, used only for error messages.

    Returns:
        The front-matter lines, and the body with surrounding blank lines removed.

    Raises:
        TicketError: The file does not open with a fence, or never closes one.
    """
    lines: list[str] = text.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_FENCE:
        raise TicketError(f"{source}: does not begin with a {FRONT_MATTER_FENCE!r} front-matter fence")
    index: int
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONT_MATTER_FENCE:
            return lines[1:index], "\n".join(lines[index + 1 :]).strip()
    raise TicketError(f"{source}: front-matter fence is never closed")


def _parse_block(lines: list[str], source: Path) -> Mapping[str, object]:
    """Parse front-matter lines into a mapping nested at most one level deep.

    Supports exactly what SCHEMA.md's front matter uses -- top-level scalars, inline lists,
    two-space-indented nested mappings, and folded block scalars -- and rejects anything else
    instead of guessing. A ticket is the contract an attempt is judged against, so a field
    read wrongly is worse than a ticket refused.

    Args:
        lines: The lines between the fences.
        source: The ticket file, used only for error messages.

    Returns:
        The parsed mapping.

    Raises:
        TicketError: A line is not ``key: value``, is indented to an unsupported depth, or is
            nested with no parent mapping above it.
    """
    result: dict[str, object] = {}
    section: dict[str, object] | None = None
    index: int = 0
    while index < len(lines):
        raw: str = lines[index]
        index += 1
        stripped: str = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent: int = len(raw) - len(raw.lstrip())
        if indent not in {0, 2}:
            raise TicketError(f"{source}: {stripped!r} is indented {indent} spaces; front matter nests exactly one level, at two spaces")
        if ":" not in stripped:
            raise TicketError(f"{source}: {stripped!r} is not a 'key: value' line")
        parts: list[str] = stripped.split(":", 1)
        key: str = parts[0].strip()
        remainder: str = parts[1].strip()
        if indent == 0:
            if remainder:
                section = None
                result[key] = _parse_scalar(remainder, key, source)
                continue
            section = {}
            result[key] = section
            continue
        if section is None:
            raise TicketError(f"{source}: {key!r} is indented but no mapping opened above it")
        if remainder == ">":
            folded: list[str] = []
            while index < len(lines) and (not lines[index].strip() or len(lines[index]) - len(lines[index].lstrip()) >= 4):
                folded.append(lines[index].strip())
                index += 1
            section[key] = " ".join(part for part in folded if part)
            continue
        section[key] = _parse_scalar(remainder, key, source)
    return result


def _require(section: Mapping[str, object], key: str, context: str, source: Path) -> object:
    """Return a required field, or raise naming the field that is absent."""
    if key not in section:
        raise TicketError(f"{source}: {context}.{key} is missing, and SCHEMA.md requires it")
    return section[key]


def _require_str(section: Mapping[str, object], key: str, context: str, source: Path) -> str:
    """Return a required string field."""
    value: object = _require(section, key, context, source)
    if not isinstance(value, str):
        raise TicketError(f"{source}: {context}.{key} is a {type(value).__name__}, expected a string")
    return value


def _require_bool(section: Mapping[str, object], key: str, context: str, source: Path) -> bool:
    """Return a required boolean field."""
    value: object = _require(section, key, context, source)
    if not isinstance(value, bool):
        raise TicketError(f"{source}: {context}.{key} is a {type(value).__name__}, expected exactly 'true' or 'false'")
    return value


def _require_int(section: Mapping[str, object], key: str, context: str, source: Path) -> int:
    """Return a required integer field, refusing a bool where an integer is meant."""
    value: object = _require(section, key, context, source)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TicketError(f"{source}: {context}.{key} is a {type(value).__name__}, expected an integer")
    return value


def _require_number(section: Mapping[str, object], key: str, context: str, source: Path) -> float:
    """Return a required numeric field, accepting an integer literal where a float is meant."""
    value: object = _require(section, key, context, source)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TicketError(f"{source}: {context}.{key} is a {type(value).__name__}, expected a number")
    return float(value)


def _require_section(front_matter: Mapping[str, object], key: str, source: Path) -> Mapping[str, object]:
    """Return a required nested mapping from the front matter."""
    value: object = _require(front_matter, key, "front matter", source)
    if not isinstance(value, dict):
        raise TicketError(f"{source}: {key!r} must open a nested mapping")
    return value


def load_ticket(path: Path) -> Ticket:
    """Read a ticket snapshot and return it parsed and validated.

    Args:
        path: The snapshot written at dispatch time, for example ``.tickets/T-014.md``.

    Returns:
        The parsed ticket.

    Raises:
        TicketError: The file is absent, the front matter is malformed, a required field is
            missing or of the wrong type, the body is empty, or ``presence_penalty`` is not
            pinned to 0.
    """
    if not path.is_file():
        raise TicketError(f"{path}: no such ticket file")
    lines: list[str]
    body: str
    lines, body = _split_front_matter(path.read_text(encoding="utf-8"), path)
    if not body:
        raise TicketError(f"{path}: the body below the front matter is empty, and the body is the entire QWen prompt")
    front: Mapping[str, object] = _parse_block(lines, path)
    claude_block: Mapping[str, object] = _require_section(front, "claude", path)
    qwen_block: Mapping[str, object] = _require_section(front, "qwen", path)
    identifier: str = _require_str(front, "ticket", "front matter", path)
    title: str = _require_str(front, "title", "front matter", path)
    phase: int = _require_int(front, "phase", "front matter", path)
    claude: ClaudeSettings = ClaudeSettings(
        model=_require_str(claude_block, "model", "claude", path),
        effort=_require_str(claude_block, "effort", "claude", path),
        on_mismatch=_require_str(claude_block, "on_mismatch", "claude", path),
        verify_via=_require_str(claude_block, "verify_via", "claude", path),
        record=_require_str(claude_block, "record", "claude", path),
    )
    # SCHEMA.md makes a missing qwen.applies a hard error: an absent block is ambiguous,
    # forgotten or deliberate, and you cannot tell which from the file.
    if not _require_bool(qwen_block, "applies", "qwen", path):
        return Ticket(identifier=identifier, title=title, phase=phase, path=path, body=body, claude=claude, qwen=None, skip_reason=_require_str(qwen_block, "reason", "qwen", path))
    presence_penalty: float = _require_number(qwen_block, "presence_penalty", "qwen", path)
    if presence_penalty != 0:
        raise TicketError(
            f"{path}: qwen.presence_penalty is {presence_penalty}, and SCHEMA.md pins it to 0. The model ships with 1.5, which penalizes a token for having appeared before; that is actively wrong for code, which repeats names and annotations by design."
        )
    qwen: QwenSettings = QwenSettings(
        model=_require_str(qwen_block, "model", "qwen", path),
        think=_require_bool(qwen_block, "think", "qwen", path),
        num_ctx=_require_int(qwen_block, "num_ctx", "qwen", path),
        temperature=_require_number(qwen_block, "temperature", "qwen", path),
        presence_penalty=presence_penalty,
        seed=_require_int(qwen_block, "seed", "qwen", path),
        keep_alive=_require_int(qwen_block, "keep_alive", "qwen", path),
        max_prompt_tokens=_require_int(qwen_block, "max_prompt_tokens", "qwen", path),
    )
    return Ticket(identifier=identifier, title=title, phase=phase, path=path, body=body, claude=claude, qwen=qwen, skip_reason=None)


def estimate_prompt_tokens(prompt: str) -> int:
    """Estimate a prompt's token count from its length in characters.

    Ollama 0.33.3 exposes no tokenizer -- ``/api/tokenize`` returns 404 against the serving
    host -- so the pre-flight guard has to estimate. The ratio is SCHEMA.md's measured one.
    The response's ``prompt_eval_count`` is authoritative and is checked separately; this
    exists only to refuse an oversized ticket before a completion has been spent on it.
    """
    return math.ceil(len(prompt) / CHARS_PER_TOKEN)


def check_claude_settings(ticket: Ticket, asserted_model: str, asserted_effort: str, verified_via: str) -> ClaudeCheck:
    """Compare the orchestrator settings a ticket requires against those actually in force.

    Only ``get_session`` counts as verified. ``operator-asserted`` records that a human set
    the level by hand, which is the degraded mode WORKFLOW.md describes: the loop is open
    rather than closed, and the log has to say so or a later failure will be misread.

    Args:
        ticket: The ticket carrying the required settings.
        asserted_model: The model the caller reports is in force.
        asserted_effort: The effort level the caller reports is in force.
        verified_via: Either ``get_session`` or ``operator-asserted``.

    Returns:
        The comparison, for the caller to act on and for the attempt log to record.
    """
    return ClaudeCheck(
        required_model=ticket.claude.model,
        required_effort=ticket.claude.effort,
        asserted_model=asserted_model,
        asserted_effort=asserted_effort,
        verified_via=verified_via,
        verified=verified_via == "get_session",
        matches=asserted_model == ticket.claude.model and asserted_effort == ticket.claude.effort,
    )


def _post(url: str, payload: Mapping[str, object], timeout: float) -> dict[str, Any]:
    """Send one JSON POST and decode the response.

    Args:
        url: Absolute endpoint URL.
        payload: Body to send as JSON.
        timeout: Seconds to wait before giving up.

    Returns:
        The decoded response object.

    Raises:
        DispatchError: The request failed, timed out, or returned undecodable content.
    """
    data: bytes = json.dumps(payload).encode()
    # S310: the scheme comes from --host, which defaults to localhost and is operator-supplied.
    request: urllib.request.Request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})  # noqa: S310
    response: Any
    raw: str = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        raise DispatchError(f"{url}: HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DispatchError(f"{url}: {type(exc).__name__}: {exc}") from exc
    try:
        decoded: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispatchError(f"{url}: response was not JSON: {raw[:300]!r}") from exc
    return decoded


def dispatch(host: str, qwen: QwenSettings, prompt: str) -> dict[str, Any]:
    """Send exactly one completion request and return the decoded response.

    Args:
        host: Ollama base URL, without a trailing slash.
        qwen: The inference settings the ticket declared.
        prompt: The ticket body, which is the whole prompt.

    Returns:
        The decoded ``/api/chat`` response.

    Raises:
        DispatchError: An option name is not one the server recognizes, or the request failed.
    """
    options: dict[str, object] = {
        "num_ctx": qwen.num_ctx,
        "temperature": qwen.temperature,
        "seed": qwen.seed,
        "presence_penalty": qwen.presence_penalty,
    }
    unknown: frozenset[str] = frozenset(options) - OLLAMA_OPTION_KEYS
    if unknown:
        raise DispatchError(f"unrecognized Ollama option name(s) {sorted(unknown)}. The server accepts and silently discards these, so a typo here would leave the model at its own defaults with nothing to show for it.")
    payload: dict[str, object] = {
        "model": qwen.model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        # Explicit on every request. Omitting the field leaves thinking ON, measured at
        # roughly nine times the tokens for a shorter answer.
        "think": qwen.think,
        "keep_alive": qwen.keep_alive,
        "options": options,
    }
    return _post(f"{host}/api/chat", payload, DISPATCH_TIMEOUT)


def append_attempt_log(log_path: Path, record: Mapping[str, object]) -> None:
    """Append one attempt record to the JSONL log, creating the file and its parent if needed."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle: io.TextIOWrapper
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="Dispatch one ticket to QWen, single-shot.", formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticket", type=Path, help="Ticket snapshot to dispatch, e.g. .tickets/T-014.md")
    parser.add_argument("--claude-model", required=True, help="Orchestrator model actually in force")
    parser.add_argument("--claude-effort", required=True, help="Orchestrator effort level actually in force")
    parser.add_argument(
        "--verified-via", required=True, choices=["get_session", "operator-asserted"], help="How the two values above were obtained. Only get_session counts as verified; the choice is recorded per attempt."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Ollama base URL (default: {DEFAULT_HOST})")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help=f"Attempt log in JSONL (default: {DEFAULT_LOG})")
    parser.add_argument("--attempt", type=int, default=1, help="Attempt number, for the two-strike rule (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="Run every guard and report the budget without spending a completion")
    return parser


def _build_record(ticket: Ticket, qwen: QwenSettings, check: ClaudeCheck, response: Mapping[str, Any], attempt: int, estimated: int, completion: str, output_path: Path) -> dict[str, object]:
    """Assemble the attempt-log record.

    Args:
        ticket: The dispatched ticket.
        qwen: The inference settings that were sent.
        check: The orchestrator model and effort comparison.
        response: The decoded Ollama response.
        attempt: Attempt number, for the two-strike rule.
        estimated: The pre-flight token estimate, kept so the estimator can be calibrated
            against ``prompt_eval_count`` as attempts accumulate.
        completion: The returned text.
        output_path: Where the completion was written.

    Returns:
        One JSON-serializable record.
    """
    served_model: str = str(response.get("model") or "")
    return {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "ticket": ticket.identifier,
        "title": ticket.title,
        "phase": ticket.phase,
        "attempt": attempt,
        "claude": {
            "required_model": check.required_model,
            "required_effort": check.required_effort,
            "asserted_model": check.asserted_model,
            "asserted_effort": check.asserted_effort,
            "verify_via_declared": ticket.claude.verify_via,
            "verified_via": check.verified_via,
            "verified": check.verified,
        },
        "qwen": {
            "configured_model": qwen.model,
            "last_served_model": served_model,
            "model_fallback": served_model != qwen.model,
            "seed": qwen.seed,
            "think": qwen.think,
            "presence_penalty": qwen.presence_penalty,
            "temperature": qwen.temperature,
            "num_ctx": qwen.num_ctx,
        },
        "tokens": {
            "max_prompt_tokens": qwen.max_prompt_tokens,
            "estimated_prompt_tokens": estimated,
            "prompt_eval_count": response.get("prompt_eval_count"),
            "prompt_eval_cached_count": response.get("prompt_eval_cached_count"),
            "eval_count": response.get("eval_count"),
        },
        "done_reason": response.get("done_reason"),
        "completion_chars": len(completion),
        "output": str(output_path),
    }


def main(argv: list[str] | None = None) -> int:
    """Dispatch one ticket and record the attempt.

    Args:
        argv: Command-line arguments, or ``None`` to read ``sys.argv``.

    Returns:
        Process exit status: 0 on a clean dispatch, and a distinct code per failure mode.
    """
    args: argparse.Namespace = build_parser().parse_args(argv)
    host: str = str(args.host).rstrip("/")
    attempt: int = int(args.attempt)
    try:
        ticket: Ticket = load_ticket(Path(args.ticket))
    except TicketError as exc:
        print(f"TICKET INVALID: {exc}", file=sys.stderr)
        return EXIT_TICKET_INVALID

    if ticket.qwen is None:
        print(f"{ticket.identifier} declares qwen.applies: false, so Claude implements this one directly.", file=sys.stderr)
        print(f"Reason: {ticket.skip_reason}", file=sys.stderr)
        return EXIT_NOT_FOR_QWEN
    qwen: QwenSettings = ticket.qwen

    check: ClaudeCheck = check_claude_settings(ticket, str(args.claude_model), str(args.claude_effort), str(args.verified_via))
    if not check.matches:
        print(f"HALT: {ticket.identifier} requires {check.required_model} at effort {check.required_effort!r}; this session reports {check.asserted_model} at {check.asserted_effort!r}.", file=sys.stderr)
        print(f"on_mismatch = {ticket.claude.on_mismatch}. Claude cannot change its own model or effort, so put this to the operator, have the level set, then re-run.", file=sys.stderr)
        return EXIT_CLAUDE_MISMATCH
    if not check.verified:
        print(
            f"NOTE: model and effort are {check.verified_via}, not verified. The ticket asks for {ticket.claude.verify_via}, which a subprocess cannot call, so the log records what the ticket required and marks this attempt unverified.",
            file=sys.stderr,
        )

    estimated: int = estimate_prompt_tokens(ticket.body)
    if estimated > qwen.max_prompt_tokens:
        print(
            f"REFUSED: the body of {ticket.identifier} is an estimated {estimated} tokens against a max_prompt_tokens of {qwen.max_prompt_tokens}. Split the ticket rather than letting the window truncate a frozen test.",
            file=sys.stderr,
        )
        return EXIT_PROMPT_TOO_LARGE

    if bool(args.dry_run):
        print(
            f"DRY RUN {ticket.identifier}: {estimated} estimated prompt tokens of {qwen.max_prompt_tokens}; model={qwen.model} think={qwen.think} seed={qwen.seed} presence_penalty={qwen.presence_penalty} num_ctx={qwen.num_ctx}"
        )
        return EXIT_OK

    try:
        response: dict[str, Any] = dispatch(host, qwen, ticket.body)
    except DispatchError as exc:
        print(f"DISPATCH FAILED: {exc}", file=sys.stderr)
        return EXIT_DISPATCH_FAILED

    message: dict[str, Any] = response.get("message") or {}
    completion: str = str(message.get("content") or "")
    served_model: str = str(response.get("model") or "")
    prompt_eval_count: object = response.get("prompt_eval_count")
    done_reason: object = response.get("done_reason")
    output_path: Path = ticket.path.with_name(f"{ticket.path.stem}.attempt-{attempt}.out.md")
    output_path.write_text(completion, encoding="utf-8")

    # Logged before any verdict below, because an attempt that breached a guard is exactly
    # the one whose numbers the budget table needs.
    append_attempt_log(Path(args.log), _build_record(ticket, qwen, check, response, attempt, estimated, completion, output_path))

    print(f"{ticket.identifier} attempt {attempt}: prompt_eval_count={prompt_eval_count} eval_count={response.get('eval_count')} seed={qwen.seed} last_served_model={served_model!r} -> {output_path}", file=sys.stderr)
    if served_model != qwen.model:
        print(f"WARNING: the served model {served_model!r} is not the configured {qwen.model!r}. The runtime fell back mid-session; treat anything authored against this attempt accordingly.", file=sys.stderr)
    if isinstance(prompt_eval_count, int) and prompt_eval_count > qwen.max_prompt_tokens:
        print(
            f"OVER BUDGET: the server evaluated {prompt_eval_count} prompt tokens against a max_prompt_tokens of {qwen.max_prompt_tokens}. The attempt is logged, but do not trust the completion: the window may have truncated the frozen test.",
            file=sys.stderr,
        )
        return EXIT_OVER_BUDGET
    if done_reason != "stop":
        print(f"TRUNCATED: the completion ended with done_reason={done_reason!r} rather than 'stop', so it is cut short and is not a whole patch.", file=sys.stderr)
        return EXIT_TRUNCATED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
