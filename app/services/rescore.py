from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ClientProfile, Tender, TenderScore
from app.services.repository import upsert_score
from app.services.scoring import cpv_match_key, rule_score_tender
from app.services.opportunity_status import is_tender_actionable


OPPORTUNITY_SOURCES = ('khmdhs_notice', 'khmdhs_request')


def rescore_existing_tenders(
    db: Session,
    profile_id: Optional[int] = None,
    active_profiles_only: bool = True,
) -> dict[str, int]:
    """Recalculate relevance scores for tenders already stored in PostgreSQL.

    This intentionally does not call KIMDIS/Diavgeia and does not download PDFs.
    It only reads the current tenders + profiles and rewrites TenderScore rows.
    Existing workflow status and user notes remain untouched because upsert_score
    only updates the scoring fields below.
    """
    profile_query = db.query(ClientProfile)
    if profile_id:
        profile_query = profile_query.filter(ClientProfile.id == profile_id)
    elif active_profiles_only:
        profile_query = profile_query.filter(ClientProfile.is_active.is_(True))
    profiles = profile_query.order_by(ClientProfile.name.asc()).all()

    # Only opportunity sources are rescored. Historical rows that may remain from
    # older installations must never turn into opportunities.
    tenders = (
        db.query(Tender)
        .filter(Tender.source.in_(OPPORTUNITY_SOURCES))
        .order_by(Tender.id.asc())
        .all()
    )
    threshold = get_settings().match_threshold
    updated = 0
    removed = 0
    matches = 0
    high = 0

    for tender in tenders:
        for profile in profiles:
            rule = rule_score_tender(tender, profile)
            substantive_match = bool(rule.matched_cpv)
            existing = (
                db.query(TenderScore)
                .filter(TenderScore.tender_id == tender.id, TenderScore.profile_id == profile.id)
                .one_or_none()
            )
            if not substantive_match:
                # Remove automatic rows that no longer belong to this profile. Keep
                # explicit user work (saved/reviewing/notes) as historical intent.
                if existing is None:
                    continue
                if existing.user_status == 'new' and not existing.user_notes:
                    db.delete(existing)
                    removed += 1
                    continue
            upsert_score(
                db,
                tender_id=tender.id,
                profile_id=profile.id,
                data={
                    'score': rule.score,
                    'rule_score': rule.score,
                    'matched_cpv': rule.matched_cpv,
                    'cpv_match_type': cpv_match_key(tender, profile),
                    'matched_keywords': rule.matched_keywords,
                    'missing_requirements': rule.missing_requirements,
                    'reasons': rule.reasons[:20],
                    'recommended_action': rule.recommended_action,
                },
            )
            updated += 1
            if rule.score >= threshold and is_tender_actionable(tender):
                matches += 1
            if rule.score >= 75 and is_tender_actionable(tender):
                high += 1

    db.flush()
    return {
        'profiles': len(profiles),
        'tenders': len(tenders),
        'scores_updated': updated,
        'scores_removed': removed,
        'matches': matches,
        'high': high,
    }
