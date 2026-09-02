import httpx

from app.services import khmdhs_client
from app.services.khmdhs_client import KhmdhsClient


class _FakeHttpClient:
    def __init__(self, post):
        self._post = post

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, json):
        return self._post(url, json)


def test_khmdhs_read_timeout_is_retried_and_page_succeeds(monkeypatch):
    calls = 0

    def post(url, _body):
        nonlocal calls
        calls += 1
        request = httpx.Request('POST', url)
        if calls == 1:
            raise httpx.ReadTimeout('slow response', request=request)
        return httpx.Response(
            200,
            request=request,
            json={'content': [{'referenceNumber': 'A'}], 'last': True, 'totalPages': 1},
        )

    client = KhmdhsClient()
    monkeypatch.setattr(client.settings, 'khmdhs_transport_retries', 2)
    monkeypatch.setattr(client.settings, 'khmdhs_transport_base_delay_seconds', 0.1)
    monkeypatch.setattr(client.rate_limiter, 'wait', lambda: None)
    monkeypatch.setattr(khmdhs_client.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(khmdhs_client.httpx, 'Client', lambda **_kwargs: _FakeHttpClient(post))

    records = client.search_resource('notice', {'dateFrom': '2026-09-01', 'dateTo': '2026-09-02'})

    assert records == [{'referenceNumber': 'A'}]
    assert calls == 2
    assert client.last_transport_error_count == 1
    assert client.last_transient_error is False


def test_exhausted_read_timeouts_are_deferred_to_self_continuation(monkeypatch):
    calls = 0

    def post(url, _body):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout('still slow', request=httpx.Request('POST', url))

    client = KhmdhsClient()
    monkeypatch.setattr(client.settings, 'khmdhs_transport_retries', 2)
    monkeypatch.setattr(client.settings, 'khmdhs_transport_base_delay_seconds', 0.1)
    monkeypatch.setattr(client.rate_limiter, 'wait', lambda: None)
    monkeypatch.setattr(khmdhs_client.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(khmdhs_client.httpx, 'Client', lambda **_kwargs: _FakeHttpClient(post))

    records = client.search_resource('notice', {'dateFrom': '2026-09-01', 'dateTo': '2026-09-02'})

    assert records == []
    assert calls == 3
    assert client.last_transport_error_count == 3
    assert client.last_transient_error is True
