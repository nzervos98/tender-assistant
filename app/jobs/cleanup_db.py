from __future__ import annotations

import argparse

from sqlalchemy import func, text

from app.db import SessionLocal, init_db
from app.models import ClientProfile, SystemEvent, Tender, TenderScore
from app.services.activity import log_event


def _delete_orphans(db, dry_run: bool) -> int:
    orphan_tenders = (
        db.query(Tender)
        .outerjoin(TenderScore, TenderScore.tender_id == Tender.id)
        .filter(TenderScore.id.is_(None))
        .all()
    )
    count = len(orphan_tenders)
    if not dry_run:
        for tender in orphan_tenders:
            db.delete(tender)
    return count


def run_cleanup(
    *,
    clear_pdf_text: bool = True,
    delete_scores_below: float | None = None,
    delete_orphan_tenders: bool = True,
    clear_events_older_than_days: int | None = None,
    vacuum: bool = True,
    dry_run: bool = False,
) -> dict[str, int | float | bool | None]:
    init_db()
    db = SessionLocal()
    stats: dict[str, int | float | bool | None] = {
        'dry_run': dry_run,
        'cleared_pdf_text': 0,
        'deleted_scores': 0,
        'deleted_orphan_tenders': 0,
        'deleted_events': 0,
        'delete_scores_below': delete_scores_below,
    }
    try:
        if clear_pdf_text:
            q = db.query(Tender).filter(Tender.pdf_text.isnot(None), func.length(Tender.pdf_text) > 0)
            stats['cleared_pdf_text'] = q.count()
            if not dry_run:
                q.update({Tender.pdf_text: None}, synchronize_session=False)

        if delete_scores_below is not None:
            score_q = db.query(TenderScore).filter(TenderScore.score < delete_scores_below)
            stats['deleted_scores'] = score_q.count()
            if not dry_run:
                score_q.delete(synchronize_session=False)

        if clear_events_older_than_days is not None:
            # PostgreSQL-friendly interval expression. For sqlite/local tests we simply skip if it fails.
            try:
                event_q = db.query(SystemEvent).filter(SystemEvent.created_at < func.now() - text(f"interval '{int(clear_events_older_than_days)} days'"))
                stats['deleted_events'] = event_q.count()
                if not dry_run:
                    event_q.delete(synchronize_session=False)
            except Exception:
                db.rollback()
                stats['deleted_events'] = 0

        if delete_orphan_tenders:
            stats['deleted_orphan_tenders'] = _delete_orphans(db, dry_run)

        if not dry_run:
            log_event(
                db,
                'db_cleanup',
                'Έγινε καθάρισμα βάσης',
                f"PDF text: {stats['cleared_pdf_text']}, scores: {stats['deleted_scores']}, orphan tenders: {stats['deleted_orphan_tenders']}",
                stats,
            )
            db.commit()

        if vacuum and not dry_run:
            # VACUUM cannot run inside a transaction in PostgreSQL.
            with db.get_bind().connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
                conn.execute(text('VACUUM ANALYZE'))

        return stats
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description='Καθάρισμα βάσης για το Tender AI Assistant.')
    parser.add_argument('--dry-run', action='store_true', help='Δείχνει τι θα γινόταν χωρίς να αλλάξει τη βάση.')
    parser.add_argument('--keep-pdf-text', action='store_true', help='Δεν καθαρίζει το αποθηκευμένο extracted PDF text.')
    parser.add_argument('--delete-scores-below', type=float, default=None, help='Προαιρετικά διαγράφει scores κάτω από το όριο, π.χ. 40.')
    parser.add_argument('--keep-orphans', action='store_true', help='Δεν διαγράφει tenders που δεν έχουν πλέον scores.')
    parser.add_argument('--clear-events-older-than-days', type=int, default=None, help='Προαιρετικά καθαρίζει παλιά system events.')
    parser.add_argument('--no-vacuum', action='store_true', help='Δεν τρέχει VACUUM ANALYZE στο τέλος.')
    args = parser.parse_args()

    stats = run_cleanup(
        clear_pdf_text=not args.keep_pdf_text,
        delete_scores_below=args.delete_scores_below,
        delete_orphan_tenders=not args.keep_orphans,
        clear_events_older_than_days=args.clear_events_older_than_days,
        vacuum=not args.no_vacuum,
        dry_run=args.dry_run,
    )
    print(stats)


if __name__ == '__main__':
    main()
