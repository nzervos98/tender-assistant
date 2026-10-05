from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import init_db, session_scope
from app.models import ClientProfile, Tender, TenderChange, TenderScore
from app.services.activity import log_event
from app.services.api_sync import ApiCheckpointStore, incremental_date_range, sync_due, sync_stream_key
from app.services.emailer import send_digest
from app.services.early_signals import (
    linked_notice_references,
    merge_signal_record,
    resolve_pending_notice_links,
    strongest_signal_stage,
    sync_signal_notice_links,
)
from app.services.khmdhs_client import KhmdhsClient, build_search_body
from app.services.pdf import fetch_and_extract_pdf_text
from app.services.profiles import collect_cpv_codes
from app.services.repository import upsert_score, upsert_tender
from app.services.scoring import cpv_match_key, rule_score_tender
from app.services.opportunity_status import actionable_tender_clause, is_tender_actionable
from app.services.timezone import today_local
from app.services.workflow import workflow_status_filter_values

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


def ingest_khmdhs_requests(
    db: Session,
    profiles: Iterable[ClientProfile],
    days_back: int,
    ingest_run_id: str,
    *,
    incremental: bool = False,
) -> Tuple[List[Tender], List[Tender], dict]:
    """Import profile-relevant REQ acts and resolve REQ -> PROC transitions."""
    profiles = list(profiles)
    info: dict = {'source': 'khmdhs_request', 'warnings': []}
    cpvs = collect_cpv_codes(profiles, expand_known_children=True)
    if not profiles or not cpvs:
        info['warnings'].append('no_active_profiles' if not profiles else 'no_cpv_codes')
        return [], [], info

    settings = get_settings()
    today = today_local()
    fixed_date_from, fixed_date_to = _date_range(days_back)
    batches = _cpv_batches(cpvs, settings.khmdhs_cpv_batch_size)
    client = KhmdhsClient()
    checkpoint_store = _checkpoint_store(db)
    query_metrics: list[dict] = []
    merged: dict[str, dict] = {}

    # Two product stages are enough for users while the explicit flags keep the
    # official API's three request collections from being silently mixed.
    base_variants = [
        ('initial', True, False, False, False),
        ('approved', False, True, True, False),
        ('cancellation', True, True, True, True),
    ]
    for batch_number, batch in enumerate(batches, start=1):
        variants = list(base_variants)
        reconciliation_stream = sync_stream_key('request', 'reconciliation', batch)
        if incremental and sync_due(
            db,
            reconciliation_stream,
            today=today,
            cadence_days=settings.khmdhs_signal_reconciliation_cadence_days,
        ):
            variants.append(('reconciliation', True, True, True, False))
        for variant, is_initial, is_approved, is_approval, cancellation_only in variants:
            stream = sync_stream_key('request', variant, batch)
            if variant == 'reconciliation':
                date_from = (today - timedelta(days=max(1, min(180, settings.khmdhs_signal_reconciliation_days)))).isoformat()
                date_to = today.isoformat()
            elif incremental:
                date_from, date_to = incremental_date_range(
                    db,
                    stream,
                    today=today,
                    fallback_days=days_back,
                    overlap_days=settings.khmdhs_sync_overlap_days,
                )
            else:
                date_from, date_to = fixed_date_from, fixed_date_to
            rows = client.search_requests(
                date_from='' if cancellation_only else date_from,
                date_to='' if cancellation_only else date_to,
                cancel_date_from=date_from if cancellation_only else '',
                cancel_date_to=date_to if cancellation_only else '',
                cpv_items=batch,
                is_initial=is_initial,
                is_approved=is_approved,
                is_approval=is_approval,
                checkpoint_store=checkpoint_store,
                stream_key=stream,
            )
            for row in rows:
                reference = str(row.get('referenceNumber') or row.get('id') or '').strip()
                if reference:
                    merged[reference] = merge_signal_record(
                        merged.get(reference),
                        row,
                        'cancelled' if cancellation_only else ('initial' if variant == 'reconciliation' else variant),
                    )
            query_metrics.append(
                _client_metrics(
                    client,
                    variant=variant,
                    batch_number=batch_number,
                    date_from=date_from,
                    date_to=date_to,
                )
            )

    signals: list[Tender] = []
    for raw in merged.values():
        reference = str(raw.get('referenceNumber') or raw.get('id') or '').strip()
        existing = (
            db.query(Tender)
            .filter(Tender.source == 'khmdhs_request', Tender.source_reference == reference)
            .one_or_none()
        ) if reference else None
        previous_stage = existing.signal_stage if existing is not None else None
        signal = upsert_tender(db, client.normalize_record('request', raw), ingest_run_id=ingest_run_id)
        signal.signal_stage = strongest_signal_stage(previous_stage, signal.signal_stage)
        sync_signal_notice_links(db, signal)
        signals.append(signal)
    db.flush()

    # Most linked notices will already have arrived through the regular notice
    # stream. Exact ADAM retrieval closes the gap without widening the search.
    linked_notice_refs = sorted({ref for signal in signals for ref in linked_notice_references(signal.raw)})
    fetched_notices: list[Tender] = []
    exact_lookup_failures = 0
    for reference in linked_notice_refs:
        existing = (
            db.query(Tender)
            .filter(Tender.source == 'khmdhs_notice', Tender.reference_number == reference)
            .one_or_none()
        )
        if existing is not None:
            continue
        try:
            rows = client.search_resource(
                'notice',
                build_search_body(resource='notice', reference_number=reference),
                max_pages=1,
            )
        except Exception:  # the pending durable link will be retried on a later run
            logger.exception('Could not resolve linked notice %s', reference)
            exact_lookup_failures += 1
            continue
        raw_notice = next(
            (row for row in rows if str(row.get('referenceNumber') or '').strip() == reference),
            None,
        )
        if raw_notice is not None:
            fetched_notices.append(
                upsert_tender(db, client.normalize_notice(raw_notice), ingest_run_id=ingest_run_id)
            )
        elif client.last_rate_limited or client.last_transient_error:
            exact_lookup_failures += 1
    resolved_links = resolve_pending_notice_links(db, fetched_notices)

    incomplete_queries = sum(
        1 for metric in query_metrics
        if metric['rate_limited'] or metric['hit_max_pages'] or metric['transient_error']
    )
    if exact_lookup_failures:
        info['warnings'].append('linked_notice_lookup_deferred')
    if any(metric['rate_limited'] for metric in query_metrics):
        info['warnings'].append('kimdis_rate_limit')
    if any(metric['transient_error'] for metric in query_metrics):
        info['warnings'].append('kimdis_temporary_connection_error')
    info.update({
        'signals': len(signals),
        'initial_signals': sum(1 for signal in signals if signal.signal_stage == 'initial'),
        'approved_signals': sum(1 for signal in signals if signal.signal_stage == 'approved'),
        'converted_signals': sum(1 for signal in signals if signal.signal_stage == 'converted'),
        'cancelled_signals': sum(1 for signal in signals if signal.signal_stage == 'cancelled'),
        'linked_notice_references': len(linked_notice_refs),
        'linked_notices_fetched': len(fetched_notices),
        'links_resolved': resolved_links,
        'exact_lookup_failures': exact_lookup_failures,
        'cpv_batch_count': len(batches),
        'query_count': len(query_metrics) + len(linked_notice_refs),
        'pages_fetched': sum(metric['pages_fetched'] for metric in query_metrics),
        'incomplete_queries': incomplete_queries,
        'continuation_required': bool(incomplete_queries or exact_lookup_failures),
        'incremental': incremental,
    })
    return signals, fetched_notices, info


