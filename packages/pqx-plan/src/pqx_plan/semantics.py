"""The configuration rules JSON Schema cannot state, checked over the whole document.

Structural validity is not coherence. A document that satisfies ``export-config.schema.json``
and :mod:`pqx_plan.config` may still name a source alias nothing declares, partition on a column
that source never registered, or render the same worksheet name for every source in a workbook.
Each of those needs to see more of the document than any one object does, which is exactly the
class of rule JSON Schema has no way to express.

``export-pipeline-spec.md`` lists them, and this module is where they live. It is also what makes
the schema-versus-model corpus honest: anything checked here is deliberately *not* checked by the
schema, so the corpus compares two sides that are trying to say the same thing.

Every finding is collected before any is raised. A configuration with four mistakes should report
four, because the alternative is four edit-and-rerun cycles to discover what one pass knew.
"""

from __future__ import annotations

import string
from collections.abc import Iterable, Iterator, Sequence
from typing import Final

from pqx_calendar.errors import BasePeriodUnsupportedError
from pqx_calendar.grids import YearSplitMonthsError, validate_year_split_months
from pqx_calendar.periods import require_base_period_supported

from pqx_plan.config import CalendarPartitioning, ExportConfig, ProfileSettings, SheetSettings, SortKey, SourceSettings

__all__ = ["ConfigSemanticError", "validate_config"]

SOURCE_TOKEN: Final[str] = "source"  # noqa: S105  # a template token, not a credential
"""The template token that distinguishes one source's sheets from another's in a shared workbook."""

WORKBOOK_TOKENS: Final[frozenset[str]] = frozenset({"profile", "period_label", "workbook_index"})
"""Tokens a workbook filename template may use."""

WORKSHEET_TOKENS: Final[frozenset[str]] = frozenset({"source", "source_stem", "profile", "period_label", "sheet_index"})
"""Tokens a worksheet name template may use."""

OVERFLOW_TOKENS: Final[frozenset[str]] = WORKSHEET_TOKENS | {"part_index"}
"""``overflow_worksheet`` additionally names the row fragment within its calendar bucket."""


class ConfigSemanticError(Exception):
    """A configuration is structurally valid and does not cohere.

    Attributes:
        findings: Every problem found, in document order. Never empty.
    """

    def __init__(self, findings: Sequence[str]) -> None:
        """Initialize the error.

        Args:
            findings: The problems found, in document order.
        """
        self.findings: tuple[str, ...] = tuple(findings)
        count: str = "1 problem" if len(self.findings) == 1 else f"{len(self.findings)} problems"
        super().__init__(f"the export configuration has {count}:\n" + "\n".join(f"  - {finding}" for finding in self.findings))


def template_tokens(template: str) -> frozenset[str]:
    """Return the field names a naming template interpolates.

    Uses ``string.Formatter`` rather than a regular expression so that format specifications --
    ``{workbook_index:03d}`` -- and escaped braces are read the way ``str.format`` will read them
    when the name is actually rendered. A regex would have to reimplement that and would disagree
    at the first template using one.

    Args:
        template: The naming template.

    Returns:
        The token names, without their format specifications.

    Raises:
        ValueError: The template is not parseable as a format string at all.
    """
    return frozenset(name for _, name, _, _ in string.Formatter().parse(template) if name)


def _check_template(label: str, template: str, allowed: frozenset[str]) -> Iterator[str]:
    """Yield a finding for each unknown token in one template, and for a malformed template.

    Traversal is reported on its own rather than as an unknown token, because
    ``{profile.__class__}`` is not a typo: it is the first step of the format-string route into
    ``__globals__``, and a message about an unrecognised token would send its author looking for
    the right spelling.
    """
    try:
        used: frozenset[str] = template_tokens(template)
    except ValueError as exc:
        yield f"{label} template {template!r} is not a valid format string: {exc}"
        return
    traversing: frozenset[str] = frozenset(token for token in used if "." in token or "[" in token)
    if traversing:
        yield f"{label} template {template!r} reaches into a value with {sorted(traversing)}; a template may name a token and format it, never traverse it"
    unknown: frozenset[str] = used - allowed - traversing
    if unknown:
        yield f"{label} template {template!r} uses unknown tokens {sorted(unknown)}; available: {sorted(allowed)}"


def _check_source_token_rule(profile: ProfileSettings) -> Iterator[str]:
    """Yield a finding when a multi-sheet profile's worksheet templates omit ``{source}``.

    Without it every source in a workbook renders the same name -- ``2025`` twice -- and
    ``sheet_collision: suffix`` resolves that to ``2025`` and ``2025_2``, which is worse than
    failing, because the names then carry no indication of which source they hold.
    """
    if len(profile.sheets) <= 1:
        return
    label: str
    template: str
    for label, template in (("worksheet", profile.naming.worksheet), ("overflow_worksheet", profile.naming.overflow_worksheet)):
        try:
            used: frozenset[str] = template_tokens(template)
        except ValueError:
            continue
        if SOURCE_TOKEN not in used:
            yield f"profile {profile.name!r} has {len(profile.sheets)} sheets, so its {label} template {template!r} must contain {{source}}; without it every source renders the same name"


