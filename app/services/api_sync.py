from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import ApiSyncCheckpoint


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def query_fingerprint(resource: str, body: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({'resource': resource, 'body': body}).encode('utf-8')).hexdigest()


def sync_stream_key(resource: str, variant: str, cpv_items: Iterable[str]) -> str:
    cpvs = sorted({str(value).strip() for value in cpv_items if str(value).strip()})
    return hashlib.sha256(_canonical({'resource': resource, 'variant': variant, 'cpvs': cpvs}).encode('utf-8')).hexdigest()


def incremental_date_range(
    db: Session,
    stream_key: str,
    *,
    today: date,
    fallback_days: int,
    overlap_days: int,
) -> tuple[str, str]:
    checkpoint = db.get(ApiSyncCheckpoint, stream_key)
    # Finish a partially fetched window before opening a newer one. Otherwise a
    # new day changes dateTo/fingerprint and pagination would restart at page 0.
    if (
        checkpoint is not None
        and checkpoint.status in {'running', 'failed'}
        and checkpoint.next_page > 0
        and checkpoint.date_from
        and checkpoint.date_to
    ):
        return checkpoint.date_from, checkpoint.date_to
    if checkpoint is None or checkpoint.last_success_date is None:
        start = today - timedelta(days=max(1, fallback_days))
    else:
        start = checkpoint.last_success_date - timedelta(days=max(0, overlap_days))
        # The public API enforces a maximum 180-day range.
        start = max(start, today - timedelta(days=180))
    return start.isoformat(), today.isoformat()


def sync_due(db: Session, stream_key: str, *, today: date, cadence_days: int) -> bool:
    checkpoint = db.get(ApiSyncCheckpoint, stream_key)
    if checkpoint is None or checkpoint.last_success_date is None:
        return True
    return checkpoint.last_success_date <= today - timedelta(days=max(1, cadence_days))


@dataclass
class PreparedCheckpoint:
    records: list[dict[str, Any]]
    next_page: int
    cache_hit: bool
    resumed: bool


class ApiCheckpointStore:
    """Small durable store used independently from the main ingest transaction."""

    def __init__(self, session_factory: Callable[[], Session] = SessionLocal, cache_hours: int = 20) -> None:
        self.session_factory = session_factory
        self.cache_hours = max(0, cache_hours)

    def prepare(self, stream_key: str, resource: str, fingerprint: str, body: dict[str, Any]) -> PreparedCheckpoint:
        db = self.session_factory()
        try:
            row = db.get(ApiSyncCheckpoint, stream_key)
            now = datetime.now(timezone.utc)
            if row is not None and row.query_fingerprint == fingerprint:
                completed_at = row.last_success_at
                if completed_at is not None and completed_at.tzinfo is None:
                    completed_at = completed_at.replace(tzinfo=timezone.utc)
                fresh = completed_at is not None and completed_at >= now - timedelta(hours=self.cache_hours)
                if row.status == 'completed' and fresh:
                    return PreparedCheckpoint(list(row.cached_records or []), row.next_page, True, False)
                if row.status in {'running', 'failed'} and row.next_page > 0:
                    row.status = 'running'
                    row.error = None
                    db.commit()
                    return PreparedCheckpoint(list(row.cached_records or []), row.next_page, False, True)
            if row is None:
                row = ApiSyncCheckpoint(stream_key=stream_key, resource=resource, query_fingerprint=fingerprint)
                db.add(row)
            row.resource = resource
            row.query_fingerprint = fingerprint
            row.query_body = body
            row.status = 'running'
            row.next_page = 0
            row.total_pages = None
            row.cached_records = []
            row.date_from = str(body.get('dateFrom') or body.get('cancelDateFrom') or '') or None
            row.date_to = str(body.get('dateTo') or body.get('cancelDateTo') or '') or None
            row.error = None
            db.commit()
            return PreparedCheckpoint([], 0, False, False)
        finally:
            db.close()

    def save_page(
        self,
        stream_key: str,
        *,
        next_page: int,
        total_pages: int | None,
        records: list[dict[str, Any]],
    ) -> None:
        db = self.session_factory()
        try:
            row = db.get(ApiSyncCheckpoint, stream_key)
            if row is None:
                return
            row.status = 'running'
            row.next_page = next_page
            row.total_pages = total_pages
            row.cached_records = records
            row.error = None
            db.commit()
        finally:
            db.close()

    def complete(self, stream_key: str, records: list[dict[str, Any]]) -> None:
        db = self.session_factory()
        try:
            row = db.get(ApiSyncCheckpoint, stream_key)
            if row is None:
                return
            now = datetime.now(timezone.utc)
            row.status = 'completed'
            row.cached_records = records
            row.last_success_at = now
            try:
                row.last_success_date = date.fromisoformat(row.date_to) if row.date_to else now.date()
            except ValueError:
                row.last_success_date = now.date()
            row.error = None
            db.commit()
        finally:
            db.close()

    def fail(self, stream_key: str, error: str) -> None:
        db = self.session_factory()
        try:
            row = db.get(ApiSyncCheckpoint, stream_key)
            if row is not None:
                row.status = 'failed'
                row.error = error[:2000]
                db.commit()
        finally:
            db.close()
