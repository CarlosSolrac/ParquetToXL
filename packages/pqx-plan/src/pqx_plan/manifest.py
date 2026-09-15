"""The run manifest: what a write produced, and what verification and reconciliation read back.

``export-pipeline-spec.md`` chose to compute every expected digest at write time, when the
converted frame is already in memory and the digest is therefore free. Verification then needs
only this manifest, the sidecar and the workbooks, and never reopens the Parquet file -- which
is the point, because the staged Parquet is deleted before verification runs.

Published per profile as ``<profile>.manifest.json``.

**Every path here is relative to the destination.** The same manifest then serves verification,
which joins the scratch root, and reconciliation, which joins the destination, and stays valid
if the destination is moved or remapped. ``DestinationRelativePath`` is what makes that a
refusal rather than a hope.

**What this model deliberately does not check.** ``expected_row_count`` is not required to equal
the sum of the fragments' ``row_count``, and the fragments' digests are not required to sum to
``expected_whole``. Both are verification's job, per the spec's per-source checks 4 and 5, and
both exist to name a specific failure -- a dropped fragment, a mutated cell. A manifest that
failed them would be a manifest verification could not load, so the mismatch it exists to report
would surface as a validation error naming the wrong thing.

What *is* checked here is what nothing downstream could recover from: an allocation that named
two things one place, or that named a place nothing claims. Every such check compares names
**case-insensitively**, per ``partitioning-spec.md``: worksheet names must be unique that way
within a workbook because Excel says so, and workbook names must be because ``Sales.xlsx`` and
``sales.xlsx`` are one file on SMB. Comparing them exactly would pass a manifest on the Linux
host that writes it and overwrite a workbook on the share that receives it, with verification --
which reads from local scratch -- seeing nothing wrong.
"""

from __future__ import annotations

from typing import Final, Literal, Self

from pqx_frame.hashing.binary_aggregate import BinaryAggregateHashedDataframe
from pqx_frame.metadata.columns import ConversionIdentity
from pydantic import BaseModel, JsonValue, NonNegativeInt, model_validator

from pqx_plan.paths import DestinationRelativePath
from pqx_plan.timestamps import UtcDatetime

type ManifestVersion = Literal[1]
"""The manifest schema versions this build reads. A closed vocabulary, so a ``Literal``.

Not an ``Enum``: an enum member is a bare class-body assignment, which
``tools/check_declarations.py`` rejects as a binding without an annotation, and which would
also serialise as its member name rather than as the number written to the file.
"""

MANIFEST_VERSION: Final[ManifestVersion] = 1
"""The version this build writes.

Annotated with the alias rather than left to inference, so widening one without the other does
not compile: change the alias to ``Literal[2]`` and this line is the next error.

Separate from ``SIDECAR_SCHEMA_VERSION`` and from every hasher and conversion version, for the
reason the sidecar's own docstring gives -- this one describes the shape of a file, and folding
it into a digest version would bump stored digests every time the layout moved.
"""

type ResolvedConfiguration = dict[str, JsonValue]
"""The resolved configuration, as it will be serialised into the manifest.

**A stand-in.** ``export-pipeline-spec.md`` types this field ``ExportConfig``, which Phase C
builds; ``docs/export-config.schema.json`` already describes its shape. Modelling it here would
be writing Phase C inside a gate, and gate 0d can still change what a profile may declare. A
JSON object round-trips through the file identically either way, so replacing this alias with
``ExportConfig`` later changes what is validated and nothing about what is written.
"""


