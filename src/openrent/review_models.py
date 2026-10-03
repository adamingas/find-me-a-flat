"""Fixed assessment types; Python owns criterion severity and the final verdict."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

Outcome = bool | None
Certainty = Literal["high", "medium", "low", "unknown"]
Evidence = Annotated[
    str,
    Field(
        min_length=1,
        max_length=8000,
        description="Supporting listing facts, numbered images or web sources; explain unknowns.",
    ),
]


class CriterionResult(BaseModel):
    """An assessment whose nonbreaking severity cannot be supplied by the model."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    _breaking: ClassVar[bool] = False

    outcome: Annotated[
        Outcome,
        Field(
            description="True if this statement holds, false if it does not, or null if unknown."
        ),
    ]
    evidence: Evidence

    @computed_field
    @property
    def breaking(self) -> bool:
        return type(self)._breaking

    @field_validator("evidence")
    @classmethod
    def valid_evidence(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("evidence must contain nonblank text without null characters")
        return value


class BreakingCriterionResult(CriterionResult):
    """A required condition: an unmet result rejects the flat."""

    _breaking: ClassVar[bool] = True


class StatedNumericalCriterionResults(BaseModel):
    """A numerical value read explicitly; evidence identifies the number and its source."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    value: Annotated[float, Field(allow_inf_nan=False)]
    evidence: Evidence

    @field_validator("evidence")
    @classmethod
    def valid_evidence(cls, value: str) -> str:
        return CriterionResult.valid_evidence(value)


class EstimatedNumericalCriterionResult(BaseModel):
    """An inferred or calculated numerical value; evidence explains the estimation method."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    value: Annotated[float, Field(allow_inf_nan=False)]
    evidence: Evidence
    certainty: Annotated[
        Certainty,
        Field(description="Confidence in the estimated numerical value, based on its evidence."),
    ]

    @field_validator("evidence")
    @classmethod
    def valid_evidence(cls, value: str) -> str:
        return CriterionResult.valid_evidence(value)


class JudgementOutput(BaseModel):
    """This user's required assessments, with severity and verdict computed locally.

    The three breaking statements are phrased as requirements to avoid the
    undesirable conditions. Nonbreaking statements are assessed as written.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    no_living_room_carpet: Annotated[
        BreakingCriterionResult,
        Field(
            description="The living room has no carpet. Carpet in the living room is disqualifying."
        ),
    ]
    bathroom_without_window: Annotated[
        CriterionResult,
        Field(description="The bathroom has no window. Assess this statement as written."),
    ]
    kitchen_counter_space_for_four_appliances: Annotated[
        CriterionResult,
        Field(
            description=(
                "The kitchen has enough usable countertop space to hold four large appliances "
                "at once: a stand mixer, a pressure cooker and two coffee machines."
            )
        ),
    ]
    gas_hob_or_induction_stovetop: Annotated[
        CriterionResult,
        Field(
            description=(
                "The kitchen has either a gas hob or an induction stovetop. "
                "A non-induction electric stovetop does not satisfy this condition."
            )
        ),
    ]
    bedroom_carpet: Annotated[
        CriterionResult,
        Field(description="The bedroom has carpet. Assess this statement as written."),
    ]
    area_at_least_50_m2: Annotated[
        BreakingCriterionResult,
        Field(description="The flat's total internal floor area is at least 50 square metres."),
    ]
    primary_bedroom_fits_super_king_bed: Annotated[
        BreakingCriterionResult,
        Field(
            description=(
                "The primary bedroom can fit a super king bed measuring 1.8 m by 2.0 m. "
                "Use dimensions or defensible visual evidence; report null if insufficient."
            )
        ),
    ]
    not_ground_floor: Annotated[
        CriterionResult,
        Field(description="The flat is not on the ground floor."),
    ]
    area_m2: Annotated[
        StatedNumericalCriterionResults | EstimatedNumericalCriterionResult | None,
        Field(
            description=(
                "The flat's total internal floor area in square metres. Use "
                "StatedNumericalCriterionResults (value and evidence) when an explicit area "
                "number was read; EstimatedNumericalCriterionResult (value, evidence and "
                "certainty) when inferred from flooring, photographs, dimensions or a floor "
                "plan; or null when unknown."
            )
        ),
    ]
    floor: Annotated[
        StatedNumericalCriterionResults | EstimatedNumericalCriterionResult | None,
        Field(
            description=(
                "The floor on which the flat is located, using UK numbering: ground floor 0, "
                "first floor 1, and basement floors negative. Use StatedNumericalCriterionResults "
                "(value and evidence) when the floor is explicitly given; "
                "EstimatedNumericalCriterionResult (value, evidence and certainty) when inferred "
                "from the available evidence; or null when unknown or ambiguous."
            )
        ),
    ]
    summary: Annotated[str, Field(min_length=1, max_length=8000)]

    @field_validator("summary")
    @classmethod
    def valid_summary(cls, value: str) -> str:
        return CriterionResult.valid_evidence(value)

    @computed_field
    @property
    def decision(self) -> Literal["pass", "reject", "uncertain"]:
        outcomes = {
            value.outcome
            for name in type(self).model_fields
            if isinstance(value := getattr(self, name), CriterionResult) and value.breaking
        }
        if False in outcomes:
            return "reject"
        if None in outcomes:
            return "uncertain"
        return "pass"

    @property
    def result(self) -> dict:
        """The entire stored assessment, including local severity and verdict."""
        return self.model_dump(mode="json")
