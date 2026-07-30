from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import Tender
from app.services.khmdhs_client import KhmdhsClient
from app.services.repository import upsert_tender


RESOURCE_BY_SOURCE = {
    'khmdhs_notice': 'notice',
    'khmdhs_request': 'request',
    'khmdhs_auction': 'auction',
    'khmdhs_contract': 'contract',
    'khmdhs_payment': 'payment',
}


def backfill_market_fields(db: Session) -> dict[str, int]:
    client = KhmdhsClient()
    run_id = f'backfill-{uuid4().hex}'
    checked = 0
    updated = 0
    rows = db.query(Tender).filter(Tender.source.in_(tuple(RESOURCE_BY_SOURCE))).order_by(Tender.id.asc()).all()
    for tender in rows:
        raw = tender.raw or {}
        if not isinstance(raw, dict) or not raw:
            continue
        checked += 1
        normalized = client.normalize_record(RESOURCE_BY_SOURCE[tender.source], raw)
        before = (
            tender.contractor_name,
            tender.contractor_vat_number,
            tender.aaht,
            tender.public_funding_ref_num,
            tender.estimated_total_cost,
            tender.contract_value,
            tender.payment_amount,
        )
        upsert_tender(db, normalized, ingest_run_id=run_id)
        after = (
            tender.contractor_name,
            tender.contractor_vat_number,
            tender.aaht,
            tender.public_funding_ref_num,
            tender.estimated_total_cost,
            tender.contract_value,
            tender.payment_amount,
        )
        if before != after:
            updated += 1
    db.flush()
    return {'checked': checked, 'updated': updated}