class Fragment(BaseModel, frozen=True):
    """One sheet's worth of one source's rows, and the digest it is expected to hash to.

    A fragment, not a sheet: fan-in puts several sources' sheets in one workbook, so
    ``source_alias`` is what says whose rows these are.
    """

    workbook: DestinationRelativePath
    sheet_name: str
    """Final, after the worksheet prefix and any collision resolution -- what verification asks
    ``fast_excel_reader`` for by name, so a pre-resolution name here would read the wrong sheet
    or none."""

    source_alias: str
    period_label: str | None
    """The coverage label, or ``None`` for balanced output, which consults no date at all."""

    part_index: int | None = None
    """Set only for an overflow fragment: a period too large for one sheet split into parts."""

    row_count: NonNegativeInt
    """Data rows, header excluded. Verification restores a trailing all-null run from it, which
    is only well defined per sheet -- which is why one ``fast_excel_reader`` call per fragment
    is strictly better than one per workbook rather than merely necessary for fan-in."""

    column_names: list[str]
    """As written, before the reader's normalisation of duplicates and blanks. Comparing against
    the normalised form could not see a header that normalises into a name the reader would have
    generated anyway."""

    expected: BinaryAggregateHashedDataframe
    """Computed at write time from the converted frame, while it is still in memory."""


class SourceFragments(BaseModel, frozen=True):
    """Every fragment one source contributed, and what the whole source is expected to hash to."""

    source_alias: str
    source_path: str
    """The **original** location, not the scratch copy. The sidecar's ``modified_utc`` is the
    remote source's mtime and this is the path it belongs to; recording the staging path here
    would make the pair describe a file that is deleted before verification even starts."""

    sidecar_path: DestinationRelativePath
    conversion: ConversionIdentity
    """Pinned so a later build that changed the conversion is a version mismatch rather than a
    digest mismatch, which names the wrong problem."""

    total_and_disjoint: bool = True
    """Always ``True`` in v1, and the seam for a future row filter or column projection. When it
    is false the reassembly check is skipped, because the fragments no longer cover the source."""

    expected_whole: BinaryAggregateHashedDataframe
    expected_row_count: NonNegativeInt
    fragments: list[Fragment]

    @model_validator(mode="after")
    def _fragments_belong_to_this_source(self) -> Self:
        """Refuse a fragment recorded under another source's alias.

        The per-source reassembly sums exactly the fragments listed here, so one belonging to a
        different source would be summed into the wrong total and read as a digest mismatch on
        both sources at once.

        Returns:
            ``self``, unchanged.

        Raises:
            ValueError: A fragment names a different ``source_alias``.
        """
        strays: list[str] = sorted({fragment.source_alias for fragment in self.fragments if fragment.source_alias != self.source_alias})
        if strays:
            message: str = f"source {self.source_alias!r} lists fragments belonging to {strays}"
            raise ValueError(message)
        return self


