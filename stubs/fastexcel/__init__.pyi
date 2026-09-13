"""Local stub narrowing fastexcel, which ships types pyright strict cannot fully use.

Like the ``python_calamine`` stub beside it, this is not filling a hole. The package carries
``py.typed`` and its own annotations, and they are correct. The problem is what they refer
to: both ``ExcelSheet.to_arrow`` and the ``eager=True`` overload of ``load_sheet`` are
annotated as returning ``pyarrow.RecordBatch``, and **pyarrow ships no ``py.typed``**, so
that name resolves to an unknown type. One unknown return makes the *whole* member partially
unknown under ``reportUnknownMemberType``, which this project turns on, and selecting the
other overload at the call site does not help -- the diagnostic is about the member.

Declaring only the ``eager=False`` overload is the entire difference for ``load_sheet``. The
eager form is real and is simply not used here.

``ArrowRecordBatch`` is a stand-in for the ``pyarrow.RecordBatch`` that
``to_arrow_with_errors`` returns. It declares only ``__arrow_c_array__``, the Arrow PyCapsule
method, which is all this project uses it for: that one method is what makes it satisfy
polars' ``ArrowArrayExportable`` protocol, so ``pl.DataFrame(batch)`` type-checks without a
pyarrow stub of its own. Naming the real class would drag the untyped package back in.

Only what this project calls is declared, transcribed from the shipped source rather than
guessed. Anything omitted becomes a hard error, which is the intended trade for shadowing a
typed package: a member this project starts using must be added deliberately.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import polars as pl

type DType = Literal["null", "int", "float", "string", "boolean", "datetime", "date", "duration"]
type DTypeMap = dict[str | int, DType]

class ColumnInfoNoDtype: ...

class ArrowRecordBatch:
    def __arrow_c_array__(self, requested_schema: object | None = None) -> tuple[object, object]: ...

class CellError:
    @property
    def position(self) -> tuple[int, int]: ...
    @property
    def row_offset(self) -> int: ...
    @property
    def offset_position(self) -> tuple[int, int]: ...
    @property
    def detail(self) -> str: ...

class CellErrors:
    @property
    def errors(self) -> list[CellError]: ...

class ExcelSheet:
    def to_polars(self) -> pl.DataFrame: ...
    def to_arrow_with_errors(self) -> tuple[ArrowRecordBatch, CellErrors | None]: ...

class ExcelReader:
    @property
    def sheet_names(self) -> list[str]: ...
    def load_sheet(
        self,
        idx_or_name: int | str,
        *,
        header_row: int | None = 0,
        column_names: list[str] | None = None,
        skip_rows: int | list[int] | Callable[[int], bool] | None = None,
        n_rows: int | None = None,
        schema_sample_rows: int | None = 1_000,
        dtype_coercion: Literal["coerce", "strict"] = "coerce",
        use_columns: list[str] | list[int] | str | Callable[[ColumnInfoNoDtype], bool] | None = None,
        dtypes: DType | DTypeMap | None = None,
        eager: Literal[False] = ...,
        skip_whitespace_tail_rows: bool = False,
        whitespace_as_null: bool = False,
    ) -> ExcelSheet: ...

def read_excel(source: Path | str | bytes) -> ExcelReader: ...
