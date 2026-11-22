from __future__ import annotations

from typing import Any

from domain.enums import (
    EvidenceSource,
    EvidenceType,
    ReliabilityLevel,
    ReviewStatus,
)
from domain.models import DocumentRef, EvidenceFact
from services.object_intake import CompletenessReport, ObjectFactValue, StoredDocument
from services.plan_extraction import ExtractedCandidate, PlanExtraction


def document_ref_from_stored(document: StoredDocument) -> DocumentRef:
    return DocumentRef(
        original_name=document.original_name,
        mime_type=document.mime_type,
        stored_path=document.stored_path,
        sha256=document.sha256,
        size_bytes=document.size_bytes,
    )


def _candidate_source(candidate: ExtractedCandidate) -> EvidenceSource:
    if candidate.evidence_type == EvidenceType.VISUAL_INFERENCE.value:
        return EvidenceSource.DOCUMENT_INFERRED
    return EvidenceSource.DOCUMENT_EXPLICIT


def evidence_facts_from_extraction(
    extraction: PlanExtraction,
    document: DocumentRef,
) -> list[EvidenceFact]:
    return [
        EvidenceFact(
            field_id=candidate.field_id,
            entity_ref=candidate.entity_ref,
            value=candidate.value,
            source=_candidate_source(candidate),
            evidence_type=EvidenceType(candidate.evidence_type),
            source_reference=candidate.source_reference,
            document_id=document.document_id,
            page_number=candidate.page_number,
            reliability_level=ReliabilityLevel(candidate.reliability_level),
            review_status=ReviewStatus.PENDING,
        )
        for candidate in extraction.candidates
    ]


def reviewed_evidence_facts(
    facts: list[ObjectFactValue],
    extraction: PlanExtraction | None,
    document: DocumentRef | None,
) -> list[EvidenceFact]:
    candidates = {
        candidate.field_id: candidate
        for candidate in (extraction.candidates if extraction else [])
    }
    reviewed: list[EvidenceFact] = []
    for fact in facts:
        candidate = candidates.get(fact.field_id)
        resolved_value = (
            False if fact.state in {"not_present", "not_applicable"} else fact.value
        )
        if candidate is None or document is None:
            reviewed.append(
                EvidenceFact(
                    field_id=fact.field_id,
                    value=resolved_value,
                    source=EvidenceSource.MANUAL,
                    evidence_type=EvidenceType.USER_PROVIDED,
                    reliability_level=ReliabilityLevel.HIGH,
                    review_status=(
                        ReviewStatus.REJECTED
                        if fact.state in {"unknown", "rejected"}
                        else ReviewStatus.CONFIRMED
                    ),
                )
            )
            continue

        unchanged = candidate.value == resolved_value
        reviewed.append(
            EvidenceFact(
                field_id=fact.field_id,
                entity_ref=candidate.entity_ref,
                value=candidate.value,
                source=_candidate_source(candidate),
                evidence_type=EvidenceType(candidate.evidence_type),
                source_reference=candidate.source_reference,
                document_id=document.document_id,
                page_number=candidate.page_number,
                reliability_level=ReliabilityLevel(candidate.reliability_level),
                review_status=(
                    ReviewStatus.CONFIRMED if unchanged else ReviewStatus.CORRECTED
                ),
                corrected_value=None if unchanged else resolved_value,
            )
        )
    return reviewed


def build_intake_payload(
    *,
    phase: str,
    extraction: PlanExtraction | None,
    report: CompletenessReport | None = None,
    profile_answers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "intake": {
            "phase": phase,
            "extraction": (
                extraction.model_dump(mode="json") if extraction is not None else None
            ),
            "completeness": (
                report.model_dump(mode="json") if report is not None else None
            ),
            "profile_answers": dict(profile_answers or {}),
        }
    }


def extraction_from_payload(payload: dict[str, Any] | None) -> PlanExtraction | None:
    if not payload:
        return None
    raw = payload.get("intake", {}).get("extraction")
    return PlanExtraction.model_validate(raw) if raw else None
