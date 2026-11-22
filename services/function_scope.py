from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from domain.enums import (
    DecisionSource,
    RecommendationStatus,
    RoomType,
)
from domain.models import Assessment, FunctionChoice, FunctionRecommendation
from services.function_catalog import FunctionCatalog
from services.function_recommendation import build_user_choice_plan


SCOPE_POLICY_VERSION = "automatic_scope_v2"
ScopeLevel = Literal["conservative", "balanced", "expanded"]


# These systems protect people, property, access, or essential infrastructure.
# A lifestyle-derived scope must not silently reduce their confirmed coverage.
PROTECTED_SYSTEMS = frozenset(
    {
        "intrusion_alarm",
        "video_surveillance",
        "access_control",
        "perimeter_security",
        "leak_protection",
        "fire_safety",
        "gas_co_detection",
        "power_protection_monitoring",
        "backup_power",
        "gate_garage_control",
        "septic_monitoring",
    }
)


# Only zone-oriented comfort systems are narrowed in the conservative profile.
# The limit is applied independently to each function, never to the whole system.
CONSERVATIVE_ZONE_LIMITS = {
    "multimedia_multiroom": 2,
    "shading_control": 2,
    "smart_lighting": 3,
    "climate_control": 3,
    "ventilation_air_quality": 3,
}


# These functions describe one coordinated installation, not one independent
# installation in every compatible room.
FUNCTION_ZONE_LIMITS = {
    "media.home_cinema": 1,
    "media.centralized_sources": 1,
}


ROOM_PRIORITY_BY_FUNCTION: dict[str, tuple[RoomType, ...]] = {
    "media.home_cinema": (
        RoomType.CINEMA_ROOM,
        RoomType.LIVING_ROOM,
        RoomType.KITCHEN_LIVING_ROOM,
    ),
    "media.centralized_sources": (
        RoomType.TECHNICAL_ROOM,
        RoomType.UTILITY_ROOM,
        RoomType.STORAGE_ROOM,
        RoomType.CINEMA_ROOM,
    ),
}


ROOM_PRIORITY_BY_SYSTEM: dict[str, tuple[RoomType, ...]] = {
    "multimedia_multiroom": (
        RoomType.CINEMA_ROOM,
        RoomType.KITCHEN_LIVING_ROOM,
        RoomType.LIVING_ROOM,
        RoomType.KITCHEN,
        RoomType.TERRACE,
        RoomType.OFFICE,
        RoomType.BEDROOM,
        RoomType.BATHROOM,
    ),
    "shading_control": (
        RoomType.KITCHEN_LIVING_ROOM,
        RoomType.LIVING_ROOM,
        RoomType.BEDROOM,
        RoomType.CHILDRENS_ROOM,
        RoomType.OFFICE,
        RoomType.KITCHEN,
    ),
    "smart_lighting": (
        RoomType.KITCHEN_LIVING_ROOM,
        RoomType.LIVING_ROOM,
        RoomType.KITCHEN,
        RoomType.HALLWAY,
        RoomType.BEDROOM,
        RoomType.CHILDRENS_ROOM,
        RoomType.BATHROOM,
    ),
    "climate_control": (
        RoomType.BEDROOM,
        RoomType.CHILDRENS_ROOM,
        RoomType.KITCHEN_LIVING_ROOM,
        RoomType.LIVING_ROOM,
        RoomType.OFFICE,
        RoomType.KITCHEN,
    ),
    "ventilation_air_quality": (
        RoomType.BEDROOM,
        RoomType.CHILDRENS_ROOM,
        RoomType.KITCHEN_LIVING_ROOM,
        RoomType.LIVING_ROOM,
        RoomType.OFFICE,
        RoomType.BATHROOM,
        RoomType.KITCHEN,
    ),
}


@dataclass(frozen=True)
class AutomaticScopePlan:
    choices: list[FunctionChoice]
    scope_level: ScopeLevel
    selected_count: int
    narrowed_count: int
    optional_added_count: int
    warnings: list[str]


