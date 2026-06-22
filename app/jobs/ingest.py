from __future__ import annotations

import argparse
import logging
from datetime import timedelta
from uuid import uuid4
from typing import Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import init_db, session_scope
from app.models import ClientProfile, Tender, TenderScore
from app.services.activity import log_event
from app.services.ai import AIService
from app.services.diavgeia_rss import fetch_rss_entries
from app.services.emailer import send_digest
from app.services.khmdhs_client import KhmdhsClient
from app.services.pdf import fetch_and_extract_pdf_text
from app.services.profiles import collect_cpv_codes
from app.services.repository import upsert_score, upsert_tender
from app.services.scoring import blend_scores, rule_score_tender
from app.services.timezone import today_local

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)


def _date_range(days_back: int) -> tuple[str, str]:
    today = today_local()
    start = today - timedelta(days=max(1, days_back))
    return start.isoformat(), today.isoformat()


def score_and_store(db: Session, tender: Tender, profile: ClientProfile, ai: AIService, ingest_run_id: str | None = None) -> TenderScore:
    rule = rule_score_tender(tender, profile)
    settings = get_settings()

    # Από v0.4.7 το ingest είναι metadata-first: δεν κατεβάζουμε PDF μαζικά.
    # Το PDF μένει ως επίσημο attachment_url και αναλύεται on-demand από τη σελίδα λεπτομέρειας
    # ή αν ενεργοποιηθεί ρητά το AUTO_FETCH_PDF_TEXT=true στο .env.
    if settings.auto_fetch_pdf_text and tender.attachment_url and not tender.pdf_text and rule.score >= settings.fetch_pdf_for_score_above:
        logger.info('Fetching PDF for %s', tender.reference_number or tender.source_reference)
        tender.pdf_text = fetch_and_extract_pdf_text(tender.attachment_url)
        db.flush()
        rule = rule_score_tender(tender, profile)

    ai_data = ai.score_tender(tender, profile) if (tender.pdf_text and rule.score >= settings.fetch_pdf_for_score_above) else None
    ai_score = float(ai_data['fit_score']) if ai_data and 'fit_score' in ai_data else None
    final_score = blend_scores(rule.score, ai_score)
    recommended_action = (ai_data or {}).get('recommended_action') or rule.recommended_action
    reasons = rule.reasons + [f'AI: {x}' for x in (ai_data or {}).get('reasons', [])]
    missing = list(dict.fromkeys(rule.missing_requirements + list((ai_data or {}).get('missing_requirements', []))))

    return upsert_score(
        db,
        tender_id=tender.id,
        profile_id=profile.id,
        data={
            'score': final_score,
            'rule_score': rule.score,
            'ai_score': ai_score,
            'matched_cpv': rule.matched_cpv,
            'matched_keywords': rule.matched_keywords,
            'missing_requirements': missing,
            'reasons': reasons[:20],
            'recommended_action': recommended_action,
        },
        ingest_run_id=ingest_run_id,
    )


