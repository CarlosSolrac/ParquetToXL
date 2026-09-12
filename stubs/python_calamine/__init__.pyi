"""Local stub narrowing python-calamine, which ships types pyright strict cannot use.

The package does carry ``py.typed`` and a ``.pyi``, so unlike the other two stubs here this
one is not filling a hole -- it is narrowing one declaration. ``CalamineWorkbook.from_path``
is typed ``path: str | os.PathLike``, and a bare unparameterised ``os.PathLike`` makes the
whole function partially unknown under ``reportUnknownMemberType``, which this project turns
on. Parameterising it is the entire difference.

Only the three members used here are declared, and they are transcribed from that shipped
``.pyi`` rather than guessed. Anything else in the package becomes an error, which is the
intended trade: this file shadows a typed package, so a member it omits must be added
deliberately rather than picked up by accident.

The reader for this project is ``fastexcel``; python-calamine is kept as the independent
cross-check, which is the only thing these members are used for.
"""

from os import PathLike
from typing import Any

class CalamineSheet:
    def to_python(self, skip_empty_area: bool = True, nrows: int | None = None) -> list[list[Any]]: ...

class CalamineWorkbook:
    sheet_names: list[str]

    @classmethod
    def from_path(cls, path: str | PathLike[str], load_tables: bool = False) -> CalamineWorkbook: ...
    def get_sheet_by_index(self, index: int) -> CalamineSheet: ...