class RunManifest(BaseModel, frozen=True):
    """Everything one profile's run wrote, and everything needed to check and reconcile it."""

    manifest_version: ManifestVersion = MANIFEST_VERSION
    """First, so it is readable at the head of the file, and a ``Literal`` so a future version
    is refused by validation rather than by whichever field happened to move."""

    run_id: str
    config_path: str
    """Where the configuration was read from. Not destination-relative: it is on the host that
    ran the export, and it is recorded to explain the run rather than to be joined with anything."""

    profile: str
    planner_version: str
    """Left an open string. The planner that would pin it is Phase C, and inventing its version
    here would be declaring a contract for code that does not exist."""

    resolved_config: ResolvedConfiguration
    output_directory: str
    """The destination every ``DestinationRelativePath`` in this manifest is relative to. Itself
    absolute, and possibly remote."""

    started_utc: UtcDatetime
    sources: list[SourceFragments]
    workbooks: list[DestinationRelativePath]
    """The keep-set reconciliation subtracts from a directory listing, so an omission here is a
    deletion rather than an inconsistency."""

    sidecars: list[DestinationRelativePath]
    """Wherever ``sidecar_location`` put them, which may not be beside the workbooks."""

    @model_validator(mode="after")
    def _every_fragment_names_a_listed_workbook(self) -> Self:
        """Refuse a fragment whose workbook the manifest does not list.

        Reconciliation deletes everything in the destination that the manifest does not claim.
        A workbook holding real fragments but missing from ``workbooks`` is therefore deleted by
        the run that wrote it, and verification -- which reads from scratch -- would have passed
        it first.

        Returns:
            ``self``, unchanged.

        Raises:
            ValueError: A fragment names a workbook absent from ``workbooks``.
        """
        listed: set[str] = set(self.workbooks)
        missing: list[str] = sorted({fragment.workbook for source in self.sources for fragment in source.fragments if fragment.workbook not in listed})
        if missing:
            message: str = f"fragments name workbooks the manifest does not list: {missing}"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _no_two_fragments_claim_one_sheet(self) -> Self:
        """Refuse two fragments claiming the same sheet of the same workbook, ignoring case.

        The second write overwrote the first, so one fragment's rows are not in the workbook at
        all. Verification would read the surviving sheet once per fragment, compare it against
        two different expected digests, and report a mismatch on whichever lost -- naming a
        corrupt cell where the real fault is an allocation that assigned one sheet twice.

        Case-insensitive because Excel is: a workbook cannot hold both ``Sales`` and ``sales``,
        so an allocation that produced both is the same overwrite wearing a different spelling.

        Returns:
            ``self``, unchanged.

        Raises:
            ValueError: One workbook-and-sheet pair is claimed more than once.
        """
        seen: set[tuple[str, str]] = set()
        repeated: set[str] = set()
        source: SourceFragments
        for source in self.sources:
            fragment: Fragment
            for fragment in source.fragments:
                claim: tuple[str, str] = (fragment.workbook.casefold(), fragment.sheet_name.casefold())
                if claim in seen:
                    repeated.add(f"{fragment.workbook}!{fragment.sheet_name}")
                seen.add(claim)
        if repeated:
            message: str = f"more than one fragment claims each of these sheets, compared without case: {sorted(repeated)}"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _no_two_workbooks_differ_only_by_case(self) -> Self:
        """Refuse two listed workbooks that are one file at the destination.

        ``partitioning-spec.md`` requires workbook names to be unique case-insensitively,
        because the destination may be SMB, where they are not two names. Two entries here mean
        the second publish overwrote the first, and reconciliation then keeps a file holding the
        wrong workbook's sheets while verification, reading scratch, never sees it.

        Returns:
            ``self``, unchanged.

        Raises:
            ValueError: Two entries differ only by case.
        """
        folded: dict[str, str] = {}
        collisions: list[str] = []
        workbook: str
        for workbook in self.workbooks:
            if workbook.casefold() in folded:
                collisions.append(f"{folded[workbook.casefold()]!r} and {workbook!r}")
            folded[workbook.casefold()] = workbook
        if collisions:
            message: str = f"workbook names must be unique without case, because they are one file on SMB: {collisions}"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _every_sidecar_a_source_names_is_claimed(self) -> Self:
        """Refuse a source whose sidecar the manifest does not list.

        ``sidecars`` is a keep-set entry exactly as ``workbooks`` is, and the same failure
        follows from an omission: the sidecar is published early -- it marks *described*, so a
        crash after it costs the export rather than the hashing -- and then deleted at the end of
        the same run by the reconcile that does not know it is ours. The next run finds no
        sidecar, calls the source stale, and re-hashes the Parquet the sidecar existed to avoid
        re-hashing.

        Returns:
            ``self``, unchanged.

        Raises:
            ValueError: A source's ``sidecar_path`` is absent from ``sidecars``.
        """
        claimed: set[str] = {sidecar.casefold() for sidecar in self.sidecars}
        missing: list[str] = sorted({source.sidecar_path for source in self.sources if source.sidecar_path.casefold() not in claimed})
        if missing:
            message: str = f"sources name sidecars the manifest does not list: {missing}"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _no_two_sources_share_an_alias(self) -> Self:
        """Refuse two source entries under one alias.

        The receipt keys its source stamps by alias, so a duplicate would leave one source's T1
        and T2 unrecorded and its staleness permanently invisible.

        Returns:
            ``self``, unchanged.

        Raises:
            ValueError: One alias appears more than once.
        """
        aliases: list[str] = [source.source_alias for source in self.sources]
        repeated: list[str] = sorted({alias for alias in aliases if aliases.count(alias) > 1})
        if repeated:
            message: str = f"more than one source entry uses each of these aliases: {repeated}"
            raise ValueError(message)
        return self
