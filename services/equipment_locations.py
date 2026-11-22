from __future__ import annotations

from collections import defaultdict
from typing import Mapping

from domain.enums import LocationKind, RoomType
from domain.models import Room


EQUIPMENT_ROOM_TYPES: dict[str, frozenset[RoomType]] = {
    "underfloor_heating": frozenset({
        RoomType.BATHROOM, RoomType.WC, RoomType.KITCHEN, RoomType.KITCHEN_LIVING_ROOM,
        RoomType.HALLWAY, RoomType.LIVING_ROOM, RoomType.DINING_ROOM,
        RoomType.BEDROOM, RoomType.GUEST_ROOM,
        RoomType.CHILDRENS_ROOM, RoomType.OFFICE, RoomType.OTHER,
    }),
    "radiators": frozenset({
        RoomType.LIVING_ROOM, RoomType.DINING_ROOM, RoomType.KITCHEN,
        RoomType.KITCHEN_LIVING_ROOM, RoomType.BEDROOM, RoomType.GUEST_ROOM,
        RoomType.CHILDRENS_ROOM, RoomType.OFFICE,
        RoomType.HALLWAY, RoomType.OTHER,
    }),
    "cooling": frozenset({
        RoomType.LIVING_ROOM, RoomType.DINING_ROOM, RoomType.KITCHEN,
        RoomType.KITCHEN_LIVING_ROOM, RoomType.BEDROOM, RoomType.GUEST_ROOM,
        RoomType.CHILDRENS_ROOM, RoomType.OFFICE,
        RoomType.OTHER,
    }),
    "exhaust": frozenset({
        RoomType.BATHROOM, RoomType.WC, RoomType.LAUNDRY,
        RoomType.UTILITY_ROOM, RoomType.OTHER,
    }),
}

FUNCTION_EQUIPMENT_KEYS = {
    "climate.underfloor_heating_control": "underfloor_heating",
    "climate.radiator_zone_control": "radiators",
    "climate.cooling_zone_control": "cooling",
    "ventilation.exhaust_fan_control": "exhaust",
}


def room_slot(room_type: RoomType | str, ordinal: int) -> str:
    return f"{RoomType(room_type).value}:{ordinal}"


def room_slots(rooms: list[Room]) -> dict[str, str]:
    seen: dict[RoomType, int] = defaultdict(int)
    slots: dict[str, str] = {}
    for room in rooms:
        if room.location_kind != LocationKind.ROOM:
            continue
        seen[room.room_type] += 1
        slots[room.room_id] = room_slot(room.room_type, seen[room.room_type])
    return slots


def confirmed_equipment_slots(
    answers: Mapping[str, object], equipment_key: str
) -> set[str]:
    values = answers.get("equipment_room_slots")
    if not isinstance(values, dict):
        return set()
    selected = values.get(equipment_key)
    if not isinstance(selected, list):
        return set()
    return {value for value in selected if isinstance(value, str)}
