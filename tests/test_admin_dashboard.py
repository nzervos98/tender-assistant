import sys
import types
from pathlib import Path

sys.modules.setdefault('feedparser', types.SimpleNamespace(parse=lambda *args, **kwargs: types.SimpleNamespace(entries=[])))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import admin_overview_summary
from app.models import AppUser, ClientProfile, Tender, TenderScore


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_admin_overview_groups_users_profiles_and_profile_checks():
    db = _session()
    admin = AppUser(username='admin', password_hash='x', role='admin', is_active=True)
    user = AppUser(username='user', password_hash='x', role='user', is_active=True)
    inactive_user = AppUser(username='inactive', password_hash='x', role='user', is_active=False)
    db.add_all([admin, user, inactive_user])
    db.flush()
    db.add_all(
        [
            ClientProfile(slug='owned', name='Owned', owner_user_id=user.id, cpv_codes=['33000000-0'], is_active=True),
            ClientProfile(slug='missing-cpv', name='Missing CPV', owner_user_id=user.id, cpv_codes=[], is_active=True),
            ClientProfile(slug='orphan', name='Orphan', owner_user_id=None, cpv_codes=['33790000-4'], is_active=True),
            ClientProfile(slug='inactive', name='Inactive', owner_user_id=user.id, cpv_codes=['15000000-8'], is_active=False),
        ]
    )
    db.commit()

    overview = admin_overview_summary(db)

    assert overview['users_total'] == 3
    assert overview['active_users'] == 2
    assert overview['admin_users'] == 1
    assert overview['active_profiles'] == 3
    assert [profile.name for profile in overview['profiles_without_owner']] == ['Orphan']
    assert [profile.name for profile in overview['active_profiles_without_cpv']] == ['Missing CPV']
    assert [profile.name for profile in overview['inactive_profiles']] == ['Inactive']
    assert {row['user'].username: row['profiles'] for row in overview['user_rows']}['user'] == 3


def test_admin_template_exposes_cockpit_actions():
    template = Path('app/templates/admin.html').read_text(encoding='utf-8')
    main = Path('app/main.py').read_text(encoding='utf-8')

    assert "@app.get('/admin'" in main
    assert "url='/admin' if user.is_admin else '/'" in main
    assert '/?profile_id=0' in template
    assert '/admin/users' in template
    assert '/maintenance' in template
    assert '/ingest/run' in template
    assert '/rescore/run' in template


def test_admin_overview_separates_notices_from_early_signals():
    db = _session()
    profile = ClientProfile(slug='profile', name='Profile', cpv_codes=['72000000-5'], is_active=True)
    db.add(profile)
    db.flush()
    notice = Tender(source='khmdhs_notice', source_reference='26PROC000000001', title='Notice')
    signal = Tender(source='khmdhs_request', source_reference='26REQ000000001', title='Request', signal_stage='initial')
    db.add_all([notice, signal])
    db.flush()
    db.add_all([
        TenderScore(tender_id=notice.id, profile_id=profile.id, score=90, rule_score=90, is_new_in_latest_ingest=True),
        TenderScore(tender_id=signal.id, profile_id=profile.id, score=85, rule_score=85, is_new_in_latest_ingest=True, user_status='saved'),
    ])
    db.commit()

    overview = admin_overview_summary(db)

    assert overview['tenders'] == 1
    assert overview['signals'] == 1
    assert overview['scores'] == 1
    assert overview['signal_scores'] == 1
    assert overview['latest_new'] == 1
    assert overview['saved_count'] == 0
    assert overview['signal_saved_count'] == 1
