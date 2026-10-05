from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.jobs import ingest
from app.main import dashboard_summary, templates
from app.models import ClientProfile, Tender, TenderLink, TenderScore
from app.services.early_signals import (
    linked_notice_references,
    merge_signal_record,
    resolve_pending_notice_links,
    signal_stage_from_raw,
    strongest_signal_stage,
    sync_signal_notice_links,
)
from app.services.khmdhs_client import KhmdhsClient, build_search_body
from app.services.reports import ReportFilters, query_report_scores


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'signals.sqlite').as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def test_request_search_flags_separate_initial_and_approved_streams():
    initial = build_search_body(
        resource='request', is_initial=True, is_approved=False, is_approval=False
    )
    approved = build_search_body(
        resource='request', is_initial=False, is_approved=True, is_approval=True
    )

    assert initial == {'isInitial': True, 'isApproved': False, 'isApproval': False}
    assert approved == {'isInitial': False, 'isApproved': True, 'isApproval': True}


def test_early_signal_and_search_templates_compile():
    for name in ('base.html', 'signals.html', 'kimdis_search.html', 'admin.html'):
        assert templates.get_template(name) is not None


def test_signal_stage_is_monotonic_and_link_aware():
    initial = {'referenceNumber': '26REQ000000001'}
    approved = {'referenceNumber': '26REQ000000001', '_signal_stage': 'approved'}
    converted = {'referenceNumber': '26REQ000000001', 'noticeRefNo': ['26PROC000000001']}

    merged = merge_signal_record(initial, approved, 'approved')
    merged = merge_signal_record(merged, converted, 'initial')

    assert signal_stage_from_raw(merged) == 'converted'
    assert strongest_signal_stage('converted', 'initial') == 'converted'
    assert linked_notice_references(merged) == ['26PROC000000001']


def test_request_to_notice_links_are_durable_and_resolved(tmp_path):
    db = _db(tmp_path)
    signal = Tender(
        source='khmdhs_request',
        source_reference='26REQ000000001',
        reference_number='26REQ000000001',
        title='Πρωτογενές αίτημα',
        raw={'noticeRefNo': ['26PROC000000001', '26PROC000000002']},
        signal_stage='initial',
    )
    db.add(signal)
    db.flush()

    links = sync_signal_notice_links(db, signal)
    assert signal.signal_stage == 'converted'
    assert {link.related_reference for link in links} == {'26PROC000000001', '26PROC000000002'}
    assert all(link.related_tender_id is None for link in links)

    notice = Tender(
        source='khmdhs_notice',
        source_reference='26PROC000000001',
        reference_number='26PROC000000001',
        title='Διακήρυξη',
        raw={},
    )
    db.add(notice)
    db.flush()
    assert resolve_pending_notice_links(db, [notice]) == 1
    resolved = db.query(TenderLink).filter(TenderLink.related_reference == notice.reference_number).one()
    assert resolved.related_tender_id == notice.id


def test_signal_ingest_fetches_and_links_new_proc(monkeypatch, tmp_path):
    db = _db(tmp_path)
    profile = ClientProfile(slug='it-ingest', name='IT', cpv_codes=['72000000-5'], is_active=True)
    db.add(profile)
    db.flush()

    real_client = KhmdhsClient()

    class FakeClient:
        def __init__(self):
            self.last_pages_fetched = 1
            self.last_rate_limited = False
            self.last_hit_max_pages = False
            self.last_rate_limit_hits = 0
            self.last_transient_error = False
            self.last_transport_error_count = 0
            self.last_cache_hit = False
            self.last_resumed_from_page = 0

        def search_requests(self, **kwargs):
            if kwargs.get('cancel_date_from'):
                return []
            row = {
                'referenceNumber': '26REQ000000010',
                'title': 'Ανάγκη υπηρεσιών IT',
                'objectDetails': [{'cpvs': [{'key': '72000000-5', 'value': 'Υπηρεσίες IT'}]}],
            }
            if not kwargs.get('is_initial'):
                row['approved'] = True
                row['noticeRefNo'] = ['26PROC000000010']
            return [row]

        def search_resource(self, resource, body, **kwargs):
            assert resource == 'notice'
            assert body['referenceNumber'] == '26PROC000000010'
            return [{
                'referenceNumber': '26PROC000000010',
                'title': 'Διακήρυξη υπηρεσιών IT',
                'objectDetails': [{'cpvs': [{'key': '72000000-5', 'value': 'Υπηρεσίες IT'}]}],
            }]

        def normalize_record(self, resource, record):
            return real_client.normalize_record(resource, record)

        def normalize_notice(self, record):
            return real_client.normalize_notice(record)

    monkeypatch.setattr(ingest, 'KhmdhsClient', FakeClient)
    signals, notices, info = ingest.ingest_khmdhs_requests(
        db, [profile], days_back=3, ingest_run_id='signal-run', incremental=False
    )

    assert len(signals) == 1
    assert signals[0].signal_stage == 'converted'
    assert [notice.reference_number for notice in notices] == ['26PROC000000010']
    link = db.query(TenderLink).one()
    assert link.related_tender_id == notices[0].id
    assert info['converted_signals'] == 1
    assert info['links_resolved'] == 1


