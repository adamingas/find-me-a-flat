import asyncio
import json
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, ValidationError

import openrent.judge as judge_module
from openrent.backends import BackendConfig, BackendError, parse_output, strict_schema
from openrent.evidence import ReviewSnapshot
from openrent.judge import JudgementOutput, judge_property
from openrent.review_models import (
    BreakingCriterionResult,
    CriterionResult,
    EstimatedNumericalCriterionResult,
    StatedNumericalCriterionResults,
)

EXPECTED_SEVERITY = {
    "no_living_room_carpet": True,
    "bathroom_without_window": False,
    "kitchen_counter_space_for_four_appliances": False,
    "gas_hob_or_induction_stovetop": False,
    "bedroom_carpet": False,
    "area_at_least_50_m2": True,
    "primary_bedroom_fits_super_king_bed": True,
    "not_ground_floor": False,
}


def assessment(outcomes=None):
    fields = EXPECTED_SEVERITY
    values = [True] * len(fields) if outcomes is None else outcomes
    return {
        "summary": "Suitable according to the supplied conditions.",
        "area_m2": None,
        "floor": None,
        **{
            name: {"outcome": outcome, "evidence": "Photo 1."}
            for name, outcome in zip(fields, values, strict=True)
        },
    }


def test_review_facade_supplies_same_schema_instructions_and_full_evidence_to_both_backends(
    monkeypatch,
):
    calls = []
    data = {
        "id": 101,
        "title": "Bright flat on Example Street",
        "url": "https://www.openrent.co.uk/property-to-rent/london/101",
        "description": "A complete description with space for a desk.",
        "rent_pcm_pence": 190_025,
        "bedrooms": 2,
        "extra_metadata": {"size": 44},
    }
    original = deepcopy(data)
    images = [
        SimpleNamespace(content=b"image-one", content_type="image/png", kind="photo"),
        SimpleNamespace(content=b"image-two", content_type="image/jpeg", kind="floorplan"),
    ]

    def factory(name, config):
        assert config.model == "chosen-model" and config.schema is JudgementOutput
        assert "Only bright flats." in config.instructions
        assert config.timeout == 12

        async def run(supplied_data, supplied_images):
            assert [image.content for image in supplied_images] == [b"image-one", b"image-two"]
            assert data["title"] in supplied_data and data["description"] in supplied_data
            assert data["url"] in supplied_data
            calls.append((name, supplied_data, config.instructions, strict_schema(config.schema)))
            return JudgementOutput.model_validate(assessment())

        return SimpleNamespace(run=run)

    monkeypatch.setattr(judge_module, "create_backend", factory)
    for name in ("codex", "responses"):
        result = asyncio.run(
            judge_property(
                ReviewSnapshot(101, "fingerprint", data, images),
                "Only bright flats.",
                model="chosen-model",
                backend=name,
                timeout=12,
            )
        )
        assert result.decision == "pass"
    assert [call[0] for call in calls] == ["codex", "responses"]
    assert calls[0][1:] == calls[1][1:]
    assert data == original and [image.content for image in images] == [b"image-one", b"image-two"]


def test_fixed_response_contract_requires_boolean_criteria_and_rejects_injected_fields():
    fields = EXPECTED_SEVERITY
    first = next(iter(fields))
    invalid = [
        assessment() | {"summary": " "},
        assessment() | {"decision": "pass"},
        assessment() | {first: {"outcome": True, "evidence": "Photo 1.", "breaking": False}},
        assessment() | {first: {"outcome": True, "evidence": " "}},
        assessment() | {"unexpected": True},
    ]
    for value in (1, 0, "true", "false"):
        invalid.append(assessment() | {first: {"outcome": value, "evidence": "Photo 1."}})
    for name in fields | {"area_m2": None, "floor": None, "summary": None}:
        missing = assessment()
        del missing[name]
        invalid.append(missing)
    for item in invalid:
        with pytest.raises(BackendError):
            parse_output(json.dumps(item), JudgementOutput)
    for raw in ['{"summary": NaN}', '{"summary":"x","summary":"y"}', "```json\n{}\n```"]:
        with pytest.raises(BackendError):
            parse_output(raw, JudgementOutput)


