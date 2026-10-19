from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import exists
from sqlalchemy.orm import Session

from app.models import Tender, TenderLink, TenderScore


def unreferenced_tender_query(db: Session, candidate_ids: Iterable[int]):
    ids = sorted({int(value) for value in candidate_ids if value})
    if not ids:
        return db.query(Tender).filter(False)
    has_score = exists().where(TenderScore.tender_id == Tender.id)
    has_outgoing_link = exists().where(TenderLink.source_tender_id == Tender.id)
    has_incoming_link = exists().where(TenderLink.related_tender_id == Tender.id)
    return db.query(Tender).filter(
        Tender.id.in_(ids),
        ~has_score,
        ~has_outgoing_link,
        ~has_incoming_link,
    )


def delete_unreferenced_tenders(db: Session, candidate_ids: Iterable[int]) -> int:
    """Delete only candidate tenders with no profile score and no lifecycle edge."""
    orphaned = unreferenced_tender_query(db, candidate_ids).all()
    count = len(orphaned)
    for tender in orphaned:
        db.delete(tender)
    db.flush()
    return count