def _check_sheet(config: ExportConfig, profile: ProfileSettings, index: int, sheet: SheetSettings) -> Iterator[str]:
    """Yield findings for one sheet: its alias, its partition column, and its sort keys."""
    where: str = f"profile {profile.name!r} sheet {index}"
    if sheet.source not in config.sources:
        yield f"{where} names source {sheet.source!r}, which is not declared; declared: {sorted(config.sources)}"
        return
    source: SourceSettings = config.sources[sheet.source]

    is_calendar: bool = isinstance(profile.partitioning, CalendarPartitioning)
    if is_calendar and sheet.partition_column is None:
        yield f"{where} has no partition_column, which is required under algorithm {profile.partitioning.algorithm!r}"
    if not is_calendar and sheet.partition_column is not None:
        yield f"{where} sets partition_column to {sheet.partition_column!r}, but algorithm 'balanced' consults no date at all"

    if sheet.partition_column is not None:
        if sheet.partition_column not in source.date_columns:
            yield f"{where} partitions on {sheet.partition_column!r}, which source {sheet.source!r} does not register; registered: {sorted(source.date_columns)}"
        elif isinstance(profile.partitioning, CalendarPartitioning):
            try:
                require_base_period_supported(source.date_columns[sheet.partition_column], profile.partitioning.base_period)
            except BasePeriodUnsupportedError as exc:
                yield f"{where}: {exc}"

    seen: set[str] = set()
    key_index: int
    key: SortKey
    for key_index, key in enumerate(sheet.sort):
        if key.column in seen:
            yield f"{where} sorts on {key.column!r} twice; the second key at position {key_index} can never change the order"
        seen.add(key.column)


def _check_profile(config: ExportConfig, profile: ProfileSettings) -> Iterator[str]:
    """Yield findings for one profile: its grid, its templates, its limits, and each sheet."""
    if isinstance(profile.partitioning, CalendarPartitioning):
        try:
            validate_year_split_months(profile.partitioning.year_split_months)
        except YearSplitMonthsError as exc:
            yield f"profile {profile.name!r}: {exc}"

    yield from _check_template("workbook", profile.naming.workbook, WORKBOOK_TOKENS)
    yield from _check_template("single_worksheet", profile.naming.single_worksheet, WORKSHEET_TOKENS)
    yield from _check_template("worksheet", profile.naming.worksheet, WORKSHEET_TOKENS)
    yield from _check_template("overflow_worksheet", profile.naming.overflow_worksheet, OVERFLOW_TOKENS)
    yield from _check_source_token_rule(profile)

    index: int
    sheet: SheetSettings
    for index, sheet in enumerate(profile.sheets):
        yield from _check_sheet(config, profile, index, sheet)


def _check_uniqueness(config: ExportConfig) -> Iterator[str]:
    """Yield findings for duplicate profile names and for two profiles owning one directory."""
    seen_names: dict[str, int] = {}
    seen_directories: dict[str, str] = {}
    profile: ProfileSettings
    for profile in config.profiles:
        folded: str = profile.name.casefold()
        if folded in seen_names:
            yield f"profile name {profile.name!r} is used more than once; names must be distinct case-insensitively"
        seen_names[folded] = seen_names.get(folded, 0) + 1

        # Normalised the way a filesystem would see it: a trailing separator and a case
        # difference are two spellings of one directory on the destinations this writes to.
        directory: str = profile.output_subdirectory.rstrip("/\\").casefold()
        owner: str | None = seen_directories.get(directory)
        if owner is not None:
            yield f"profiles {owner!r} and {profile.name!r} both resolve to output subdirectory {profile.output_subdirectory!r}, which each owns exclusively"
        else:
            seen_directories[directory] = profile.name


def _check_unused_sources(config: ExportConfig) -> Iterator[str]:
    """Yield a finding for a declared source no profile ever reads.

    Not an error anywhere else, and stated as one here because the usual cause is a renamed alias
    in the sheets that was not renamed in ``sources``: the export then succeeds and quietly omits
    a file the author believes it is exporting.
    """
    used: set[str] = {sheet.source for profile in config.profiles for sheet in profile.sheets}
    unused: set[str] = set(config.sources) - used
    if unused:
        yield f"sources {sorted(unused)} are declared and no profile reads them; a renamed alias in `sheets` that was not renamed here exports nothing and reports success"


def findings(config: ExportConfig) -> tuple[str, ...]:
    """Return every semantic problem in a configuration, in document order.

    Args:
        config: A structurally valid configuration.

    Returns:
        The findings, empty when the configuration coheres.
    """
    found: Iterable[str] = (
        *_check_uniqueness(config),
        *(finding for profile in config.profiles for finding in _check_profile(config, profile)),
        *_check_unused_sources(config),
    )
    return tuple(found)


def validate_config(config: ExportConfig) -> ExportConfig:
    """Check a configuration's coherence and return it unchanged.

    Args:
        config: A structurally valid configuration.

    Returns:
        ``config``, unchanged.

    Raises:
        ConfigSemanticError: The configuration does not cohere. Every problem is reported at once.
    """
    found: tuple[str, ...] = findings(config)
    if found:
        raise ConfigSemanticError(found)
    return config
