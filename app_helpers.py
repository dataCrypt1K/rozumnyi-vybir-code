from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / "storage" / "history.jsonl"

STEP_SECTIONS = (
    {"housing", "household", "lifestyle", "priorities", "preferences"},
    {"climate", "lighting", "air_quality", "shading", "multimedia"},
    {"security", "access", "engineering_safety"},
    {"power", "engineering", "outdoor"},
)

HOUSE_ONLY_QUESTIONS = {
    "private_plot",
    "gate_garage_present",
    "outdoor_ice_risk",
    "pool_present",
    "irrigation_present",
    "septic_present",
    "boiler_room_present",
    "weather_sensitive_elements",
    "weather_automation_interest",
    "visitor_access_interest",
    "keyless_access_interest",
    "lock_tamper_interest",
    "perimeter_detection_interest",
}

PLOT_QUESTIONS = {
    "outdoor_ice_risk",
    "pool_present",
    "irrigation_present",
    "septic_present",
    "weather_sensitive_elements",
    "weather_automation_interest",
    "perimeter_detection_interest",
}

OPTION_LABELS = {
    "apartment": "Квартира",
    "house": "Приватний будинок",
    "planning": "Проєктування",
    "renovation": "Ремонт",
    "completed": "Готовий ремонт",
    "reconstruction": "Реконструкція",
    "almost_never": "Майже ніколи",
    "rarely": "Рідко",
    "often": "Часто",
    "extended": "На тривалий час",
    "underfloor_heating": "Тепла підлога",
    "radiators": "Радіатори",
    "convectors": "Конвектори",
    "fan_coils": "Фанкойли",
    "boiler": "Котел",
    "air_conditioners": "Кондиціонери",
    "central_cooling": "Центральне охолодження",
    "other": "Інше",
    "none": "Немає",
    "one_to_three": "1–3 групи",
    "four_to_eight": "4–8 груп",
    "nine_or_more": "9 або більше",
    "exhaust": "Витяжна вентиляція",
    "supply_exhaust": "Припливно-витяжна вентиляція",
    "heat_recovery": "Рекуперація",
    "humidifier": "Зволоження",
    "gas_stove": "Газова плита",
    "gas_boiler": "Газовий котел",
    "fireplace": "Камін",
    "solid_fuel_boiler": "Твердопаливний котел",
    "generator": "Генератор",
    "never": "Ніколи",
    "monthly": "Щомісяця",
    "weekly": "Щотижня",
    "daily": "Щодня",
    "under_one_hour": "До 1 години",
    "one_to_four_hours": "1–4 години",
    "four_to_eight_hours": "4–8 годин",
    "over_eight_hours": "Понад 8 годин",
    "inverter": "Інвертор",
    "battery": "Акумулятори",
    "internet": "Інтернет",
    "lighting": "Освітлення",
    "heating": "Опалення",
    "water_pumps": "Водяні насоси",
    "refrigerator": "Холодильник",
    "security": "Охорона",
    "medical_equipment": "Медичне обладнання",
    "sometimes": "Іноді",
    "minor": "Було незначне протікання",
    "serious": "Було серйозне протікання",
    "low_impact": "Майже не впливає",
    "internet_or_light": "Зникає інтернет або освітлення",
    "engineering_stops": "Зупиняється опалення, вода або безпека",
    "critical_loads": "Є критичні медичні або робочі навантаження",
    "zero": "У жодному",
    "one": "В одному",
    "two_three": "У 2–3 приміщеннях",
    "four_plus": "У 4 або більше",
    "manual": "Переважно вручну",
    "assisted": "Автоматика пропонує, я підтверджую",
    "automatic": "Типові дії автоматично з можливістю втручання",
    "nowhere": "Ніде",
    "entrance_only": "Лише біля входу",
    "exterior": "На вході та у зовнішніх зонах",
    "limited_indoor": "Також в окремих внутрішніх технічних зонах",
    "sensor_notification": "Достатньо сповіщення від датчика",
    "entrance_visitor": "Хочу бачити лише відвідувача біля входу",
    "property_approaches": "Хочу бачити територію або підходи до будинку",
    "selected_zones": "Хочу бачити декілька вибраних зон",
    "basic_broad": "Базові функції в більшості кімнат",
    "balanced": "Збалансований варіант",
    "advanced_core": "Розширені функції в основних кімнатах",
    "minimum": "Мінімум для вирішення задачі",
    "balance": "Баланс можливостей і складності",
    "headroom": "Запас можливостей для розвитку",
    "critical_and_prepare": "Критичне зараз, решта як підготовка",
    "core_then_later": "Основне зараз, решта пізніше",
    "all_recommended": "Усе рекомендоване одразу",
}