def _watched_refresh_due(tender: Tender, now: datetime, settings) -> bool:
    terminal = (
        tender.signal_stage in ('converted', 'cancelled')
        if tender.source == 'khmdhs_request'
        else not is_tender_actionable(tender, at=now)
    )
    cadence = (
        settings.khmdhs_terminal_watch_refresh_cadence_days
        if terminal else settings.khmdhs_watch_refresh_cadence_days
    )
    checked = tender.watch_last_checked_at
    if checked is None:
        return True
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return checked <= now - timedelta(days=max(1, cadence))


def _inherit_saved_signal_watchers(db: Session, signal: Tender, notice: Tender) -> int:
    """Carry explicit REQ watches to its PROC without relying on profile CPVs."""
    watcher_scores = (
        db.query(TenderScore)
        .options(joinedload(TenderScore.profile))
        .filter(
            TenderScore.tender_id == signal.id,
            TenderScore.user_status.in_(workflow_status_filter_values('saved')),
        )
        .all()
    )
    inherited = 0
    for watcher in watcher_scores:
        if watcher.profile is None:
            continue
        existing = (
            db.query(TenderScore)
            .filter(TenderScore.tender_id == notice.id, TenderScore.profile_id == watcher.profile_id)
            .one_or_none()
        )
        linked_score = score_and_store(db, notice, watcher.profile, store_zero_score=True)
        if existing is None or linked_score.user_status == 'new':
            linked_score.user_status = 'saved'
            linked_score.status_updated_at = datetime.now(timezone.utc)
        linked_score.discovery_source = 'watched_signal'
        inherited += 1
    return inherited


