from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Callable, Mapping, Protocol

from pydantic import Field, field_validator, model_validator

from domain.enums import (
    EvidenceSource,
    LocationKind,
    PropertyType,
    RecommendationStatus,
    ReliabilityLevel,
    RoomType,
    WorkflowStage,
)
from domain.models import Assessment, DomainModel, RecommendationSnapshot, Room, new_id


@dataclass(frozen=True)
class LocationTypeDefinition:
    room_type: RoomType
    name_uk: str
    kind: LocationKind
    property_types: frozenset[PropertyType]


BOTH = frozenset({PropertyType.APARTMENT, PropertyType.HOUSE})
HOUSE_ONLY = frozenset({PropertyType.HOUSE})


LOCATION_TYPES: tuple[LocationTypeDefinition, ...] = (
    LocationTypeDefinition(RoomType.LIVING_ROOM, "Вітальня", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.KITCHEN, "Кухня", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(
        RoomType.KITCHEN_LIVING_ROOM, "Кухня-вітальня", LocationKind.ROOM, BOTH
    ),
    LocationTypeDefinition(RoomType.DINING_ROOM, "Їдальня", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.BEDROOM, "Спальня", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.GUEST_ROOM, "Гостьова", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(
        RoomType.CHILDRENS_ROOM, "Дитяча кімната", LocationKind.ROOM, BOTH
    ),
    LocationTypeDefinition(RoomType.BATHROOM, "Ванна кімната", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.WC, "Санвузол", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.HALLWAY, "Коридор / хол", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.OFFICE, "Кабінет", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.WARDROBE, "Гардеробна", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.LAUNDRY, "Пральня", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.PANTRY, "Комора", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(
        RoomType.STORAGE_ROOM, "Приміщення зберігання", LocationKind.ROOM, BOTH
    ),
    LocationTypeDefinition(
        RoomType.UTILITY_ROOM, "Господарське приміщення", LocationKind.ROOM, BOTH
    ),
    LocationTypeDefinition(RoomType.BALCONY, "Балкон", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.LOGGIA, "Лоджія", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.TERRACE, "Тераса", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(RoomType.BOILER_ROOM, "Котельня", LocationKind.ROOM, HOUSE_ONLY),
    LocationTypeDefinition(
        RoomType.TECHNICAL_ROOM, "Технічне приміщення", LocationKind.ROOM, HOUSE_ONLY
    ),
    LocationTypeDefinition(RoomType.GARAGE, "Гараж", LocationKind.ROOM, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.STAIRCASE, "Сходи", LocationKind.ROOM, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.ATTIC, "Горище", LocationKind.ROOM, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.BASEMENT, "Підвал", LocationKind.ROOM, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.WORKSHOP, "Майстерня", LocationKind.ROOM, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.GYM, "Спортзал", LocationKind.ROOM, HOUSE_ONLY),
    LocationTypeDefinition(
        RoomType.CINEMA_ROOM, "Домашній кінотеатр", LocationKind.ROOM, HOUSE_ONLY
    ),
    LocationTypeDefinition(RoomType.SAUNA, "Сауна", LocationKind.ROOM, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.POOL_ROOM, "Басейн", LocationKind.ROOM, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.OTHER, "Інше приміщення", LocationKind.ROOM, BOTH),
    LocationTypeDefinition(
        RoomType.WHOLE_PROPERTY, "Весь об'єкт", LocationKind.SPECIAL, BOTH
    ),
    LocationTypeDefinition(RoomType.ENTRANCE, "Вхідна зона", LocationKind.SPECIAL, BOTH),
    LocationTypeDefinition(
        RoomType.ELECTRICAL_PANEL, "Електричний щит", LocationKind.SPECIAL, BOTH
    ),
    LocationTypeDefinition(
        RoomType.WATER_ENTRY, "Ввід води", LocationKind.SPECIAL, BOTH
    ),
    LocationTypeDefinition(RoomType.PLOT, "Ділянка", LocationKind.SPECIAL, HOUSE_ONLY),
    LocationTypeDefinition(
        RoomType.PERIMETER, "Периметр ділянки", LocationKind.SPECIAL, HOUSE_ONLY
    ),
    LocationTypeDefinition(RoomType.DRIVEWAY, "Заїзд", LocationKind.SPECIAL, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.GATE, "Ворота", LocationKind.SPECIAL, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.WICKET, "Хвіртка", LocationKind.SPECIAL, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.GARDEN, "Сад", LocationKind.SPECIAL, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.LAWN, "Газон", LocationKind.SPECIAL, HOUSE_ONLY),
    LocationTypeDefinition(
        RoomType.OUTDOOR_PARKING, "Зовнішнє паркування", LocationKind.SPECIAL, HOUSE_ONLY
    ),
    LocationTypeDefinition(RoomType.ROOF, "Покрівля", LocationKind.SPECIAL, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.GUTTER, "Водостоки", LocationKind.SPECIAL, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.WELL, "Свердловина / колодязь", LocationKind.SPECIAL, HOUSE_ONLY),
    LocationTypeDefinition(RoomType.SEPTIC, "Септик", LocationKind.SPECIAL, HOUSE_ONLY),
)


LOCATION_TYPE_MAP = {item.room_type: item for item in LOCATION_TYPES}


def available_location_types(
    property_type: PropertyType | str,
    *,
    kind: LocationKind | None = None,
) -> list[LocationTypeDefinition]:
    normalized = PropertyType(property_type)
    return [
        item
        for item in LOCATION_TYPES
        if normalized in item.property_types and (kind is None or item.kind == kind)
    ]


def location_label(room_type: RoomType | str) -> str:
    return LOCATION_TYPE_MAP[RoomType(room_type)].name_uk


def default_location_counts(property_type: PropertyType | str) -> dict[RoomType, int]:
    normalized = PropertyType(property_type)
    if normalized == PropertyType.APARTMENT:
        return {
            RoomType.KITCHEN_LIVING_ROOM: 1,
            RoomType.BEDROOM: 1,
            RoomType.BATHROOM: 1,
            RoomType.HALLWAY: 1,
            RoomType.ENTRANCE: 1,
            RoomType.ELECTRICAL_PANEL: 1,
            RoomType.WATER_ENTRY: 1,
            RoomType.WHOLE_PROPERTY: 1,
        }
    return {
        RoomType.LIVING_ROOM: 1,
        RoomType.KITCHEN: 1,
        RoomType.BEDROOM: 2,
        RoomType.BATHROOM: 2,
        RoomType.HALLWAY: 1,
        RoomType.BOILER_ROOM: 1,
        RoomType.ENTRANCE: 1,
        RoomType.ELECTRICAL_PANEL: 1,
        RoomType.WATER_ENTRY: 1,
        RoomType.WHOLE_PROPERTY: 1,
    }


def reconcile_rooms(
    property_type: PropertyType | str,
    counts: Mapping[RoomType | str, int],
    existing_rooms: list[Room] | None = None,
    *,
    id_factory: Callable[[], str] = new_id,
) -> list[Room]:
    normalized_property = PropertyType(property_type)
    allowed = {
        item.room_type: item for item in available_location_types(normalized_property)
    }
    normalized_counts: dict[RoomType, int] = {}
    for raw_type, raw_count in counts.items():
        room_type = RoomType(raw_type)
        if room_type not in allowed:
            raise ValueError(
                f"location type {room_type.value} is unavailable for {normalized_property.value}"
            )
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
            raise ValueError("location counts must be non-negative integers")
        if raw_count:
            normalized_counts[room_type] = raw_count

    existing_by_type: dict[RoomType, list[Room]] = {}
    for room in existing_rooms or []:
        if room.room_type in allowed:
            existing_by_type.setdefault(room.room_type, []).append(room)

    reconciled: list[Room] = []
    sort_order = 0
    for definition in available_location_types(normalized_property):
        requested_count = normalized_counts.get(definition.room_type, 0)
        previous = existing_by_type.get(definition.room_type, [])
        for index in range(requested_count):
            if index < len(previous):
                item = previous[index].model_copy(
                    update={
                        "location_kind": definition.kind,
                        "sort_order": sort_order,
                    }
                )
            else:
                suffix = f" {index + 1}" if requested_count > 1 else ""
                item = Room(
                    room_id=id_factory(),
                    name=f"{definition.name_uk}{suffix}",
                    room_type=definition.room_type,
                    location_kind=definition.kind,
                    floor=1 if definition.kind == LocationKind.ROOM else None,
                    source=EvidenceSource.MANUAL,
                    reliability_level=ReliabilityLevel.HIGH,
                    sort_order=sort_order,
                )
            reconciled.append(item)
            sort_order += 1
    return reconciled


class ObjectStepDraft(DomainModel):
    property_type: PropertyType
    project_name: str = Field(min_length=1, max_length=160)
    total_area_m2: float = Field(gt=0, le=10000)
    floor_count: int = Field(ge=1, le=100)
    rooms: list[Room] = Field(min_length=1)

    @field_validator("project_name")
    @classmethod
    def project_name_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("project_name must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_rooms_for_property(self) -> "ObjectStepDraft":
        allowed = {
            item.room_type: item
            for item in available_location_types(self.property_type)
        }
        for room in self.rooms:
            definition = allowed.get(room.room_type)
            if definition is None:
                raise ValueError(
                    f"location type {room.room_type.value} is unavailable for "
                    f"{self.property_type.value}"
                )
            if room.location_kind != definition.kind:
                raise ValueError("room location_kind does not match its catalog type")
            if definition.kind == LocationKind.ROOM:
                if room.floor is None:
                    raise ValueError("ordinary rooms require a floor")
                if room.floor < 1 or room.floor > self.floor_count:
                    raise ValueError("room floor must be within the object floor count")
            elif room.floor is not None and not 1 <= room.floor <= self.floor_count:
                raise ValueError("special-location floor must be within the object floor count")
        if sum(room.room_type == RoomType.WHOLE_PROPERTY for room in self.rooms) != 1:
            raise ValueError("object must contain exactly one whole_property location")
        return self


def assessment_from_recommendation_result(
    answers: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    assessment_id: str,
) -> Assessment:
    recommendations = [
        RecommendationSnapshot(
            system_id=item["system_id"],
            status=RecommendationStatus(item["status"]),
            suitability_percent=item.get("suitability_percent"),
            reason=item["reason"],
            rule_score=item.get("rule_score"),
            llm_score=item.get("llm_score"),
            decision_source=item.get("decision_source", "hybrid"),
            evidence_question_ids=item.get("evidence_question_ids", []),
            missing_data=item.get("missing_data", []),
            needs_review=bool(item.get("needs_review", False)),
        )
        for item in result["recommendations"]
    ]
    return Assessment(
        assessment_id=assessment_id,
        workflow_stage=WorkflowStage.RECOMMENDATION_COMPLETE,
        property_type=PropertyType(answers["property_type"]),
        area_m2=answers.get("home_area_m2"),
        floor_count=answers.get("floor_count"),
        provider=result.get("provider"),
        model_name=result.get("model"),
        config_versions={
            "recommendation_catalog_size": len(recommendations),
            "systems_catalog": "systems_catalog_v1",
            "hybrid_alpha": result.get("hybrid_alpha"),
            "ux_flow": answers.get("_ux_flow", "v1"),
            "object_registry": answers.get("_object_registry"),
            "behavior_questionnaire": answers.get("_behavior_questionnaire"),
            "need_model": answers.get("_need_model"),
            "explanation_status": result.get("explanation_status"),
            "unverified_explanation_system_ids": [
                item["system_id"] for item in result["recommendations"]
                if item.get("explanation_needs_review")
            ],
        },
        answers=dict(answers),
        recommendations=recommendations,
    )


class ObjectStepRepository(Protocol):
    def save_object_step(
        self,
        assessment_id: str,
        expected_revision: int,
        *,
        project_name: str,
        area_m2: float,
        floor_count: int,
        rooms: list[Room],
    ) -> Assessment: ...


def persist_object_step_if_submitted(
    repository: ObjectStepRepository,
    assessment_id: str,
    expected_revision: int,
    draft: ObjectStepDraft | Mapping[str, Any],
    *,
    submitted: bool,
) -> Assessment | None:
    if not submitted:
        return None
    validated = (
        draft
        if isinstance(draft, ObjectStepDraft)
        else ObjectStepDraft.model_validate(draft)
    )
    return repository.save_object_step(
        assessment_id,
        expected_revision,
        project_name=validated.project_name,
        area_m2=validated.total_area_m2,
        floor_count=validated.floor_count,
        rooms=validated.rooms,
    )


def database_configuration_notice(database_url: str | None) -> str | None:
    if not database_url:
        return (
            "Попередній проєкт потребує PostgreSQL. Додайте DATABASE_URL до локального "
            ".env і виконайте `python -m alembic upgrade head`."
        )
    scheme = database_url.partition(":")[0].lower()
    if scheme == "sqlite" and os.getenv("SMART_HOME_ALLOW_SQLITE_RUNTIME") == "1":
        return None
    if scheme != "postgres" and not scheme.startswith("postgresql"):
        return "Для runtime-сховища попереднього проєкту підтримується PostgreSQL."
    return None