def _room_rank(
    recommendation: FunctionRecommendation,
    assessment: Assessment,
) -> tuple[int, int]:
    room_by_id = {room.room_id: room for room in assessment.rooms}
    room = room_by_id[recommendation.room_id]
    priorities = ROOM_PRIORITY_BY_FUNCTION.get(
        recommendation.function_id,
        ROOM_PRIORITY_BY_SYSTEM.get(recommendation.system_id, ()),
    )
    try:
        priority = priorities.index(room.room_type)
    except ValueError:
        priority = len(priorities)
    return priority, room.sort_order


def _is_protected(recommendation: FunctionRecommendation) -> bool:
    return (
        recommendation.system_id in PROTECTED_SYSTEMS
        or recommendation.safety_criticality == 2
    )


def build_automatic_scope_plan(
    assessment: Assessment,
    recommendations: list[FunctionRecommendation],
    catalog: FunctionCatalog,
    scope_level: ScopeLevel | str,
) -> AutomaticScopePlan:
    normalized_scope: ScopeLevel
    if scope_level not in {"conservative", "balanced", "expanded"}:
        normalized_scope = "balanced"
    else:
        normalized_scope = scope_level  # type: ignore[assignment]

    whole_property_id = next(
        room.room_id
        for room in assessment.rooms
        if room.room_type == RoomType.WHOLE_PROPERTY
    )
    parent_status = {
        item.system_id: item.status for item in assessment.recommendations
    }
    selected_keys: set[tuple[str, str]] = set()
    optional_added_keys: set[tuple[str, str]] = set()

    by_function: dict[str, list[FunctionRecommendation]] = {}
    for recommendation in recommendations:
        by_function.setdefault(recommendation.function_id, []).append(recommendation)

    for function_recommendations in by_function.values():
        recommended = [
            item
            for item in function_recommendations
            if item.status == RecommendationStatus.RECOMMENDED
        ]
        selected_recommended = sorted(
            recommended,
            key=lambda item: _room_rank(item, assessment),
        )
        function_id = function_recommendations[0].function_id
        function_limit = FUNCTION_ZONE_LIMITS.get(function_id)
        if function_limit is not None:
            selected_recommended = selected_recommended[:function_limit]
        system_id = function_recommendations[0].system_id
        limit = CONSERVATIVE_ZONE_LIMITS.get(system_id)
        if normalized_scope == "conservative" and limit is not None:
            protected = [item for item in selected_recommended if _is_protected(item)]
            flexible = [item for item in selected_recommended if not _is_protected(item)]
            remaining = max(limit - len(protected), 0)
            selected_recommended = protected + sorted(
                flexible,
                key=lambda item: _room_rank(item, assessment),
            )[:remaining]

        selected_keys.update(
            (item.function_id, item.room_id) for item in selected_recommended
        )

        if normalized_scope == "expanded":
            optional = sorted(
                [
                    item
                    for item in function_recommendations
                    if item.status == RecommendationStatus.OPTIONAL
                    and not item.needs_review
                    and parent_status.get(item.system_id)
                    == RecommendationStatus.RECOMMENDED
                ],
                key=lambda item: _room_rank(item, assessment),
            )
            if function_limit is not None:
                remaining = max(function_limit - len(selected_recommended), 0)
                optional = optional[:remaining]
            optional_keys = {
                (item.function_id, item.room_id) for item in optional
            }
            optional_added_keys.update(optional_keys)
            selected_keys.update(optional_keys)

    requested = {
        (item.function_id, item.room_id): (
            (item.function_id, item.room_id) in selected_keys,
            item.recommended_phase
            if (item.function_id, item.room_id) in selected_keys
            else None,
        )
        for item in recommendations
    }
    normalized = build_user_choice_plan(
        recommendations,
        catalog,
        requested,
        whole_property_id=whole_property_id,
        decision_source_for_changes=DecisionSource.SYSTEM_DEFAULT,
    )
    selected_after_dependencies = {
        (item.function_id, item.room_id)
        for item in normalized.choices
        if item.selected
    }
    recommended_keys = {
        (item.function_id, item.room_id)
        for item in recommendations
        if item.status == RecommendationStatus.RECOMMENDED
    }
    return AutomaticScopePlan(
        choices=normalized.choices,
        scope_level=normalized_scope,
        selected_count=len(selected_after_dependencies),
        narrowed_count=len(recommended_keys - selected_after_dependencies),
        optional_added_count=len(optional_added_keys & selected_after_dependencies),
        warnings=normalized.warnings,
    )
