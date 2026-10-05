from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models import Tender, TenderLink


SIGNAL_STAGE_LABELS = {
    'initial': 'Νέο αίτημα',
    'approved': 'Εγκεκριμένο αίτημα',
    'converted': 'Έγινε διακήρυξη',
    'cancelled': 'Ματαιώθηκε',
}

SIGNAL_STAGE_ORDER = {
    'initial': 0,
    'approved': 1,
    'converted': 2,
    'cancelled': 3,
}


def strongest_signal_stage(*stages: str | None) -> str:
    valid = [stage for stage in stages if stage in SIGNAL_STAGE_ORDER]
    return max(valid, key=lambda stage: SIGNAL_STAGE_ORDER[stage]) if valid else 'initial'


def _references(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif value:
        values = [value]
    else:
        values = []
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def linked_notice_references(raw: dict[str, Any] | None) -> list[str]:
    raw = raw if isinstance(raw, dict) else {}
    refs: list[str] = []
    for key in ('noticeRefNo', 'notices', 'relatedNoticeADAM'):
        for ref in _references(raw.get(key)):
            if 'PROC' in ref.upper() and ref not in refs:
                refs.append(ref)
    return refs


def signal_stage_from_raw(raw: dict[str, Any] | None, *, fallback: str = 'initial') -> str:
    raw = raw if isinstance(raw, dict) else {}
    if bool(raw.get('cancelled')):
        return 'cancelled'
    if linked_notice_references(raw):
        return 'converted'
    explicit = str(raw.get('_signal_stage') or '').strip()
    if explicit in SIGNAL_STAGE_LABELS:
        return explicit
    if bool(raw.get('approved')) or raw.get('previousRequestReferenceNumber'):
        return 'approved'
    return fallback if fallback in SIGNAL_STAGE_LABELS else 'initial'


def merge_signal_record(existing: dict[str, Any] | None, incoming: dict[str, Any], stage: str) -> dict[str, Any]:
    """Keep the strongest representation when one REQ appears in two streams."""
    candidate = dict(incoming)
    candidate['_signal_stage'] = signal_stage_from_raw(candidate, fallback=stage)
    if existing is None:
        return candidate
    current_stage = signal_stage_from_raw(existing)
    candidate_stage = signal_stage_from_raw(candidate)
    if SIGNAL_STAGE_ORDER.get(candidate_stage, 0) >= SIGNAL_STAGE_ORDER.get(current_stage, 0):
        return candidate
    return existing


def sync_signal_notice_links(db: Session, signal: Tender) -> list[TenderLink]:
    """Persist every REQ -> PROC edge and resolve it when the notice exists."""
    if signal.source != 'khmdhs_request':
        return []
    refs = linked_notice_references(signal.raw)
    links: list[TenderLink] = []
    now = datetime.now(timezone.utc)
    for reference in refs:
        notice = (
            db.query(Tender)
            .filter(Tender.source == 'khmdhs_notice', Tender.reference_number == reference)
            .one_or_none()
        )
        link = (
            db.query(TenderLink)
            .filter(
                TenderLink.source_tender_id == signal.id,
                TenderLink.relation_type == 'request_to_notice',
                TenderLink.related_reference == reference,
            )
            .one_or_none()
        )
        if link is None:
            link = TenderLink(
                source_tender_id=signal.id,
                related_tender_id=notice.id if notice else None,
                relation_type='request_to_notice',
                related_reference=reference,
            )
            db.add(link)
        else:
            link.related_tender_id = notice.id if notice else link.related_tender_id
            link.last_seen_at = now
        links.append(link)
    if refs:
        signal.signal_stage = 'converted'
    elif signal.cancelled:
        signal.signal_stage = 'cancelled'
    elif not signal.signal_stage:
        signal.signal_stage = signal_stage_from_raw(signal.raw)
    signal.signal_last_checked_at = now
    db.flush()
    return links


def resolve_pending_notice_links(db: Session, notices: Iterable[Tender] = ()) -> int:
    by_reference = {
        str(tender.reference_number): tender
        for tender in notices
        if tender.source == 'khmdhs_notice' and tender.reference_number
    }
    pending = db.query(TenderLink).filter(TenderLink.related_tender_id.is_(None)).all()
    resolved = 0
    for link in pending:
        notice = by_reference.get(link.related_reference)
        if notice is None:
            notice = (
                db.query(Tender)
                .filter(Tender.source == 'khmdhs_notice', Tender.reference_number == link.related_reference)
                .one_or_none()
            )
        if notice is not None:
            link.related_tender_id = notice.id
            resolved += 1
    db.flush()
    return resolved
