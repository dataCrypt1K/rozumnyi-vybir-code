from __future__ import annotations

from collections import Counter
import re
from typing import Any

from domain.enums import LocationKind, PropertyType, RoomType
from services.object_step_service import available_location_types, location_label


def allowed_room_types(property_type: PropertyType | str) -> list[RoomType]:
    return [
        item.room_type
        for item in available_location_types(property_type, kind=LocationKind.ROOM)
    ]


def suggested_room_type(name: str, property_type: PropertyType | str) -> str | None:
    normalized = re.sub(r"[^\w]+", " ", name.casefold()).strip()
    words = set(normalized.split())
    matches: set[RoomType] = set()
    patterns = (
        (RoomType.KITCHEN, r"кух|kitchen"),
        (RoomType.LIVING_ROOM, r"віталь|гостин|living"),
        (RoomType.DINING_ROOM, r"їдаль|dining"),
        (RoomType.CHILDRENS_ROOM, r"дитяч|children|kids"),
        (RoomType.BEDROOM, r"спаль|bedroom"),
        (RoomType.GUEST_ROOM, r"гостьов|guest"),
        (RoomType.BATHROOM, r"ванн|bathroom"),
        (RoomType.WC, r"санвуз|туалет|\bwc\b|\bсв\b"),
        (RoomType.HALLWAY, r"передпок|коридор|хол|тамбур|hall"),
        (RoomType.OFFICE, r"кабінет|office"),
        (RoomType.WARDROBE, r"гардероб|wardrobe"),
        (RoomType.LAUNDRY, r"пральн|laundry"),
        (RoomType.PANTRY, r"комора|pantry"),
        (RoomType.STORAGE_ROOM, r"склад|зберіган|storage"),
        (RoomType.UTILITY_ROOM, r"господар|utility"),
        (RoomType.BALCONY, r"балкон|balcony"),
        (RoomType.LOGGIA, r"лоджі|loggia"),
        (RoomType.TERRACE, r"терас|terrace"),
        (RoomType.BOILER_ROOM, r"котельн|котел|boiler"),
        (RoomType.TECHNICAL_ROOM, r"техніч|\bтех\b|technical"),
        (RoomType.GARAGE, r"гараж|garage"),
        (RoomType.STAIRCASE, r"сход|stair"),
        (RoomType.ATTIC, r"горищ|attic"),
        (RoomType.BASEMENT, r"підвал|basement"),
        (RoomType.WORKSHOP, r"майстерн|workshop"),
        (RoomType.GYM, r"спортзал|тренажер|gym"),
        (RoomType.CINEMA_ROOM, r"кінотеатр|cinema"),
        (RoomType.SAUNA, r"саун|хамам|sauna"),
        (RoomType.POOL_ROOM, r"басейн|pool"),
    )
    for room_type, pattern in patterns:
        if re.search(pattern, normalized):
            matches.add(room_type)
    if matches == {RoomType.KITCHEN, RoomType.LIVING_ROOM}:
        matches = {RoomType.KITCHEN_LIVING_ROOM}
    elif matches == {RoomType.CHILDRENS_ROOM, RoomType.BEDROOM}:
        matches = {RoomType.CHILDRENS_ROOM}
    elif matches == {RoomType.GUEST_ROOM, RoomType.BEDROOM}:
        matches = {RoomType.GUEST_ROOM}
    elif matches == {RoomType.GUEST_ROOM, RoomType.WC}:
        matches = {RoomType.WC}
    elif matches == {RoomType.GUEST_ROOM, RoomType.BATHROOM}:
        matches = {RoomType.BATHROOM}
    if words == {"св"}:
        matches = {RoomType.WC}
    if "/" in name and matches != {RoomType.KITCHEN_LIVING_ROOM}:
        return None
    if len(matches) != 1:
        return None
    selected = next(iter(matches))
    return selected.value if selected in allowed_room_types(property_type) else None


def _area(value: Any) -> float | None:
    try:
        area = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return area if 0 < area <= 10000 else None


def room_drafts_from_candidate(
    value: Any, property_type: PropertyType | str
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    allowed = {item.value for item in allowed_room_types(property_type)}
    drafts = []
    for item in value:
        if isinstance(item, str):
            name, raw_type, area, count = item.strip(), None, None, 1
        elif isinstance(item, dict):
            raw_type = item.get("type")
            name = str(item.get("name") or "").strip()
            if not name and raw_type in allowed:
                name = location_label(raw_type)
            area = _area(item.get("area_m2"))
            try:
                count = int(item.get("count", 1))
            except (TypeError, ValueError):
                count = 1
        else:
            continue
        if not name:
            continue
        selected_type = (
            raw_type if raw_type in allowed else suggested_room_type(name, property_type)
        )
        for _ in range(max(0, min(count, 100))):
            drafts.append({
                "id": f"plan_{len(drafts)}",
                "name": name,
                "type": selected_type,
                "area_m2": area,
                "origin": "plan",
            })
    return drafts


def default_room_drafts() -> list[dict[str, Any]]:
    return [
        {
            "id": f"manual_{index}",
            "name": location_label(room_type),
            "type": room_type.value,
            "area_m2": None,
            "origin": "manual",
        }
        for index, room_type in enumerate(
            (RoomType.LIVING_ROOM, RoomType.KITCHEN, RoomType.BATHROOM)
        )
    ]


def confirmed_room_details(
    drafts: list[dict[str, Any]], property_type: PropertyType | str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not drafts:
        raise ValueError("Додайте хоча б одне приміщення.")
    allowed = {item.value for item in allowed_room_types(property_type)}
    counts: Counter[str] = Counter()
    names: Counter[str] = Counter()
    for index, item in enumerate(drafts, 1):
        name = str(item.get("name") or "").strip()
        room_type = item.get("type")
        if not name or len(name) > 120:
            raise ValueError(f"Приміщення {index}: вкажіть назву до 120 символів.")
        if room_type not in allowed:
            raise ValueError(f"Приміщення {index} ({name}): оберіть тип.")
        area = item.get("area_m2")
        if area is not None and _area(area) is None:
            raise ValueError(f"Приміщення {index} ({name}): перевірте площу.")
        counts[room_type] += 1
        names[name] += 1
    seen: Counter[str] = Counter()
    details = []
    for item in drafts:
        name = str(item["name"]).strip()
        seen[name] += 1
        if names[name] > 1:
            name = f"{name} {seen[name]}"
        details.append({
            "name": name,
            "type": item["type"],
            "area_m2": _area(item.get("area_m2")),
            "origin": item.get("origin", "manual"),
        })
    aggregate = [{"type": key, "count": count} for key, count in counts.items()]
    return aggregate, details
