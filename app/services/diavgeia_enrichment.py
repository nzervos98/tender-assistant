from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.models import DiavgeiaDecision, Tender
from app.services.diavgeia_client import (
    DiavgeiaClient,
    DiavgeiaClientError,
    extract_decisions,
    extract_total,
    hydrate_decisions,
    normalize_decision,
)
from app.services.text_normalizer import normalize_text_tree


@dataclass(frozen=True)
class DiavgeiaEnrichmentResult:
    reference: str
    total: int
    stored: int
    created: int
    updated: int
    decisions: list[DiavgeiaDecision]


def diavgeia_reference_for_tender(tender: Tender) -> str:
    """Return the best ΚΗΜΔΗΣ reference to use as a Διαύγεια search term."""
    for value in (tender.reference_number, tender.source_reference):
        text = (value or '').strip()
        if text:
            return text
    return ''


def _decision_payload(summary_raw: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(summary_raw, Mapping):
        return {}
    normalized = normalize_text_tree(dict(summary_raw))
    return normalized if isinstance(normalized, dict) else {}


def upsert_diavgeia_decision(
    db: Session,
    *,
    tender: Tender,
    adam_reference: str,
    decision_payload: Mapping[str, Any],
) -> tuple[DiavgeiaDecision | None, bool]:
    """Store one Διαύγεια decision related to a tender, deduped by tender + ΑΔΑ."""
    summary = normalize_decision(decision_payload)
    if not summary.ada:
        return None, False

    row = (
        db.query(DiavgeiaDecision)
        .filter(DiavgeiaDecision.tender_id == tender.id, DiavgeiaDecision.ada == summary.ada)
        .one_or_none()
    )
    created = row is None
    if created:
        row = DiavgeiaDecision(tender_id=tender.id, ada=summary.ada)
        db.add(row)

    row.adam_reference = adam_reference
    row.subject = summary.subject
    row.organization_name = summary.organization or None
    row.organization_uid = summary.organization_uid or None
    row.decision_type = summary.decision_type or None
    row.decision_type_uid = summary.decision_type_uid or None
    row.issue_date = summary.issue_date or None
    row.submission_timestamp = summary.submission_timestamp or None
    row.status = summary.status or None
    row.url = summary.url or None
    row.api_url = summary.api_url or None
    row.raw = _decision_payload(summary.raw)
    db.flush()
    return row, created


def find_and_store_related_diavgeia_decisions(
    db: Session,
    tender: Tender,
    *,
    client: DiavgeiaClient | None = None,
    size: int = 10,
    hydrate: bool = True,
) -> DiavgeiaEnrichmentResult:
    """Search Διαύγεια for a tender's ΑΔΑΜ and persist related decisions.

    This is intentionally read-only from the user's point of view: it does not
    create new tender opportunities and it does not affect scoring. It only adds
    supporting context to the tender detail page.
    """
    reference = diavgeia_reference_for_tender(tender)
    if not reference:
        return DiavgeiaEnrichmentResult(reference='', total=0, stored=0, created=0, updated=0, decisions=[])

    api = client or DiavgeiaClient()
    payload = api.search_by_adam(reference, size=size)
    decisions = extract_decisions(payload)
    if hydrate and decisions:
        decisions = hydrate_decisions(api, decisions, max_items=size)

    rows: list[DiavgeiaDecision] = []
    created_count = 0
    for decision in decisions:
        row, created = upsert_diavgeia_decision(db, tender=tender, adam_reference=reference, decision_payload=decision)
        if row is None:
            continue
        rows.append(row)
        if created:
            created_count += 1

    stored = len(rows)
    return DiavgeiaEnrichmentResult(
        reference=reference,
        total=extract_total(payload, fallback=stored),
        stored=stored,
        created=created_count,
        updated=max(0, stored - created_count),
        decisions=rows,
    )


__all__ = [
    'DiavgeiaClientError',
    'DiavgeiaEnrichmentResult',
    'diavgeia_reference_for_tender',
    'find_and_store_related_diavgeia_decisions',
    'upsert_diavgeia_decision',
]
