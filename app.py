from __future__ import annotations

import os
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import streamlit as st
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from active_config import load_active_configuration
from app_helpers import (
    COST_LABELS,
    STATUS_LABELS,
    load_history,
    normalize_multi_choice,
    option_label,
    plan_area_m2,
    questions_for_step,
    save_history,
    validate_step,
)
from hybrid_recommender import recommend, recommend_v2
from llm_client import ROOT as PROTOTYPE_ROOT, load_env_file
from evaluate_rules import load_json_compatible_yaml
from domain.enums import (
    BudgetLevel,
    ChannelType,
    DecisionSource,
    ImplementationPhase,
    LocationKind,
    PropertyType,
    RecommendationStatus,
    ReliabilityLevel,
    ReviewStatus,
    RoomType,
    TechnicalInputSource,
    PhysicalDeviceType,
    ValveProfile,
    WorkflowStage,
)
from domain.models import (
    Assessment,
    BudgetPreference,
    DocumentRef,
    EvidenceFact,
    Room,
    TechnicalLocationInput,
)
from repositories.database import (
    AssessmentAlreadyExistsError,
    AssessmentRepository,
    Database,
    DownstreamRecomputationRequiredError,
    RevisionConflictError,
)
from services.object_step_service import (
    LOCATION_TYPE_MAP,
    ObjectStepDraft,
    assessment_from_recommendation_result,
    available_location_types,
    database_configuration_notice,
    default_location_counts,
    location_label,
    persist_object_step_if_submitted,
    reconcile_rooms,
)
from services.function_catalog import load_function_catalog
from services.equipment_locations import EQUIPMENT_ROOM_TYPES, room_slot
from services.legacy_function_catalog import catalog_for_saved_functions
from services.function_recommendation import (
    FUNCTION_TARGETING_POLICY_VERSION,
    default_function_choices,
    generate_function_plan,
    persist_function_step_if_submitted,
)
from services.function_scope import (
    SCOPE_POLICY_VERSION,
    build_automatic_scope_plan,
)
from services.function_rules import load_function_rules
from services.budget_summary import (
    build_project_summary,
    empty_scope_guidance,
    group_scenario_functions,
    persist_budget_step_if_submitted,
)
from services.project_pdf import PdfGenerationError, build_project_pdf, safe_pdf_filename
from services.cloud_access import allowed_emails, cloud_mode, google_owner_key
from services.community_database import neon_url, upgrade_community_database
from services.technical_requirements import (
    TECHNICAL_FUNCTION_IDS,
    build_technical_calculation,
    persist_technical_step_if_submitted,
)
from services.object_intake import (
    CompletenessReport,
    ObjectFactValue,
    calculate_completeness,
    load_object_parameter_registry,
    parameter_is_applicable,
    store_document_locally,
    validate_document,
)
from services.plan_extraction import PlanExtraction, build_plan_extractor
from services.pdf_plan_pages import (
    MAX_ANALYSIS_PAGES,
    inspect_pdf_pages,
    suggested_pdf_pages,
)
from services.intake_persistence import (
    build_intake_payload,
    document_ref_from_stored,
    evidence_facts_from_extraction,
    extraction_from_payload,
    reviewed_evidence_facts,
)
from services.profile_questionnaire import (
    calculate_behavior_profile,
    load_behavior_questionnaire,
)
from services.room_intake import (
    allowed_room_types,
    confirmed_room_details,
    default_room_drafts,
    room_drafts_from_candidate,
)


