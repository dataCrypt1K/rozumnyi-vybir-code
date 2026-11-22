from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
from typing import Any, Iterator

from pydantic import TypeAdapter

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    false,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from domain.enums import (
    BudgetLevel,
    DecisionSource,
    EvidenceSource,
    EvidenceType,
    ImplementationPhase,
    LocationKind,
    PropertyType,
    RecommendationStatus,
    ReliabilityLevel,
    ReviewStatus,
    RoomType,
    WorkflowStage,
)
from domain.models import (
    Assessment,
    BudgetPreference,
    DocumentRef,
    EvidenceFact,
    FunctionChoice,
    FunctionRecommendation,
    ImplementationChoice,
    RecommendationSnapshot,
    Room,
    TechnicalCalculation,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "storage" / "smart_home.db"


def normalize_database_url(url: str) -> str:
    """Use psycopg 3 for provider URLs with an implicit PostgreSQL driver."""
    scheme, separator, endpoint = url.strip().partition(":")
    if scheme.lower() in {"postgres", "postgresql"}:
        scheme = "postgresql+psycopg"
    return f"{scheme}{separator}{endpoint}"


def default_database_url() -> str:
    configured = os.getenv("DATABASE_URL")
    if not configured:
        raise RuntimeError(
            "DATABASE_URL is required for runtime storage. "
            "Use an explicit SQLite URL only in tests."
        )
    return normalize_database_url(configured)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AssessmentRecord(Base):
    __tablename__ = "assessments"
    __table_args__ = (
        Index("ix_assessments_updated_at", "updated_at"),
        Index("ix_assessments_owner_updated_at", "owner_key", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    project_name: Mapped[str | None] = mapped_column(String(160))
    derived_from_assessment_id: Mapped[str | None] = mapped_column(
        ForeignKey("assessments.id"), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    workflow_stage: Mapped[str] = mapped_column(String(40), nullable=False)
    property_type: Mapped[str | None] = mapped_column(String(24))
    area_m2: Mapped[float | None] = mapped_column(Float)
    floor_count: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(80))
    model_name: Mapped[str | None] = mapped_column(String(160))
    config_versions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AssessmentAnswerRecord(Base):
    __tablename__ = "assessment_answers"

    assessment_id: Mapped[str] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), primary_key=True
    )
    question_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="questionnaire")


class RecommendationRecord(Base):
    __tablename__ = "recommendations"
    __table_args__ = (
        UniqueConstraint(
            "assessment_id",
            "system_id",
            "status",
            name="uq_recommendations_identity_status",
        ),
        CheckConstraint(
            "(status = 'not_applicable' AND suitability_percent IS NULL) OR "
            "(status <> 'not_applicable' AND suitability_percent BETWEEN 0 AND 100)",
            name="ck_recommendations_suitability_applicability",
        ),
        CheckConstraint(
            "rule_score IS NULL OR rule_score BETWEEN 0 AND 100",
            name="ck_recommendations_rule_score",
        ),
        CheckConstraint(
            "llm_score IS NULL OR llm_score BETWEEN 0 AND 100",
            name="ck_recommendations_llm_score",
        ),
    )

    assessment_id: Mapped[str] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), primary_key=True
    )
    system_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    suitability_percent: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    rule_score: Mapped[int | None] = mapped_column(Integer)
    llm_score: Mapped[int | None] = mapped_column(Integer)
    decision_source: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence_question_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    missing_data: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


class RoomRecord(Base):
    __tablename__ = "rooms"
    __table_args__ = (
        UniqueConstraint("assessment_id", "sort_order", name="uq_rooms_assessment_sort"),
        UniqueConstraint("assessment_id", "id", name="uq_rooms_assessment_id_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assessment_id: Mapped[str] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    room_type: Mapped[str] = mapped_column(String(50), nullable=False)
    location_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    floor: Mapped[int | None] = mapped_column(Integer)
    area_m2: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    reliability_level: Mapped[str | None] = mapped_column(String(16))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ImplementationChoiceRecord(Base):
    __tablename__ = "implementation_choices"
    __table_args__ = (
        ForeignKeyConstraint(
            ["assessment_id", "system_id", "recommendation_status"],
            [
                "recommendations.assessment_id",
                "recommendations.system_id",
                "recommendations.status",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "NOT (recommendation_status = 'not_applicable' AND selected)",
            name="ck_implementation_choices_not_applicable_unselected",
        ),
        CheckConstraint(
            "recommendation_status <> 'not_applicable' OR phase IS NULL",
            name="ck_implementation_choices_not_applicable_no_phase",
        ),
    )

    assessment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    system_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    recommendation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    suitability_percent: Mapped[int | None] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    phase: Mapped[str | None] = mapped_column(String(24), nullable=True)
    decision_source: Mapped[str] = mapped_column(String(32), nullable=False)


class BudgetPreferenceRecord(Base):
    __tablename__ = "budget_preferences"

    assessment_id: Mapped[str] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), primary_key=True
    )
    level: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_eur: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))


