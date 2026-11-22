from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol

from domain.enums import (
    DecisionSource,
    ImplementationPhase,
    LocationKind,
    PropertyType,
    RecommendationStatus,
    RoomType,
)
from domain.models import (
    Assessment,
    FunctionChoice,
    FunctionRecommendation,
    RecommendationSnapshot,
    Room,
)
from services.function_catalog import FunctionCatalog, FunctionCatalogItem
from services.equipment_locations import (
    FUNCTION_EQUIPMENT_KEYS,
    confirmed_equipment_slots,
    room_slots,
)
from services.function_rules import ConditionExpression, FunctionRule, FunctionRulesConfig


class TruthValue(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


PHASE_ORDER = {
    ImplementationPhase.FIRST: 0,
    ImplementationPhase.SECOND: 1,
    ImplementationPhase.LATER: 2,
}


FUNCTION_TARGETING_POLICY_VERSION = "function_targeting_v10"


OPENING_DETECTION_ALWAYS_TARGETS = frozenset(
    {
        RoomType.ENTRANCE,
        RoomType.BALCONY,
        RoomType.LOGGIA,
        RoomType.TERRACE,
        RoomType.GARAGE,
    }
)
OPENING_DETECTION_WINDOW_ROOMS = frozenset(
    {
        RoomType.LIVING_ROOM,
        RoomType.KITCHEN,
        RoomType.KITCHEN_LIVING_ROOM,
        RoomType.DINING_ROOM,
        RoomType.BEDROOM,
        RoomType.GUEST_ROOM,
        RoomType.CHILDRENS_ROOM,
    }
)


@dataclass(frozen=True)
class FunctionPlan:
    recommendations: list[FunctionRecommendation]
    choices: list[FunctionChoice]


@dataclass(frozen=True)
class ChoicePlan:
    choices: list[FunctionChoice]
    warnings: list[str]


class FunctionStepRepository(Protocol):
    def save_function_step(
        self,
        assessment_id: str,
        expected_revision: int,
        *,
        recommendations: list[FunctionRecommendation] | None,
        choices: list[FunctionChoice],
        config_versions: dict[str, Any] | None = None,
    ) -> Assessment: ...


def _one_step_earlier(phase: ImplementationPhase) -> ImplementationPhase:
    if phase == ImplementationPhase.LATER:
        return ImplementationPhase.SECOND
    if phase == ImplementationPhase.SECOND:
        return ImplementationPhase.FIRST
    return ImplementationPhase.FIRST


def _earliest(*phases: ImplementationPhase) -> ImplementationPhase:
    return min(phases, key=lambda phase: PHASE_ORDER[phase])


def _lookup_condition_value(
    condition: ConditionExpression,
    assessment: Assessment,
    parent: RecommendationSnapshot,
    target: Room,
) -> tuple[bool, Any]:
    assert condition.source is not None
    assert condition.field is not None
    if condition.source == "questionnaire":
        if condition.field not in assessment.answers:
            return False, None
        return True, assessment.answers[condition.field]
    if condition.source == "system_recommendation":
        values = {
            "status": parent.status.value,
            "suitability_percent": parent.suitability_percent,
            "rule_score": parent.rule_score,
            "llm_score": parent.llm_score,
            "needs_review": parent.needs_review,
        }
        return condition.field in values, values.get(condition.field)

    room_types = [
        room.room_type.value
        for room in assessment.rooms
        if room.location_kind == LocationKind.ROOM
    ]
    location_types = [
        room.room_type.value
        for room in assessment.rooms
        if room.location_kind == LocationKind.SPECIAL
    ]
    values = {
        "property_type": assessment.property_type.value if assessment.property_type else None,
        "area_m2": assessment.area_m2,
        "total_area_m2": assessment.area_m2,
        "floor_count": assessment.floor_count,
        "room_count": len(room_types),
        "room_types": room_types,
        "location_types": location_types,
        "all_location_types": room_types + location_types,
        "target_room_type": target.room_type.value,
        "target_location_kind": target.location_kind.value,
        "target_floor": target.floor,
    }
    return condition.field in values, values.get(condition.field)


def evaluate_condition(
    condition: ConditionExpression,
    assessment: Assessment,
    parent: RecommendationSnapshot,
    target: Room,
) -> TruthValue:
    present, actual = _lookup_condition_value(condition, assessment, parent, target)
    assert condition.operator is not None
    if condition.operator == "exists":
        expected = True if condition.value is None else bool(condition.value)
        exists = present and actual is not None
        return TruthValue.TRUE if exists == expected else TruthValue.FALSE
    if not present or actual is None:
        return TruthValue.UNKNOWN
    expected = condition.value
    try:
        if condition.operator == "equals":
            result = actual == expected
        elif condition.operator == "not_equals":
            result = actual != expected
        elif condition.operator == "contains":
            result = expected in actual
        elif condition.operator == "not_contains":
            result = expected not in actual
        elif condition.operator == "in":
            result = actual in expected
        elif condition.operator == "gte":
            result = actual >= expected
        elif condition.operator == "lte":
            result = actual <= expected
        else:
            return TruthValue.UNKNOWN
    except (TypeError, ValueError):
        return TruthValue.UNKNOWN
    return TruthValue.TRUE if result else TruthValue.FALSE


def _evaluate_all(values: list[TruthValue]) -> TruthValue:
    if any(value == TruthValue.FALSE for value in values):
        return TruthValue.FALSE
    if any(value == TruthValue.UNKNOWN for value in values):
        return TruthValue.UNKNOWN
    return TruthValue.TRUE


def _evaluate_any(values: list[TruthValue]) -> TruthValue | None:
    if not values:
        return None
    if any(value == TruthValue.TRUE for value in values):
        return TruthValue.TRUE
    if any(value == TruthValue.UNKNOWN for value in values):
        return TruthValue.UNKNOWN
    return TruthValue.FALSE


def evaluate_expression(
    expression: ConditionExpression,
    assessment: Assessment,
    parent: RecommendationSnapshot,
    target: Room,
) -> TruthValue | None:
    if expression.is_leaf:
        return evaluate_condition(expression, assessment, parent, target)
    if expression.all is not None:
        nested = [
            evaluate_expression(item, assessment, parent, target)
            for item in expression.all
        ]
        return _evaluate_all([item for item in nested if item is not None])
    assert expression.any is not None
    nested = [
        evaluate_expression(item, assessment, parent, target)
        for item in expression.any
    ]
    return _evaluate_any([item for item in nested if item is not None])


def _phase_for(
    item: FunctionCatalogItem,
    rule: FunctionRule,
    status: RecommendationStatus,
    answers: Mapping[str, Any],
) -> tuple[ImplementationPhase | None, list[str]]:
    if status == RecommendationStatus.NOT_APPLICABLE:
        return None, ["PHASE_NOT_APPLICABLE"]
    if status == RecommendationStatus.INEXPEDIENT:
        return ImplementationPhase.LATER, [
            "PHASE_INITIALIZE_DEFAULT",
            "PHASE_INEXPEDIENT_LATER",
        ]

    default = item.default_phase_priority
    candidates = [default]
    codes = ["PHASE_INITIALIZE_DEFAULT"]
    factors = rule.factors
    if factors.safety_criticality == 2:
        candidates.append(ImplementationPhase.FIRST)
        codes.append("PHASE_SAFETY_FIRST")
    if status == RecommendationStatus.RECOMMENDED and factors.daily_impact == 2:
        candidates.append(_one_step_earlier(default))
        codes.append("PHASE_DAILY_PROMOTE_ONE")
    retrofit_possible = (
        answers.get("project_stage") in {"planning", "renovation", "reconstruction"}
    )
    if factors.retrofit_penalty == 2 and retrofit_possible:
        candidates.append(_one_step_earlier(default))
        codes.append("PHASE_RETROFIT_PROMOTE_ONE")
    return _earliest(*candidates), codes


def _target_rooms(
    item: FunctionCatalogItem,
    assessment: Assessment,
    rules: FunctionRulesConfig,
) -> tuple[list[Room], bool]:
    primary_room_types: set[RoomType] = set()
    infrastructure_room_types: set[RoomType] = set()
    virtual_aliases: list[str] = []
    for alias in item.applicable_room_types:
        definition = rules.aliases.room_types[alias]
        if definition.resolution == "virtual":
            virtual_aliases.append(alias)
            continue
        target = (
            infrastructure_room_types
            if definition.target_role == "infrastructure_anchor"
            else primary_room_types
        )
        target.update(definition.ui_room_types)
    for alias in item.applicable_location_types:
        definition = rules.aliases.location_types[alias]
        if definition.resolution == "virtual":
            virtual_aliases.append(alias)
            continue
        target = (
            infrastructure_room_types
            if definition.target_role == "infrastructure_anchor"
            else primary_room_types
        )
        target.update(definition.ui_room_types)

    # An infrastructure anchor describes where hardware is installed, not an
    # additional user-facing recommendation. It becomes the target only when
    # the function has no service zone or aggregate scope of its own.
    room_types = primary_room_types or infrastructure_room_types
    unresolved_virtual_only = bool(virtual_aliases) and not room_types
    targets = [room for room in assessment.rooms if room.room_type in room_types]
    if item.function_id == "leak.manual_valve_control" and not targets:
        if assessment.answers.get("water_entry_present") is True:
            targets = [_whole_property(assessment)]
    if item.function_id == "intrusion.opening_detection":
        exposed_openings = assessment.answers.get("exposed_access_points") is True
        filtered_targets: list[Room] = []
        for room in targets:
            if room.room_type in OPENING_DETECTION_ALWAYS_TARGETS:
                filtered_targets.append(room)
                continue
            if not exposed_openings:
                continue
            if room.room_type not in OPENING_DETECTION_WINDOW_ROOMS:
                continue
            if (
                assessment.property_type == PropertyType.HOUSE
                and room.floor not in {None, 1}
            ):
                continue
            filtered_targets.append(room)
        targets = filtered_targets

    sources = set(assessment.answers.get("gas_co_sources") or [])
    if item.function_id == "gas.gas_detection" and sources & {"gas_stove", "gas_boiler"}:
        gas_room_types: set[RoomType] = set()
        if "gas_stove" in sources:
            gas_room_types.update({RoomType.KITCHEN, RoomType.KITCHEN_LIVING_ROOM})
        if "gas_boiler" in sources:
            gas_room_types.add(RoomType.BOILER_ROOM)
        targets = [room for room in targets if room.room_type in gas_room_types]
        missing_stove_room = "gas_stove" in sources and not any(
            room.room_type in {RoomType.KITCHEN, RoomType.KITCHEN_LIVING_ROOM}
            for room in targets
        )
        missing_boiler_room = "gas_boiler" in sources and not any(
            room.room_type == RoomType.BOILER_ROOM for room in targets
        )
        if missing_stove_room or missing_boiler_room:
            targets.append(_whole_property(assessment))
    if item.function_id == "gas.co_detection" and sources & {
        "gas_boiler", "fireplace", "solid_fuel_boiler", "generator", "other"
    }:
        co_room_types = {RoomType.HALLWAY, RoomType.STAIRCASE}
        if "fireplace" in sources:
            co_room_types.add(RoomType.LIVING_ROOM)
        if sources & {"gas_boiler", "solid_fuel_boiler"}:
            co_room_types.add(RoomType.BOILER_ROOM)
        targets = [room for room in targets if room.room_type in co_room_types]
        if not any(
            room.room_type in {RoomType.HALLWAY, RoomType.STAIRCASE}
            for room in targets
        ):
            targets.append(_whole_property(assessment))

    equipment_key = FUNCTION_EQUIPMENT_KEYS.get(item.function_id)
    if equipment_key is not None:
        selected_slots = confirmed_equipment_slots(assessment.answers, equipment_key)
        room_slot_map = room_slots(assessment.rooms)
        targets = [
            room for room in targets if room_slot_map.get(room.room_id) in selected_slots
        ]
        sources_by_key = {
            "underfloor_heating": "underfloor_heating" in set(
                assessment.answers.get("heating_systems") or []
            ),
            "radiators": "radiators" in set(
                assessment.answers.get("heating_systems") or []
            ),
            "cooling": bool(
                set(assessment.answers.get("cooling_systems") or []) - {"none"}
            ),
            "exhaust": "exhaust" in set(
                assessment.answers.get("ventilation_equipment") or []
            ),
        }
        if not targets and sources_by_key[equipment_key]:
            targets = [_whole_property(assessment)]

    if item.function_id == "ventilation.humidity_control":
        targets = _priority_room_targets(
            targets,
            (RoomType.BATHROOM, RoomType.LAUNDRY, RoomType.WC,
             RoomType.BASEMENT, RoomType.KITCHEN, RoomType.KITCHEN_LIVING_ROOM),
        )
    if item.function_id == "ventilation.co2_monitoring":
        targets = _priority_room_targets(
            targets,
            (RoomType.CHILDRENS_ROOM, RoomType.BEDROOM, RoomType.OFFICE,
             RoomType.LIVING_ROOM, RoomType.KITCHEN_LIVING_ROOM),
        )

    return targets, unresolved_virtual_only


def _priority_room_targets(
    targets: list[Room], priority: tuple[RoomType, ...], *, limit: int = 2
) -> list[Room]:
    selected: list[Room] = []
    for room_type in priority:
        room = next((item for item in targets if item.room_type == room_type), None)
        if room is not None:
            selected.append(room)
        if len(selected) == limit:
            break
    return selected


def _whole_property(assessment: Assessment) -> Room:
    for room in assessment.rooms:
        if room.room_type == RoomType.WHOLE_PROPERTY:
            return room
    raise ValueError("object step must contain the whole_property location")


def _house_has_elevated_siren_risk(assessment: Assessment) -> bool:
    if assessment.property_type != PropertyType.HOUSE:
        return False
    priority = assessment.answers.get("intrusion_priority")
    return (
        assessment.answers.get("previous_intrusion") is True
        or assessment.answers.get("exposed_access_points") is True
        or (
            isinstance(priority, (int, float))
            and not isinstance(priority, bool)
            and priority >= 4
        )
    )


def _expression_rule_codes(
    function_id: str,
    group: str,
    expression: ConditionExpression,
    assessment: Assessment,
    parent: RecommendationSnapshot,
    target: Room,
) -> list[str]:
    codes: list[str] = []

    def visit(node: ConditionExpression, path: str) -> None:
        if node.is_leaf:
            result = evaluate_condition(node, assessment, parent, target)
            codes.append(
                f"{function_id}:{group}:{path}:{node.source}:"
                f"{node.field}:{node.operator}:{result.value}"
            )
            return
        children = node.all if node.all is not None else node.any or []
        branch = "all" if node.all is not None else "any"
        for index, child in enumerate(children, start=1):
            visit(child, f"{path}.{branch}{index}")

    visit(expression, "root")
    return codes


def _recommend_for_target(
    item: FunctionCatalogItem,
    rule: FunctionRule,
    assessment: Assessment,
    parent: RecommendationSnapshot,
    target: Room,
    *,
    rules_config: FunctionRulesConfig,
    used_virtual_alias: bool,
) -> FunctionRecommendation:
    factors = rule.factors
    if parent.status == RecommendationStatus.NOT_APPLICABLE:
        return FunctionRecommendation(
            function_id=item.function_id,
            system_id=item.system_id,
            room_id=target.room_id,
            status=RecommendationStatus.NOT_APPLICABLE,
            reason="Батьківська система не застосовна до цього профілю.",
            recommended_phase=None,
            decision_source="deterministic_function_rules",
            rule_codes=["PARENT_SYSTEM_NOT_APPLICABLE", "PHASE_NOT_APPLICABLE"],
            safety_criticality=factors.safety_criticality,
            retrofit_penalty=factors.retrofit_penalty,
            dependency_role=factors.dependency_role,
            daily_impact=factors.daily_impact,
            phase_rules_version=rules_config.phase_rules_version,
        )

    applicability_value = evaluate_expression(
        rule.applicability, assessment, parent, target
    )
    applicability = applicability_value or TruthValue.TRUE
    rule_codes = _expression_rule_codes(
        item.function_id,
        "app",
        rule.applicability,
        assessment,
        parent,
        target,
    )
    if applicability == TruthValue.FALSE:
        status = RecommendationStatus.NOT_APPLICABLE
        reason = "Умови застосовності цієї функції не підтверджені."
        needs_review = False
        rule_codes.append(
            rules_config.fallback_rule["reason_codes"]["applicability_false"]
        )
    elif applicability == TruthValue.UNKNOWN:
        status = parent.status
        reason = (
            "Для перевірки застосовності бракує даних; тимчасово успадковано "
            "статус батьківської системи."
        )
        needs_review = True
        rule_codes.append(
            rules_config.fallback_rule["reason_codes"]["applicability_unknown"]
        )
    else:
        relevance = evaluate_expression(rule.relevance, assessment, parent, target)
        rule_codes.extend(
            _expression_rule_codes(
                item.function_id,
                "rel",
                rule.relevance,
                assessment,
                parent,
                target,
            )
        )
        if relevance == TruthValue.TRUE:
            status = RecommendationStatus(
                rules_config.fallback_rule["relevance"]["match_status_map"][
                    parent.status.value
                ]
            )
            reason = (
                "Потребу підтверджено явним правилом; статус підвищено не більш "
                "ніж на один рівень від оцінки системи."
            )
            needs_review = False
            rule_codes.append(
                rules_config.fallback_rule["reason_codes"]["relevance_match"]
            )
        elif relevance == TruthValue.FALSE:
            status = RecommendationStatus(
                rules_config.fallback_rule["relevance"]["no_match_status_map"][
                    parent.status.value
                ]
            )
            reason = (
                "Специфічну потребу не підтверджено; рекомендацію консервативно "
                "обмежено відносно оцінки системи."
            )
            needs_review = False
            rule_codes.append(
                rules_config.fallback_rule["reason_codes"]["relevance_no_match"]
            )
        else:
            status = parent.status
            reason = (
                "Окремих даних для цієї функції немає; успадковано статус "
                "батьківської системи."
            )
            needs_review = relevance == TruthValue.UNKNOWN
            reason_code = (
                "relevance_unknown"
                if relevance == TruthValue.UNKNOWN
                else "fallback_inherited"
            )
            rule_codes.append(
                rules_config.fallback_rule["reason_codes"][reason_code]
            )

    recommended_phase, phase_codes = _phase_for(
        item, rule, status, assessment.answers
    )
    recommendation = FunctionRecommendation(
        function_id=item.function_id,
        system_id=item.system_id,
        room_id=target.room_id,
        status=status,
        reason=reason,
        recommended_phase=recommended_phase,
        decision_source="deterministic_function_rules",
        rule_codes=[*rule_codes, *phase_codes],
        needs_review=needs_review,
        safety_criticality=factors.safety_criticality,
        retrofit_penalty=factors.retrofit_penalty,
        dependency_role=factors.dependency_role,
        daily_impact=factors.daily_impact,
        phase_rules_version=rules_config.phase_rules_version,
    )
    if used_virtual_alias and recommendation.status != RecommendationStatus.NOT_APPLICABLE:
        recommendation = recommendation.model_copy(
            update={
                "needs_review": True,
                "reason": (
                    recommendation.reason
                    + " Точну фасадну або іншу похідну локацію треба підтвердити "
                    "на наступному етапі."
                ),
                "rule_codes": [
                    *recommendation.rule_codes,
                    "VIRTUAL_LOCATION_NEEDS_REVIEW",
                ],
            }
        )
    if (
        item.function_id == "intrusion.opening_detection"
        and assessment.property_type == PropertyType.APARTMENT
        and assessment.answers.get("exposed_access_points") is True
        and target.room_type in OPENING_DETECTION_WINDOW_ROOMS
        and recommendation.status != RecommendationStatus.NOT_APPLICABLE
    ):
        recommendation = recommendation.model_copy(
            update={
                "needs_review": True,
                "reason": (
                    recommendation.reason
                    + " Анкета підтвердила вразливі отвори, але конкретні "
                    "доступні вікна потрібно звірити з планом або оглядом об'єкта."
                ),
                "rule_codes": [
                    *recommendation.rule_codes,
                    "APARTMENT_EXPOSED_OPENINGS_REQUIRE_LOCATION_REVIEW",
                ],
            }
        )
    if (
        item.function_id == "intrusion.local_siren"
        and recommendation.status != RecommendationStatus.NOT_APPLICABLE
    ):
        if _house_has_elevated_siren_risk(assessment):
            scope_note = (
                " Передбачений обсяг: внутрішня сирена та зовнішня сирена як "
                "розширення для приватного будинку з підвищеним ризиком."
            )
            scope_code = "LOCAL_SIREN_INTERNAL_WITH_EXTERNAL_EXTENSION"
        elif assessment.property_type == PropertyType.HOUSE:
            scope_note = (
                " Передбачений обсяг: внутрішня сирена; окреме зовнішнє "
                "оповіщення за поточними даними не додається."
            )
            scope_code = "LOCAL_SIREN_INTERNAL_ONLY"
        else:
            scope_note = (
                " Передбачений обсяг: внутрішня сирена; окрема фасадна сирена "
                "для квартири не пропонується."
            )
            scope_code = "LOCAL_SIREN_APARTMENT_INTERNAL_ONLY"
        recommendation = recommendation.model_copy(
            update={
                "reason": recommendation.reason + scope_note,
                "rule_codes": [*recommendation.rule_codes, scope_code],
            }
        )
    if (
        item.function_id == "leak.detection_and_shutoff"
        and recommendation.status != RecommendationStatus.NOT_APPLICABLE
    ):
        if assessment.answers.get("water_entry_present") is True:
            scope_note = (
                " Обсяг включає датчик у підтвердженій зоні ризику; "
                "автоматичне перекриття допускається на доступному вводі води."
            )
            scope_code = "LEAK_DETECTION_WITH_SHUTOFF_AVAILABLE"
        else:
            scope_note = (
                " Обсяг включає виявлення та повідомлення; автоматичне "
                "перекриття не додається, доки доступ до вводу води не підтверджено."
            )
            scope_code = "LEAK_DETECTION_WITHOUT_CONFIRMED_SHUTOFF"
        recommendation = recommendation.model_copy(
            update={
                "reason": recommendation.reason + scope_note,
                "rule_codes": [*recommendation.rule_codes, scope_code],
            }
        )
    if (
        item.function_id == "leak.manual_valve_control"
        and recommendation.status != RecommendationStatus.NOT_APPLICABLE
    ):
        location_unconfirmed = target.room_type == RoomType.WHOLE_PROPERTY
        scope_note = (
            " Доступ до вводу води підтверджено, але його точну локацію та "
            "кількість незалежних гілок потрібно перевірити з інженером."
            if location_unconfirmed
            else " Функцію прив'язано до вводу води, а не дубльовано за "
            "сусідніми приміщеннями; кількість гілок уточнюється з інженером."
        )
        recommendation = recommendation.model_copy(
            update={
                "needs_review": recommendation.needs_review or location_unconfirmed,
                "reason": recommendation.reason + scope_note,
                "rule_codes": [
                    *recommendation.rule_codes,
                    (
                        "LEAK_VALVE_LOCATION_NEEDS_REVIEW"
                        if location_unconfirmed
                        else "LEAK_VALVE_WATER_ENTRY_ANCHOR"
                    ),
                ],
            }
        )
    if (
        item.function_id == "fire.smoke_detection"
        and recommendation.status != RecommendationStatus.NOT_APPLICABLE
    ):
        known_floors = {
            room.floor
            for room in assessment.rooms
            if room.location_kind == LocationKind.ROOM and room.floor is not None
        }
        missing_floor_layout = bool(
            assessment.floor_count
            and len(known_floors) < assessment.floor_count
        )
        recommendation = recommendation.model_copy(
            update={
                "needs_review": recommendation.needs_review or missing_floor_layout,
                "reason": (
                    recommendation.reason
                    + " Димове виявлення має охопити спальні, прилеглі шляхи "
                    "евакуації та кожен рівень житла; точне розміщення "
                    "визначається за планом."
                ),
                "rule_codes": [
                    *recommendation.rule_codes,
                    "FIRE_SMOKE_SLEEPING_AREAS_AND_EVERY_LEVEL",
                    *(
                        ["FIRE_FLOOR_LAYOUT_NEEDS_REVIEW"]
                        if missing_floor_layout
                        else []
                    ),
                ],
            }
        )
    if (
        item.function_id == "fire.heat_detection"
        and target.room_type in {RoomType.KITCHEN, RoomType.KITCHEN_LIVING_ROOM}
        and recommendation.status != RecommendationStatus.NOT_APPLICABLE
    ):
        recommendation = recommendation.model_copy(
            update={
                "reason": (
                    recommendation.reason
                    + " Для кухонної зони базово передбачається теплове "
                    "виявлення; комбінований датчик допускається після "
                    "перевірки місця та ризику хибних тривог."
                ),
                "rule_codes": [
                    *recommendation.rule_codes,
                    "FIRE_KITCHEN_HEAT_OR_ENGINEER_CONFIRMED_COMBINED",
                ],
            }
        )
    if (
        item.function_id in {"fire.local_alarm", "fire.remote_notification"}
        and recommendation.status != RecommendationStatus.NOT_APPLICABLE
    ):
        note = (
            " Локальне й дистанційне оповіщення є взаємопов'язаними "
            "компонентами однієї пожежної системи."
        )
        if item.function_id == "fire.local_alarm":
            note += " Локальний сигнал має бути чутним на всіх рівнях житла."
        recommendation = recommendation.model_copy(
            update={
                "reason": recommendation.reason + note,
                "rule_codes": [
                    *recommendation.rule_codes,
                    "FIRE_LOCAL_AND_REMOTE_ALERTING_BUNDLED",
                ],
            }
        )
    if item.function_id == "gas.gas_detection" and target.room_type == RoomType.WHOLE_PROPERTY:
        recommendation = recommendation.model_copy(
            update={
                "needs_review": True,
                "reason": recommendation.reason
                + " Газовий прилад підтверджено, але його приміщення треба вказати на плані.",
                "rule_codes": [*recommendation.rule_codes, "GAS_APPLIANCE_LOCATION_NEEDS_REVIEW"],
            }
        )
    if item.function_id == "gas.co_detection" and recommendation.status != RecommendationStatus.NOT_APPLICABLE:
        known_hallway_floors = {
            room.floor
            for room in assessment.rooms
            if room.room_type in {RoomType.HALLWAY, RoomType.STAIRCASE}
            and room.floor is not None
        }
        missing_floor_coverage = bool(
            assessment.floor_count
            and not set(range(1, assessment.floor_count + 1)).issubset(
                known_hallway_floors
            )
        )
        needs_location_review = (
            target.room_type == RoomType.WHOLE_PROPERTY
            or missing_floor_coverage
            or "other" in set(assessment.answers.get("gas_co_sources") or [])
        )
        recommendation = recommendation.model_copy(
            update={
                "needs_review": recommendation.needs_review or needs_location_review,
                "reason": recommendation.reason
                + " CO-сповіщення охоплює житлові рівні та зони біля спалень; "
                "місця встановлення уточнюються за планом. Генератор має працювати надворі.",
                "rule_codes": [
                    *recommendation.rule_codes,
                    "CO_LIVING_FLOORS_AND_SLEEPING_AREAS",
                    *(["CO_FLOOR_LAYOUT_NEEDS_REVIEW"] if missing_floor_coverage else []),
                    *(["CO_LOCATION_NEEDS_REVIEW"] if needs_location_review else []),
                ],
            }
        )
    if item.function_id in {"gas.local_alarm", "gas.remote_notification"} and recommendation.status != RecommendationStatus.NOT_APPLICABLE:
        recommendation = recommendation.model_copy(
            update={
                "reason": recommendation.reason
                + " Локальна тривога й дистанційне повідомлення є частинами однієї системи; "
                "віддалений канал не замінює звукового сигналу в житлі.",
                "rule_codes": [*recommendation.rule_codes, "GAS_CO_ALERTING_BUNDLED"],
            }
        )
    if item.function_id in FUNCTION_EQUIPMENT_KEYS and recommendation.status != RecommendationStatus.NOT_APPLICABLE:
        if target.room_type == RoomType.WHOLE_PROPERTY:
            recommendation = recommendation.model_copy(
                update={
                    "needs_review": True,
                    "reason": recommendation.reason
                    + " Обладнання є або планується, але кімнати його розміщення потрібно підтвердити.",
                    "rule_codes": [*recommendation.rule_codes, "EQUIPMENT_ROOMS_NEED_REVIEW"],
                }
            )
    if item.function_id == "climate.underfloor_heating_control" and recommendation.status != RecommendationStatus.NOT_APPLICABLE:
        recommendation = recommendation.model_copy(
            update={
                "reason": recommendation.reason
                + " Тип теплої підлоги та спосіб підключення визначаються під час проєктування.",
                "rule_codes": [*recommendation.rule_codes, "UNDERFLOOR_TECHNOLOGY_UNSPECIFIED"],
            }
        )
    if item.function_id == "backup.critical_load_supply" and recommendation.status != RecommendationStatus.NOT_APPLICABLE:
        if not (set(assessment.answers.get("backup_power_sources") or []) - {"none"}):
            recommendation = recommendation.model_copy(
                update={
                    "needs_review": True,
                    "reason": recommendation.reason
                    + " Критичні навантаження можна рекомендувати до вибору резервного джерела; "
                    "його тип і потужність треба уточнити.",
                    "rule_codes": [*recommendation.rule_codes, "BACKUP_SOURCE_SELECTION_PENDING"],
                }
            )
    if item.function_id == "backup.automatic_transfer" and recommendation.status != RecommendationStatus.NOT_APPLICABLE:
        source_missing = not (
            set(assessment.answers.get("backup_power_sources") or []) - {"none"}
        )
        recommendation = recommendation.model_copy(
            update={
                "status": (
                    RecommendationStatus.OPTIONAL
                    if source_missing and recommendation.status == RecommendationStatus.RECOMMENDED
                    else recommendation.status
                ),
                "needs_review": True,
                "reason": recommendation.reason
                + " Автоматичне перемикання потребує вибору сумісного джерела та "
                "підтвердженої електричної схеми.",
                "rule_codes": [
                    *recommendation.rule_codes,
                    "BACKUP_TRANSFER_ENGINEERING_REVIEW",
                    *(["BACKUP_SOURCE_SELECTION_PENDING"] if source_missing else []),
                ],
            }
        )
    if item.function_id == "backup.source_monitoring" and recommendation.status != RecommendationStatus.NOT_APPLICABLE:
        if not (set(assessment.answers.get("backup_power_sources") or []) - {"none"}):
            recommendation = recommendation.model_copy(
                update={
                    "status": (
                        RecommendationStatus.OPTIONAL
                        if recommendation.status == RecommendationStatus.RECOMMENDED
                        else recommendation.status
                    ),
                    "needs_review": True,
                    "reason": recommendation.reason
                    + " Параметри моніторингу визначаються після вибору резервного джерела.",
                    "rule_codes": [*recommendation.rule_codes, "BACKUP_SOURCE_SELECTION_PENDING"],
                }
            )
    if item.function_id == "power.notifications" and recommendation.status != RecommendationStatus.NOT_APPLICABLE:
        if assessment.answers.get("power_notification_path_confirmed") is not True:
            recommendation = recommendation.model_copy(
                update={
                    "status": (
                        RecommendationStatus.OPTIONAL
                        if recommendation.status == RecommendationStatus.RECOMMENDED
                        else recommendation.status
                    ),
                    "needs_review": True,
                    "reason": recommendation.reason
                    + " Доставку повідомлення під час відключення можна підтвердити "
                    "лише після перевірки резервного живлення контролера й каналу зв'язку.",
                    "rule_codes": [*recommendation.rule_codes, "POWER_NOTIFICATION_PATH_NEEDS_REVIEW"],
                }
            )
    return recommendation


def _prerequisite_candidate(
    dependent: FunctionRecommendation,
    candidates: list[FunctionRecommendation],
    whole_property_id: str,
) -> FunctionRecommendation | None:
    applicable = [
        item for item in candidates if item.status != RecommendationStatus.NOT_APPLICABLE
    ]
    if not applicable:
        return None
    for item in applicable:
        if item.room_id == dependent.room_id:
            return item
    for item in applicable:
        if item.room_id == whole_property_id:
            return item
    return applicable[0]


def normalize_recommendation_dependencies(
    recommendations: list[FunctionRecommendation],
    catalog: FunctionCatalog,
    *,
    whole_property_id: str,
) -> list[FunctionRecommendation]:
    by_function: dict[str, list[FunctionRecommendation]] = {}
    for recommendation in recommendations:
        by_function.setdefault(recommendation.function_id, []).append(recommendation)
    prerequisites = {
        item.function_id: item.prerequisite_function_ids for item in catalog.functions
    }
    normalized = list(recommendations)
    key_to_index = {
        (item.function_id, item.room_id): index
        for index, item in enumerate(normalized)
    }

    for _ in range(len(catalog.functions)):
        changed = False
        current_by_function: dict[str, list[FunctionRecommendation]] = {}
        for item in normalized:
            current_by_function.setdefault(item.function_id, []).append(item)
        for dependent in list(normalized):
            if dependent.status == RecommendationStatus.NOT_APPLICABLE:
                continue
            for prerequisite_id in prerequisites[dependent.function_id]:
                prerequisite = _prerequisite_candidate(
                    dependent,
                    current_by_function.get(prerequisite_id, []),
                    whole_property_id,
                )
                dependent_key = (dependent.function_id, dependent.room_id)
                if prerequisite is None:
                    replacement = dependent.model_copy(
                        update={
                            "status": RecommendationStatus.NOT_APPLICABLE,
                            "recommended_phase": None,
                            "reason": (
                                "Функція не застосовна, оскільки обов'язкова "
                                "передумова недоступна для цього об'єкта."
                            ),
                            "rule_codes": [
                                *dependent.rule_codes,
                                f"PREREQUISITE_UNAVAILABLE:{prerequisite_id}",
                            ],
                        }
                    )
                    normalized[key_to_index[dependent_key]] = replacement
                    changed = True
                    break

                updates: dict[str, Any] = {}
                codes = list(prerequisite.rule_codes)
                if (
                    dependent.status == RecommendationStatus.RECOMMENDED
                    and prerequisite.status != RecommendationStatus.RECOMMENDED
                ):
                    updates["status"] = RecommendationStatus.RECOMMENDED
                if (
                    dependent.recommended_phase is not None
                    and prerequisite.recommended_phase is not None
                    and PHASE_ORDER[prerequisite.recommended_phase]
                    > PHASE_ORDER[dependent.recommended_phase]
                ):
                    updates["recommended_phase"] = dependent.recommended_phase
                code = f"PREREQUISITE_FOR:{dependent.function_id}"
                if code not in codes:
                    codes.append(code)
                    updates["rule_codes"] = codes
                    updates["reason"] = (
                        prerequisite.reason
                        + " Пріоритет скориговано через залежну функцію "
                        + dependent.function_id
                        + "."
                    )
                if updates:
                    prerequisite_key = (
                        prerequisite.function_id,
                        prerequisite.room_id,
                    )
                    normalized[key_to_index[prerequisite_key]] = prerequisite.model_copy(
                        update=updates
                    )
                    changed = True
        if not changed:
            return normalized
    raise ValueError("prerequisite normalization did not converge")


def generate_function_plan(
    assessment: Assessment,
    catalog: FunctionCatalog,
    rules: FunctionRulesConfig,
) -> FunctionPlan:
    if assessment.property_type is None:
        raise ValueError("assessment property_type is required")
    if not assessment.rooms:
        raise ValueError("object step must contain rooms and locations")
    whole_property = _whole_property(assessment)
    parent_by_system = {item.system_id: item for item in assessment.recommendations}
    recommendations: list[FunctionRecommendation] = []

    for item in catalog.functions:
        parent = parent_by_system.get(item.system_id)
        if parent is None:
            raise ValueError(f"missing parent system recommendation: {item.system_id}")
        rule = rules.functions[item.function_id]
        property_applicable = assessment.property_type in item.applicable_property_types
        targets, used_virtual_alias = (
            _target_rooms(item, assessment, rules)
            if property_applicable
            else ([], False)
        )
        if not targets:
            factors = rule.factors
            reason = (
                "Функція не застосовна до цього типу житла."
                if not property_applicable
                else "Для функції немає відповідного приміщення або локації."
            )
            code = "PROPERTY_TYPE_MISMATCH" if not property_applicable else "NO_MATCHING_LOCATION"
            recommendations.append(
                FunctionRecommendation(
                    function_id=item.function_id,
                    system_id=item.system_id,
                    room_id=whole_property.room_id,
                    status=RecommendationStatus.NOT_APPLICABLE,
                    reason=reason,
                    recommended_phase=None,
                    decision_source="deterministic_function_rules",
                    rule_codes=[code, "PHASE_NOT_APPLICABLE"],
                    safety_criticality=factors.safety_criticality,
                    retrofit_penalty=factors.retrofit_penalty,
                    dependency_role=factors.dependency_role,
                    daily_impact=factors.daily_impact,
                    phase_rules_version=rules.phase_rules_version,
                )
            )
            continue
        for target in targets:
            recommendations.append(
                _recommend_for_target(
                    item,
                    rule,
                    assessment,
                    parent,
                    target,
                    rules_config=rules,
                    used_virtual_alias=used_virtual_alias,
                )
            )

    monitoring_recommended = any(
        item.function_id in {
            "ventilation.humidity_control", "ventilation.co2_monitoring"
        }
        and item.status == RecommendationStatus.RECOMMENDED
        for item in recommendations
    )
    if not monitoring_recommended:
        recommendations = [
            item.model_copy(
                update={
                    "status": (
                        RecommendationStatus.OPTIONAL
                        if item.status == RecommendationStatus.RECOMMENDED
                        else item.status
                    ),
                    "needs_review": True,
                    "reason": item.reason
                    + " Сповіщення можливе після вибору джерела вимірювання вологості або CO2.",
                    "rule_codes": [*item.rule_codes, "AIR_QUALITY_SENSOR_SOURCE_PENDING"],
                }
            )
            if item.function_id == "ventilation.air_quality_notifications"
            and item.status != RecommendationStatus.NOT_APPLICABLE
            else item
            for item in recommendations
        ]

    recommendations = normalize_recommendation_dependencies(
        recommendations,
        catalog,
        whole_property_id=whole_property.room_id,
    )
    return FunctionPlan(
        recommendations=recommendations,
        choices=default_function_choices(recommendations),
    )


def default_function_choices(
    recommendations: list[FunctionRecommendation],
) -> list[FunctionChoice]:
    return [
        FunctionChoice(
            function_id=item.function_id,
            system_id=item.system_id,
            room_id=item.room_id,
            selected=item.status == RecommendationStatus.RECOMMENDED,
            selected_phase=(
                item.recommended_phase
                if item.status == RecommendationStatus.RECOMMENDED
                else None
            ),
            decision_source=DecisionSource.SYSTEM_DEFAULT,
        )
        for item in recommendations
    ]


def build_user_choice_plan(
    recommendations: list[FunctionRecommendation],
    catalog: FunctionCatalog,
    requested: Mapping[
        tuple[str, str], tuple[bool, ImplementationPhase | str | None]
    ],
    *,
    whole_property_id: str,
    decision_source_for_changes: DecisionSource = DecisionSource.USER,
) -> ChoicePlan:
    recommendation_map = {
        (item.function_id, item.room_id): item for item in recommendations
    }
    choices: dict[tuple[str, str], FunctionChoice] = {}
    warnings: list[str] = []
    for key, recommendation in recommendation_map.items():
        default_selected = recommendation.status == RecommendationStatus.RECOMMENDED
        raw_selected, raw_phase = requested.get(
            key,
            (
                default_selected,
                recommendation.recommended_phase if default_selected else None,
            ),
        )
        selected = bool(raw_selected)
        if recommendation.status == RecommendationStatus.NOT_APPLICABLE and selected:
            raise ValueError("not_applicable functions cannot be selected")
        phase = ImplementationPhase(raw_phase) if selected and raw_phase else None
        if selected and phase is None:
            phase = recommendation.recommended_phase
        changed = (
            selected != default_selected
            or (selected and phase != recommendation.recommended_phase)
        )
        choices[key] = FunctionChoice(
            function_id=recommendation.function_id,
            system_id=recommendation.system_id,
            room_id=recommendation.room_id,
            selected=selected,
            selected_phase=phase,
            decision_source=(
                decision_source_for_changes
                if changed
                else DecisionSource.SYSTEM_DEFAULT
            ),
        )
        if (
            decision_source_for_changes == DecisionSource.USER
            and selected
            and recommendation.status in {
            RecommendationStatus.OPTIONAL,
            RecommendationStatus.INEXPEDIENT,
            }
        ):
            warnings.append(
                f"{recommendation.function_id} додано користувачем попри статус "
                f"{recommendation.status.value}."
            )

    prerequisites = {
        item.function_id: item.prerequisite_function_ids for item in catalog.functions
    }
    recommendations_by_function: dict[str, list[FunctionRecommendation]] = {}
    for item in recommendations:
        recommendations_by_function.setdefault(item.function_id, []).append(item)

    for _ in range(len(catalog.functions)):
        changed = False
        for key, choice in list(choices.items()):
            if not choice.selected:
                continue
            recommendation = recommendation_map[key]
            for prerequisite_id in prerequisites[choice.function_id]:
                prerequisite = _prerequisite_candidate(
                    recommendation,
                    recommendations_by_function.get(prerequisite_id, []),
                    whole_property_id,
                )
                if prerequisite is None:
                    raise ValueError(
                        f"selected function has unavailable prerequisite: {prerequisite_id}"
                    )
                prerequisite_key = (prerequisite.function_id, prerequisite.room_id)
                prerequisite_choice = choices[prerequisite_key]
                target_phase = _earliest(
                    prerequisite.recommended_phase or ImplementationPhase.LATER,
                    choice.selected_phase or ImplementationPhase.LATER,
                )
                if (
                    not prerequisite_choice.selected
                    or prerequisite_choice.selected_phase != target_phase
                ):
                    choices[prerequisite_key] = prerequisite_choice.model_copy(
                        update={
                            "selected": True,
                            "selected_phase": target_phase,
                            "decision_source": DecisionSource.SYSTEM_DEFAULT,
                        }
                    )
                    warnings.append(
                        f"{prerequisite_id} автоматично додано або перенесено "
                        f"через залежність від {choice.function_id}."
                    )
                    changed = True
        if not changed:
            break
    else:
        raise ValueError("choice prerequisite normalization did not converge")

    result = list(choices.values())
    if dependency_violation_count(result, recommendations, catalog, whole_property_id):
        raise ValueError("dependency normalization left phase violations")
    return ChoicePlan(choices=result, warnings=list(dict.fromkeys(warnings)))


def dependency_violation_count(
    choices: list[FunctionChoice],
    recommendations: list[FunctionRecommendation],
    catalog: FunctionCatalog,
    whole_property_id: str,
) -> int:
    choice_map = {(item.function_id, item.room_id): item for item in choices}
    recommendation_map = {
        (item.function_id, item.room_id): item for item in recommendations
    }
    by_function: dict[str, list[FunctionRecommendation]] = {}
    for item in recommendations:
        by_function.setdefault(item.function_id, []).append(item)
    prerequisites = {
        item.function_id: item.prerequisite_function_ids for item in catalog.functions
    }
    violations = 0
    for key, choice in choice_map.items():
        if not choice.selected:
            continue
        recommendation = recommendation_map[key]
        for prerequisite_id in prerequisites[choice.function_id]:
            prerequisite = _prerequisite_candidate(
                recommendation,
                by_function.get(prerequisite_id, []),
                whole_property_id,
            )
            if prerequisite is None:
                violations += 1
                continue
            prerequisite_choice = choice_map[(prerequisite.function_id, prerequisite.room_id)]
            if not prerequisite_choice.selected:
                violations += 1
            elif (
                prerequisite_choice.selected_phase is not None
                and choice.selected_phase is not None
                and PHASE_ORDER[prerequisite_choice.selected_phase]
                > PHASE_ORDER[choice.selected_phase]
            ):
                violations += 1
    return violations


def persist_function_step_if_submitted(
    repository: FunctionStepRepository,
    assessment_id: str,
    expected_revision: int,
    recommendations: list[FunctionRecommendation],
    catalog: FunctionCatalog,
    requested: Mapping[
        tuple[str, str], tuple[bool, ImplementationPhase | str | None]
    ],
    *,
    whole_property_id: str,
    submitted: bool,
    recommendations_already_persisted: bool = False,
    config_versions: dict[str, Any] | None = None,
    decision_source_for_changes: DecisionSource = DecisionSource.USER,
) -> tuple[Assessment, list[str]] | None:
    if not submitted:
        return None
    choice_plan = build_user_choice_plan(
        recommendations,
        catalog,
        requested,
        whole_property_id=whole_property_id,
        decision_source_for_changes=decision_source_for_changes,
    )
    saved = repository.save_function_step(
        assessment_id,
        expected_revision,
        recommendations=None if recommendations_already_persisted else recommendations,
        choices=choice_plan.choices,
        config_versions=config_versions,
    )
    return saved, choice_plan.warnings
