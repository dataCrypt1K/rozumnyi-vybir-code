from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evaluate_rules import matches
from services.profile_questionnaire import BehaviorProfile, BehaviorQuestionnaire


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_PATH = ROOT / "config" / "system_need_model_v2.yaml"

FACTOR_LABELS_UK = {
    "risk_vigilance": "увага до побутових ризиків",
    "remote_oversight": "потреба дистанційно контролювати житло",
    "automation_delegation": "готовність передавати повторювані дії автоматизації",
    "routine_variability": "змінний режим перебування вдома",
    "thermal_light_comfort": "потреба у стабільному тепловому та світловому комфорті",
    "health_air_quality": "увага до якості повітря",
    "security_need": "потреба у контролі безпеки",
    "continuity_resilience": "чутливість до перерв електроживлення",
    "energy_awareness": "увага до енергоспоживання",
    "maintenance_avoidance": "бажання зменшити ручне обслуговування",
    "media_social_use": "регулярне використання музики, кіно та гостьових сценаріїв",
}


class ObjectSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    operator: str = Field(pattern=r"^(equals|contains|not_contains|gte|lte|in)$")
    value: Any
    score: int = Field(ge=0, le=100)
    weight: float = Field(gt=0)
    reason_uk: str = Field(min_length=1)


class AnswerSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    operator: str = Field(pattern=r"^(equals|in)$")
    value: Any
    score: int = Field(ge=0, le=100)
    weight: float = Field(gt=0)
    reason_uk: str = Field(min_length=1)


class StatusRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    when: dict[str, Any]
    status: str = Field(pattern=r"^(recommended|optional|inexpedient)$")
    score: int = Field(ge=0, le=100)
    reason_uk: str = Field(min_length=1)


class SystemNeedDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_id: str
    ignored_hard_rule_ids: list[str] = Field(default_factory=list)
    factor_weights: dict[str, float]
    question_weights: dict[str, float]
    object_signals: list[ObjectSignal]
    answer_signals: list[AnswerSignal] = Field(default_factory=list)
    status_rules: list[StatusRule] = Field(default_factory=list)

    @field_validator("factor_weights", "question_weights")
    @classmethod
    def weights_must_be_positive(cls, value: dict[str, float]) -> dict[str, float]:
        if not value or any(weight <= 0 for weight in value.values()):
            raise ValueError("need-model weights must be positive and non-empty")
        return value


class SystemNeedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    model_id: str
    status_thresholds: dict[str, int]
    systems: list[SystemNeedDefinition]

    @field_validator("systems")
    @classmethod
    def systems_must_be_unique(
        cls, value: list[SystemNeedDefinition]
    ) -> list[SystemNeedDefinition]:
        ids = [item.system_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("system need definitions must be unique")
        return value


def load_system_need_model(
    path: Path = DEFAULT_MODEL_PATH,
) -> SystemNeedModel:
    model = SystemNeedModel.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )
    thresholds = model.status_thresholds
    if set(thresholds) != {"recommended", "optional"}:
        raise ValueError("need model must define recommended and optional thresholds")
    if not 0 <= thresholds["optional"] < thresholds["recommended"] <= 100:
        raise ValueError("need-model thresholds are invalid")
    return model


def validate_system_need_model(
    model: SystemNeedModel,
    questionnaire: BehaviorQuestionnaire,
    system_ids: set[str],
) -> None:
    configured_ids = {item.system_id for item in model.systems}
    if configured_ids != system_ids:
        raise ValueError(
            "system need model does not match the active catalog: "
            f"missing={sorted(system_ids - configured_ids)}, "
            f"extra={sorted(configured_ids - system_ids)}"
        )
    factors = set(questionnaire.factors)
    question_ids = {item.id for item in questionnaire.questions}
    for definition in model.systems:
        if len(definition.ignored_hard_rule_ids) != len(
            set(definition.ignored_hard_rule_ids)
        ):
            raise ValueError(
                f"{definition.system_id}: ignored hard-rule ids must be unique"
            )
        status_rule_ids = [item.rule_id for item in definition.status_rules]
        if len(status_rule_ids) != len(set(status_rule_ids)):
            raise ValueError(
                f"{definition.system_id}: status-rule ids must be unique"
            )
        for rule in definition.status_rules:
            if _status(rule.score, model.status_thresholds) != rule.status:
                raise ValueError(
                    f"{definition.system_id}: status rule {rule.rule_id} has "
                    "a score outside its configured status band"
                )
        unknown_factors = set(definition.factor_weights) - factors
        unknown_questions = set(definition.question_weights) - question_ids
        unknown_answer_signals = {
            item.field_id for item in definition.answer_signals
        } - question_ids
        if unknown_factors:
            raise ValueError(
                f"{definition.system_id}: unknown factors {sorted(unknown_factors)}"
            )
        if unknown_questions:
            raise ValueError(
                f"{definition.system_id}: unknown questions {sorted(unknown_questions)}"
            )
        if unknown_answer_signals:
            raise ValueError(
                f"{definition.system_id}: unknown answer-signal questions "
                f"{sorted(unknown_answer_signals)}"
            )
        question_map = {item.id: item for item in questionnaire.questions}
        for signal in definition.answer_signals:
            options = set(question_map[signal.field_id].options)
            values = signal.value if signal.operator == "in" else [signal.value]
            if not isinstance(values, list) or not set(values).issubset(options):
                raise ValueError(
                    f"{definition.system_id}: unsupported answer-signal value "
                    f"for {signal.field_id}"
                )


def _status(score: int, thresholds: dict[str, int]) -> str:
    if score >= thresholds["recommended"]:
        return "recommended"
    if score >= thresholds["optional"]:
        return "optional"
    return "inexpedient"