def test_request_normalization_keeps_explicit_signal_stage():
    normalized = KhmdhsClient().normalize_record(
        'request',
        {
            'referenceNumber': '26REQ000000001',
            'title': 'Αίτημα',
            '_signal_stage': 'approved',
            'objectDetails': [],
        },
    )
    assert normalized['source'] == 'khmdhs_request'
    assert normalized['signal_stage'] == 'approved'
    assert normalized['signal_last_checked_at'] is not None


def test_request_scores_do_not_pollute_dashboard_or_reports(tmp_path):
    db = _db(tmp_path)
    profile = ClientProfile(slug='it', name='IT', cpv_codes=['72000000-5'], is_active=True)
    notice = Tender(
        source='khmdhs_notice', source_reference='proc', reference_number='26PROC000000001',
        title='Notice', cpv_codes=['72000000-5'], raw={},
    )
    signal = Tender(
        source='khmdhs_request', source_reference='req', reference_number='26REQ000000001',
        title='Request', cpv_codes=['72000000-5'], raw={}, signal_stage='initial',
    )
    db.add_all([profile, notice, signal])
    db.flush()
    db.add_all([
        TenderScore(tender_id=notice.id, profile_id=profile.id, score=100, rule_score=100, cpv_match_type='exact_full'),
        TenderScore(tender_id=signal.id, profile_id=profile.id, score=100, rule_score=100, cpv_match_type='exact_full'),
    ])
    db.commit()

    summary = dashboard_summary(db, selected_profile_id=profile.id)
    report_rows = query_report_scores(
        db,
        ReportFilters(profile_id=profile.id, active_only=False, deadline_filter='all'),
    )

    assert summary['total_scores'] == 1
    assert [row.tender.source for row in report_rows] == ['khmdhs_notice']


def test_saved_signal_is_refreshed_once_and_proc_watch_is_inherited(monkeypatch, tmp_path):
    db = _db(tmp_path)
    profiles = [
        ClientProfile(slug='watch-a', name='Watch A', cpv_codes=['72000000-5'], is_active=True),
        ClientProfile(slug='watch-b', name='Watch B', cpv_codes=['48000000-8'], is_active=True),
    ]
    signal = Tender(
        source='khmdhs_request', source_reference='26REQ000000099',
        reference_number='26REQ000000099', title='Αποθηκευμένο αίτημα',
        cpv_codes=['99999999-9'], raw={}, signal_stage='initial',
    )
    db.add_all([*profiles, signal])
    db.flush()
    db.add_all([
        TenderScore(
            tender_id=signal.id, profile_id=profile.id, score=0,
            rule_score=0, cpv_match_type='none', user_status='saved',
            discovery_source='manual',
        )
        for profile in profiles
    ])
    db.commit()

    real_client = KhmdhsClient()

    class FakeClient:
        calls = []
        last_rate_limited = False
        last_transient_error = False

        def search_resource(self, resource, body, **kwargs):
            self.calls.append((resource, body['referenceNumber']))
            if resource == 'request':
                return [{
                    'referenceNumber': '26REQ000000099', 'title': 'Αποθηκευμένο αίτημα',
                    'noticeRefNo': ['26PROC000000099'],
                    'objectDetails': [{'cpvs': [{'key': '99999999-9'}]}],
                }]
            return [{
                'referenceNumber': '26PROC000000099', 'title': 'Νέος συνδεδεμένος διαγωνισμός',
                'objectDetails': [{'cpvs': [{'key': '99999999-9'}]}],
            }]

        def normalize_record(self, resource, record):
            return real_client.normalize_record(resource, record)

        def normalize_notice(self, record):
            return real_client.normalize_notice(record)

    monkeypatch.setattr(ingest, 'KhmdhsClient', FakeClient)
    updated, info = ingest.refresh_manually_watched_acts(db, 'watch-run')

    assert FakeClient.calls == [
        ('request', '26REQ000000099'),
        ('notice', '26PROC000000099'),
    ]
    assert info['watched_unique'] == 1
    assert info['links_resolved'] == 1
    assert info['watches_inherited'] == 2
    notice = db.query(Tender).filter(Tender.reference_number == '26PROC000000099').one()
    inherited = db.query(TenderScore).filter(TenderScore.tender_id == notice.id).all()
    assert {score.profile_id for score in inherited} == {profile.id for profile in profiles}
    assert all(score.user_status == 'saved' for score in inherited)
    assert all(score.discovery_source == 'watched_signal' for score in inherited)
    assert {item.reference_number for item in updated} == {'26REQ000000099', '26PROC000000099'}
