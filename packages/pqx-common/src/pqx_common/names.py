"""What counts as a legal name at this project's destinations, enforced regardless of host.

The host generating names is Linux, and Linux objects to almost nothing: a workbook called
``CON.xlsx``, or ``Q1 .xlsx``, or one differing from its neighbour only in case, is written
without complaint and then fails -- or silently overwrites -- on an SMB share. The destination
may be a Windows or SMB share, Azure Blob or ADLS, or a POSIX mount, so the rules here are the
**union** of what those destinations forbid, applied everywhere.

The rules live in ``pqx-common`` rather than in ``pqx-staging`` so that ``pqx-plan`` can validate
the names it renders without taking a dependency on the package that moves files.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "AZURE_BLOB_NAME_LIMIT",
    "IDENTIFIER_PATTERN",
    "SMB_PATH_LIMIT",
    "WORKSHEET_NAME_LIMIT",
    "IdentifierError",
    "NameRegistry",
    "PathLengthError",
    "PortableNameError",
    "WorkbookNameError",
    "WorksheetNameError",
    "deduplicate_worksheet_name",
    "validate_identifier",
    "validate_path_length",
    "validate_workbook_name",
    "validate_worksheet_name",
]

WORKSHEET_NAME_LIMIT: Final[int] = 31
"""Excel's worksheet-name ceiling. A prefix and its separator count toward it."""

WORKSHEET_FORBIDDEN_CHARACTERS: Final[frozenset[str]] = frozenset("/\\?*:[]")
"""Characters Excel refuses in a worksheet name."""

WORKSHEET_RESERVED_NAMES: Final[frozenset[str]] = frozenset({"history"})
"""Casefolded names Excel reserves. ``History`` is the macro sheet the change log lives on."""

WORKBOOK_FORBIDDEN_CHARACTERS: Final[frozenset[str]] = frozenset('<>:"/\\|?*')
"""Characters Windows and SMB refuse in a filename. A superset of what POSIX minds."""

WINDOWS_RESERVED_DEVICE_NAMES: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{digit}" for digit in range(1, 10)), *(f"lpt{digit}" for digit in range(1, 10))},
)
"""Casefolded DOS device names. Reserved with any extension, so ``CON.xlsx`` is refused too."""

TRAVERSAL_NAMES: Final[frozenset[str]] = frozenset({".", ".."})
"""The two names that address a directory rather than a file in it."""

SMB_PATH_LIMIT: Final[int] = 260
"""Maximum full path length on SMB. The tighter of the two destination limits, so it binds."""

AZURE_BLOB_NAME_LIMIT: Final[int] = 1024
"""Maximum Azure blob name length. Looser than SMB, and recorded so the reason 260 wins is visible."""

IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z")
"""The shape of a source alias or a profile name. Both are interpolated into output names, so
both are restricted to characters every destination accepts without escaping.

``partitioning-spec.md`` writes this pattern as ``^[A-Za-z0-9][A-Za-z0-9_-]*$``, and it is
transcribed here with ``\\A`` and ``\\Z`` instead, which is not a change of intent but a
correction of dialect. In ECMA-262 -- the flavour JSON Schema uses, so the flavour the spec's
pattern is written in -- an unflagged ``$`` matches only at the end of the input. In Python it
also matches *before a trailing newline*, so the spec's pattern transcribed literally accepts
the alias ``"sales\\n"`` and interpolates a newline into a filename. The two anchors here have
the ECMA-262 meaning in Python.

This divergence is exactly what Phase C's shared valid/invalid corpus exists to catch, and it
is the first instance found: the schema and the model would have classified ``"sales\\n"``
differently while looking like they said the same thing."""


class PortableNameError(Exception):
    """A name would not survive at one of this project's destinations.

    Named for the rule rather than the host, because the point is that the check does not depend
    on which host happens to be running: a name refused here is refused on Linux too.
    """


class WorksheetNameError(PortableNameError):
    """A worksheet name Excel would refuse, or one that collides inside its workbook."""


class WorkbookNameError(PortableNameError):
    """A workbook basename a Windows, SMB or Azure destination would refuse."""


class IdentifierError(PortableNameError):
    """A source alias or profile name outside the pattern both are restricted to."""


class PathLengthError(PortableNameError):
    """A full destination path longer than the tightest destination allows.

    Distinct from :class:`WorkbookNameError` because the name may be perfectly legal and the
    directory it is being written into is what pushed the path over: the fix is a shorter
    ``output_directory``, not a different naming template.
    """


