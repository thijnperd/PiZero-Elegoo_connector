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


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "elegoo-bridge"


#: Transitions a notification may be sent for (see docs/DESIGN_PHILOSOPHY.md P13).
NOTIFY_EVENTS = ("started", "finished", "failed", "paused", "resumed", "stopped")
NOTIFY_FORMATS = ("ntfy", "telegram", "discord", "json")


def _format_for(url: str, raw: str | None) -> str:
    """Pick the payload shape from the URL when the operator did not say."""
    if raw and raw.strip().lower() in NOTIFY_FORMATS:
        return raw.strip().lower()
    lowered = url.lower()
    if "api.telegram.org" in lowered:
        return "telegram"
    if "discord.com/api/webhooks" in lowered or "discordapp.com/api/webhooks" in lowered:
        return "discord"
    if "ntfy" in lowered:
        return "ntfy"
    return "json"


def _events(raw: str | None) -> frozenset[str]:
    """Only finished/failed by default: an interruption has to earn its place."""
    if raw is None:
        return frozenset({"finished", "failed"})
    wanted = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not wanted or "none" in wanted:
        return frozenset()
    if "all" in wanted:
        return frozenset(NOTIFY_EVENTS)
    return frozenset(name for name in wanted if name in NOTIFY_EVENTS)


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
    camera_path: str
    state_dir: Path
    password: str | None
    admin_user: str
    session_ttl_s: int
    web_dir: Path
    log_level: str
    upload_max_bytes: int
    trust_loopback: bool = False
    # Push notifications. Empty URL = off; nothing else may be enabled by accident.
    notify_url: str | None = None
    notify_format: str = "json"
    notify_events: frozenset[str] = frozenset({"finished", "failed"})
    notify_token: str | None = None
    notify_target: str | None = None
    # Usage bookkeeping and the optional "time to service" reminder (0 = off).
    maintenance_hours: float = 0.0
    # One upstream camera read per window, however many viewers ask for a snapshot.
    snapshot_cache_ms: int = 800
    # Prometheus scrapers cannot log in; a bearer token is the practical alternative.
    metrics_token: str | None = None
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
        camera_path=(os.environ.get("EC_CAMERA_PATH") or "/video").strip() or "/video",
        state_dir=Path(os.environ.get("EC_STATE_DIR") or default_state_dir()),
        password=(os.environ.get("EC_AUTH_PASSWORD") or "").strip() or None,
        admin_user=os.environ.get("EC_AUTH_USER", "operator").strip() or "operator",
        session_ttl_s=_int("EC_SESSION_TTL_S", 60 * 60 * 24 * 30),
        web_dir=web_dir,
        log_level=os.environ.get("EC_LOG_LEVEL", "INFO").upper(),
        upload_max_bytes=_int("EC_UPLOAD_MAX_MB", 2048) * 1024 * 1024,
        trust_loopback=_bool("EC_TRUST_LOOPBACK", False),
        notify_url=(os.environ.get("EC_NOTIFY_URL") or "").strip() or None,
        notify_format=_format_for(
            (os.environ.get("EC_NOTIFY_URL") or "").strip(), os.environ.get("EC_NOTIFY_FORMAT")
        ),
        notify_events=_events(os.environ.get("EC_NOTIFY_EVENTS")),
        notify_token=(os.environ.get("EC_NOTIFY_TOKEN") or "").strip() or None,
        notify_target=(os.environ.get("EC_NOTIFY_TARGET") or "").strip() or None,
        maintenance_hours=max(0.0, _float("EC_MAINTENANCE_HOURS", 0.0)),
        snapshot_cache_ms=max(0, _int("EC_SNAPSHOT_CACHE_MS", 800)),
        metrics_token=(os.environ.get("EC_METRICS_TOKEN") or "").strip() or None,
    )