def test_fixed_criterion_types_own_severity_and_numeric_certainty():
    for subtype, flag in ((CriterionResult, False), (BreakingCriterionResult, True)):
        result = subtype(outcome=True, evidence="Photo 1.")
        assert result.breaking is flag and result.model_dump()["breaking"] is flag
        with pytest.raises(ValidationError):
            subtype(outcome=True, evidence="Photo 1.", breaking=not flag)
    branches = (
        (StatedNumericalCriterionResults, {"value": 45.5, "evidence": "Stated in the listing."}),
        (
            EstimatedNumericalCriterionResult,
            {"value": 44.5, "certainty": "medium", "evidence": "Estimated from the floorplan."},
        ),
        (type(None), None),
    )
    for name in ("area_m2", "floor"):
        for expected_type, value in branches:
            payload = assessment() | {name: value}
            result = parse_output(json.dumps(payload), JudgementOutput)
            assert type(getattr(result, name)) is expected_type
            assert result.model_dump(mode="json")[name] == value

    for value in (-1.0, 0.0, 45.5):
        stated = StatedNumericalCriterionResults(value=value, evidence="Source figure.")
        assert stated.value == value and set(stated.model_dump()) == {"value", "evidence"}
        for certainty in ("high", "medium", "low", "unknown"):
            estimated = EstimatedNumericalCriterionResult(
                value=value, certainty=certainty, evidence="Estimated figure."
            )
            assert estimated.value == value and estimated.certainty == certainty
            assert set(estimated.model_dump()) == {"value", "certainty", "evidence"}
    with pytest.raises(ValidationError):
        EstimatedNumericalCriterionResult(value=1.0, evidence="Missing certainty.")
    with pytest.raises(ValidationError):
        StatedNumericalCriterionResults(value=1.0, evidence="Stated.", certainty="high")
    for subtype in (StatedNumericalCriterionResults, EstimatedNumericalCriterionResult):
        extra = {"certainty": "low"} if subtype is EstimatedNumericalCriterionResult else {}
        for value in (True, "45.5", float("inf"), float("nan"), None):
            with pytest.raises(ValidationError):
                subtype(value=value, evidence="Invalid numeric value.", **extra)
        with pytest.raises(ValidationError):
            subtype(value=1.0, evidence="No injected severity.", breaking=False, **extra)


@st.composite
def numerical_results(draw):
    kind = draw(st.sampled_from(["unknown", "stated", "estimated"]))
    if kind == "unknown":
        return None
    result = {
        "value": draw(st.floats(allow_nan=False, allow_infinity=False)),
        "evidence": "Original source figure or estimate.",
    }
    if kind == "estimated":
        result["certainty"] = draw(st.sampled_from(["high", "medium", "low", "unknown"]))
    return result


@given(
    outcomes=st.lists(st.sampled_from([True, False, None]), min_size=8, max_size=8),
    area=numerical_results(),
    floor=numerical_results(),
)
def test_verdict_depends_only_on_breaking_criteria(outcomes, area, floor):
    fields = EXPECTED_SEVERITY
    breaking = [name for name, required in fields.items() if required]
    optional = [name for name, required in fields.items() if not required]
    payload = assessment(outcomes)
    payload["area_m2"] = area
    payload["floor"] = floor
    required_outcomes = {payload[name]["outcome"] for name in breaking}
    expected = (
        "reject"
        if False in required_outcomes
        else "uncertain"
        if None in required_outcomes
        else "pass"
    )
    result = parse_output(json.dumps(payload), JudgementOutput)
    assert result.decision == expected
    stored = result.model_dump(mode="json")
    assert stored["area_m2"] == area and stored["floor"] == floor
    assert stored["decision"] == expected
    assert all(stored[name]["breaking"] is (name in breaking) for name in fields)

    changed = deepcopy(payload)
    for name in optional:
        changed[name]["outcome"] = {True: False, False: None, None: True}[changed[name]["outcome"]]
    changed["area_m2"] = {
        "value": 44.5,
        "evidence": "Stated floor area.",
    }
    changed["floor"] = {"value": -1.0, "certainty": "low", "evidence": "Estimated basement level."}
    assert parse_output(json.dumps(changed), JudgementOutput).decision == expected


def test_configurable_schema_preserves_json_date_uuid_types_and_rejects_bad_contracts():
    class Result(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        available: date
        identifier: UUID
        score: float

    raw = (
        '{"available":"2026-10-03","identifier":"12345678-1234-5678-1234-567812345678","score":1.5}'
    )
    parsed = parse_output(raw, Result)
    assert parsed.available == date(2026, 10, 3)
    assert parsed.identifier == UUID("12345678-1234-5678-1234-567812345678")
    config = BackendConfig("model-name", Result, "Instruction string")
    assert config.schema is Result and strict_schema(Result)["additionalProperties"] is False
    for replacement in ("NaN", "Infinity", "1e999"):
        with pytest.raises(BackendError):
            parse_output(raw.replace("1.5", replacement), Result)
    with pytest.raises(BackendError):
        parse_output(raw.replace('"score":1.5', '"score":1.5,"score":2.0'), Result)
