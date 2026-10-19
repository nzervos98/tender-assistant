from __future__ import annotations

from sqlalchemy import func, text
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import AppUser, BackgroundJob, ClientProfile, SystemEvent, Tender, TenderScore
from app.services.opportunity_status import actionable_tender_clause, expired_tender_clause
from app.services.timezone import now_utc
from app.services.workflow import workflow_status_filter_values, workflow_status_label


def _latest_event(db: Session, event_type: str) -> SystemEvent | None:
    return (
        db.query(SystemEvent)
        .filter(SystemEvent.event_type == event_type)
        .order_by(SystemEvent.created_at.desc(), SystemEvent.id.desc())
        .first()
    )


def _event_payload(event: SystemEvent | None) -> dict:
    return event.payload if event is not None and isinstance(event.payload, dict) else {}


def _source_label(source: str | None) -> str:
    value = str(source or '')
    if value == 'khmdhs_notice':
        return 'ΚΗΜΔΗΣ — Διαγωνισμοί'
    if value == 'khmdhs_request':
        return 'ΚΗΜΔΗΣ — Πρώιμα σήματα'
    if value.startswith('khmdhs'):
        return 'ΚΗΜΔΗΣ'
    if value.startswith('diavgeia'):
        return 'Διαύγεια'
    return value or 'Άγνωστη πηγή'


def database_usage_summary(db: Session) -> dict[str, object]:
    """Read-only maintenance metrics with tenders and early signals separated."""
    settings = get_settings()
    latest_ingest = _latest_event(db, 'ingest')
    latest_rescore = _latest_event(db, 'rescore')
    latest_ingest_payload = _event_payload(latest_ingest)

    db_size = 'Δεν είναι διαθέσιμο'
    try:
        if not settings.database_url.startswith('sqlite'):
            db_size = str(db.execute(text('select pg_size_pretty(pg_database_size(current_database()))')).scalar() or db_size)
    except Exception:
        db_size = 'Δεν είναι διαθέσιμο'

    notice_query = db.query(Tender).filter(Tender.source == 'khmdhs_notice')
    signal_query = db.query(Tender).filter(Tender.source == 'khmdhs_request')
    notice_scores = db.query(TenderScore).join(Tender).filter(Tender.source == 'khmdhs_notice')
    signal_scores = db.query(TenderScore).join(Tender).filter(Tender.source == 'khmdhs_request')
    now = now_utc()

    source_counts = [
        {'source': _source_label(source), 'count': count}
        for source, count in db.query(Tender.source, func.count(Tender.id)).group_by(Tender.source).order_by(Tender.source.asc()).all()
    ]
    status_counts = [
        {'status': workflow_status_label(value), 'count': count}
        for value, count in notice_scores.with_entities(TenderScore.user_status, func.count(TenderScore.id)).group_by(TenderScore.user_status).order_by(TenderScore.user_status.asc()).all()
    ]
    signal_status_counts = [
        {'status': workflow_status_label(value), 'count': count}
        for value, count in signal_scores.with_entities(TenderScore.user_status, func.count(TenderScore.id)).group_by(TenderScore.user_status).order_by(TenderScore.user_status.asc()).all()
    ]

    return {
        'db_size': db_size,
        'profiles': db.query(ClientProfile).count(),
        'active_profiles': db.query(ClientProfile).filter(ClientProfile.is_active.is_(True)).count(),
        'tenders': notice_query.count(),
        'signals': signal_query.count(),
        'scores': notice_scores.count(),
        'signal_scores': signal_scores.count(),
        'active_or_unknown': notice_query.filter(actionable_tender_clause(now)).count(),
        'expired': notice_query.filter(expired_tender_clause(now)).count(),
        'cancelled': notice_query.filter(Tender.cancelled.is_(True)).count(),
        'latest_new': notice_scores.filter(TenderScore.is_new_in_latest_ingest.is_(True)).count(),
        'pdf_text_count': notice_query.filter(Tender.pdf_text.isnot(None), Tender.pdf_text != '').count(),
        'latest_ingest': latest_ingest,
        'latest_ingest_payload': latest_ingest_payload,
        'latest_rescore': latest_rescore,
        'source_counts': source_counts,
        'status_counts': status_counts,
        'signal_status_counts': signal_status_counts,
        'schedule': f'{settings.schedule_hour:02d}:{settings.schedule_minute:02d} {settings.app_timezone}',
        'ingest_days_back': settings.ingest_days_back,
        'khmdhs_max_pages': settings.khmdhs_max_pages,
        'khmdhs_requests_per_minute': settings.khmdhs_requests_per_minute,
        'khmdhs_query_cache_hours': settings.khmdhs_query_cache_hours,
        'khmdhs_sync_overlap_days': settings.khmdhs_sync_overlap_days,
        'khmdhs_continuation_delay_seconds': settings.khmdhs_continuation_delay_seconds,
        'khmdhs_continuation_max_attempts': settings.khmdhs_continuation_max_attempts,
        'match_threshold': settings.match_threshold,
    }