def _describe_control_characters(value: str) -> str:
    """Return a readable list of the control characters in a value.

    Printing the value itself is useless when the problem is a character with no glyph, so the
    offenders are named by codepoint instead.
    """
    return ", ".join(sorted({f"U+{ord(character):04X}" for character in value if character < " " or character == "\x7f"}))


def validate_identifier(value: str, *, kind: str = "identifier") -> str:
    """Check a source alias or profile name and return it unchanged.

    Args:
        value: The candidate.
        kind: What to call it in the message, such as ``"source alias"``.

    Returns:
        ``value``, unchanged.

    Raises:
        IdentifierError: The value does not match :data:`IDENTIFIER_PATTERN`. Both aliases and
            profile names are interpolated into filenames, so a space or a dot here becomes a
            name problem at the destination rather than a configuration problem at the source.
    """
    if not IDENTIFIER_PATTERN.match(value):
        message: str = f"{kind} {value!r} must start with a letter or digit and hold only letters, digits, underscores and hyphens"
        raise IdentifierError(message)
    return value


def validate_worksheet_name(name: str) -> str:
    """Check one fully expanded worksheet name and return it unchanged.

    Applied *after* ``worksheet_prefix`` and its separator are added, because the prefix counts
    toward the 31-character limit and a name that fits before prefixing may not after.

    ``rustpy-xlsxwriter``'s own ``validate_sheet_name`` covers the length and the character set
    but not ``History``, apostrophes or uniqueness, so the whole rule is restated here where
    ``pqx-plan`` can reach it without depending on the writer.

    Args:
        name: The expanded name, prefix included.

    Returns:
        ``name``, unchanged.

    Raises:
        WorksheetNameError: The name is empty, too long, holds a forbidden character or a control
            character, begins or ends with an apostrophe, or is the reserved name ``History``.
    """
    if not name:
        empty_message: str = "a worksheet name may not be empty"
        raise WorksheetNameError(empty_message)
    if len(name) > WORKSHEET_NAME_LIMIT:
        length_message: str = f"worksheet name {name!r} is {len(name)} characters; Excel allows {WORKSHEET_NAME_LIMIT}"
        raise WorksheetNameError(length_message)
    forbidden: str = "".join(sorted({character for character in name if character in WORKSHEET_FORBIDDEN_CHARACTERS}))
    if forbidden:
        character_message: str = f"worksheet name {name!r} holds {forbidden!r}; Excel forbids / \\ ? * : [ ]"
        raise WorksheetNameError(character_message)
    controls: str = _describe_control_characters(name)
    if controls:
        control_message: str = f"worksheet name {name!r} holds control characters ({controls})"
        raise WorksheetNameError(control_message)
    if name.startswith("'") or name.endswith("'"):
        apostrophe_message: str = f"worksheet name {name!r} begins or ends with an apostrophe, which Excel refuses"
        raise WorksheetNameError(apostrophe_message)
    if name.casefold() in WORKSHEET_RESERVED_NAMES:
        reserved_message: str = f"worksheet name {name!r} is reserved by Excel"
        raise WorksheetNameError(reserved_message)
    return name


def validate_workbook_name(name: str) -> str:
    """Check one workbook basename and return it unchanged.

    The basename, extension included -- ``sales_2025_001.xlsx``, never a path. Separators are
    among the forbidden characters precisely so that a name cannot smuggle one in.

    Args:
        name: The rendered basename.

    Returns:
        ``name``, unchanged.

    Raises:
        WorkbookNameError: The name is empty, holds a forbidden or control character, ends in a
            dot or a space, addresses a directory, or collides with a DOS device name.
    """
    if not name:
        empty_message: str = "a workbook name may not be empty"
        raise WorkbookNameError(empty_message)
    if name in TRAVERSAL_NAMES:
        traversal_message: str = f"workbook name {name!r} addresses a directory rather than a file in one"
        raise WorkbookNameError(traversal_message)
    forbidden: str = "".join(sorted({character for character in name if character in WORKBOOK_FORBIDDEN_CHARACTERS}))
    if forbidden:
        character_message: str = f'workbook name {name!r} holds {forbidden!r}; Windows and SMB forbid < > : " / \\ | ? *'
        raise WorkbookNameError(character_message)
    controls: str = _describe_control_characters(name)
    if controls:
        control_message: str = f"workbook name {name!r} holds control characters ({controls})"
        raise WorkbookNameError(control_message)
    if name.endswith((".", " ")):
        # Windows silently strips both, so `Q1 .xlsx` and `Q1.xlsx` become the same file and the
        # second write overwrites the first -- with no error anywhere to point at.
        trailing_message: str = f"workbook name {name!r} ends in a dot or a space, which Windows strips silently"
        raise WorkbookNameError(trailing_message)
    stem: str = name.partition(".")[0]
    if stem.casefold() in WINDOWS_RESERVED_DEVICE_NAMES:
        device_message: str = f"workbook name {name!r} uses the reserved device name {stem!r}, which is reserved with any extension"
        raise WorkbookNameError(device_message)
    return name


