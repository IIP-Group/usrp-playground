"""
Admin authentication via HTTP-only session cookie.

Credentials are read from the .env:
    ADMIN_USERNAME=admin
    ADMIN_PASSWORD=...
    ADMIN_SESSION_SECRET=...   # HMAC key for the session cookies

The session cookie carries an HMAC-signed token plus expiry — no DB
round-trip is needed for auth.
"""
import os
import hmac
import time
import base64
import hashlib
import secrets
from fastapi import Request, HTTPException, Response


SESSION_COOKIE_NAME = "admin_session"
SESSION_TTL_SEC = 60 * 60 * 8  # 8 Stunden


def _secret() -> bytes:
    return os.getenv("ADMIN_SESSION_SECRET", "change-me-admin-secret").encode("utf-8")


def _admin_user() -> str:
    return os.getenv("ADMIN_USERNAME", "admin")


def _admin_pass() -> str:
    return os.getenv("ADMIN_PASSWORD", "")


def _sign(payload: bytes) -> str:
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload).decode() + "." + \
           base64.urlsafe_b64encode(sig).decode()


def _verify(token: str) -> dict | None:
    try:
        p_b64, sig_b64 = token.split(".")
        payload = base64.urlsafe_b64decode(p_b64)
        sig = base64.urlsafe_b64decode(sig_b64)
        expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        exp_str, username = payload.decode().split("|", 1)
        if int(exp_str) < int(time.time()):
            return None
        return {"username": username, "exp": int(exp_str)}
    except Exception:
        return None


def check_credentials(username: str, password: str) -> bool:
    """Constant-time compare."""
    admin_u = _admin_user()
    admin_p = _admin_pass()
    if not admin_p:
        return False
    u_ok = hmac.compare_digest(username.encode(), admin_u.encode())
    p_ok = hmac.compare_digest(password.encode(), admin_p.encode())
    return u_ok and p_ok


def issue_session(response: Response, username: str) -> str:
    exp = int(time.time()) + SESSION_TTL_SEC
    payload = f"{exp}|{username}".encode()
    token = _sign(payload)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SEC,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return token


def clear_session(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


def require_admin(request: Request) -> dict:
    """Dependency: raises 401 if not admin."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = _verify(token)
    if not session:
        raise HTTPException(status_code=401, detail="Session expired")
    return session