st.set_page_config(
    page_title="Розумний вибір дому",
    page_icon=":material/home_iot_device:",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    .stApp { background: #f7f8fa; }
    .block-container { max-width: 1120px; padding-top: 2rem; padding-bottom: 4rem; }
    h1, h2, h3 { letter-spacing: 0 !important; color: #182028; }
    [data-testid="stSidebar"] { background: #eef2f1; }
    .app-kicker { color: #d95f2a; font-size: .82rem; font-weight: 700; text-transform: uppercase; }
    .app-lead { color: #53606b; max-width: 720px; font-size: 1.03rem; }
    .step-label { color: #53606b; font-size: .9rem; margin-bottom: .35rem; }
    .status-label { font-weight: 700; font-size: .82rem; text-transform: uppercase; }
    .status-recommended { color: #16754a; }
    .status-optional { color: #b35320; }
    .status-inexpedient { color: #7a4e62; }
    .status-not_applicable { color: #68737d; }
    .system-description { color: #53606b; margin-top: -.35rem; }
    .history-meta { color: #68737d; font-size: .88rem; }
    div[data-testid="stMetric"] { background: white; border: 1px solid #dfe5e7; padding: 12px 14px; border-radius: 6px; }
    div[data-testid="stForm"] { background: white; border: 1px solid #dfe5e7; border-radius: 6px; padding: 18px; }
    </style>
    """,
    unsafe_allow_html=True,
)

STEP_TITLES = (
    "Житло та пріоритети",
    "Комфорт і щоденні сценарії",
    "Безпека",
    "Енергетика та ділянка",
)

PROFILE_STEP_SECTIONS = (
    ("daily_life", "comfort"),
    ("safety",),
    ("resilience", "media"),
    ("automation", "scope"),
)

PROFILE_STEP_TITLES = (
    "Щоденні звички та комфорт",
    "Безпека і контроль",
    "Стійкість та мультимедіа",
    "Автоматизація і масштаб реалізації",
)

BUDGET_LABELS = {
    BudgetLevel.BASIC: "Обмежений бюджет",
    BudgetLevel.BALANCED: "Збалансований бюджет",
    BudgetLevel.FLEXIBLE: "Гнучкий бюджет",
}

BUDGET_DESCRIPTIONS = {
    BudgetLevel.BASIC: "Фокус сценарію на функціях першої черги.",
    BudgetLevel.BALANCED: "Фокус сценарію на першій і другій черзі.",
    BudgetLevel.FLEXIBLE: "У фокусі весь вибраний обсяг у збережених фазах.",
}

PHASE_LABELS = {
    ImplementationPhase.FIRST: "У першу чергу",
    ImplementationPhase.SECOND: "У другу чергу",
    ImplementationPhase.LATER: "При бажанні",
}


@st.cache_data
def load_config() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    questions, systems, _, _ = load_active_configuration()
    return questions["questions"], systems["systems"]


@st.cache_resource
def load_function_configuration():
    catalog = load_function_catalog()
    return catalog, load_function_rules(catalog=catalog)


@st.cache_data
def load_v2_intake_configuration():
    return load_object_parameter_registry(), load_behavior_questionnaire()


@st.cache_data(show_spinner=False, max_entries=4)
def cached_pdf_page_previews(data: bytes):
    return inspect_pdf_pages(data)


def init_state() -> None:
    defaults = {
        "view": "Нова оцінка",
        "navigation_view": "Нова оцінка",
        "pending_navigation_view": None,
        "step": 0,
        "answers": {},
        "result": None,
        "saved_record_id": None,
        "object_step_active": False,
        "project_assessment_id": None,
        "project_revision": None,
        "object_rooms": [],
        "object_selected_types": [],
        "function_step_active": False,
        "function_step_message": None,
        "function_step_warnings": [],
        "budget_step_active": False,
        "technical_step_active": False,
        "summary_step_active": False,
        "budget_step_message": None,
        "v2_intake_phase": "source",
        "v2_intake_id": uuid4().hex,
        "v2_intake_revision": None,
        "v2_document": None,
        "v2_extraction": None,
        "v2_object_facts": [],
        "v2_object_report": None,
        "v2_object_complete": False,
        "v2_profile_step": 0,
        "v2_profile_answers": {},
        "v2_behavior_profile": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def reset_assessment() -> None:
    st.session_state.pop("plan_preview_bytes", None)
    st.session_state.pop("plan_previews", None)
    for key in list(st.session_state):
        if (
            key.startswith("q_")
            or key.startswith("object_")
            or key.startswith("function_")
            or key.startswith("technical_")
            or key.startswith("v2_")
        ):
            del st.session_state[key]
    st.session_state.step = 0
    st.session_state.answers = {}
    st.session_state.result = None
    st.session_state.saved_record_id = None
    st.session_state.object_step_active = False
    st.session_state.project_assessment_id = None
    st.session_state.project_revision = None
    st.session_state.object_rooms = []
    st.session_state.object_selected_types = []
    st.session_state.function_step_active = False
    st.session_state.function_step_message = None
    st.session_state.function_step_warnings = []
    st.session_state.budget_step_active = False
    st.session_state.technical_step_active = False
    st.session_state.summary_step_active = False
    st.session_state.budget_step_message = None
    st.session_state.v2_intake_phase = "source"
    st.session_state.v2_intake_id = uuid4().hex
    st.session_state.v2_intake_revision = None
    st.session_state.v2_document = None
    st.session_state.v2_extraction = None
    st.session_state.v2_object_facts = []
    st.session_state.v2_object_report = None
    st.session_state.v2_object_complete = False
    st.session_state.v2_profile_step = 0
    st.session_state.v2_profile_answers = {}
    st.session_state.v2_behavior_profile = None
    st.session_state.view = "Нова оцінка"
    st.session_state.pending_navigation_view = "Нова оцінка"


@st.cache_resource
def project_database() -> Database:
    if os.getenv("SMART_HOME_COMMUNITY_CLOUD") != "1":
        return Database()
    database = Database(neon_url(st.secrets.get("DATABASE_URL")))
    try:
        upgrade_community_database(database)
    except Exception:
        database.dispose()
        raise
    return database


def project_repository() -> AssessmentRepository:
    owner_key = None
    if cloud_mode():
        owner_key = google_owner_key(st.user, allowed_emails())
        if owner_key is None:
            raise RuntimeError("A verified project owner is required")
    return AssessmentRepository(project_database(), owner_key=owner_key)


def show_operation_error(message: str, error: Exception) -> None:
    st.error(f"{message}. Спробуйте ще раз." if cloud_mode() else f"{message}: {error}")


def require_cloud_login() -> None:
    emails = allowed_emails()
    if google_owner_key(st.user, emails):
        return
    st.title("Розумний вибір")
    st.caption("Персональний підбір систем для квартири або будинку")
    if st.user.get("is_logged_in") is True:
        st.warning("Для цього облікового запису немає доступу. Увійдіть з адреси, на яку отримали запрошення.")
        if st.button("Змінити обліковий запис"):
            st.logout()
    elif not emails:
        st.info("Вхід буде доступний після налаштування запрошень.")
    else:
        try:
            auth = st.secrets["auth"]
        except (KeyError, FileNotFoundError):
            auth = {}
        if not auth:
            st.info("Вхід через Google ще налаштовується.")
            st.stop()
        st.info("Увійдіть через Google, щоб створити проєкт і повернутися до своїх анкет.")
        if st.button("Увійти через Google", type="primary"):
            st.login()
    st.stop()


def persist_v2_intake_draft(
    provider: str,
    *,
    extraction: PlanExtraction | None,
    document: DocumentRef | None = None,
    evidence_facts: list[EvidenceFact] | None = None,
    answers: dict[str, Any] | None = None,
    report: CompletenessReport | None = None,
    phase: str,
) -> Assessment:
    notice = database_configuration_notice(os.getenv("DATABASE_URL"))
    if notice:
        raise RuntimeError(notice)
    repository = project_repository()
    assessment_id = st.session_state.v2_intake_id
    profile_answers = st.session_state.get("v2_profile_answers", {})
    raw_payload = build_intake_payload(
        phase=phase,
        extraction=extraction,
        report=report,
        profile_answers=profile_answers,
    )
    document_payload = document.model_dump(mode="json") if document is not None else None
    evidence_payloads = (
        [fact.model_dump(mode="json") for fact in evidence_facts]
        if evidence_facts is not None
        else None
    )
    current = repository.get(assessment_id)
    config_versions = {
        "ux_flow": "v2",
        "object_registry": "object_parameters_v2",
    }
    if current is None:
        current = Assessment(
            assessment_id=assessment_id,
            workflow_stage=WorkflowStage.DRAFT,
            provider=provider,
            config_versions=config_versions,
            answers=answers or {},
            documents=[document_payload] if document_payload is not None else [],
            evidence_facts=evidence_payloads or [],
            property_type=(
                PropertyType(answers["property_type"])
                if answers and answers.get("property_type")
                else None
            ),
            area_m2=(answers.get("home_area_m2") if answers else None),
            floor_count=(answers.get("floor_count") if answers else None),
            raw_payload=raw_payload,
        )
        repository.save(current)
    else:
        current = repository.save_intake_step(
            assessment_id,
            current.revision,
            provider=provider,
            config_versions=config_versions,
            raw_payload=raw_payload,
            documents=([document_payload] if document_payload is not None else None),
            evidence_facts=evidence_payloads,
            property_type=(
                PropertyType(answers["property_type"])
                if answers and answers.get("property_type")
                else None
            ),
            area_m2=(answers.get("home_area_m2") if answers else None),
            floor_count=(answers.get("floor_count") if answers else None),
            answers=answers,
        )
    st.session_state.v2_intake_revision = current.revision
    return current


def restore_v2_intake_draft(assessment: Assessment) -> None:
    for key in list(st.session_state):
        if key.startswith("v2_"):
            del st.session_state[key]
    intake_payload = (assessment.raw_payload or {}).get("intake", {})
    extraction = extraction_from_payload(assessment.raw_payload)
    fact_states = {
        ReviewStatus.PENDING: "extracted",
        ReviewStatus.CONFIRMED: "confirmed",
        ReviewStatus.CORRECTED: "corrected",
        ReviewStatus.REJECTED: "rejected",
    }
    object_facts = [
        ObjectFactValue(
            field_id=fact.field_id,
            value=(
                fact.corrected_value
                if fact.review_status == ReviewStatus.CORRECTED
                else fact.value
            ),
            state=fact_states[fact.review_status],
            source=fact.source.value,
            confidence=fact.reliability_level.value,
        )
        for fact in assessment.evidence_facts
    ]
    report_payload = intake_payload.get("completeness")
    if object_facts:
        registry, _ = load_v2_intake_configuration()
        report = calculate_completeness(registry, object_facts)
    else:
        report = (
            CompletenessReport.model_validate(report_payload)
            if report_payload
            else None
        )
    profile_answers = dict(intake_payload.get("profile_answers") or {})
    profile_step = 0
    if profile_answers:
        _, questionnaire = load_v2_intake_configuration()
        for index, sections in enumerate(PROFILE_STEP_SECTIONS):
            section_ids = {
                item.id for item in questionnaire.questions if item.section in sections
            }
            if not section_ids.issubset(profile_answers):
                profile_step = index
                break
        else:
            profile_step = len(PROFILE_STEP_SECTIONS) - 1

    st.session_state.v2_intake_id = assessment.assessment_id
    st.session_state.v2_intake_revision = assessment.revision
    st.session_state.v2_document = assessment.documents[0] if assessment.documents else None
    st.session_state.v2_extraction = extraction
    st.session_state.v2_object_facts = object_facts
    st.session_state.v2_object_report = report
    st.session_state.v2_object_complete = bool(
        assessment.property_type and report and report.recommendation_ready
    )
    st.session_state.v2_profile_step = profile_step
    st.session_state.v2_profile_answers = profile_answers
    st.session_state.v2_behavior_profile = None
    st.session_state.v2_intake_phase = (
        "review" if extraction is not None else "source"
    )
    st.session_state.answers = dict(assessment.answers)
    st.session_state.result = None
    st.session_state.saved_record_id = None
    st.session_state.object_step_active = False
    st.session_state.function_step_active = False
    st.session_state.budget_step_active = False
    st.session_state.technical_step_active = False
    st.session_state.summary_step_active = False
    st.session_state.project_assessment_id = assessment.assessment_id
    st.session_state.project_revision = assessment.revision
    st.session_state.view = "Нова оцінка"
    st.session_state.pending_navigation_view = "Нова оцінка"


def activate_saved_project(assessment: Assessment) -> None:
    """Restore the last persisted workflow step without recomputation."""
    st.session_state.project_assessment_id = assessment.assessment_id
    st.session_state.project_revision = assessment.revision
    st.session_state.object_rooms = assessment.rooms
    st.session_state.object_step_active = False
    st.session_state.function_step_active = False
    st.session_state.budget_step_active = False
    st.session_state.technical_step_active = False
    st.session_state.summary_step_active = False
    if assessment.config_versions.get("ux_flow") == "v2":
        if assessment.workflow_stage == WorkflowStage.DRAFT:
            restore_v2_intake_draft(assessment)
            return
        if assessment.function_choices and assessment.budget is not None:
            st.session_state.summary_step_active = True
        elif assessment.rooms:
            st.session_state.function_step_active = True
        else:
            if assessment.property_type is None:
                raise ValueError("saved project has no property type")
            initialize_object_step_state(assessment.property_type, assessment.rooms)
            st.session_state.object_step_active = True
        st.session_state.view = "Нова оцінка"
        st.session_state.pending_navigation_view = "Нова оцінка"
        return
    if assessment.budget is not None:
        st.session_state.summary_step_active = True
    elif assessment.function_choices:
        st.session_state.budget_step_active = True
    elif assessment.rooms:
        st.session_state.function_step_active = True
    else:
        if assessment.property_type is None:
            raise ValueError("saved project has no property type")
        initialize_object_step_state(assessment.property_type, assessment.rooms)
        st.session_state.object_step_active = True
    st.session_state.view = "Нова оцінка"
    st.session_state.pending_navigation_view = "Нова оцінка"


def initialize_object_step_state(
    property_type: PropertyType,
    existing_rooms: list[Room] | None = None,
) -> None:
    counts = default_location_counts(property_type)
    if existing_rooms:
        counts = {}
        for room in existing_rooms:
            counts[room.room_type] = counts.get(room.room_type, 0) + 1
    selected_types = [
        item.value for item in counts if item != RoomType.WHOLE_PROPERTY
    ]
    st.session_state.object_selected_types = selected_types
    for room_type, count in counts.items():
        st.session_state[f"object_count_{room_type.value}"] = count
    st.session_state.object_rooms = reconcile_rooms(
        property_type,
        counts,
        existing_rooms,
    )


def open_object_step(result: dict[str, Any]) -> None:
    notice = database_configuration_notice(os.getenv("DATABASE_URL"))
    if notice:
        st.error(notice)
        return

    assessment_id = st.session_state.saved_record_id or uuid4().hex
    try:
        repository = project_repository()
        assessment = repository.get(assessment_id)
        if assessment is None:
            assessment = assessment_from_recommendation_result(
                st.session_state.answers,
                result,
                assessment_id=assessment_id,
            )
            repository.save(assessment)
        intake_rooms = st.session_state.answers.get("rooms", [])
        if (
            assessment.config_versions.get("ux_flow") == "v2"
            and intake_rooms
            and not assessment.rooms
        ):
            counts: dict[RoomType, int] = {RoomType.WHOLE_PROPERTY: 1}
            for item in intake_rooms:
                if not isinstance(item, dict) or not item.get("type"):
                    continue
                room_type = RoomType(item["type"])
                counts[room_type] = counts.get(room_type, 0) + int(item.get("count", 1))
            confirmed_rooms = []
            for item in st.session_state.answers.get("room_details", []):
                if not isinstance(item, dict):
                    continue
                confirmed_rooms.append(
                    Room(
                        name=item["name"],
                        room_type=RoomType(item["type"]),
                        area_m2=item.get("area_m2"),
                        floor=1,
                    )
                )
            counts[RoomType.ENTRANCE] = max(
                1, int(st.session_state.answers.get("entrance_count", 1))
            )
            if st.session_state.answers.get("electrical_panel_present", True):
                counts[RoomType.ELECTRICAL_PANEL] = 1
            if st.session_state.answers.get("water_entry_present", False):
                counts[RoomType.WATER_ENTRY] = 1
            if assessment.property_type == PropertyType.HOUSE:
                special_flags = {
                    RoomType.PLOT: "private_plot",
                    RoomType.PERIMETER: "perimeter_present",
                    RoomType.GATE: "gate_garage_present",
                    RoomType.GARDEN: "irrigation_present",
                    RoomType.SEPTIC: "septic_present",
                    RoomType.ROOF: "roof_gutters_present",
                    RoomType.GUTTER: "roof_gutters_present",
                }
                for room_type, answer_id in special_flags.items():
                    if st.session_state.answers.get(answer_id, False):
                        counts[room_type] = 1
            generated_rooms = reconcile_rooms(
                assessment.property_type,
                counts,
                confirmed_rooms,
            )
            assessment = repository.save_object_step(
                assessment.assessment_id,
                assessment.revision,
                project_name=f"Проєкт {datetime.now():%d.%m.%Y}",
                area_m2=float(assessment.area_m2 or 60),
                floor_count=int(assessment.floor_count or 1),
                rooms=[room.model_dump(mode="json") for room in generated_rooms],
            )
        st.session_state.project_assessment_id = assessment.assessment_id
        st.session_state.project_revision = assessment.revision
        if assessment.property_type is None:
            raise ValueError("assessment property_type is required")
        initialize_object_step_state(assessment.property_type, assessment.rooms)
        if assessment.config_versions.get("ux_flow") == "v2" and assessment.rooms:
            st.session_state.object_step_active = False
            st.session_state.function_step_active = True
        else:
            st.session_state.object_step_active = True
        st.rerun()
    except AssessmentAlreadyExistsError:
        st.error("Проєкт із цим ідентифікатором уже існує. Оновіть сторінку.")
    except ValueError as error:
        show_operation_error("Не вдалося підготувати дані проєкту", error)
    except (SQLAlchemyError, RuntimeError):
        st.error(
            "Не вдалося відкрити сховище проєкту. Перевірте PostgreSQL і виконайте "
            "`python -m alembic upgrade head`."
        )


def render_question(question: dict[str, Any], answers: dict[str, Any]) -> None:
    question_id = question["id"]
    key = f"q_{question_id}"
    label = question["text"]
    question_type = question["type"]
    current = answers.get(question_id)

    if question_type == "single_choice":
        options = question["options"]
        index = options.index(current) if current in options else 0
        value = st.selectbox(label, options, index=index, format_func=option_label, key=key)
    elif question_type == "multi_choice":
        options = question["options"]
        default = current if isinstance(current, list) else (["none"] if "none" in options else [])
        value = st.multiselect(label, options, default=default, format_func=option_label, key=key)
        value = normalize_multi_choice(value)
    elif question_type == "scale":
        value = st.slider(
            label,
            min_value=int(question["min"]),
            max_value=int(question["max"]),
            value=int(current if current is not None else 3),
            key=key,
        )
    elif question_type == "integer":
        minimum = int(question["min"])
        maximum = int(question["max"])
        suggested_defaults = {
            "home_area_m2": 60 if answers.get("property_type") == "apartment" else 150,
            "floor_count": 1 if answers.get("property_type") == "apartment" else 2,
            "household_size": 2,
            "water_zones": 2,
        }
        default = int(current if current is not None else suggested_defaults.get(question_id, minimum))
        value = st.number_input(
            label, min_value=minimum, max_value=maximum, value=default, step=1, key=key
        )
    elif question_type == "boolean":
        options = [True, False]
        index = 0 if current is True else 1
        value = st.radio(
            label,
            options,
            index=index,
            format_func=lambda item: "Так" if item else "Ні",
            horizontal=True,
            key=key,
        )
    else:
        raise ValueError(f"Unsupported question type: {question_type}")
    answers[question_id] = value


def _candidate_value(field_id: str, default: Any = None) -> Any:
    extraction = st.session_state.v2_extraction
    if extraction is None:
        return default
    for item in extraction.candidates:
        if item.field_id == field_id:
            return item.value
    return default


def _candidate_facts() -> list[ObjectFactValue]:
    extraction = st.session_state.v2_extraction
    if extraction is None:
        return []
    return [
        ObjectFactValue(
            field_id=item.field_id,
            value=item.value,
            state="extracted",
            source=(
                "document_inferred"
                if item.evidence_type == "visual_inference"
                else "document_explicit"
            ),
            confidence=item.reliability_level,
        )
        for item in extraction.candidates
    ]


def _candidate_map() -> dict[str, Any]:
    extraction = st.session_state.v2_extraction
    if extraction is None:
        return {}
    return {item.field_id: item for item in extraction.candidates}


def _review_field_label(field_id: str, label: str) -> str:
    return f"{label} · з плану" if field_id in _candidate_map() else label


def _candidate_display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Так" if value else "Ні"
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type"):
                try:
                    label = location_label(str(item["type"]))
                except ValueError:
                    label = _candidate_display_value(item["type"])
                parts.append(f"{label}: {_candidate_display_value(item.get('count', 1))}")
            else:
                parts.append(_candidate_display_value(item))
        return ", ".join(parts) if parts else "Порожній перелік"
    if isinstance(value, dict):
        if value.get("name"):
            name = str(value["name"]).strip()
            area = value.get("area_m2")
            if type(area) in {int, float}:
                return f"{name} ({area:g} м²)"
            return name
        return ", ".join(
            f"{option_label(key)}: {_candidate_display_value(item)}"
            for key, item in value.items()
        )
    return option_label(value)


def _plan_parameter_groups(registry) -> tuple[list[Any], list[Any]]:
    facts = _candidate_facts()
    fact_map = {item.field_id: item for item in facts}
    applicable = [
        item for item in registry.parameters if parameter_is_applicable(item, fact_map)
    ]
    found_ids = set(_candidate_map())
    return (
        [item for item in applicable if item.field_id in found_ids],
        [item for item in applicable if item.field_id not in found_ids],
    )


def render_object_source_v2(provider: str) -> None:
    registry, _ = load_v2_intake_configuration()
    st.markdown('<div class="app-kicker">Підбір систем · Крок 1</div>', unsafe_allow_html=True)
    st.title("Додайте план житла")
    st.markdown(
        '<div class="app-lead">PDF, PNG або JPG допоможе попередньо знайти '
        "приміщення та інженерні ознаки. План не обов'язковий: усі дані можна "
        "ввести вручну.</div>",
        unsafe_allow_html=True,
    )
    st.progress(0.08)
    if provider == "openai":
        st.caption(
            "Хмарна модель шукає лише факти про об'єкт. Усі знайдені значення "
            "потрібно буде перевірити перед розрахунком."
        )
    uploaded = st.file_uploader(
        "План квартири або будинку",
        type=["pdf", "png", "jpg", "jpeg"],
        help="Максимальний розмір одного файла: 25 МБ.",
        disabled=provider == "rules",
    )
    if provider == "rules":
        st.caption("Для оцінки за правилами заповніть дані про житло вручну.")
    mime_type = ""
    data = b""
    selected_pages: list[int] = []
    document_valid = uploaded is not None
    if uploaded is not None:
        mime_type = uploaded.type or {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }.get(Path(uploaded.name).suffix.lower(), "")
        data = uploaded.getvalue()
        try:
            validate_document(uploaded.name, mime_type, data)
            if mime_type == "application/pdf":
                if cloud_mode():
                    if st.session_state.get("plan_preview_bytes") != data:
                        st.session_state.plan_previews = inspect_pdf_pages(data)
                        st.session_state.plan_preview_bytes = data
                    previews = st.session_state.plan_previews
                else:
                    previews = cached_pdf_page_previews(data)
                labels = {page.number: page.label for page in previews}
                fingerprint = hashlib.sha256(data).hexdigest()[:12]
                selected_pages = st.multiselect(
                    "Сторінки для аналізу",
                    options=[page.number for page in previews],
                    default=suggested_pdf_pages(previews),
                    format_func=lambda number: f"Стор. {number} · {labels[number]}",
                    key=f"v2_selected_plan_pages_{fingerprint}",
                    help=f"Можна вибрати до {MAX_ANALYSIS_PAGES} сторінок одного PDF.",
                )
                if len(previews) > 1 and all(page.text_length == 0 for page in previews):
                    st.info(
                        "У PDF немає текстового шару. Перевірте ескізи: "
                        "якщо є плани інших поверхів, додайте ці сторінки до аналізу."
                    )
                with st.expander(f"Ескізи сторінок · {len(previews)}"):
                    for start in range(0, len(previews), 4):
                        columns = st.columns(4)
                        for column, page in zip(columns, previews[start:start + 4]):
                            with column:
                                st.image(page.thumbnail_png, use_container_width=True)
                                st.caption(f"Стор. {page.number} · {page.label}")
                if len(selected_pages) > MAX_ANALYSIS_PAGES:
                    document_valid = False
                    st.warning(
                        f"Для одного аналізу виберіть не більш ніж "
                        f"{MAX_ANALYSIS_PAGES} сторінок."
                    )
                if not selected_pages:
                    document_valid = False
        except ValueError as error:
            document_valid = False
            st.error(str(error))
    consent = True
    if provider == "openai" and uploaded is not None:
        consent = st.checkbox(
            "Погоджуюся передати цей файл хмарній моделі для одноразового аналізу",
            value=False,
        )
    analyze_col, manual_col = st.columns(2)
    analyze = analyze_col.button(
        "Проаналізувати план",
        icon=":material/document_search:",
        type="primary",
        use_container_width=True,
        disabled=not document_valid or not consent,
    )
    manual = manual_col.button(
        "Заповнити без плану",
        icon=":material/edit_note:",
        use_container_width=True,
    )

    if manual:
        extraction = PlanExtraction(
            candidates=[],
            unresolved_field_ids=[item.field_id for item in registry.parameters],
            warnings=[],
        )
        remaining_answers = {
            key: value for key, value in st.session_state.answers.items()
            if key not in {"rooms", "room_details"}
        }
        try:
            persist_v2_intake_draft(
                provider,
                extraction=extraction,
                answers=remaining_answers,
                phase="review",
            )
        except (SQLAlchemyError, RuntimeError, ValueError) as error:
            show_operation_error("Не вдалося зберегти чернетку", error)
            return
        st.session_state.pop("plan_preview_bytes", None)
        st.session_state.pop("plan_previews", None)
        st.session_state.v2_extraction = extraction
        st.session_state.answers = remaining_answers
        st.session_state.v2_object_complete = False
        st.session_state.pop("v2_room_drafts", None)
        st.session_state.v2_intake_phase = "review"
        st.rerun()

    if not analyze or uploaded is None or provider == "rules":
        return
    try:
        llm_config = load_json_compatible_yaml(PROTOTYPE_ROOT / "config" / "llm.yaml")
        if cloud_mode():
            llm_config["cache_enabled"] = False
            document = DocumentRef(
                original_name=uploaded.name,
                mime_type=mime_type,
                stored_path="not_retained",
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        else:
            stored = store_document_locally(
                st.session_state.v2_intake_id, uploaded.name, mime_type, data
            )
            document = document_ref_from_stored(stored)
        extractor = build_plan_extractor(provider, llm_config)
        progress = st.empty()
        with st.spinner("Аналізуємо план..."):
            if mime_type == "application/pdf" and provider == "openai":
                extraction = extractor.extract_selected_pdf_pages(
                    uploaded.name,
                    data,
                    registry,
                    selected_pages,
                    on_progress=lambda index, total, page: progress.caption(
                        f"Сторінка {page} · {index} із {total}"
                    ),
                )
            else:
                extraction = extractor.extract(
                    uploaded.name, mime_type, data, registry
                )
        progress.empty()
        remaining_answers = {
            key: value for key, value in st.session_state.answers.items()
            if key not in {"rooms", "room_details"}
        }
        persist_v2_intake_draft(
            provider,
            extraction=extraction,
            document=document,
            evidence_facts=evidence_facts_from_extraction(extraction, document),
            answers=remaining_answers,
            phase="review",
        )
        st.session_state.v2_document = document
        st.session_state.pop("plan_preview_bytes", None)
        st.session_state.pop("plan_previews", None)
        st.session_state.v2_extraction = extraction
        st.session_state.answers = remaining_answers
        st.session_state.v2_object_complete = False
        st.session_state.pop("v2_room_drafts", None)
        st.session_state.v2_intake_phase = "review"
        st.rerun()
    except (SQLAlchemyError, RuntimeError, ValueError) as error:
        show_operation_error("Не вдалося проаналізувати файл", error)


def _yes_no(label: str, *, value: bool, key: str) -> bool:
    return st.radio(
        label,
        [True, False],
        index=0 if value else 1,
        format_func=lambda item: "Так" if item else "Ні",
        horizontal=True,
        key=key,
    )


def render_object_review_v2(provider: str) -> None:
    registry, _ = load_v2_intake_configuration()
    candidate_report = calculate_completeness(registry, _candidate_facts())
    found_parameters, missing_parameters = _plan_parameter_groups(registry)
    candidates = _candidate_map()
    st.markdown('<div class="app-kicker">Підбір систем · Крок 2</div>', unsafe_allow_html=True)
    st.title("Перевірте дані про об'єкт")
    st.markdown(
        '<div class="app-lead">Значення з плану є лише пропозиціями. '
        "Підтвердіть або виправте їх, а відсутні параметри заповніть вручну.</div>",
        unsafe_allow_html=True,
    )
    metric_a, metric_b = st.columns(2)
    metric_a.metric(
        "Покриття планом",
        f"{candidate_report.plan_coverage_percent}%",
    )
    metric_a.caption(
        f"Знайдено {len(found_parameters)} із "
        f"{len(found_parameters) + len(missing_parameters)} застосовних параметрів. "
        "Відсоток зважений: ключові параметри мають більшу вагу."
    )
    metric_b.metric("Підтверджено для рекомендації", "Ще не підтверджено")
    metric_b.caption(
        "Після перевірки всіх обов'язкових полів цей показник стане 100%."
    )
    with st.expander(
        f"Що модель побачила на плані · {len(found_parameters)} параметрів",
        expanded=True,
    ):
        found_col, missing_col = st.columns(2)
        with found_col:
            st.markdown("**Знайдено на плані**")
            if not found_parameters:
                st.caption("Модель не знайшла надійних параметрів.")
            for parameter in found_parameters:
                candidate = candidates[parameter.field_id]
                reliability = {
                    "high": "висока надійність",
                    "medium": "середня надійність",
                    "low": "низька надійність",
                }[candidate.reliability_level]
                page = f" · стор. {candidate.page_number}" if candidate.page_number else ""
                st.markdown(
                    f"- **{parameter.label_uk}:** "
                    f"{_candidate_display_value(candidate.value)}"
                )
                st.caption(f"{reliability}{page} · {candidate.source_reference}")
        with missing_col:
            st.markdown("**Не знайдено, потрібно перевірити**")
            if not missing_parameters:
                st.caption("Модель знайшла всі застосовні параметри реєстру.")
            else:
                st.markdown(
                    "\n".join(f"- {item.label_uk}" for item in missing_parameters)
                )
    if st.session_state.v2_extraction:
        for warning in st.session_state.v2_extraction.warnings:
            st.info(warning)
    if st.button("Назад до завантаження", icon=":material/arrow_back:"):
        st.session_state.v2_intake_phase = "source"
        st.rerun()

    property_default = _candidate_value("property_type", "apartment")
    property_type = st.radio(
        _review_field_label("property_type", "Тип житла"),
        ["apartment", "house"],
        index=0 if property_default != "house" else 1,
        format_func=option_label,
        horizontal=True,
        key="v2_property_type",
    )
    extracted_rooms = _candidate_value("rooms", [])
    if "v2_room_drafts" not in st.session_state:
        saved = st.session_state.answers.get("room_details")
        if isinstance(saved, list) and saved:
            st.session_state.v2_room_drafts = [
                {**item, "id": f"saved_{index}"}
                for index, item in enumerate(saved)
                if isinstance(item, dict)
            ]
        else:
            st.session_state.v2_room_drafts = (
                room_drafts_from_candidate(extracted_rooms, property_type)
                or default_room_drafts()
            )
    room_types = allowed_room_types(property_type)

    with st.container():
        general_left, general_right = st.columns(2)
        project_stage = general_left.selectbox(
            _review_field_label("project_stage", "Стадія об'єкта"),
            ["planning", "renovation", "completed", "reconstruction"],
            index=["planning", "renovation", "completed", "reconstruction"].index(
                _candidate_value("project_stage", "renovation")
                if _candidate_value("project_stage", "renovation")
                in {"planning", "renovation", "completed", "reconstruction"}
                else "renovation"
            ),
            format_func=option_label,
        )
        area_candidate = _candidate_value("home_area_m2")
        area_default = plan_area_m2(area_candidate)
        if area_default is None:
            if area_candidate is not None:
                st.warning("Площу з плану не вдалося прочитати. Вкажіть її вручну.")
            area_default = 60.0 if property_type == "apartment" else 150.0
        home_area_m2 = general_right.number_input(
            _review_field_label("home_area_m2", "Загальна площа, м²"),
            min_value=10.0,
            max_value=10000.0,
            value=area_default,
            step=0.01,
            format="%.2f",
        )
        floor_count = general_left.number_input(
            _review_field_label("floor_count", "Кількість рівнів"),
            min_value=1,
            max_value=100,
            value=int(_candidate_value("floor_count", 1 if property_type == "apartment" else 2)),
        )
        entrance_count = general_right.number_input(
            _review_field_label("entrance_count", "Кількість окремих входів"),
            min_value=1,
            max_value=30,
            value=int(_candidate_value("entrance_count", 1)),
        )

        st.markdown(f"**{_review_field_label('rooms', 'Приміщення')}**")
        st.caption("Перевірте назву й тип кожного приміщення. Невизначені типи оберіть вручну.")
        room_drafts = st.session_state.v2_room_drafts
        room_values = []
        remove_room = None
        for index, draft in enumerate(room_drafts):
            room_id = draft["id"]
            name_col, type_col, area_col, remove_col = st.columns([3, 3, 1.4, 0.5])
            name = name_col.text_input(
                f"Назва приміщення {index + 1}",
                value=draft["name"],
                key=f"v2_room_name_{room_id}",
            )
            current_type = draft.get("type")
            room_type = type_col.selectbox(
                f"Тип приміщення {index + 1}",
                room_types,
                index=(
                    room_types.index(RoomType(current_type))
                    if current_type in {item.value for item in room_types}
                    else None
                ),
                format_func=location_label,
                placeholder="Уточніть тип",
                key=f"v2_room_type_{room_id}",
            )
            area = area_col.number_input(
                f"Площа {index + 1}, м²",
                min_value=0.01,
                max_value=10000.0,
                value=draft.get("area_m2"),
                step=0.1,
                format="%.2f",
                key=f"v2_room_area_{room_id}",
            )
            if remove_col.button(
                ":material/delete:",
                key=f"v2_room_remove_{room_id}",
                help=f"Прибрати приміщення {index + 1}",
            ):
                remove_room = room_id
            room_values.append({
                "id": room_id,
                "name": name,
                "type": room_type.value if room_type else None,
                "area_m2": area,
                "origin": draft.get("origin", "manual"),
            })
        unresolved_count = sum(item["type"] is None for item in room_values)
        if unresolved_count:
            st.warning(f"Потрібно уточнити тип: {unresolved_count} приміщень.")
        add_room = st.button("Додати приміщення", icon=":material/add:")
        if add_room or remove_room is not None:
            st.session_state.v2_room_drafts = [
                item for item in room_values if item["id"] != remove_room
            ]
            if add_room:
                st.session_state.v2_room_drafts.append({
                    "id": uuid4().hex,
                    "name": "",
                    "type": None,
                    "area_m2": None,
                    "origin": "manual",
                })
            st.rerun()
        room_counts: dict[str, int] = {}
        for item in room_values:
            if item["type"]:
                room_counts[item["type"]] = room_counts.get(item["type"], 0) + 1

        st.markdown("**Інженерні дані**")
        engineering_left, engineering_right = st.columns(2)
        water_zones = engineering_left.number_input(
            _review_field_label("water_zones", "Кількість водних зон"),
            min_value=0,
            max_value=100,
            value=int(_candidate_value("water_zones", 2)),
        )
        electrical_panel_present = engineering_right.checkbox(
            _review_field_label(
                "electrical_panel_present", "Є або запланований електрощит"
            ),
            value=bool(_candidate_value("electrical_panel_present", True)),
        )
        heating_systems = engineering_left.multiselect(
            _review_field_label("heating_systems", "Опалення"),
            ["underfloor_heating", "radiators", "convectors", "fan_coils", "boiler", "other", "none"],
            default=_candidate_value("heating_systems", ["radiators"]),
            format_func=option_label,
        )
        cooling_systems = engineering_right.multiselect(
            _review_field_label("cooling_systems", "Охолодження"),
            ["air_conditioners", "fan_coils", "central_cooling", "other", "none"],
            default=_candidate_value("cooling_systems", ["air_conditioners"]),
            format_func=option_label,
        )
        ventilation_equipment = engineering_left.multiselect(
            _review_field_label(
                "ventilation_equipment", "Вентиляція та підготовка повітря"
            ),
            ["exhaust", "supply_exhaust", "heat_recovery", "humidifier", "other", "none"],
            default=_candidate_value("ventilation_equipment", ["none"]),
            format_func=option_label,
        )
        equipment_room_slots: dict[str, list[str]] = {}
        room_slot_labels: dict[str, str] = {}
        room_name_counts: dict[str, int] = {}
        for item in room_values:
            if item["type"]:
                name = item["name"].strip() or location_label(item["type"])
                room_name_counts[name] = room_name_counts.get(name, 0) + 1
        room_ordinals: dict[str, int] = {}
        seen_names: dict[str, int] = {}
        for item in room_values:
            room_type_value = item["type"]
            if room_type_value is None:
                continue
            ordinal = room_ordinals.get(room_type_value, 0) + 1
            room_ordinals[room_type_value] = ordinal
            slot = room_slot(room_type_value, ordinal)
            name = item["name"].strip() or location_label(room_type_value)
            seen_names[name] = seen_names.get(name, 0) + 1
            suffix = f" {seen_names[name]}" if room_name_counts[name] > 1 else ""
            room_slot_labels[slot] = name + suffix
        equipment_groups = (
            ("underfloor_heating", "Де є або планується тепла підлога?",
             "underfloor_heating" in heating_systems),
            ("radiators", "Де є або плануються радіатори?",
             "radiators" in heating_systems),
            ("cooling", "Де є або планується охолодження?",
             bool(set(cooling_systems) - {"none"})),
            ("exhaust", "Де є або плануються витяжні вентилятори?",
             "exhaust" in ventilation_equipment),
        )
        if any(enabled for _, _, enabled in equipment_groups):
            st.markdown("**Розміщення інженерного обладнання**")
        saved_equipment_slots = st.session_state.answers.get(
            "equipment_room_slots", {}
        )
        if not isinstance(saved_equipment_slots, dict):
            saved_equipment_slots = {}
        for equipment_key, label, enabled in equipment_groups:
            if not enabled:
                continue
            allowed = EQUIPMENT_ROOM_TYPES[equipment_key]
            options = [
                slot for slot in room_slot_labels
                if RoomType(slot.rsplit(":", 1)[0]) in allowed
            ]
            previous = saved_equipment_slots.get(equipment_key, [])
            defaults = [slot for slot in previous if slot in options]
            equipment_room_slots[equipment_key] = st.multiselect(
                label,
                options,
                default=defaults,
                format_func=room_slot_labels.__getitem__,
                key=f"v2_equipment_rooms_{equipment_key}",
            )
        gas_co_sources = engineering_right.multiselect(
            _review_field_label(
                "gas_co_sources", "Джерела ризику газу або CO"
            ),
            ["gas_stove", "gas_boiler", "fireplace", "solid_fuel_boiler", "generator", "other", "none"],
            default=_candidate_value("gas_co_sources", ["none"]),
            format_func=option_label,
        )
        lighting_group_options = ["one_to_three", "four_to_eight", "nine_or_more"]
        suggested_lighting_groups = _candidate_value(
            "lighting_group_count", "four_to_eight"
        )
        lighting_group_count = engineering_left.selectbox(
            _review_field_label(
                "lighting_group_count", "Орієнтовна кількість груп освітлення"
            ),
            lighting_group_options,
            index=(
                lighting_group_options.index(suggested_lighting_groups)
                if suggested_lighting_groups in lighting_group_options
                else 1
            ),
            format_func=option_label,
        )
        cabling_possible = engineering_right.checkbox(
            _review_field_label(
                "cabling_possible",
                "На поточному етапі можна прокладати нові кабелі",
            ),
            value=bool(_candidate_value("cabling_possible", project_stage != "completed")),
        )
        water_entry_present = engineering_left.checkbox(
            _review_field_label(
                "water_entry_present",
                "Є доступ до вводу води для встановлення перекриття",
            ),
            value=bool(_candidate_value("water_entry_present", True)),
        )
        motorized_shading_present = engineering_right.checkbox(
            _review_field_label(
                "motorized_shading_present",
                "Є або плануються моторизовані штори чи жалюзі",
            ),
            value=bool(_candidate_value("motorized_shading_present", False)),
        )

        st.markdown("**Електроживлення під час відключень**")
        power_left, power_right = st.columns(2)
        outage_options = ["never", "rarely", "monthly", "weekly", "daily"]
        previous_outage = st.session_state.answers.get("outage_frequency", "rarely")
        outage_frequency = power_left.selectbox(
            "Як часто у вас зникає електроенергія?",
            outage_options,
            index=(
                outage_options.index(previous_outage)
                if previous_outage in outage_options
                else 1
            ),
            format_func=option_label,
        )
        duration_options = [
            "under_one_hour",
            "one_to_four_hours",
            "four_to_eight_hours",
            "over_eight_hours",
        ]
        previous_duration = st.session_state.answers.get(
            "outage_duration", "under_one_hour"
        )
        outage_duration = power_right.selectbox(
            "Скільки зазвичай триває відключення?",
            duration_options,
            index=(
                duration_options.index(previous_duration)
                if previous_duration in duration_options
                else 0
            ),
            format_func=option_label,
            disabled=outage_frequency == "never",
        )
        critical_loads = power_left.multiselect(
            "Що має працювати під час відключення?",
            [
                "internet", "lighting", "heating", "water_pumps",
                "refrigerator", "security", "medical_equipment", "other", "none",
            ],
            default=st.session_state.answers.get("critical_loads", ["none"]),
            format_func=option_label,
        )
        backup_power_sources = power_right.multiselect(
            "Яке резервне джерело вже є або планується?",
            ["inverter", "battery", "generator", "other", "none"],
            default=st.session_state.answers.get("backup_power_sources", ["none"]),
            format_func=option_label,
        )
        alert_path_options = [None, True, False]
        previous_alert_path = st.session_state.answers.get(
            "power_notification_path_confirmed"
        )
        power_notification_path_confirmed = power_right.selectbox(
            "Чи працюватимуть контролер та інтернет без електромережі?",
            alert_path_options,
            index=alert_path_options.index(previous_alert_path),
            format_func=lambda value: {
                None: "Поки невідомо",
                True: "Так",
                False: "Ні",
            }[value],
        )

        st.markdown("**Мешканці та умови користування**")
        resident_left, resident_right = st.columns(2)
        household_size = resident_left.number_input(
            "Кількість постійних мешканців", min_value=1, max_value=30, value=2
        )
        vulnerable_residents = resident_right.checkbox(
            "Є діти, літні люди або мешканці з обмеженою мобільністю"
        )
        pets_present = resident_left.checkbox("Є домашні тварини")
        home_staff_present = resident_right.checkbox(
            "У житлі регулярно працює домашній персонал"
        )
        previous_intrusion = resident_left.checkbox(
            "Раніше були проникнення або спроби злому"
        )
        exposed_access_points = resident_right.checkbox(
            "Є легкодоступні вікна або кілька зовнішніх дверей"
        )

        private_plot = False
        perimeter_present = False
        gate_garage_present = False
        pool_present = False
        irrigation_present = False
        septic_present = False
        roof_gutters_present = False
        boiler_room_present = False
        outdoor_ice_risk = False
        weather_sensitive_elements = False
        if property_type == "house":
            st.markdown("**Ділянка та зовнішні зони**")
            outside_left, outside_right = st.columns(2)
            private_plot = outside_left.checkbox(
                _review_field_label("private_plot", "Є приватна ділянка"),
                value=bool(_candidate_value("private_plot", True)),
            )
            perimeter_present = outside_right.checkbox(
                _review_field_label("perimeter_present", "Є власний периметр"),
                value=bool(_candidate_value("perimeter_present", private_plot)),
            )
            gate_garage_present = outside_left.checkbox(
                _review_field_label("gate_garage_present", "Є ворота або гараж"),
                value=bool(_candidate_value("gate_garage_present", False)),
            )
            boiler_room_present = outside_right.checkbox(
                _review_field_label("boiler_room_present", "Є окрема котельня"),
                value=bool(_candidate_value("boiler_room_present", False)),
            )
            pool_present = outside_left.checkbox(
                _review_field_label("pool_present", "Є або планується басейн"),
                value=bool(_candidate_value("pool_present", False)),
            )
            irrigation_present = outside_right.checkbox(
                _review_field_label(
                    "irrigation_present", "Є або планується автоматичний полив"
                ),
                value=bool(_candidate_value("irrigation_present", False)),
            )
            septic_present = outside_left.checkbox(
                _review_field_label(
                    "septic_present", "Є септик або автономна каналізація"
                ),
                value=bool(_candidate_value("septic_present", False)),
            )
            roof_gutters_present = outside_right.checkbox(
                _review_field_label("roof_gutters_present", "Є дах і водостоки"),
                value=bool(_candidate_value("roof_gutters_present", False)),
            )
            outdoor_ice_risk = outside_left.checkbox("Є зовнішні зони з ризиком обмерзання")
            weather_sensitive_elements = outside_right.checkbox("Є маркізи, зовнішні жалюзі або інші погодочутливі елементи")

        submitted = st.button(
            "Підтвердити дані про об'єкт",
            icon=":material/check_circle:",
            type="primary",
            use_container_width=True,
        )

    if not submitted:
        return
    try:
        normalized_rooms, room_details = confirmed_room_details(room_values, property_type)
    except ValueError as error:
        st.error(str(error))
        return
    if (
        not heating_systems
        or not cooling_systems
        or not ventilation_equipment
        or not gas_co_sources
        or not critical_loads
        or not backup_power_sources
    ):
        st.error("Для кожного інженерного переліку виберіть варіант або «Немає».")
        return
    object_values = {
        "property_type": property_type,
        "project_stage": project_stage,
        "home_area_m2": float(home_area_m2),
        "floor_count": int(floor_count),
        "rooms": normalized_rooms,
        "entrance_count": int(entrance_count),
        "water_zones": int(water_zones),
        "electrical_panel_present": bool(electrical_panel_present),
        "heating_systems": normalize_multi_choice(heating_systems),
        "cooling_systems": normalize_multi_choice(cooling_systems),
        "ventilation_equipment": normalize_multi_choice(ventilation_equipment),
        "gas_co_sources": normalize_multi_choice(gas_co_sources),
        "water_entry_present": bool(water_entry_present),
        "motorized_shading_present": bool(motorized_shading_present),
        "cabling_possible": bool(cabling_possible),
        "private_plot": bool(private_plot),
        "perimeter_present": bool(perimeter_present),
        "gate_garage_present": bool(gate_garage_present),
        "pool_present": bool(pool_present),
        "irrigation_present": bool(irrigation_present),
        "septic_present": bool(septic_present),
        "roof_gutters_present": bool(roof_gutters_present),
        "boiler_room_present": bool(boiler_room_present),
        "lighting_group_count": lighting_group_count,
    }
    fact_map = {item.field_id: item for item in registry.parameters}
    facts = []
    for field_id, value in object_values.items():
        parameter = fact_map[field_id]
        applicable = not parameter.required_when or (
            object_values.get(parameter.required_when["field_id"])
            == parameter.required_when.get("equals")
        )
        facts.append(
            ObjectFactValue(
                field_id=field_id,
                value=value if applicable else None,
                state="confirmed" if applicable else "not_applicable",
                source="manual",
                confidence="high",
            )
        )
    for optional_id in ("heating_zones", "cooling_zones", "leak_points", "shading_drives"):
        facts.append(
            ObjectFactValue(
                field_id=optional_id,
                state="unknown",
                source="manual",
            )
        )
    report = calculate_completeness(registry, facts)
    st.session_state.v2_object_facts = facts
    st.session_state.v2_object_report = report
    st.session_state.answers = {
        **object_values,
        "room_details": room_details,
        "equipment_room_slots": equipment_room_slots,
        "household_size": int(household_size),
        "vulnerable_residents": bool(vulnerable_residents),
        "pets_present": bool(pets_present),
        "home_staff_present": bool(home_staff_present),
        "previous_intrusion": bool(previous_intrusion),
        "exposed_access_points": bool(exposed_access_points),
        "outdoor_ice_risk": bool(outdoor_ice_risk),
        "weather_sensitive_elements": bool(weather_sensitive_elements),
        "safe_present": False,
        "backup_power_sources": normalize_multi_choice(backup_power_sources),
        "critical_loads": normalize_multi_choice(critical_loads),
        "outage_frequency": outage_frequency,
        "outage_duration": outage_duration,
        "power_notification_path_confirmed": power_notification_path_confirmed,
    }
    try:
        persist_v2_intake_draft(
            provider,
            extraction=st.session_state.v2_extraction,
            document=st.session_state.v2_document,
            evidence_facts=reviewed_evidence_facts(
                facts,
                st.session_state.v2_extraction,
                st.session_state.v2_document,
            ),
            answers=st.session_state.answers,
            report=report,
            phase="profile",
        )
    except (SQLAlchemyError, RuntimeError, ValueError) as error:
        show_operation_error("Не вдалося зберегти підтверджені дані", error)
        return
    st.session_state.v2_object_complete = report.recommendation_ready
    if not report.recommendation_ready:
        st.error(
            "Поки недостатньо підтверджених даних для рекомендації: "
            + ", ".join(report.missing_required)
        )
        return
    st.rerun()


def render_behavior_questionnaire_v2(provider: str) -> None:
    _, questionnaire = load_v2_intake_configuration()
    step = st.session_state.v2_profile_step
    answers = st.session_state.v2_profile_answers
    sections = PROFILE_STEP_SECTIONS[step]
    questions = [item for item in questionnaire.questions if item.section in sections]
    object_report = st.session_state.v2_object_report

    st.markdown('<div class="app-kicker">Підбір систем · Крок 3</div>', unsafe_allow_html=True)
    st.title("Профіль потреб і сценаріїв користування")
    st.markdown(
        '<div class="app-lead">Питання описують реальні ситуації, а не '
        "просять вас самостійно обирати системи. Результат не є психологічною "
        "діагностикою.</div>",
        unsafe_allow_html=True,
    )
    metric_a, metric_b = st.columns(2)
    metric_a.metric(
        "Готовність даних об'єкта",
        f"{object_report.decision_readiness_percent}%",
    )
    profile_progress = round(100 * (step + 1) / len(PROFILE_STEP_SECTIONS))
    metric_b.metric("Етап анкети", f"{profile_progress}%")
    metric_b.caption(
        f"Частина {step + 1} із {len(PROFILE_STEP_SECTIONS)}; "
        "після останньої частини анкета завершена."
    )
    st.markdown(
        f'<div class="step-label">Частина {step + 1} із {len(PROFILE_STEP_SECTIONS)}</div>',
        unsafe_allow_html=True,
    )
    st.progress((step + 1) / len(PROFILE_STEP_SECTIONS))
    st.subheader(PROFILE_STEP_TITLES[step])

    with st.form(f"v2_profile_form_{step}"):
        for question in questions:
            current = answers.get(question.id)
            index = question.options.index(current) if current in question.options else None
            answers[question.id] = st.selectbox(
                question.text,
                question.options,
                index=index,
                format_func=option_label,
                placeholder="Оберіть відповідь",
                key=f"v2_profile_{question.id}",
            )
        final_step = step == len(PROFILE_STEP_SECTIONS) - 1
        recommendation_provider = provider
        if final_step and cloud_mode() and provider == "openai":
            consent = st.checkbox(
                "Дозволяю передати дані про житло та відповіді анкети до OpenAI для оцінки й пояснень.",
                help="Без цієї згоди оцінка буде сформована за експертними правилами.",
            )
            if not consent:
                recommendation_provider = "rules"
        left, right = st.columns(2)
        back = left.form_submit_button(
            "Назад", icon=":material/arrow_back:", disabled=step == 0, use_container_width=True
        )
        next_label = "Розрахувати рекомендації" if final_step else "Продовжити"
        next_clicked = right.form_submit_button(
            next_label,
            icon=":material/auto_awesome:" if step == len(PROFILE_STEP_SECTIONS) - 1 else ":material/arrow_forward:",
            type="primary",
            use_container_width=True,
        )

    if back:
        st.session_state.v2_profile_step = max(0, step - 1)
        st.rerun()
    if not next_clicked:
        return
    missing_visible = [item.id for item in questions if not answers.get(item.id)]
    if missing_visible:
        st.error("Оберіть відповідь на кожне питання цієї частини.")
        return
    st.session_state.v2_profile_answers = answers
    try:
        draft = persist_v2_intake_draft(
            provider,
            extraction=st.session_state.v2_extraction,
            report=object_report,
            phase="profile",
        )
    except (SQLAlchemyError, RuntimeError, ValueError) as error:
        show_operation_error("Не вдалося зберегти прогрес анкети", error)
        return
    if step < len(PROFILE_STEP_SECTIONS) - 1:
        st.session_state.v2_profile_step += 1
        st.rerun()

    if provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        st.error("Для хмарного аналізу не знайдено OPENAI_API_KEY у файлі .env.")
        return
    profile = calculate_behavior_profile(questionnaire, answers)
    object_answers = dict(st.session_state.answers)
    recommendation_answers = {
        **object_answers,
        **answers,
        **profile.legacy_signals,
        "implementation_scope": profile.scope_level,
        "scope_intensity": profile.scope_intensity,
        "_ux_flow": "v2",
        "_object_registry": "object_parameters_v2",
        "_behavior_questionnaire": questionnaire.questionnaire_id,
    }
    with st.spinner("Оцінюємо застосовність і доцільність усіх 19 систем..."):
        result = recommend_v2(
            object_answers,
            answers,
            profile,
            questionnaire,
            provider=recommendation_provider,
        )
    recommendation_answers["_need_model"] = result["need_model"]
    completed = assessment_from_recommendation_result(
        recommendation_answers,
        result,
        assessment_id=st.session_state.v2_intake_id,
    ).model_copy(
        update={
            "raw_payload": build_intake_payload(
                phase="results",
                extraction=st.session_state.v2_extraction,
                report=object_report,
                profile_answers=answers,
            )
        }
    )
    try:
        assessment = project_repository().save_recommendation_step(
            completed.assessment_id,
            draft.revision,
            completed=completed,
        )
    except (SQLAlchemyError, RuntimeError, ValueError) as error:
        show_operation_error("Не вдалося зберегти рекомендації", error)
        return
    st.session_state.v2_behavior_profile = profile
    st.session_state.answers = recommendation_answers
    st.session_state.result = result
    st.session_state.saved_record_id = assessment.assessment_id
    if not cloud_mode():
        save_history(recommendation_answers, result, record_id=assessment.assessment_id)
    st.session_state.project_assessment_id = assessment.assessment_id
    st.session_state.project_revision = assessment.revision
    st.session_state.v2_intake_revision = assessment.revision
    st.rerun()


def render_questionnaire(questions: list[dict[str, Any]], provider: str) -> None:
    step = st.session_state.step
    answers = st.session_state.answers
    visible_questions = questions_for_step(questions, step, answers)

    st.markdown('<div class="app-kicker">Персональна оцінка</div>', unsafe_allow_html=True)
    st.title("Які системи потрібні саме вашому дому")
    st.markdown(
        '<div class="app-lead">Відповідайте про житло та звички. '
        "Бюджет з'явиться після оцінювання й не приховає корисні системи.</div>",
        unsafe_allow_html=True,
    )
    st.markdown(f'<div class="step-label">Крок {step + 1} із 4</div>', unsafe_allow_html=True)
    st.progress((step + 1) / 4)
    st.subheader(STEP_TITLES[step])

    with st.form(f"questionnaire_step_{step}"):
        for question in visible_questions:
            render_question(question, answers)
        st.session_state.answers = answers

        left, right = st.columns([1, 1])
        back_clicked = left.form_submit_button(
            "Назад", icon=":material/arrow_back:", disabled=step == 0, use_container_width=True
        )
        next_label = "Отримати рекомендації" if step == 3 else "Продовжити"
        next_icon = ":material/auto_awesome:" if step == 3 else ":material/arrow_forward:"
        next_clicked = right.form_submit_button(
            next_label, icon=next_icon, type="primary", use_container_width=True
        )

    if back_clicked:
        st.session_state.step = max(0, step - 1)
        st.rerun()

    if next_clicked:
        errors = validate_step(visible_questions, answers)
        if errors:
            st.error("Заповніть усі обов'язкові питання цього кроку.")
            return
        if step < 3:
            st.session_state.step += 1
            st.rerun()

        if provider == "openai" and not os.getenv("OPENAI_API_KEY"):
            st.error("Для хмарного аналізу не знайдено OPENAI_API_KEY у файлі .env.")
            return
        with st.spinner("Аналізуємо профіль і перевіряємо технічні обмеження..."):
            result = recommend(answers, provider=provider)
        st.session_state.result = result
        st.session_state.saved_record_id = save_history(answers, result)
        st.rerun()


def render_recommendation_card(
    item: dict[str, Any], system: dict[str, Any]
) -> None:
    with st.container(border=True):
        title_col, status_col = st.columns([3, 1])
        title_col.subheader(system["name_uk"])
        status_col.markdown(
            f'<div class="status-label status-{item["status"]}">'
            f'{STATUS_LABELS[item["status"]]}</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="system-description">{system["description"]}</div>',
            unsafe_allow_html=True,
        )
        if item["suitability_percent"] is not None:
            score = int(item["suitability_percent"])
            st.progress(score / 100)
            st.caption(
                f"Доцільність: {score}% · Орієнтовний клас вартості: "
                f"{COST_LABELS.get(system.get('cost_class'), 'Не визначено')}"
            )
        st.write(item["reason"])
        if item.get("explanation_needs_review"):
            st.caption("Це пояснення LLM потребує перевірки за відповідями анкети.")
        if (
            item.get("confidence_percent") is not None
            and int(item["confidence_percent"]) < 100
        ):
            st.caption(
                "Для цієї оцінки доступно "
                f'{int(item["confidence_percent"])}% передбачених вхідних даних.'
            )


def render_results(
    result: dict[str, Any], systems: list[dict[str, Any]], from_history: bool = False
) -> None:
    system_map = {item["system_id"]: item for item in systems}
    recommendations = result["recommendations"]
    counts = {
        status: sum(item["status"] == status for item in recommendations)
        for status in STATUS_LABELS
    }

    st.markdown('<div class="app-kicker">Результат оцінювання</div>', unsafe_allow_html=True)
    st.title("Персональна конфігурація розумного дому")
    st.markdown(
        '<div class="app-lead">Показані всі системи каталогу. Відсоток означає '
        "доцільність для вашого профілю, а не ймовірність чи точну економію.</div>",
        unsafe_allow_html=True,
    )
    if result.get("explanation_status") == "initial_llm_unverified":
        st.warning(
            "Фінальне пояснення LLM не пройшло перевірку. Показано її початковий "
            "текст; звірте його з відповідями та статусом перед використанням."
        )
    elif result.get("explanation_status") == "partial_llm_validated":
        st.warning(
            "Частину пояснень LLM перевірено. Неперевірені тексти позначено "
            "безпосередньо біля відповідних систем."
        )
    elif result.get("explanation_status") == "rule_only_fallback":
        st.warning(
            "Хмарна модель зараз недоступна. Цей результат містить лише правилові "
            "оцінки та пояснення, без аналізу LLM."
        )
    elif result.get("explanation_status") == "rule_only":
        st.caption("Статуси й пояснення сформовано за експертними правилами.")
    metric_columns = st.columns(4)
    for column, status in zip(metric_columns, STATUS_LABELS):
        column.metric(STATUS_LABELS[status], counts[status])

    tabs = st.tabs(
        [f"{STATUS_LABELS[status]} · {counts[status]}" for status in STATUS_LABELS]
    )
    for tab, status in zip(tabs, STATUS_LABELS):
        with tab:
            items = [item for item in recommendations if item["status"] == status]
            if not items:
                st.info("У цьому профілі немає систем із таким статусом.")
            for item in sorted(
                items,
                key=lambda value: value.get("suitability_percent") or -1,
                reverse=True,
            ):
                render_recommendation_card(item, system_map[item["system_id"]])

    if not from_history:
        st.divider()
        v2_flow = st.session_state.answers.get("_ux_flow") == "v2"
        st.subheader("Функції та рекомендований обсяг" if v2_flow else "Перейти до попереднього проєкту")
        st.caption(
            "Система автоматично прив'яже функції до підтверджених приміщень і покаже чергу реалізації."
            if v2_flow
            else "Додайте структуру об'єкта, щоб надалі прив'язати рекомендовані функції до конкретних приміщень."
        )
        notice = database_configuration_notice(os.getenv("DATABASE_URL"))
        if notice:
            st.warning(notice)
        project_col, restart_col = st.columns([2, 1])
        if project_col.button(
            "Переглянути функції" if v2_flow else "Вказати приміщення",
            icon=":material/account_tree:" if v2_flow else ":material/domain_add:",
            type="primary",
            use_container_width=True,
            disabled=notice is not None,
        ):
            open_object_step(result)
        if restart_col.button(
            "Почати нову оцінку",
            icon=":material/restart_alt:",
            use_container_width=True,
        ):
            reset_assessment()
            st.rerun()


def render_object_step() -> None:
    assessment_id = st.session_state.project_assessment_id
    if not assessment_id:
        st.session_state.object_step_active = False
        st.rerun()

    try:
        repository = project_repository()
        assessment = repository.get(assessment_id)
    except (SQLAlchemyError, RuntimeError):
        st.error("Не вдалося прочитати проєкт із PostgreSQL. Перевірте стан бази даних.")
        return
    if assessment is None or assessment.property_type is None:
        st.error("Проєкт не знайдено або тип житла не визначено.")
        return

    property_type = assessment.property_type
    st.markdown('<div class="app-kicker">Попередній проєкт · Крок 1</div>', unsafe_allow_html=True)
    st.title("Структура об'єкта")
    st.markdown(
        '<div class="app-lead">Вкажіть приміщення та спеціальні локації. '
        "Зміни збережуться тільки після натискання кнопки внизу.</div>",
        unsafe_allow_html=True,
    )
    if st.button("До рекомендацій", icon=":material/arrow_back:"):
        st.session_state.object_step_active = False
        st.rerun()

    definitions = [
        item
        for item in available_location_types(property_type)
        if item.room_type != RoomType.WHOLE_PROPERTY
    ]
    available_values = [item.room_type.value for item in definitions]
    selected_values = st.multiselect(
        "Типи приміщень і локацій",
        options=available_values,
        format_func=lambda value: location_label(value),
        key="object_selected_types",
    )

    room_values = [
        value
        for value in selected_values
        if LOCATION_TYPE_MAP[RoomType(value)].kind == LocationKind.ROOM
    ]
    special_values = [
        value
        for value in selected_values
        if LOCATION_TYPE_MAP[RoomType(value)].kind == LocationKind.SPECIAL
    ]
    counts: dict[RoomType, int] = {RoomType.WHOLE_PROPERTY: 1}
    for title, values in (
        ("Приміщення", room_values),
        ("Спеціальні локації", special_values),
    ):
        if not values:
            continue
        st.markdown(f"**{title}**")
        columns = st.columns(3)
        for index, value in enumerate(values):
            key = f"object_count_{value}"
            if key not in st.session_state:
                st.session_state[key] = 1
            count = columns[index % 3].number_input(
                location_label(value),
                min_value=0,
                max_value=50,
                step=1,
                key=key,
            )
            counts[RoomType(value)] = int(count)

    try:
        draft_rooms = reconcile_rooms(
            property_type,
            counts,
            st.session_state.object_rooms,
        )
    except ValueError:
        st.error("Перелік містить тип локації, несумісний із вибраним типом житла.")
        return
    st.session_state.object_rooms = draft_rooms

    area_default = float(assessment.area_m2 or (60 if property_type == PropertyType.APARTMENT else 150))
    floors_default = int(
        assessment.floor_count or (1 if property_type == PropertyType.APARTMENT else 2)
    )
    max_floors = 3 if property_type == PropertyType.APARTMENT else 10

    with st.form("object_step_form"):
        project_name = st.text_input(
            "Назва проєкту",
            value=assessment.project_name or "",
            placeholder="Наприклад, Квартира на вул. Шевченка",
        )
        general_left, general_right = st.columns(2)
        total_area_m2 = general_left.number_input(
            "Загальна площа, м²",
            min_value=10.0,
            max_value=10000.0,
            value=area_default,
            step=1.0,
        )
        floor_count = general_right.number_input(
            "Кількість рівнів житла",
            min_value=1,
            max_value=max(max_floors, floors_default),
            value=floors_default,
            step=1,
        )

        st.markdown("**Редагування переліку**")
        edited_rooms: list[Room] = []
        if not draft_rooms:
            st.info("Додайте хоча б один тип приміщення або локації вище.")
        for index, room in enumerate(draft_rooms):
            definition = LOCATION_TYPE_MAP[room.room_type]
            name_col, floor_col = st.columns([3, 1])
            name = name_col.text_input(
                location_label(room.room_type),
                value=room.name,
                key=f"object_room_name_{room.room_id}",
            )
            if definition.kind == LocationKind.ROOM:
                floor = floor_col.selectbox(
                    "Поверх",
                    options=list(range(1, int(floor_count) + 1)),
                    index=min(max((room.floor or 1) - 1, 0), int(floor_count) - 1),
                    key=f"object_room_floor_{room.room_id}",
                )
            else:
                floor_options: list[int | None] = [None, *range(1, int(floor_count) + 1)]
                current_floor = room.floor if room.floor in floor_options else None
                floor = floor_col.selectbox(
                    "Рівень",
                    options=floor_options,
                    index=floor_options.index(current_floor),
                    format_func=lambda value: "Не прив'язано" if value is None else str(value),
                    key=f"object_room_floor_{room.room_id}",
                )
            edited_rooms.append(
                room.model_copy(
                    update={"name": name, "floor": floor, "sort_order": index}
                )
            )

        submitted = st.form_submit_button(
            "Зберегти дані об'єкта",
            icon=":material/save:",
            type="primary",
            use_container_width=True,
        )

    if not submitted:
        return
    try:
        draft = ObjectStepDraft(
            property_type=property_type,
            project_name=project_name,
            total_area_m2=total_area_m2,
            floor_count=int(floor_count),
            rooms=edited_rooms,
        )
        saved = persist_object_step_if_submitted(
            repository,
            assessment.assessment_id,
            assessment.revision,
            draft,
            submitted=True,
        )
        assert saved is not None
        st.session_state.project_revision = saved.revision
        st.session_state.object_rooms = saved.rooms
        st.session_state.object_step_active = False
        st.session_state.function_step_active = True
        st.session_state.function_step_message = (
            "Дані об'єкта збережено. Сформовано детерміновані рекомендації функцій."
        )
        st.rerun()
    except ValidationError:
        st.error("Перевірте назву проєкту, площу, поверховість і перелік приміщень.")
    except DownstreamRecomputationRequiredError:
        st.error(
            "Для цього проєкту вже сформовано функціональні рекомендації. "
            "Зміна структури потребує нового оцінювання або явного перерахунку."
        )
    except RevisionConflictError:
        st.error("Проєкт було змінено в іншій сесії. Оновіть сторінку перед повторним збереженням.")
    except ValueError:
        st.error(
            "Площа або поверховість відрізняється від даних, використаних у рекомендації. "
            "Для зміни цих параметрів почніть нове оцінювання."
        )
    except SQLAlchemyError:
        st.error("Не вдалося зберегти проєкт у PostgreSQL. Перевірте стан бази даних.")


def render_automatic_function_step(
    systems: list[dict[str, Any]],
    repository: AssessmentRepository,
    assessment: Assessment,
    catalog,
    rules,
) -> None:
    function_plan_is_current = (
        assessment.config_versions.get("function_targeting")
        == FUNCTION_TARGETING_POLICY_VERSION
    )
    if assessment.function_recommendations and not function_plan_is_current:
        st.title("Збережений функціональний план")
        st.warning(
            "Цей проєкт використовує попередню версію правил. Його рекомендації "
            "збережено без змін; для нової методики створіть нове оцінювання."
        )
        if assessment.budget is not None and st.button(
            "Переглянути історичний підсумок", icon=":material/summarize:"
        ):
            st.session_state.function_step_active = False
            st.session_state.summary_step_active = True
            st.rerun()
        return
    try:
        if assessment.function_recommendations and function_plan_is_current:
            recommendations = assessment.function_recommendations
            recommendations_already_persisted = True
        else:
            plan = generate_function_plan(assessment, catalog, rules)
            recommendations = plan.recommendations
            recommendations_already_persisted = False
    except ValueError as error:
        st.error(f"Не вдалося сформувати функціональні рекомендації: {error}")
        return

    st.markdown(
        '<div class="app-kicker">Персональний проєкт · Крок 4</div>',
        unsafe_allow_html=True,
    )
    st.title("Рекомендовані функції за приміщеннями")
    st.markdown(
        '<div class="app-lead">Система сама визначила функції, приміщення й '
        "чергу. На цьому етапі рекомендація не редагується; сумнівні факти "
        "потрібно виправляти у вихідних даних об'єкта.</div>",
        unsafe_allow_html=True,
    )
    scope_level = str(assessment.answers.get("implementation_scope", "balanced"))
    scope_labels = {
        "conservative": "Основні зони та підготовка до розширення",
        "balanced": "Збалансований обсяг",
        "expanded": "Розширений обсяг",
    }
    st.metric("Масштаб реалізації", scope_labels.get(scope_level, scope_level))

    if assessment.function_choices and function_plan_is_current:
        choices = assessment.function_choices
        scope_plan = None
    else:
        scope_plan = build_automatic_scope_plan(
            assessment,
            recommendations,
            catalog,
            scope_level,
        )
        choices = scope_plan.choices
    selected_choices = [item for item in choices if item.selected]
    selected_total = len(selected_choices)
    unique_function_total = len({item.function_id for item in selected_choices})
    narrowed_total = scope_plan.narrowed_count if scope_plan else 0
    added_total = scope_plan.optional_added_count if scope_plan else 0
    metric_columns = st.columns(3)
    metric_columns[0].metric("Типів функцій", unique_function_total)
    metric_columns[0].caption(
        f"{selected_total} застосувань у приміщеннях: один тип функції може "
        "повторюватися в кількох кімнатах."
    )
    metric_columns[1].metric("Звужено до основних зон", narrowed_total)
    metric_columns[2].metric("Додано для розширеного обсягу", added_total)
    if selected_total == 0:
        message, optional_text = empty_scope_guidance(assessment, systems)
        st.info(message)
        if optional_text:
            st.caption(optional_text)

    catalog_map = {item.function_id: item for item in catalog.functions}
    room_map = {item.room_id: item for item in assessment.rooms}
    choice_map = {(item.function_id, item.room_id): item for item in choices}
    by_system: dict[str, list[Any]] = {}
    for recommendation in recommendations:
        by_system.setdefault(recommendation.system_id, []).append(recommendation)

    for system in systems:
        items = by_system.get(system["system_id"], [])
        selected_items = [
            item
            for item in items
            if choice_map[(item.function_id, item.room_id)].selected
        ]
        selected_count = len(selected_items)
        selected_function_count = len({item.function_id for item in selected_items})
        with st.expander(
            f'{system["name_uk"]} · {selected_function_count} функцій · '
            f'{selected_count} зон',
            expanded=False,
        ):
            if not items:
                st.caption("Для системи немає функцій у поточному каталозі.")
                continue
            selected_by_function: dict[str, list[Any]] = {}
            for recommendation in selected_items:
                selected_by_function.setdefault(recommendation.function_id, []).append(
                    recommendation
                )
            if not selected_by_function:
                st.caption("У рекомендований обсяг функції цієї системи не входять.")
            for function_id, function_items in sorted(selected_by_function.items()):
                function = catalog_map[function_id]
                room_names = [
                    room_map[item.room_id].name
                    for item in sorted(
                        function_items,
                        key=lambda item: room_map[item.room_id].sort_order,
                    )
                ]
                phases = {
                    choice_map[(item.function_id, item.room_id)].selected_phase
                    for item in function_items
                }
                phase_text = ", ".join(
                    PHASE_LABELS[phase]
                    for phase in ImplementationPhase
                    if phase in phases
                )
                title_col, phase_col = st.columns([3, 2])
                title_col.markdown(f"**{function.name_uk}** · {len(room_names)} зон")
                phase_col.caption(phase_text)
                st.caption("Де: " + ", ".join(room_names))
                st.caption(function_items[0].reason)

            excluded_function_ids = {
                item.function_id
                for item in items
                if not choice_map[(item.function_id, item.room_id)].selected
            }
            if excluded_function_ids:
                with st.expander(
                    f"Не включено автоматично · {len(excluded_function_ids)} функцій"
                ):
                    for function_id in sorted(excluded_function_ids):
                        st.markdown(f"- {catalog_map[function_id].name_uk}")

    go_to_summary = st.button(
        "Перейти до підсумку",
        icon=":material/summarize:",
        type="primary",
        use_container_width=True,
    )
    if go_to_summary:
        try:
            saved = assessment
            if not assessment.function_choices or not function_plan_is_current:
                whole_property_id = next(
                    room.room_id
                    for room in assessment.rooms
                    if room.room_type == RoomType.WHOLE_PROPERTY
                )
                requested = {
                    (item.function_id, item.room_id): (
                        choice_map[(item.function_id, item.room_id)].selected,
                        choice_map[(item.function_id, item.room_id)].selected_phase,
                    )
                    for item in recommendations
                }
                result = persist_function_step_if_submitted(
                    repository,
                    assessment.assessment_id,
                    assessment.revision,
                    recommendations,
                    catalog,
                    requested,
                    whole_property_id=whole_property_id,
                    submitted=True,
                    recommendations_already_persisted=recommendations_already_persisted,
                    config_versions={
                        "functions_catalog": catalog.schema_version,
                        "function_rules": rules.schema_version,
                        "phase_rules": rules.phase_rules_version,
                        "implementation_scope": scope_level,
                        "automatic_scope": SCOPE_POLICY_VERSION,
                        "function_targeting": FUNCTION_TARGETING_POLICY_VERSION,
                    },
                    decision_source_for_changes=DecisionSource.SYSTEM_DEFAULT,
                )
                assert result is not None
                saved, warnings = result
                st.session_state.function_step_warnings = warnings
            if saved.budget is None:
                budget_level = {
                    "conservative": BudgetLevel.BASIC,
                    "balanced": BudgetLevel.BALANCED,
                    "expanded": BudgetLevel.FLEXIBLE,
                }.get(scope_level, BudgetLevel.BALANCED)
                saved = repository.save_budget_step(
                    saved.assessment_id,
                    saved.revision,
                    budget=BudgetPreference(level=budget_level),
                )
            st.session_state.project_revision = saved.revision
            st.session_state.function_step_active = False
            st.session_state.budget_step_active = False
            st.session_state.technical_step_active = False
            st.session_state.summary_step_active = True
            st.rerun()
        except StopIteration:
            st.error("У структурі об'єкта відсутня службова локація «Весь об'єкт».")
        except RevisionConflictError:
            st.error("Проєкт змінено в іншій сесії. Оновіть сторінку.")
        except (SQLAlchemyError, ValueError, ValidationError) as error:
            show_operation_error("Не вдалося зберегти автоматичний обсяг", error)


def render_function_step(systems: list[dict[str, Any]]) -> None:
    assessment_id = st.session_state.project_assessment_id
    if not assessment_id:
        st.session_state.function_step_active = False
        st.rerun()

    try:
        repository = project_repository()
        assessment = repository.get(assessment_id)
        catalog, rules = load_function_configuration()
    except (SQLAlchemyError, RuntimeError, ValueError):
        st.error(
            "Не вдалося відкрити функціональний крок. Перевірте PostgreSQL і "
            "версії каталогів."
        )
        return
    if assessment is None or not assessment.rooms:
        st.error("Спочатку збережіть структуру об'єкта.")
        return

    catalog = catalog_for_saved_functions(
        catalog,
        {item.function_id for item in assessment.function_recommendations}
        | {item.function_id for item in assessment.function_choices},
    )

    if assessment.config_versions.get("ux_flow") == "v2":
        render_automatic_function_step(
            systems,
            repository,
            assessment,
            catalog,
            rules,
        )
        return

    try:
        if assessment.function_recommendations:
            recommendations = assessment.function_recommendations
            initial_choices = (
                assessment.function_choices
                or default_function_choices(recommendations)
            )
            recommendations_already_persisted = True
        else:
            plan = generate_function_plan(assessment, catalog, rules)
            recommendations = plan.recommendations
            initial_choices = plan.choices
            recommendations_already_persisted = False
    except ValueError as error:
        st.error(f"Не вдалося сформувати функціональні рекомендації: {error}")
        return

    st.markdown(
        '<div class="app-kicker">Попередній проєкт · Крок 2</div>',
        unsafe_allow_html=True,
    )
    st.title("Функції за приміщеннями")
    st.markdown(
        '<div class="app-lead">Рекомендації сформовані детермінованими правилами. '
        "Зміна вибору або фази не змінює початковий результат системи.</div>",
        unsafe_allow_html=True,
    )
    if st.session_state.function_step_message:
        st.success(st.session_state.function_step_message)
        st.session_state.function_step_message = None
    for warning in st.session_state.function_step_warnings:
        st.warning(warning)
    st.session_state.function_step_warnings = []

    if st.button("До структури об'єкта", icon=":material/arrow_back:"):
        st.session_state.function_step_active = False
        st.session_state.object_step_active = True
        st.rerun()

    catalog_map = {item.function_id: item for item in catalog.functions}
    room_map = {item.room_id: item for item in assessment.rooms}
    choice_map = {
        (item.function_id, item.room_id): item for item in initial_choices
    }
    system_map = {item["system_id"]: item for item in systems}
    by_system: dict[str, list[Any]] = {}
    for recommendation in recommendations:
        by_system.setdefault(recommendation.system_id, []).append(recommendation)

    requested: dict[
        tuple[str, str], tuple[bool, ImplementationPhase | None]
    ] = {}
    with st.form("function_step_form"):
        for system in systems:
            system_id = system["system_id"]
            items = by_system.get(system_id, [])
            selected_count = sum(
                choice_map[(item.function_id, item.room_id)].selected
                for item in items
            )
            with st.expander(
                f'{system_map[system_id]["name_uk"]} · {selected_count}/{len(items)}',
                expanded=False,
            ):
                for recommendation in items:
                    function = catalog_map[recommendation.function_id]
                    room = room_map[recommendation.room_id]
                    key_suffix = f"{recommendation.function_id}_{recommendation.room_id}"
                    current_choice = choice_map[
                        (recommendation.function_id, recommendation.room_id)
                    ]
                    selectable = (
                        recommendation.status != RecommendationStatus.NOT_APPLICABLE
                    )
                    label = f"{function.name_uk} · {room.name}"
                    select_col, status_col, phase_col = st.columns([5, 2, 2])
                    selected = select_col.checkbox(
                        label,
                        value=current_choice.selected,
                        disabled=not selectable,
                        key=f"function_selected_{key_suffix}",
                    )
                    status_col.caption(
                        STATUS_LABELS[recommendation.status.value]
                        + (" · перевірити" if recommendation.needs_review else "")
                    )
                    phase_default = (
                        current_choice.selected_phase
                        or recommendation.recommended_phase
                        or ImplementationPhase.LATER
                    )
                    selected_phase = phase_col.selectbox(
                        "Фаза",
                        options=list(PHASE_LABELS),
                        index=list(PHASE_LABELS).index(phase_default),
                        format_func=lambda value: PHASE_LABELS[value],
                        disabled=not selectable,
                        key=f"function_phase_{key_suffix}",
                        label_visibility="collapsed",
                    )
                    st.caption(recommendation.reason)
                    requested[(recommendation.function_id, recommendation.room_id)] = (
                        bool(selected),
                        selected_phase if selected else None,
                    )
                if not items:
                    st.caption("Для системи немає функцій у поточному каталозі.")

        submitted = st.form_submit_button(
            "Зберегти вибір функцій",
            icon=":material/save:",
            type="primary",
            use_container_width=True,
        )

    if not submitted:
        return
    try:
        whole_property_id = next(
            room.room_id
            for room in assessment.rooms
            if room.room_type == RoomType.WHOLE_PROPERTY
        )
        result = persist_function_step_if_submitted(
            repository,
            assessment.assessment_id,
            assessment.revision,
            recommendations,
            catalog,
            requested,
            whole_property_id=whole_property_id,
            submitted=True,
            recommendations_already_persisted=recommendations_already_persisted,
            config_versions={
                "functions_catalog": catalog.schema_version,
                "function_rules": rules.schema_version,
                "phase_rules": rules.phase_rules_version,
                "function_targeting": FUNCTION_TARGETING_POLICY_VERSION,
            },
        )
        assert result is not None
        saved, warnings = result
        st.session_state.project_revision = saved.revision
        st.session_state.function_step_warnings = warnings
        st.session_state.function_step_active = False
        if saved.budget is None:
            st.session_state.budget_step_active = True
            st.session_state.budget_step_message = "Вибір функцій збережено."
        else:
            st.session_state.summary_step_active = True
            st.session_state.budget_step_message = (
                "Вибір функцій оновлено. Сценарій перераховано без зміни рекомендацій."
            )
        st.rerun()
    except StopIteration:
        st.error("У структурі об'єкта відсутня службова локація «Весь об'єкт».")
    except RevisionConflictError:
        st.error("Проєкт змінено в іншій сесії. Оновіть сторінку перед збереженням.")
    except (ValueError, ValidationError) as error:
        show_operation_error("Не вдалося зберегти вибір функцій", error)
    except SQLAlchemyError:
        st.error("Не вдалося зберегти функції у PostgreSQL.")


def render_budget_step() -> None:
    assessment_id = st.session_state.project_assessment_id
    if not assessment_id:
        st.session_state.budget_step_active = False
        st.rerun()

    try:
        repository = project_repository()
        assessment = repository.get(assessment_id)
    except (SQLAlchemyError, RuntimeError):
        st.error("Не вдалося відкрити бюджетний крок у PostgreSQL.")
        return
    if assessment is None or not assessment.function_choices:
        st.error("Спочатку збережіть вибір функцій.")
        return

    st.markdown(
        '<div class="app-kicker">Попередній проєкт · Крок 3</div>',
        unsafe_allow_html=True,
    )
    st.title("Бюджетна позиція")
    st.markdown(
        '<div class="app-lead">Оберіть не суму, а бажаний масштаб першого етапу. '
        "Оцінки систем і черги залишаються незмінними, але підсумок чітко покаже, "
        "що реалізовувати зараз, що перенести, а що залишити як розширення.</div>",
        unsafe_allow_html=True,
    )
    if st.session_state.budget_step_message:
        st.success(st.session_state.budget_step_message)
        st.session_state.budget_step_message = None

    if st.button("До вибору функцій", icon=":material/arrow_back:"):
        st.session_state.budget_step_active = False
        st.session_state.function_step_active = True
        st.rerun()

    default_level = (
        assessment.budget.level if assessment.budget else BudgetLevel.BALANCED
    )
    with st.form("budget_step_form"):
        budget_level = st.segmented_control(
            "Бюджетна позиція",
            options=list(BUDGET_LABELS),
            default=default_level,
            format_func=lambda value: BUDGET_LABELS[value],
        ) or default_level
        st.caption(BUDGET_DESCRIPTIONS[budget_level])
        st.caption(
            "Конкретна сума тут не використовується: без обстеження та повної "
            "специфікації вона створювала б хибне відчуття точності."
        )
        submitted = st.form_submit_button(
            "Зберегти бюджет і відкрити підсумок",
            icon=":material/save:",
            type="primary",
            use_container_width=True,
        )

    if not submitted:
        return
    try:
        budget = BudgetPreference(level=budget_level)
        saved = persist_budget_step_if_submitted(
            repository,
            assessment.assessment_id,
            assessment.revision,
            budget,
            submitted=True,
        )
        assert saved is not None
        st.session_state.project_revision = saved.revision
        st.session_state.budget_step_active = False
        st.session_state.summary_step_active = True
        st.session_state.budget_step_message = "Бюджетну позицію збережено."
        st.rerun()
    except ValidationError:
        st.error("Не вдалося зберегти бюджетну позицію.")
    except RevisionConflictError:
        st.error("Проєкт змінено в іншій сесії. Оновіть сторінку перед збереженням.")
    except SQLAlchemyError:
        st.error("Не вдалося зберегти бюджетну позицію у PostgreSQL.")


TECHNICAL_FIELD_LABELS = {
    "floor_zone_count": "Окремі зони електричної теплої підлоги",
    "floor_temperature_sensor_count": "Зони з датчиком температури підлоги",
    "relay_group_count": "Групи світла лише увімк./вимк.",
    "triac_group_count": "Групи димування світла 230 В",
    "pwm_group_count": "Низьковольтні LED-стрічки з димуванням",
    "dali_group_count": "Окремі групи DALI",
    "dali_address_count": "Світильники або драйвери DALI",
    "leak_sensor_count": "Точки контролю протікання",
    "valve_count": "Клапани перекриття води",
    "fan_count": "Витяжні вентилятори",
    "fan_group_count": "Незалежні групи керування вентиляторами",
}

TECHNICAL_SUGGESTED_VALUES = {
    "floor_zone_count": 1,
    "floor_temperature_sensor_count": 1,
    "relay_group_count": 1,
    "triac_group_count": 1,
    "pwm_group_count": 0,
    "dali_group_count": 0,
    "dali_address_count": 0,
    "leak_sensor_count": 1,
    "valve_count": 0,
    "fan_count": 1,
    "fan_group_count": 1,
}

CHANNEL_LABELS = {
    ChannelType.RO: "Релейні виходи (увімк./вимк.)",
    ChannelType.DI: "Дискретні входи (датчики та кнопки)",
    ChannelType.TRIAC: "Канали димування 230 В",
    ChannelType.PWM: "Канали LED 12/24 В",
    ChannelType.DALI_ADDRESS: "Адреси DALI",
}

PHYSICAL_DEVICE_LABELS = {
    PhysicalDeviceType.FLOOR_TEMPERATURE_SENSOR: "Датчики температури підлоги",
    PhysicalDeviceType.LEAK_SENSOR: "Датчики протікання",
    PhysicalDeviceType.VALVE: "Клапани перекриття води",
    PhysicalDeviceType.EXHAUST_FAN: "Витяжні вентилятори",
}


def _technical_number_input(
    field_name: str,
    value: int | None,
    *,
    key: str,
) -> int:
    return st.number_input(
        TECHNICAL_FIELD_LABELS[field_name],
        min_value=0,
        max_value=10000,
        value=int(
            value if value is not None else TECHNICAL_SUGGESTED_VALUES[field_name]
        ),
        step=1,
        format="%d",
        key=key,
    )


def _render_technical_table(calculation, catalog_map, room_map) -> None:
    active_requirements = [
        item for item in calculation.channel_requirements if item.required_total > 0
    ]
    if active_requirements:
        for index in range(0, len(active_requirements), 3):
            columns = st.columns(3)
            for column, item in zip(columns, active_requirements[index : index + 3]):
                column.metric(
                    CHANNEL_LABELS[item.channel_type],
                    item.required_total,
                    f"резерв +{item.reserve}" if item.reserve else "без резерву",
                )
    else:
        st.caption("Після заповнення кількостей тут з'явиться потреба в каналах.")

    physical_items = [item for item in calculation.physical_devices if item.count > 0]
    if physical_items:
        st.markdown("**Фізичні пристрої, враховані у розрахунку**")
        st.markdown(
            "\n".join(
                f"- {PHYSICAL_DEVICE_LABELS[item.device_type]}: {item.count}"
                for item in physical_items
            )
        )

    with st.expander("Як утворився розрахунок"):
        if not calculation.contributions:
            st.caption("Підтверджених значень ще немає.")
        for item in calculation.contributions:
            st.markdown(
                f"- **{catalog_map[item.function_id].name_uk}**, "
                f"{room_map[item.room_id].name}: {TECHNICAL_FIELD_LABELS[item.input_field]} "
                f"— {item.input_value}; додає {item.base_units} "
                f"до «{CHANNEL_LABELS[item.channel_type]}»."
            )


def render_technical_step() -> None:
    assessment_id = st.session_state.project_assessment_id
    if not assessment_id:
        st.error("Технічний крок не прив'язано до проєкту.")
        return
    try:
        assessment = project_repository().get(assessment_id)
    except (SQLAlchemyError, RuntimeError, ValueError):
        st.error("Не вдалося прочитати проєкт із PostgreSQL.")
        return
    if assessment is None or assessment.budget is None:
        st.error("Перед технічним кроком збережіть функції та бюджетну позицію.")
        return

    catalog, _ = load_function_configuration()
    catalog = catalog_for_saved_functions(
        catalog,
        {item.function_id for item in assessment.function_recommendations}
        | {item.function_id for item in assessment.function_choices},
    )
    catalog_map = {item.function_id: item for item in catalog.functions}
    room_map = {room.room_id: room for room in assessment.rooms}
    selected_keys = sorted(
        (choice.function_id, choice.room_id)
        for choice in assessment.function_choices
        if choice.selected and choice.function_id in TECHNICAL_FUNCTION_IDS
    )
    st.markdown('<div class="app-kicker">Необов’язковий підетап</div>', unsafe_allow_html=True)
    st.title("Демонстраційний розрахунок каналів")
    st.markdown(
        '<div class="app-lead">Цей підетап приблизно переводить кількість зон, '
        "датчиків і груп у входи та виходи контролера. Він охоплює лише п'ять "
        "функцій дипломного прототипу й не є повною специфікацією.</div>",
        unsafe_allow_html=True,
    )
    with st.expander("Що таке канали і навіщо вони тут?"):
        st.markdown(
            "1. Рекомендаційна система визначає функцію і приміщення.\n"
            "2. Кількість зон, груп або датчиків переводиться у входи та виходи "
            "контролера. Наприклад, п'ять незалежних груп світла потребують п'яти "
            "релейних виходів.\n"
            "3. До керованих каналів додається 20% резерву для майбутнього "
            "розширення. Це навчальний попередній розрахунок, а не специфікація."
        )
    if not selected_keys:
        st.info("У цьому проєкті немає вибраних функцій, для яких створено демонстраційний розрахунок.")
        if st.button("Повернутися до підсумку", icon=":material/arrow_back:"):
            st.session_state.technical_step_active = False
            st.session_state.summary_step_active = True
            st.rerun()
        return

    existing_inputs = {
        (item.function_id, item.room_id): item
        for item in (
            assessment.technical_calculation.inputs
            if assessment.technical_calculation is not None
            else []
        )
    }
    draft_inputs: list[TechnicalLocationInput] = []
    grouped: dict[str, list[str]] = {}
    for function_id, room_id in selected_keys:
        grouped.setdefault(room_id, []).append(function_id)

    leak_room_ids = [
        room_id
        for function_id, room_id in selected_keys
        if function_id == "leak.detection_and_shutoff"
    ]
    primary_valve_room_id = leak_room_ids[0] if leak_room_ids else None
    water_entry_present = assessment.answers.get("water_entry_present") is True

    st.info(
        "Початкові кількості вже заповнені автоматично: одна зона або група на "
        "кожне рекомендоване застосування. Датчики протікання задаються для "
        "кожної водної зони, а клапани — окремо за кількістю незалежних вводів "
        "води. Змініть числа лише за наявності точніших даних."
    )

    with st.form("technical_input_form"):
        for room_id, function_ids in grouped.items():
            st.subheader(room_map[room_id].name)
            for function_id in function_ids:
                existing = existing_inputs.get((function_id, room_id))
                prefix = f"technical_{function_id}_{room_id}"
                st.markdown(f"**{catalog_map[function_id].name_uk}**")
                values: dict[str, Any] = {}
                if function_id == "climate.electric_underfloor_heating":
                    fields = ("floor_zone_count", "floor_temperature_sensor_count")
                elif function_id == "lighting.switched_group_control":
                    fields = ("relay_group_count",)
                elif function_id == "lighting.dimmable_group_control":
                    fields = (
                        "triac_group_count",
                        "pwm_group_count",
                        "dali_group_count",
                        "dali_address_count",
                    )
                elif function_id == "leak.detection_and_shutoff":
                    fields = ("leak_sensor_count",)
                else:
                    fields = ("fan_count", "fan_group_count")
                field_columns = st.columns(2 if len(fields) > 1 else 1)
                for field_index, field_name in enumerate(fields):
                    existing_value = getattr(existing, field_name) if existing else None
                    with field_columns[field_index % len(field_columns)]:
                        values[field_name] = _technical_number_input(
                            field_name,
                            existing_value,
                            key=f"{prefix}_{field_name}",
                        )
                draft_inputs.append(
                    TechnicalLocationInput(
                        function_id=function_id,
                        room_id=room_id,
                        source=TechnicalInputSource.QUESTIONNAIRE,
                        confidence=ReliabilityLevel.HIGH,
                        confirmed_by_user=True,
                        **values,
                    )
                )
            st.divider()

        if primary_valve_room_id is not None:
            if water_entry_present:
                st.subheader("Ввід води")
                st.caption(
                    "Клапан рахується не для кожної кімнати, а за кількістю "
                    "незалежних вводів або гілок, які потрібно перекривати."
                )
                valve_input_index = next(
                    index
                    for index, item in enumerate(draft_inputs)
                    if item.function_id == "leak.detection_and_shutoff"
                    and item.room_id == primary_valve_room_id
                )
                existing_valve_input = existing_inputs.get(
                    ("leak.detection_and_shutoff", primary_valve_room_id)
                )
                valve_columns = st.columns(2)
                with valve_columns[0]:
                    valve_count = _technical_number_input(
                        "valve_count",
                        (
                            existing_valve_input.valve_count
                            if existing_valve_input
                            and existing_valve_input.valve_count is not None
                            else 1
                        ),
                        key="technical_leak_water_entry_valve_count",
                    )
                profile_options = [None, *list(ValveProfile)]
                existing_profile = (
                    existing_valve_input.valve_profile
                    if existing_valve_input
                    and existing_valve_input.valve_profile is not None
                    else ValveProfile.SINGLE_OUTPUT
                )
                with valve_columns[1]:
                    valve_profile = st.selectbox(
                        "Підключення клапана перекриття води",
                        profile_options,
                        index=profile_options.index(existing_profile),
                        format_func=lambda value: {
                            None: "Не вказано",
                            ValveProfile.SINGLE_OUTPUT: "1 вихід",
                            ValveProfile.REVERSIBLE_TWO_OUTPUT: "2 реверсивні виходи",
                        }[value],
                        key="technical_leak_water_entry_valve_profile",
                    )
                draft_inputs[valve_input_index] = draft_inputs[
                    valve_input_index
                ].model_copy(
                    update={
                        "valve_count": valve_count,
                        "valve_profile": valve_profile,
                    }
                )
            else:
                st.info(
                    "Доступний ввід води не підтверджено: у розрахунку "
                    "залишаються датчики й сповіщення, а клапан перекриття не додається."
                )
            st.divider()
        save_col, skip_col = st.columns(2)
        submitted = save_col.form_submit_button(
            "Зберегти розрахунок",
            icon=":material/save:",
            type="primary",
            use_container_width=True,
        )
        skipped = skip_col.form_submit_button(
            "Пропустити й перейти до підсумку",
            icon=":material/skip_next:",
            use_container_width=True,
        )

    preview = build_technical_calculation(assessment, draft_inputs)
    st.subheader("Потреба в каналах")
    st.caption(
        "Нижче показано сумарні входи й виходи для п'яти демонстраційних функцій. "
        "Резерв уже включено у підсумкове число."
    )
    _render_technical_table(preview, catalog_map, room_map)
    if preview.needs_review:
        st.info(
            f"Для збереження розрахунку заповніть усі показані кількості. "
            f"Зараз бракує значень: {len(preview.blocking_missing_data)}."
        )
    else:
        st.success("Усі кількості заповнено. Розрахунок можна зберегти.")

    if skipped:
        st.session_state.technical_step_active = False
        st.session_state.summary_step_active = True
        st.rerun()

    if submitted:
        if preview.needs_review:
            st.warning("Заповніть усі поля або пропустіть необов'язковий підетап.")
            return
        try:
            saved = persist_technical_step_if_submitted(
                project_repository(), assessment, draft_inputs, submitted=True
            )
        except RevisionConflictError:
            st.error("Проєкт уже змінено в іншій сесії. Відкрийте його повторно.")
            return
        except (SQLAlchemyError, RuntimeError, ValueError, ValidationError) as error:
            show_operation_error("Не вдалося зберегти технічні входи", error)
            return
        assert saved is not None
        st.session_state.project_revision = saved.revision
        st.session_state.technical_step_active = False
        st.session_state.summary_step_active = True
        st.rerun()

    if st.button("Повернутися до підсумку", icon=":material/arrow_back:"):
        st.session_state.technical_step_active = False
        st.session_state.summary_step_active = True
        st.rerun()


def render_summary(systems: list[dict[str, Any]]) -> None:
    assessment_id = st.session_state.project_assessment_id
    if not assessment_id:
        st.session_state.summary_step_active = False
        st.rerun()
    try:
        repository = project_repository()
        assessment = repository.get(assessment_id)
        catalog, _ = load_function_configuration()
        if assessment is None:
            raise ValueError("assessment not found")
        catalog = catalog_for_saved_functions(
            catalog,
            {item.function_id for item in assessment.function_recommendations}
            | {item.function_id for item in assessment.function_choices},
        )
        summary = build_project_summary(assessment, catalog)
    except (SQLAlchemyError, RuntimeError, ValueError):
        st.error("Не вдалося сформувати підсумок проєкту.")
        return

    catalog_map = {item.function_id: item for item in catalog.functions}
    room_map = {item.room_id: item for item in assessment.rooms}
    system_map = {item["system_id"]: item for item in systems}
    recommendation_map = {
        (item.function_id, item.room_id): item
        for item in assessment.function_recommendations
    }
    room_order = {item.room_id: item.sort_order for item in assessment.rooms}

    st.markdown(
        '<div class="app-kicker">Попередній проєкт · Підсумок</div>',
        unsafe_allow_html=True,
    )
    st.title(assessment.project_name or "Проєкт розумного дому")
    st.caption(
        f"ID: {assessment.assessment_id} · ревізія {assessment.revision} · "
        f"оновлено {assessment.updated_at.astimezone():%d.%m.%Y %H:%M}"
    )
    if assessment.config_versions:
        version_text = " · ".join(
            f"{key}: {value}"
            for key, value in sorted(assessment.config_versions.items())
        )
        st.caption(f"Версії конфігурацій · {version_text}")
    if (
        assessment.config_versions.get("function_targeting")
        != FUNCTION_TARGETING_POLICY_VERSION
    ):
        st.warning(
            "Це історичний результат за попередньою версією правил. Його дані "
            "не змінюються; нова методика застосовується до нового оцінювання."
        )
    if st.session_state.budget_step_message:
        st.success(st.session_state.budget_step_message)
        st.session_state.budget_step_message = None

    object_columns = st.columns(4)
    object_columns[0].metric(
        "Тип житла",
        "Квартира" if assessment.property_type == PropertyType.APARTMENT else "Будинок",
    )
    object_columns[1].metric("Площа", f"{assessment.area_m2:g} м²")
    object_columns[2].metric("Рівні", assessment.floor_count)
    object_columns[3].metric(
        "Приміщення",
        sum(room.location_kind == LocationKind.ROOM for room in assessment.rooms),
    )

    st.subheader("Оцінка систем")
    if assessment.config_versions.get("explanation_status") == "initial_llm_unverified":
        st.warning(
            "Фінальні пояснення LLM не пройшли перевірку; нижче залишено "
            "її початкові формулювання."
        )
    elif assessment.config_versions.get("explanation_status") == "partial_llm_validated":
        st.warning(
            "Частину пояснень LLM перевірено; неперевірені тексти позначено "
            "біля відповідних систем."
        )
    elif assessment.config_versions.get("explanation_status") == "rule_only_fallback":
        st.warning("LLM була недоступна: показано лише правилові пояснення.")
    elif assessment.config_versions.get("explanation_status") == "rule_only":
        st.caption("Статуси й пояснення сформовано за експертними правилами.")
    status_columns = st.columns(4)
    for column, status in zip(status_columns, RecommendationStatus):
        column.metric(STATUS_LABELS[status.value], summary.system_counts[status])
    unverified_explanations = set(
        assessment.config_versions.get("unverified_explanation_system_ids", [])
    )
    for item in assessment.recommendations:
        system_name = system_map.get(item.system_id, {}).get("name_uk", item.system_id)
        suitability = (
            f"{item.suitability_percent}%"
            if item.suitability_percent is not None
            else "не застосовна"
        )
        with st.expander(
            f"{system_name} · {STATUS_LABELS[item.status.value]} · {suitability}"
        ):
            st.write(item.reason)
            if item.system_id in unverified_explanations:
                st.caption("Це пояснення LLM потребує перевірки за відповідями анкети.")

    st.subheader("Рекомендований функціональний обсяг")
    function_metrics = st.columns(4)
    unique_selected_total = len(
        {item.function_id for item in assessment.function_choices if item.selected}
    )
    function_metrics[0].metric("Типів функцій", unique_selected_total)
    function_metrics[0].caption("Різні можливості системи без повторів за кімнатами")
    function_metrics[1].metric("Застосувань у зонах", summary.selected_total)
    function_metrics[1].caption("Одна функція може бути потрібна у кількох приміщеннях")
    function_metrics[2].metric("Не включено системою", summary.not_selected_count)
    function_metrics[2].caption("Опційні, недоцільні, незастосовні або зайві для обраного масштабу")
    function_metrics[3].metric(
        "Потрібно уточнити",
        summary.needs_review_count if summary.selected_total else 0,
    )
    function_metrics[3].caption(
        "Точне місце або технічну можливість треба перевірити"
        if summary.selected_total else "У поточному обсязі немає функцій для уточнення"
    )
    if summary.selected_total == 0:
        message, optional_text = empty_scope_guidance(assessment, systems)
        st.info(message)
        if optional_text:
            st.caption(optional_text)

    review_items = [
        item for item in assessment.function_recommendations if item.needs_review
    ] if summary.selected_total else []
    if review_items:
        grouped_review: dict[tuple[str, str], list[str]] = {}
        for item in review_items:
            grouped_review.setdefault((item.function_id, item.reason), []).append(
                item.room_id
            )
        with st.expander(
            f"Що саме потрібно уточнити · {len(grouped_review)} уточнень"
        ):
            st.caption(
                "Це не повторне підтвердження всієї рекомендації. Для цих позицій "
                "бракує точної локації або технічної ознаки об'єкта."
            )
            for (function_id, reason), room_ids in grouped_review.items():
                room_names = [
                    room_map[room_id].name
                    for room_id in sorted(
                        dict.fromkeys(room_ids),
                        key=lambda room_id: room_order.get(room_id, 10**9),
                    )
                ]
                st.markdown(
                    f"- **{catalog_map[function_id].name_uk}** · "
                    f"{', '.join(room_names)}: {reason}"
                )

    dependency_rows = []
    for choice in assessment.function_choices:
        if not choice.selected:
            continue
        function = catalog_map[choice.function_id]
        for prerequisite_id in function.prerequisite_function_ids:
            dependency_rows.append(
                f"{function.name_uk} потребує «{catalog_map[prerequisite_id].name_uk}»"
            )
    if dependency_rows:
        with st.expander(
            f"Автоматично враховані технічні передумови · "
            f"{len(dict.fromkeys(dependency_rows))}"
        ):
            st.caption(
                "Передумова — допоміжна функція, без якої інша рекомендація не може "
                "працювати коректно. Система додає її автоматично не пізніше залежної функції."
            )
            for row in dict.fromkeys(dependency_rows):
                st.markdown(f"- {row}")

    with st.expander("Як система визначає чергу реалізації?"):
        st.markdown(
            "- **У першу чергу:** безпека, критичні інженерні функції, щоденний "
            "ефект і те, що дорого або складно додавати після ремонту.\n"
            "- **У другу чергу:** корисні функції без критичної терміновості.\n"
            "- **При бажанні:** недоцільні для першого складу або додаткові "
            "комфортні сценарії.\n\n"
            "Якщо функція залежить від іншої, передумова автоматично отримує таку "
            "саму або більш ранню чергу."
        )
    phase_function_counts = {
        phase: len(
            {
                item.function_id
                for item in summary.scenario.items
                if item.selected_phase == phase
            }
        )
        for phase in ImplementationPhase
    }
    phase_tabs = (
        st.tabs([
            f"{PHASE_LABELS[phase]} · {phase_function_counts[phase]} функцій"
            for phase in ImplementationPhase
        ])
        if summary.selected_total else []
    )
    for tab, phase in zip(phase_tabs, ImplementationPhase):
        with tab:
            phase_items = [
                item for item in summary.scenario.items if item.selected_phase == phase
            ]
            if not phase_items:
                st.caption("У цій фазі немає вибраних функцій.")
            phase_groups = group_scenario_functions(
                phase_items,
                room_sort_order=room_order,
            )
            if phase_items:
                st.caption(
                    f"{len(phase_groups)} типів функцій · "
                    f"{len(phase_items)} застосувань у приміщеннях"
                )
            for group in phase_groups:
                function = catalog_map[group.function_id]
                room_names = [room_map[room_id].name for room_id in group.room_ids]
                recommendations = [
                    recommendation_map[(group.function_id, room_id)]
                    for room_id in group.room_ids
                ]
                reasons = list(dict.fromkeys(item.reason for item in recommendations))
                markers = []
                if group.is_prerequisite:
                    markers.append("передумова")
                if group.needs_review:
                    markers.append("потребує перевірки")
                marker_text = f" · {', '.join(markers)}" if markers else ""
                st.markdown(f"**{function.name_uk}**{marker_text}")
                st.caption("Приміщення: " + ", ".join(room_names))
                status_text = " / ".join(
                    STATUS_LABELS[status.value]
                    for status in group.recommendation_statuses
                )
                phase_text = " / ".join(
                    PHASE_LABELS[selected_phase]
                    for selected_phase in group.selected_phases
                )
                st.caption(
                    f"{system_map[group.system_id]['name_uk']} · {status_text} · "
                    f"черга реалізації: {phase_text}"
                )
                for reason in reasons:
                    st.write(reason)

    st.subheader("Обсяг з урахуванням бюджетної позиції")
    assert assessment.budget is not None
    st.info(
        f"{BUDGET_LABELS[assessment.budget.level]}. "
        f"{BUDGET_DESCRIPTIONS[assessment.budget.level]}"
    )
    if assessment.budget.amount_eur is not None:
        st.caption(
            "Раніше введена конкретна сума не використовується у сценарії: без "
            "повної специфікації обладнання вона не є достовірним обмеженням."
        )
    focus_labels = {
        "current_focus": "Включити у поточний обсяг",
        "next_stage": "Перенести на наступний етап",
        "later_or_optional": "Залишити як розширення",
    }
    focus_help = {
        "current_focus": "Саме цей перелік змінюється залежно від бюджетної позиції.",
        "next_stage": "Функції залишаються рекомендованими, але не входять до поточного обсягу.",
        "later_or_optional": "Можна реалізувати пізніше без втрати основної користі.",
    }
    if summary.selected_total == 0:
        st.caption(
            "Бюджетна позиція не розподіляє функції за етапами, коли "
            "автоматичний план реалізації порожній."
        )
    focus_columns = st.columns(3) if summary.selected_total else []
    for column, focus_state in zip(focus_columns, focus_labels):
        with column:
            items = [
                item for item in summary.scenario.items if item.focus_state == focus_state
            ]
            groups = group_scenario_functions(items, room_sort_order=room_order)
            st.metric(focus_labels[focus_state], len(groups))
            st.caption(
                f"{len(items)} застосувань у приміщеннях. {focus_help[focus_state]}"
            )
            if not items:
                st.caption("Немає функцій")
            for group in groups:
                function = catalog_map[group.function_id]
                room_names = [room_map[room_id].name for room_id in group.room_ids]
                phase_text = " / ".join(
                    PHASE_LABELS[selected_phase]
                    for selected_phase in group.selected_phases
                )
                prep = " · передбачити підготовку" if group.preparation_required else ""
                st.markdown(
                    f"- **{function.name_uk}** · {phase_text}{prep}<br>"
                    f"<small>{', '.join(room_names)}</small>",
                    unsafe_allow_html=True,
                )

    if summary.unmet_dependencies:
        st.error(
            f"Не додано обов'язкових технічних передумов: "
            f"{summary.unmet_dependencies}."
        )
    else:
        st.caption(
            f"Система автоматично перевірила {summary.dependency_links} "
            "технічних передумов; усі потрібні допоміжні функції враховано."
        )
    st.warning(
        "Це попередній планувальний документ, а не проєкт, специфікація чи комерційна "
        "пропозиція. Бюджетна позиція змінює поточний фокус реалізації, але не "
        "переписує оцінки систем або черги та не є кошторисом. Обладнання, роботи, кабелі, сумісність, "
        "навантаження і розміщення визначає інтегратор після обстеження об'єкта."
    )

    st.subheader("Експорт")
    try:
        pdf_payload = build_project_pdf(assessment, catalog, systems)
        st.download_button(
            "Завантажити попередній проєкт у PDF",
            data=pdf_payload,
            file_name=safe_pdf_filename(assessment),
            mime="application/pdf",
            icon=":material/download:",
            type="primary",
            use_container_width=True,
        )
        st.caption(
            "PDF формується в пам'яті з поточної ревізії та не створює нового запису в базі."
        )
    except PdfGenerationError as error:
        st.error(f"Не вдалося сформувати PDF: {error}")

    edit_object, edit_functions, edit_budget = st.columns(3)
    if edit_object.button("Об'єкт", icon=":material/edit:", use_container_width=True):
        if assessment.property_type is None:
            st.error("У проєкті не визначено тип житла.")
        else:
            initialize_object_step_state(assessment.property_type, assessment.rooms)
            st.session_state.summary_step_active = False
            st.session_state.object_step_active = True
            st.rerun()
    if edit_functions.button("Функції", icon=":material/tune:", use_container_width=True):
        st.session_state.summary_step_active = False
        st.session_state.function_step_active = True
        st.rerun()
    if edit_budget.button("Бюджет", icon=":material/payments:", use_container_width=True):
        st.session_state.summary_step_active = False
        st.session_state.budget_step_active = True
        st.rerun()
    st.caption(
        "Зміна відповідей анкети, типу житла, площі або поверховості має створювати "
        "нове пов'язане оцінювання; цей перехід буде додано окремим підетапом."
    )


def render_saved_questionnaire(assessment: Assessment) -> None:
    registry, questionnaire = load_v2_intake_configuration()
    intake = (assessment.raw_payload or {}).get("intake", {})
    saved_profile = intake.get("profile_answers") or assessment.answers
    object_answers = {
        field.label_uk: _candidate_display_value(assessment.answers[field.field_id])
        for field in registry.parameters
        if assessment.answers.get(field.field_id) is not None
    }
    profile_answers = {
        question.text: _candidate_display_value(saved_profile[question.id])
        for question in questionnaire.questions
        if saved_profile.get(question.id) is not None
    }
    with st.expander("Відповіді анкети"):
        sections = {"Дані про житло": object_answers, "Профіль потреб": profile_answers}
        for title, responses in sections.items():
            if responses:
                st.markdown(f"**{title}**")
                for label, answer in responses.items():
                    st.text(f"{label} — {answer}")
        if not any(sections.values()):
            st.info("Збережених відповідей поки немає.")
            return
        st.download_button(
            "Завантажити відповіді",
            data=json.dumps(sections, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name=f"anketa-{assessment.assessment_id}.json",
            mime="application/json",
            key=f"export_answers_{assessment.assessment_id}",
            on_click="ignore",
        )


def render_projects() -> None:
    hosted = cloud_mode()
    if not hosted:
        st.markdown('<div class="app-kicker">PostgreSQL</div>', unsafe_allow_html=True)
    st.title("Мої проєкти" if hosted else "Збережені проєкти")
    page = st.session_state.get("projects_page", 0) if hosted else 0
    page_size = 12 if hosted else 50
    try:
        projects = project_repository().list_recent(
            limit=page_size + int(hosted), offset=page * page_size
        )
    except (SQLAlchemyError, RuntimeError, ValueError):
        st.error("Не вдалося прочитати список проєктів із PostgreSQL.")
        return
    if not projects:
        st.info("У вас ще немає проєктів." if hosted else "У PostgreSQL ще немає проєктів.")
        return

    stage_labels = {
        WorkflowStage.DRAFT: "Анкета об'єкта",
        WorkflowStage.RECOMMENDATION_COMPLETE: "Рекомендації",
        WorkflowStage.OBJECT_COMPLETE: "Об'єкт",
        WorkflowStage.FUNCTIONS_COMPLETE: "Функції",
        WorkflowStage.BUDGET_COMPLETE: "Підсумок",
    }
    for assessment in projects[:page_size]:
        with st.container(border=True):
            title_col, action_col = st.columns([4, 1])
            title_col.markdown(f"**{assessment.project_name or 'Проєкт без назви'}**")
            title_col.caption(
                f"{stage_labels.get(assessment.workflow_stage, assessment.workflow_stage.value)} · "
                f"ревізія {assessment.revision} · "
                f"{assessment.updated_at.astimezone():%d.%m.%Y %H:%M}"
            )
            if action_col.button(
                (
                    "Продовжити"
                    if assessment.workflow_stage == WorkflowStage.DRAFT
                    else "Відкрити"
                ),
                key=f"open_project_{assessment.assessment_id}",
                use_container_width=True,
            ):
                try:
                    activate_saved_project(assessment)
                    st.rerun()
                except ValueError:
                    st.error("Проєкт не містить достатньо даних для відкриття.")
            if hosted:
                render_saved_questionnaire(assessment)
    if hosted and (page or len(projects) > page_size):
        previous, following = st.columns(2)
        if previous.button("Попередня сторінка", disabled=page == 0):
            st.session_state.projects_page = page - 1
            st.rerun()
        if following.button("Наступна сторінка", disabled=len(projects) <= page_size):
            st.session_state.projects_page = page + 1
            st.rerun()


def render_history(systems: list[dict[str, Any]]) -> None:
    st.markdown('<div class="app-kicker">Локальні дані</div>', unsafe_allow_html=True)
    st.title("Історія оцінювань")
    records = load_history()
    if not records:
        st.info("Історія поки порожня.")
        return

    labels = {}
    for record in records:
        created = datetime.fromisoformat(record["created_at"]).astimezone()
        property_name = option_label(record["answers"].get("property_type", ""))
        labels[record["record_id"]] = f"{created:%d.%m.%Y %H:%M} · {property_name}"
    selected_id = st.selectbox(
        "Збережені оцінювання",
        options=[record["record_id"] for record in records],
        format_func=lambda record_id: labels[record_id],
    )
    record = next(item for item in records if item["record_id"] == selected_id)
    st.markdown(
        f'<div class="history-meta">Запис {record["record_id"]}</div>',
        unsafe_allow_html=True,
    )
    render_results(record["result"], systems, from_history=True)


def main() -> None:
    if not cloud_mode():
        load_env_file()
    hosted = cloud_mode()
    if hosted:
        require_cloud_login()
    init_state()
    questions, systems = load_config()

    if st.session_state.pending_navigation_view is not None:
        st.session_state.navigation_view = st.session_state.pending_navigation_view
        st.session_state.pending_navigation_view = None

    with st.sidebar:
        st.markdown("## Розумний вибір")
        st.caption("Персональний підбір систем для квартири або будинку")
        view_options = ["Нова оцінка", "Мої проєкти"] if hosted else ["Нова оцінка", "Проєкти", "Історія"]
        view = st.radio(
            "Розділ",
            view_options,
            label_visibility="collapsed",
            key="navigation_view",
        )
        st.session_state.view = view
        st.divider()
        if hosted:
            provider = "openai" if os.getenv("OPENAI_API_KEY") else "rules"
            st.caption("Анкети й результати зберігаються у вашому обліковому записі.")
            if provider == "openai":
                st.caption("Файл плану обробляється після вашої згоди. У проєкті залишаються знайдені дані.")
            if st.button("Вийти"):
                st.logout()
        else:
            provider_label = st.selectbox(
                "Режим аналізу",
                ["Хмарна модель", "Демонстраційний"],
                index=0,
            )
            provider = "openai" if provider_label == "Хмарна модель" else "mock"
            st.caption(
                "Проєкт зберігається локально. План передається хмарній моделі лише після окремої згоди."
                if provider == "openai"
                else "Демонстраційний режим не виконує зовнішніх запитів."
            )

    if view in {"Мої проєкти", "Проєкти"}:
        render_projects()
    elif view == "Історія" and not hosted:
        render_history(systems)
    elif st.session_state.summary_step_active:
        render_summary(systems)
    elif st.session_state.budget_step_active:
        render_budget_step()
    elif st.session_state.function_step_active:
        render_function_step(systems)
    elif st.session_state.object_step_active:
        render_object_step()
    elif st.session_state.result is not None:
        render_results(st.session_state.result, systems)
    elif not st.session_state.v2_object_complete:
        if st.session_state.v2_intake_phase == "source":
            render_object_source_v2(provider)
        else:
            render_object_review_v2(provider)
    else:
        render_behavior_questionnaire_v2(provider)


if __name__ == "__main__":
    main()
