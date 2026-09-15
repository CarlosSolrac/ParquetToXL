"""Phase 0 gate harnesses: measurements that decide a design question, kept with their evidence.

Not library code and not tests. Nothing under ``packages/`` imports this, pytest does not
collect it (``testpaths`` names ``packages`` and ``tests``), and it is outside
``coverage.source`` so it neither raises nor lowers the 100% gate.

It is committed anyway, for the reason ``docs/tickets/WORKFLOW.md`` gives for keeping
measurements: a number in a document that cannot be re-derived is an assertion, not evidence.
The numbers these produced are in ``docs/decisions/2026-09-15-phase-0.md``; re-run a module to
check one on different hardware.

Each is runnable on its own::

    uv run python -m gates.gate_0a_sheet_memory
    uv run python -m gates.gate_0d_sort_memory
"""
