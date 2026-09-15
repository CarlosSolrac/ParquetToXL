"""The export configuration: user-maintained intent, mirroring ``export-config.schema.json``.

Structure only. Every rule JSON Schema can state lives here as a field constraint or a
``model_validator``; every rule it cannot state -- that a sheet's ``source`` names a declared
alias, that ``partition_column`` is registered in *that* source's ``date_columns``, the
``{source}`` token rule, per-source capacity, two profiles resolving to one directory -- lives in
:mod:`pqx_plan.semantics`, because those need to see more of the document than any one model does.

The split is deliberate and load-bearing: ``export-pipeline-spec.md`` requires a shared corpus of
fragments that the schema and these models classify identically, and that check is only meaningful
if both sides are trying to state the same rules. Anything a validator here enforces that the
schema does not would make the corpus fail for a reason that is not a bug.

Closed vocabularies are ``Literal`` plus ``Final`` constants, never ``enum``: ``check_declarations``
rejects every enum member, because annotating one turns it into a plain attribute rather than a
member.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pqx_calendar.columns import DateColumn
from pqx_calendar.labels import MonthFormat
from pqx_calendar.periods import BasePeriod, PeriodOrder
from pqx_common.names import PortableNameError, validate_identifier
from pydantic import BaseModel, Field, model_validator

from pqx_plan.timestamps import UtcDatetime

CONFIG_VERSION: Final[int] = 1
"""The only configuration version v1 reads. Independent of the sidecar, conversion and hash versions."""

IDENTIFIER_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
r"""Source aliases and profile names, both interpolated into output names.

Spelled exactly as ``export-config.schema.json`` spells it, and **three validators read it three
ways** -- a fact worth stating here because the input that separates them, ``"sales\n"``, would
put a newline in a worksheet name:

- **ECMA-262**, which JSON Schema specifies and a browser-based editor uses: ``$`` matches only
  at the end of the input, so ``"sales\n"`` is refused. This is the intended meaning.
- **Pydantic**, which applies ``pattern`` through ``rust-regex``: same answer, refused. So every
  field using this constant is strict.
- **Python's ``jsonschema`` library**, which implements ``pattern`` with Python's ``re``: ``$``
  also matches before a trailing newline, so it **accepts** ``"sales\n"``, diverging from the
  specification it implements.

Nothing here can fix the third, so the corpus records it as a known dialect divergence rather
than papering over it, and every check this project makes takes the strict reading. Mapping keys
go through ``pqx_common.names.validate_identifier``, whose ``\\A``/``\\Z`` anchors give Python
``re`` the ECMA-262 meaning.
"""

type YearSplitGrid = Literal[6, 4, 3, 2, 1]
"""The calendar grids an oversized year may be tried against.

Only these five divide twelve without a ragged last bucket. Spelled as a ``Literal`` rather than
validated in a body so it matches the schema's ``items.enum`` shape as directly as the two
languages allow.
"""

EXCEL_MAX_DATA_ROWS: Final[int] = 1_048_575
"""Excel's 1,048,576 rows less the one header row every sheet here writes."""

SUPPORTED_WRITER: Final[str] = "rustpy-xlsxwriter"
"""The only writer a configuration may select.

``polars-xlsxwriter`` is refused rather than merely discouraged: it is documented as not
round-trip safe -- ``Float64`` loses precision and 1900-01-01 shifts back a day -- so selecting it
would make every verification fail for reasons unrelated to the data.
"""


class _ConfigModel(BaseModel, extra="forbid", frozen=True):
    """Shared configuration for every model here.

    ``extra="forbid"`` mirrors the schema's ``additionalProperties: false``, which the schema sets
    on every object: an unknown field is rejected rather than ignored, because a misspelled key
    that takes effect as a default is invisible. ``frozen`` because the resolved configuration is
    hashed, and a mutable field would let the hash and the value disagree.

    Repeated as class keywords on every subclass rather than inherited: Pydantic carries the
    config down, but a type checker reads a non-frozen subclass of a frozen base as an error, and
    the keywords are the only thing that tells it otherwise.
    """


class BesideSourceSidecar(_ConfigModel, extra="forbid", frozen=True):
    """Sidecars live next to the Parquet file they describe.

    Needs write access to the source location, which a read-only container will not give.
    """

    kind: Literal["beside_source"]


class DirectorySidecar(_ConfigModel, extra="forbid", frozen=True):
    """Sidecars live together under one directory."""

    kind: Literal["directory"]
    path: str = Field(min_length=1)


