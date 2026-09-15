"""The corpus check: the JSON Schema and the Pydantic models must reach the same verdict.

Deliberately not a generated-and-diffed comparison of the two. That would test the
representation, and the two legitimately differ in spelling -- ``unevaluatedProperties`` against
``additionalProperties``, ``if``/``then`` against a ``model_validator``. What must agree is the
verdict on a document, which is what this checks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pqx_plan.config import ExportConfig
from pqx_plan.corpus import ALL_CASES, BASE, DIALECT, INVALID, SEMANTIC, VALID, Case, as_records, render_artifact
from pqx_plan.semantics import ConfigSemanticError, findings, validate_config
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Iterator

SCHEMA_PATH: Path = Path(__file__).resolve().parents[3] / "docs" / "export-config.schema.json"
EXAMPLE_PATH: Path = Path(__file__).resolve().parents[3] / "docs" / "export-config.example.json"
CORPUS_PATH: Path = Path(__file__).resolve().parents[3] / "docs" / "export-config-corpus.json"


@pytest.fixture(scope="session")
def validator() -> Draft202012Validator:
    """The documented schema, with format checking actually switched on.

    Without ``format_checker`` -- and without ``rfc3339-validator`` installed beside it --
    jsonschema treats ``format: date-time`` as an annotation and accepts ``"last Tuesday"`` for
    ``config_modified_utc``. Any consumer of this schema, the web editor included, needs the
    equivalent, or the schema is quietly weaker than it reads.
    """
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _schema_accepts(validator: Draft202012Validator, document: dict[str, Any]) -> bool:
    """Return whether the documented JSON Schema accepts a document.

    The ignore survives ``types-jsonschema`` being installed, and is not a substitute for it:
    mypy is satisfied by the stubs, and pyright still reports the member as partly unknown because
    the stub's *second* ``is_valid`` overload leaves ``instance`` unannotated. Both overloads
    return ``bool``, so the narrowing this suppresses is about the argument, not the result.
    """
    return validator.is_valid(document)  # pyright: ignore[reportUnknownMemberType]


def _models_accept(document: dict[str, Any]) -> bool:
    """Return whether the Pydantic models accept a document."""
    try:
        ExportConfig.model_validate(document)
    except ValidationError:
        return False
    return True


def _cases(source: list[Case]) -> Iterator[Any]:
    """Yield pytest parameters carrying each case's identifier as the test id."""
    case: Case
    for case in source:
        yield pytest.param(case, id=case.identifier)


@pytest.mark.parametrize("case", _cases([case for case in ALL_CASES if not case.dialect_divergence]))
def test_both_validators_reach_the_same_verdict(case: Case, validator: Draft202012Validator) -> None:
    schema_verdict: bool = _schema_accepts(validator, case.document)
    model_verdict: bool = _models_accept(case.document)
    assert schema_verdict == model_verdict, (
        f"{case.identifier}: the schema says {'valid' if schema_verdict else 'invalid'} and the models say {'valid' if model_verdict else 'invalid'}. One of them is wrong about: {case.reason}"
    )


@pytest.mark.parametrize("case", _cases([case for case in ALL_CASES if not case.dialect_divergence]))
def test_each_case_reaches_the_verdict_it_declares(case: Case, validator: Draft202012Validator) -> None:
    # Agreement alone would be satisfied by two validators that are both wrong in the same way.
    assert _schema_accepts(validator, case.document) is case.structurally_valid, case.reason


@pytest.mark.parametrize("case", _cases(DIALECT))
def test_this_project_takes_the_strict_reading_where_the_dialects_differ(case: Case) -> None:
    # The models agree with ECMA-262, which is what JSON Schema specifies and what a
    # browser-based editor runs. Each of these inputs would otherwise put a newline into a
    # worksheet name or a filename.
    assert case.structurally_valid is False
    assert not _models_accept(case.document), case.reason


@pytest.mark.parametrize("case", _cases(DIALECT))
def test_pythons_jsonschema_is_the_lax_side(case: Case, validator: Draft202012Validator) -> None:
    # Pinned rather than worked around. Python's jsonschema implements `pattern` with Python's
    # `re`, whose `$` also matches before a trailing newline. If a release ever fixes that, this
    # fails and the case graduates into INVALID.
    assert _schema_accepts(validator, case.document), f"{case.identifier}: Python's jsonschema now agrees with ECMA-262; move this case out of DIALECT"


