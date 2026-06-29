from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {'check_same_thread': False} if settings.database_url.startswith('sqlite') else {}
engine = create_engine(settings.database_url, echo=False, future=True, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


def _add_column_if_missing(table_name: str, column_name: str, ddl: str) -> None:
    """Tiny migration helper for this MVP.

    SQLAlchemy create_all() does not alter existing tables. This helper keeps old
    Docker volumes working after we add columns to the models.
    """
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    existing = {column['name'] for column in inspector.get_columns(table_name)}
    if column_name in existing:
        return
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}'))


def run_lightweight_migrations() -> None:
    if settings.database_url.startswith('sqlite'):
        _add_column_if_missing('client_profiles', 'owner_user_id', 'INTEGER')
    else:
        _add_column_if_missing('client_profiles', 'owner_user_id', 'INTEGER')

    if settings.database_url.startswith('sqlite'):
        _add_column_if_missing('tender_scores', 'user_status', "VARCHAR(40) DEFAULT 'new' NOT NULL")
        _add_column_if_missing('tender_scores', 'user_notes', 'TEXT')
        _add_column_if_missing('tender_scores', 'status_updated_at', 'DATETIME')
    else:
        _add_column_if_missing('tender_scores', 'user_status', "VARCHAR(40) DEFAULT 'new' NOT NULL")
        _add_column_if_missing('tender_scores', 'user_notes', 'TEXT')
        _add_column_if_missing('tender_scores', 'status_updated_at', 'TIMESTAMP WITH TIME ZONE')

    if settings.database_url.startswith('sqlite'):
        _add_column_if_missing('tenders', 'is_new_in_latest_ingest', 'BOOLEAN DEFAULT 0 NOT NULL')
        _add_column_if_missing('tenders', 'first_seen_ingest_run_id', 'VARCHAR(80)')
        _add_column_if_missing('tenders', 'last_seen_ingest_run_id', 'VARCHAR(80)')
        _add_column_if_missing('tender_scores', 'is_new_in_latest_ingest', 'BOOLEAN DEFAULT 0 NOT NULL')
        _add_column_if_missing('tender_scores', 'first_seen_ingest_run_id', 'VARCHAR(80)')
        _add_column_if_missing('tender_scores', 'last_seen_ingest_run_id', 'VARCHAR(80)')
    else:
        _add_column_if_missing('tenders', 'is_new_in_latest_ingest', 'BOOLEAN DEFAULT FALSE NOT NULL')
        _add_column_if_missing('tenders', 'first_seen_ingest_run_id', 'VARCHAR(80)')
        _add_column_if_missing('tenders', 'last_seen_ingest_run_id', 'VARCHAR(80)')
        _add_column_if_missing('tender_scores', 'is_new_in_latest_ingest', 'BOOLEAN DEFAULT FALSE NOT NULL')
        _add_column_if_missing('tender_scores', 'first_seen_ingest_run_id', 'VARCHAR(80)')
        _add_column_if_missing('tender_scores', 'last_seen_ingest_run_id', 'VARCHAR(80)')
    _normalize_legacy_workflow_statuses()


def _normalize_legacy_workflow_statuses() -> None:
    """Collapse older workflow labels to the current four statuses.

    This keeps existing Docker volumes tidy after the workflow simplification.
    The alias logic remains in the UI, but the stored values become easier to query.
    """
    inspector = inspect(engine)
    if 'tender_scores' not in inspector.get_table_names():
        return
    existing = {column['name'] for column in inspector.get_columns('tender_scores')}
    if 'user_status' not in existing:
        return
    with engine.begin() as conn:
        conn.execute(text("UPDATE tender_scores SET user_status = 'saved' WHERE user_status IN ('interested', 'will_bid')"))
        conn.execute(text("UPDATE tender_scores SET user_status = 'not_relevant' WHERE user_status IN ('not_interested', 'rejected')"))


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    run_lightweight_migrations()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
