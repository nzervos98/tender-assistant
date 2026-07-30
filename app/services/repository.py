from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.orm import Session

from app.models import Tender, TenderChange, TenderScore
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

    monitored_fields = {
        'title': 'metadata',
        'final_submission_date': 'deadline',
        'total_cost_without_vat': 'amount',
        'estimated_total_cost': 'amount',
        'contract_value': 'amount',
        'payment_amount': 'amount',
        'cancelled': 'cancellation',
        'cancellation_date': 'cancellation',
        'cancellation_reason': 'cancellation',
        'cancellation_ada': 'cancellation',
        'is_modified': 'modification',
        'contractor_name': 'contractor',
        'contractor_vat_number': 'contractor',
    }

    for key, value in data.items():
        if hasattr(tender, key) and value is not None:
            old_value = getattr(tender, key, None)
            if not created and key in monitored_fields and old_value != value:
                def serialize(item: Any) -> str | None:
                    if item is None:
                        return None
                    if hasattr(item, 'isoformat'):
                        return item.isoformat()
                    return str(item)

                db.add(TenderChange(
                    tender=tender,
                    ingest_run_id=ingest_run_id,
                    change_type=monitored_fields[key],
                    field_name=key,
                    old_value=serialize(old_value),
                    new_value=serialize(value),
                ))
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
