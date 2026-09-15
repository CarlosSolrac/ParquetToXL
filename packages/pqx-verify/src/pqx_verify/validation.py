"""Deciding whether a workbook holds the data a sidecar describes.

The sidecar carries everything the decision needs -- the reader's schema, the row extent, the
expected digest, and the identity of the rules that produced it -- but a caller assembling
those by hand has to remember the parts that are easy to skip. Two of the checks here are of
that kind: a sidecar written by a different conversion or hasher version records digests
computed under different rules, and comparing them reports a difference in the *data* that is
really a difference in the *algorithm*. This function refuses rather than compares.

The source frame is never opened. ``source_path`` is used to find the sidecar beside it and
for nothing else, so validation works wherever the Parquet file itself has gone.

Scope
-----
The question answered is **round-trip stability of the converted frame**: data written to
Excel comes back unchanged. Two things are deliberately outside it.

This is not an integrity check against tampering. There is no attacker in the threat model,
matching what ``hashing.binary_aggregate`` already says of the digest -- every check here
closes an accident, not an attack. The causes worth catching are ordinary: a workbook from a
different export path, the wrong workbook paired with a sidecar, a file damaged in transit.

It also does not measure equivalence to the Parquet. The digest covers the *converted* frame
on both sides, so everything ``DataframeConversionToExcel`` removes -- non-finite floats,
empty strings, sub-second time, the DST-fold distinction -- is gone before the comparison and
invisible to it. That is by construction rather than an oversight: modelling Excel's limits is
the conversion's purpose, and the resulting loss is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

import fastexcel
import polars as pl
from pqx_excel.fast_reader import fast_excel_reader
from pqx_frame.conversion.to_excel import DataframeConversionToExcel
from pqx_frame.hashing.binary_aggregate import DataFrameHasherBinaryAggregateHash
from pqx_sidecar.store import get_sidecar_store

if TYPE_CHECKING:
    from pqx_frame.hashing import HashedDataframe
    from pqx_frame.metadata.columns import DataframeColumnsMetadata
    from pqx_sidecar.document import SidecarDocument
    from upath import UPath

SUPPORTED_HASHER_IDENTIFIER: Final = "binary-aggregate-xxh3-128"
"""The only hasher this build can recompute a digest with."""

SUPPORTED_HASHER_VERSION: Final = 2
"""Its algorithm version. A v1 record aggregated without row relationships, so its dataframe
digest cannot be reproduced from the data and must be recomputed rather than compared."""


type VerdictKind = Literal[
    "valid",
    "digest-mismatch",
    "columns-differ",
    "workbook-unreadable",
    "unsupported-conversion",
    "unsupported-hasher",
    "no-excel-digest",
]
"""Why validation reached the answer it did.

Five of the six are refusals with different causes, and a caller acts on them differently: a
mismatch means investigate the data, an unreadable workbook means investigate the file, an
unsupported version means this build cannot judge the sidecar at all, and a missing digest
means the sidecar was never written for this purpose.

