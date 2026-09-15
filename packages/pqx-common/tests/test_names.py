"""Frozen tests for what counts as a legal name at this project's destinations.

Every rule here is one Linux would not have enforced. The point of the package is that the host
generating a name is not the host that has to live with it.
"""

from __future__ import annotations

import re

import pytest
from pqx_common.names import (
    AZURE_BLOB_NAME_LIMIT,
    SMB_PATH_LIMIT,
    WORKSHEET_NAME_LIMIT,
    IdentifierError,
    NameRegistry,
    PathLengthError,
    PortableNameError,
    WorkbookNameError,
    WorksheetNameError,
    deduplicate_worksheet_name,
    validate_identifier,
    validate_path_length,
    validate_workbook_name,
    validate_worksheet_name,
)

# --------------------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["sales", "S", "9", "sales_2025", "sales-2025", "a_-b", "Sales2025"])
def test_a_well_formed_identifier_is_accepted(value: str) -> None:
    assert validate_identifier(value) == value


@pytest.mark.parametrize("value", ["", "_sales", "-sales", "sales 2025", "sales.parquet", "sales/2025", "ventas_ñ", "sales\n"])
def test_a_malformed_identifier_is_refused(value: str) -> None:
    # Aliases and profile names are interpolated into filenames, so a space or a dot here
    # becomes a name problem at the destination rather than a configuration problem at source.
    with pytest.raises(IdentifierError):
        validate_identifier(value)


def test_an_identifier_error_names_what_kind_of_identifier_it_was() -> None:
    with pytest.raises(IdentifierError, match="source alias"):
        validate_identifier("bad name", kind="source alias")


# --------------------------------------------------------------------------------------
# Worksheet names
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["2025", "sales 2025-01~06", "debits 2025-Jan~Jun", "a", "a" * WORKSHEET_NAME_LIMIT, "O'Brien sales", "Historical"])
def test_a_legal_worksheet_name_is_accepted(name: str) -> None:
    assert validate_worksheet_name(name) == name


def test_an_empty_worksheet_name_is_refused() -> None:
    with pytest.raises(WorksheetNameError, match="may not be empty"):
        validate_worksheet_name("")


def test_a_worksheet_name_over_thirty_one_characters_is_refused() -> None:
    # The prefix and its separator count toward the limit, which is why validation happens
    # after expansion rather than on the template.
    with pytest.raises(WorksheetNameError, match="32 characters"):
        validate_worksheet_name("a" * (WORKSHEET_NAME_LIMIT + 1))


@pytest.mark.parametrize("character", ["/", "\\", "?", "*", ":", "[", "]"])
def test_each_character_excel_forbids_is_refused(character: str) -> None:
    with pytest.raises(WorksheetNameError, match="Excel forbids"):
        validate_worksheet_name(f"sales{character}2025")


def test_a_control_character_is_refused_and_named_by_codepoint() -> None:
    # Printing the value is useless when the offender has no glyph.
    with pytest.raises(WorksheetNameError, match=r"U\+0009"):
        validate_worksheet_name("sales\t2025")


@pytest.mark.parametrize("name", ["'2025", "2025'", "'2025'"])
def test_a_leading_or_trailing_apostrophe_is_refused(name: str) -> None:
    with pytest.raises(WorksheetNameError, match="apostrophe"):
        validate_worksheet_name(name)


@pytest.mark.parametrize("name", ["History", "history", "HISTORY", "HiStOrY"])
def test_the_reserved_name_history_is_refused_in_any_case(name: str) -> None:
    # Excel treats sheet names case-insensitively, so the reservation is case-insensitive too.
    with pytest.raises(WorksheetNameError, match="reserved"):
        validate_worksheet_name(name)


# --------------------------------------------------------------------------------------
# Workbook names
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["sales_2025_001.xlsx", "a.xlsx", "sales-2025_Undated_001.xlsx", "CONSOLE.xlsx", "con_1.xlsx", "report.final.xlsx"])
def test_a_legal_workbook_name_is_accepted(name: str) -> None:
    assert validate_workbook_name(name) == name


