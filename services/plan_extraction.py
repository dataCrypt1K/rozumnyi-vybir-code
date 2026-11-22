from __future__ import annotations

import base64
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from llm_client import extract_output_text, load_env_file
from services.object_intake import ObjectParameterRegistry, ParameterDefinition, validate_document
from services.pdf_plan_pages import (
    pdf_known_floor_count,
    pdf_page_is_floor_specific,
    render_selected_pdf_pages,
)


PLAN_EXTRACTION_CACHE_VERSION = "plan_extraction_v9"
DEFAULT_PLAN_EXTRACTION_CACHE = (
    Path(__file__).resolve().parents[1] / "storage" / "plan_extraction_cache"
)


class ExtractedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    entity_ref: str | None = None
    value: Any
    page_number: int | None = Field(default=None, ge=1)
    source_reference: str = Field(min_length=1, max_length=500)
    evidence_type: str = Field(
        pattern=r"^(explicit_text|explicit_symbol|visual_inference)$"
    )
    reliability_level: str = Field(pattern=r"^(low|medium|high)$")


class PlanExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[ExtractedCandidate]
    unresolved_field_ids: list[str]
    warnings: list[str]

    @field_validator("unresolved_field_ids")
    @classmethod
    def unresolved_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("unresolved field ids must be unique")
        return value


PLAN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string"},
                    "entity_ref": {"type": ["string", "null"]},
                    "value_json": {"type": "string"},
                    "page_number": {"type": ["integer", "null"], "minimum": 1},
                    "source_reference": {"type": "string", "minLength": 1},
                    "evidence_type": {
                        "type": "string",
                        "enum": ["explicit_text", "explicit_symbol", "visual_inference"],
                    },
                    "reliability_level": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                },
                "required": [
                    "field_id",
                    "entity_ref",
                    "value_json",
                    "page_number",
                    "source_reference",
                    "evidence_type",
                    "reliability_level"
                ],
                "additionalProperties": False,
            },
        },
        "unresolved_field_ids": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["candidates", "unresolved_field_ids", "warnings"],
    "additionalProperties": False,
}


def validate_plan_payload(
    payload: dict[str, Any], registry: ObjectParameterRegistry
) -> PlanExtraction:
    parameters = {item.field_id: item for item in registry.parameters}
    allowed_ids = set(parameters)
    candidates = []
    warnings = list(payload.get("warnings", []))
    invalid_value_ids = []
    for raw in payload.get("candidates", []):
        if raw.get("field_id") not in allowed_ids:
            raise ValueError(f"unknown extracted field_id: {raw.get('field_id')}")
        try:
            value = json.loads(raw["value_json"])
        except (KeyError, TypeError, json.JSONDecodeError):
            invalid_value_ids.append(raw["field_id"])
            continue
        if raw["field_id"] == "rooms" and isinstance(value, dict):
            if all(str(key).isdigit() and isinstance(name, str) for key, name in value.items()):
                value = list(value.values())
            elif all(isinstance(area, (int, float)) for area in value.values()):
                value = [
                    {"name": name, "area_m2": area}
                    for name, area in value.items()
                ]
        if not _value_matches_parameter(value, parameters[raw["field_id"]]):
            invalid_value_ids.append(raw["field_id"])
            continue
        candidate_payload = {
            key: item for key, item in raw.items() if key != "value_json"
        }
        candidate_payload["value"] = value
        try:
            candidates.append(ExtractedCandidate.model_validate(candidate_payload))
        except ValidationError:
            invalid_value_ids.append(raw["field_id"])

    if invalid_value_ids:
        warnings.append(
            "Некоректні значення з плану пропущено; уточніть ці параметри вручну: "
            + ", ".join(sorted(set(invalid_value_ids)))
            + "."
        )
    candidates, rejected_ids = _filter_unsupported_candidates(candidates)
    if rejected_ids:
        warnings.append(
            "Непідтверджені припущення з плану не використано: "
            + ", ".join(sorted(set(rejected_ids)))
            + ". Перевірте ці поля вручну."
        )

    unresolved = payload.get("unresolved_field_ids", [])
    invalid_unresolved = set(unresolved) - allowed_ids
    if invalid_unresolved:
        raise ValueError(f"unknown unresolved field ids: {sorted(invalid_unresolved)}")
    candidate_ids = {candidate.field_id for candidate in candidates}
    unresolved = [
        item.field_id
        for item in registry.parameters
        if item.field_id not in candidate_ids
    ]
    try:
        return PlanExtraction.model_validate(
            {
                "candidates": candidates,
                "unresolved_field_ids": unresolved,
                "warnings": warnings,
            }
        )
    except ValidationError as error:
        raise ValueError(f"invalid plan extraction response: {error}") from error


