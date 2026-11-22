from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from domain.enums import RoomType
from domain.models import DomainModel
from services.function_catalog import FunctionCatalog, load_function_catalog


RULES_SCHEMA_VERSION = "0.1"
PHASE_RULES_VERSION = "function_phase_v1"
TargetRole = Literal[
    "service_zone",
    "device_endpoint",
    "infrastructure_anchor",
    "aggregate_scope",
]


class ConditionExpression(DomainModel):
    source: Literal["questionnaire", "object", "system_recommendation"] | None = None
    field: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$", max_length=120
    )
    operator: Literal[
        "equals",
        "not_equals",
        "contains",
        "not_contains",
        "in",
        "gte",
        "lte",
        "exists",
    ] | None = None
    value: Any = None
    all: list[ConditionExpression] | None = None
    any: list[ConditionExpression] | None = None

    @model_validator(mode="after")
    def validate_expression_shape(self) -> "ConditionExpression":
        is_leaf = all(
            value is not None for value in (self.source, self.field, self.operator)
        )
        group_count = int(self.all is not None) + int(self.any is not None)
        if is_leaf and group_count == 0:
            return self
        if not is_leaf and group_count == 1:
            return self
        raise ValueError("condition must be one leaf or one all/any expression")

    @property
    def is_leaf(self) -> bool:
        return self.source is not None


class PhaseFactors(DomainModel):
    safety_criticality: int = Field(ge=0, le=2)
    retrofit_penalty: int = Field(ge=0, le=2)
    dependency_role: int = Field(ge=0, le=2)
    daily_impact: int = Field(ge=0, le=2)


class FunctionRule(DomainModel):
    factors: PhaseFactors
    applicability: ConditionExpression
    relevance: ConditionExpression


class AliasDefinition(DomainModel):
    ui_room_types: list[RoomType] = Field(default_factory=list)
    resolution: Literal["direct", "expanded", "virtual"]
    target_role: TargetRole = "service_zone"

    @field_validator("ui_room_types")
    @classmethod
    def ui_room_types_must_be_unique(cls, value: list[RoomType]) -> list[RoomType]:
        if len(value) != len(set(value)):
            raise ValueError("alias ui_room_types must not contain duplicates")
        return value

    @model_validator(mode="after")
    def virtual_aliases_are_the_only_empty_aliases(self) -> "AliasDefinition":
        if not self.ui_room_types and self.resolution != "virtual":
            raise ValueError("only virtual aliases may have no direct UI room type")
        return self


class AliasCatalog(DomainModel):
    room_types: dict[str, AliasDefinition]
    location_types: dict[str, AliasDefinition]
    virtual_location_note: str | None = Field(default=None, max_length=1000)


class FunctionRulesConfig(DomainModel):
    schema_version: str = Field(min_length=1, max_length=20)
    review_status: Literal["draft_for_expert_review", "expert_reviewed"]
    format_note: str = Field(min_length=1, max_length=4000)
    phase_rules_version: str = Field(min_length=1, max_length=80)
    factor_scale: dict[str, Any]
    condition_schema: dict[str, Any]
    aliases: AliasCatalog
    fallback_rule: dict[str, Any]
    phase_rules: dict[str, Any]
    functions: dict[str, FunctionRule] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_versions_and_factor_scale(self) -> "FunctionRulesConfig":
        if self.schema_version != RULES_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported function rules schema_version: {self.schema_version}"
            )
        if self.phase_rules.get("version") != self.phase_rules_version:
            raise ValueError("phase_rules.version must equal phase_rules_version")
        if self.phase_rules_version != PHASE_RULES_VERSION:
            raise ValueError(
                f"unsupported phase_rules_version: {self.phase_rules_version}"
            )
        minimum = self.factor_scale.get("min", self.factor_scale.get("minimum"))
        maximum = self.factor_scale.get("max", self.factor_scale.get("maximum"))
        if minimum is not None and minimum != 0:
            raise ValueError("factor_scale minimum must be 0")
        if maximum is not None and maximum != 2:
            raise ValueError("factor_scale maximum must be 2")
        relevance = self.fallback_rule.get("relevance", {})
        expected_match = {
            "recommended": "recommended",
            "optional": "recommended",
            "inexpedient": "optional",
        }
        expected_no_match = {
            "recommended": "optional",
            "optional": "optional",
            "inexpedient": "inexpedient",
        }
        if relevance.get("match_status_map") != expected_match:
            raise ValueError("unsupported relevance match_status_map")
        if relevance.get("no_match_status_map") != expected_no_match:
            raise ValueError("unsupported relevance no_match_status_map")
        expected_phase_steps = [
            "PHASE_NOT_APPLICABLE",
            "PHASE_INITIALIZE_DEFAULT",
            "PHASE_INEXPEDIENT_LATER",
            "PHASE_SAFETY_FIRST",
            "PHASE_DAILY_PROMOTE_ONE",
            "PHASE_RETROFIT_PROMOTE_ONE",
            "PHASE_PREREQUISITE_NORMALIZATION",
        ]
        actual_phase_steps = [
            item.get("rule_id") for item in self.phase_rules.get("steps", [])
        ]
        if actual_phase_steps != expected_phase_steps:
            raise ValueError("phase_rules.steps do not match the supported hierarchy")
        return self


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"cannot read YAML rules {path.name}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"YAML rules {path.name} must contain a mapping")
    return payload


def load_function_rules(
    rules_path: str | Path | None = None,
    *,
    catalog: FunctionCatalog | None = None,
) -> FunctionRulesConfig:
    project_root = Path(__file__).resolve().parents[1]
    resolved_path = Path(rules_path) if rules_path else (
        project_root / "config" / "function_rules_v1.yaml"
    )
    rules = FunctionRulesConfig.model_validate(_read_yaml(resolved_path))
    function_catalog = catalog or load_function_catalog()
    catalog_ids = {item.function_id for item in function_catalog.functions}
    rule_ids = set(rules.functions)
    missing = catalog_ids - rule_ids
    unknown = rule_ids - catalog_ids
    if missing or unknown:
        details = []
        if missing:
            details.append("missing: " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown: " + ", ".join(sorted(unknown)))
        raise ValueError("function rules must cover the catalog exactly; " + "; ".join(details))

    room_tags = {
        tag for item in function_catalog.functions for tag in item.applicable_room_types
    }
    location_tags = {
        tag
        for item in function_catalog.functions
        for tag in item.applicable_location_types
    }
    missing_room_aliases = room_tags - set(rules.aliases.room_types)
    missing_location_aliases = location_tags - set(rules.aliases.location_types)
    if missing_room_aliases or missing_location_aliases:
        details = []
        if missing_room_aliases:
            details.append("room aliases: " + ", ".join(sorted(missing_room_aliases)))
        if missing_location_aliases:
            details.append(
                "location aliases: " + ", ".join(sorted(missing_location_aliases))
            )
        raise ValueError("function rules do not normalize all catalog aliases; " + "; ".join(details))
    return rules
