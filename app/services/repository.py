from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.orm import Session

from app.models import Tender, TenderScore
from app.services.text_normalizer import normalize_text_tree


def upsert_tender(db: Session, data: Dict[str, Any], *, ingest_run_id: str | None = None) -> Tender:
    data = normalize_text_tree(data)
    tender = (
        db.query(Tender)
        .filter(Tender.source == data['source'], Tender.source_reference == str(data['source_reference']))
        .one_or_none()
    )
    created = tender is None
    if created:
        tender = Tender(source=data['source'], source_reference=str(data['source_reference']), title=data.get('title') or '')
        db.add(tender)

    for key, value in data.items():
        if hasattr(tender, key) and value is not None:
            setattr(tender, key, value)

    if ingest_run_id:
        tender.last_seen_ingest_run_id = ingest_run_id
        if created:
            tender.first_seen_ingest_run_id = ingest_run_id
            tender.is_new_in_latest_ingest = True
        else:
            # Existing ΑΔΑΜ returned again by KIMDIS is an update/duplicate, not "new" for the latest run.
            tender.is_new_in_latest_ingest = False

    db.flush()
    return tender


def upsert_score(
    db: Session,
    tender_id: int,
    profile_id: int,
    data: Dict[str, Any],
    *,
    ingest_run_id: str | None = None,
) -> TenderScore:
    score = (
        db.query(TenderScore)
        .filter(TenderScore.tender_id == tender_id, TenderScore.profile_id == profile_id)
        .one_or_none()
    )
    created = score is None
    if created:
        score = TenderScore(tender_id=tender_id, profile_id=profile_id)
        db.add(score)

    for key, value in data.items():
        if hasattr(score, key):
            setattr(score, key, value)

    if ingest_run_id:
        score.last_seen_ingest_run_id = ingest_run_id
        if created:
            score.first_seen_ingest_run_id = ingest_run_id
            score.is_new_in_latest_ingest = True
        else:
            # Existing tender-score pair returned again is an update, not new for this profile.
            score.is_new_in_latest_ingest = False

    db.flush()
    return score
