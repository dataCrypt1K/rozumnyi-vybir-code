"""Stage 2 baseline schema.

Revision ID: 20261008_0001
Revises: None
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261008_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assessments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_name", sa.String(length=160), nullable=True),
        sa.Column("derived_from_assessment_id", sa.String(length=64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workflow_stage", sa.String(length=40), nullable=False),
        sa.Column("property_type", sa.String(length=24), nullable=True),
        sa.Column("area_m2", sa.Float(), nullable=True),
        sa.Column("floor_count", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("model_name", sa.String(length=160), nullable=True),
        sa.Column("config_versions", sa.JSON(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["derived_from_assessment_id"], ["assessments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assessments_updated_at", "assessments", ["updated_at"])

    op.create_table(
        "assessment_answers",
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("question_id", sa.String(length=160), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assessment_id", "question_id"),
    )
    op.create_table(
        "recommendations",
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("system_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("suitability_percent", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rule_score", sa.Integer(), nullable=True),
        sa.Column("llm_score", sa.Integer(), nullable=True),
        sa.Column("decision_source", sa.String(length=80), nullable=False),
        sa.Column("evidence_question_ids", sa.JSON(), nullable=False),
        sa.Column("missing_data", sa.JSON(), nullable=False),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "(status = 'not_applicable' AND suitability_percent IS NULL) OR "
            "(status <> 'not_applicable' AND suitability_percent BETWEEN 0 AND 100)",
            name="ck_recommendations_suitability_applicability",
        ),
        sa.CheckConstraint(
            "rule_score IS NULL OR rule_score BETWEEN 0 AND 100",
            name="ck_recommendations_rule_score",
        ),
        sa.CheckConstraint(
            "llm_score IS NULL OR llm_score BETWEEN 0 AND 100",
            name="ck_recommendations_llm_score",
        ),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assessment_id", "system_id"),
        sa.UniqueConstraint(
            "assessment_id",
            "system_id",
            "status",
            name="uq_recommendations_identity_status",
        ),
    )
    op.create_table(
        "rooms",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("room_type", sa.String(length=50), nullable=False),
        sa.Column("location_kind", sa.String(length=24), nullable=False),
        sa.Column("floor", sa.Integer(), nullable=True),
        sa.Column("area_m2", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("reliability_level", sa.String(length=16), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assessment_id", "id", name="uq_rooms_assessment_id_id"
        ),
        sa.UniqueConstraint("assessment_id", "sort_order", name="uq_rooms_assessment_sort"),
    )
    op.create_index("ix_rooms_assessment_id", "rooms", ["assessment_id"])
    op.create_table(
        "implementation_choices",
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("system_id", sa.String(length=120), nullable=False),
        sa.Column("recommendation_status", sa.String(length=32), nullable=False),
        sa.Column("suitability_percent", sa.Integer(), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("phase", sa.String(length=24), nullable=True),
        sa.Column("decision_source", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "NOT (recommendation_status = 'not_applicable' AND selected)",
            name="ck_implementation_choices_not_applicable_unselected",
        ),
        sa.CheckConstraint(
            "recommendation_status <> 'not_applicable' OR phase IS NULL",
            name="ck_implementation_choices_not_applicable_no_phase",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id", "system_id", "recommendation_status"],
            [
                "recommendations.assessment_id",
                "recommendations.system_id",
                "recommendations.status",
            ],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("assessment_id", "system_id"),
    )
    op.create_table(
        "budget_preferences",
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=24), nullable=False),
        sa.Column("amount_eur", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assessment_id"),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=80), nullable=False),
        sa.Column("stored_path", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_assessment_id", "documents", ["assessment_id"])
    op.create_table(
        "evidence_facts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("field_id", sa.String(length=160), nullable=False),
        sa.Column("entity_ref", sa.String(length=160), nullable=True),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("evidence_type", sa.String(length=40), nullable=False),
        sa.Column("source_reference", sa.String(length=500), nullable=True),
        sa.Column("document_id", sa.String(length=64), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("reliability_level", sa.String(length=16), nullable=False),
        sa.Column("review_status", sa.String(length=24), nullable=False),
        sa.Column("corrected_value_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_facts_assessment_id", "evidence_facts", ["assessment_id"])
    op.create_table(
        "function_recommendations",
        sa.Column("safety_criticality", sa.Integer(), nullable=False),
        sa.Column("retrofit_penalty", sa.Integer(), nullable=False),
        sa.Column("dependency_role", sa.Integer(), nullable=False),
        sa.Column("daily_impact", sa.Integer(), nullable=False),
        sa.Column("phase_rules_version", sa.String(length=80), nullable=False),
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("function_id", sa.String(length=160), nullable=False),
        sa.Column("room_id", sa.String(length=64), nullable=False),
        sa.Column("system_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recommended_phase", sa.String(length=24), nullable=True),
        sa.Column("decision_source", sa.String(length=80), nullable=False),
        sa.Column("rule_codes", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "safety_criticality BETWEEN 0 AND 2",
            name="ck_function_recommendations_safety_criticality",
        ),
        sa.CheckConstraint(
            "retrofit_penalty BETWEEN 0 AND 2",
            name="ck_function_recommendations_retrofit_penalty",
        ),
        sa.CheckConstraint(
            "dependency_role BETWEEN 0 AND 2",
            name="ck_function_recommendations_dependency_role",
        ),
        sa.CheckConstraint(
            "daily_impact BETWEEN 0 AND 2",
            name="ck_function_recommendations_daily_impact",
        ),
        sa.CheckConstraint(
            "(status = 'not_applicable' AND recommended_phase IS NULL) OR "
            "(status <> 'not_applicable' AND recommended_phase IS NOT NULL)",
            name="ck_function_recommendations_phase_applicability",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id", "room_id"],
            ["rooms.assessment_id", "rooms.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id", "system_id"],
            ["recommendations.assessment_id", "recommendations.system_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("assessment_id", "function_id", "room_id"),
        sa.UniqueConstraint(
            "assessment_id",
            "function_id",
            "room_id",
            "system_id",
            name="uq_function_recommendations_identity_system",
        ),
    )
    op.create_index(
        "ix_function_recommendations_assessment_system",
        "function_recommendations",
        ["assessment_id", "system_id"],
    )
    op.create_table(
        "function_choices",
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("function_id", sa.String(length=160), nullable=False),
        sa.Column("room_id", sa.String(length=64), nullable=False),
        sa.Column("system_id", sa.String(length=120), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("selected_phase", sa.String(length=24), nullable=True),
        sa.Column("decision_source", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["assessment_id", "function_id", "room_id", "system_id"],
            [
                "function_recommendations.assessment_id",
                "function_recommendations.function_id",
                "function_recommendations.room_id",
                "function_recommendations.system_id",
            ],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(selected AND selected_phase IS NOT NULL) OR "
            "(NOT selected AND selected_phase IS NULL)",
            name="ck_function_choices_selected_phase",
        ),
        sa.PrimaryKeyConstraint("assessment_id", "function_id", "room_id"),
    )
    op.create_index(
        "ix_function_choices_assessment_selected",
        "function_choices",
        ["assessment_id", "selected"],
    )
    op.create_table(
        "legacy_imports",
        sa.Column("legacy_record_id", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.String(length=1024), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("legacy_record_id"),
    )
def downgrade() -> None:
    op.drop_table("legacy_imports")
    op.drop_index("ix_function_choices_assessment_selected", table_name="function_choices")
    op.drop_table("function_choices")
    op.drop_index(
        "ix_function_recommendations_assessment_system",
        table_name="function_recommendations",
    )
    op.drop_table("function_recommendations")
    op.drop_index("ix_evidence_facts_assessment_id", table_name="evidence_facts")
    op.drop_table("evidence_facts")
    op.drop_index("ix_documents_assessment_id", table_name="documents")
    op.drop_table("documents")
    op.drop_table("budget_preferences")
    op.drop_table("implementation_choices")
    op.drop_index("ix_rooms_assessment_id", table_name="rooms")
    op.drop_table("rooms")
    op.drop_table("recommendations")
    op.drop_table("assessment_answers")
    op.drop_index("ix_assessments_updated_at", table_name="assessments")
    op.drop_table("assessments")
