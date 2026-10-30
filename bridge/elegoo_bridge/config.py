"""Config for the Elegoo bridge.

Everything is environment-driven so the systemd unit is the only place that holds
deployment-specific values (and the password is never in git).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "elegoo-bridge"


@dataclass(slots=True)
class Config:
    printer_host: str
    mainboard_id: str | None
    bind: str
    port: int
    control: bool
    poll_ms: int
    push_ms: int
    camera_port: int
    state_dir: Path
    password: str | None
    admin_user: str
    session_ttl_s: int
    web_dir: Path
    log_level: str
    upload_max_bytes: int
    # Derived.
    auth_enabled: bool = field(init=False)

    def __post_init__(self) -> None:
        self.auth_enabled = bool(self.password)

    @property
    def cookie_name(self) -> str:
        return "ec_session"

    @property
    def secret(self) -> bytes:
        """Stable secret for signing session cookies.

        Derived from the password + machine identity so it survives restarts but is
        not stored on disk in plaintext. If auth is off the secret is irrelevant.
        """
        seed = f"{self.password or ''}:{self.printer_host}".encode()
        return hashlib.sha256(b"elegoo-bridge-session-v1:" + seed).digest()

    @property
    def cache_file(self) -> Path:
        return self.state_dir / "printer.json"


def load_config() -> Config:
    here = Path(__file__).resolve().parent
    web_dir = Path(os.environ.get("EC_WEB_DIR") or (here.parent.parent / "web"))
    return Config(
        printer_host=os.environ.get("EC_PRINTER_HOST", "").strip(),
        mainboard_id=(os.environ.get("EC_MAINBOARD_ID") or "").strip() or None,
        bind=os.environ.get("EC_BIND", "127.0.0.1").strip(),
        port=_int("EC_PORT", 8787),
        # Read-only unless the operator opts in. See docs/API.md §4.
        control=_bool("EC_CONTROL", False),
        poll_ms=max(1000, _int("EC_POLL_MS", 5000)),
        push_ms=max(500, _int("EC_PUSH_MS", 2000)),
        camera_port=_int("EC_CAMERA_PORT", 3031),
        state_dir=Path(os.environ.get("EC_STATE_DIR") or default_state_dir()),
        password=(os.environ.get("EC_AUTH_PASSWORD") or "").strip() or None,
        admin_user=os.environ.get("EC_AUTH_USER", "operator").strip() or "operator",
        session_ttl_s=_int("EC_SESSION_TTL_S", 60 * 60 * 24 * 30),
        web_dir=web_dir,
        log_level=os.environ.get("EC_LOG_LEVEL", "INFO").upper(),
        upload_max_bytes=_int("EC_UPLOAD_MAX_MB", 2048) * 1024 * 1024,
    )
