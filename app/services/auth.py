from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone


PBKDF2_ITERATIONS = 260_000
SESSION_COOKIE = 'tender_session'


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('ascii'), PBKDF2_ITERATIONS)
    return f'pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}'


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        algorithm, iterations_text, salt, expected = password_hash.split('$', 3)
        iterations = int(iterations_text)
    except ValueError:
        return False
    if algorithm != 'pbkdf2_sha256':
        return False
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('ascii'), iterations).hex()
    return hmac.compare_digest(digest, expected)


def _sign(value: str, secret_key: str) -> str:
    return hmac.new(secret_key.encode('utf-8'), value.encode('utf-8'), hashlib.sha256).hexdigest()


def make_session_token(user_id: int, secret_key: str) -> str:
    issued_at = int(datetime.now(timezone.utc).timestamp())
    payload = f'{user_id}:{issued_at}:{secrets.token_hex(8)}'
    encoded = base64.urlsafe_b64encode(payload.encode('utf-8')).decode('ascii').rstrip('=')
    return f'{encoded}.{_sign(encoded, secret_key)}'


def parse_session_token(token: str | None, secret_key: str, max_age_seconds: int | None = None) -> int | None:
    if not token or '.' not in token or not secret_key:
        return None
    encoded, signature = token.rsplit('.', 1)
    if not hmac.compare_digest(_sign(encoded, secret_key), signature):
        return None
    try:
        padding = '=' * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode((encoded + padding).encode('ascii')).decode('utf-8')
        parts = payload.split(':', 2)
        user_id_text = parts[0]
        if max_age_seconds is not None and max_age_seconds > 0:
            issued_at = int(parts[1])
            now = int(datetime.now(timezone.utc).timestamp())
            if issued_at > now or now - issued_at > max_age_seconds:
                return None
        return int(user_id_text)
    except (IndexError, ValueError, UnicodeDecodeError):
        return None
