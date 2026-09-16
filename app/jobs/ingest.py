from __future__ import annotations

import argparse
import logging
from datetime import timedelta
from uuid import uuid4
from typing import Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import init_db, session_scope
from app.models import ClientProfile, Tender, TenderChange, TenderScore
from app.services.activity import log_event
from app.services.api_sync import ApiCheckpointStore, incremental_date_range, sync_stream_key
from app.services.emailer import send_digest
from app.services.khmdhs_client import KhmdhsClient
from app.services.pdf import fetch_and_extract_pdf_text
from app.services.profiles import collect_cpv_codes
from app.services.repository import upsert_score, upsert_tender
from app.services.scoring import cpv_match_key, rule_score_tender
from app.services.opportunity_status import actionable_tender_clause, is_tender_actionable
from app.services.timezone import today_local

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)


def _date_range(days_back: int) -> tuple[str, str]:
    today = today_local()
    start = today - timedelta(days=max(1, days_back))
    return start.isoformat(), today.isoformat()


def _cpv_batches(codes: Iterable[str], batch_size: int) -> list[list[str]]:
    """Return deterministic, duplicate-free API payload batches."""
    unique = sorted({str(code).strip() for code in codes if str(code).strip()})
    size = max(1, int(batch_size))
    return [unique[index:index + size] for index in range(0, len(unique), size)]


def _client_metrics(client: KhmdhsClient, *, variant: str, batch_number: int, date_from: str, date_to: str) -> dict:
    return {
        'variant': variant,
        'batch_number': batch_number,
        'pages_fetched': client.last_pages_fetched,
        'rate_limited': client.last_rate_limited,
        'hit_max_pages': client.last_hit_max_pages,
        'rate_limit_hits': client.last_rate_limit_hits,
        'transient_error': client.last_transient_error,
        'transport_error_count': client.last_transport_error_count,
        'cache_hit': client.last_cache_hit,
        'resumed_from_page': client.last_resumed_from_page,
        'date_from': date_from,
        'date_to': date_to,
    }


def score_and_store(
    db: Session,
    tender: Tender,
    profile: ClientProfile,
    ingest_run_id: str | None = None,
    store_zero_score: bool = False,
) -> TenderScore | None:
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

    # A score row means that this tender belongs to this profile's CPV candidate set.
    # Budget/region alone can help rank a candidate, but must not attach every tender
    # in the shared database to every profile.
    substantive_match = bool(rule.matched_cpv)
    if not store_zero_score and not substantive_match:
        existing = (
            db.query(TenderScore)
            .filter(TenderScore.tender_id == tender.id, TenderScore.profile_id == profile.id)
            .one_or_none()
        )
        if existing is None:
            return None
        if existing.user_status == 'new' and not existing.user_notes:
            db.delete(existing)
            db.flush()
            return None

    return upsert_score(
        db,
        tender_id=tender.id,
        profile_id=profile.id,
        data={
            'score': rule.score,
            'rule_score': rule.score,
            'matched_cpv': rule.matched_cpv,
            'cpv_match_type': cpv_match_key(tender, profile),
            'matched_keywords': rule.matched_keywords,
            'missing_requirements': rule.missing_requirements,
            'reasons': rule.reasons[:20],
            'recommended_action': rule.recommended_action,
        },
        ingest_run_id=ingest_run_id,
    )


def _checkpoint_store(db: Session) -> ApiCheckpointStore | None:
    # Durable page checkpoints need a separate transaction. PostgreSQL supports
    # that while the ingest transaction is open; SQLite permits only one writer.
    if db.get_bind().dialect.name != 'postgresql':
        return None
    return ApiCheckpointStore(cache_hours=get_settings().khmdhs_query_cache_hours)