STATUS_LABELS = {
    "recommended": "Рекомендована",
    "optional": "Опціональна",
    "inexpedient": "Недоцільна",
    "not_applicable": "Не застосовна",
}

COST_LABELS = {"low": "Низька", "medium": "Середня", "high": "Висока"}


def option_label(value: Any) -> str:
    return OPTION_LABELS.get(value, str(value).replace("_", " ").capitalize())


def plan_area_m2(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        area = float(str(value).strip().replace(",", "."))
    except ValueError:
        return None
    return area if math.isfinite(area) and 10 <= area <= 10000 else None


def question_is_visible(question: dict[str, Any], answers: dict[str, Any]) -> bool:
    question_id = question["id"]
    property_type = answers.get("property_type")
    if question_id in HOUSE_ONLY_QUESTIONS and property_type != "house":
        return False
    if question_id in PLOT_QUESTIONS and answers.get("private_plot") is False:
        return False
    if question_id == "shading_automation_interest" and answers.get("motorized_shading_present") is False:
        return False
    return True


def questions_for_step(
    questions: list[dict[str, Any]], step: int, answers: dict[str, Any]
) -> list[dict[str, Any]]:
    sections = STEP_SECTIONS[step]
    return [
        question
        for question in questions
        if question["section"] in sections and question_is_visible(question, answers)
    ]


def normalize_multi_choice(values: list[str]) -> list[str]:
    if "none" in values and len(values) > 1:
        return [item for item in values if item != "none"]
    return values


def validate_step(questions: list[dict[str, Any]], answers: dict[str, Any]) -> list[str]:
    errors = []
    for question in questions:
        if not question.get("required"):
            continue
        value = answers.get(question["id"])
        if value is None or value == "" or value == []:
            errors.append(question["text"])
    return errors


def save_history(
    answers: dict[str, Any],
    result: dict[str, Any],
    *,
    record_id: str | None = None,
) -> str:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    record_id = record_id or uuid4().hex[:12]
    record = {
        "record_id": record_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "answers": answers,
        "result": result,
    }
    with HISTORY_PATH.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record_id


def load_history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    records = []
    with HISTORY_PATH.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(records))


def implementation_phases(
    recommendations: list[dict[str, Any]], budget_level: str
) -> dict[str, list[str]]:
    applicable = [
        item for item in recommendations if item["status"] != "not_applicable"
    ]
    recommended = sorted(
        (item for item in applicable if item["status"] == "recommended"),
        key=lambda item: item.get("suitability_percent") or 0,
        reverse=True,
    )
    optional = sorted(
        (item for item in applicable if item["status"] == "optional"),
        key=lambda item: item.get("suitability_percent") or 0,
        reverse=True,
    )
    inexpedient = [item for item in applicable if item["status"] == "inexpedient"]

    first_limit = {"basic": 3, "balanced": 6, "flexible": len(recommended)}[budget_level]
    second_optional_limit = {"basic": 1, "balanced": 3, "flexible": len(optional)}[budget_level]
    first = recommended[:first_limit]
    second = recommended[first_limit:] + optional[:second_optional_limit]
    later = optional[second_optional_limit:] + inexpedient
    return {
        "first": [item["system_id"] for item in first],
        "second": [item["system_id"] for item in second],
        "later": [item["system_id"] for item in later],
    }
