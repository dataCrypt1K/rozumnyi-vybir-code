from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from active_config import iter_rule_conditions, load_active_configuration
from evaluate_rules import evaluate_profile, load_json_compatible_yaml, matches
from llm_client import MockLLMClient, OpenAIResponsesClient, PartialExplanationError
from services.profile_questionnaire import BehaviorProfile, BehaviorQuestionnaire
from services.system_need_scoring import (
    SystemNeedModel,
    evaluate_system_needs,
    load_system_need_model,
)


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"

EXPLANATION_CONTEXT_EXTRAS = {
    "power_protection_monitoring": (
        "outage_frequency", "q_home_unattended", "critical_loads"
    ),
    "backup_power": ("outage_frequency", "outage_duration"),
}


def explanation_context_fact_ids(
    system_id: str,
    need_model: SystemNeedModel,
    active_rules: dict[str, Any],
) -> list[str]:
    definition = next(
        item for item in need_model.systems if item.system_id == system_id
    )
    ids = [
        *definition.question_weights,
        *(signal.field_id for signal in definition.object_signals),
        *(signal.field_id for signal in definition.answer_signals),
    ]
    for rule in definition.status_rules:
        ids.extend(
            condition["question_id"]
            for condition in iter_rule_conditions(rule.when)
            if "question_id" in condition
        )
    for rule in active_rules["systems"][system_id].get("hard_rules", []):
        ids.extend(
            condition["question_id"]
            for condition in iter_rule_conditions(rule)
            if "question_id" in condition
        )
    ids.extend(EXPLANATION_CONTEXT_EXTRAS.get(system_id, ()))
    return list(dict.fromkeys(ids))


def directional_explanation_fact_ids(
    system_id: str,
    status: str,
    profile: dict[str, Any],
    questionnaire: BehaviorQuestionnaire,
    need_model: SystemNeedModel,
    active_rules: dict[str, Any],
) -> list[str]:
    ids = explanation_context_fact_ids(system_id, need_model, active_rules)
    if status != "recommended":
        return ids

    definition = next(
        item for item in need_model.systems if item.system_id == system_id
    )
    questions = {item.id: item for item in questionnaire.questions}
    positive_signals = [*definition.object_signals, *definition.answer_signals]
    selected = []
    for field_id in ids:
        if field_id not in profile:
            continue
        if field_id in questions:
            question = questions[field_id]
            if question.scores.get(profile[field_id], 0) >= 0.5:
                selected.append(field_id)
            continue
        signals = [item for item in positive_signals if item.field_id == field_id]
        if signals:
            if any(
                signal.score > 0 and matches(
                    {"question_id": field_id, "operator": signal.operator,
                     "value": signal.value},
                    profile,
                )
                for signal in signals
            ):
                selected.append(field_id)
            continue
        value = profile[field_id]
        if value is True or (
            isinstance(value, int) and not isinstance(value, bool) and value > 0
        ) or (
            isinstance(value, list) and bool(value) and "none" not in value
        ):
            selected.append(field_id)
    return selected


def status_for_score(score: int, thresholds: dict[str, int]) -> str:
    if score >= thresholds["recommended"]:
        return "recommended"
    if score >= thresholds["optional"]:
        return "optional"
    return "inexpedient"


def combine_scores(
    rule_score: int,
    llm_score: int,
    alpha: float,
    review_difference: int,
) -> tuple[int, bool, str]:
    needs_review = abs(rule_score - llm_score) > review_difference
    if needs_review:
        return rule_score, True, "rule_fallback"
    hybrid_score = round(alpha * rule_score + (1 - alpha) * llm_score)
    return hybrid_score, False, "hybrid"


def guard_score_to_rule_status(
    score: int,
    rule_status: str,
    thresholds: dict[str, int],
) -> tuple[int, bool]:
    """Keep LLM variation inside the status band selected by verified rules."""
    recommended = thresholds["recommended"]
    optional = thresholds["optional"]
    if rule_status == "recommended":
        guarded = max(recommended, min(100, score))
    elif rule_status == "optional":
        guarded = max(optional, min(recommended - 1, score))
    elif rule_status == "inexpedient":
        guarded = max(0, min(optional - 1, score))
    else:
        raise ValueError(f"Unsupported rule status for score guard: {rule_status}")
    return guarded, guarded != score


