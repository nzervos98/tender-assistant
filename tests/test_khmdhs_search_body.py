from app.services.khmdhs_client import build_search_body


def test_reference_lookup_body_has_no_dates_when_blank():
    body = build_search_body(resource='notice', reference_number='26PROC019063961')
    assert body == {'isModified': False, 'referenceNumber': '26PROC019063961'}


def test_blank_dates_are_not_sent_to_khmdhs():
    body = build_search_body(resource='notice', date_from='', date_to='', final_date_from='', final_date_to='')
    assert 'dateFrom' not in body
    assert 'dateTo' not in body
    assert 'finalDateFrom' not in body
    assert 'finalDateTo' not in body


def test_final_date_date_only_expands_to_full_day():
    body = build_search_body(resource='notice', final_date_from='2026-06-01', final_date_to='2026-06-10')
    assert body['finalDateFrom'] == '2026-06-01 00:00'
    assert body['finalDateTo'] == '2026-06-10 23:59'