def refresh_manually_watched_acts(
    db: Session,
    ingest_run_id: str,
    *,
    profile_ids: list[int] | None = None,
    already_refreshed: set[tuple[str, str]] | None = None,
) -> tuple[list[Tender], dict]:
    """Refresh each unique manually saved ADAM once, independently of CPV searches.

    The timestamp is stored on the shared Tender row, so ten users watching the
    same ADAM still cost one exact KIMDIS call. Failed rows remain due and are
    retried by the existing ingest continuation mechanism.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    query = (
        db.query(Tender)
        .join(TenderScore, TenderScore.tender_id == Tender.id)
        .join(ClientProfile, ClientProfile.id == TenderScore.profile_id)
        .filter(
            Tender.source.in_(('khmdhs_notice', 'khmdhs_request')),
            TenderScore.user_status.in_(workflow_status_filter_values('saved')),
            ClientProfile.is_active.is_(True),
        )
    )
    if profile_ids is not None:
        query = query.filter(TenderScore.profile_id.in_(profile_ids))
    watched = query.distinct().order_by(Tender.watch_last_checked_at.asc().nullsfirst(), Tender.id.asc()).all()
    excluded = already_refreshed or set()
    client = KhmdhsClient()
    updated: list[Tender] = []
    fetched_linked_notices: list[Tender] = []
    failures = 0
    checked = 0
    for tender in watched:
        key = (tender.source, tender.source_reference)
        if key in excluded:
            tender.watch_last_checked_at = now
            continue
        if not _watched_refresh_due(tender, now, settings):
            continue
        resource = 'request' if tender.source == 'khmdhs_request' else 'notice'
        reference = tender.reference_number or tender.source_reference
        try:
            rows = client.search_resource(
                resource,
                build_search_body(resource=resource, reference_number=reference),
                max_pages=1,
            )
        except Exception:
            logger.exception('Could not refresh watched KIMDIS act %s', reference)
            failures += 1
            break
        raw = next(
            (row for row in rows if str(row.get('referenceNumber') or '').strip() == reference),
            rows[0] if rows else None,
        )
        if raw is not None:
            refreshed = upsert_tender(
                db,
                client.normalize_record(resource, raw),
                ingest_run_id=ingest_run_id,
            )
            if resource == 'request':
                refreshed.signal_stage = strongest_signal_stage(
                    tender.signal_stage, refreshed.signal_stage,
                )
                sync_signal_notice_links(db, refreshed)
            refreshed.watch_last_checked_at = now
            updated.append(refreshed)
        else:
            tender.watch_last_checked_at = now
        checked += 1
        if client.last_rate_limited or client.last_transient_error:
            failures += 1
            break

    db.flush()
    # A watched REQ is useful only if a newly announced PROC is fetched in the
    # same flow. Exact lookup avoids depending on any profile's current CPVs.
    pending_notice_refs = sorted({
        link.related_reference
        for item in updated if item.source == 'khmdhs_request'
        for link in item.outgoing_links
        if link.related_tender_id is None
    })
    for reference in pending_notice_refs:
        try:
            rows = client.search_resource(
                'notice',
                build_search_body(resource='notice', reference_number=reference),
                max_pages=1,
            )
        except Exception:
            logger.exception('Could not fetch PROC linked from watched signal %s', reference)
            failures += 1
            break
        raw_notice = next(
            (row for row in rows if str(row.get('referenceNumber') or '').strip() == reference),
            rows[0] if rows else None,
        )
        if raw_notice is not None:
            notice = upsert_tender(
                db,
                client.normalize_notice(raw_notice),
                ingest_run_id=ingest_run_id,
            )
            notice.watch_last_checked_at = now
            fetched_linked_notices.append(notice)
        elif client.last_rate_limited or client.last_transient_error:
            failures += 1
            break

    updated.extend(fetched_linked_notices)
    resolved = resolve_pending_notice_links(db, [item for item in updated if item.source == 'khmdhs_notice'])
    inherited = 0
    watched_signals = [item for item in watched if item.source == 'khmdhs_request']
    for signal in watched_signals:
        for link in signal.outgoing_links:
            if link.related_tender is not None:
                inherited += _inherit_saved_signal_watchers(db, signal, link.related_tender)
    db.flush()
    info = {
        'watched_unique': len(watched),
        'checked': checked,
        'updated': len(updated),
        'linked_notices_fetched': len(fetched_linked_notices),
        'links_resolved': resolved,
        'watches_inherited': inherited,
        'failures': failures,
        'continuation_required': bool(failures),
        'warnings': ['watched_refresh_deferred'] if failures else [],
    }
    return updated, info


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
        signal_tenders, linked_notice_tenders, signal_info = ingest_khmdhs_requests(
            db, profiles, days_back, ingest_run_id, incremental=incremental,
        )
        tenders.extend(signal_tenders)
        known_ids = {tender.id for tender in tenders}
        tenders.extend(tender for tender in linked_notice_tenders if tender.id not in known_ids)
        already_refreshed = {(tender.source, tender.source_reference) for tender in tenders}
        watched_tenders, watched_info = refresh_manually_watched_acts(
            db,
            ingest_run_id,
            profile_ids=profile_ids if profile_id else None,
            already_refreshed=already_refreshed,
        )
        known_ids = {tender.id for tender in tenders}
        tenders.extend(tender for tender in watched_tenders if tender.id not in known_ids)
        created_scores: List[TenderScore] = []
        for tender in tenders:
            for profile in profiles:
                score = score_and_store(db, tender, profile, ingest_run_id=ingest_run_id, store_zero_score=False)
                if score is not None:
                    created_scores.append(score)
        db.flush()

        current_matches = [
            score for score in created_scores
            if score.tender.source == 'khmdhs_notice'
            and score.score >= settings.match_threshold and is_tender_actionable(score.tender)
        ]
        matches_query = (
            db.query(TenderScore)
            .options(joinedload(TenderScore.tender), joinedload(TenderScore.profile))
            .join(Tender)
            .filter(
                Tender.source == 'khmdhs_notice',
                TenderScore.score >= settings.match_threshold,
                actionable_tender_clause(),
            )
        )
        if profile_ids:
            matches_query = matches_query.filter(TenderScore.profile_id.in_(profile_ids))
        digest_matches = matches_query.order_by(TenderScore.score.desc()).limit(30).all()
        if send_email:
            send_digest(digest_matches, settings.digest_recipient_list)
        latest_new_count = sum(
            1 for score in created_scores
            if score.tender.source == 'khmdhs_notice'
            and score.is_new_in_latest_ingest and is_tender_actionable(score.tender)
        )
        changes_detected = db.query(TenderChange).filter(TenderChange.ingest_run_id == ingest_run_id).count()
        profile_names = [profile.name for profile in profiles]
        per_profile = {}
        for profile in profiles:
            profile_scores = [score for score in created_scores if score.profile_id == profile.id]
            opportunity_scores = [score for score in profile_scores if score.tender.source == 'khmdhs_notice']
            signal_scores = [score for score in profile_scores if score.tender.source == 'khmdhs_request']
            per_profile[str(profile.id)] = {
                'profile_id': profile.id,
                'profile_name': profile.name,
                'tenders': len(opportunity_scores),
                'signals': len(signal_scores),
                'new_tenders': sum(
                    1 for score in opportunity_scores
                    if score.is_new_in_latest_ingest and is_tender_actionable(score.tender)
                ),
                'scores': len(opportunity_scores),
                'matches': sum(
                    1 for score in opportunity_scores
                    if score.score >= settings.match_threshold and is_tender_actionable(score.tender)
                ),
            }
        result = {
            'tenders': sum(1 for tender in tenders if tender.source == 'khmdhs_notice'),
            'signals': len(signal_tenders),
            'new_tenders': latest_new_count,
            'scores': len(created_scores),
            'matches': len(current_matches),
            'changes_detected': changes_detected,
            'digest_matches': len(digest_matches),
            'warnings': list(dict.fromkeys([
                *khmdhs_info.get('warnings', []),
                *signal_info.get('warnings', []),
                *watched_info.get('warnings', []),
            ])),
            'khmdhs': khmdhs_info,
            'early_signals': signal_info,
            'watched_refresh': watched_info,
            'profile_scope': profile_scope,
            'profile_id': profile_id,
            'profile_names': profile_names,
            'per_profile': per_profile,
            'incremental': incremental,
            'continuation_required': bool(
                khmdhs_info.get('continuation_required')
                or signal_info.get('continuation_required')
                or watched_info.get('continuation_required')
            ),
        }
        scope_text = f"το προφίλ {profile_names[0]}" if profile_scope == 'selected_profile' and profile_names else 'όλα τα ενεργά προφίλ'
        log_event(
            db,
            event_type='ingest',
            title='Ολοκληρώθηκε εισαγωγή δεδομένων',
            message=(
                f"Εισαγωγή για {scope_text}: ελέγχθηκαν/ενημερώθηκαν "
                f"{sum(1 for tender in tenders if tender.source == 'khmdhs_notice')} διαγωνισμοί και "
                f"{len(signal_tenders)} πρώιμα σήματα. Δημιουργήθηκαν/ενημερώθηκαν "
                f"{len(created_scores)} σχετικές αξιολογήσεις, από τις οποίες {latest_new_count} "
                f"ήταν νέοι διαγωνισμοί και {len(current_matches)} πέρασαν το όριο match."
            ),
            payload={'days_back': days_back, 'ingest_run_id': ingest_run_id, **result},
        )
        logger.info(
            'Ingest finished: notices=%s signals=%s scores=%s matches=%s',
            sum(1 for tender in tenders if tender.source == 'khmdhs_notice'),
            len(signal_tenders),
            len(created_scores),
            len(current_matches),
        )
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