def build_client(provider: str, config: dict[str, Any]):
    if provider == "mock":
        return MockLLMClient()
    if provider == "openai":
        return OpenAIResponsesClient(config)
    raise ValueError(f"Unsupported provider: {provider}")


def call_with_retry(
    client,
    profile: dict[str, Any],
    systems: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    rule_results: dict[str, dict[str, Any]],
    max_attempts: int,
):
    last_error: Exception | None = None
    for _ in range(max(1, max_attempts)):
        try:
            return client.recommend(profile, systems, questions, rule_results)
        except (RuntimeError, ValueError) as error:
            last_error = error
    assert last_error is not None
    raise last_error


def build_rule_only_fallback(
    rule_results_list: list[dict[str, Any]],
    model: str,
    alpha: float,
    error: Exception | None,
) -> dict[str, Any]:
    mode = "rule_only" if error is None else "rule_only_fallback"
    recommendations = []
    for item in rule_results_list:
        is_not_applicable = item["status"] == "not_applicable"
        reasons = item.get("reasons", [])
        recommendations.append(
            {
                "system_id": item["system_id"],
                "status": item["status"],
                "suitability_percent": None if is_not_applicable else item["rule_score"],
                "rule_score": None if is_not_applicable else item["rule_score"],
                "llm_score": None,
                "reason": reasons[0] if reasons else "Оцінку сформовано за правилами.",
                "evidence_question_ids": item.get("evidence_question_ids", []),
                "missing_data": item.get("missing_data", []),
                "confidence_percent": item.get("confidence_percent"),
                "needs_review": False,
                "decision_source": mode,
            }
        )
    return {
        "provider": mode,
        "requested_provider": "rules" if error is None else "openai",
        "explanation_status": mode,
        "model": model,
        "hybrid_alpha": alpha,
        **({"fallback_error": str(error)} if error is not None else {}),
        "recommendations": recommendations,
    }


def finalize_llm_explanations(
    client: Any,
    provider: str,
    profile: dict[str, Any],
    systems: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    final_results: list[dict[str, Any]],
    max_attempts: int,
) -> str:
    if provider != "openai":
        return "deterministic_mock"
    applicable = [
        item for item in final_results if item["status"] != "not_applicable"
    ]
    if not applicable:
        return "no_applicable_systems"
    pending = {item["system_id"]: item for item in applicable}
    validated = {}
    feedback = None
    for _ in range(max(1, max_attempts)):
        try:
            response = client.explain(
                profile, systems, questions, list(pending.values()), feedback
            )
            for explanation in response.explanations:
                validated[explanation.system_id] = explanation
                pending.pop(explanation.system_id, None)
            if not pending:
                break
        except PartialExplanationError as error:
            for explanation in error.valid_explanations:
                validated[explanation.system_id] = explanation
                pending.pop(explanation.system_id, None)
            feedback = str(error).splitlines()[0][:300]
        except (RuntimeError, ValueError, KeyError) as error:
            feedback = str(error).splitlines()[0][:300]
    for item in applicable:
        explanation = validated.get(item["system_id"])
        if explanation is not None:
            item["initial_llm_reason"] = item["reason"]
            item["reason"] = explanation.reason
            item["final_explanation_evidence_question_ids"] = [
                fact.question_id for fact in explanation.evidence
            ]
            item["evidence_question_ids"] = list(dict.fromkeys(
                [qid for qid in item.get("evidence_question_ids", []) if qid in profile]
                + [fact.question_id for fact in explanation.evidence]
            ))
            continue
        item["explanation_needs_review"] = True
        item["explanation_validation_error"] = feedback
        item["evidence_question_ids"] = [
            qid for qid in item.get("evidence_question_ids", []) if qid in profile
        ]
    if not pending:
        return "final_llm_validated"
    return "partial_llm_validated" if validated else "initial_llm_unverified"


