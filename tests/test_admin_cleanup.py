from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import AppUser, ClientProfile, Tender, TenderLink, TenderScore
from app.services.admin_cleanup import admin_cleanup_plan, execute_admin_cleanup


def _db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_profile_cleanup_keeps_shared_and_signal_linked_tenders():
    db = _db()
    owner = AppUser(username='owner', password_hash='x', role='user', is_active=True)
    other = AppUser(username='other', password_hash='x', role='user', is_active=True)
    db.add_all([owner, other])
    db.flush()
    profile = ClientProfile(slug='profile-clean', name='Profile to clean', owner_user_id=owner.id, cpv_codes=['72000000-5'])
    other_profile = ClientProfile(slug='profile-keep', name='Keep profile', owner_user_id=other.id, cpv_codes=['03000000-1'])
    db.add_all([profile, other_profile])
    db.flush()

    orphan = Tender(source='khmdhs_notice', source_reference='orphan', title='Unlinked')
    shared = Tender(source='khmdhs_notice', source_reference='shared', title='Shared')
    linked = Tender(source='khmdhs_notice', source_reference='linked', title='Linked from signal')
    signal = Tender(source='khmdhs_request', source_reference='req', title='Signal', signal_stage='converted')
    db.add_all([orphan, shared, linked, signal])
    db.flush()
    db.add_all([
        TenderScore(tender_id=orphan.id, profile_id=profile.id, score=90, rule_score=90),
        TenderScore(tender_id=shared.id, profile_id=profile.id, score=80, rule_score=80),
        TenderScore(tender_id=shared.id, profile_id=other_profile.id, score=80, rule_score=80),
        TenderScore(tender_id=linked.id, profile_id=profile.id, score=75, rule_score=75),
    ])
    db.add(TenderLink(
        source_tender_id=signal.id,
        related_tender_id=linked.id,
        relation_type='request_to_notice',
        related_reference=linked.source_reference,
    ))
    db.commit()

    plan = admin_cleanup_plan(db, 'profile', profile.id)
    assert plan['score_count'] == 3
    assert plan['candidate_tender_count'] == 3
    assert plan['orphan_tender_count'] == 1
    assert plan['preserved_tender_count'] == 2

    result = execute_admin_cleanup(db, 'profile', profile.id)
    db.commit()

    assert result['score_count'] == 3
    assert result['orphan_tender_count'] == 1
    assert db.get(Tender, orphan.id) is None
    assert db.get(Tender, shared.id) is not None
    assert db.get(Tender, linked.id) is not None
    assert db.query(TenderScore).filter_by(tender_id=shared.id, profile_id=other_profile.id).count() == 1
    assert db.query(TenderScore).filter(TenderScore.profile_id == profile.id).count() == 0
    assert db.get(ClientProfile, profile.id) is not None


def test_user_cleanup_covers_all_owned_profiles_but_keeps_user_and_profile_settings():
    db = _db()
    user = AppUser(username='cleanup-user', password_hash='x', role='user', is_active=True)
    db.add(user)
    db.flush()
    profiles = [
        ClientProfile(slug='user-p1', name='Profile 1', owner_user_id=user.id, cpv_codes=['72000000-5']),
        ClientProfile(slug='user-p2', name='Profile 2', owner_user_id=user.id, cpv_codes=['48000000-8']),
    ]
    db.add_all(profiles)
    db.flush()
    tenders = [
        Tender(source='khmdhs_notice', source_reference=f'user-{i}', title=f'Tender {i}')
        for i in range(2)
    ]
    db.add_all(tenders)
    db.flush()
    db.add_all([
        TenderScore(tender_id=tenders[i].id, profile_id=profiles[i].id, score=70, rule_score=70)
        for i in range(2)
    ])
    db.commit()

    plan = admin_cleanup_plan(db, 'user', user.id)
    assert plan['profile_count'] == 2
    assert plan['score_count'] == 2
    assert plan['orphan_tender_count'] == 2

    execute_admin_cleanup(db, 'user', user.id)
    db.commit()

    assert db.get(AppUser, user.id) is not None
    assert db.query(ClientProfile).filter(ClientProfile.owner_user_id == user.id).count() == 2
    assert db.query(TenderScore).count() == 0
    assert db.query(Tender).count() == 0


def test_admin_profile_is_excluded_from_profile_cleanup_but_included_in_user_cleanup():
    db = _db()
    admin = AppUser(username='admin-cleanup', password_hash='x', role='admin', is_active=True)
    db.add(admin)
    db.flush()
    profile = ClientProfile(slug='admin-profile', name='Admin profile', owner_user_id=admin.id, cpv_codes=['72000000-5'])
    tender = Tender(source='khmdhs_notice', source_reference='admin-tender', title='Admin tender')
    db.add_all([profile, tender])
    db.flush()
    db.add(TenderScore(tender_id=tender.id, profile_id=profile.id, score=80, rule_score=80))
    db.commit()

    try:
        admin_cleanup_plan(db, 'profile', profile.id)
    except ValueError as exc:
        assert 'απλού χρήστη' in str(exc)
    else:
        raise AssertionError('Admin profiles must not be eligible for profile-scoped cleanup')

    plan = admin_cleanup_plan(db, 'user', admin.id)
    assert plan['profile_ids'] == [profile.id]
    assert plan['score_count'] == 1