def _value_matches_parameter(value: Any, parameter: ParameterDefinition) -> bool:
    kind = parameter.value_type
    if kind == "boolean":
        return type(value) is bool
    if kind in {"number", "integer"}:
        if kind == "number":
            valid_type = type(value) in {int, float}
        else:
            valid_type = type(value) is int
        return (
            valid_type
            and math.isfinite(value)
            and (parameter.min is None or value >= parameter.min)
            and (parameter.max is None or value <= parameter.max)
        )
    if kind == "single_choice":
        return isinstance(value, str) and value in (parameter.options or [])
    if kind == "multi_choice":
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and item in (parameter.options or []) for item in value)
            and not ("none" in value and len(value) > 1)
        )
    if kind == "room_list":
        return (
            isinstance(value, list)
            and bool(value)
            and all(
                isinstance(room, str)
                or (isinstance(room, dict) and bool(room.get("name")))
                for room in value
            )
        )
    if kind == "location_count_map":
        return isinstance(value, dict) and bool(value)
    return False


def _room_name_text(value: Any) -> str:
    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, list):
        return " ".join(_room_name_text(item) for item in value)
    if isinstance(value, dict):
        names = [str(value.get("name", ""))]
        names.extend(str(key) for key in value if key not in {"name", "area", "area_m2"})
        return " ".join(names).casefold()
    return ""


def _source_contains_exact_area(value: int | float, source: str) -> bool:
    numbers = re.findall(r"(?<!\d)\d+(?:[.,]\d+)?(?!\d)", source)
    return any(
        math.isclose(float(number.replace(",", ".")), float(value), abs_tol=0.005)
        for number in numbers
    )


def _filter_unsupported_candidates(
    candidates: list[ExtractedCandidate],
) -> tuple[list[ExtractedCandidate], list[str]]:
    generic_sources = {
        "pdf", "file", "image", "floor_plan", "floor plan", "plan", "план", "креслення"
    }
    rooms_text = " ".join(
        _room_name_text(candidate.value)
        for candidate in candidates
        if candidate.field_id == "rooms"
    )
    non_residential = any(term in rooms_text for term in ("гараж", "терас", "garage", "terrace"))
    wet_rooms = any(term in rooms_text for term in ("санвуз", "ванн", "туалет", "душ", "bathroom", "toilet"))
    accepted = []
    rejected = []
    for candidate in candidates:
        source = candidate.source_reference.strip().casefold()
        is_generic = source in generic_sources or (
            source.startswith("plan_") and source.endswith((".pdf", ".png", ".jpg"))
        )
        area_ambiguous = candidate.field_id == "home_area_m2" and (
            "площа забудови" in source
            or "building footprint" in source
            or not _source_contains_exact_area(candidate.value, source)
            or (non_residential and not any(
                term in source for term in (
                    "житлова площа", "площа житла", "опалювана площа",
                    "residential area", "heated area",
                )
            ))
            or ("площею" in source and any(item.field_id == "rooms" for item in candidates))
        )
        wet_zone_conflict = (
            candidate.field_id == "water_zones"
            and candidate.value == 0
            and wet_rooms
        )
        stage_from_title = (
            candidate.field_id == "project_stage"
            and "фінальне планування" in source
        )
        negative_value = candidate.value is False or candidate.value == ["none"]
        absence_evidence = any(
            marker in source
            for marker in ("не передбач", "відсут", "немає", "без ", "not present", "absent", "none")
        )
        unsupported_absence = negative_value and not absence_evidence
        if is_generic or area_ambiguous or wet_zone_conflict or stage_from_title or unsupported_absence:
            rejected.append(candidate.field_id)
            continue
        if candidate.evidence_type == "visual_inference" and candidate.reliability_level == "high":
            candidate = candidate.model_copy(update={"reliability_level": "medium"})
        accepted.append(candidate)
    return accepted, rejected