def admin_overview_summary(db: Session) -> dict[str, object]:
    """Build the admin cockpit view model without mixing requests with tenders."""
    stats = database_usage_summary(db)
    users = db.query(AppUser).order_by(AppUser.is_active.desc(), AppUser.username.asc()).all()
    profiles = (
        db.query(ClientProfile)
        .options(joinedload(ClientProfile.owner))
        .order_by(ClientProfile.is_active.desc(), ClientProfile.name.asc())
        .all()
    )
    owned_profile_counts = {
        user_id: total
        for user_id, total in (
            db.query(
                ClientProfile.owner_user_id,
                func.count(ClientProfile.id),
            )
            .filter(ClientProfile.owner_user_id.isnot(None))
            .group_by(ClientProfile.owner_user_id)
            .all()
        )
    }
    active_owned_counts = dict(
        db.query(ClientProfile.owner_user_id, func.count(ClientProfile.id))
        .filter(ClientProfile.owner_user_id.isnot(None), ClientProfile.is_active.is_(True))
        .group_by(ClientProfile.owner_user_id)
        .all()
    )
    user_rows = [
        {
            'user': user,
            'profiles': owned_profile_counts.get(user.id, 0),
            'active_profiles': active_owned_counts.get(user.id, 0),
        }
        for user in users
    ]
    active_profiles = [profile for profile in profiles if profile.is_active]
    notice_scores = db.query(TenderScore).join(Tender).filter(Tender.source == 'khmdhs_notice')
    high_priority_count = (
        notice_scores
        .filter(
            TenderScore.score >= 75,
            ~TenderScore.user_status.in_(workflow_status_filter_values('not_relevant')),
            actionable_tender_clause(now_utc()),
        )
        .count()
    )
    reviewing_count = notice_scores.filter(TenderScore.user_status.in_(workflow_status_filter_values('reviewing'))).count()
    saved_count = notice_scores.filter(TenderScore.user_status.in_(workflow_status_filter_values('saved'))).count()
    signal_saved_count = (
        db.query(TenderScore).join(Tender)
        .filter(Tender.source == 'khmdhs_request', TenderScore.user_status.in_(workflow_status_filter_values('saved')))
        .count()
    )
    job_status_counts = dict(
        db.query(BackgroundJob.status, func.count(BackgroundJob.id))
        .group_by(BackgroundJob.status)
        .all()
    )
    recent_jobs = db.query(BackgroundJob).order_by(BackgroundJob.created_at.desc()).limit(8).all()
    recent_events = db.query(SystemEvent).order_by(SystemEvent.created_at.desc(), SystemEvent.id.desc()).limit(8).all()
    warning_events = (
        db.query(SystemEvent)
        .filter(SystemEvent.event_type.in_([
            'kimdis_rate_limit', 'profile_warning', 'ingest_error', 'ingest_warning', 'background_job_failed',
        ]))
        .order_by(SystemEvent.created_at.desc(), SystemEvent.id.desc())
        .limit(8)
        .all()
    )
    return {
        **stats,
        'users': users,
        'users_total': len(users),
        'active_users': sum(1 for user in users if user.is_active),
        'admin_users': sum(1 for user in users if user.is_admin),
        'user_rows': user_rows,
        'profile_rows': profiles,
        'profiles_without_owner': [profile for profile in profiles if profile.owner_user_id is None],
        'active_profiles_without_cpv': [profile for profile in active_profiles if not (profile.cpv_codes or [])],
        'inactive_profiles': [profile for profile in profiles if not profile.is_active],
        'reviewing_count': reviewing_count,
        'saved_count': saved_count,
        'signal_saved_count': signal_saved_count,
        'high_priority_count': high_priority_count,
        'job_status_counts': job_status_counts,
        'recent_jobs': recent_jobs,
        'recent_events': recent_events,
        'warning_events': warning_events,
    }
