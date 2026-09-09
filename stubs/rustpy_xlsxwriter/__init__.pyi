"""Minimal local stub for rustpy-xlsxwriter, which ships a ``.pyi`` but no ``py.typed``.

Without the marker both checkers treat the installed package as untyped and ignore the
``.pyi`` beside it, so the types it already declares never reach a call site here. This
stub re-declares only what this project calls, transcribed from that shipped ``.pyi``
rather than guessed.

``records`` is ``Iterable``, not ``list``, because the writer documents accepting a
generator and this project relies on that: streaming rows is what keeps its constant-memory
mode meaningful, and materialising a list first would defeat it. Verified equal -- a
generator and a list of the same rows produce byte-identical workbooks.

Only the parameters this project supplies are declared. The real function takes about two
dozen more formatting options; declaring them unused would invent a contract nothing checks.
"""

from collections.abc import Iterable
from os import PathLike
from typing import Any

def write_worksheet(
    records: Iterable[dict[str, Any]],
    file_name: str | PathLike[str],
    sheet_name: str | None = None,
    *,
    autofit: bool = True,
    dedupe_strings: bool = False,
) -> None: ...