def merge_page_extractions(
    page_results: list[tuple[int, PlanExtraction]],
    registry: ObjectParameterRegistry,
) -> PlanExtraction:
    if not page_results:
        raise ValueError("Немає проаналізованих сторінок PDF.")
    grouped: dict[str, list[ExtractedCandidate]] = {}
    labels = {item.field_id: item.label_uk for item in registry.parameters}
    warnings = [
        "Проаналізовано сторінки PDF: "
        + ", ".join(str(number) for number, _ in page_results)
        + "."
    ]
    for page_number, result in page_results:
        warnings.extend(f"Стор. {page_number}: {warning}" for warning in result.warnings)
        for candidate in result.candidates:
            grouped.setdefault(candidate.field_id, []).append(
                candidate.model_copy(update={"page_number": page_number})
            )

    accepted = []
    for parameter in registry.parameters:
        field_id = parameter.field_id
        items = grouped.get(field_id, [])
        if not items:
            continue
        if len(items) == 1:
            accepted.append(items[0])
            continue
        pages = sorted({item.page_number for item in items})
        page_label = ", ".join(str(number) for number in pages)
        if field_id == "rooms" and all(isinstance(item.value, list) for item in items):
            lists = [item.value for item in items]
            if all(value == lists[0] for value in lists[1:]):
                accepted.append(items[0])
                continue
            detailed = [item for item in items if _is_detailed_room_schedule(item)]
            sketches = [item for item in items if not _has_room_areas(item.value)]
            if len(items) == 2 and len(detailed) == 1 and len(sketches) == 1:
                schedule, sketch = detailed[0], sketches[0]
                shared_names = _room_names(schedule.value) & _room_names(sketch.value)
                if (
                    schedule is not sketch
                    and len(schedule.value) >= len(sketch.value) + 3
                    and len(shared_names) >= 2
                ):
                    accepted.append(schedule.model_copy(update={"reliability_level": "medium"}))
                    warnings.append(
                        f"{labels[field_id]}: повну експлікацію стор. {schedule.page_number} "
                        f"не дубльовано попереднім планом стор. {sketch.page_number}."
                    )
                    continue
            merged_rooms = list(items[0].value)
            for item in items[1:]:
                previous = Counter(
                    json.dumps(room, ensure_ascii=False, sort_keys=True)
                    for room in merged_rooms
                )
                current = Counter(
                    json.dumps(room, ensure_ascii=False, sort_keys=True)
                    for room in item.value
                )
                overlap = sum((previous & current).values())
                if overlap / min(len(merged_rooms), len(item.value)) < 0.8:
                    merged_rooms.extend(item.value)
                    continue
                for room in item.value:
                    signature = json.dumps(room, ensure_ascii=False, sort_keys=True)
                    if previous[signature] < current[signature]:
                        merged_rooms.append(room)
                        previous[signature] += 1
            source = "; ".join(
                f"стор. {item.page_number}: {item.source_reference}" for item in items
            )[:500]
            accepted.append(items[0].model_copy(update={
                "value": merged_rooms,
                "page_number": None,
                "source_reference": source,
                "reliability_level": "medium",
            }))
            warnings.append(
                f"{labels[field_id]}: об'єднано дані зі сторінок {page_label}; "
                "перевірте повтори та кількість приміщень."
            )
            continue
        if field_id in {"water_zones", "entrance_count"}:
            warnings.append(
                f"{labels[field_id]}: на сторінках {page_label} можуть бути часткові "
                "кількості; вкажіть загальне значення вручну."
            )
            continue
        first_value = json.dumps(items[0].value, ensure_ascii=False, sort_keys=True)
        if all(
            json.dumps(item.value, ensure_ascii=False, sort_keys=True) == first_value
            for item in items[1:]
        ):
            accepted.append(items[0])
        else:
            warnings.append(
                f"{labels[field_id]}: сторінки {page_label} суперечать одна одній; "
                "уточніть значення вручну."
            )
    accepted, rejected_ids = _filter_unsupported_candidates(accepted)
    if rejected_ids:
        warnings.append(
            "Після звірки сторінок відхилено непідтверджені факти: "
            + ", ".join(sorted(set(rejected_ids))) + "."
        )
    accepted = _check_room_area_consistency(accepted, warnings)
    accepted = _derive_water_zones_from_rooms(accepted, warnings)
    found = {item.field_id for item in accepted}
    return PlanExtraction(
        candidates=accepted,
        unresolved_field_ids=[
            item.field_id for item in registry.parameters if item.field_id not in found
        ],
        warnings=warnings,
    )


def _room_names(rooms: list[Any]) -> set[str]:
    return {
        str(room.get("name", "") if isinstance(room, dict) else room).strip().casefold()
        for room in rooms
    }


def _has_room_areas(rooms: list[Any]) -> bool:
    return bool(rooms) and all(
        isinstance(room, dict) and type(room.get("area_m2")) in {int, float}
        for room in rooms
    )


