"""Session auth for the bridge.

The printer has no authentication. The bridge adds one, because it is the piece that
can be reached from outside the house. Nothing fancy: one password, an HMAC-signed
cookie, a login rate limiter, and a same-origin check on mutations.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from .config import Config

MAX_ATTEMPTS = 5
ATTEMPT_WINDOW_S = 300.0

# The Pi's own screen has no keyboard, so making it type the password leaves the
# dashboard unusable at boot. Requests that never left the machine can count as
# signed in, but only when the operator asks for it (EC_TRUST_LOOPBACK).
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
# A tunnel or reverse proxy connects to the bridge over loopback, so its requests look
# local. Any of these headers proves the request came from somewhere else.
PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-host", "forwarded")
_attempts: dict[str, deque[float]] = defaultdict(deque)


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def issue_token(config: Config, *, now: float | None = None) -> str:
    expires = int((now or time.time()) + config.session_ttl_s)
    return f"{expires}.{_sign(config.secret, str(expires))}"


def verify_token(config: Config, token: str | None, *, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    raw_expiry, _, signature = token.partition(".")
    try:
        expires = int(raw_expiry)
    except ValueError:
        return False
    if expires < (now or time.time()):
        return False
    return hmac.compare_digest(signature, _sign(config.secret, raw_expiry))


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_allowed(key: str) -> bool:
    window = _attempts[key]
    now = time.time()
    while window and now - window[0] > ATTEMPT_WINDOW_S:
        window.popleft()
    return len(window) < MAX_ATTEMPTS


def record_failed_login(key: str) -> None:
    _attempts[key].append(time.time())


def reset_login_attempts(key: str) -> None:
    _attempts.pop(key, None)


def check_password(config: Config, password: str) -> bool:
    if not config.password:
        return True
    return hmac.compare_digest(password, config.password)


def is_local_request(request: Request) -> bool:
    """True when the request came from the Pi itself, not through a proxy or tunnel."""
    host = request.client.host if request.client else ""
    if host not in LOOPBACK_HOSTS:
        return False
    return not any(name in request.headers for name in PROXY_HEADERS)


def is_authenticated(config: Config, request: Request) -> bool:
    if not config.auth_enabled:
        return True
    if config.trust_loopback and is_local_request(request):
        return True
    return verify_token(config, request.cookies.get(config.cookie_name))


def require_session(config: Config):
    """FastAPI dependency: 401 unless the caller holds a valid session cookie."""

    async def dependency(request: Request) -> None:
        if not is_authenticated(config, request):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")

    return dependency


def require_same_origin(request: Request) -> None:
    """Reject cross-site writes.

    A valid cookie is not enough: without this, any page the operator visits could POST
    a form to the bridge. Browsers always send Origin on cross-origin POSTs, so a
    mismatch (or a foreign Host) is refused.
    """
    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer")
        if not referer:
            return  # non-browser client (curl, scripts); cookie still required
        origin = referer
    host = request.headers.get("host", "")
    allowed = {f"http://{host}", f"https://{host}"}
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        allowed |= {f"https://{forwarded}", f"http://{forwarded}"}
    if origin.rstrip("/") not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-origin request refused")
