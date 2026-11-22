from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = ROOT / "config" / "object_parameters_v2.yaml"
DOCUMENT_ROOT = ROOT / "storage" / "documents"
ALLOWED_MIME_TYPES = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024


class ParameterDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label_uk: str = Field(min_length=1)
    group: str = Field(pattern=r"^(core|conditional|detail)$")
    weight: int = Field(ge=1, le=10)
    required: bool
    value_type: str
    options: list[Any] | None = None
    min: float | None = None
    max: float | None = None
    required_when: dict[str, Any] | None = None


class ObjectParameterRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    registry_id: str
    readiness_threshold_percent: int = Field(ge=1, le=100)
    parameters: list[ParameterDefinition]

    @field_validator("parameters")
    @classmethod
    def parameter_ids_must_be_unique(
        cls, value: list[ParameterDefinition]
    ) -> list[ParameterDefinition]:
        ids = [item.field_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("object parameter field_id values must be unique")
        return value


class ObjectFactValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    value: Any = None
    state: str = Field(
        pattern=(
            r"^(unknown|extracted|confirmed|corrected|not_present|"
            r"not_applicable|rejected)$"
        )
    )
    source: str = Field(
        pattern=r"^(manual|questionnaire|document_explicit|document_inferred)$"
    )
    confidence: str | None = Field(default=None, pattern=r"^(low|medium|high)$")


class CompletenessReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_coverage_percent: int = Field(ge=0, le=100)
    decision_readiness_percent: int = Field(ge=0, le=100)
    core_complete: bool
    recommendation_ready: bool
    missing_core: list[str]
    missing_required: list[str]


@dataclass(frozen=True)
class StoredDocument:
    original_name: str
    mime_type: str
    stored_path: str
    sha256: str
    size_bytes: int


def load_object_parameter_registry(
    path: Path = DEFAULT_REGISTRY_PATH,
) -> ObjectParameterRegistry:
    return ObjectParameterRegistry.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )


def _fact_value(facts: dict[str, ObjectFactValue], field_id: str) -> Any:
    fact = facts.get(field_id)
    if fact is None or fact.state in {"unknown", "rejected"}:
        return None
    if fact.state in {"not_present", "not_applicable"}:
        return False
    return fact.value


def parameter_is_applicable(
    parameter: ParameterDefinition,
    facts: dict[str, ObjectFactValue],
) -> bool:
    condition = parameter.required_when
    if not condition:
        return True
    return _fact_value(facts, str(condition["field_id"])) == condition.get("equals")


def calculate_completeness(
    registry: ObjectParameterRegistry,
    facts: list[ObjectFactValue],
) -> CompletenessReport:
    fact_map = {item.field_id: item for item in facts}
    unknown_ids = set(fact_map) - {item.field_id for item in registry.parameters}
    if unknown_ids:
        raise ValueError(f"unknown object facts: {sorted(unknown_ids)}")

    applicable = [
        item for item in registry.parameters if parameter_is_applicable(item, fact_map)
    ]
    total_weight = sum(item.weight for item in applicable)
    plan_weight = 0
    required_weight = sum(item.weight for item in applicable if item.required)
    resolved_required_weight = 0
    missing_required: list[str] = []
    missing_core: list[str] = []

    for parameter in applicable:
        fact = fact_map.get(parameter.field_id)
        from_plan = bool(
            fact
            and fact.source in {"document_explicit", "document_inferred"}
            and fact.state != "unknown"
        )
        resolved = bool(
            fact
            and fact.state
            in {"confirmed", "corrected", "not_present", "not_applicable"}
        )
        if from_plan:
            plan_weight += parameter.weight
        if resolved and parameter.required:
            resolved_required_weight += parameter.weight
        if parameter.required and not resolved:
            missing_required.append(parameter.field_id)
            if parameter.group == "core":
                missing_core.append(parameter.field_id)

    def percent(value: int) -> int:
        return round(100 * value / total_weight) if total_weight else 100

    readiness = (
        round(100 * resolved_required_weight / required_weight)
        if required_weight
        else 100
    )
    core_complete = not missing_core
    return CompletenessReport(
        plan_coverage_percent=percent(plan_weight),
        decision_readiness_percent=readiness,
        core_complete=core_complete,
        recommendation_ready=(
            core_complete
            and readiness >= registry.readiness_threshold_percent
            and not missing_required
        ),
        missing_core=missing_core,
        missing_required=missing_required,
    )


def validate_document(original_name: str, mime_type: str, data: bytes) -> str:
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ValueError("supported document types are PDF, PNG and JPEG")
    if not data:
        raise ValueError("document is empty")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("document exceeds the 25 MB limit")
    suffix = Path(original_name).suffix.lower()
    allowed_suffixes = {ALLOWED_MIME_TYPES[mime_type]}
    if mime_type == "image/jpeg":
        allowed_suffixes.add(".jpeg")
    if suffix not in allowed_suffixes:
        raise ValueError("file extension does not match its MIME type")
    return suffix


def store_document_locally(
    assessment_id: str,
    original_name: str,
    mime_type: str,
    data: bytes,
    *,
    root: Path = DOCUMENT_ROOT,
) -> StoredDocument:
    suffix = validate_document(original_name, mime_type, data)
    safe_assessment_id = re.sub(r"[^a-zA-Z0-9_-]", "_", assessment_id)[:64]
    if not safe_assessment_id:
        raise ValueError("assessment_id must contain a safe character")
    target_dir = root / safe_assessment_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid4().hex}{suffix}"
    target.write_bytes(data)
    return StoredDocument(
        original_name=Path(original_name).name,
        mime_type=mime_type,
        stored_path=str(target.resolve()),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