type SidecarLocation = Annotated[BesideSourceSidecar | DirectorySidecar, Field(discriminator="kind")]
"""Where this configuration's sidecars are written."""


class ExcelSettings(_ConfigModel, extra="forbid", frozen=True):
    """Which writer produces the workbooks, and what to tell it."""

    writer: Literal["rustpy-xlsxwriter"]
    options: dict[str, object]
    """Required, not defaulted: the schema lists it in ``required``, so a default here would accept
    a document the schema refuses."""


class SourceSettings(_ConfigModel, extra="forbid", frozen=True):
    """One Parquet file and what is interpretable as a date in it.

    ``date_columns`` declares what *could* be partitioned on; a sheet's ``partition_column`` picks
    exactly one. Nothing combines them: a composite grouping such as ``(invoice year, payment
    month)`` is out of scope for v1 and refused rather than approximated.
    """

    path: str = Field(min_length=1)
    date_columns: dict[str, DateColumn]
    """Required, not defaulted, for the same reason as ``ExcelSettings.options``. An empty mapping
    is a legitimate value -- a source used only under `balanced` registers nothing -- but leaving
    the key out entirely is not."""

    @model_validator(mode="after")
    def check_column_names_are_nonempty(self) -> Self:
        """Refuse an empty column name, mirroring the schema's ``propertyNames.minLength``.

        Returns:
            This model, unchanged.

        Raises:
            ValueError: A registered column has an empty name, which names no Parquet column.
        """
        if any(not name for name in self.date_columns):
            message: str = "a date column must be keyed by a source column name, and one key is empty"
            raise ValueError(message)
        return self


class SortKey(_ConfigModel, extra="forbid", frozen=True):
    """One level of a sheet's sort, with its direction and null placement stated rather than defaulted."""

    column: str = Field(min_length=1)
    direction: PeriodOrder
    nulls: Literal["first", "last"]


class SheetSettings(_ConfigModel, extra="forbid", frozen=True):
    """One source's contribution to a profile: which file, which date column, and in what order.

    ``partition_column`` is ``null`` when, and only when, the profile's algorithm is ``balanced``.
    The field stays required so that its absence is written down: left optional, "no date column"
    and "someone forgot" would be the same document. That the null tracks the algorithm is checked
    in :mod:`pqx_plan.semantics`, which can see the profile this sheet belongs to.
    """

    source: str = Field(pattern=IDENTIFIER_PATTERN)
    partition_column: str | None = Field(min_length=1)
    sort: tuple[SortKey, ...]


class ProfileLimits(_ConfigModel, extra="forbid", frozen=True):
    """The two planning ceilings, both enforced and neither an Excel memory guarantee.

    ``max_cells_per_workbook`` counts every exported position, nulls included, plus one header row
    per worksheet, summed across every sheet in the workbook. That matches this project's
    ``value_count`` convention, which also includes nulls.
    """

    max_data_rows_per_worksheet: int = Field(ge=1, le=EXCEL_MAX_DATA_ROWS)
    max_cells_per_workbook: int = Field(ge=1)


class BalancedPartitioning(_ConfigModel, extra="forbid", frozen=True):
    """Slice globally sorted rows evenly. Consults no date at all, which is why no grid is declared."""

    algorithm: Literal["balanced"]
    balance_across: Literal["worksheets", "workbooks"]


class CalendarPartitioning(_ConfigModel, extra="forbid", frozen=True):
    """One shared calendar grid every sheet in the profile is bucketed against.

    The partition column is per sheet, not here: each source has its own date column. What is here
    is the grid, and it applies to every sheet, which is what keeps every source's rows totally and
    disjointly partitioned -- the property the pipeline's verification depends on.
    """

    algorithm: Literal["calendar_greedy", "calendar_periods"]
    base_period: BasePeriod
    period_order: PeriodOrder
    year_split_months: tuple[YearSplitGrid, ...] = Field(min_length=1)
    """Candidate grids in preference order.

    The member values and their uniqueness are structural, so they are stated here and in the
    schema's ``items.enum`` and ``uniqueItems``. That the list is strictly descending and ends in
    ``1`` is semantic, and lives in :mod:`pqx_plan.semantics` -- which is what the schema's own
    description says too. Typing this as a bare ``tuple[int, ...]`` was a real divergence the
    corpus caught: the schema refused ``[5, 1]`` and ``[6, 6, 1]`` and the model accepted both.
    """

    oversized_period: Literal["balanced_rows", "error"]
    null_dates: Literal["separate", "error"]

    @model_validator(mode="after")
    def check_grids_are_distinct(self) -> Self:
        """Refuse a repeated grid, mirroring the schema's ``uniqueItems``.

        Returns:
            This model, unchanged.

        Raises:
            ValueError: A grid appears more than once, which would try it twice and get the same
                answer twice.
        """
        if len(set(self.year_split_months)) != len(self.year_split_months):
            message: str = f"year_split_months {list(self.year_split_months)} repeats a grid; each is tried once"
            raise ValueError(message)
        return self


