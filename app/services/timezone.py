from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import get_settings


def app_tz() -> ZoneInfo:
    tz_name = get_settings().app_timezone or 'Europe/Athens'
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo('Europe/Athens')


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return ensure_aware_utc(value).astimezone(app_tz())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return now_utc().astimezone(app_tz())


def today_local() -> date:
    return now_local().date()


def local_day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=app_tz()).astimezone(timezone.utc)


def local_day_end(value: date) -> datetime:
    return datetime.combine(value, time.max, tzinfo=app_tz()).astimezone(timezone.utc)


def format_local_datetime(value: datetime | None, include_tz: bool = True) -> str:
    local = to_local(value)
    if local is None:
        return '-'
    # Οι ώρες εμφανίζονται ήδη στη ζώνη ώρας της εφαρμογής. Δεν προσθέτουμε λεκτικό suffix στο UI.
    return local.strftime('%d/%m/%Y %H:%M')


def format_local_date(value: datetime | date | None) -> str:
    if value is None:
        return '-'
    if isinstance(value, datetime):
        local = to_local(value)
        return local.strftime('%d/%m/%Y') if local else '-'
    return value.strftime('%d/%m/%Y')


def iso_local_datetime(value: datetime | None) -> str:
    local = to_local(value)
    return local.isoformat() if local else ''


def format_kimdis_publication_datetime(value: datetime | None) -> str:
    """Display KIMDIS publishedDate without a misleading midnight time.

    KIMDIS publishedDate often represents a date-only value stored as 00:00.
    For end users, showing 00:00 looks like an actual publication time, so we
    omit the time when the local time is exactly midnight.
    """
    local = to_local(value)
    if local is None:
        return '-'
    if (value.hour == 0 and value.minute == 0 and value.second == 0) or (local.hour == 0 and local.minute == 0 and local.second == 0):
        return local.strftime('%d/%m/%Y')
    return local.strftime('%d/%m/%Y %H:%M')
