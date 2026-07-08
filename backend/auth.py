"""Simple single-user session auth.

Credentials come from .env (APP_USERNAME / APP_PASSWORD). Sessions are
HMAC-signed tokens in an HttpOnly cookie, so they survive server restarts
without any session table.
"""
import hashlib
import hmac
import os
import secrets
import time

APP_USERNAME = os.environ.get("APP_USERNAME", "Akshay")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "Puli123")

SESSION_COOKIE = "ap_session"
SESSION_TTL = 30 * 24 * 3600  # 30 days

from .db import DATA_DIR

_SECRET_FILE = DATA_DIR / ".session_secret"


def _secret() -> bytes:
    env_secret = os.environ.get("SESSION_SECRET")
    if env_secret:
        return env_secret.encode()
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_bytes()
    _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    _SECRET_FILE.write_bytes(secret)
    _SECRET_FILE.chmod(0o600)
    return secret


def check_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username.strip(), APP_USERNAME) and hmac.compare_digest(
        password, APP_PASSWORD
    )


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def create_token(username: str) -> str:
    payload = f"{username}.{int(time.time()) + SESSION_TTL}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        username, expiry, signature = token.rsplit(".", 2)
        payload = f"{username}.{expiry}"
        if not hmac.compare_digest(signature, _sign(payload)):
            return False
        return int(expiry) > time.time()
    except (ValueError, TypeError):
        return False
