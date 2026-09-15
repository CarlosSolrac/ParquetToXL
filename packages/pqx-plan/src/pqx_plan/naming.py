"""Rendering the names a plan's workbooks and worksheets take, and refusing the ones that collide.

Templates use a small allowlist of tokens with literal text and optional integer zero-padding such
as ``{workbook_index:03d}``. Nothing here evaluates a Python expression, an attribute, an arbitrary
format directive or an environment variable: the tokens are built into a mapping and
``str.format_map`` is given nothing else to reach.

Names are validated **after** the prefix is added and **before** anything is written, because
``worksheet_prefix`` and its separator count toward Excel's 31-character limit -- a name that fits
before prefixing may not after.
"""

from __future__ import annotations

import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pqx_calendar.labels import UNDATED_LABEL, UNDATED_SUFFIX, UNSPLIT_LABEL, CoverageSpan, period_label
from pqx_calendar.periods import PeriodKey, PeriodPrecision
from pqx_common.names import NameRegistry, PortableNameError, deduplicate_worksheet_name, validate_workbook_name, validate_worksheet_name

from pqx_plan.allocate import AllocatedWorkbook
from pqx_plan.partition import PlannedSheet

if TYPE_CHECKING:
    from pqx_calendar.labels import MonthFormat

    from pqx_plan.config import NamingSettings

__all__ = ["NamedSheet", "NamedWorkbook", "name_workbooks", "reject_traversal", "render_template", "sheet_period_label", "workbook_period_label_for"]

PRECISION_RANK: Mapping[PeriodPrecision, int] = {"year": 0, "month": 1, "day": 2}
"""Coarsest first. Used to summarise a workbook whose sheets cover calendar at different grains."""


@dataclass(frozen=True, slots=True)
class NamedSheet:
    """One worksheet with its final, validated name.

    Attributes:
        sheet: The planned sheet this names.
        name: The expanded name, prefix included, as it will appear in the workbook.
        period_label: The coverage label this name was rendered from, or ``None`` for output that
            describes no calendar. Carried rather than re-derived so the manifest records exactly
            what the sheet is called: a second derivation could drift from the first.
    """

    sheet: PlannedSheet
    name: str
    period_label: str | None = None


@dataclass(frozen=True, slots=True)
class NamedWorkbook:
    """One workbook with its final filename and its named sheets, in order.

    Attributes:
        index: One-based workbook index within the profile.
        filename: The basename, extension included.
        sheets: The named sheets, in the order they will be written.
    """

    index: int
    filename: str
    sheets: tuple[NamedSheet, ...]


def reject_traversal(template: str) -> None:
    """Refuse a template field that reaches into a value rather than naming one.

    ``str.format_map`` is **not** sufficient on its own, which is easy to assume and wrong.
    Restricting the mapping restricts which *names* a template can reach, and does nothing about
    what it can reach *through* them: ``{profile.__class__}`` renders ``<class 'str'>``,
    ``{profile.__class__.__mro__}`` walks further, and ``{p[0]}`` indexes. From there the usual
    format-string route into ``__globals__`` is open, and these templates come out of a
    configuration file.

    ``partitioning-spec.md`` says it plainly -- do not evaluate Python expressions, attributes or
    arbitrary format directives -- so attribute and index access are refused outright. Zero-padding
    and other format *specifications* stay, because those are applied to the value and cannot
    traverse it.

    Args:
        template: The naming template.

    Raises:
        ValueError: A field name reaches an attribute or an index, or the template is malformed.
    """
    parsed: tuple[str, str | None, str | None, str | None]
    for parsed in string.Formatter().parse(template):
        field: str | None = parsed[1]
        if field is not None and ("." in field or "[" in field):
            message: str = f"template field {field!r} reaches into a value; naming templates may name a token and format it, never traverse it"
            raise ValueError(message)


def render_template(template: str, tokens: Mapping[str, object]) -> str:
    """Expand one naming template against a fixed token mapping.

    Traversal is refused first (see :func:`reject_traversal`), then ``format_map`` over a plain
    mapping bounds which names a template can reach at all.

    Args:
        template: The naming template.
        tokens: The values available to it.

    Returns:
        The expanded name.

    Raises:
        KeyError: The template names a token that is not available here.
        ValueError: The template is malformed, reaches into a value, or applies a format
            specification a token's type does not support -- ``{profile:03d}`` on a string, say.
    """
    reject_traversal(template)
    return template.format_map(tokens)


def sheet_period_label(sheet: PlannedSheet, month_format: MonthFormat) -> str:
    """Return the ``period_label`` token for one planned sheet.

    ``Undated`` for the null-date bucket, ``Data`` for a sheet describing no calendar -- balanced
    output, and the single-sheet shortcut -- and the coverage label otherwise.

    Args:
        sheet: The planned sheet.
        month_format: The profile's naming choice.

    Returns:
        The label.
    """
    if sheet.kind == "undated":
        return UNDATED_LABEL
    if sheet.coverage is None:
        return UNSPLIT_LABEL
    return period_label(sheet.coverage, month_format)