def ingest_khmdhs(
    db: Session,
    profiles: Iterable[ClientProfile],
    days_back: int,
    ingest_run_id: str,
    *,
    incremental: bool = False,
) -> Tuple[List[Tender], dict]:
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
    settings = get_settings()
    today = today_local()
    batches = _cpv_batches(cpvs, settings.khmdhs_cpv_batch_size)
    fixed_date_from, fixed_date_to = _date_range(days_back)
    client = KhmdhsClient()
    checkpoint_store = _checkpoint_store(db)
    expanded_children = max(0, len(cpvs) - len(profile_cpvs))
    logger.info(
        'Searching KIMDIS notices for %s CPV codes in %s batches (%s selected, %s descendants)',
        len(cpvs),
        len(batches),
        len(profile_cpvs),
        expanded_children,
    )
    raw_notices: list[dict] = []
    cancelled_notices: list[dict] = []
    query_metrics: list[dict] = []
    for batch_number, batch in enumerate(batches, start=1):
        for variant in ('registration', 'cancellation'):
            stream = sync_stream_key('notice', variant, batch)
            if incremental:
                date_from, date_to = incremental_date_range(
                    db,
                    stream,
                    today=today,
                    fallback_days=days_back,
                    overlap_days=settings.khmdhs_sync_overlap_days,
                )
            else:
                date_from, date_to = fixed_date_from, fixed_date_to
            logger.info(
                'KIMDIS %s batch %s/%s: %s CPV codes, %s to %s',
                variant,
                batch_number,
                len(batches),
                len(batch),
                date_from,
                date_to,
            )
            if variant == 'registration':
                rows = client.search_notices(
                    date_from=date_from,
                    date_to=date_to,
                    cpv_items=batch,
                    checkpoint_store=checkpoint_store,
                    stream_key=stream,
                )
                raw_notices.extend(rows)
            else:
                rows = client.search_cancelled_notices(
                    cancel_date_from=date_from,
                    cancel_date_to=date_to,
                    cpv_items=batch,
                    checkpoint_store=checkpoint_store,
                    stream_key=stream,
                )
                cancelled_notices.extend(rows)
            query_metrics.append(
                _client_metrics(
                    client,
                    variant=variant,
                    batch_number=batch_number,
                    date_from=date_from,
                    date_to=date_to,
                )
            )

    # A cancellation can affect an older notice whose original registration date
    # is outside the normal ingest window. Merge every batch/variant by ADAM.
    merged_by_reference: dict[str, dict] = {}
    for row in [*raw_notices, *cancelled_notices]:
        key = str(row.get('referenceNumber') or row.get('id') or '')
        if key:
            merged_by_reference[key] = row
    raw_notices = list(merged_by_reference.values())
    info.update({
        'cpv_count': len(cpvs),
        'selected_cpv_count': len(profile_cpvs),
        'expanded_child_cpv_count': expanded_children,
        'cpv_batch_size': settings.khmdhs_cpv_batch_size,
        'cpv_batch_count': len(batches),
        'query_count': len(query_metrics),
        'pages_fetched': sum(metric['pages_fetched'] for metric in query_metrics),
        'rate_limited': any(metric['rate_limited'] for metric in query_metrics),
        'hit_max_pages': any(metric['hit_max_pages'] for metric in query_metrics),
        'rate_limit_hits': sum(metric['rate_limit_hits'] for metric in query_metrics),
        'transport_error_count': sum(metric['transport_error_count'] for metric in query_metrics),
        'cancelled_records_checked': len(cancelled_notices),
        'cache_hits': sum(1 for metric in query_metrics if metric['cache_hit']),
        'resumed_queries': sum(1 for metric in query_metrics if metric['resumed_from_page']),
        'incomplete_queries': sum(
            1 for metric in query_metrics
            if metric['rate_limited'] or metric['hit_max_pages'] or metric['transient_error']
        ),
        'incremental': incremental,
    })
    info['continuation_required'] = bool(info['incomplete_queries'])

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
    if info['rate_limited']:
        info['warnings'].append('kimdis_rate_limit')
        log_event(
            db,
            event_type='ingest_warning',
            title='Προσωρινό όριο ΚΗΜΔΗΣ',
            message='Το ΚΗΜΔΗΣ επέστρεψε 429 Too Many Requests ακόμη και μετά από καθυστερημένες επαναλήψεις. Η εισαγωγή κράτησε όσα αποτελέσματα είχαν ήδη επιστραφεί.',
            payload=info,
        )
    if info['hit_max_pages']:
        info['warnings'].append('kimdis_max_pages')
        log_event(
            db,
            event_type='ingest_warning',
            title='Η εισαγωγή έφτασε το όριο σελίδων',
            message='Το ΚΗΜΔΗΣ είχε περισσότερες σελίδες από το τρέχον KHMDHS_MAX_PAGES. Αυξήστε το όριο ή στενέψτε τα φίλτρα αν χρειάζεται πλήρες backfill.',
            payload=info,
        )
    if any(metric['transient_error'] for metric in query_metrics):
        info['warnings'].append('kimdis_temporary_connection_error')
        log_event(
            db,
            event_type='ingest_warning',
            title='Προσωρινή καθυστέρηση ΚΗΜΔΗΣ',
            message='Ένα αίτημα του ΚΗΜΔΗΣ καθυστέρησε ή διακόπηκε. Η πρόοδος αποθηκεύτηκε και η εισαγωγή θα συνεχιστεί αυτόματα από το ίδιο σημείο.',
            payload=info,
        )
    tenders: List[Tender] = []
    for raw in raw_notices:
        normalized = client.normalize_notice(raw)
        tenders.append(upsert_tender(db, normalized, ingest_run_id=ingest_run_id))
    db.flush()
    logger.info('Stored/updated %s KIMDIS notices', len(tenders))
    return tenders, info


