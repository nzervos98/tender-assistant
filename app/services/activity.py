from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import SystemEvent


def log_event(db: Session, event_type: str, title: str, message: str = '', payload: dict[str, Any] | None = None) -> SystemEvent:
    event = SystemEvent(event_type=event_type, title=title, message=message, payload=payload or {})
    db.add(event)
    return event
