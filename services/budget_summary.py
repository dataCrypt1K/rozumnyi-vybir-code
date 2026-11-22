from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Literal, Mapping, Protocol

from pydantic import Field

from domain.enums import (
    BudgetLevel,
    ImplementationPhase,
    RecommendationStatus,
    RoomType,
)
from domain.models import Assessment, BudgetPreference, DomainModel
from services.function_catalog import FunctionCatalog
from services.function_recommendation import dependency_violation_count


FocusState = Literal["current_focus", "next_stage", "later_or_optional"]


class BudgetRepository(Protocol):
    def save_budget_step(
        self,
        assessment_id: str,
        expected_revision: int,
        *,
        budget: BudgetPreference,
    ) -> Assessment: ...


class ScenarioFunction(DomainModel):
    function_id: str
    system_id: str
    room_id: str
    recommendation_status: RecommendationStatus
    recommended_phase: ImplementationPhase
    selected_phase: ImplementationPhase
    focus_state: FocusState
    is_prerequisite: bool = False
    preparation_required: bool = False
    needs_review: bool = False


class ScenarioFunctionGroup(DomainModel):
    function_id: str
    system_id: str
    room_ids: list[str]
    recommendation_statuses: list[RecommendationStatus]
    recommended_phases: list[ImplementationPhase]
    selected_phases: list[ImplementationPhase]
    focus_states: list[FocusState]
    is_prerequisite: bool = False
    preparation_required: bool = False
    needs_review: bool = False


class ImplementationScenario(DomainModel):
    budget_level: BudgetLevel
    user_amount_eur: Decimal | None = None
    items: list[ScenarioFunction] = Field(default_factory=list)


class ProjectSummary(DomainModel):
    assessment_id: str
    system_counts: dict[RecommendationStatus, int]
    selected_counts: dict[ImplementationPhase, int]
    selected_total: int
    not_selected_count: int
    needs_review_count: int
    dependency_links: int
    unmet_dependencies: int
    selected_technical_mvp_count: int
    unpriced_selected_count: int
    final_engineering_count: int
    scenario: ImplementationScenario


def empty_scope_guidance(
    assessment: Assessment,
    systems: list[dict[str, object]],
) -> tuple[str, str | None]:
    """Explain an empty automatic scope without changing recommendations."""
    has_recommended = any(
        item.status == RecommendationStatus.RECOMMENDED
        for item in assessment.recommendations
    )
    if has_recommended:
        message = (
            "До попереднього обсягу не ввійшла жодна функція за підтвердженими "
            "даними про приміщення та обраним масштабом. Оцінки систем вище "
            "залишаються чинними; якщо очікували інший обсяг, перевірте дані об'єкта."
        )
    else:
        message = (
            "За підтвердженим профілем жодна система не отримала статусу "
            "«рекомендована», тому автоматичний план реалізації порожній. "
            "Це не помилка й не означає, що автоматизація неможлива."
        )
    names = {str(item["system_id"]): str(item["name_uk"]) for item in systems}
    optional = [
        f"{names.get(item.system_id, item.system_id)} ({item.suitability_percent}%)"
        for item in assessment.recommendations
        if item.status == RecommendationStatus.OPTIONAL
    ]
    optional_text = (
        "За бажанням можна розглянути: " + ", ".join(optional) + ". "
        "Персональні пояснення наведено в оцінці систем."
        if optional else None
    )
    return message, optional_text


def group_scenario_functions(
    items: list[ScenarioFunction],
    *,
    room_sort_order: Mapping[str, int] | None = None,
) -> list[ScenarioFunctionGroup]:
    """Collapse repeated room applications into one display row per function."""
    grouped: dict[str, list[ScenarioFunction]] = {}
    for item in items:
        grouped.setdefault(item.function_id, []).append(item)

    order = room_sort_order or {}
    result: list[ScenarioFunctionGroup] = []
    for function_id, function_items in grouped.items():
        room_ids = list(dict.fromkeys(item.room_id for item in function_items))
        room_ids.sort(key=lambda room_id: (order.get(room_id, 10**9), room_id))
        result.append(
            ScenarioFunctionGroup(
                function_id=function_id,
                system_id=function_items[0].system_id,
                room_ids=room_ids,
                recommendation_statuses=list(
                    dict.fromkeys(item.recommendation_status for item in function_items)
                ),
                recommended_phases=list(
                    dict.fromkeys(item.recommended_phase for item in function_items)
                ),
                selected_phases=list(
                    dict.fromkeys(item.selected_phase for item in function_items)
                ),
                focus_states=list(
                    dict.fromkeys(item.focus_state for item in function_items)
                ),
                is_prerequisite=any(item.is_prerequisite for item in function_items),
                preparation_required=any(
                    item.preparation_required for item in function_items
                ),
                needs_review=any(item.needs_review for item in function_items),
            )
        )
    return result