def recommend_v2(
    object_answers: dict[str, Any],
    behavior_answers: dict[str, Any],
    behavior_profile: BehaviorProfile,
    questionnaire: BehaviorQuestionnaire,
    provider: str = "mock",
) -> dict[str, Any]:
    questions_data, systems_data, active_rules, _ = load_active_configuration()
    llm_config = load_json_compatible_yaml(CONFIG_DIR / "llm.yaml")
    need_model = load_system_need_model()
    direct_results_list = evaluate_system_needs(
        object_answers=object_answers,
        behavior_answers=behavior_answers,
        profile=behavior_profile,
        questionnaire=questionnaire,
        model=need_model,
        active_rules=active_rules,
    )
    if provider == "rules":
        result = build_rule_only_fallback(direct_results_list, "rules", 0.0, None)
        result["need_model"] = need_model.model_id
        result["recommendation_method"] = "direct_v2"
        return result
    direct_results = {item["system_id"]: item for item in direct_results_list}
    system_map = {
        item["system_id"]: item for item in systems_data["systems"]
    }
    applicable_systems = [
        system_map[item["system_id"]]
        for item in direct_results_list
        if item["status"] != "not_applicable"
    ]

    object_questions = [
        item
        for item in questions_data["questions"]
        if item["id"] in object_answers
    ]
    behavior_questions = [
        {
            "id": item.id,
            "text": item.text,
            "type": item.type,
            "options": item.options,
            "option_scores": item.scores,
        }
        for item in questionnaire.questions
    ]
    llm_questions = object_questions + behavior_questions
    llm_profile = {**object_answers, **behavior_answers}
    client = build_client(provider, llm_config)
    try:
        llm_response = call_with_retry(
            client,
            llm_profile,
            applicable_systems,
            llm_questions,
            direct_results,
            int(llm_config.get("max_attempts", 2)),
        )
    except (RuntimeError, ValueError) as error:
        if provider != "openai":
            raise
        fallback = build_rule_only_fallback(
            direct_results_list,
            getattr(client, "model", llm_config["model"]),
            float(llm_config["hybrid_alpha"]),
            error,
        )
        fallback["need_model"] = need_model.model_id
        fallback["recommendation_method"] = "direct_v2"
        return fallback

    llm_results = {
        item.system_id: item for item in llm_response.recommendations
    }
    alpha = float(llm_config["hybrid_alpha"])
    review_difference = int(llm_config["review_difference"])
    final_results = []
    for direct_result in direct_results_list:
        system_id = direct_result["system_id"]
        if direct_result["status"] == "not_applicable":
            final_results.append(
                {
                    "system_id": system_id,
                    "status": "not_applicable",
                    "suitability_percent": None,
                    "rule_score": None,
                    "llm_score": None,
                    "reason": direct_result["reasons"][0],
                    "evidence_question_ids": [],
                    "missing_data": [],
                    "confidence_percent": direct_result["confidence_percent"],
                    "needs_review": False,
                    "decision_source": "hard_rule",
                }
            )
            continue

        llm_result = llm_results[system_id]
        direct_score = int(direct_result["rule_score"])
        hybrid_score, needs_review, decision_source = combine_scores(
            direct_score,
            llm_result.llm_score,
            alpha,
            review_difference,
        )
        status_guard_applied = False
        if bool(llm_config.get("preserve_rule_status", True)):
            hybrid_score, status_guard_applied = guard_score_to_rule_status(
                hybrid_score,
                direct_result["status"],
                need_model.status_thresholds,
            )
            if status_guard_applied and decision_source == "hybrid":
                decision_source = "hybrid_status_guard"
        final_results.append(
            {
                "system_id": system_id,
                "status": status_for_score(
                    hybrid_score, need_model.status_thresholds
                ),
                "suitability_percent": hybrid_score,
                "rule_score": direct_score,
                "llm_score": llm_result.llm_score,
                "reason": llm_result.reason,
                "evidence_question_ids": list(
                    dict.fromkeys(
                        direct_result["evidence_question_ids"]
                        + llm_result.evidence_question_ids
                    )
                ),
                "explanation_candidate_fact_ids": directional_explanation_fact_ids(
                    system_id,
                    status_for_score(hybrid_score, need_model.status_thresholds),
                    llm_profile,
                    questionnaire,
                    need_model,
                    active_rules,
                ),
                "missing_data": list(
                    dict.fromkeys(
                        direct_result["missing_data"] + llm_result.missing_data
                    )
                ),
                "confidence_percent": direct_result["confidence_percent"],
                "needs_review": needs_review,
                "decision_source": decision_source,
                "status_guard_applied": status_guard_applied,
            }
        )

    explanation_status = finalize_llm_explanations(
        client,
        provider,
        llm_profile,
        applicable_systems,
        llm_questions,
        final_results,
        int(llm_config.get("max_attempts", 2)),
    )

    return {
        "provider": provider,
        "explanation_status": explanation_status,
        "model": (
            "deterministic_mock"
            if provider == "mock"
            else getattr(client, "model", llm_config["model"])
        ),
        "hybrid_alpha": alpha,
        "need_model": need_model.model_id,
        "recommendation_method": "direct_v2",
        "recommendations": final_results,
    }


