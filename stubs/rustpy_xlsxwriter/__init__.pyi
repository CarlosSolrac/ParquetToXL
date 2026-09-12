"""Minimal local stub for rustpy-xlsxwriter, which ships a ``.pyi`` but no ``py.typed``.

Without the marker both checkers treat the installed package as untyped and ignore the
``.pyi`` beside it, so the types it already declares never reach a call site here. This
stub re-declares only what this project calls, transcribed from that shipped ``.pyi``
rather than guessed.

``records`` is ``Iterable``, not ``list``, because the writer documents accepting a
generator and this project relies on that: streaming rows is what keeps its constant-memory
mode meaningful, and materialising a list first would defeat it. Verified equal -- a
generator and a list of the same rows produce byte-identical workbooks.

It also admits a ``pl.DataFrame``. The package's own ``SheetData`` names a *pandas* frame,
but a Polars one is accepted at runtime and is the only way to write a zero-row frame with
its headers intact -- a generator over no rows carries no column names. Verified: the sheet
XML for an empty frame passed this way is byte-identical to the header row the streaming
path produces.

Only the parameters this project supplies are declared. The real function takes about two
dozen more formatting options; declaring them unused would invent a contract nothing checks.
"""

from collections.abc import Iterable
from os import PathLike
from typing import Any

import polars as pl

def write_worksheet(
    records: Iterable[dict[str, Any]] | pl.DataFrame,
    file_name: str | PathLike[str],
    sheet_name: str | None = None,
    *,
    autofit: bool = True,
    dedupe_strings: bool = False,
) -> None: ...