class DocumentRecord(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assessment_id: Mapped[str] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(80), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvidenceFactRecord(Base):
    __tablename__ = "evidence_facts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assessment_id: Mapped[str] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_id: Mapped[str] = mapped_column(String(160), nullable=False)
    entity_ref: Mapped[str | None] = mapped_column(String(160))
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(500))
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    page_number: Mapped[int | None] = mapped_column(Integer)
    reliability_level: Mapped[str] = mapped_column(String(16), nullable=False)
    review_status: Mapped[str] = mapped_column(String(24), nullable=False)
    corrected_value_json: Mapped[Any | None] = mapped_column(JSON)


class FunctionRecommendationRecord(Base):
    __tablename__ = "function_recommendations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["assessment_id", "room_id"],
            ["rooms.assessment_id", "rooms.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["assessment_id", "system_id"],
            ["recommendations.assessment_id", "recommendations.system_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "assessment_id",
            "function_id",
            "room_id",
            "system_id",
            name="uq_function_recommendations_identity_system",
        ),
        CheckConstraint(
            "safety_criticality BETWEEN 0 AND 2",
            name="ck_function_recommendations_safety_criticality",
        ),
        CheckConstraint(
            "retrofit_penalty BETWEEN 0 AND 2",
            name="ck_function_recommendations_retrofit_penalty",
        ),
        CheckConstraint(
            "dependency_role BETWEEN 0 AND 2",
            name="ck_function_recommendations_dependency_role",
        ),
        CheckConstraint(
            "daily_impact BETWEEN 0 AND 2",
            name="ck_function_recommendations_daily_impact",
        ),
        CheckConstraint(
            "(status = 'not_applicable' AND recommended_phase IS NULL) OR "
            "(status <> 'not_applicable' AND recommended_phase IS NOT NULL)",
            name="ck_function_recommendations_phase_applicability",
        ),
        Index(
            "ix_function_recommendations_assessment_system",
            "assessment_id",
            "system_id",
        ),
    )

    assessment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    function_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    room_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    system_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_phase: Mapped[str | None] = mapped_column(String(24), nullable=True)
    decision_source: Mapped[str] = mapped_column(String(80), nullable=False)
    rule_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


    safety_criticality: Mapped[int] = mapped_column(Integer, nullable=False)
    retrofit_penalty: Mapped[int] = mapped_column(Integer, nullable=False)
    dependency_role: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_impact: Mapped[int] = mapped_column(Integer, nullable=False)
    phase_rules_version: Mapped[str] = mapped_column(String(80), nullable=False)


class FunctionChoiceRecord(Base):
    __tablename__ = "function_choices"
    __table_args__ = (
        ForeignKeyConstraint(
            ["assessment_id", "function_id", "room_id", "system_id"],
            [
                "function_recommendations.assessment_id",
                "function_recommendations.function_id",
                "function_recommendations.room_id",
                "function_recommendations.system_id",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(selected AND selected_phase IS NOT NULL) OR "
            "(NOT selected AND selected_phase IS NULL)",
            name="ck_function_choices_selected_phase",
        ),
        Index("ix_function_choices_assessment_selected", "assessment_id", "selected"),
    )

    assessment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    function_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    room_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    system_id: Mapped[str] = mapped_column(String(120), nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    selected_phase: Mapped[str | None] = mapped_column(String(24), nullable=True)
    decision_source: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TechnicalCalculationRecord(Base):
    __tablename__ = "technical_calculations"

    assessment_id: Mapped[str] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), primary_key=True
    )
    rules_version: Mapped[str] = mapped_column(String(80), nullable=False)
    reserve_ratio: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class LegacyImportRecord(Base):
    __tablename__ = "legacy_imports"

    legacy_record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class Database:
    def __init__(self, url: str | None = None) -> None:
        self.url = normalize_database_url(url) if url else default_database_url()
        connect_args = {"check_same_thread": False} if self.url.startswith("sqlite") else {}
        self.engine = create_engine(
            self.url,
            future=True,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        if self.url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, class_=Session
        )

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def create_schema(self) -> None:
        """Explicit schema initialization for tests and local tooling only."""
        Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def dispose(self) -> None:
        self.engine.dispose()


class AssessmentNotFoundError(LookupError):
    pass


class RevisionConflictError(RuntimeError):
    def __init__(self, assessment_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"assessment {assessment_id} revision conflict: "
            f"expected {expected}, actual {actual}"
        )
        self.assessment_id = assessment_id
        self.expected = expected
        self.actual = actual


class AssessmentAlreadyExistsError(RuntimeError):
    """Raised when the creation-only save API receives an existing assessment."""


class DownstreamRecomputationRequiredError(RuntimeError):
    """Raised when object changes would invalidate computed recommendations."""


