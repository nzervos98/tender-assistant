from app.config import get_settings
from app.main import _LOGIN_FAILURES, _login_is_rate_limited, _record_login_failure, csrf_token
from app.services.auth import verify_csrf_token


class _Client:
    host = '127.0.0.1'


class _Request:
    client = _Client()


def test_csrf_token_is_signed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    request = type('Req', (), {'cookies': {}, 'state': type('State', (), {})()})()
    token = csrf_token({'request': request})

    assert verify_csrf_token(token, 'dev-session-secret-change-me')
    get_settings.cache_clear()


def test_login_rate_limit_blocks_after_configured_failures(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('LOGIN_RATE_LIMIT_ATTEMPTS', '5')
    monkeypatch.setenv('LOGIN_RATE_LIMIT_WINDOW_SECONDS', '300')
    get_settings.cache_clear()
    request = _Request()
    _LOGIN_FAILURES.clear()

    for _ in range(5):
        assert not _login_is_rate_limited(request, 'demo')
        _record_login_failure(request, 'demo')

    assert _login_is_rate_limited(request, 'demo')
    _LOGIN_FAILURES.clear()
    get_settings.cache_clear()