def test_an_empty_workbook_name_is_refused() -> None:
    with pytest.raises(WorkbookNameError, match="may not be empty"):
        validate_workbook_name("")


@pytest.mark.parametrize("name", [".", ".."])
def test_a_name_addressing_a_directory_is_refused(name: str) -> None:
    with pytest.raises(WorkbookNameError, match="addresses a directory"):
        validate_workbook_name(name)


@pytest.mark.parametrize("character", ["<", ">", ":", '"', "/", "\\", "|", "?", "*"])
def test_each_character_windows_and_smb_forbid_is_refused(character: str) -> None:
    # Separators are in the set precisely so a basename cannot smuggle a path component in.
    with pytest.raises(WorkbookNameError, match="Windows and SMB forbid"):
        validate_workbook_name(f"sales{character}2025.xlsx")


def test_a_control_character_in_a_workbook_name_is_refused() -> None:
    with pytest.raises(WorkbookNameError, match=r"U\+0000"):
        validate_workbook_name("sales\x002025.xlsx")


@pytest.mark.parametrize("name", ["sales.xlsx.", "sales.xlsx ", "Q1 ", "sales."])
def test_a_trailing_dot_or_space_is_refused(name: str) -> None:
    # Windows strips both silently, so `Q1 .xlsx` and `Q1.xlsx` become one file and the second
    # write overwrites the first with no error anywhere to point at.
    with pytest.raises(WorkbookNameError, match="ends in a dot or a space"):
        validate_workbook_name(name)


@pytest.mark.parametrize("stem", ["CON", "con", "PRN", "AUX", "NUL", "COM1", "com9", "LPT1", "lpt9"])
def test_a_dos_device_name_is_refused_with_and_without_an_extension(stem: str) -> None:
    with pytest.raises(WorkbookNameError, match="reserved device name"):
        validate_workbook_name(stem)
    with pytest.raises(WorkbookNameError, match="reserved device name"):
        validate_workbook_name(f"{stem}.xlsx")


@pytest.mark.parametrize("stem", ["COM0", "LPT0", "COM10", "CONS", "NULL"])
def test_a_name_merely_resembling_a_device_name_is_accepted(stem: str) -> None:
    assert validate_workbook_name(f"{stem}.xlsx") == f"{stem}.xlsx"


# --------------------------------------------------------------------------------------
# Path length
# --------------------------------------------------------------------------------------


def test_a_path_within_the_smb_limit_is_accepted() -> None:
    path: str = "a" * SMB_PATH_LIMIT
    assert validate_path_length(path) == path


def test_a_path_over_the_smb_limit_is_refused() -> None:
    with pytest.raises(PathLengthError, match="over the 260-character limit"):
        validate_path_length("a" * (SMB_PATH_LIMIT + 1))


def test_the_smb_limit_applies_by_default_even_to_an_azure_style_path() -> None:
    # A profile's output_directory can be repointed at a share next quarter, while the names it
    # produced are already written and expected to repeat.
    with pytest.raises(PathLengthError):
        validate_path_length("az://exports/" + "a" * SMB_PATH_LIMIT)


def test_the_azure_limit_can_be_requested_deliberately() -> None:
    path: str = "a" * (SMB_PATH_LIMIT + 1)
    assert validate_path_length(path, limit=AZURE_BLOB_NAME_LIMIT) == path


def test_the_azure_limit_is_looser_than_the_smb_one() -> None:
    # Recorded so the reason 260 wins is visible rather than inferred.
    assert AZURE_BLOB_NAME_LIMIT > SMB_PATH_LIMIT


# --------------------------------------------------------------------------------------
# Uniqueness
# --------------------------------------------------------------------------------------


def test_a_registry_starts_empty() -> None:
    registry: NameRegistry = NameRegistry("workbook 'sales_001'")
    assert len(registry) == 0
    assert registry.names == ()
    assert "anything" not in registry


def test_claiming_a_name_records_it() -> None:
    registry: NameRegistry = NameRegistry("workbook 'sales_001'")
    assert registry.claim("2025") == "2025"
    assert "2025" in registry
    assert len(registry) == 1
    assert registry.names == ("2025",)


