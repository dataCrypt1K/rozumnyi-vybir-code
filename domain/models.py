from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import (
    BudgetLevel,
    ChannelType,
    DecisionSource,
    EvidenceSource,
    EvidenceType,
    ImplementationPhase,
    LocationKind,
    PropertyType,
    RecommendationStatus,
    ReliabilityLevel,
    ReviewStatus,
    RoomType,
    PhysicalDeviceType,
    TechnicalInputSource,
    ValveProfile,
    WorkflowStage,
)


def new_id() -> str:
    return uuid4().hex


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class Room(DomainModel):
    room_id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    room_type: RoomType
    location_kind: LocationKind = LocationKind.ROOM
    floor: int | None = Field(default=None, ge=-5, le=100)
    area_m2: float | None = Field(default=None, gt=0, le=10000)
    source: EvidenceSource = EvidenceSource.MANUAL
    reliability_level: ReliabilityLevel | None = None
    sort_order: int = Field(default=0, ge=0)


class DocumentRef(DomainModel):
    document_id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    original_name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(pattern=r"^(application/pdf|image/png|image/jpeg)$")
    stored_path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    page_count: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("original_name")
    @classmethod
    def original_name_must_not_contain_a_path(cls, value: str) -> str:
        if Path(value).name != value or "/" in value or "\\" in value:
            raise ValueError("original_name must be a file name, not a path")
        return value


class EvidenceFact(DomainModel):
    fact_id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    field_id: str = Field(min_length=1, max_length=160)
    entity_ref: str | None = Field(default=None, max_length=160)
    value: Any
    source: EvidenceSource
    evidence_type: EvidenceType
    source_reference: str | None = Field(default=None, max_length=500)
    document_id: str | None = Field(default=None, max_length=64)
    page_number: int | None = Field(default=None, ge=1)
    reliability_level: ReliabilityLevel
    review_status: ReviewStatus = ReviewStatus.PENDING
    corrected_value: Any | None = None

    @model_validator(mode="after")
    def validate_review_state(self) -> "EvidenceFact":
        if self.review_status == ReviewStatus.CORRECTED and self.corrected_value is None:
            raise ValueError("corrected_value is required when review_status is corrected")
        if self.source in {
            EvidenceSource.DOCUMENT_EXPLICIT,
            EvidenceSource.DOCUMENT_INFERRED,
        } and self.document_id is None:
            raise ValueError("document evidence must reference document_id")
        allowed_evidence_types = {
            EvidenceSource.MANUAL: {EvidenceType.USER_PROVIDED},
            EvidenceSource.QUESTIONNAIRE: {EvidenceType.QUESTIONNAIRE_ANSWER},
            EvidenceSource.DOCUMENT_EXPLICIT: {
                EvidenceType.EXPLICIT_TEXT,
                EvidenceType.EXPLICIT_SYMBOL,
            },
            EvidenceSource.DOCUMENT_INFERRED: {EvidenceType.VISUAL_INFERENCE},
        }
        if self.evidence_type not in allowed_evidence_types[self.source]:
            raise ValueError(
                f"evidence_type {self.evidence_type.value} is not valid for "
                f"source {self.source.value}"
            )
        return self

    @property
    def can_be_used(self) -> bool:
        if self.review_status == ReviewStatus.REJECTED:
            return False
        if self.source == EvidenceSource.DOCUMENT_INFERRED:
            return self.review_status in {ReviewStatus.CONFIRMED, ReviewStatus.CORRECTED}
        if self.source == EvidenceSource.DOCUMENT_EXPLICIT:
            return self.review_status in {ReviewStatus.CONFIRMED, ReviewStatus.CORRECTED}
        return True

    def value_for_use(self) -> Any:
        """Return a decision-safe value or fail closed for unreviewed document facts."""
        if not self.can_be_used:
            raise ValueError("evidence fact is not confirmed and cannot be used")
        if self.review_status == ReviewStatus.CORRECTED:
            return self.corrected_value
        return self.value


class RecommendationSnapshot(DomainModel):
    system_id: str = Field(min_length=1, max_length=120)
    status: RecommendationStatus
    suitability_percent: int | None = Field(default=None, ge=0, le=100)
    reason: str = Field(min_length=1, max_length=1000)
    rule_score: int | None = Field(default=None, ge=0, le=100)
    llm_score: int | None = Field(default=None, ge=0, le=100)
    decision_source: str = Field(min_length=1, max_length=80)
    evidence_question_ids: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    needs_review: bool = False

    @model_validator(mode="after")
    def validate_not_applicable_score(self) -> "RecommendationSnapshot":
        if self.status == RecommendationStatus.NOT_APPLICABLE:
            if self.suitability_percent is not None:
                raise ValueError(
                    "not_applicable recommendations must not have a percentage"
                )
        elif self.suitability_percent is None:
            raise ValueError("applicable recommendations require a percentage")
        return self


