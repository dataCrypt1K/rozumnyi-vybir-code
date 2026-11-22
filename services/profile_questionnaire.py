from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUESTIONNAIRE_PATH = ROOT / "config" / "behavior_questionnaire_v2.yaml"


class BehaviorQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^q_[a-z0-9_]+$")
    section: str
    text: str = Field(min_length=1)
    type: str = Field(pattern=r"^single_choice$")
    required: bool
    options: list[str] = Field(min_length=2)
    scores: dict[str, float]
    factor_weights: dict[str, float] | None = None
    scope_weight: float | None = Field(default=None, gt=0)
    constraint_only: bool = False

    @field_validator("scores")
    @classmethod
    def scores_must_be_normalized(cls, value: dict[str, float]) -> dict[str, float]:
        if any(score < 0 or score > 1 for score in value.values()):
            raise ValueError("question scores must be in range 0..1")
        return value


class BehaviorQuestionnaire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    questionnaire_id: str
    factors: list[str]
    questions: list[BehaviorQuestion]

    @field_validator("questions")
    @classmethod
    def questions_must_be_consistent(
        cls, value: list[BehaviorQuestion]
    ) -> list[BehaviorQuestion]:
        ids = [item.id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("behavior question ids must be unique")
        for question in value:
            if set(question.options) != set(question.scores):
                raise ValueError(f"{question.id}: every option needs exactly one score")
            scoring_modes = sum(
                (
                    bool(question.factor_weights),
                    bool(question.scope_weight),
                    question.constraint_only,
                )
            )
            if scoring_modes != 1:
                raise ValueError(
                    f"{question.id}: define exactly one of factor_weights, "
                    "scope_weight or constraint_only"
                )
        return value


class FactorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=100)
    confidence_percent: int = Field(ge=0, le=100)
    evidence_question_ids: list[str]


class BehaviorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questionnaire_id: str
    factors: dict[str, FactorResult]
    scope_intensity: int = Field(ge=0, le=100)
    scope_level: str = Field(pattern=r"^(conservative|balanced|expanded)$")
    completeness_percent: int = Field(ge=0, le=100)
    legacy_signals: dict[str, Any]


def load_behavior_questionnaire(
    path: Path = DEFAULT_QUESTIONNAIRE_PATH,
) -> BehaviorQuestionnaire:
    return BehaviorQuestionnaire.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )


def validate_behavior_answers(
    questionnaire: BehaviorQuestionnaire, answers: dict[str, Any]
) -> list[str]:
    question_map = {item.id: item for item in questionnaire.questions}
    unknown = set(answers) - set(question_map)
    if unknown:
        raise ValueError(f"unknown behavior answers: {sorted(unknown)}")
    missing = []
    for question in questionnaire.questions:
        if question.required and question.id not in answers:
            missing.append(question.id)
            continue
        if question.id in answers and answers[question.id] not in question.options:
            raise ValueError(
                f"{question.id}: unsupported option {answers[question.id]!r}"
            )
    return missing


def _scale_to_five(score: float) -> int:
    return max(1, min(5, round(1 + 4 * score)))


def _question_score(
    question_map: dict[str, BehaviorQuestion], answers: dict[str, Any], question_id: str
) -> float:
    question = question_map[question_id]
    value = answers.get(question_id)
    return question.scores[value] if value in question.scores else 0.0


