import sys
import types
from datetime import timedelta

sys.modules.setdefault('feedparser', types.SimpleNamespace(parse=lambda *args, **kwargs: types.SimpleNamespace(entries=[])))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import _default_dashboard_profile_id, _filter_scores_for_user, _get_visible_profile, _visible_profile_ids, _visible_profiles_query, dashboard_summary
from app.models import AppUser, ClientProfile, Tender, TenderScore
from app.services.reports import ReportFilters, query_report_scores
from app.services.timezone import now_utc


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_two_users(db):
    user_a = AppUser(username='user-a', password_hash='x', role='user', is_active=True)
    user_b = AppUser(username='user-b', password_hash='x', role='user', is_active=True)
    admin = AppUser(username='admin', password_hash='x', role='admin', is_active=True)
    db.add_all([user_a, user_b, admin])
    db.flush()

    profile_a = ClientProfile(slug='profile-a', name='Profile A', owner_user_id=user_a.id, cpv_codes=['33000000-0'], is_active=True)
    profile_b = ClientProfile(slug='profile-b', name='Profile B', owner_user_id=user_b.id, cpv_codes=['33790000-4'], is_active=True)
    db.add_all([profile_a, profile_b])
    db.flush()

    tender_a = Tender(
        source='khmdhs_notice',
        source_reference='A',
        reference_number='26PROCA',
        title='Tender A',
        organization_name='Org A',
        final_submission_date=now_utc() + timedelta(days=3),
        cpv_codes=['33000000-0'],
    )
    tender_b = Tender(
        source='khmdhs_notice',
        source_reference='B',
        reference_number='26PROCB',
        title='Tender B',
        organization_name='Org B',
        final_submission_date=now_utc() + timedelta(days=3),
        cpv_codes=['33790000-4'],
    )
    db.add_all([tender_a, tender_b])
    db.flush()

    score_a = TenderScore(profile_id=profile_a.id, tender_id=tender_a.id, score=80, rule_score=80, user_status='new')
    score_b = TenderScore(profile_id=profile_b.id, tender_id=tender_b.id, score=90, rule_score=90, user_status='new')
    db.add_all([score_a, score_b])
    db.commit()
    return user_a, user_b, admin, profile_a, profile_b, tender_a, tender_b


def test_non_admin_sees_only_their_profiles():
    db = _session()
    user_a, user_b, admin, profile_a, profile_b, *_ = _seed_two_users(db)

    assert _visible_profile_ids(db, user_a) == [profile_a.id]
    assert _visible_profile_ids(db, user_b) == [profile_b.id]
    assert set(_visible_profile_ids(db, admin)) == {profile_a.id, profile_b.id}

    assert _get_visible_profile(db, user_a, profile_a.id).id == profile_a.id
    assert _get_visible_profile(db, user_a, profile_b.id) is None


def test_dashboard_defaults_to_all_profiles_for_admin_only():
    db = _session()
    user_a, _user_b, admin, profile_a, profile_b, *_ = _seed_two_users(db)

    assert _default_dashboard_profile_id(admin, [profile_a, profile_b], '') is None
    assert _default_dashboard_profile_id(user_a, [profile_a, profile_b], '') == profile_a.id
    assert _default_dashboard_profile_id(admin, [profile_a, profile_b], str(profile_b.id)) == profile_b.id


def test_non_admin_score_queries_are_limited_to_owned_profiles():
    db = _session()
    user_a, user_b, admin, profile_a, profile_b, tender_a, tender_b = _seed_two_users(db)

    rows_for_a = _filter_scores_for_user(db.query(TenderScore), user_a).order_by(TenderScore.id.asc()).all()
    rows_for_b = _filter_scores_for_user(db.query(TenderScore), user_b).order_by(TenderScore.id.asc()).all()
    rows_for_admin = _filter_scores_for_user(db.query(TenderScore), admin).order_by(TenderScore.id.asc()).all()

    assert [row.profile_id for row in rows_for_a] == [profile_a.id]
    assert [row.profile_id for row in rows_for_b] == [profile_b.id]
    assert [row.profile_id for row in rows_for_admin] == [profile_a.id, profile_b.id]

    summary_a = dashboard_summary(db, selected_profile_id=None, user=user_a)
    summary_b = dashboard_summary(db, selected_profile_id=None, user=user_b)
    assert summary_a['db_matches'] == 1
    assert summary_b['db_matches'] == 1


def test_reports_can_be_limited_to_current_users_profile_ids():
    db = _session()
    user_a, user_b, _admin, profile_a, profile_b, *_ = _seed_two_users(db)

    rows_for_a = query_report_scores(db, ReportFilters(profile_ids=_visible_profile_ids(db, user_a), min_score=55, active_only=True))
    rows_for_b = query_report_scores(db, ReportFilters(profile_ids=_visible_profile_ids(db, user_b), min_score=55, active_only=True))

    assert [row.profile_id for row in rows_for_a] == [profile_a.id]
    assert [row.profile_id for row in rows_for_b] == [profile_b.id]
