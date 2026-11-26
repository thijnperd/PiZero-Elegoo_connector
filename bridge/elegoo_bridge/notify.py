"""Push one message per meaningful transition, and nothing else.

This is principle P13 in docs/DESIGN_PHILOSOPHY.md made real. The log is already
deduplicated by construction — :meth:`StateStore._announce_transition` fires once per
state-slug change, never per status frame — so this module subscribes to those events
instead of looking at the stream. A print that runs for nine hours and produces nine
thousand frames produces three messages: started (if you asked for it), finished.

Four payload shapes, one HTTP call each, no provider SDK:

``ntfy``      POST the message text to a topic URL; title, priority and tags as headers.
``telegram``  POST ``sendMessage`` with ``chat_id`` from ``EC_NOTIFY_TARGET``.
``discord``   POST the webhook URL with ``content`` and mentions disabled.
``json``      POST a small object; a bearer token is sent when one is configured.

Failure policy: retry the transient things (network, 429, 5xx) with a short backoff,
never the permanent ones (a wrong token stays wrong, and retrying it is how you get
rate-limited), and always leave one line in the log saying what happened. The queue is
deliberately small: a printer produces a handful of events per print, so a backlog
means the receiver is broken, not that we are behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)

ATTEMPT_DELAYS = (1.5, 4.0, 9.0)
QUEUE_MAX = 24
#: A flapping state can produce the same transition twice; one message is enough.
DEDUPE_WINDOW_S = 120.0
RETRY_AFTER_CAP_S = 30.0


@dataclass(slots=True)
class NotifySettings:
    """Everything the notifier needs, resolved from the environment once."""

    url: str
    format: str = "json"
    events: frozenset[str] = frozenset({"finished", "failed"})
    token: str | None = None
    target: str | None = None
    timeout_s: float = 8.0


def _clock(seconds: float | None) -> str:
    if not seconds:
        return "unknown duration"
    total = int(max(0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def describe(event: dict[str, Any]) -> tuple[str, str]:
    """The title and body a human reads on a lock screen.

    Deliberately short and free of anything that is only meaningful inside the
    dashboard: the phone shows one line, and "layer 231 of 300" is not that line.
    """
    job = event.get("job") or {}
    name = job.get("filename") or "the print"
    kind = event.get("event")

    if kind == "finished":
        return "Print finished", f"{name} finished in {_clock(job.get('elapsed_s'))}."
    if kind == "failed":
        error = (event.get("error") or {}).get("message")
        detail = f" — {error}" if error else ""
        return "Print failed", f"{name} stopped with an error{detail}."
    if kind == "stopped":
        return "Print stopped", f"{name} was cancelled after {_clock(job.get('elapsed_s'))}."
    if kind == "paused":
        return "Print paused", f"{name} is paused and waiting for you."
    if kind == "resumed":
        return "Print resumed", f"{name} is printing again."
    if kind == "started":
        return "Print started", f"{name} is now printing."
    return "Printer", f"{name}: {event.get('state') or 'state changed'}"


class Notifier:
    """Queue, deduplicate and deliver transition messages."""

    def __init__(
        self,
        settings: NotifySettings | None,
        *,
        on_log: Callable[..., None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._on_log = on_log
        self._transport = transport
        self._queue: asyncio.Queue[tuple[dict[str, Any], str, str]] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._recent: deque[tuple[str, float]] = deque(maxlen=32)
        self._client: httpx.AsyncClient | None = None
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.last_error: str | None = None
        self.last_sent_ts: float | None = None

    # --- lifecycle ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings and self.settings.url and self.settings.events)

    def start(self) -> None:
        if not self.enabled or self._worker is not None:
            return
        self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._client = httpx.AsyncClient(
            timeout=self.settings.timeout_s if self.settings else 8.0,
            transport=self._transport,
            follow_redirects=False,
        )
        self._worker = asyncio.create_task(self._run(), name="notifier")
        log.info(
            "notifications on: format=%s events=%s",
            self.settings.format if self.settings else "?",
            ",".join(sorted(self.settings.events)) if self.settings else "",
        )

    async def close(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
        self._worker = None
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    # --- event intake ------------------------------------------------------

    def handle(self, event: dict[str, Any]) -> None:
        """Called from the state store; must never raise or block."""
        if not self.enabled or self._queue is None:
            return
        kind = str(event.get("event") or "")
        if kind not in (self.settings.events if self.settings else frozenset()):
            return
        if self._duplicate(event, kind):
            return

        job = event.get("job") or {}
        title, text = describe(event)
        try:
            self._queue.put_nowait((event, title, text))
        except asyncio.QueueFull:
            # A full queue means the receiver is not answering. Dropping the newest
            # event is honest and bounded; inventing a larger buffer on a 512 MB Pi
            # only delays the same failure.
            self.dropped += 1
            self._log("Notification dropped: the queue is full", level="warn", kind="notify")

    def _duplicate(self, event: dict[str, Any], kind: str) -> bool:
        job = event.get("job") or {}
        key = "|".join(
            [
                kind,
                str(job.get("task_id") or job.get("filename") or "-"),
                str(event.get("state") or "-"),
            ]
        )
        now = time.time()
        self._recent = deque(
            ((item, ts) for item, ts in self._recent if now - ts < DEDUPE_WINDOW_S),
            maxlen=32,
        )
        if any(seen == key for seen, _ in self._recent):
            log.debug("notification suppressed as duplicate: %s", key)
            return True
        self._recent.append((key, now))
        return False

    # --- delivery ----------------------------------------------------------

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            event, title, text = await self._queue.get()
            try:
                await self._deliver(event, title, text)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - a notifier may never kill the app
                self.failed += 1
                self.last_error = str(err) or err.__class__.__name__
                log.warning("notification failed: %s", self.last_error)
                self._log(f"Notification failed: {self.last_error}", level="warn", kind="notify")
            finally:
                self._queue.task_done()

    def _request(self, event: dict[str, Any], title: str, text: str) -> tuple[str, dict[str, Any], dict[str, str]]:
        """(url, json body or None, headers) plus the text payload, per format."""
        settings = self.settings
        assert settings is not None
        url = settings.url
        headers: dict[str, str] = {}
        body: dict[str, Any] = {}

        if settings.format == "ntfy":
            headers = {
                "Title": title.encode("ascii", "replace").decode("ascii") or "Printer",
                "Priority": "5" if event.get("event") == "failed" else "3",
                "Tags": "printer,error" if event.get("event") == "failed" else "printer",
            }
            if settings.token:
                headers["Authorization"] = f"Bearer {settings.token}"
            return url, {}, headers

        if settings.format == "telegram":
            body = {
                "chat_id": settings.target or "",
                "text": f"{title}\n{text}",
                "disable_web_page_preview": True,
            }
        elif settings.format == "discord":
            body = {"content": f"**{title}**\n{text}", "allowed_mentions": {"parse": []}}
        else:
            body = {
                "event": event.get("event"),
                "title": title,
                "message": text,
                "state": event.get("state"),
                "ts": round(time.time(), 1),
                "job": {
                    "filename": (event.get("job") or {}).get("filename"),
                    "progress": (event.get("job") or {}).get("progress"),
                    "elapsed_s": (event.get("job") or {}).get("elapsed_s"),
                },
            }
            if settings.token:
                headers["Authorization"] = f"Bearer {settings.token}"
        return url, body, headers

    async def _deliver(self, event: dict[str, Any], title: str, text: str) -> None:
        settings = self.settings
        assert settings is not None and self._client is not None
        if settings.format == "ntfy" and not settings.url:
            return

        url, body, headers = self._request(event, title, text)
        payload: dict[str, Any] = {"headers": headers}
        if settings.format == "ntfy":
            payload["content"] = text
        else:
            payload["json"] = body

        last_error = "unknown error"
        for attempt, delay in enumerate((0.0, *ATTEMPT_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self._client.post(url, **payload)
            except httpx.HTTPError as err:
                last_error = str(err) or err.__class__.__name__
                continue

            if 200 <= response.status_code < 300:
                self.sent += 1
                self.last_sent_ts = time.time()
                self.last_error = None
                self._log(f"Notified: {text}", kind="notify")
                return

            last_error = f"HTTP {response.status_code}"
            retry_after = _retry_after(response)
            if response.status_code == 429 and retry_after is not None:
                await asyncio.sleep(min(retry_after, RETRY_AFTER_CAP_S))
                continue
            if 400 <= response.status_code < 500 and response.status_code != 429:
                # A rejected token, a deleted webhook, a chat the bot was removed from:
                # retrying cannot fix any of them.
                break

        raise RuntimeError(last_error)

    # --- reporting ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """For the snapshot. The URL and token never leave the server."""
        settings = self.settings
        return {
            "enabled": self.enabled,
            "format": settings.format if self.enabled else None,
            "events": sorted(settings.events) if settings else [],
            "sent": self.sent,
            "failed": self.failed,
            "dropped": self.dropped,
            "queued": self._queue.qsize() if self._queue is not None else 0,
            "last_sent_ts": None if self.last_sent_ts is None else round(self.last_sent_ts, 1),
            "last_error": self.last_error,
        }

    def _log(self, message: str, *, level: str = "info", kind: str = "notify") -> None:
        if self._on_log is None:
            return
        with contextlib.suppress(Exception):
            self._on_log(message, level=level, kind=kind)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def same_token(expected: str, provided: str | None) -> bool:
    """Constant-time comparison for the metrics token."""
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)
