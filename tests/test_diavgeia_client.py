from datetime import date

import httpx

from app.services.diavgeia_client import DiavgeiaClient, decisions_to_public_dicts, extract_decisions, extract_total, hydrate_decisions, normalize_decision


def test_diavgeia_search_uses_json_accept_and_simple_params(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured['url'] = str(request.url)
        captured['accept'] = request.headers.get('accept')
        return httpx.Response(200, json={'decisions': [], 'info': {'total': 0}})

    client = DiavgeiaClient(base_url='https://diavgeia.test/opendata', http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    payload = client.search(term='26PROC019188090', from_date=date(2026, 6, 1), to_date='2026-06-30', size=5)

    assert payload['decisions'] == []
    assert captured['accept'] == 'application/json'
    assert 'term=26PROC019188090' in captured['url']
    assert 'from_date=2026-06-01' in captured['url']
    assert 'to_date=2026-06-30' in captured['url']
    assert 'size=5' in captured['url']


def test_extract_decisions_handles_known_response_shapes():
    assert extract_decisions({'decisions': [{'ada': 'A'}]}) == [{'ada': 'A'}]
    assert extract_decisions({'decisionSearchResult': {'decisions': {'ada': 'B'}}}) == [{'ada': 'B'}]
    assert extract_decisions({'response': {'docs': [{'ada': 'C'}]}}) == [{'ada': 'C'}]


def test_extract_total_is_tolerant():
    assert extract_total({'info': {'total': '12'}}) == 12
    assert extract_total({'decisionSearchResult': {'info': {'total': 7}}}) == 7
    assert extract_total({'response': {'numFound': '5'}}) == 5
    assert extract_total({}, fallback=3) == 3


def test_normalize_decision_public_summary():
    summary = normalize_decision({
        'ada': 'Ψ123456-ΑΒΓ',
        'subject': 'Απόφαση ανάθεσης προμήθειας',
        'organization': {'uid': '99221990', 'label': 'ΝΟΣΟΚΟΜΕΙΟ'},
        'decisionType': {'uid': 'Β.2.1', 'label': 'ΑΝΑΘΕΣΗ'},
        'issueDate': '1780963200000',
        'submissionTimestamp': '1780994523143',
        'status': 'published',
    })

    assert summary.ada == 'Ψ123456-ΑΒΓ'
    assert summary.organization == 'ΝΟΣΟΚΟΜΕΙΟ'
    assert summary.organization_uid == '99221990'
    assert summary.decision_type == 'ΑΝΑΘΕΣΗ'
    assert summary.decision_type_uid == 'Β.2.1'
    assert summary.issue_date == '2026-06-09'
    assert summary.submission_timestamp.startswith('2026-06-09T11:42:03')
    assert summary.url.endswith('/decision/view/Ψ123456-ΑΒΓ')
    assert summary.api_url.endswith('/luminapi/api/decisions/Ψ123456-ΑΒΓ')


def test_decisions_to_public_dicts_hides_raw_payload():
    items = decisions_to_public_dicts([{'ada': 'A', 'subject': 'Θέμα', 'secretish': 'raw'}])
    assert items == [{
        'ada': 'A',
        'subject': 'Θέμα',
        'organization': '',
        'organization_uid': '',
        'decision_type': '',
        'decision_type_uid': '',
        'issue_date': '',
        'submission_timestamp': '',
        'status': '',
        'url': 'https://diavgeia.gov.gr/decision/view/A',
        'api_url': 'https://diavgeia.gov.gr/luminapi/api/decisions/A',
    }]



def test_hydrate_decisions_merges_detail_payload(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={
            'ada': 'ΑΔΑ1',
            'subject': 'Λεπτομερές θέμα',
            'organizationLabel': 'ΓΕΝΙΚΟ ΝΟΣΟΚΟΜΕΙΟ',
            'organizationUid': '9922',
            'decisionTypeLabel': 'ΑΝΑΘΕΣΗ',
            'decisionTypeUid': 'Β.2.3',
        })

    client = DiavgeiaClient(base_url='https://diavgeia.test/opendata', http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    hydrated = hydrate_decisions(client, [{'ada': 'ΑΔΑ1', 'subject': 'Αρχικό θέμα'}])
    summary = normalize_decision(hydrated[0])

    assert calls == ['https://diavgeia.test/opendata/decisions/%CE%91%CE%94%CE%911/']
    assert summary.subject == 'Λεπτομερές θέμα'
    assert summary.organization == 'ΓΕΝΙΚΟ ΝΟΣΟΚΟΜΕΙΟ'
    assert summary.organization_uid == '9922'
    assert summary.decision_type == 'ΑΝΑΘΕΣΗ'
    assert summary.decision_type_uid == 'Β.2.3'