class ImplementationChoice(DomainModel):
    system_id: str = Field(min_length=1, max_length=120)
    recommendation_status: RecommendationStatus
    suitability_percent: int | None = Field(default=None, ge=0, le=100)
    selected: bool
    phase: ImplementationPhase | None = Field(
        default=None,
        description="Legacy/derived system phase; function choices are authoritative."
    )
    decision_source: DecisionSource = DecisionSource.SYSTEM_DEFAULT

    @model_validator(mode="after")
    def not_applicable_cannot_be_selected(self) -> "ImplementationChoice":
        if self.recommendation_status == RecommendationStatus.NOT_APPLICABLE:
            if self.selected:
                raise ValueError("not_applicable systems cannot be selected")
            if self.phase is not None:
                raise ValueError("not_applicable systems must not have a phase")
        return self


class FunctionRecommendation(DomainModel):
    function_id: str = Field(min_length=1, max_length=160)
    system_id: str = Field(min_length=1, max_length=120)
    room_id: str = Field(min_length=1, max_length=64)
    status: RecommendationStatus
    reason: str = Field(min_length=1, max_length=1000)
    recommended_phase: ImplementationPhase | None = None
    decision_source: str = Field(min_length=1, max_length=80)
    rule_codes: list[str] = Field(default_factory=list)
    needs_review: bool = False
    safety_criticality: int = Field(ge=0, le=2)
    retrofit_penalty: int = Field(ge=0, le=2)
    dependency_role: int = Field(ge=0, le=2)
    daily_impact: int = Field(ge=0, le=2)
    phase_rules_version: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_recommended_phase(self) -> "FunctionRecommendation":
        if self.status == RecommendationStatus.NOT_APPLICABLE:
            if self.recommended_phase is not None:
                raise ValueError("not_applicable functions must not have a phase")
        elif self.recommended_phase is None:
            raise ValueError("applicable functions require a recommended phase")
        return self


class FunctionChoice(DomainModel):
    function_id: str = Field(min_length=1, max_length=160)
    system_id: str = Field(min_length=1, max_length=120)
    room_id: str = Field(min_length=1, max_length=64)
    selected: bool
    selected_phase: ImplementationPhase | None = None
    decision_source: DecisionSource = DecisionSource.SYSTEM_DEFAULT
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_selected_phase(self) -> "FunctionChoice":
        if self.selected and self.selected_phase is None:
            raise ValueError("selected functions require selected_phase")
        if not self.selected and self.selected_phase is not None:
            raise ValueError("unselected functions must not have selected_phase")
        return self


class BudgetPreference(DomainModel):
    level: BudgetLevel
    amount_eur: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)


class TechnicalLocationInput(DomainModel):
    function_id: str = Field(min_length=1, max_length=160)
    room_id: str = Field(min_length=1, max_length=64)
    source: TechnicalInputSource = TechnicalInputSource.USER_CORRECTION
    confidence: ReliabilityLevel = ReliabilityLevel.HIGH
    confirmed_by_user: bool = False
    floor_zone_count: int | None = Field(default=None, ge=0, le=10000)
    floor_temperature_sensor_count: int | None = Field(default=None, ge=0, le=10000)
    relay_group_count: int | None = Field(default=None, ge=0, le=10000)
    triac_group_count: int | None = Field(default=None, ge=0, le=10000)
    pwm_group_count: int | None = Field(default=None, ge=0, le=10000)
    dali_group_count: int | None = Field(default=None, ge=0, le=10000)
    dali_address_count: int | None = Field(default=None, ge=0, le=10000)
    leak_sensor_count: int | None = Field(default=None, ge=0, le=10000)
    valve_count: int | None = Field(default=None, ge=0, le=10000)
    valve_profile: ValveProfile | None = None
    fan_count: int | None = Field(default=None, ge=0, le=10000)
    fan_group_count: int | None = Field(default=None, ge=0, le=10000)


class ChannelContribution(DomainModel):
    function_id: str = Field(min_length=1, max_length=160)
    room_id: str = Field(min_length=1, max_length=64)
    input_field: str = Field(min_length=1, max_length=120)
    input_value: int = Field(ge=0)
    channel_type: ChannelType
    base_units: int = Field(ge=0)


