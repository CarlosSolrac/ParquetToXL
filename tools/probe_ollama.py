"""Probe a local Ollama instance for the facts the QWen ticket workflow depends on.

The ticket workflow is designed against four unknowns that cannot be answered from a
model card or from training priors:

1. Whether this Ollama build accepts the ``think`` request field, ignores it, or rejects it.
2. Whether the served model reports a thinking capability at all.
3. How many prompt tokens a realistic ticket actually costs, measured rather than estimated.
4. How many tokens the model spends reasoning before it emits anything useful.

Run this where Ollama is reachable — the WSL side, not a remote container — and paste the
JSON report back. Every number in the workflow's budget table is a placeholder until this
has run.

Usage:
    uv run python -m tools.probe_ollama --model qwen3.6:latest
    uv run python -m tools.probe_ollama --model qwen3.6:latest --prompt-file .tickets/T-007.md

Exit status is 1 when any probe fails.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

DEFAULT_HOST: Final[str] = "http://localhost:11434"
METADATA_TIMEOUT: Final[float] = 10.0
INFERENCE_TIMEOUT: Final[float] = 600.0

# Stands in for a real ticket when none is supplied: a contract to satisfy, a frozen test
# that must not be edited, and a house rule that constrains the output. Short by design —
# the measurement that matters is the ratio of reasoning to output, not the prompt size.
SAMPLE_TICKET: Final[str] = """\
Implement the body of `sanitize_sheet_name` below. Do not modify tests. Do not create files.

def sanitize_sheet_name(name: str, taken: frozenset[str]) -> str:
    \"\"\"Return an Excel-legal worksheet name derived from ``name``.

    Excel rejects names longer than 31 characters, names containing any of []:*?/\\\\, and
    blank names. The result must not collide with any entry in ``taken``.

    Args:
        name: Proposed worksheet name.
        taken: Names already used in this workbook.

    Returns:
        A legal, unique worksheet name.
    \"\"\"

Frozen test (read-only):

    def test_truncates_to_31_chars() -> None:
        assert len(sanitize_sheet_name("x" * 40, frozenset())) == 31

    def test_strips_illegal_characters() -> None:
        assert sanitize_sheet_name("a[b]c:d", frozenset()) == "abcd"

    def test_blank_name_gets_placeholder() -> None:
        assert sanitize_sheet_name("", frozenset()) == "Sheet1"

    def test_collision_is_suffixed() -> None:
        assert sanitize_sheet_name("Data", frozenset({"Data"})) == "Data_1"

House rule: every local variable is annotated at its first binding.
Reply with the function body only.
"""


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single probe against the Ollama HTTP API."""

    name: str
    ok: bool
    detail: dict[str, Any]

    def render(self) -> str:
        """Return a single-line human-readable summary."""
        return f"[{'ok' if self.ok else 'FAIL'}] {self.name}"


