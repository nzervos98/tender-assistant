from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, or_

from app.models import Tender
from app.services.timezone import now_utc


def actionable_tender_clause(at: datetime | None = None):
    """SQL clause for tenders that can still be acted on."""
    at = at or now_utc()
    return and_(
        Tender.cancelled.is_(False),
        or_(Tender.final_submission_date.is_(None), Tender.final_submission_date >= at),
    )


def expired_tender_clause(at: datetime | None = None):
    """Expired and cancelled are separate lifecycle states."""
    at = at or now_utc()
    return and_(Tender.cancelled.is_(False), Tender.final_submission_date < at)


def is_tender_actionable(tender: Tender, at: datetime | None = None) -> bool:
    if tender.cancelled:
        return False
    if tender.final_submission_date is None:
        return True
    at = at or now_utc()
    deadline = tender.final_submission_date
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return deadline >= at


def tender_lifecycle_status(tender: Tender, at: datetime | None = None) -> str:
    if tender.cancelled:
        return 'cancelled'
    if tender.final_submission_date is None:
        return 'unknown_deadline'
    return 'active' if is_tender_actionable(tender, at) else 'expired'


def tender_lifecycle_label(tender: Tender, at: datetime | None = None) -> str:
    return {
        'active': 'Ενεργό',
        'unknown_deadline': 'Άγνωστη προθεσμία',
        'expired': 'Έληξε',
        'cancelled': 'Ακυρώθηκε / ματαιώθηκε',
    }[tender_lifecycle_status(tender, at)]