def build_legacy_signals(
    questionnaire: BehaviorQuestionnaire,
    answers: dict[str, Any],
    factor_scores: dict[str, int],
) -> dict[str, Any]:
    question_map = {item.id: item for item in questionnaire.questions}

    def q(question_id: str) -> float:
        return _question_score(question_map, answers, question_id)

    def level(value: float) -> int:
        return _scale_to_five(value)

    factors = {key: value / 100 for key, value in factor_scores.items()}
    security = max(factors["security_need"], factors["risk_vigilance"])
    lighting_scenes = max(q("q_light_adjustment"), q("q_repetitive_actions"), q("q_hosting"))
    visual_need = {
        "sensor_notification": 0.0,
        "entrance_visitor": 0.35,
        "property_approaches": 0.9,
        "selected_zones": 1.0,
    }.get(answers.get("q_visual_verification"), 0.0)
    outdoor_privacy_limit = {
        "nowhere": 0.0,
        "entrance_only": 0.45,
        "exterior": 1.0,
        "limited_indoor": 0.85,
    }.get(answers.get("q_camera_privacy"), 0.0)
    indoor_visual_need = {
        "sensor_notification": 0.0,
        "entrance_visitor": 0.0,
        "property_approaches": 0.0,
        "selected_zones": 0.9,
    }.get(answers.get("q_visual_verification"), 0.0)
    indoor_privacy_limit = {
        "nowhere": 0.0,
        "entrance_only": 0.0,
        "exterior": 0.0,
        "limited_indoor": 1.0,
    }.get(answers.get("q_camera_privacy"), 0.0)
    return {
        "absence_frequency": answers.get("q_home_unattended", "rarely"),
        "comfort_priority": level(factors["thermal_light_comfort"]),
        "energy_priority": level(factors["energy_awareness"]),
        "safety_priority": level(max(factors["risk_vigilance"], factors["continuity_resilience"])),
        "remote_control_interest": level(factors["remote_oversight"]),
        "automation_interest": level(factors["automation_delegation"]),
        "precondition_home": level(q("q_return_temperature")),
        "zoned_temperature": level(q("q_temperature_differences")),
        "occupancy_based_climate": level(
            (factors["automation_delegation"] + factors["energy_awareness"]) / 2
        ),
        "dimming_interest": level(q("q_light_adjustment")),
        "color_temperature_interest": level(q("q_light_adjustment") * 0.75),
        "rgb_interest": level(q("q_hosting") * 0.35),
        "presence_lighting_interest": level(q("q_night_routes")),
        "lighting_scenes_interest": level(lighting_scenes),
        "previous_leak": answers.get("q_water_incident") in {"minor", "serious"},
        "automatic_shutoff_interest": level(
            max(q("q_water_incident"), factors["risk_vigilance"])
        ),
        "air_quality_interest": level(q("q_air_symptoms")),
        "shading_automation_interest": level(q("q_sun_discomfort")),
        "sun_overheating_problem": q("q_sun_discomfort") >= 0.67,
        "intrusion_priority": level(security),
        "opening_monitoring_interest": level(q("q_entry_uncertainty")),
        "siren_interest": level(security),
        "indoor_camera_interest": level(
            min(indoor_visual_need, indoor_privacy_limit)
        ),
        "outdoor_camera_interest": level(
            min(visual_need, outdoor_privacy_limit)
        ),
        "visitor_access_interest": level(q("q_visitors_absent")),
        "keyless_access_interest": level(
            (q("q_visitors_absent") + q("q_entry_uncertainty")) / 2
        ),
        "lock_tamper_interest": level(q("q_entry_uncertainty")),
        "perimeter_detection_interest": level(security),
        "emergency_notifications_interest": level(
            max(factors["risk_vigilance"], factors["remote_oversight"])
        ),
        "autonomy_priority": level(factors["continuity_resilience"]),
        "energy_monitoring_interest": level(factors["energy_awareness"]),
        "load_shedding_interest": level(
            (factors["continuity_resilience"] + factors["automation_delegation"]) / 2
        ),
        "multimedia_priority": level(factors["media_social_use"]),
        "home_cinema_interest": level(q("q_cinema_use")),
        "multiroom_audio_interest": level(q("q_music_rooms")),
        "voice_control_interest": level(factors["automation_delegation"] * 0.6),
        "weather_automation_interest": level(
            (q("q_sun_discomfort") + factors["automation_delegation"]) / 2
        ),
    }


def calculate_behavior_profile(
    questionnaire: BehaviorQuestionnaire,
    answers: dict[str, Any],
) -> BehaviorProfile:
    missing = validate_behavior_answers(questionnaire, answers)
    question_map = {item.id: item for item in questionnaire.questions}
    weighted_values = {factor: 0.0 for factor in questionnaire.factors}
    answered_weights = {factor: 0.0 for factor in questionnaire.factors}
    possible_weights = {factor: 0.0 for factor in questionnaire.factors}
    evidence = {factor: [] for factor in questionnaire.factors}
    scope_value = 0.0
    scope_answered_weight = 0.0
    scope_possible_weight = 0.0

    for question in questionnaire.questions:
        if question.factor_weights:
            for factor, weight in question.factor_weights.items():
                if factor not in weighted_values:
                    raise ValueError(f"{question.id}: unknown factor {factor}")
                possible_weights[factor] += weight
                if question.id in answers:
                    weighted_values[factor] += question.scores[answers[question.id]] * weight
                    answered_weights[factor] += weight
                    evidence[factor].append(question.id)
        elif question.scope_weight is not None:
            scope_possible_weight += question.scope_weight
            if question.id in answers:
                scope_value += question.scores[answers[question.id]] * question.scope_weight
                scope_answered_weight += question.scope_weight

    results = {}
    raw_factor_scores = {}
    for factor in questionnaire.factors:
        answered_weight = answered_weights[factor]
        score = round(100 * weighted_values[factor] / answered_weight) if answered_weight else 0
        confidence = (
            round(100 * answered_weight / possible_weights[factor])
            if possible_weights[factor]
            else 0
        )
        raw_factor_scores[factor] = score
        results[factor] = FactorResult(
            score=score,
            confidence_percent=confidence,
            evidence_question_ids=evidence[factor],
        )

    scope_intensity = (
        round(100 * scope_value / scope_answered_weight)
        if scope_answered_weight
        else 0
    )
    scope_level = (
        "conservative"
        if scope_intensity < 40
        else "balanced"
        if scope_intensity < 70
        else "expanded"
    )
    answered_required = len(questionnaire.questions) - len(missing)
    completeness = round(100 * answered_required / len(questionnaire.questions))
    return BehaviorProfile(
        questionnaire_id=questionnaire.questionnaire_id,
        factors=results,
        scope_intensity=scope_intensity,
        scope_level=scope_level,
        completeness_percent=completeness,
        legacy_signals=build_legacy_signals(
            questionnaire, answers, raw_factor_scores
        ),
    )