def _is_detailed_room_schedule(candidate: ExtractedCandidate) -> bool:
    source = candidate.source_reference.casefold()
    return _has_room_areas(candidate.value) and any(
        term in source for term in ("експлікац", "таблиця приміщень", "room schedule")
    )


def _check_room_area_consistency(
    candidates: list[ExtractedCandidate], warnings: list[str]
) -> list[ExtractedCandidate]:
    area = next((item for item in candidates if item.field_id == "home_area_m2"), None)
    rooms = next((item for item in candidates if item.field_id == "rooms"), None)
    if rooms is None:
        return candidates
    signatures = Counter(
        json.dumps(room, ensure_ascii=False, sort_keys=True) for room in rooms.value
    )
    duplicate_count = sum(count - 1 for count in signatures.values())
    if len(rooms.value) >= 10 and duplicate_count >= 3:
        warnings.append(
            "Перелік приміщень: на вибраних сторінках є повторні експлікації; "
            "оберіть один план кожного рівня або заповніть приміщення вручну."
        )
        return [item for item in candidates if item.field_id != "rooms"]
    if area is None:
        return candidates
    numeric_areas = [
        room["area_m2"] for room in rooms.value
        if isinstance(room, dict) and type(room.get("area_m2")) in {int, float}
    ]
    if not numeric_areas:
        return candidates
    listed_total = sum(numeric_areas)
    tolerance = max(0.5, area.value * 0.01)
    if listed_total <= area.value + tolerance and (
        len(numeric_areas) != len(rooms.value)
        or abs(listed_total - area.value) <= tolerance
    ):
        return candidates
    warnings.append(
        "Перелік приміщень: сума відомих площ не узгоджується із загальною "
        "площею; перевірте вибрані сторінки або заповніть приміщення вручну."
    )
    return [item for item in candidates if item.field_id != "rooms"]


def _derive_water_zones_from_rooms(
    candidates: list[ExtractedCandidate], warnings: list[str]
) -> list[ExtractedCandidate]:
    if any(item.field_id == "water_zones" for item in candidates):
        return candidates
    rooms = next((item for item in candidates if item.field_id == "rooms"), None)
    if rooms is None or not _is_detailed_room_schedule(rooms):
        return candidates
    wet_names = [
        str(room["name"]) for room in rooms.value
        if re.search(
            r"санвуз|ванн|туалет|душ|пральн|bath|toilet|shower|laundry|\bwc\b",
            str(room["name"]).casefold(),
        )
    ]
    if not wet_names:
        return candidates
    source = f"{rooms.source_reference}; водні приміщення: {', '.join(wet_names)}"
    candidates.append(ExtractedCandidate(
        field_id="water_zones",
        value=len(wet_names),
        page_number=rooms.page_number,
        source_reference=source[:500],
        evidence_type="visual_inference",
        reliability_level="medium",
    ))
    warnings.append(
        "Кількість водних зон підраховано за назвами приміщень; "
        "підтвердьте її перед розрахунком."
    )
    return candidates


def _exclude_floor_subtotal(
    extraction: PlanExtraction,
    registry: ObjectParameterRegistry,
) -> PlanExtraction:
    area_candidates = [
        item for item in extraction.candidates if item.field_id == "home_area_m2"
    ]
    if not area_candidates:
        return extraction
    whole_object_terms = ("будинку", "квартири", "житла", "об'єкта", "building", "apartment", "home")
    if any(
        ("загальн" in item.source_reference.casefold() or "total" in item.source_reference.casefold())
        and any(term in item.source_reference.casefold() for term in whole_object_terms)
        for item in area_candidates
    ):
        return extraction
    candidates = [
        item for item in extraction.candidates if item.field_id != "home_area_m2"
    ]
    found = {item.field_id for item in candidates}
    return PlanExtraction(
        candidates=candidates,
        unresolved_field_ids=[
            item.field_id for item in registry.parameters if item.field_id not in found
        ],
        warnings=[
            *extraction.warnings,
            "Площа окремого поверху не прийнята як площа всього житла.",
        ],
    )


class MockPlanExtractor:
    def extract(
        self,
        file_name: str,
        mime_type: str,
        data: bytes,
        registry: ObjectParameterRegistry,
    ) -> PlanExtraction:
        del file_name, mime_type, data
        return PlanExtraction(
            candidates=[],
            unresolved_field_ids=[item.field_id for item in registry.parameters],
            warnings=[
                "Демонстраційний режим не аналізує креслення. Заповніть дані вручну."
            ],
        )


