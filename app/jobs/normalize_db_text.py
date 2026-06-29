from __future__ import annotations

import argparse
import logging

from sqlalchemy.orm import joinedload

from app.db import init_db, session_scope
from app.models import ClientProfile, Tender, TenderScore
from app.services.activity import log_event
from app.services.scoring import rule_score_tender
from app.services.text_normalizer import normalize_greek_text, normalize_text_tree

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)


def normalize_existing_tenders(rescore: bool = True) -> dict[str, int]:
    init_db()
    changed = 0
    rescored = 0
    with session_scope() as db:
        tenders = db.query(Tender).all()
        profiles = db.query(ClientProfile).filter(ClientProfile.is_active.is_(True)).all()
        for tender in tenders:
            before = (
                tender.title,
                tender.organization_name,
                tender.contract_type,
                tender.procedure_type,
                tender.cpv_descriptions,
                tender.raw,
                tender.pdf_text,
            )
            tender.title = normalize_greek_text(tender.title) or tender.title
            tender.organization_name = normalize_greek_text(tender.organization_name) if tender.organization_name else None
            tender.contract_type = normalize_greek_text(tender.contract_type) if tender.contract_type else None
            tender.procedure_type = normalize_greek_text(tender.procedure_type) if tender.procedure_type else None
            tender.cpv_descriptions = normalize_text_tree(tender.cpv_descriptions or {})
            tender.raw = normalize_text_tree(tender.raw or {})
            tender.pdf_text = normalize_greek_text(tender.pdf_text) if tender.pdf_text else None
            after = (
                tender.title,
                tender.organization_name,
                tender.contract_type,
                tender.procedure_type,
                tender.cpv_descriptions,
                tender.raw,
                tender.pdf_text,
            )
            if before != after:
                changed += 1
        db.flush()

        if rescore:
            scores = (
                db.query(TenderScore)
                .options(joinedload(TenderScore.tender), joinedload(TenderScore.profile))
                .all()
            )
            for score in scores:
                if not score.tender or not score.profile:
                    continue
                rule = rule_score_tender(score.tender, score.profile)
                score.rule_score = rule.score
                score.score = rule.score
                score.matched_cpv = rule.matched_cpv
                score.matched_keywords = rule.matched_keywords
                score.missing_requirements = rule.missing_requirements
                score.reasons = rule.reasons[:20]
                # Δεν πειράζουμε user_status/user_notes.
                score.recommended_action = rule.recommended_action
                rescored += 1
            db.flush()

        result = {'normalized_tenders': changed, 'rescored': rescored}
        log_event(
            db,
            event_type='maintenance',
            title='Διορθώθηκε ελληνικό κείμενο στη βάση',
            message=f"Διορθώθηκαν {changed} πράξεις και αναβαθμολογήθηκαν {rescored} εγγραφές.",
            payload=result,
        )
        logger.info('Greek text normalization finished: %s', result)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description='Fix Greek CP737 mojibake in existing database rows.')
    parser.add_argument('--no-rescore', action='store_true', help='Μόνο διόρθωση κειμένου, χωρίς αναβαθμολόγηση.')
    args = parser.parse_args()
    print(normalize_existing_tenders(rescore=not args.no_rescore))


if __name__ == '__main__':
    main()
