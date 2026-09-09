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
"""

from os import PathLike
from typing import Any

class Workbook:
    def __init__(self, filename: str | PathLike[str], options: dict[str, Any] | None = None) -> None: ...
    def close(self) -> None: ...