class OpenAIPlanExtractor:
    def __init__(self, config: dict[str, Any]) -> None:
        load_env_file()
        self.endpoint = config["endpoint"]
        self.model = (
            os.getenv("OPENAI_PLAN_MODEL")
            or config.get("plan_model")
            or os.getenv("OPENAI_MODEL")
            or config["model"]
        )
        self.timeout_seconds = int(config.get("timeout_seconds", 60))
        self.max_attempts = max(1, int(config.get("max_attempts", 2)))
        self.max_output_tokens = int(config.get("max_output_tokens", 6000))
        self.temperature = float(config.get("temperature", 0))
        self.retry_delay_seconds = float(config.get("retry_delay_seconds", 0.5))
        self.store = bool(config.get("store", False))
        self.cache_enabled = bool(config.get("cache_enabled", True))
        self.cache_dir = Path(
            config.get("cache_dir", DEFAULT_PLAN_EXTRACTION_CACHE)
        )
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key or self.api_key == "PASTE_YOUR_KEY_HERE":
            raise RuntimeError("OPENAI_API_KEY is not configured")

    def _cache_path(
        self, data: bytes, registry: ObjectParameterRegistry
    ) -> Path:
        digest = hashlib.sha256()
        digest.update(data)
        digest.update(self.model.encode("utf-8"))
        digest.update(registry.registry_id.encode("utf-8"))
        digest.update(registry.schema_version.encode("utf-8"))
        digest.update(str(self.temperature).encode("utf-8"))
        digest.update(PLAN_EXTRACTION_CACHE_VERSION.encode("utf-8"))
        return self.cache_dir / f"{digest.hexdigest()}.json"

    def _load_cached_extraction(
        self, data: bytes, registry: ObjectParameterRegistry
    ) -> PlanExtraction | None:
        if not self.cache_enabled:
            return None
        path = self._cache_path(data, registry)
        if not path.exists():
            return None
        try:
            return PlanExtraction.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            return None

    def _store_cached_extraction(
        self,
        data: bytes,
        registry: ObjectParameterRegistry,
        extraction: PlanExtraction,
    ) -> None:
        if not self.cache_enabled:
            return
        path = self._cache_path(data, registry)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)

    def build_request(
        self,
        file_name: str,
        mime_type: str,
        data: bytes,
        registry: ObjectParameterRegistry,
    ) -> dict[str, Any]:
        validate_document(file_name, mime_type, data)
        schema = deepcopy(PLAN_OUTPUT_SCHEMA)
        allowed_ids = [item.field_id for item in registry.parameters]
        schema["properties"]["candidates"]["items"]["properties"]["field_id"] = {
            "type": "string",
            "enum": allowed_ids,
        }
        schema["properties"]["unresolved_field_ids"]["items"] = {
            "type": "string",
            "enum": allowed_ids,
        }
        encoded = base64.b64encode(data).decode("ascii")
        if mime_type == "application/pdf":
            document_part = {
                "type": "input_file",
                "filename": Path(file_name).name,
                "file_data": f"data:{mime_type};base64,{encoded}",
                "detail": "high",
            }
        else:
            document_part = {
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{encoded}",
                "detail": "high",
            }
        registry_prompt = [
            {
                "field_id": item.field_id,
                "label": item.label_uk,
                "value_type": item.value_type,
                "options": item.options,
            }
            for item in registry.parameters
        ]
        instructions = (
            "Проаналізуй план житла як джерело кандидатних фактів. Не вигадуй "
            "відсутні значення. Не роби висновків про мешканців, бюджет, дохід, "
            "характер чи вподобання. Кожен allowed_parameter повинен з'явитися рівно "
            "в одному місці: або в candidates, якщо значення має прямий текстовий, символьний "
            "або візуальний доказ у файлі, або в unresolved_field_ids. Відсутність позначки не є доказом false. "
            "Не виводь значення одного параметра з іншого або із загальних знань про квартири чи будинки. "
            "Явно відрізняй текст/символ від візуального припущення. "
            "Для кожного факту наведи в source_reference конкретний напис або позначення, а не просто PDF/план. "
            "Для home_area_m2 наведи в source_reference точне числове значення площі з напису на кресленні. "
            "Для rooms використовуй JSON-список назв або об'єктів з name та area_m2, не словник номерів кімнат. "
            "Не плутай площу житла з площею забудови, площею окремого поверху або підсумком, що включає гараж чи тераси; "
            "коли це неоднозначно, залиш home_area_m2 невизначеним. Назва 'фінальне планування' не доводить, що ремонт завершено. "
            "value_json має бути коректним JSON-рядком."
        )
        return {
            "model": self.model,
            "instructions": instructions,
            "input": [
                {
                    "role": "user",
                    "content": [
                        document_part,
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                {"allowed_parameters": registry_prompt},
                                ensure_ascii=False,
                            ),
                        },
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "smart_home_plan_facts",
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "store": self.store,
        }

    @staticmethod
    def _http_error_message(error: urllib.error.HTTPError) -> str:
        details = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(details)
            api_error = payload.get("error", {})
            message = str(api_error.get("message") or "невідома помилка API")
            code = api_error.get("code") or api_error.get("type")
        except (json.JSONDecodeError, AttributeError):
            message = "сервер не повернув зрозумілий опис"
            code = None
        suffix = f" ({code})" if code else ""
        return f"OpenAI API: {message}{suffix}; HTTP {error.code}"

    def extract(
        self,
        file_name: str,
        mime_type: str,
        data: bytes,
        registry: ObjectParameterRegistry,
    ) -> PlanExtraction:
        validate_document(file_name, mime_type, data)
        cached = self._load_cached_extraction(data, registry)
        if cached is not None:
            return cached
        payload = self.build_request(file_name, mime_type, data, registry)
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        response_payload: dict[str, Any] | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    response_payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as error:
                retryable = error.code == 429 or error.code >= 500
                if not retryable or attempt == self.max_attempts:
                    raise RuntimeError(self._http_error_message(error)) from error
            except urllib.error.URLError as error:
                if attempt == self.max_attempts:
                    raise RuntimeError(
                        "Не вдалося з'єднатися з OpenAI API. Перевірте інтернет "
                        f"і повторіть спробу: {error.reason}"
                    ) from error
            if self.retry_delay_seconds:
                time.sleep(self.retry_delay_seconds * attempt)

        if response_payload is None:
            raise RuntimeError("OpenAI API не повернув відповідь.")
        if response_payload.get("status") == "incomplete":
            reason = (
                response_payload.get("incomplete_details", {}).get("reason")
                or "невідома причина"
            )
            raise RuntimeError(
                f"Аналіз плану не завершено ({reason}). Спробуйте менший файл або повторіть запит."
            )
        output_text = extract_output_text(response_payload)
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise ValueError("plan extraction output is not valid JSON") from error
        extraction = validate_plan_payload(parsed, registry)
        self._store_cached_extraction(data, registry, extraction)
        return extraction

    def extract_selected_pdf_pages(
        self,
        file_name: str,
        data: bytes,
        registry: ObjectParameterRegistry,
        page_numbers: list[int],
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> PlanExtraction:
        validate_document(file_name, "application/pdf", data)
        pages = render_selected_pdf_pages(data, page_numbers)
        results = []
        for index, (page_number, image_data) in enumerate(pages, 1):
            if on_progress:
                on_progress(index, len(pages), page_number)
            extraction = self.extract(
                f"plan_page_{page_number}.png", "image/png", image_data, registry
            )
            if pdf_page_is_floor_specific(data, page_number):
                extraction = _exclude_floor_subtotal(extraction, registry)
            results.append((page_number, extraction))
        merged = merge_page_extractions(results, registry)
        floor_evidence = pdf_known_floor_count(data, page_numbers)
        if floor_evidence is not None and "floor_count" in merged.unresolved_field_ids:
            count, source_pages = floor_evidence
            candidate = ExtractedCandidate(
                field_id="floor_count",
                value=count,
                page_number=None,
                source_reference=(
                    "Плани першого і другого поверхів на сторінках "
                    + ", ".join(map(str, source_pages))
                ),
                evidence_type="explicit_text",
                reliability_level="medium",
            )
            merged = merged.model_copy(update={
                "candidates": [*merged.candidates, candidate],
                "unresolved_field_ids": [
                    field_id for field_id in merged.unresolved_field_ids
                    if field_id != "floor_count"
                ],
                "warnings": [
                    *merged.warnings,
                    "Два рівні визначено за заголовками планів; підтвердьте кількість рівнів.",
                ],
            })
        return merged


def build_plan_extractor(provider: str, config: dict[str, Any]):
    if provider == "mock":
        return MockPlanExtractor()
    if provider == "openai":
        return OpenAIPlanExtractor(config)
    raise ValueError(f"unsupported plan extraction provider: {provider}")