def validate_path_length(path: str, *, limit: int = SMB_PATH_LIMIT) -> str:
    """Check that a full destination path fits the tightest destination limit.

    The default is SMB's 260 rather than Azure's 1024, and it applies even to an Azure-only
    destination. A profile's ``output_directory`` is configuration and can be repointed at a
    share next quarter, while the names it produced are already on disk and are expected to
    repeat; validating against the loosest destination would make that repointing the moment a
    year of stable names becomes unwritable.

    Args:
        path: The full path, not a basename.
        limit: The ceiling to apply. Pass :data:`AZURE_BLOB_NAME_LIMIT` deliberately, never by
            default, and only where a destination is known never to be an SMB share.

    Returns:
        ``path``, unchanged.

    Raises:
        PathLengthError: The path is longer than ``limit``.
    """
    if len(path) > limit:
        message: str = f"destination path is {len(path)} characters, over the {limit}-character limit: {path!r}"
        raise PathLengthError(message)
    return path


class NameRegistry:
    """Case-insensitive uniqueness within one namespace: a workbook's sheets, or a directory.

    Case-insensitive because ``Sales.xlsx`` and ``sales.xlsx`` are one file on SMB and two on
    the Linux host that generated them, and because Excel treats two sheet names differing only
    in case as the same name. The registry keeps the first spelling claimed, since that is the
    one that will be written.

    Attributes:
        label: What this namespace is called in error messages.
    """

    def __init__(self, label: str) -> None:
        """Initialize an empty registry.

        Args:
            label: What to call this namespace in a message, such as ``"workbook 'sales_001'"``.
        """
        self.label: str = label
        self._claimed: dict[str, str] = {}

    def __contains__(self, name: str) -> bool:
        """Return whether a name is already claimed, ignoring case."""
        return name.casefold() in self._claimed

    def __len__(self) -> int:
        """Return how many names are claimed."""
        return len(self._claimed)

    @property
    def names(self) -> tuple[str, ...]:
        """Return the claimed names in the order they were claimed, in their original spelling."""
        return tuple(self._claimed.values())

    def claim(self, name: str) -> str:
        """Record a name as used, refusing one already taken.

        Args:
            name: The name to claim.

        Returns:
            ``name``, unchanged.

        Raises:
            PortableNameError: The name, or one differing from it only in case, is already claimed.
        """
        folded: str = name.casefold()
        existing: str | None = self._claimed.get(folded)
        if existing is not None:
            message: str = f"{self.label} already holds {existing!r}, which collides with {name!r} case-insensitively"
            raise PortableNameError(message)
        self._claimed[folded] = name
        return name


def deduplicate_worksheet_name(name: str, registry: NameRegistry) -> str:
    """Claim a worksheet name, appending ``_2``, ``_3`` and so on until one is free.

    The ``sheet_collision: suffix`` policy. Space for the suffix is reserved by truncating the
    stem, because a 31-character name with ``_2`` appended is 33 characters and Excel would
    refuse it -- so the suffix has to displace the stem rather than extend it.

    Each candidate is checked against *every* name already claimed, not only against the one it
    collided with: truncating two different 31-character stems can produce the same prefix.

    Args:
        name: The rendered name, already valid on its own.
        registry: The workbook's namespace, which this call mutates.

    Every candidate is a valid worksheet name by construction, so none is re-validated here:
    it is a nonempty prefix of an already-valid name followed by ``_`` and digits, which cannot
    exceed the limit, introduce a forbidden character, end in an apostrophe or spell ``History``.
    A test asserts that invariant over the cases that stress it, rather than a branch in this
    function that no input could take.

    Returns:
        The name actually claimed, which is ``name`` when nothing collided.
    """
    if name not in registry:
        return registry.claim(name)
    index: int = 2
    while True:
        suffix: str = f"_{index}"
        candidate: str = f"{name[: WORKSHEET_NAME_LIMIT - len(suffix)]}{suffix}"
        if candidate not in registry:
            return registry.claim(candidate)
        index += 1
