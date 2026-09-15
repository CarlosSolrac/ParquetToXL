"""Write the shared validation corpus out as JSON, for consumers outside Python.

The web configuration editor validates the same documents against the same schema in a different
language, so the corpus has to leave this repository as data rather than as a Python module.

Usage:
    uv run python -m tools.build_config_corpus

Writes ``docs/export-config-corpus.json``. A test asserts the committed file matches, so running
this is how you update it after adding a case.
"""

from __future__ import annotations

from pathlib import Path

from pqx_plan.corpus import as_records, render_artifact

OUTPUT: Path = Path(__file__).resolve().parents[1] / "docs" / "export-config-corpus.json"


def main() -> None:
    """Write the artifact and report where it went.

    The rendering itself lives in ``pqx_plan.corpus`` so the test guarding the committed file can
    reach it without importing from here.
    """
    OUTPUT.write_text(render_artifact(), encoding="utf-8")
    print(f"wrote {OUTPUT} ({len(as_records())} cases)")


if __name__ == "__main__":
    main()
