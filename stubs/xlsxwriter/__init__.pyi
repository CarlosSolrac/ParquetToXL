"""Minimal local stub for XlsxWriter, which ships neither ``py.typed`` nor typeshed stubs.

Audited in Phase 0: of the seven runtime dependencies, this is the only one without inline
types, and there is no ``types-xlsxwriter`` or ``xlsxwriter-stubs`` on PyPI (checked, 404).

Covers only what this project actually calls, and each member was added against the real
runtime signature rather than guessed, because a speculative stub type-checks silently when
it is wrong. An access to anything not declared here is a hard error, which is intended.

``__init__`` and ``close`` were added for ``tests/fixtures/generate.py``, which does
construct a workbook. The Phase 0 note said this project never would -- polars owns the
xlsxwriter interaction and accepts a path -- and that stopped holding: writing the fixture
workbooks needs the ``remove_timezone`` and ``nan_inf_to_errors`` options, which
``DataFrame.write_excel`` exposes no parameter for. Passing a pre-configured ``Workbook``
is the only route to them.

``filename`` is narrowed to ``str | PathLike[str]`` from the runtime's wider union: the
file-object and ``None`` forms are real but unused here, and a stub that admits arguments
no call site passes is a stub that cannot catch a mistake at those call sites.

``add_worksheet`` and ``Worksheet.write_row`` were added for
``tests/unit/test_fast_excel_reader.py``, which needs multi-sheet workbooks to read back.
Neither registered writer produces one -- both write a single sheet per file by design -- so
the test builds them here instead. ``write_row`` takes ``object`` cells rather than a
narrower union because a test row is deliberately mixed.
"""

from collections.abc import Sequence
from os import PathLike
from typing import Any

class Worksheet:
    def write_row(self, row: int, col: int, data: Sequence[object]) -> int: ...

class Workbook:
    def __init__(self, filename: str | PathLike[str], options: dict[str, Any] | None = None) -> None: ...
    def add_worksheet(self, name: str | None = None) -> Worksheet: ...
    def close(self) -> None: ...
