from __future__ import annotations

import argparse
import logging

from app.db import init_db, session_scope
from app.services.activity import log_event
from app.services.rescore import rescore_existing_tenders

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)


def run_rescore(profile_id: int | None = None) -> dict[str, int]:
    init_db()
    with session_scope() as db:
        result = rescore_existing_tenders(db, profile_id=profile_id)
        log_event(
            db,
            event_type='rescore',
            title='Ολοκληρώθηκε ανανέωση σχετικότητας',
            message=(
                f"Ενημερώθηκαν {result['scores_updated']} αξιολογήσεις "
                f"για {result['tenders']} αποθηκευμένες πράξεις και {result['profiles']} προφίλ."
            ),
            payload={'profile_id': profile_id, **result},
        )
        logger.info('Rescore finished: %s', result)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description='Recalculate relevance scores for existing tenders without fetching new data.')
    parser.add_argument('--profile-id', type=int, default=None, help='Optional profile id to rescore only one profile.')
    args = parser.parse_args()
    result = run_rescore(profile_id=args.profile_id)
    print(result)


if __name__ == '__main__':
    main()
