from __future__ import annotations

import logging
from typing import Any

from app.db import init_db, session_scope
from app.models import Tender
from app.services.activity import log_event
from app.services.khmdhs_client import parse_dt

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)


def _first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value:
            return value
    return None


def _fix_tender_dates(tender: Tender) -> bool:
    if not str(tender.source or '').startswith('khmdhs_'):
        return False
    raw = tender.raw if isinstance(tender.raw, dict) else {}
    if not raw:
        return False

    changed = False
    mappings = {
        'submission_date': raw.get('submissionDate'),
        'final_submission_date': raw.get('finalSubmissionDate'),
        'published_date': _first_value(raw, 'publishedDate', 'signedDate', 'lastUpdateDate'),
    }
    for attr, raw_value in mappings.items():
        fixed = parse_dt(raw_value)
        if fixed is not None and getattr(tender, attr) != fixed:
            setattr(tender, attr, fixed)
            changed = True
    return changed


def main() -> None:
    init_db()
    with session_scope() as db:
        tenders = db.query(Tender).filter(Tender.source.like('khmdhs_%')).all()
        fixed = 0
        for tender in tenders:
            if _fix_tender_dates(tender):
                fixed += 1
        log_event(
            db,
            event_type='maintenance',
            title='Διορθώθηκαν ζώνες ώρας ΚΗΜΔΗΣ',
            message=f'Ελέγχθηκαν {len(tenders)} πράξεις και ενημερώθηκαν ημερομηνίες σε {fixed}.',
            payload={'checked': len(tenders), 'updated': fixed},
        )
        logger.info('Timezone normalization finished: checked=%s updated=%s', len(tenders), fixed)


if __name__ == '__main__':
    main()