def _request(url: str, payload: dict[str, Any] | None, timeout: float) -> tuple[bool, dict[str, Any]]:
    """Send a JSON request to Ollama and decode the response.

    Args:
        url: Absolute endpoint URL.
        payload: Body to send as JSON, or ``None`` to issue a GET.
        timeout: Seconds to wait before giving up.

    Returns:
        An ``(ok, body)`` pair. When ``ok`` is false, ``body`` carries an ``error`` key
        naming the failure mode rather than raising, so one failed probe does not abort
        the rest of the report.
    """
    data: bytes | None = json.dumps(payload).encode() if payload is not None else None
    # S310: the scheme comes from --host, which defaults to localhost and is operator-supplied, not attacker-supplied.
    request: urllib.request.Request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})  # noqa: S310
    raw: str = ""
    response: Any
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        return False, {"error": "http", "status": exc.code, "body": exc.read().decode(errors="replace")[:500]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, {"error": "connection", "detail": str(exc)}
    try:
        return True, json.loads(raw)
    except json.JSONDecodeError:
        return False, {"error": "decode", "raw": raw[:500]}


def probe_version(host: str) -> ProbeResult:
    """Report the Ollama server version.

    The ``think`` request field is build-dependent; the version is what tells you whether
    to expect it to work.

    Args:
        host: Base URL of the Ollama server.

    Returns:
        The probe outcome.
    """
    ok: bool
    body: dict[str, Any]
    ok, body = _request(f"{host}/api/version", None, METADATA_TIMEOUT)
    return ProbeResult("version", ok, body)


def probe_capabilities(host: str, model: str) -> ProbeResult:
    """Report the model's declared capabilities and context length.

    Args:
        host: Base URL of the Ollama server.
        model: Ollama model tag, exactly as ``ollama ps`` reports it.

    Returns:
        The probe outcome. A ``thinking`` entry in ``capabilities`` settles whether this
        is a hybrid-reasoning model without guessing from the name.
    """
    ok: bool
    body: dict[str, Any]
    ok, body = _request(f"{host}/api/show", {"model": model}, METADATA_TIMEOUT)
    if not ok:
        return ProbeResult("capabilities", False, body)

    info: dict[str, Any] = body.get("model_info", {})
    context_keys: list[str] = [key for key in info if key.endswith("context_length")]
    return ProbeResult(
        "capabilities",
        True,
        {
            "capabilities": body.get("capabilities", []),
            "parameters": body.get("parameters", ""),
            "context_length": {key: info[key] for key in context_keys},
        },
    )


def probe_chat(host: str, model: str, prompt: str, *, think: bool | None, num_ctx: int, seed: int) -> ProbeResult:
    """Send one single-shot completion and measure what it cost.

    Args:
        host: Base URL of the Ollama server.
        model: Ollama model tag.
        prompt: Full user message — a representative ticket.
        think: Value for the ``think`` field, or ``None`` to omit it entirely.
        num_ctx: Context window to request. Ollama truncates silently past its own
            default, so this is always sent explicitly.
        seed: Sampling seed, recorded so a result can be reproduced.

    Returns:
        The probe outcome, carrying the token counts that replace the workflow's
        estimates and a flag for whether the response carried reasoning content.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": -1,
        "options": {"num_ctx": num_ctx, "temperature": 0.2, "seed": seed},
    }
    if think is not None:
        payload["think"] = think

    ok: bool
    body: dict[str, Any]
    ok, body = _request(f"{host}/api/chat", payload, INFERENCE_TIMEOUT)
    label: str = f"chat(think={think})"
    if not ok:
        return ProbeResult(label, False, body)

    message: dict[str, Any] = body.get("message", {})
    thinking: str = message.get("thinking") or ""
    content: str = message.get("content") or ""
    return ProbeResult(
        label,
        True,
        {
            "think_field_accepted": True,
            "returned_thinking": bool(thinking),
            "thinking_chars": len(thinking),
            "content_chars": len(content),
            "prompt_eval_count": body.get("prompt_eval_count"),
            "eval_count": body.get("eval_count"),
            "seed": seed,
            "content_head": content[:300],
        },
    )


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Ollama base URL (default: {DEFAULT_HOST})")
    parser.add_argument("--model", required=True, help="Model tag, e.g. qwen3.6:latest")
    parser.add_argument("--prompt-file", type=Path, default=None, help="Real ticket to calibrate against; defaults to a built-in sample")
    parser.add_argument("--num-ctx", type=int, default=32768, help="Context window to request (default: 32768)")
    parser.add_argument("--seed", type=int, default=20260908, help="Sampling seed, recorded in the report")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run every probe and print a JSON report.

    Args:
        argv: Command-line arguments, or ``None`` to read ``sys.argv``.

    Returns:
        Process exit status: 0 when every probe succeeded, 1 otherwise.
    """
    args: argparse.Namespace = build_parser().parse_args(argv)
    host: str = str(args.host).rstrip("/")
    model: str = str(args.model)
    prompt: str = args.prompt_file.read_text(encoding="utf-8") if args.prompt_file is not None else SAMPLE_TICKET

    results: list[ProbeResult] = [
        probe_version(host),
        probe_capabilities(host, model),
        # Omitted, false, and true in turn: whether the field is accepted is as
        # informative as the token counts, and the pair of runs shows what thinking costs.
        probe_chat(host, model, prompt, think=None, num_ctx=args.num_ctx, seed=args.seed),
        probe_chat(host, model, prompt, think=False, num_ctx=args.num_ctx, seed=args.seed),
        probe_chat(host, model, prompt, think=True, num_ctx=args.num_ctx, seed=args.seed),
    ]

    result: ProbeResult
    for result in results:
        print(result.render(), file=sys.stderr)

    report: dict[str, Any] = {
        "host": host,
        "model": model,
        "num_ctx_requested": args.num_ctx,
        "prompt_source": str(args.prompt_file) if args.prompt_file is not None else "built-in sample",
        "probes": {result.name: {"ok": result.ok, **result.detail} for result in results},
    }
    print(json.dumps(report, indent=2))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