type Partitioning = Annotated[BalancedPartitioning | CalendarPartitioning, Field(discriminator="algorithm")]
"""How a profile turns rows into sheets."""


class NamingSettings(_ConfigModel, extra="forbid", frozen=True):
    """The templates every output name is rendered from, and the two choices that shape them.

    ``worksheet_prefix`` is literal text, not another template: a nonempty value is prepended to
    every rendered worksheet name with one space, including single-sheet, balanced, undated and
    overflow names, and it counts toward the 31-character limit. For a different separator, leave
    it empty and put literal text in each template.
    """

    workbook: str = Field(min_length=1)
    single_worksheet: str = Field(min_length=1)
    worksheet: str = Field(min_length=1)
    overflow_worksheet: str = Field(min_length=1)
    month_format: MonthFormat
    worksheet_prefix: str
    sheet_collision: Literal["suffix", "error"]


class ProfileSettings(_ConfigModel, extra="forbid", frozen=True):
    """One way of arranging the same sources: an annual review, a monthly view, balanced delivery.

    ``output_subdirectory`` is owned exclusively by this profile. Two profiles resolving to one
    directory is a validation error, and a foreign receipt found there refuses the run.
    """

    name: str = Field(pattern=IDENTIFIER_PATTERN)
    output_subdirectory: str = Field(min_length=1)
    sheets: tuple[SheetSettings, ...] = Field(min_length=1)
    limits: ProfileLimits
    partitioning: Partitioning
    naming: NamingSettings


class ExportConfig(_ConfigModel, extra="forbid", frozen=True):
    """A whole export configuration, as read from disk and before any semantic check.

    Structurally valid is not the same as coherent: a document passing this model may still name a
    source alias that does not exist, or partition on a column that source never registered. Run
    :func:`pqx_plan.semantics.validate_config` before planning anything from it.
    """

    json_schema: str | None = Field(default=None, alias="$schema")
    """The optional schema pointer an editor writes at the top of the document.

    Declared because the JSON Schema declares it. Under ``additionalProperties: false`` on one
    side and ``extra="forbid"`` on the other, omitting it here would make the two disagree about
    the project's own example file -- which is the first thing the corpus check reads.
    """

    config_version: Literal[1]
    config_id: str = Field(min_length=1)
    config_modified_utc: UtcDatetime
    output_directory: str = Field(min_length=1)
    sidecar_location: SidecarLocation
    scratch_root: str | None = Field(min_length=1)
    excel: ExcelSettings
    sources: dict[str, SourceSettings] = Field(min_length=1)
    profiles: tuple[ProfileSettings, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def check_source_aliases(self) -> Self:
        r"""Refuse a source alias outside the pattern, mirroring the schema's ``propertyNames``.

        Pydantic applies ``pattern`` to field *values*, not to mapping keys, so the rule the schema
        states on ``propertyNames`` has to be restated here rather than declared.

        Delegated to ``pqx_common.names.validate_identifier`` rather than spelled with Python's
        ``re`` and the schema's ``^...$``. Python's ``$`` also matches before a trailing newline,
        so that spelling accepts the alias ``"sales\n"`` -- and interpolates a newline into a
        worksheet name. See :data:`IDENTIFIER_PATTERN` for which dialects read it which way.

        Returns:
            This model, unchanged.

        Raises:
            ValueError: An alias is not a legal identifier. Aliases are interpolated into worksheet
                names, so a space or a dot here becomes a name problem at the destination.
        """
        bad: list[str] = []
        alias: str
        for alias in self.sources:
            try:
                validate_identifier(alias, kind="source alias")
            except PortableNameError:
                bad.append(alias)
        if bad:
            message: str = f"source aliases {sorted(bad)} must start with a letter or digit and hold only letters, digits, underscores and hyphens"
            raise ValueError(message)
        return self
