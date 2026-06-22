from __future__ import annotations

import logging

from app.db import init_db, session_scope
from app.jobs.ingest import score_and_store
from app.models import ClientProfile, Tender
from app.services.ai import AIService
from app.services.khmdhs_client import KhmdhsClient
from app.services.repository import upsert_tender
from app.services.text_normalizer import looks_like_replacement_garbage

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)

SOURCE_TO_RESOURCE = {
    'khmdhs_notice': 'notice',
    'khmdhs_request': 'request',
    'khmdhs_auction': 'auction',
    'khmdhs_contract': 'contract',
    'khmdhs_payment': 'payment',
}


def main() -> None:
    init_db()
    repaired = 0
    still_bad = 0
    skipped = 0
    client = KhmdhsClient()
    with session_scope() as db:
        tenders = db.query(Tender).all()
        profiles = db.query(ClientProfile).filter(ClientProfile.is_active == True).all()  # noqa: E712
        ai = AIService()
        for tender in tenders:
            if not (
                looks_like_replacement_garbage(tender.title)
                or looks_like_replacement_garbage(tender.organization_name)
                or looks_like_replacement_garbage(tender.pdf_text)
            ):
                continue
            resource = SOURCE_TO_RESOURCE.get(tender.source)
            if not resource or not tender.reference_number:
                skipped += 1
                continue
            try:
                records = client.search_resource(resource, {'referenceNumber': tender.reference_number}, max_pages=1)
            except Exception as exc:  # defensive repair command
                logger.warning('Could not refetch %s: %s', tender.reference_number, exc)
                skipped += 1
                continue
            if not records:
                skipped += 1
                continue
            normalized = client.normalize_record(resource, records[0])
            updated = upsert_tender(db, normalized)
            db.flush()
            for profile in profiles:
                score_and_store(db, updated, profile, ai)
            if looks_like_replacement_garbage(updated.title) or looks_like_replacement_garbage(updated.organization_name):
                still_bad += 1
            else:
                repaired += 1
        logger.info('Corrupted text repair finished: repaired=%s still_bad=%s skipped=%s', repaired, still_bad, skipped)
        print({'repaired': repaired, 'still_bad': still_bad, 'skipped': skipped})


if __name__ == '__main__':
    main()