class AssessmentRepository:
    def __init__(self, database: Database, *, owner_key: str | None = None) -> None:
        if owner_key is not None and (
            not isinstance(owner_key, str) or not owner_key.strip() or len(owner_key) > 128
        ):
            raise ValueError("owner_key must contain between 1 and 128 characters")
        self.database = database
        self._owner_key = owner_key

    def _owner_filter(self) -> ColumnElement[bool]:
        # Unassigned projects belong to the local workflow only.
        return AssessmentRecord.owner_key == self._owner_key

    def _owned_record(self, session: Session, assessment_id: str) -> AssessmentRecord | None:
        return session.scalar(
            select(AssessmentRecord).where(
                AssessmentRecord.id == assessment_id,
                self._owner_filter(),
            )
        )

    def save(
        self,
        assessment: Assessment,
        *,
        session: Session | None = None,
    ) -> None:
        """Create a new assessment; existing assessments require step APIs."""
        if session is not None:
            self._save(session, assessment)
            return
        with self.database.transaction() as transaction:
            self._save(transaction, assessment)

    def save_intake_step(
        self,
        assessment_id: str,
        expected_revision: int,
        *,
        provider: str,
        model_name: str | None = None,
        config_versions: dict[str, Any] | None = None,
        raw_payload: dict[str, Any] | None = None,
        documents: list[DocumentRef] | None = None,
        evidence_facts: list[EvidenceFact] | None = None,
        property_type: PropertyType | None = None,
        area_m2: float | None = None,
        floor_count: int | None = None,
        project_name: str | None = None,
        answers: dict[str, Any] | None = None,
    ) -> Assessment:
        """Persist a resumable intake draft before recommendations exist."""
        with self.database.transaction() as session:
            current = self._require(session, assessment_id)
            if current.workflow_stage != WorkflowStage.DRAFT:
                raise ValueError("intake can only update a draft assessment")
            if current.recommendations:
                raise ValueError("draft intake cannot contain recommendations")

            merged_versions = dict(current.config_versions)
            if config_versions:
                merged_versions.update(config_versions)
            proposed = self._validated_copy(
                current,
                provider=provider,
                model_name=model_name or current.model_name,
                config_versions=merged_versions,
                raw_payload=(raw_payload if raw_payload is not None else current.raw_payload),
                documents=(documents if documents is not None else current.documents),
                evidence_facts=(
                    evidence_facts
                    if evidence_facts is not None
                    else current.evidence_facts
                ),
                property_type=property_type or current.property_type,
                area_m2=area_m2 if area_m2 is not None else current.area_m2,
                floor_count=(
                    floor_count if floor_count is not None else current.floor_count
                ),
                project_name=project_name or current.project_name,
                answers=answers if answers is not None else current.answers,
            )
            payload = proposed.model_dump(mode="json")
            existing_document_ids = {item.document_id for item in current.documents}
            self._check_evidence_documents(proposed, existing_document_ids)
            self._claim_revision(
                session,
                assessment_id,
                expected_revision,
                WorkflowStage.DRAFT,
                project_name=proposed.project_name,
                property_type=(
                    proposed.property_type.value if proposed.property_type else None
                ),
                area_m2=proposed.area_m2,
                floor_count=proposed.floor_count,
                provider=proposed.provider,
                model_name=proposed.model_name,
                config_versions=payload["config_versions"],
                raw_payload=payload["raw_payload"],
            )

            if answers is not None:
                session.execute(
                    delete(AssessmentAnswerRecord).where(
                        AssessmentAnswerRecord.assessment_id == assessment_id
                    )
                )
                for question_id, value in payload["answers"].items():
                    session.add(
                        AssessmentAnswerRecord(
                            assessment_id=assessment_id,
                            question_id=question_id,
                            value_json=value,
                            source=EvidenceSource.MANUAL.value,
                        )
                    )
            if documents is not None:
                for item in proposed.documents:
                    document = self._document_record(assessment_id, item)
                    if item.document_id in existing_document_ids:
                        session.merge(document)
                    else:
                        session.add(document)
                session.flush()
            if evidence_facts is not None:
                session.execute(
                    delete(EvidenceFactRecord).where(
                        EvidenceFactRecord.assessment_id == assessment_id
                    )
                )
                for item in proposed.evidence_facts:
                    session.add(self._evidence_fact_record(assessment_id, item))
            session.flush()
            session.expire_all()
            return self._require(session, assessment_id)

    def save_recommendation_step(
        self,
        assessment_id: str,
        expected_revision: int,
        *,
        completed: Assessment,
    ) -> Assessment:
        """Promote an existing intake draft to a completed recommendation."""
        if completed.assessment_id != assessment_id:
            raise ValueError("completed assessment id does not match draft id")
        if completed.workflow_stage != WorkflowStage.RECOMMENDATION_COMPLETE:
            raise ValueError("completed assessment must be at recommendation_complete")

        with self.database.transaction() as session:
            current = self._require(session, assessment_id)
            if current.workflow_stage != WorkflowStage.DRAFT:
                raise ValueError("only a draft can be promoted to recommendations")
            if current.recommendations:
                raise ValueError("draft already contains recommendations")

            merged_versions = dict(current.config_versions)
            merged_versions.update(completed.config_versions)
            proposed = Assessment.model_validate(
                {
                    **completed.model_dump(mode="python"),
                    "derived_from_assessment_id": current.derived_from_assessment_id,
                    "revision": expected_revision + 1,
                    "created_at": current.created_at,
                    "project_name": completed.project_name or current.project_name,
                    "documents": current.documents,
                    "evidence_facts": current.evidence_facts,
                    "config_versions": merged_versions,
                    "raw_payload": completed.raw_payload or current.raw_payload,
                }
            )
            payload = proposed.model_dump(mode="json")
            self._claim_revision(
                session,
                assessment_id,
                expected_revision,
                WorkflowStage.RECOMMENDATION_COMPLETE,
                project_name=proposed.project_name,
                property_type=proposed.property_type.value,
                area_m2=proposed.area_m2,
                floor_count=proposed.floor_count,
                provider=proposed.provider,
                model_name=proposed.model_name,
                config_versions=payload["config_versions"],
                raw_payload=payload["raw_payload"],
            )
            session.execute(
                delete(AssessmentAnswerRecord).where(
                    AssessmentAnswerRecord.assessment_id == assessment_id
                )
            )
            for question_id, value in payload["answers"].items():
                session.add(
                    AssessmentAnswerRecord(
                        assessment_id=assessment_id,
                        question_id=question_id,
                        value_json=value,
                        source=EvidenceSource.QUESTIONNAIRE.value,
                    )
                )
            for item in proposed.recommendations:
                session.add(
                    RecommendationRecord(
                        assessment_id=assessment_id,
                        system_id=item.system_id,
                        status=item.status.value,
                        suitability_percent=item.suitability_percent,
                        reason=item.reason,
                        rule_score=item.rule_score,
                        llm_score=item.llm_score,
                        decision_source=item.decision_source,
                        evidence_question_ids=item.evidence_question_ids,
                        missing_data=item.missing_data,
                        needs_review=item.needs_review,
                    )
                )
            session.flush()
            session.expire_all()
            return self._require(session, assessment_id)

    def save_object_step(
        self,
        assessment_id: str,
        expected_revision: int,
        *,
        project_name: str,
        area_m2: float,
        floor_count: int,
        rooms: list[Room | dict[str, Any]],
    ) -> Assessment:
        with self.database.transaction() as session:
            current = self._require(session, assessment_id)
            adapter = TypeAdapter(Any)
            normalized_rooms = [
                Room.model_validate(adapter.dump_python(item, mode="json"))
                for item in rooms
            ]
            if current.area_m2 is not None and abs(current.area_m2 - area_m2) > 1e-9:
                raise ValueError(
                    "area_m2 is recommendation input; changing it requires a new assessment"
                )
            if current.floor_count is not None and current.floor_count != floor_count:
                raise ValueError(
                    "floor_count is recommendation input; changing it requires a new assessment"
                )
            if current.function_recommendations:
                current_rooms = [item.model_dump(mode="json") for item in current.rooms]
                requested_rooms = [item.model_dump(mode="json") for item in normalized_rooms]
                if current_rooms != requested_rooms:
                    raise DownstreamRecomputationRequiredError(
                        "room changes require a new assessment or explicit downstream recomputation"
                    )
            proposed = self._validated_copy(
                current,
                project_name=project_name,
                area_m2=area_m2,
                floor_count=floor_count,
                rooms=normalized_rooms,
            )
            next_stage = self._later_stage(
                current.workflow_stage, WorkflowStage.OBJECT_COMPLETE
            )
            self._claim_revision(
                session,
                assessment_id,
                expected_revision,
                next_stage,
                project_name=proposed.project_name,
                area_m2=proposed.area_m2,
                floor_count=proposed.floor_count,
            )

            session.execute(
                update(RoomRecord)
                .where(RoomRecord.assessment_id == assessment_id)
                .values(sort_order=RoomRecord.sort_order + 1_000_000)
            )
            new_ids = {item.room_id for item in proposed.rooms}
            delete_statement = delete(RoomRecord).where(
                RoomRecord.assessment_id == assessment_id
            )
            if new_ids:
                delete_statement = delete_statement.where(RoomRecord.id.not_in(new_ids))
            session.execute(delete_statement)
            existing_room_ids = {item.room_id for item in current.rooms}
            for item in proposed.rooms:
                room = self._room_record(assessment_id, item)
                if item.room_id in existing_room_ids:
                    session.merge(room)
                else:
                    session.add(room)
            session.flush()
            session.expire_all()
            return self._require(session, assessment_id)

    def save_function_step(
        self,
        assessment_id: str,
        expected_revision: int,
        *,
        recommendations: list[FunctionRecommendation] | None,
        choices: list[FunctionChoice],
        config_versions: dict[str, Any] | None = None,
    ) -> Assessment:
        with self.database.transaction() as session:
            current = self._require(session, assessment_id)
            effective_recommendations = (
                current.function_recommendations
                if recommendations is None
                else recommendations
            )
            if current.function_recommendations and recommendations is not None:
                existing_payload = {
                    (item.function_id, item.room_id): item.model_dump(mode="json")
                    for item in current.function_recommendations
                }
                new_payload = {
                    (item.function_id, item.room_id): item.model_dump(mode="json")
                    for item in recommendations
                }
                if existing_payload != new_payload:
                    raise ValueError(
                        "function recommendations are immutable for an assessment"
                    )
            merged_config_versions = dict(current.config_versions)
            if config_versions:
                merged_config_versions.update(config_versions)
            proposed = self._validated_copy(
                current,
                function_recommendations=effective_recommendations,
                function_choices=choices,
                config_versions=merged_config_versions,
            )
            next_stage = self._later_stage(
                current.workflow_stage, WorkflowStage.FUNCTIONS_COMPLETE
            )
            self._claim_revision(
                session,
                assessment_id,
                expected_revision,
                next_stage,
                config_versions=proposed.config_versions,
            )

            session.execute(
                delete(FunctionChoiceRecord).where(
                    FunctionChoiceRecord.assessment_id == assessment_id
                )
            )
            if not current.function_recommendations:
                for item in proposed.function_recommendations:
                    session.add(self._function_recommendation_record(assessment_id, item))
                session.flush()
            for item in proposed.function_choices:
                session.add(self._function_choice_record(assessment_id, item))
            session.flush()
            session.expire_all()
            return self._require(session, assessment_id)

    def save_budget_step(
        self,
        assessment_id: str,
        expected_revision: int,
        *,
        budget: BudgetPreference,
    ) -> Assessment:
        with self.database.transaction() as session:
            current = self._require(session, assessment_id)
            proposed = self._validated_copy(current, budget=budget)
            next_stage = self._later_stage(
                current.workflow_stage, WorkflowStage.BUDGET_COMPLETE
            )
            self._claim_revision(
                session, assessment_id, expected_revision, next_stage
            )
            session.merge(
                BudgetPreferenceRecord(
                    assessment_id=assessment_id,
                    level=proposed.budget.level.value,
                    amount_eur=proposed.budget.amount_eur,
                )
            )
            session.flush()
            session.expire_all()
            return self._require(session, assessment_id)

    def save_technical_step(
        self,
        assessment_id: str,
        expected_revision: int,
        *,
        calculation: TechnicalCalculation,
    ) -> Assessment:
        with self.database.transaction() as session:
            current = self._require(session, assessment_id)
            if current.budget is None:
                raise ValueError("technical inputs require a completed budget step")
            merged_config_versions = dict(current.config_versions)
            merged_config_versions["technical_rules"] = calculation.rules_version
            proposed = self._validated_copy(
                current,
                technical_calculation=calculation,
                config_versions=merged_config_versions,
            )
            self._claim_revision(
                session,
                assessment_id,
                expected_revision,
                self._later_stage(current.workflow_stage, WorkflowStage.TECHNICAL_INPUT),
                config_versions=proposed.config_versions,
            )
            payload = proposed.technical_calculation.model_dump(mode="json")
            session.merge(
                TechnicalCalculationRecord(
                    assessment_id=assessment_id,
                    rules_version=proposed.technical_calculation.rules_version,
                    reserve_ratio=proposed.technical_calculation.reserve_ratio,
                    needs_review=proposed.technical_calculation.needs_review,
                    payload_json=payload,
                )
            )
            session.flush()
            session.expire_all()
            return self._require(session, assessment_id)

    @staticmethod
    def _validated_copy(current: Assessment, **updates: Any) -> Assessment:
        payload = current.model_dump(mode="json")
        adapter = TypeAdapter(Any)
        payload.update(
            {key: adapter.dump_python(value, mode="json") for key, value in updates.items()}
        )
        return Assessment.model_validate(payload)

    @staticmethod
    def _later_stage(
        current: WorkflowStage, requested: WorkflowStage
    ) -> WorkflowStage:
        order = {
            WorkflowStage.DRAFT: 0,
            WorkflowStage.RECOMMENDATION_COMPLETE: 1,
            WorkflowStage.OBJECT_COMPLETE: 2,
            WorkflowStage.FUNCTIONS_COMPLETE: 3,
            WorkflowStage.SCOPE_COMPLETE: 3,
            WorkflowStage.BUDGET_COMPLETE: 4,
            WorkflowStage.FACTS_REVIEW: 5,
            WorkflowStage.TECHNICAL_INPUT: 6,
            WorkflowStage.SPECIFICATION_COMPLETE: 7,
        }
        return requested if order[requested] > order[current] else current

    def _claim_revision(
        self,
        session: Session,
        assessment_id: str,
        expected_revision: int,
        workflow_stage: WorkflowStage,
        **values: Any,
    ) -> None:
        values.update(
            revision=expected_revision + 1,
            updated_at=utc_now(),
            workflow_stage=workflow_stage.value,
        )
        result = session.execute(
            update(AssessmentRecord)
            .where(
                AssessmentRecord.id == assessment_id,
                AssessmentRecord.revision == expected_revision,
                self._owner_filter(),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            return
        record = self._owned_record(session, assessment_id)
        if record is None:
            raise AssessmentNotFoundError(assessment_id)
        session.refresh(record, attribute_names=["revision"])
        raise RevisionConflictError(assessment_id, expected_revision, record.revision)

    @staticmethod
    def _room_record(assessment_id: str, item: Room) -> RoomRecord:
        return RoomRecord(
            id=item.room_id,
            assessment_id=assessment_id,
            name=item.name,
            room_type=item.room_type.value,
            location_kind=item.location_kind.value,
            floor=item.floor,
            area_m2=item.area_m2,
            source=item.source.value,
            reliability_level=(
                item.reliability_level.value if item.reliability_level else None
            ),
            sort_order=item.sort_order,
        )

    @staticmethod
    def _document_record(assessment_id: str, item: DocumentRef) -> DocumentRecord:
        return DocumentRecord(
            id=item.document_id,
            assessment_id=assessment_id,
            original_name=item.original_name,
            mime_type=item.mime_type,
            stored_path=item.stored_path,
            sha256=item.sha256,
            size_bytes=item.size_bytes,
            page_count=item.page_count,
            created_at=item.created_at,
        )

    @staticmethod
    def _evidence_fact_record(
        assessment_id: str, item: EvidenceFact
    ) -> EvidenceFactRecord:
        payload = item.model_dump(mode="json")
        return EvidenceFactRecord(
            id=item.fact_id,
            assessment_id=assessment_id,
            field_id=item.field_id,
            entity_ref=item.entity_ref,
            value_json=payload["value"],
            source=item.source.value,
            evidence_type=item.evidence_type.value,
            source_reference=item.source_reference,
            document_id=item.document_id,
            page_number=item.page_number,
            reliability_level=item.reliability_level.value,
            review_status=item.review_status.value,
            corrected_value_json=payload["corrected_value"],
        )

    @staticmethod
    def _function_recommendation_record(
        assessment_id: str, item: FunctionRecommendation
    ) -> FunctionRecommendationRecord:
        return FunctionRecommendationRecord(
            assessment_id=assessment_id,
            function_id=item.function_id,
            system_id=item.system_id,
            room_id=item.room_id,
            status=item.status.value,
            reason=item.reason,
            recommended_phase=(
                item.recommended_phase.value if item.recommended_phase else None
            ),
            decision_source=item.decision_source,
            rule_codes=item.rule_codes,
            needs_review=item.needs_review,
            safety_criticality=item.safety_criticality,
            retrofit_penalty=item.retrofit_penalty,
            dependency_role=item.dependency_role,
            daily_impact=item.daily_impact,
            phase_rules_version=item.phase_rules_version,
        )

    @staticmethod
    def _function_choice_record(
        assessment_id: str, item: FunctionChoice
    ) -> FunctionChoiceRecord:
        return FunctionChoiceRecord(
            assessment_id=assessment_id,
            function_id=item.function_id,
            system_id=item.system_id,
            room_id=item.room_id,
            selected=item.selected,
            selected_phase=(item.selected_phase.value if item.selected_phase else None),
            decision_source=item.decision_source.value,
            updated_at=item.updated_at,
        )

    def _require(self, session: Session, assessment_id: str) -> Assessment:
        assessment = self._get(session, assessment_id)
        if assessment is None:
            raise AssessmentNotFoundError(assessment_id)
        return assessment

    @staticmethod
    def _check_evidence_documents(
        assessment: Assessment, existing_document_ids: set[str] | None = None
    ) -> None:
        document_ids = {item.document_id for item in assessment.documents}
        document_ids.update(existing_document_ids or ())
        if any(
            item.document_id is not None and item.document_id not in document_ids
            for item in assessment.evidence_facts
        ):
            raise ValueError("evidence must reference a document in the same assessment")

    def _save(self, session: Session, assessment: Assessment) -> None:
        payload = assessment.model_dump(mode="json")
        existing = session.get(AssessmentRecord, assessment.assessment_id)
        if existing is not None:
            if existing.owner_key == self._owner_key:
                raise AssessmentAlreadyExistsError(assessment.assessment_id)
            raise AssessmentNotFoundError(assessment.assessment_id)
        source_id = assessment.derived_from_assessment_id
        if source_id is not None and self._owned_record(session, source_id) is None:
            raise AssessmentNotFoundError(source_id)
        self._check_evidence_documents(assessment)
        session.add(
            AssessmentRecord(
                id=assessment.assessment_id,
                owner_key=self._owner_key,
                project_name=assessment.project_name,
                derived_from_assessment_id=assessment.derived_from_assessment_id,
                revision=assessment.revision,
                created_at=assessment.created_at,
                updated_at=assessment.updated_at,
                workflow_stage=assessment.workflow_stage.value,
                property_type=(assessment.property_type.value if assessment.property_type else None),
                area_m2=assessment.area_m2,
                floor_count=assessment.floor_count,
                provider=assessment.provider,
                model_name=assessment.model_name,
                config_versions=payload["config_versions"],
                raw_payload=payload["raw_payload"],
            )
        )
        session.flush()
        for question_id, value in payload["answers"].items():
            session.merge(
                AssessmentAnswerRecord(
                    assessment_id=assessment.assessment_id,
                    question_id=question_id,
                    value_json=value,
                    source=EvidenceSource.QUESTIONNAIRE.value,
                )
            )
        for item in assessment.recommendations:
            session.merge(
                RecommendationRecord(
                    assessment_id=assessment.assessment_id,
                    system_id=item.system_id,
                    status=item.status.value,
                    suitability_percent=item.suitability_percent,
                    reason=item.reason,
                    rule_score=item.rule_score,
                    llm_score=item.llm_score,
                    decision_source=item.decision_source,
                    evidence_question_ids=item.evidence_question_ids,
                    missing_data=item.missing_data,
                    needs_review=item.needs_review,
                )
            )
        for item in assessment.rooms:
            session.add(self._room_record(assessment.assessment_id, item))
        for item in assessment.implementation_choices:
            session.merge(
                ImplementationChoiceRecord(
                    assessment_id=assessment.assessment_id,
                    system_id=item.system_id,
                    recommendation_status=item.recommendation_status.value,
                    suitability_percent=item.suitability_percent,
                    selected=item.selected,
                    phase=item.phase.value if item.phase else None,
                    decision_source=item.decision_source.value,
                )
            )
        if assessment.budget:
            session.merge(
                BudgetPreferenceRecord(
                    assessment_id=assessment.assessment_id,
                    level=assessment.budget.level.value if assessment.budget.level else None,
                    amount_eur=assessment.budget.amount_eur,
                )
            )
        for item in assessment.documents:
            session.add(self._document_record(assessment.assessment_id, item))
        if assessment.documents:
            session.flush()
        for item in assessment.evidence_facts:
            session.add(self._evidence_fact_record(assessment.assessment_id, item))
        session.flush()
        for item in assessment.function_recommendations:
            session.merge(
                FunctionRecommendationRecord(
                    assessment_id=assessment.assessment_id,
                    function_id=item.function_id,
                    system_id=item.system_id,
                    room_id=item.room_id,
                    status=item.status.value,
                    reason=item.reason,
                    recommended_phase=(
                        item.recommended_phase.value if item.recommended_phase else None
                    ),
                    decision_source=item.decision_source,
                    rule_codes=item.rule_codes,
                    safety_criticality=item.safety_criticality,
                    retrofit_penalty=item.retrofit_penalty,
                    dependency_role=item.dependency_role,
                    daily_impact=item.daily_impact,
                    phase_rules_version=item.phase_rules_version,
                )
            )
        if assessment.function_recommendations:
            session.flush()
        for item in assessment.function_choices:
            session.merge(
                FunctionChoiceRecord(
                    assessment_id=assessment.assessment_id,
                    function_id=item.function_id,
                    system_id=item.system_id,
                    room_id=item.room_id,
                    selected=item.selected,
                    selected_phase=(
                        item.selected_phase.value if item.selected_phase else None
                    ),
                    decision_source=item.decision_source.value,
                    updated_at=item.updated_at,
                )
            )
        if assessment.technical_calculation is not None:
            technical_payload = assessment.technical_calculation.model_dump(mode="json")
            session.merge(
                TechnicalCalculationRecord(
                    assessment_id=assessment.assessment_id,
                    rules_version=assessment.technical_calculation.rules_version,
                    reserve_ratio=assessment.technical_calculation.reserve_ratio,
                    needs_review=assessment.technical_calculation.needs_review,
                    payload_json=technical_payload,
                )
            )

    def get(self, assessment_id: str, *, session: Session | None = None) -> Assessment | None:
        if session is not None:
            return self._get(session, assessment_id)
        with self.database.session_factory() as query_session:
            return self._get(query_session, assessment_id)

    def list_recent(self, *, limit: int = 50, offset: int = 0) -> list[Assessment]:
        """Return this owner's projects, newest update first."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        with self.database.session_factory() as session:
            assessment_ids = list(
                session.scalars(
                    select(AssessmentRecord.id)
                    .where(self._owner_filter())
                    .order_by(
                        AssessmentRecord.updated_at.desc(),
                        AssessmentRecord.id.asc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            )
            return [
                assessment
                for assessment_id in assessment_ids
                if (assessment := self._get(session, assessment_id)) is not None
            ]

    def _get(self, session: Session, assessment_id: str) -> Assessment | None:
        record = self._owned_record(session, assessment_id)
        if record is None:
            return None

        answers = {
            item.question_id: item.value_json
            for item in session.scalars(
                select(AssessmentAnswerRecord).where(
                    AssessmentAnswerRecord.assessment_id == assessment_id
                )
            )
        }
        recommendations = [
            RecommendationSnapshot(
                system_id=item.system_id,
                status=RecommendationStatus(item.status),
                suitability_percent=item.suitability_percent,
                reason=item.reason,
                rule_score=item.rule_score,
                llm_score=item.llm_score,
                decision_source=item.decision_source,
                evidence_question_ids=item.evidence_question_ids,
                missing_data=item.missing_data,
                needs_review=item.needs_review,
            )
            for item in session.scalars(
                select(RecommendationRecord).where(
                    RecommendationRecord.assessment_id == assessment_id
                )
            )
        ]
        rooms = [
            Room(
                room_id=item.id,
                name=item.name,
                room_type=RoomType(item.room_type),
                location_kind=LocationKind(item.location_kind),
                floor=item.floor,
                area_m2=item.area_m2,
                source=EvidenceSource(item.source),
                reliability_level=(
                    ReliabilityLevel(item.reliability_level)
                    if item.reliability_level
                    else None
                ),
                sort_order=item.sort_order,
            )
            for item in session.scalars(
                select(RoomRecord)
                .where(RoomRecord.assessment_id == assessment_id)
                .order_by(RoomRecord.sort_order)
            )
        ]
        choices = [
            ImplementationChoice(
                system_id=item.system_id,
                recommendation_status=RecommendationStatus(item.recommendation_status),
                suitability_percent=item.suitability_percent,
                selected=item.selected,
                phase=ImplementationPhase(item.phase) if item.phase else None,
                decision_source=DecisionSource(item.decision_source),
            )
            for item in session.scalars(
                select(ImplementationChoiceRecord).where(
                    ImplementationChoiceRecord.assessment_id == assessment_id
                )
            )
        ]
        budget_record = session.get(BudgetPreferenceRecord, assessment_id)
        budget = (
            BudgetPreference(
                level=BudgetLevel(budget_record.level) if budget_record.level else None,
                amount_eur=budget_record.amount_eur,
            )
            if budget_record
            else None
        )
        documents = [
            DocumentRef(
                document_id=item.id,
                original_name=item.original_name,
                mime_type=item.mime_type,
                stored_path=item.stored_path,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
                page_count=item.page_count,
                created_at=item.created_at,
            )
            for item in session.scalars(
                select(DocumentRecord).where(DocumentRecord.assessment_id == assessment_id)
            )
        ]
        facts = [
            EvidenceFact(
                fact_id=item.id,
                field_id=item.field_id,
                entity_ref=item.entity_ref,
                value=item.value_json,
                source=EvidenceSource(item.source),
                evidence_type=EvidenceType(item.evidence_type),
                source_reference=item.source_reference,
                document_id=item.document_id,
                page_number=item.page_number,
                reliability_level=ReliabilityLevel(item.reliability_level),
                review_status=ReviewStatus(item.review_status),
                corrected_value=item.corrected_value_json,
            )
            for item in session.scalars(
                select(EvidenceFactRecord).where(
                    EvidenceFactRecord.assessment_id == assessment_id
                )
            )
        ]
        function_recommendations = [
            FunctionRecommendation(
                function_id=item.function_id,
                system_id=item.system_id,
                room_id=item.room_id,
                status=RecommendationStatus(item.status),
                reason=item.reason,
                recommended_phase=(
                    ImplementationPhase(item.recommended_phase)
                    if item.recommended_phase
                    else None
                ),
                decision_source=item.decision_source,
                rule_codes=item.rule_codes,
                needs_review=item.needs_review,
                safety_criticality=item.safety_criticality,
                retrofit_penalty=item.retrofit_penalty,
                dependency_role=item.dependency_role,
                daily_impact=item.daily_impact,
                phase_rules_version=item.phase_rules_version,
            )
            for item in session.scalars(
                select(FunctionRecommendationRecord)
                .where(FunctionRecommendationRecord.assessment_id == assessment_id)
                .order_by(
                    FunctionRecommendationRecord.room_id,
                    FunctionRecommendationRecord.function_id,
                )
            )
        ]
        function_choices = [
            FunctionChoice(
                function_id=item.function_id,
                system_id=item.system_id,
                room_id=item.room_id,
                selected=item.selected,
                selected_phase=(
                    ImplementationPhase(item.selected_phase)
                    if item.selected_phase
                    else None
                ),
                decision_source=DecisionSource(item.decision_source),
                updated_at=item.updated_at,
            )
            for item in session.scalars(
                select(FunctionChoiceRecord)
                .where(FunctionChoiceRecord.assessment_id == assessment_id)
                .order_by(
                    FunctionChoiceRecord.room_id,
                    FunctionChoiceRecord.function_id,
                )
            )
        ]
        technical_record = session.get(TechnicalCalculationRecord, assessment_id)
        technical_calculation = (
            TechnicalCalculation.model_validate(technical_record.payload_json)
            if technical_record is not None
            else None
        )
        return Assessment(
            assessment_id=record.id,
            project_name=record.project_name,
            derived_from_assessment_id=record.derived_from_assessment_id,
            revision=record.revision,
            created_at=record.created_at,
            updated_at=record.updated_at,
            workflow_stage=WorkflowStage(record.workflow_stage),
            property_type=PropertyType(record.property_type) if record.property_type else None,
            area_m2=record.area_m2,
            floor_count=record.floor_count,
            provider=record.provider,
            model_name=record.model_name,
            config_versions=record.config_versions,
            answers=answers,
            recommendations=recommendations,
            rooms=rooms,
            implementation_choices=choices,
            budget=budget,
            documents=documents,
            evidence_facts=facts,
            function_recommendations=function_recommendations,
            function_choices=function_choices,
            technical_calculation=technical_calculation,
            raw_payload=record.raw_payload,
        )


def legacy_record_exists(session: Session, legacy_record_id: str) -> bool:
    return session.get(LegacyImportRecord, legacy_record_id) is not None


def mark_legacy_imported(
    session: Session,
    *,
    legacy_record_id: str,
    source_path: str,
    source_sha256: str,
) -> None:
    session.add(
        LegacyImportRecord(
            legacy_record_id=legacy_record_id,
            source_path=source_path,
            source_sha256=source_sha256,
            imported_at=utc_now(),
        )
    )