def run_ingest(
    days_back: Optional[int] = None,
    send_email: bool = True,
    profile_id: Optional[int] = None,
    *,
    incremental: bool = False,
) -> dict:
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
        khmdhs_tenders, khmdhs_info = ingest_khmdhs(
            db, profiles, days_back, ingest_run_id, incremental=incremental,
        )
        tenders.extend(khmdhs_tenders)
        created_scores: List[TenderScore] = []
        for tender in tenders:
            for profile in profiles:
                score = score_and_store(db, tender, profile, ingest_run_id=ingest_run_id, store_zero_score=False)
                if score is not None:
                    created_scores.append(score)
        db.flush()

        current_matches = [
            score for score in created_scores
            if score.score >= settings.match_threshold and is_tender_actionable(score.tender)
        ]
        matches_query = (
            db.query(TenderScore)
            .options(joinedload(TenderScore.tender), joinedload(TenderScore.profile))
            .join(Tender)
            .filter(TenderScore.score >= settings.match_threshold, actionable_tender_clause())
        )
        if profile_ids:
            matches_query = matches_query.filter(TenderScore.profile_id.in_(profile_ids))
        digest_matches = matches_query.order_by(TenderScore.score.desc()).limit(30).all()
        if send_email:
            send_digest(digest_matches, settings.digest_recipient_list)
        latest_new_count = sum(
            1 for score in created_scores
            if score.is_new_in_latest_ingest and is_tender_actionable(score.tender)
        )
        changes_detected = db.query(TenderChange).filter(TenderChange.ingest_run_id == ingest_run_id).count()
        profile_names = [profile.name for profile in profiles]
        per_profile = {}
        for profile in profiles:
            profile_scores = [score for score in created_scores if score.profile_id == profile.id]
            per_profile[str(profile.id)] = {
                'profile_id': profile.id,
                'profile_name': profile.name,
                'tenders': len(profile_scores),
                'new_tenders': sum(
                    1 for score in profile_scores
                    if score.is_new_in_latest_ingest and is_tender_actionable(score.tender)
                ),
                'scores': len(profile_scores),
                'matches': sum(
                    1 for score in profile_scores
                    if score.score >= settings.match_threshold and is_tender_actionable(score.tender)
                ),
            }
        result = {
            'tenders': len(tenders),
            'new_tenders': latest_new_count,
            'scores': len(created_scores),
            'matches': len(current_matches),
            'changes_detected': changes_detected,
            'digest_matches': len(digest_matches),
            'warnings': khmdhs_info.get('warnings', []),
            'khmdhs': khmdhs_info,
            'profile_scope': profile_scope,
            'profile_id': profile_id,
            'profile_names': profile_names,
            'per_profile': per_profile,
            'incremental': incremental,
            'continuation_required': bool(khmdhs_info.get('continuation_required')),
        }
        scope_text = f"το προφίλ {profile_names[0]}" if profile_scope == 'selected_profile' and profile_names else 'όλα τα ενεργά προφίλ'
        log_event(
            db,
            event_type='ingest',
            title='Ολοκληρώθηκε εισαγωγή δεδομένων',
            message=f"Εισαγωγή για {scope_text}: ελέγχθηκαν/ενημερώθηκαν {len(tenders)} πράξεις. Δημιουργήθηκαν/ενημερώθηκαν {len(created_scores)} σχετικές αξιολογήσεις, από τις οποίες {latest_new_count} ήταν νέες στην τελευταία εισαγωγή και {len(current_matches)} πέρασαν το όριο match.",
            payload={'days_back': days_back, 'ingest_run_id': ingest_run_id, **result},
        )
        logger.info('Ingest finished: tenders=%s scores=%s matches=%s', len(tenders), len(created_scores), len(current_matches))
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