def ingest_khmdhs(db: Session, profiles: Iterable[ClientProfile], days_back: int, ingest_run_id: str) -> Tuple[List[Tender], dict]:
    profiles = list(profiles)
    info: dict = {'source': 'khmdhs_notice', 'warnings': []}
    if not profiles:
        message = 'Δεν έγινε εισαγωγή ΚΗΜΔΗΣ: δεν υπάρχει ενεργό προφίλ.'
        logger.warning(message)
        log_event(
            db,
            event_type='ingest_skipped',
            title='Παράλειψη εισαγωγής ΚΗΜΔΗΣ',
            message='Δεν υπάρχει ενεργό προφίλ. Η ημερήσια εισαγωγή δεν θα φέρνει αποτελέσματα.',
            payload={'reason': 'no_active_profiles', 'days_back': days_back},
        )
        info['warnings'].append('no_active_profiles')
        return [], info
    profile_cpvs = collect_cpv_codes(profiles, expand_known_children=False)
    cpvs = collect_cpv_codes(profiles, expand_known_children=True)
    if not cpvs:
        message = 'Δεν έγινε εισαγωγή ΚΗΜΔΗΣ: δεν υπάρχουν CPV σε ενεργά προφίλ.'
        logger.warning(message)
        log_event(
            db,
            event_type='ingest_skipped',
            title='Παράλειψη εισαγωγής ΚΗΜΔΗΣ',
            message=message,
            payload={'reason': 'no_cpv_codes', 'days_back': days_back, 'active_profiles': len(profiles)},
        )
        info['warnings'].append('no_cpv_codes')
        return [], info
    date_from, date_to = _date_range(days_back)
    client = KhmdhsClient()
    expanded_children = max(0, len(cpvs) - len(profile_cpvs))
    logger.info(
        'Searching KIMDIS notices from %s to %s for %s CPV codes (%s selected, %s descendants)',
        date_from,
        date_to,
        len(cpvs),
        len(profile_cpvs),
        expanded_children,
    )
    raw_notices = client.search_notices(date_from=date_from, date_to=date_to, cpv_items=cpvs)
    info.update({
        'cpv_count': len(cpvs),
        'selected_cpv_count': len(profile_cpvs),
        'expanded_child_cpv_count': expanded_children,
        'pages_fetched': client.last_pages_fetched,
        'rate_limited': client.last_rate_limited,
        'hit_max_pages': client.last_hit_max_pages,
        'rate_limit_hits': client.last_rate_limit_hits,
    })

    if len(cpvs) >= 300:
        info['warnings'].append('broad_cpv_profile')
        log_event(
            db,
            event_type='ingest_warning',
            title='Πολύ ευρύ CPV προφίλ',
            message=(
                'Το ενεργό προφίλ επεκτάθηκε σε πολλούς CPV απογόνους. '
                'Το backfill πολλών ημερών μπορεί να αργήσει ή να χτυπήσει προσωρινό όριο ΚΗΜΔΗΣ. '
                'Για δοκιμή προτιμήστε μικρότερο --days ή πιο ειδικό CPV.'
            ),
            payload=info,
        )
    if client.last_rate_limited:
        info['warnings'].append('kimdis_rate_limit')
        log_event(
            db,
            event_type='ingest_warning',
            title='Προσωρινό όριο ΚΗΜΔΗΣ',
            message='Το ΚΗΜΔΗΣ επέστρεψε 429 Too Many Requests ακόμη και μετά από καθυστερημένες επαναλήψεις. Η εισαγωγή κράτησε όσα αποτελέσματα είχαν ήδη επιστραφεί.',
            payload=info,
        )
    if client.last_hit_max_pages:
        info['warnings'].append('kimdis_max_pages')
        log_event(
            db,
            event_type='ingest_warning',
            title='Η εισαγωγή έφτασε το όριο σελίδων',
            message='Το ΚΗΜΔΗΣ είχε περισσότερες σελίδες από το τρέχον KHMDHS_MAX_PAGES. Αυξήστε το όριο ή στενέψτε τα φίλτρα αν χρειάζεται πλήρες backfill.',
            payload=info,
        )
    tenders: List[Tender] = []
    for raw in raw_notices:
        normalized = client.normalize_notice(raw)
        tenders.append(upsert_tender(db, normalized, ingest_run_id=ingest_run_id))
    db.flush()
    logger.info('Stored/updated %s KIMDIS notices', len(tenders))
    return tenders, info


def ingest_diavgeia(db: Session, profiles: Iterable[ClientProfile], ingest_run_id: str) -> List[Tender]:
    feeds = []
    for profile in profiles:
        feeds.extend(profile.rss_feeds or [])
    feeds = list(dict.fromkeys(feeds))
    if not feeds:
        return []
    logger.info('Fetching %s Diavgeia RSS feeds', len(feeds))
    entries = fetch_rss_entries(feeds)
    tenders = [upsert_tender(db, entry, ingest_run_id=ingest_run_id) for entry in entries]
    db.flush()
    logger.info('Stored/updated %s Diavgeia RSS entries', len(tenders))
    return tenders


