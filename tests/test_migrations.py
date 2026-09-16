import sqlite3

from alembic import command
from alembic.config import Config

from app.config import get_settings
from app.main import EXPECTED_SCHEMA_REVISION


def test_alembic_bootstraps_operational_schema(monkeypatch, tmp_path):
    database_path = tmp_path / 'migration.sqlite'
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{database_path.as_posix()}')
    get_settings.cache_clear()
    try:
        command.upgrade(Config('alembic.ini'), 'head')
        connection = sqlite3.connect(database_path)
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone()[0] == EXPECTED_SCHEMA_REVISION
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'background_jobs', 'tender_changes', 'api_sync_checkpoints', 'api_rate_limit_state'} <= tables
        background_job_columns = {row[1] for row in connection.execute('PRAGMA table_info(background_jobs)')}
        assert 'available_at' in background_job_columns
        tender_columns = {row[1] for row in connection.execute('PRAGMA table_info(tenders)')}
        assert {'contractor_name', 'public_funding_ref_num', 'is_modified'} <= tender_columns
        diavgeia_columns = {row[1] for row in connection.execute('PRAGMA table_info(diavgeia_decisions)')}
        assert {'match_confidence', 'match_evidence', 'is_current', 'last_verified_at'} <= diavgeia_columns
        score_columns = {row[1] for row in connection.execute('PRAGMA table_info(tender_scores)')}
        assert 'cpv_match_type' in score_columns
        score_indexes = {row[1] for row in connection.execute("PRAGMA index_list('tender_scores')")}
        assert 'ix_tender_scores_profile_match_score' in score_indexes
    finally:
        get_settings.cache_clear()