def test_claiming_the_same_name_twice_is_refused() -> None:
    registry: NameRegistry = NameRegistry("workbook 'sales_001'")
    registry.claim("2025")
    with pytest.raises(PortableNameError, match="collides"):
        registry.claim("2025")


def test_a_name_differing_only_in_case_collides() -> None:
    # Sales.xlsx and sales.xlsx are one file on SMB and two on the Linux host that made them.
    registry: NameRegistry = NameRegistry("directory 'exports'")
    registry.claim("Sales.xlsx")
    assert "sales.xlsx" in registry
    with pytest.raises(PortableNameError, match=re.escape("Sales.xlsx")):
        registry.claim("sales.xlsx")


def test_the_registry_keeps_the_first_spelling_claimed() -> None:
    # That is the spelling that will actually be written.
    registry: NameRegistry = NameRegistry("directory 'exports'")
    registry.claim("Sales.xlsx")
    assert registry.names == ("Sales.xlsx",)


def test_a_collision_message_names_the_namespace() -> None:
    registry: NameRegistry = NameRegistry("workbook 'sales_001'")
    registry.claim("2025")
    with pytest.raises(PortableNameError, match="workbook 'sales_001'"):
        registry.claim("2025")


# --------------------------------------------------------------------------------------
# Collision suffixing
# --------------------------------------------------------------------------------------


def test_a_name_that_does_not_collide_is_claimed_unchanged() -> None:
    registry: NameRegistry = NameRegistry("workbook 'sales_001'")
    assert deduplicate_worksheet_name("2025", registry) == "2025"


def test_successive_collisions_take_successive_suffixes() -> None:
    registry: NameRegistry = NameRegistry("workbook 'sales_001'")
    claimed: list[str] = [deduplicate_worksheet_name("2025", registry) for _ in range(4)]
    assert claimed == ["2025", "2025_2", "2025_3", "2025_4"]


def test_a_collision_differing_only_in_case_is_still_suffixed() -> None:
    registry: NameRegistry = NameRegistry("workbook 'sales_001'")
    deduplicate_worksheet_name("Sales", registry)
    assert deduplicate_worksheet_name("sales", registry) == "sales_2"


def test_the_suffix_displaces_the_stem_rather_than_extending_it() -> None:
    # A 31-character name with _2 appended is 33 characters and Excel would refuse it.
    registry: NameRegistry = NameRegistry("workbook 'sales_001'")
    long_name: str = "a" * WORKSHEET_NAME_LIMIT
    deduplicate_worksheet_name(long_name, registry)
    suffixed: str = deduplicate_worksheet_name(long_name, registry)
    assert len(suffixed) == WORKSHEET_NAME_LIMIT
    assert suffixed.endswith("_2")


def test_a_candidate_is_checked_against_every_name_already_claimed() -> None:
    # Truncating two different 31-character stems can produce the same prefix, so checking only
    # against the name that collided would hand out a name already in use.
    registry: NameRegistry = NameRegistry("workbook 'sales_001'")
    long_name: str = "a" * WORKSHEET_NAME_LIMIT
    registry.claim(long_name)
    registry.claim(f"{'a' * (WORKSHEET_NAME_LIMIT - 2)}_2")
    assert deduplicate_worksheet_name(long_name, registry) == f"{'a' * (WORKSHEET_NAME_LIMIT - 2)}_3"


def test_every_generated_candidate_is_itself_a_valid_worksheet_name() -> None:
    # The invariant that lets deduplicate_worksheet_name skip re-validating what it builds.
    # Asserted over the cases that stress it rather than as a branch no input could take.
    stem: str
    for stem in ["2025", "a" * WORKSHEET_NAME_LIMIT, "O'Brien", "Historic", "a"]:
        registry: NameRegistry = NameRegistry("workbook 'x'")
        claimed: list[str] = [deduplicate_worksheet_name(stem, registry) for _ in range(12)]
        name: str
        for name in claimed:
            assert validate_worksheet_name(name) == name
        assert len(set(claimed)) == len(claimed)
