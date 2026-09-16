from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.jobs.ingest import run_ingest, score_and_store
from app.models import BackgroundJob, ClientProfile, Tender
from app.services.activity import log_event
from app.services.pdf import fetch_and_extract_pdf_text
from app.services.rescore import rescore_existing_tenders
from app.services.timezone import now_utc


logger = logging.getLogger(__name__)
JOB_TYPES = {'ingest', 'rescore', 'pdf_analysis'}
ACTIVE_STATUSES = ('queued', 'running')


def job_lock_key(
    job_type: str,
    profile_id: int | None,
    payload: dict[str, Any] | None = None,
    requested_by_user_id: int | None = None,
) -> str:
    if job_type == 'pdf_analysis':
        tender_id = int((payload or {}).get('tender_id') or 0)
        if not tender_id:
            raise ValueError('PDF analysis requires a tender_id')
        # The requested profile IDs and job visibility are user-scoped. Sharing an
        # active PDF job between users would prevent the second user's profiles
        # from being rescored and expose a job ID they cannot subsequently read.
        return f'pdf_analysis:user:{requested_by_user_id or "system"}:tender:{tender_id}'
    return f'{job_type}:profile:{profile_id or "all"}'


def enqueue_job(
    db: Session,
    *,
    job_type: str,
    profile_id: int | None = None,
    requested_by_user_id: int | None = None,
    payload: dict[str, Any] | None = None,
    available_at: datetime | None = None,
) -> tuple[BackgroundJob, bool]:
    if job_type not in JOB_TYPES:
        raise ValueError(f'Unsupported background job type: {job_type}')
    payload = payload or {}
    lock_key = job_lock_key(job_type, profile_id, payload, requested_by_user_id)
    existing = (
        db.query(BackgroundJob)
        .filter(BackgroundJob.lock_key == lock_key, BackgroundJob.status.in_(ACTIVE_STATUSES))
        .order_by(BackgroundJob.created_at.desc())
        .first()
    )
    if existing is not None:
        return existing, False

    job = BackgroundJob(
        id=uuid4().hex,
        job_type=job_type,
        status='queued',
        lock_key=lock_key,
        profile_id=profile_id,
        requested_by_user_id=requested_by_user_id or None,
        payload=payload,
        result={},
        available_at=available_at or now_utc(),
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        existing = (
            db.query(BackgroundJob)
            .filter(BackgroundJob.lock_key == lock_key, BackgroundJob.status.in_(ACTIVE_STATUSES))
            .order_by(BackgroundJob.created_at.desc())
            .first()
        )
        if existing is None:
            raise
        return existing, False
    return job, True


def recover_stale_jobs(db: Session, stale_after_minutes: int = 60) -> int:
    cutoff = now_utc() - timedelta(minutes=max(5, stale_after_minutes))
    stale = (
        db.query(BackgroundJob)
        .filter(
            BackgroundJob.status == 'running',
            BackgroundJob.heartbeat_at.isnot(None),
            BackgroundJob.heartbeat_at < cutoff,
        )
        .all()
    )
    for job in stale:
        job.status = 'failed'
        job.error = 'Worker heartbeat expired before the job completed.'
        job.finished_at = now_utc()
    return len(stale)


def claim_next_job(db: Session) -> BackgroundJob | None:
    now = now_utc()
    query = (
        db.query(BackgroundJob)
        .filter(
            BackgroundJob.status == 'queued',
            or_(BackgroundJob.available_at.is_(None), BackgroundJob.available_at <= now),
        )
        .order_by(BackgroundJob.available_at.asc(), BackgroundJob.created_at.asc())
    )
    if db.get_bind().dialect.name == 'postgresql':
        query = query.with_for_update(skip_locked=True)
    job = query.first()
    if job is None:
        return None
    job.status = 'running'
    job.started_at = now
    job.heartbeat_at = now
    db.commit()
    return job


def _complete(job_id: str, *, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    db = SessionLocal()
    try:
        job = db.query(BackgroundJob).filter(BackgroundJob.id == job_id).one_or_none()
        if job is None:
            return
        job.finished_at = now_utc()
        job.heartbeat_at = job.finished_at
        if error:
            job.status = 'failed'
            job.error = error[:8000]
            job.result = result or {}
            log_event(db, 'background_job_failed', 'Απέτυχε εργασία παρασκηνίου', error[:500], {'job_id': job.id, 'job_type': job.job_type})
        else:
            job.status = 'completed'
            job.error = None
            job.result = result or {}
            log_event(db, 'background_job_completed', 'Ολοκληρώθηκε εργασία παρασκηνίου', job.job_type, {'job_id': job.id, **(result or {})})
        db.commit()
    finally:
        db.close()


def _complete_with_ingest_continuation(
    job: BackgroundJob,
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    attempt: int,
) -> None:
    """Atomically finish one ingest chunk and queue its delayed continuation."""
    settings = get_settings()
    db = SessionLocal()
    try:
        current = db.query(BackgroundJob).filter(BackgroundJob.id == job.id).one()
        now = now_utc()
        current.status = 'completed'
        current.error = None
        current.finished_at = now
        current.heartbeat_at = now
        db.flush()  # releases the partial unique active lock inside this transaction

        rapid_limit = max(1, settings.khmdhs_continuation_max_attempts)
        cooling_down = attempt + 1 >= rapid_limit
        next_attempt = 0 if cooling_down else attempt + 1
        continuation_cycle = max(0, int(payload.get('continuation_cycle') or 0)) + int(cooling_down)
        continuation_payload = {
            'days': int(payload.get('days') or settings.ingest_days_back),
            'scheduled': bool(payload.get('scheduled')),
            'continuation': True,
            'continuation_attempt': next_attempt,
            'continuation_cycle': continuation_cycle,
            'root_ingest_job_id': payload.get('root_ingest_job_id') or job.id,
        }
        delay_seconds = (
            settings.khmdhs_continuation_cooldown_seconds
            if cooling_down else settings.khmdhs_continuation_delay_seconds
        )
        available_at = now + timedelta(seconds=max(1, delay_seconds))
        continuation, created = enqueue_job(
            db,
            job_type='ingest',
            profile_id=job.profile_id,
            requested_by_user_id=job.requested_by_user_id,
            payload=continuation_payload,
            available_at=available_at,
        )
        result['continuation_job'] = {
            'id': continuation.id,
            'queued': created,
            'attempt': next_attempt,
            'cycle': continuation_cycle,
            'cooling_down': cooling_down,
            'available_at': available_at.isoformat(),
        }
        current.result = result
        log_event(
            db,
            'background_job_completed',
            'Ολοκληρώθηκε τμήμα εισαγωγής',
            job.job_type,
            {'job_id': current.id, **result},
        )
        log_event(
            db,
            'background_job_queued',
            'Προγραμματίστηκε αυτόματη συνέχεια εισαγωγής',
            continuation.id,
            {
                'job_id': continuation.id,
                'previous_job_id': current.id,
                'attempt': next_attempt,
                'cycle': continuation_cycle,
                'cooling_down': cooling_down,
                'available_at': available_at.isoformat(),
            },
        )
        db.commit()
    finally:
        db.close()


def execute_job(job: BackgroundJob) -> None:
    try:
        payload = job.payload or {}
        if job.job_type == 'ingest':
            continuation_attempt = max(0, int(payload.get('continuation_attempt') or 0))
            result = run_ingest(
                days_back=int(payload.get('days') or 0) or None,
                send_email=False,
                profile_id=job.profile_id,
                incremental=bool(payload.get('scheduled') or payload.get('continuation')),
            )
            if result.get('continuation_required'):
                _complete_with_ingest_continuation(
                    job,
                    result,
                    payload,
                    attempt=continuation_attempt,
                )
                return
            # Always finish an ingest with a full local rescore for its profile
            # scope. This updates older rows whose deadlines crossed since they
            # were last returned by the rolling KIMDIS window.
            followup_db = SessionLocal()
            try:
                followup, created = enqueue_job(
                    followup_db,
                    job_type='rescore',
                    profile_id=job.profile_id,
                    payload={'after_ingest_job_id': job.id},
                )
                followup_db.commit()
                result['rescore_job'] = {'id': followup.id, 'queued': created}
            finally:
                followup_db.close()
        elif job.job_type == 'rescore':
            db = SessionLocal()
            try:
                result = rescore_existing_tenders(db, profile_id=job.profile_id)
                db.commit()
            finally:
                db.close()
        elif job.job_type == 'pdf_analysis':
            db = SessionLocal()
            try:
                tender_id = int(payload.get('tender_id') or 0)
                tender = db.query(Tender).filter(Tender.id == tender_id).one_or_none()
                if tender is None:
                    raise ValueError('Tender not found')
                if not tender.attachment_url:
                    raise ValueError('Tender has no PDF attachment')
                pdf_text = fetch_and_extract_pdf_text(tender.attachment_url, strict=True)
                if not pdf_text:
                    raise RuntimeError('Δεν ήταν δυνατή η εξαγωγή κειμένου από το PDF.')
                tender.pdf_text = pdf_text
                profile_ids = sorted({int(value) for value in (payload.get('profile_ids') or []) if value})
                profiles = (
                    db.query(ClientProfile)
                    .filter(ClientProfile.id.in_(profile_ids))
                    .all()
                    if profile_ids else []
                )
                scores_updated = 0
                for profile in profiles:
                    if score_and_store(db, tender, profile) is not None:
                        scores_updated += 1
                log_event(
                    db,
                    event_type='pdf_analyzed',
                    title='Ολοκληρώθηκε ανάλυση PDF',
                    message=f'{tender.reference_number or tender.source_reference} — {tender.title[:160]}',
                    payload={
                        'tender_id': tender.id,
                        'reference_number': tender.reference_number,
                        'characters': len(pdf_text),
                        'scores_updated': scores_updated,
                    },
                )
                result = {
                    'tender_id': tender.id,
                    'characters': len(pdf_text),
                    'scores_updated': scores_updated,
                }
                db.commit()
            finally:
                db.close()
        else:
            raise ValueError(f'Unsupported background job type: {job.job_type}')
        _complete(job.id, result=result)
    except Exception as exc:  # noqa: BLE001
        logger.exception('Background job %s failed', job.id)
        _complete(job.id, error=f'{type(exc).__name__}: {exc}')


def process_one_job() -> bool:
    db = SessionLocal()
    try:
        recover_stale_jobs(db)
        db.commit()
        job = claim_next_job(db)
    finally:
        db.close()
    if job is None:
        return False
    execute_job(job)
    return True
