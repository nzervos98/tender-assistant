from __future__ import annotations

import argparse
from datetime import timedelta

from sqlalchemy import func, text

from app.db import SessionLocal, init_db
from app.models import SystemEvent, Tender, TenderScore
from app.services.activity import log_event
from app.services.tender_cleanup import delete_unreferenced_tenders, unreferenced_tender_query
from app.services.timezone import now_utc


def _delete_orphans(db, dry_run: bool, min_age_days: int = 30) -> int:
    cutoff = now_utc() - timedelta(days=max(1, int(min_age_days)))
    candidate_ids = [
        row[0]
        for row in (
            db.query(Tender)
            .outerjoin(TenderScore, TenderScore.tender_id == Tender.id)
            .filter(TenderScore.id.is_(None), func.coalesce(Tender.updated_at, Tender.created_at) < cutoff)
            .with_entities(Tender.id)
            .all()
        )
    ]
    if dry_run:
        return unreferenced_tender_query(db, candidate_ids).count()
    return delete_unreferenced_tenders(db, candidate_ids)


def run_cleanup(
    *,
    clear_pdf_text: bool = True,
    delete_scores_below: float | None = None,
    delete_orphan_tenders: bool = True,
    clear_events_older_than_days: int | None = None,
    vacuum: bool = True,
    dry_run: bool = False,
    orphan_min_age_days: int = 30,
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
        'orphan_min_age_days': max(1, int(orphan_min_age_days)),
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
            cutoff = now_utc() - timedelta(days=max(0, int(clear_events_older_than_days)))
            event_q = db.query(SystemEvent).filter(SystemEvent.created_at < cutoff)
            stats['deleted_events'] = event_q.count()
            if not dry_run:
                event_q.delete(synchronize_session=False)

        if delete_orphan_tenders:
            stats['deleted_orphan_tenders'] = _delete_orphans(db, dry_run, orphan_min_age_days)

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
            with db.get_bind().connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
                if db.get_bind().dialect.name == 'sqlite':
                    conn.execute(text('VACUUM'))
                    conn.execute(text('ANALYZE'))
                else:
                    conn.execute(text('VACUUM ANALYZE'))

        return stats
    finally:
        db.close()


def run_scheduled_orphan_cleanup(min_age_days: int = 30) -> dict[str, int | float | bool | None]:
    """Scheduled safe sweep: only old, unscored, unlinked tenders; never PDFs or scores."""
    return run_cleanup(
        clear_pdf_text=False,
        delete_scores_below=None,
        delete_orphan_tenders=True,
        clear_events_older_than_days=None,
        vacuum=False,
        dry_run=False,
        orphan_min_age_days=min_age_days,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='Καθάρισμα βάσης για το Tender Assistant.')
    parser.add_argument('--dry-run', action='store_true', help='Δείχνει τι θα γινόταν χωρίς να αλλάξει τη βάση.')
    parser.add_argument('--keep-pdf-text', action='store_true', help='Δεν καθαρίζει το αποθηκευμένο extracted PDF text.')
    parser.add_argument('--delete-scores-below', type=float, default=None, help='Προαιρετικά διαγράφει scores κάτω από το όριο, π.χ. 40.')
    parser.add_argument('--keep-orphans', action='store_true', help='Δεν διαγράφει tenders που δεν έχουν πλέον scores.')
    parser.add_argument('--clear-events-older-than-days', type=int, default=None, help='Προαιρετικά καθαρίζει παλιά system events.')
    parser.add_argument('--no-vacuum', action='store_true', help='Δεν τρέχει VACUUM ANALYZE στο τέλος.')
    parser.add_argument('--orphan-min-age-days', type=int, default=30, help='Ελάχιστη ηλικία (από τελευταία ενημέρωση) για ορφανές πράξεις.')
    args = parser.parse_args()

    stats = run_cleanup(
        clear_pdf_text=not args.keep_pdf_text,
        delete_scores_below=args.delete_scores_below,
        delete_orphan_tenders=not args.keep_orphans,
        clear_events_older_than_days=args.clear_events_older_than_days,
        vacuum=not args.no_vacuum,
        dry_run=args.dry_run,
        orphan_min_age_days=args.orphan_min_age_days,
    )
    print(stats)


if __name__ == '__main__':
    main()