def run_ingest(days_back: Optional[int] = None, send_email: bool = True, profile_id: Optional[int] = None) -> dict:
    settings = get_settings()
    days_back = days_back or settings.ingest_days_back
    init_db()
    with session_scope() as db:
        if profile_id:
            profile = db.query(ClientProfile).filter(ClientProfile.id == profile_id).one_or_none()
            if profile is None:
                result = {
                    'tenders': 0,
                    'new_tenders': 0,
                    'scores': 0,
                    'matches': 0,
                    'warnings': ['profile_not_found'],
                    'profile_scope': 'selected_profile',
                    'profile_id': profile_id,
                    'khmdhs': {'source': 'khmdhs_notice', 'warnings': ['profile_not_found']},
                }
                log_event(
                    db,
                    event_type='ingest_skipped',
                    title='Παράλειψη εισαγωγής ΚΗΜΔΗΣ',
                    message='Δεν βρέθηκε το επιλεγμένο προφίλ για χειροκίνητη εισαγωγή.',
                    payload={'reason': 'profile_not_found', 'days_back': days_back, 'profile_id': profile_id},
                )
                return result
            profiles = [profile]
            profile_scope = 'selected_profile'
        else:
            profiles = db.query(ClientProfile).filter(ClientProfile.is_active.is_(True)).order_by(ClientProfile.name.asc()).all()
            profile_scope = 'all_active_profiles'
        profile_ids = [profile.id for profile in profiles]
        ai = AIService()
        ingest_run_id = uuid4().hex
        # "Νέο από εισαγωγή" is now profile-specific. Clear previous markers only for
        # the profile(s) covered by this run. The tender-level marker is kept for source-level audit,
        # but dashboard/reports use TenderScore.is_new_in_latest_ingest.
        if profile_ids:
            db.query(TenderScore).filter(TenderScore.profile_id.in_(profile_ids)).update(
                {TenderScore.is_new_in_latest_ingest: False},
                synchronize_session=False,
            )
        # Keep the legacy tender-level marker as the source-level latest run marker.
        db.query(Tender).update({Tender.is_new_in_latest_ingest: False}, synchronize_session=False)
        db.flush()
        tenders = []
        khmdhs_tenders, khmdhs_info = ingest_khmdhs(db, profiles, days_back, ingest_run_id)
        tenders.extend(khmdhs_tenders)
        if settings.enable_diavgeia_rss:
            tenders.extend(ingest_diavgeia(db, profiles, ingest_run_id))

        created_scores: List[TenderScore] = []
        for tender in tenders:
            for profile in profiles:
                created_scores.append(score_and_store(db, tender, profile, ai, ingest_run_id=ingest_run_id))
        db.flush()

        matches_query = (
            db.query(TenderScore)
            .options(joinedload(TenderScore.tender), joinedload(TenderScore.profile))
            .filter(TenderScore.score >= settings.match_threshold)
        )
        if profile_ids:
            matches_query = matches_query.filter(TenderScore.profile_id.in_(profile_ids))
        matches = matches_query.order_by(TenderScore.score.desc()).limit(30).all()
        if send_email:
            send_digest(matches, settings.digest_recipient_list)
        latest_new_count = sum(1 for score in created_scores if score.is_new_in_latest_ingest)
        profile_names = [profile.name for profile in profiles]
        result = {
            'tenders': len(tenders),
            'new_tenders': latest_new_count,
            'scores': len(created_scores),
            'matches': len(matches),
            'warnings': khmdhs_info.get('warnings', []),
            'khmdhs': khmdhs_info,
            'profile_scope': profile_scope,
            'profile_id': profile_id,
            'profile_names': profile_names,
        }
        scope_text = f"το προφίλ {profile_names[0]}" if profile_scope == 'selected_profile' and profile_names else 'όλα τα ενεργά προφίλ'
        log_event(
            db,
            event_type='ingest',
            title='Ολοκληρώθηκε εισαγωγή δεδομένων',
            message=f"Εισαγωγή για {scope_text}: ελέγχθηκαν/ενημερώθηκαν {len(tenders)} πράξεις, από τις οποίες {latest_new_count} ήταν νέες στην τελευταία εισαγωγή. Δημιουργήθηκαν {len(created_scores)} αξιολογήσεις και βρέθηκαν {len(matches)} matches.",
            payload={'days_back': days_back, 'ingest_run_id': ingest_run_id, **result},
        )
        logger.info('Ingest finished: tenders=%s scores=%s matches=%s', len(tenders), len(created_scores), len(matches))
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description='Import and score tenders from KIMDIS and Diavgeia RSS.')
    parser.add_argument('--days', type=int, default=None, help='How many days back to search in KIMDIS.')
    parser.add_argument('--no-email', action='store_true', help='Do not send email digest.')
    parser.add_argument('--profile-id', type=int, default=None, help='Run ingest only for this profile id. Scheduler/default run uses all active profiles.')
    args = parser.parse_args()
    result = run_ingest(days_back=args.days, send_email=not args.no_email, profile_id=args.profile_id)
    print(result)


if __name__ == '__main__':
    main()
