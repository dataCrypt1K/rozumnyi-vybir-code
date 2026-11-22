from __future__ import annotations

from copy import deepcopy
from typing import Any

from evaluate_rules import CONFIG_DIR, load_json_compatible_yaml


ACTIVE_MODULES = [
    ("rules_security_v1.yaml", "profile_security_extension_v1.json"),
    ("rules_safety_power_v1.yaml", "profile_safety_power_extension_v1.json"),
    ("rules_comfort_media_v1.yaml", "profile_comfort_media_extension_v1.json"),
    ("rules_outdoor_v1.yaml", "profile_outdoor_extension_v1.json"),
]


def iter_rule_conditions(rule: dict[str, Any]):
    for group in ("all", "any"):
        if group in rule:
            for item in rule[group]:
                yield from iter_rule_conditions(item)
            return
    yield rule


def load_active_configuration() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    questions = deepcopy(load_json_compatible_yaml(CONFIG_DIR / "questions.yaml"))
    systems = deepcopy(load_json_compatible_yaml(CONFIG_DIR / "systems.yaml"))
    rules = deepcopy(load_json_compatible_yaml(CONFIG_DIR / "rules.yaml"))
    profiles = deepcopy(load_json_compatible_yaml(CONFIG_DIR / "test_profiles.json"))

    module_rules = [
        load_json_compatible_yaml(CONFIG_DIR / rules_file)
        for rules_file, _ in ACTIVE_MODULES
    ]
    active_module_system_ids = set()
    for module in module_rules:
        rules["systems"].update(deepcopy(module["systems"]))
        active_module_system_ids.update(module["systems"])

    full_catalog = load_json_compatible_yaml(CONFIG_DIR / "systems_catalog_v1.yaml")
    systems["systems"].extend(
        deepcopy(
            [
                item
                for item in full_catalog["systems"]
                if item["system_id"] in active_module_system_ids
            ]
        )
    )

    referenced_ids = set()
    for module in module_rules:
        for system_rules in module["systems"].values():
            for rule in system_rules.get("hard_rules", []) + system_rules.get(
                "score_rules", []
            ):
                referenced_ids.update(
                    condition["question_id"]
                    for condition in iter_rule_conditions(rule)
                )
    existing_ids = {item["id"] for item in questions["questions"]}
    extension_questions = load_json_compatible_yaml(
        CONFIG_DIR / "questions_extension_v1.yaml"
    )["questions"]
    questions["questions"].extend(
        deepcopy(
            [
                item
                for item in extension_questions
                if item["id"] in referenced_ids and item["id"] not in existing_ids
            ]
        )
    )

    for _, profile_file in ACTIVE_MODULES:
        profile_extensions = {
            item["profile_id"]: item
            for item in load_json_compatible_yaml(CONFIG_DIR / profile_file)["profiles"]
        }
        for profile in profiles["profiles"]:
            extension = profile_extensions[profile["profile_id"]]
            profile["answers"].update(deepcopy(extension["answers"]))
            profile["expected_statuses"].update(
                deepcopy(extension["expected_statuses"])
            )

    return questions, systems, rules, profiles
