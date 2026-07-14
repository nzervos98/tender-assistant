from pathlib import Path

import sys
import types

sys.modules.setdefault('feedparser', types.SimpleNamespace(parse=lambda *args, **kwargs: types.SimpleNamespace(entries=[])))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import delete_tender
from app.models import AppUser, ClientProfile, Tender, TenderScore


def test_tender_delete_endpoint_and_ui_buttons_exist():
    main = Path('app/main.py').read_text(encoding='utf-8')
    dashboard = Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    kimdis = Path('app/templates/kimdis_search.html').read_text(encoding='utf-8')
    tender = Path('app/templates/tender.html').read_text(encoding='utf-8')
    base = Path('app/templates/base.html').read_text(encoding='utf-8')

    assert "@app.post('/tenders/{tender_id}/delete'" in main
    assert "db.delete(tender)" in main
    assert "event_type='tender_deleted'" in main
    assert "value.startswith('//')" in main

    assert '/tenders/{{ s.tender.id }}/delete' not in dashboard
    assert '/tenders/{{ item.saved_id }}/delete' in kimdis
    assert '/tenders/{{ tender.id }}/delete' in tender
    assert 'Οριστική διαγραφή από τη βάση' in kimdis
    assert 'Οριστική διαγραφή από τη βάση' in tender
    assert 'button.trash' in base


def test_non_admin_delete_removes_only_owned_saved_score_when_shared():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    user_a = AppUser(username='user-a', password_hash='x', role='user', is_active=True)
    user_b = AppUser(username='user-b', password_hash='x', role='user', is_active=True)
    db.add_all([user_a, user_b])
    db.flush()
    profile_a = ClientProfile(slug='a', name='A', owner_user_id=user_a.id, cpv_codes=[], is_active=True)
    profile_b = ClientProfile(slug='b', name='B', owner_user_id=user_b.id, cpv_codes=[], is_active=True)
    db.add_all([profile_a, profile_b])
    db.flush()
    tender = Tender(source='khmdhs_notice', source_reference='ref', title='Shared tender')
    db.add(tender)
    db.flush()
    score_a = TenderScore(tender_id=tender.id, profile_id=profile_a.id, score=80, user_status='saved')
    score_b = TenderScore(tender_id=tender.id, profile_id=profile_b.id, score=70, user_status='saved')
    db.add_all([score_a, score_b])
    db.commit()

    request = type('Req', (), {'state': type('State', (), {'current_user': user_a})()})()
    response = delete_tender(request, tender.id, return_to='/kimdis', db=db)

    assert response.status_code == 303
    assert db.query(Tender).filter(Tender.id == tender.id).one_or_none() is not None
    assert db.query(TenderScore).filter(TenderScore.id == score_a.id).one_or_none() is None
    assert db.query(TenderScore).filter(TenderScore.id == score_b.id).one_or_none() is not None


def test_non_admin_delete_rejects_unowned_tender():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    user_a = AppUser(username='user-a', password_hash='x', role='user', is_active=True)
    user_b = AppUser(username='user-b', password_hash='x', role='user', is_active=True)
    db.add_all([user_a, user_b])
    db.flush()
    profile_b = ClientProfile(slug='b', name='B', owner_user_id=user_b.id, cpv_codes=[], is_active=True)
    db.add(profile_b)
    db.flush()
    tender = Tender(source='khmdhs_notice', source_reference='ref', title='Other tender')
    db.add(tender)
    db.flush()
    db.add(TenderScore(tender_id=tender.id, profile_id=profile_b.id, score=70, user_status='saved'))
    db.commit()

    request = type('Req', (), {'state': type('State', (), {'current_user': user_a})()})()
    try:
        delete_tender(request, tender.id, return_to='/kimdis', db=db)
    except Exception as exc:
        assert getattr(exc, 'status_code', None) == 404
    else:
        raise AssertionError('unowned tender delete should be rejected')
