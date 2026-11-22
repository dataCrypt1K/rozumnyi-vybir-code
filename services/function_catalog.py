from __future__ import annotations

from pathlib import Path
import re
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from domain.enums import ImplementationPhase, PropertyType
from domain.models import DomainModel


CATALOG_SCHEMA_VERSION = "0.1"
IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?$"
TAG_PATTERN = r"^[a-z][a-z0-9_]*$"


class FunctionCatalogItem(DomainModel):
    function_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=160)
    system_id: str = Field(pattern=TAG_PATTERN, max_length=120)
    name_uk: str = Field(min_length=1, max_length=200)
    description_uk: str = Field(min_length=1, max_length=1000)
    applicable_property_types: list[PropertyType] = Field(min_length=1)
    applicable_room_types: list[str] = Field(default_factory=list)
    applicable_location_types: list[str] = Field(default_factory=list)
    selection_unit: str = Field(pattern=TAG_PATTERN, max_length=120)
    default_phase_priority: ImplementationPhase
    prerequisite_function_ids: list[str] = Field(default_factory=list)
    technical_mvp: bool
    evidence_requirement: Literal["none", "official", "expert", "both"]
    final_engineering_required: bool

    @field_validator(
        "applicable_room_types",
        "applicable_location_types",
        "prerequisite_function_ids",
    )
    @classmethod
    def validate_identifier_lists(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("catalog identifier lists must not contain duplicates")
        if any(not re.fullmatch(IDENTIFIER_PATTERN, item) for item in value):
            raise ValueError("catalog identifier lists contain an invalid identifier")
        return value

    @field_validator("applicable_property_types")
    @classmethod
    def property_types_must_be_unique(
        cls, value: list[PropertyType]
    ) -> list[PropertyType]:
        if len(value) != len(set(value)):
            raise ValueError("applicable_property_types must not contain duplicates")
        return value

    @model_validator(mode="after")
    def require_location_scope(self) -> "FunctionCatalogItem":
        if not self.applicable_room_types and not self.applicable_location_types:
            raise ValueError(
                "a function must define applicable room types or location types"
            )
        if self.applicable_room_types and "indoor_room" in self.applicable_location_types:
            raise ValueError(
                "indoor_room cannot broaden an explicit applicable_room_types list"
            )
        return self


class FunctionCatalog(DomainModel):
    schema_version: str = Field(min_length=1, max_length=20)
    review_status: Literal["draft_for_expert_review", "expert_reviewed"]
    format_note: str = Field(min_length=1, max_length=2000)
    functions: list[FunctionCatalogItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> "FunctionCatalog":
        if self.schema_version != CATALOG_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported function catalog schema_version: {self.schema_version}"
            )

        function_ids = [item.function_id for item in self.functions]
        if len(function_ids) != len(set(function_ids)):
            raise ValueError("function_id values must be unique")
        known = set(function_ids)
        dependencies = {
            item.function_id: item.prerequisite_function_ids
            for item in self.functions
        }
        for function_id, prerequisites in dependencies.items():
            unknown = set(prerequisites) - known
            if unknown:
                raise ValueError(
                    f"{function_id} references unknown prerequisite functions: "
                    + ", ".join(sorted(unknown))
                )
            if function_id in prerequisites:
                raise ValueError(f"{function_id} cannot depend on itself")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(function_id: str) -> None:
            if function_id in visiting:
                raise ValueError("function prerequisite graph contains a cycle")
            if function_id in visited:
                return
            visiting.add(function_id)
            for prerequisite in dependencies[function_id]:
                visit(prerequisite)
            visiting.remove(function_id)
            visited.add(function_id)

        for function_id in function_ids:
            visit(function_id)
        return self


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"cannot read YAML catalog {path.name}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"YAML catalog {path.name} must contain a mapping")
    return raw


def load_function_catalog(
    functions_path: str | Path | None = None,
    systems_path: str | Path | None = None,
) -> FunctionCatalog:
    project_root = Path(__file__).resolve().parents[1]
    resolved_functions = Path(functions_path) if functions_path else (
        project_root / "config" / "functions_catalog_v1.yaml"
    )
    resolved_systems = Path(systems_path) if systems_path else (
        project_root / "config" / "systems_catalog_v1.yaml"
    )

    catalog = FunctionCatalog.model_validate(_load_yaml_mapping(resolved_functions))
    systems_payload = _load_yaml_mapping(resolved_systems)
    systems = systems_payload.get("systems")
    if not isinstance(systems, list):
        raise ValueError("systems catalog must contain a systems list")
    try:
        system_ids = {item["system_id"] for item in systems}
    except (KeyError, TypeError) as error:
        raise ValueError("every systems catalog item must contain system_id") from error
    if len(system_ids) != len(systems):
        raise ValueError("systems catalog system_id values must be unique")
    if len(system_ids) != 19:
        raise ValueError(f"systems catalog must contain 19 systems, got {len(system_ids)}")

    function_system_ids = {item.system_id for item in catalog.functions}
    unknown = function_system_ids - system_ids
    missing = system_ids - function_system_ids
    if unknown:
        raise ValueError(
            "function catalog references unknown systems: "
            + ", ".join(sorted(unknown))
        )
    if missing:
        raise ValueError(
            "function catalog does not cover systems: " + ", ".join(sorted(missing))
        )
    return catalog