@pytest.mark.parametrize("case", _cases(VALID))
def test_a_valid_case_also_coheres(case: Case) -> None:
    validate_config(ExportConfig.model_validate(case.document))


@pytest.mark.parametrize("case", _cases(SEMANTIC))
def test_a_semantic_case_passes_structure_and_fails_coherence(case: Case) -> None:
    # The class of rule JSON Schema cannot state at all, which is why the corpus keeps earning
    # its place after the structural cases agree.
    config: ExportConfig = ExportConfig.model_validate(case.document)
    found: tuple[str, ...] = findings(config)
    assert len(found) == case.findings, f"{case.identifier}: expected {case.findings} findings, got {found}"
    with pytest.raises(ConfigSemanticError):
        validate_config(config)


def test_the_corpus_has_no_duplicate_identifiers() -> None:
    identifiers: list[str] = [case.identifier for case in ALL_CASES]
    assert len(set(identifiers)) == len(identifiers)


def test_the_corpus_covers_both_verdicts_in_quantity() -> None:
    # A corpus of twenty valid documents and one invalid one would pass the agreement check and
    # prove almost nothing.
    assert len(VALID) >= 15
    assert len(INVALID) >= 30
    assert len(SEMANTIC) >= 10
    assert len(DIALECT) >= 3


def test_the_shipped_example_is_valid_on_both_sides(validator: Draft202012Validator) -> None:
    document: dict[str, Any] = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    assert _schema_accepts(validator, document)
    validate_config(ExportConfig.model_validate(document))


def test_the_base_document_is_the_example_shape(validator: Draft202012Validator) -> None:
    # Every case is one mutation away from this, so a broken base would make every case test
    # something other than what it says.
    assert _schema_accepts(validator, BASE)


def test_format_checking_is_actually_switched_on(validator: Draft202012Validator) -> None:
    # Guards the fixture's whole point. If rfc3339-validator is ever dropped, `format: date-time`
    # silently becomes an annotation and the naive_timestamp case starts reporting a divergence
    # that is really a missing dependency.
    document: dict[str, Any] = {**BASE, "config_modified_utc": "last Tuesday"}
    assert not _schema_accepts(validator, document)


def test_the_exported_artifact_matches_the_corpus() -> None:
    # The web editor validates the same documents in another language, so the corpus has to leave
    # this repository as data. If this fails, run: uv run python -m tools.build_config_corpus
    assert CORPUS_PATH.read_text(encoding="utf-8") == render_artifact(), "docs/export-config-corpus.json is stale; regenerate it with `uv run python -m tools.build_config_corpus`"


def test_every_exported_record_carries_its_verdict() -> None:
    records: list[dict[str, Any]] = as_records()
    assert len(records) == len(ALL_CASES)
    record: dict[str, Any]
    for record in records:
        assert set(record) == {"id", "reason", "structurally_valid", "semantic_only", "expected_findings", "dialect_divergence", "document"}
    assert json.dumps(records)


def test_a_configuration_round_trips_through_its_own_serialisation() -> None:
    # One field has an alias it cannot be spelled without -- `$schema` is not a Python identifier
    # -- and without `serialize_by_alias` a dump writes the field name, which validation then
    # refuses under extra="forbid". Found by embedding a configuration in a run manifest, which
    # dumps the whole thing in one call and cannot be asked to remember by_alias=True.
    config: ExportConfig = ExportConfig.model_validate({**BASE, "$schema": "./export-config.schema.json"})
    assert ExportConfig.model_validate_json(config.model_dump_json()) == config


def test_a_configuration_with_no_schema_pointer_round_trips_too() -> None:
    config: ExportConfig = ExportConfig.model_validate(BASE)
    assert ExportConfig.model_validate_json(config.model_dump_json()) == config


def test_a_serialised_configuration_is_one_the_schema_accepts(validator: Draft202012Validator) -> None:
    # The property that matters for a manifest: what a web editor reads back out of
    # `resolved_config` should be a configuration document it would accept.
    config: ExportConfig = ExportConfig.model_validate(BASE)
    assert _schema_accepts(validator, json.loads(config.model_dump_json()))


def test_an_absent_schema_pointer_is_omitted_rather_than_written_as_null() -> None:
    # The schema types the pointer as a string, so a null there would make a configuration fail
    # the very schema it names.
    dumped: dict[str, Any] = json.loads(ExportConfig.model_validate(BASE).model_dump_json())
    assert "$schema" not in dumped
    assert dumped["scratch_root"] is None, "an explicit null that must stay: `null` is written down rather than left out"
