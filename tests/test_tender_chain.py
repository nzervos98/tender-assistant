import httpx

from app.main import load_tender_chain, normalize_chain_items, stored_tender_chain_items
from app.models import Tender


LIVE_SHAPE = {
    'requests': ['26REQ019693650'],
    'approvedRequests': ['26REQ019698501'],
    'notices': ['26PROC019699496'],
    'auctions': [],
    'contracts': [],
    'payments': [],
}

REQUEST_RECORD = {
    'title': 'Απόφαση έγκρισης',
    'referenceNumber': '26REQ019698501',
    'previousRequestReferenceNumber': '26REQ019693650',
    'approved': True,
    'submissionDate': '2026-08-31T13:10:31.428',
    'noticeRefNo': ['26PROC019699496'],
    'contractRefNo': [],
    'paymentRefNo': [],
}


class _ChainClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.reference = None
        self.timeout_seconds = None

    def adam_chain(self, reference, *, timeout_seconds):
        self.reference = reference
        self.timeout_seconds = timeout_seconds
        if self.error:
            raise self.error
        return self.payload

    def request_by_reference(self, reference, *, timeout_seconds):
        self.reference = reference
        self.timeout_seconds = timeout_seconds
        if self.error:
            raise self.error
        return self.payload


def _notice():
    return Tender(
        source='khmdhs_notice',
        source_reference='26PROC019699496',
        reference_number='26PROC019699496',
        title='Cybersecurity',
        raw={'approvedRequests': [{'code': '26REQ019698501'}]},
    )


def test_normalize_chain_accepts_live_string_array_shape():
    items = normalize_chain_items(LIVE_SHAPE)

    assert [(item['stage'], item['reference']) for item in items] == [
        ('Αρχικό αίτημα', '26REQ019693650'),
        ('Εγκεκριμένο αίτημα', '26REQ019698501'),
        ('Διακήρυξη / πρόσκληση', '26PROC019699496'),
    ]


def test_notice_chain_uses_stored_approved_request_as_fast_seed():
    client = _ChainClient(REQUEST_RECORD)

    items, error = load_tender_chain(_notice(), client=client)

    assert error is None
    assert client.reference == '26REQ019698501'
    assert client.timeout_seconds == 4
    assert {item['reference'] for item in items} == {
        '26REQ019693650', '26REQ019698501', '26PROC019699496',
    }
    assert [item['reference'] for item in items] == [
        '26REQ019693650', '26REQ019698501', '26PROC019699496',
    ]


def test_chain_timeout_returns_stored_relation_instead_of_empty_result():
    tender = _notice()
    client = _ChainClient(error=httpx.ReadTimeout('slow KIMDIS'))

    items, error = load_tender_chain(tender, client=client)

    assert stored_tender_chain_items(tender) == items
    assert [item['reference'] for item in items] == ['26REQ019698501']
    assert error is None


def test_empty_request_response_keeps_stored_relation_without_warning():
    tender = _notice()
    client = _ChainClient({})

    items, error = load_tender_chain(tender, client=client)

    assert [item['reference'] for item in items] == ['26REQ019698501']
    assert items[0]['stage'] == 'Εγκεκριμένο αίτημα'
    assert error is None


def test_chain_without_stored_links_reports_timeout():
    tender = Tender(
        source='khmdhs_notice', source_reference='26PROC000000001',
        reference_number='26PROC000000001', title='No stored links', raw={},
    )
    client = _ChainClient(error=httpx.ReadTimeout('slow KIMDIS'))

    items, error = load_tender_chain(tender, client=client)

    assert items == []
    assert 'καθυστέρησε' in error
