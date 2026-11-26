"""Notifications: one message per transition, retried only when that can help."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from aiohttp import web

from elegoo_bridge import notify as notify_module
from elegoo_bridge.notify import Notifier, NotifySettings


@contextlib.asynccontextmanager
async def receiver(statuses: list[int] | None = None):
    """A tiny HTTP endpoint that records what it was sent."""
    calls: list[dict[str, Any]] = []
    pending = list(statuses or [])

    async def handle(request: web.Request) -> web.Response:
        calls.append(
            {
                "headers": dict(request.headers),
                "body": await request.text(),
                "content_type": request.content_type,
            }
        )
        code = pending.pop(0) if pending else 200
        return web.Response(status=code, text="ok")

    app = web.Application()
    app.router.add_post("/hook", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001 - ephemeral test port
    try:
        yield calls, f"http://127.0.0.1:{port}/hook"
    finally:
        await runner.cleanup()


async def wait_for(check, *, timeout: float = 10.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = check()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def event(kind: str = "finished", **job: Any) -> dict[str, Any]:
    return {
        "event": kind,
        "state": {"finished": "completed", "failed": "error", "paused": "paused"}.get(kind, kind),
        "ts": 1761820000.0,
        "job": {
            "filename": "cube.gcode",
            "elapsed_s": 2480,
            "progress": 100,
            "task_id": "task-abc",
            **job,
        },
        "error": {"code": 7, "message": "Nozzle temperature abnormal"} if kind == "failed" else None,
    }


@contextlib.asynccontextmanager
async def running(settings: NotifySettings | None, *, statuses: list[int] | None = None):
    async with receiver(statuses) as (calls, url):
        if settings is not None and not settings.url:
            settings = NotifySettings(url=url, format=settings.format, events=settings.events)
        notifier = Notifier(settings)
        notifier.start()
        try:
            yield notifier, calls
        finally:
            await notifier.close()


# --------------------------------------------------------------------------- basics


async def test_one_message_per_finished_transition() -> None:
    async with running(NotifySettings(url="", format="json", events=frozenset({"finished"}))) as (n, calls):
        n.handle(event())
        n.handle(event())
        await wait_for(lambda: calls)
        await asyncio.sleep(0.1)
        assert len(calls) == 1  # the duplicate is suppressed, not sent twice
        body = json.loads(calls[0]["body"])
        assert body["event"] == "finished"
        assert body["title"] == "Print finished"
        assert "cube.gcode" in body["message"]
        assert body["job"]["filename"] == "cube.gcode"
        assert n.status()["sent"] == 1


async def test_events_that_were_not_asked_for_are_ignored() -> None:
    async with running(NotifySettings(url="", format="json", events=frozenset({"finished"}))) as (n, calls):
        n.handle(event("paused"))
        n.handle(event("started"))
        n.handle(event("resumed"))
        await asyncio.sleep(0.2)
        assert calls == []
        assert n.status()["sent"] == 0


async def test_started_can_be_opted_into() -> None:
    events = frozenset({"started", "finished"})
    async with running(NotifySettings(url="", format="json", events=events)) as (n, calls):
        n.handle(event("started"))
        await wait_for(lambda: calls)
        assert json.loads(calls[0]["body"])["event"] == "started"


async def test_a_disabled_notifier_does_nothing() -> None:
    async with receiver() as (calls, url):
        notifier = Notifier(None)
        notifier.start()  # no settings: start is a no-op
        notifier.handle(event())
        await asyncio.sleep(0.1)
        assert calls == []
        status = notifier.status()
        assert status["enabled"] is False
        assert status["events"] == []

        off = Notifier(NotifySettings(url=url, events=frozenset()))
        assert off.enabled is False


# --------------------------------------------------------------------------- failures


async def test_a_server_error_is_retried(monkeypatch) -> None:
    monkeypatch.setattr(notify_module, "ATTEMPT_DELAYS", (0.01, 0.01))
    async with receiver(statuses=[500]) as (calls, url):
        notifier = Notifier(NotifySettings(url=url, format="json", events=frozenset({"finished"})))
        notifier.start()
        try:
            notifier.handle(event())
            await wait_for(lambda: notifier.sent)
        finally:
            await notifier.close()
        assert len(calls) == 2  # the 500, then the delivery
        assert notifier.status()["failed"] == 0


async def test_a_rejected_token_is_not_retried(monkeypatch) -> None:
    monkeypatch.setattr(notify_module, "ATTEMPT_DELAYS", (0.01, 0.01))
    async with receiver(statuses=[401, 200]) as (calls, url):
        logs: list[str] = []
        notifier = Notifier(
            NotifySettings(url=url, format="json", events=frozenset({"finished"})),
            on_log=lambda message, **_: logs.append(message),
        )
        notifier.start()
        try:
            notifier.handle(event())
            await wait_for(lambda: notifier.failed)
        finally:
            await notifier.close()
        assert len(calls) == 1, "a permanent 4xx must not be repeated"
        assert "Notification failed" in logs[0]


# --------------------------------------------------------------------------- formats


async def test_ntfy_sends_the_text_with_headers() -> None:
    async with receiver() as (calls, url):
        notifier = Notifier(NotifySettings(url=url, format="ntfy", events=frozenset({"failed"})))
        notifier.start()
        try:
            notifier.handle(event("failed"))
            await wait_for(lambda: calls)
        finally:
            await notifier.close()
        headers = {k.lower(): v for k, v in calls[0]["headers"].items()}
        assert headers["title"] == "Print failed"
        assert headers["priority"] == "5"
        assert "Nozzle temperature abnormal" in calls[0]["body"]


async def test_json_webhook_carries_a_bearer_token() -> None:
    async with receiver() as (calls, url):
        notifier = Notifier(
            NotifySettings(url=url, format="json", events=frozenset({"finished"}), token="s3cret")
        )
        notifier.start()
        try:
            notifier.handle(event())
            await wait_for(lambda: calls)
        finally:
            await notifier.close()
        headers = {k.lower(): v for k, v in calls[0]["headers"].items()}
        assert headers["authorization"] == "Bearer s3cret"


async def test_status_never_leaks_the_url_or_token() -> None:
    secret = "http://127.0.0.1:1/topic-that-is-a-password"
    notifier = Notifier(NotifySettings(url=secret, format="ntfy", token="token-value"))
    rendered = json.dumps(notifier.status())
    assert "topic-that-is-a-password" not in rendered
    assert "token-value" not in rendered
    assert notifier.status()["format"] == "ntfy"