A ``Literal`` rather than an enum, which is how every other closed vocabulary here is spelled
-- ``scope``, the hasher ``identifier``, the dtype ``kind``. It is also the only spelling the
declaration checker permits: enum members are class-body assignments, and annotating one
turns it into a plain attribute rather than a member.
"""

VALID: Final[VerdictKind] = "valid"
DIGEST_MISMATCH: Final[VerdictKind] = "digest-mismatch"
COLUMNS_DIFFER: Final[VerdictKind] = "columns-differ"
WORKBOOK_UNREADABLE: Final[VerdictKind] = "workbook-unreadable"
UNSUPPORTED_CONVERSION: Final[VerdictKind] = "unsupported-conversion"
UNSUPPORTED_HASHER: Final[VerdictKind] = "unsupported-hasher"
NO_EXCEL_DIGEST: Final[VerdictKind] = "no-excel-digest"


@dataclass(frozen=True)
class Verdict:
    """The answer, and enough to act on it.

    Both digests are carried on a mismatch, because "they differ" is rarely the end of the
    question. They are ``None`` where no comparison was reached.
    """

    kind: VerdictKind
    detail: str
    expected_digest: str | None = None
    actual_digest: str | None = None

    @property
    def valid(self) -> bool:
        """Whether the workbook holds what the sidecar describes."""
        return self.kind == VALID


def _written_headers(workbook_path: UPath) -> list[list[str | None]]:
    """The header cells of every sheet, exactly as written.

    Read separately, and before the reader is asked for data, because the reader normalizes
    headers on the way in: duplicates are suffixed and blanks are named. Comparing the names
    it hands back therefore cannot see a header that happens to normalize *into* a name the
    reader would have generated anyway -- a sheet headed ``n, n`` is deduplicated to ``n, n_1``
    and matches a record of exactly those columns. An exporter that writes duplicate or blank
    headers reaches this without anyone meaning harm, which is why it is checked even though
    tampering is out of scope.

    Args:
        workbook_path: The workbook to inspect.

    Returns:
        One list of raw header cells per sheet, in sheet order. A blank cell is ``None``, and
        a sheet with no rows at all contributes an empty list.
    """
    reader: fastexcel.ExcelReader = fastexcel.read_excel(str(workbook_path))
    headers: list[list[str | None]] = []
    name: str
    for name in reader.sheet_names:
        first: pl.DataFrame = reader.load_sheet(name, header_row=None, n_rows=1).to_polars()
        headers.append([] if first.height == 0 else [None if cell is None else str(cell) for cell in first.row(0)])
    return headers


def _excel_record(document: SidecarDocument) -> DataframeColumnsMetadata | None:
    """Return the ToExcel record, found by identifier rather than by position."""
    record: DataframeColumnsMetadata
    for record in document.metadata.column_metadata_of_conversions:
        if record.conversion is not None and record.conversion.identifier == DataframeConversionToExcel.identifier:
            return record
    return None


def validate_workbook(source_path: UPath, workbook_path: UPath, *, store: str = "json") -> Verdict:
    """Decide whether ``workbook_path`` holds the data the sidecar beside ``source_path`` describes.

    Reads the workbook at the schema and row extent the sidecar records, applies the Excel
    conversion, and compares the digest against the one recorded before the file existed.

    Args:
        source_path: The file the sidecar describes. Used to locate the sidecar; never read.
        workbook_path: The workbook to judge.
        store: Identifier of the sidecar store to read through.

    Returns:
        The verdict. Every outcome that is a judgement about the workbook is a value rather
        than an exception, so a batch job does not stop at the first bad file.

    Raises:
        FileNotFoundError: The sidecar or the workbook is not there. A path that does not
            exist is a wiring mistake rather than a statement about the data, and reporting
            it as "invalid" would make it hard to find.
        ValueError: The sidecar is not readable as a document this build understands.
    """
    document: SidecarDocument = get_sidecar_store(store).read(source_path)
    record: DataframeColumnsMetadata | None = _excel_record(document)
    if record is None or record.conversion is None:
        return Verdict(NO_EXCEL_DIGEST, f"{source_path.name} has no {DataframeConversionToExcel.identifier!r} record; it was extracted without that conversion")
    if record.conversion.version != DataframeConversionToExcel.version:
        return Verdict(
            UNSUPPORTED_CONVERSION,
            f"recorded under {DataframeConversionToExcel.identifier} {record.conversion.version}, and this build implements {DataframeConversionToExcel.version}; its digests describe different rules",
        )
    if not record.dataframe_hashes:
        return Verdict(NO_EXCEL_DIGEST, f"{source_path.name} records the conversion but no digest; it was extracted without hashers")

    stored: HashedDataframe = record.dataframe_hashes[0]
    if stored.identifier != SUPPORTED_HASHER_IDENTIFIER or stored.version != SUPPORTED_HASHER_VERSION:
        return Verdict(
            UNSUPPORTED_HASHER,
            f"digest recorded by {stored.identifier} v{stored.version}, and this build recomputes with {SUPPORTED_HASHER_IDENTIFIER} v{SUPPORTED_HASHER_VERSION}",
        )

    # Missing files are the caller's problem; a file that is present but unreadable is a fact
    # about the workbook, so it becomes a verdict.
    if not workbook_path.exists():
        message: str = f"no workbook at {workbook_path}"
        raise FileNotFoundError(message)

    expected_columns: list[str] = [column.name for column in record.columns]
    schema: dict[str, pl.DataType] = {column.name: column.dtype.to_polars() for column in record.columns}
    rows: int | None = record.columns[0].value_count if record.columns else None
    actual: str
    # The boundary covers reading, converting and hashing, not reading alone. A cell can be
    # readable and still unusable: an Excel date serial of 2958466 parses to a Polars date in
    # year 10000, which only fails when the hasher reads the value out. Leaving that outside
    # would abort a batch on one damaged file.
    #
    # FastExcelError covers the reader library refusing the file at all -- a corrupt archive,
    # a missing sheet -- while ValueError covers fast_excel_reader's own refusals, which are
    # about the file not matching what the sidecar says it should hold.
    try:
        sheet_headers: list[list[str | None]] = _written_headers(workbook_path)
        written: list[str | None]
        for written in sheet_headers:
            if written != expected_columns:
                return Verdict(
                    COLUMNS_DIFFER,
                    f"{workbook_path.name} is headed {written} where {source_path.name} describes {expected_columns}",
                    expected_digest=stored.digest_hex,
                )
        back: pl.DataFrame = fast_excel_reader(workbook_path, schema=schema, expected_rows=rows)
        converted: pl.DataFrame = DataframeConversionToExcel().metadata_of_converted_dataframe(back, []).converted_dataframe
        actual = DataFrameHasherBinaryAggregateHash().hash_dataframe(converted).digest_hex
    except (ValueError, pl.exceptions.PolarsError, fastexcel.FastExcelError) as error:
        return Verdict(WORKBOOK_UNREADABLE, f"{workbook_path.name} could not be read as the sidecar describes it: {error}", expected_digest=stored.digest_hex)

    if actual != stored.digest_hex:
        return Verdict(DIGEST_MISMATCH, f"{workbook_path.name} does not hold the data {source_path.name} describes", expected_digest=stored.digest_hex, actual_digest=actual)
    return Verdict(VALID, f"{workbook_path.name} holds the data {source_path.name} describes", expected_digest=stored.digest_hex, actual_digest=actual)
