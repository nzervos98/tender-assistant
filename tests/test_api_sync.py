from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import ApiSyncCheckpoint
from app.services.api_sync import (
    ApiCheckpointStore,
    incremental_date_range,
    query_fingerprint,
    sync_due,
    sync_stream_key,
)
from app.jobs.ingest import _cpv_batches


def _database(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'checkpoints.sqlite').as_posix()}")
    ApiSyncCheckpoint.__table__.create(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_fingerprints_are_stable_for_equivalent_inputs():
    assert query_fingerprint('notice', {'b': 2, 'a': 1}) == query_fingerprint(
        'notice', {'a': 1, 'b': 2}
    )


def test_cpv_batches_are_deterministic_deduplicated_and_bounded():
    batches = _cpv_batches(['3', '1', '2', '1', '5', '4'], 2)

    assert batches == [['1', '2'], ['3', '4'], ['5']]
    assert all(len(batch) <= 2 for batch in batches)
    assert sync_stream_key('notice', 'registration', ['72000000', '48000000']) == sync_stream_key(
        'notice', 'registration', ['48000000', '72000000', '72000000']
    )


def test_checkpoint_resumes_then_serves_completed_query_from_cache(tmp_path):
    factory = _database(tmp_path)
    store = ApiCheckpointStore(factory, cache_hours=20)
    body = {'dateFrom': '2026-07-27', 'dateTo': '2026-07-30', 'cpvItems': ['72000000']}
    fingerprint = query_fingerprint('notice', body)

    initial = store.prepare('notice-stream', 'notice', fingerprint, body)
    assert not initial.cache_hit
    assert not initial.resumed
    assert initial.next_page == 0

    first_page = [{'referenceNumber': 'A'}]
    store.save_page('notice-stream', next_page=1, total_pages=2, records=first_page)
    store.fail('notice-stream', 'temporary 429')

    resumed = store.prepare('notice-stream', 'notice', fingerprint, body)
    assert resumed.resumed
    assert resumed.next_page == 1
    assert resumed.records == first_page

    all_records = [*first_page, {'referenceNumber': 'B'}]
    store.save_page('notice-stream', next_page=2, total_pages=2, records=all_records)
    store.complete('notice-stream', all_records)

    cached = store.prepare('notice-stream', 'notice', fingerprint, body)
    assert cached.cache_hit
    assert cached.records == all_records

    with factory() as db:
        row = db.get(ApiSyncCheckpoint, 'notice-stream')
        assert row.status == 'completed'
        assert row.last_success_date == date(2026, 7, 30)


def test_failed_checkpoint_does_not_advance_watermark(tmp_path):
    factory = _database(tmp_path)
    store = ApiCheckpointStore(factory)
    body = {'dateFrom': '2026-07-27', 'dateTo': '2026-07-30'}
    fingerprint = query_fingerprint('notice', body)
    store.prepare('notice-stream', 'notice', fingerprint, body)
    store.save_page('notice-stream', next_page=1, total_pages=3, records=[{'id': 1}])
    store.fail('notice-stream', 'incomplete')

    with factory() as db:
        row = db.get(ApiSyncCheckpoint, 'notice-stream')
        assert row.status == 'failed'
        assert row.next_page == 1
        assert row.last_success_date is None


def test_incremental_window_uses_watermark_overlap_and_cadence(tmp_path):
    factory = _database(tmp_path)
    today = date(2026, 7, 30)
    with factory() as db:
        assert incremental_date_range(
            db, 'new-stream', today=today, fallback_days=3, overlap_days=1
        ) == ('2026-07-27', '2026-07-30')
        assert sync_due(db, 'new-stream', today=today, cadence_days=3)

        row = ApiSyncCheckpoint(
            stream_key='existing-stream',
            resource='notice',
            query_fingerprint='f' * 64,
            query_body={},
            cached_records=[],
            status='completed',
            last_success_date=today - timedelta(days=2),
        )
        db.add(row)
        db.commit()

        assert incremental_date_range(
            db, 'existing-stream', today=today, fallback_days=3, overlap_days=1
        ) == ('2026-07-27', '2026-07-30')
        assert not sync_due(db, 'existing-stream', today=today, cadence_days=3)
        row.last_success_date = today - timedelta(days=3)
        db.commit()
        assert sync_due(db, 'existing-stream', today=today, cadence_days=3)


def test_incremental_window_finishes_partial_query_before_moving_dates(tmp_path):
    factory = _database(tmp_path)
    with factory() as db:
        db.add(
            ApiSyncCheckpoint(
                stream_key='partial-stream',
                resource='notice',
                query_fingerprint='f' * 64,
                query_body={},
                cached_records=[{'id': 1}],
                status='failed',
                next_page=20,
                date_from='2026-07-27',
                date_to='2026-07-30',
            )
        )
        db.commit()

        assert incremental_date_range(
            db,
            'partial-stream',
            today=date(2026, 7, 31),
            fallback_days=3,
            overlap_days=1,
        ) == ('2026-07-27', '2026-07-30')