def _reason(
    score: int,
    positive_reasons: list[tuple[float, str]],
    recommended_threshold: int,
) -> str:
    unique = []
    for _, item in sorted(positive_reasons, reverse=True):
        if item not in unique:
            unique.append(item)
    if unique:
        prefix = (
            "Основні підстави рекомендації"
            if score >= recommended_threshold
            else "На оцінку найбільше вплинули"
        )
        return f"{prefix}: " + "; ".join(unique[:2]) + "."
    return (
        "Підтверджені сценарії не показали вираженої потреби в цій системі; "
        "її можна не включати до першого складу проєкту."
    )


def evaluate_system_needs(
    *,
    object_answers: dict[str, Any],
    behavior_answers: dict[str, Any],
    profile: BehaviorProfile,
    questionnaire: BehaviorQuestionnaire,
    model: SystemNeedModel,
    active_rules: dict[str, Any],
) -> list[dict[str, Any]]:
    question_map = {item.id: item for item in questionnaire.questions}
    validate_system_need_model(model, questionnaire, set(active_rules["systems"]))
    results = []

    for definition in model.systems:
        hard_rules = active_rules["systems"][definition.system_id].get(
            "hard_rules", []
        )
        ignored_hard_rules = set(definition.ignored_hard_rule_ids)
        unknown_ignored = ignored_hard_rules - {
            item["rule_id"] for item in hard_rules
        }
        if unknown_ignored:
            raise ValueError(
                f"{definition.system_id}: unknown ignored hard rules "
                f"{sorted(unknown_ignored)}"
            )
        hard_match = next(
            (
                rule
                for rule in hard_rules
                if rule["rule_id"] not in ignored_hard_rules
                if matches(rule, object_answers)
            ),
            None,
        )
        if hard_match is not None:
            results.append(
                {
                    "system_id": definition.system_id,
                    "status": "not_applicable",
                    "rule_score": None,
                    "confidence_percent": 100,
                    "triggered_rules": [hard_match["rule_id"]],
                    "evidence_question_ids": [],
                    "missing_data": [],
                    "reasons": [hard_match["reason"]],
                    "need_model": model.model_id,
                }
            )
            continue

        weighted_score = 0.0
        scoring_weight = 0.0
        confidence_weight = 0.0
        possible_weight = 0.0
        evidence_ids: list[str] = []
        missing_data: list[str] = []
        positive_reasons: list[tuple[float, str]] = []

        for factor, weight in definition.factor_weights.items():
            possible_weight += weight
            result = profile.factors[factor]
            effective_weight = weight * result.confidence_percent / 100
            if effective_weight <= 0:
                missing_data.append(factor)
                continue
            weighted_score += result.score * effective_weight
            scoring_weight += effective_weight
            confidence_weight += effective_weight
            evidence_ids.extend(result.evidence_question_ids)
            if result.score >= 50:
                positive_reasons.append(
                    (result.score * effective_weight, FACTOR_LABELS_UK[factor])
                )

        for question_id, weight in definition.question_weights.items():
            possible_weight += weight
            answer = behavior_answers.get(question_id)
            question = question_map[question_id]
            if answer not in question.scores:
                missing_data.append(question_id)
                continue
            score = round(100 * question.scores[answer])
            weighted_score += score * weight
            scoring_weight += weight
            confidence_weight += weight
            evidence_ids.append(question_id)

        for signal in definition.object_signals:
            possible_weight += signal.weight
            if signal.field_id not in object_answers:
                missing_data.append(signal.field_id)
                continue
            confidence_weight += signal.weight
            condition = {
                "question_id": signal.field_id,
                "operator": signal.operator,
                "value": signal.value,
            }
            signal_score = signal.score if matches(condition, object_answers) else 0
            if signal_score:
                weighted_score += signal_score * signal.weight
                scoring_weight += signal.weight
                evidence_ids.append(signal.field_id)
                positive_reasons.append(
                    (signal_score * signal.weight, signal.reason_uk)
                )

        for signal in definition.answer_signals:
            possible_weight += signal.weight
            if signal.field_id not in behavior_answers:
                missing_data.append(signal.field_id)
                continue
            confidence_weight += signal.weight
            condition = {
                "question_id": signal.field_id,
                "operator": signal.operator,
                "value": signal.value,
            }
            signal_score = signal.score if matches(condition, behavior_answers) else 0
            if signal_score:
                weighted_score += signal_score * signal.weight
                scoring_weight += signal.weight
                evidence_ids.append(signal.field_id)
                positive_reasons.append(
                    (signal_score * signal.weight, signal.reason_uk)
                )

        score = round(weighted_score / scoring_weight) if scoring_weight else 0
        confidence = (
            round(100 * confidence_weight / possible_weight)
            if possible_weight
            else 0
        )
        combined_answers = {**object_answers, **behavior_answers}
        status_rule = next(
            (
                rule
                for rule in definition.status_rules
                if matches(rule.when, combined_answers)
            ),
            None,
        )
        if status_rule is not None:
            score = status_rule.score
            result_status = status_rule.status
            triggered_rules = [status_rule.rule_id]
            reasons = [status_rule.reason_uk]
        else:
            result_status = _status(score, model.status_thresholds)
            triggered_rules = []
            reasons = [
                _reason(
                    score,
                    positive_reasons,
                    model.status_thresholds["recommended"],
                )
            ]
        results.append(
            {
                "system_id": definition.system_id,
                "status": result_status,
                "rule_score": score,
                "confidence_percent": confidence,
                "triggered_rules": triggered_rules,
                "evidence_question_ids": list(dict.fromkeys(evidence_ids)),
                "missing_data": list(dict.fromkeys(missing_data)),
                "reasons": reasons,
                "need_model": model.model_id,
            }
        )
    return results
