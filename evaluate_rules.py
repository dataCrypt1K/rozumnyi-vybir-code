from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"


def load_json_compatible_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def matches(condition: dict[str, Any], answers: dict[str, Any]) -> bool:
    if "all" in condition:
        return all(matches(item, answers) for item in condition["all"])
    if "any" in condition:
        return any(matches(item, answers) for item in condition["any"])

    value = answers.get(condition["question_id"])
    expected = condition.get("value")
    operator = condition["operator"]

    if operator == "equals":
        return value == expected
    if operator == "in":
        return value in expected
    if operator == "gte":
        return value is not None and value >= expected
    if operator == "lte":
        return value is not None and value <= expected
    if operator == "contains":
        return isinstance(value, list) and expected in value
    if operator == "not_contains":
        return isinstance(value, list) and expected not in value
    raise ValueError(f"Unsupported operator: {operator}")


def status_for_score(score: int, thresholds: dict[str, int]) -> str:
    if score >= thresholds["recommended"]:
        return "recommended"
    if score >= thresholds["optional"]:
        return "optional"
    return "inexpedient"


def iter_conditions(rule: dict[str, Any]):
    if "all" in rule:
        for item in rule["all"]:
            yield from iter_conditions(item)
        return
    if "any" in rule:
        for item in rule["any"]:
            yield from iter_conditions(item)
        return
    yield rule


def validate_answer(question: dict[str, Any], value: Any) -> None:
    question_id = question["id"]
    question_type = question["type"]

    if question_type == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{question_id}: expected boolean")
    if question_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{question_id}: expected integer")
        if not question["min"] <= value <= question["max"]:
            raise ValueError(f"{question_id}: value outside allowed range")
    if question_type == "scale":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{question_id}: expected integer scale value")
        if not question["min"] <= value <= question["max"]:
            raise ValueError(f"{question_id}: scale value outside allowed range")
    if question_type == "single_choice" and value not in question["options"]:
        raise ValueError(f"{question_id}: unsupported option {value!r}")
    if question_type == "multi_choice":
        if not isinstance(value, list) or not value:
            raise ValueError(f"{question_id}: expected a non-empty list")
        invalid = set(value) - set(question["options"])
        if invalid:
            raise ValueError(f"{question_id}: unsupported options {sorted(invalid)}")
        if "none" in value and len(value) > 1:
            raise ValueError(f"{question_id}: 'none' cannot be combined with other options")


def validate_configuration(
    questions_data: dict[str, Any],
    systems_data: dict[str, Any],
    rules_data: dict[str, Any],
    profiles_data: dict[str, Any],
) -> None:
    questions = questions_data["questions"]
    systems = systems_data["systems"]
    question_map = {question["id"]: question for question in questions}
    system_ids = {system["system_id"] for system in systems}

    if len(question_map) != len(questions):
        raise ValueError("Duplicate question id")
    if len(system_ids) != len(systems):
        raise ValueError("Duplicate system id")

    rule_system_ids = set(rules_data["systems"])
    if rule_system_ids != system_ids:
        raise ValueError(
            f"System mismatch: catalog={sorted(system_ids)}, rules={sorted(rule_system_ids)}"
        )

    for system_id, system_rules in rules_data["systems"].items():
        for rule in system_rules.get("hard_rules", []) + system_rules.get("score_rules", []):
            for condition in iter_conditions(rule):
                question_id = condition["question_id"]
                if question_id not in question_map:
                    raise ValueError(
                        f"{system_id}/{rule['rule_id']}: unknown question {question_id}"
                    )

    required_ids = {
        question["id"] for question in questions if question.get("required", False)
    }
    for profile in profiles_data["profiles"]:
        answers = profile["answers"]
        missing = required_ids - set(answers)
        if missing:
            raise ValueError(
                f"{profile['profile_id']}: missing required answers {sorted(missing)}"
            )
        for question_id, value in answers.items():
            if question_id not in question_map:
                raise ValueError(f"{profile['profile_id']}: unknown answer {question_id}")
            validate_answer(question_map[question_id], value)

        expected_ids = set(profile["expected_statuses"])
        if expected_ids != system_ids:
            raise ValueError(
                f"{profile['profile_id']}: expected statuses do not match system catalog"
            )


def evaluate_system(
    system_id: str,
    system_rules: dict[str, Any],
    answers: dict[str, Any],
    base_score: int,
    thresholds: dict[str, int],
) -> dict[str, Any]:
    for rule in system_rules.get("hard_rules", []):
        if matches(rule, answers):
            return {
                "system_id": system_id,
                "status": rule["result"],
                "rule_score": None,
                "triggered_rules": [rule["rule_id"]],
                "reasons": [rule["reason"]],
            }

    triggered = []
    group_points: dict[str, int] = {}
    reasons = []

    for rule in system_rules.get("score_rules", []):
        if not matches(rule, answers):
            continue
        triggered.append(rule["rule_id"])
        reasons.append(rule["reason"])
        group = rule["factor_group"]
        group_points[group] = group_points.get(group, 0) + rule["points"]

    capped_group_points = {}
    for group, points in group_points.items():
        cap = system_rules.get("factor_group_caps", {}).get(group, 100)
        capped_group_points[group] = max(-cap, min(points, cap))

    score = max(0, min(100, base_score + sum(capped_group_points.values())))
    return {
        "system_id": system_id,
        "status": status_for_score(score, thresholds),
        "rule_score": score,
        "triggered_rules": triggered,
        "factor_points": capped_group_points,
        "reasons": reasons,
    }


def evaluate_profile(answers: dict[str, Any], rules: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        evaluate_system(
            system_id,
            system_rules,
            answers,
            rules["base_score"],
            rules["status_thresholds"],
        )
        for system_id, system_rules in rules["systems"].items()
    ]


def run_tests() -> int:
    from active_config import load_active_configuration

    questions, systems, rules, test_data = load_active_configuration()
    validate_configuration(questions, systems, rules, test_data)
    print("PASS configuration validation")
    failures = []

    for profile in test_data["profiles"]:
        results = evaluate_profile(profile["answers"], rules)
        actual = {item["system_id"]: item["status"] for item in results}
        expected = profile["expected_statuses"]
        if actual != expected:
            failures.append(
                {
                    "profile_id": profile["profile_id"],
                    "expected": expected,
                    "actual": actual,
                    "details": results,
                }
            )
        else:
            scores = {
                item["system_id"]: item["rule_score"]
                for item in results
            }
            print(f"PASS {profile['profile_id']}: {actual} scores={scores}")

    if failures:
        print(json.dumps({"failures": failures}, ensure_ascii=False, indent=2))
        return 1

    print(f"All {len(test_data['profiles'])} profiles passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate smart-home recommendation rules.")
    parser.add_argument("--test", action="store_true", help="Run bundled control profiles.")
    parser.add_argument("--profile", type=Path, help="Evaluate one JSON profile.")
    args = parser.parse_args()

    if args.test:
        return run_tests()
    if args.profile:
        rules = load_json_compatible_yaml(CONFIG_DIR / "rules.yaml")
        with args.profile.open("r", encoding="utf-8") as stream:
            profile = json.load(stream)
        print(json.dumps(evaluate_profile(profile, rules), ensure_ascii=False, indent=2))
        return 0

    parser.error("Use --test or --profile PATH")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
