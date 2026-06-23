from pathlib import Path


def test_tender_delete_endpoint_and_ui_buttons_exist():
    main = Path('app/main.py').read_text(encoding='utf-8')
    dashboard = Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    tender = Path('app/templates/tender.html').read_text(encoding='utf-8')
    base = Path('app/templates/base.html').read_text(encoding='utf-8')

    assert "@app.post('/tenders/{tender_id}/delete'" in main
    assert "db.delete(tender)" in main
    assert "event_type='tender_deleted'" in main
    assert "not return_to.startswith('//')" in main

    assert '/tenders/{{ s.tender.id }}/delete' in dashboard
    assert '/tenders/{{ tender.id }}/delete' in tender
    assert 'Οριστική διαγραφή από τη βάση' in dashboard
    assert 'Οριστική διαγραφή από τη βάση' in tender
    assert 'button.trash' in base