def workbook_period_label_for(sheets: Sequence[PlannedSheet], month_format: MonthFormat) -> str:
    """Return the ``period_label`` token for a whole workbook.

    Summarises earliest through latest coverage, with ``_Undated`` appended when the workbook also
    holds undated rows, and exactly ``Undated`` when that is all it holds. A workbook of sheets
    that describe no calendar is ``Data``.

    Exact coverage belongs in the manifest; this is a name.

    Args:
        sheets: Every sheet the workbook holds.
        month_format: The profile's naming choice.

    Returns:
        The label.
    """
    spans: list[CoverageSpan] = [sheet.coverage for sheet in sheets if sheet.coverage is not None]
    has_undated: bool = any(sheet.kind == "undated" for sheet in sheets)
    if not spans:
        return UNDATED_LABEL if has_undated else UNSPLIT_LABEL
    # Fan-in makes mixed precision ordinary: one source's 2025 may be subdivided into quarters
    # while another's fits a single year sheet, and both land in this workbook. The summary takes
    # the coarsest grain present, because that is the only one every span can honestly be stated
    # at. Exact coverage belongs in the manifest; this is a name.
    grain: PeriodPrecision = min(spans, key=lambda span: PRECISION_RANK[span.precision]).precision
    first: PeriodKey = min((_coarsen(span.first, grain) for span in spans), key=lambda key: key.sort_key)
    last: PeriodKey = max((_coarsen(span.last, grain) for span in spans), key=lambda key: key.sort_key)
    label: str = period_label(CoverageSpan(first, last), month_format)
    return f"{label}{UNDATED_SUFFIX}" if has_undated else label


def _coarsen(key: PeriodKey, precision: PeriodPrecision) -> PeriodKey:
    """Return the bucket at ``precision`` that contains ``key``.

    Only ever coarsens: the caller picks the coarsest grain present, so a key is already at that
    grain or finer.
    """
    if precision == "year":
        return PeriodKey("year", key.year)
    if precision == "month":
        return PeriodKey("month", key.year, key.month)
    return key


def _worksheet_template(naming: NamingSettings, sheet: PlannedSheet) -> str:
    """Return the template that names one sheet, by its kind."""
    if sheet.kind == "single":
        return naming.single_worksheet
    if sheet.kind == "overflow":
        return naming.overflow_worksheet
    return naming.worksheet


def _prefixed(prefix: str, name: str) -> str:
    """Prepend a nonempty prefix with exactly one space.

    Literal text, not another template. For a different separator, leave the prefix empty and put
    the text in each template instead.
    """
    return f"{prefix} {name}" if prefix else name


def name_workbooks(
    workbooks: Sequence[AllocatedWorkbook],
    naming: NamingSettings,
    *,
    profile: str,
    stems: Mapping[str, str],
) -> tuple[NamedWorkbook, ...]:
    """Render and validate every name a plan produces.

    Worksheet names are unique **within a workbook**; two workbooks may each hold a sheet called
    ``2025``. Workbook filenames are unique within the profile's output directory, and a collision
    there is always an error -- unlike a worksheet collision, which ``sheet_collision`` may resolve
    by suffixing.

    Args:
        workbooks: The allocated workbooks, in order.
        naming: The profile's templates and choices.
        profile: The profile name, for ``{profile}``.
        stems: Each source alias's file basename without ``.parquet``, for ``{source_stem}``.

    Returns:
        The named workbooks, in order.

    Raises:
        PortableNameError: A rendered name is invalid at this project's destinations, or two names
            collide where the policy does not permit it.
        KeyError: A template names a token unavailable to it. The semantic validator catches this
            at configuration time; reaching it here means a template changed since.
    """
    filenames: NameRegistry = NameRegistry(f"profile {profile!r}")
    named: list[NamedWorkbook] = []
    workbook: AllocatedWorkbook
    for workbook in workbooks:
        filename: str = validate_workbook_name(
            render_template(
                naming.workbook,
                {"profile": profile, "period_label": workbook_period_label_for(workbook.sheets, naming.month_format), "workbook_index": workbook.index},
            ),
        )
        filenames.claim(filename)
        named.append(NamedWorkbook(index=workbook.index, filename=filename, sheets=_name_sheets(workbook, naming, profile=profile, stems=stems, filename=filename)))
    return tuple(named)


def _name_sheets(workbook: AllocatedWorkbook, naming: NamingSettings, *, profile: str, stems: Mapping[str, str], filename: str) -> tuple[NamedSheet, ...]:
    """Render and validate one workbook's worksheet names, applying the collision policy."""
    registry: NameRegistry = NameRegistry(f"workbook {filename!r}")
    named: list[NamedSheet] = []
    index: int
    sheet: PlannedSheet
    for index, sheet in enumerate(workbook.sheets):
        rendered: str = render_template(
            _worksheet_template(naming, sheet),
            {
                "source": sheet.source_alias,
                "source_stem": stems[sheet.source_alias],
                "profile": profile,
                "period_label": sheet_period_label(sheet, naming.month_format),
                "sheet_index": index + 1,
                "part_index": sheet.part_index or 1,
            },
        )
        candidate: str = validate_worksheet_name(_prefixed(naming.worksheet_prefix, rendered))
        # None for output that describes no calendar -- balanced, and the single-sheet shortcut --
        # which is what Fragment.period_label expects. `Data` is a name, not a coverage.
        label: str | None = sheet_period_label(sheet, naming.month_format) if (sheet.coverage is not None or sheet.kind == "undated") else None
        if naming.sheet_collision == "suffix":
            named.append(NamedSheet(sheet=sheet, name=deduplicate_worksheet_name(candidate, registry), period_label=label))
            continue
        if candidate in registry:
            message: str = f"worksheet name {candidate!r} is already used in workbook {filename!r}, and sheet_collision is 'error'"
            raise PortableNameError(message)
        named.append(NamedSheet(sheet=sheet, name=registry.claim(candidate), period_label=label))
    return tuple(named)
