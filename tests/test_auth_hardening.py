from app.config import get_settings
from app.main import _session_cookie_secure, _session_secret
from app.services.auth import password_meets_policy


def test_production_requires_explicit_session_secret(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.delenv('SESSION_SECRET_KEY', raising=False)
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)
    monkeypatch.delenv('BOOTSTRAP_ADMIN_PASSWORD', raising=False)

    try:
        _session_secret()
    except RuntimeError as exc:
        assert 'SESSION_SECRET_KEY is required' in str(exc)
    else:
        raise AssertionError('production should require SESSION_SECRET_KEY')
    finally:
        get_settings.cache_clear()


def test_explicit_session_secret_is_used(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('SESSION_SECRET_KEY', 'not-the-default-secret')

    assert _session_secret() == 'not-the-default-secret'
    get_settings.cache_clear()


def test_password_policy_checks_minimum_length():
    assert password_meets_policy('long-enough-pass', 12)
    assert not password_meets_policy('short', 12)


def test_production_uses_secure_session_cookie(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('SESSION_COOKIE_SECURE', 'false')

    assert _session_cookie_secure()
    get_settings.cache_clear()