class ChannelRequirement(DomainModel):
    channel_type: ChannelType
    base: int = Field(ge=0)
    reserve: int = Field(ge=0)
    required_total: int = Field(ge=0)

    @model_validator(mode="after")
    def total_must_equal_base_plus_reserve(self) -> "ChannelRequirement":
        if self.required_total != self.base + self.reserve:
            raise ValueError("required_total must equal base plus reserve")
        if self.channel_type == ChannelType.DALI_ADDRESS and self.reserve != 0:
            raise ValueError("DALI_ADDRESS does not receive channel reserve")
        return self


class PhysicalDeviceRequirement(DomainModel):
    device_type: PhysicalDeviceType
    count: int = Field(ge=0)


class TechnicalCalculation(DomainModel):
    rules_version: str = Field(min_length=1, max_length=80)
    reserve_ratio: Decimal = Field(ge=0, le=1, decimal_places=4)
    inputs: list[TechnicalLocationInput] = Field(default_factory=list)
    contributions: list[ChannelContribution] = Field(default_factory=list)
    channel_requirements: list[ChannelRequirement] = Field(default_factory=list)
    physical_devices: list[PhysicalDeviceRequirement] = Field(default_factory=list)
    needs_review: bool = False
    blocking_missing_data: list[str] = Field(default_factory=list)

    @field_validator("inputs")
    @classmethod
    def input_locations_must_be_unique(
        cls, value: list[TechnicalLocationInput]
    ) -> list[TechnicalLocationInput]:
        keys = [(item.function_id, item.room_id) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("technical input function_id and room_id pairs must be unique")
        return value

    @field_validator("channel_requirements")
    @classmethod
    def channel_types_must_be_unique(
        cls, value: list[ChannelRequirement]
    ) -> list[ChannelRequirement]:
        channel_types = [item.channel_type for item in value]
        if len(channel_types) != len(set(channel_types)):
            raise ValueError("technical channel requirement types must be unique")
        return value

    @model_validator(mode="after")
    def review_state_matches_missing_data(self) -> "TechnicalCalculation":
        if self.needs_review != bool(self.blocking_missing_data):
            raise ValueError("needs_review must match blocking_missing_data")
        return self


class Assessment(DomainModel):
    assessment_id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    project_name: str | None = Field(default=None, min_length=1, max_length=160)
    derived_from_assessment_id: str | None = Field(default=None, max_length=64)
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    workflow_stage: WorkflowStage = WorkflowStage.DRAFT
    property_type: PropertyType | None = None
    area_m2: float | None = Field(default=None, gt=0, le=10000)
    floor_count: int | None = Field(default=None, ge=1, le=100)
    provider: str | None = Field(default=None, max_length=80)
    model_name: str | None = Field(default=None, max_length=160)
    config_versions: dict[str, Any] = Field(default_factory=dict)
    answers: dict[str, Any] = Field(default_factory=dict)
    recommendations: list[RecommendationSnapshot] = Field(default_factory=list)
    rooms: list[Room] = Field(default_factory=list)
    implementation_choices: list[ImplementationChoice] = Field(default_factory=list)
    function_recommendations: list[FunctionRecommendation] = Field(default_factory=list)
    function_choices: list[FunctionChoice] = Field(default_factory=list)
    budget: BudgetPreference | None = None
    technical_calculation: TechnicalCalculation | None = None
    documents: list[DocumentRef] = Field(default_factory=list)
    evidence_facts: list[EvidenceFact] = Field(default_factory=list)
    raw_payload: dict[str, Any] | None = None

    @field_validator("recommendations")
    @classmethod
    def recommendations_must_be_unique(
        cls, value: list[RecommendationSnapshot]
    ) -> list[RecommendationSnapshot]:
        ids = [item.system_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("recommendation system_id values must be unique")
        return value

    @field_validator("rooms")
    @classmethod
    def rooms_must_be_unique(cls, value: list[Room]) -> list[Room]:
        ids = [item.room_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("room_id values must be unique")
        sort_orders = [item.sort_order for item in value]
        if len(sort_orders) != len(set(sort_orders)):
            raise ValueError("room sort_order values must be unique")
        return value

    @field_validator("implementation_choices")
    @classmethod
    def choices_must_be_unique(
        cls, value: list[ImplementationChoice]
    ) -> list[ImplementationChoice]:
        ids = [item.system_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("implementation choice system_id values must be unique")
        return value

    @field_validator("function_recommendations")
    @classmethod
    def function_recommendations_must_be_unique(
        cls, value: list[FunctionRecommendation]
    ) -> list[FunctionRecommendation]:
        keys = [(item.function_id, item.room_id) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("function_id and room_id pairs must be unique")
        return value

    @field_validator("function_choices")
    @classmethod
    def function_choices_must_be_unique(
        cls, value: list[FunctionChoice]
    ) -> list[FunctionChoice]:
        keys = [(item.function_id, item.room_id) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("function choice function_id and room_id pairs must be unique")
        return value

    @model_validator(mode="after")
    def choices_must_reference_recommendations(self) -> "Assessment":
        if self.workflow_stage != WorkflowStage.DRAFT:
            expected_count = self.config_versions.get(
                "recommendation_catalog_size", 19
            )
            if (
                isinstance(expected_count, bool)
                or not isinstance(expected_count, int)
                or expected_count < 1
            ):
                raise ValueError(
                    "config_versions.recommendation_catalog_size must be a positive integer"
                )
            if (
                expected_count != 19
                and self.config_versions.get("catalog_scope") != "legacy_snapshot"
            ):
                raise ValueError(
                    "partial recommendation catalogs are allowed only for legacy snapshots"
                )
            if len(self.recommendations) != expected_count:
                raise ValueError(
                    "completed recommendation snapshots must contain the full catalog: "
                    f"expected {expected_count}, got {len(self.recommendations)}"
                )

        recommendation_map = {item.system_id: item for item in self.recommendations}
        recommendation_ids = set(recommendation_map)
        unknown = {
            item.system_id
            for item in self.implementation_choices
            if item.system_id not in recommendation_ids
        }
        if unknown:
            raise ValueError(
                "implementation choices reference unknown recommendations: "
                + ", ".join(sorted(unknown))
            )
        for item in self.implementation_choices:
            recommendation = recommendation_map[item.system_id]
            if item.recommendation_status != recommendation.status:
                raise ValueError(
                    f"implementation choice status does not match {item.system_id} recommendation"
                )
            if item.suitability_percent != recommendation.suitability_percent:
                raise ValueError(
                    f"implementation choice percentage does not match {item.system_id} recommendation"
                )

        room_ids = {item.room_id for item in self.rooms}
        system_ids = {item.system_id for item in self.recommendations}
        function_map = {
            (item.function_id, item.room_id): item
            for item in self.function_recommendations
        }
        for item in self.function_recommendations:
            if item.room_id not in room_ids:
                raise ValueError(
                    f"function recommendation references unknown room_id: {item.room_id}"
                )
            if item.system_id not in system_ids:
                raise ValueError(
                    f"function recommendation references unknown system_id: {item.system_id}"
                )
            system_recommendation = recommendation_map[item.system_id]
            if (
                system_recommendation.status == RecommendationStatus.NOT_APPLICABLE
                and item.status != RecommendationStatus.NOT_APPLICABLE
            ):
                raise ValueError(
                    "functions of a not_applicable system must also be not_applicable"
                )
        configured_phase_rules = self.config_versions.get("phase_rules")
        if configured_phase_rules is not None and any(
            item.phase_rules_version != configured_phase_rules
            for item in self.function_recommendations
        ):
            raise ValueError(
                "function recommendation phase_rules_version does not match config_versions"
            )
        for item in self.function_choices:
            recommendation = function_map.get((item.function_id, item.room_id))
            if recommendation is None:
                raise ValueError(
                    "function choice must reference an existing function recommendation"
                )
            if recommendation.system_id != item.system_id:
                raise ValueError("function choice system_id does not match recommendation")
            if recommendation.status == RecommendationStatus.NOT_APPLICABLE and item.selected:
                raise ValueError("not_applicable functions cannot be selected")

        phase_order = {
            ImplementationPhase.FIRST: 0,
            ImplementationPhase.SECOND: 1,
            ImplementationPhase.LATER: 2,
        }
        choices_by_system: dict[str, list[FunctionChoice]] = {}
        for item in self.function_choices:
            if item.selected:
                choices_by_system.setdefault(item.system_id, []).append(item)
        for system_choice in self.implementation_choices:
            selected_functions = choices_by_system.get(system_choice.system_id, [])
            if selected_functions and not system_choice.selected:
                raise ValueError(
                    "a system with selected functions must also be selected"
                )
            if selected_functions and system_choice.phase is not None:
                derived_phase = min(
                    (item.selected_phase for item in selected_functions),
                    key=lambda phase: phase_order[phase],
                )
                if system_choice.phase != derived_phase:
                    raise ValueError(
                        "legacy system phase must equal the earliest selected function phase"
                    )
        return self