def recommend(profile: dict[str, Any], provider: str = "mock") -> dict[str, Any]:
    questions_data, systems_data, rules, _ = load_active_configuration()
    llm_config = load_json_compatible_yaml(CONFIG_DIR / "llm.yaml")

    rule_results_list = evaluate_profile(profile, rules)
    rule_results = {item["system_id"]: item for item in rule_results_list}
    system_map = {
        item["system_id"]: item for item in systems_data["systems"]
    }
    applicable_systems = [
        system_map[item["system_id"]]
        for item in rule_results_list
        if item["status"] != "not_applicable"
    ]

    client = build_client(provider, llm_config)
    try:
        llm_response = call_with_retry(
            client,
            profile,
            applicable_systems,
            questions_data["questions"],
            rule_results,
            int(llm_config.get("max_attempts", 2)),
        )
    except (RuntimeError, ValueError) as error:
        if provider != "openai":
            raise
        return build_rule_only_fallback(
            rule_results_list,
            getattr(client, "model", llm_config["model"]),
            float(llm_config["hybrid_alpha"]),
            error,
        )
    llm_results = {
        item.system_id: item for item in llm_response.recommendations
    }

    alpha = float(llm_config["hybrid_alpha"])
    review_difference = int(llm_config["review_difference"])
    thresholds = rules["status_thresholds"]
    final_results = []

    for rule_result in rule_results_list:
        system_id = rule_result["system_id"]
        if rule_result["status"] == "not_applicable":
            final_results.append(
                {
                    "system_id": system_id,
                    "status": "not_applicable",
                    "suitability_percent": None,
                    "rule_score": None,
                    "llm_score": None,
                    "reason": rule_result["reasons"][0],
                    "evidence_question_ids": [],
                    "missing_data": [],
                    "needs_review": False,
                    "decision_source": "hard_rule",
                }
            )
            continue

        llm_result = llm_results[system_id]
        rule_score = int(rule_result["rule_score"])
        llm_score = llm_result.llm_score
        hybrid_score, needs_review, decision_source = combine_scores(
            rule_score, llm_score, alpha, review_difference
        )
        status_guard_applied = False
        if bool(llm_config.get("preserve_rule_status", True)):
            hybrid_score, status_guard_applied = guard_score_to_rule_status(
                hybrid_score,
                rule_result["status"],
                thresholds,
            )
            if status_guard_applied and decision_source == "hybrid":
                decision_source = "hybrid_status_guard"
        final_results.append(
            {
                "system_id": system_id,
                "status": status_for_score(hybrid_score, thresholds),
                "suitability_percent": hybrid_score,
                "rule_score": rule_score,
                "llm_score": llm_score,
                "reason": llm_result.reason,
                "evidence_question_ids": llm_result.evidence_question_ids,
                "missing_data": llm_result.missing_data,
                "needs_review": needs_review,
                "decision_source": decision_source,
                "status_guard_applied": status_guard_applied,
            }
        )

    explanation_status = finalize_llm_explanations(
        client,
        provider,
        profile,
        applicable_systems,
        questions_data["questions"],
        final_results,
        int(llm_config.get("max_attempts", 2)),
    )

    return {
        "provider": provider,
        "explanation_status": explanation_status,
        "model": (
            "deterministic_mock"
            if provider == "mock"
            else getattr(client, "model", llm_config["model"])
        ),
        "hybrid_alpha": alpha,
        "recommendations": final_results,
    }


def load_profile(profile_path: Path, profile_id: str | None) -> dict[str, Any]:
    with profile_path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if "profiles" not in payload:
        return payload.get("answers", payload)
    if not profile_id:
        raise ValueError("--profile-id is required for a collection of profiles")
    for profile in payload["profiles"]:
        if profile["profile_id"] == profile_id:
            return profile["answers"]
    raise ValueError(f"Unknown profile_id: {profile_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run hybrid smart-home recommendations.")
    parser.add_argument(
        "--profile",
        type=Path,
        default=CONFIG_DIR / "test_profiles.json",
    )
    parser.add_argument("--profile-id")
    parser.add_argument("--provider", choices=["mock", "openai"], default="mock")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    profile = load_profile(args.profile, args.profile_id)
    result = recommend(profile, provider=args.provider)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