def persist_budget_step_if_submitted(
    repository: BudgetRepository,
    assessment_id: str,
    expected_revision: int,
    budget: BudgetPreference,
    *,
    submitted: bool,
) -> Assessment | None:
    """Keep drafts side-effect free; only the explicit form submit writes."""
    if not submitted:
        return None
    return repository.save_budget_step(
        assessment_id,
        expected_revision,
        budget=budget,
    )


def _focused_phases(level: BudgetLevel) -> set[ImplementationPhase]:
    if level == BudgetLevel.BASIC:
        return {ImplementationPhase.FIRST}
    if level == BudgetLevel.BALANCED:
        return {ImplementationPhase.FIRST, ImplementationPhase.SECOND}
    return set(ImplementationPhase)


def build_implementation_scenario(
    assessment: Assessment,
    catalog: FunctionCatalog,
) -> ImplementationScenario:
    if assessment.budget is None:
        raise ValueError("budget is required to build an implementation scenario")

    catalog_map = {item.function_id: item for item in catalog.functions}
    recommendation_map = {
        (item.function_id, item.room_id): item
        for item in assessment.function_recommendations
    }
    selected = [item for item in assessment.function_choices if item.selected]
    focused_phases = _focused_phases(assessment.budget.level)
    focused_keys = {
        (item.function_id, item.room_id)
        for item in selected
        if item.selected_phase in focused_phases
    }

    scenario_items: list[ScenarioFunction] = []
    for choice in selected:
        if choice.function_id not in catalog_map:
            raise ValueError("selected function is missing from the active catalog")
        key = (choice.function_id, choice.room_id)
        recommendation = recommendation_map.get(key)
        if recommendation is None or choice.selected_phase is None:
            raise ValueError("selected function must reference a recommendation and phase")
        if recommendation.recommended_phase is None:
            raise ValueError("selected function must have a recommended phase")

        if key in focused_keys:
            focus_state: FocusState = "current_focus"
        elif choice.selected_phase == ImplementationPhase.SECOND:
            focus_state = "next_stage"
        else:
            focus_state = "later_or_optional"
        scenario_items.append(
            ScenarioFunction(
                function_id=choice.function_id,
                system_id=choice.system_id,
                room_id=choice.room_id,
                recommendation_status=recommendation.status,
                recommended_phase=recommendation.recommended_phase,
                selected_phase=choice.selected_phase,
                focus_state=focus_state,
                is_prerequisite=any(
                    code.startswith("PREREQUISITE_FOR:")
                    for code in recommendation.rule_codes
                ),
                preparation_required=(
                    focus_state != "current_focus"
                    and recommendation.retrofit_penalty == 2
                ),
                needs_review=recommendation.needs_review,
            )
        )

    phase_order = {
        ImplementationPhase.FIRST: 0,
        ImplementationPhase.SECOND: 1,
        ImplementationPhase.LATER: 2,
    }
    scenario_items.sort(
        key=lambda item: (
            phase_order[item.selected_phase],
            item.system_id,
            item.function_id,
            item.room_id,
        )
    )
    return ImplementationScenario(
        budget_level=assessment.budget.level,
        user_amount_eur=assessment.budget.amount_eur,
        items=scenario_items,
    )


def build_project_summary(
    assessment: Assessment,
    catalog: FunctionCatalog,
) -> ProjectSummary:
    scenario = build_implementation_scenario(assessment, catalog)
    catalog_map = {item.function_id: item for item in catalog.functions}
    selected = [item for item in assessment.function_choices if item.selected]
    dependency_links = 0
    for choice in selected:
        prerequisites = catalog_map[choice.function_id].prerequisite_function_ids
        dependency_links += len(prerequisites)
    whole_property_id = next(
        room.room_id
        for room in assessment.rooms
        if room.room_type == RoomType.WHOLE_PROPERTY
    )
    unmet_dependencies = dependency_violation_count(
        assessment.function_choices,
        assessment.function_recommendations,
        catalog,
        whole_property_id,
    )

    system_counts = Counter(item.status for item in assessment.recommendations)
    selected_counts = Counter(
        item.selected_phase for item in selected if item.selected_phase is not None
    )
    recommendation_map = {
        (item.function_id, item.room_id): item
        for item in assessment.function_recommendations
    }
    return ProjectSummary(
        assessment_id=assessment.assessment_id,
        system_counts={status: system_counts[status] for status in RecommendationStatus},
        selected_counts={phase: selected_counts[phase] for phase in ImplementationPhase},
        selected_total=len(selected),
        not_selected_count=sum(not item.selected for item in assessment.function_choices),
        needs_review_count=sum(
            item.needs_review for item in assessment.function_recommendations
        ),
        dependency_links=dependency_links,
        unmet_dependencies=unmet_dependencies,
        selected_technical_mvp_count=sum(
            catalog_map[item.function_id].technical_mvp for item in selected
        ),
        unpriced_selected_count=len(selected),
        final_engineering_count=sum(
            catalog_map[item.function_id].final_engineering_required for item in selected
        ),
        scenario=scenario,
    )
