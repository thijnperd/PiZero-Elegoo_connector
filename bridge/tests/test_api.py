"""End-to-end tests against the fake printer.

The app runs through httpx's ASGI transport — no live server, no port juggling beyond
the fake printer's own ephemeral ports.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from aiohttp import web
from cc1client import camera as cc1_camera
from cc1client import client as cc1_client
from cc1client import upload as cc1_upload

from elegoo_bridge.config import Config
from elegoo_bridge.server import create_app
from fake_printer import MAINBOARD, FakePrinter

PASSWORD = "correct horse"


def make_config(
    tmp_path,
    fake: FakePrinter,
    *,
    control: bool = True,
    camera_port: int | None = None,
    notify_url: str | None = None,
    metrics_token: str | None = None,
) -> Config:
    return Config(
        printer_host="127.0.0.1",
        mainboard_id=MAINBOARD,
        bind="127.0.0.1",
        port=8787,
        control=control,
        poll_ms=1000,
        push_ms=500,
        camera_port=camera_port or fake.http_port,
        camera_path="/video",
        state_dir=tmp_path,
        password=PASSWORD,
        admin_user="operator",
        session_ttl_s=3600,
        web_dir=tmp_path / "web",  # deliberately absent: exercises the fallback route
        log_level="WARNING",
        upload_max_bytes=8 * 1024 * 1024,
        notify_url=notify_url,
        notify_format="json",
        notify_events=frozenset({"finished", "failed"}),
        metrics_token=metrics_token,
        snapshot_cache_ms=5000,
    )


@contextlib.asynccontextmanager
async def rig(
    tmp_path,
    monkeypatch,
    *,
    control: bool = True,
    camera_port: int | None = None,
    notify_url: str | None = None,
    metrics_token: str | None = None,
):
    fake = FakePrinter()
    await fake.start()
    monkeypatch.setattr(cc1_client, "WS_PORT", fake.ws_port)
    monkeypatch.setattr(cc1_upload, "UPLOAD_PORT", fake.http_port)
    monkeypatch.setattr(cc1_camera, "CAMERA_PORT", fake.http_port)

    config = make_config(
        tmp_path,
        fake,
        control=control,
        camera_port=camera_port,
        notify_url=notify_url,
        metrics_token=metrics_token,
    )
    app = create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            try:
                yield SimpleNamespace(fake=fake, app=app, client=client, config=config)
            finally:
                pass
    await fake.stop()


async def wait_for(check, *, timeout: float = 10.0, interval: float = 0.05) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/login", json={"password": PASSWORD})
    assert response.status_code == 204, response.text


async def online_state(client: httpx.AsyncClient) -> dict[str, Any]:
    async def check():
        response = await client.get("/api/state")
        assert response.status_code == 200, response.text
        snapshot = response.json()
        if snapshot["connection"]["state"] == "online" and snapshot["printer"]:
            return snapshot
        return None

    return await wait_for(check)


# --------------------------------------------------------------------------- auth


async def test_health_is_public_but_state_is_not(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        health = await r.client.get("/healthz")
        body = health.json()
        # Liveness for a tunnel/uptime check, plus context that is not a secret.
        assert body["ok"] is True
        assert body["auth"] == "enabled"
        assert body["control"] is True
        assert body["printer"] is None  # nothing reported yet
        assert body["uptime_s"] >= 0
        # The shell must revalidate: `git pull` on the Pi has to show up on reload.
        assert health.headers["cache-control"] == "no-cache"
        assert (await r.client.get("/api/state")).status_code == 401


async def test_login_flow(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        bad = await r.client.post("/api/login", json={"password": "nope"})
        assert bad.status_code == 401
        assert bad.json()["detail"] == "invalid password"

        await login(r.client)
        assert (await r.client.get("/api/state")).status_code == 200

        await r.client.post("/api/logout")
        assert (await r.client.get("/api/state")).status_code == 401


async def test_login_is_rate_limited(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        for _ in range(5):
            assert (await r.client.post("/api/login", json={"password": "nope"})).status_code == 401
        assert (await r.client.post("/api/login", json={"password": "nope"})).status_code == 429


# --------------------------------------------------------------------------- state


async def test_snapshot_matches_contract(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        snapshot = await online_state(r.client)

        assert snapshot["schema"] == 1
        assert snapshot["connection"]["mainboard_id"] == MAINBOARD
        # Attributes arrive on their own push, just after the first status frame.
        await wait_for(lambda: _firmware_seen(r.client))
        snapshot = (await r.client.get("/api/state")).json()
        assert snapshot["connection"]["firmware"] == "V1.1.46"
        assert snapshot["capabilities"] == {
            "control": True,
            "camera": True,
            "files": True,
            "upload": True,
            "history": True,
        }

        printer = snapshot["printer"]
        assert printer["state"] == "printing"
        assert printer["stale"] is False
        assert printer["job"]["filename"] == "cube.gcode"
        assert printer["temps"]["nozzle"]["actual"] == pytest.approx(214.9)
        assert printer["temps"]["nozzle"]["target"] == pytest.approx(215.0)
        assert printer["fans"]["model"] == 100

        assert isinstance(snapshot["log"], list)
        assert r.fake.max_connections == 1  # one owner, not one per request


async def _firmware_seen(client: httpx.AsyncClient):
    snapshot = (await client.get("/api/state")).json()
    return snapshot["connection"]["firmware"] or None


async def test_diagnostics_redacts_password(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        body = (await r.client.get("/api/diagnostics")).json()
        assert body["config"]["control"] is True
        assert body["config"]["auth"] == "enabled"
        assert PASSWORD not in str(body)


# --------------------------------------------------------------------------- commands


ORIGIN = {"Origin": "http://testserver"}


async def test_pause_reaches_the_printer(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        await online_state(r.client)

        response = await r.client.post("/api/print/pause", headers=ORIGIN)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["accepted"] is True
        assert body["status"] == "sent"
        assert any(cmd == 129 for cmd, _ in r.fake.commands)


async def test_cross_origin_write_is_refused(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        await online_state(r.client)

        response = await r.client.post("/api/print/stop", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
        assert not any(cmd == 130 for cmd, _ in r.fake.commands)


async def test_invalid_speed_mode_is_rejected(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        await online_state(r.client)

        response = await r.client.post("/api/print/speed", json={"mode": "turbo"}, headers=ORIGIN)
        assert response.status_code == 422

        ok = await r.client.post("/api/print/speed", json={"mode": "sport"}, headers=ORIGIN)
        assert ok.status_code == 200
        assert (403, {"PrintSpeedPct": 130}) in r.fake.commands


async def test_heater_bounds_are_enforced(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        await online_state(r.client)

        too_hot = await r.client.post("/api/print/temps", json={"nozzle": 400}, headers=ORIGIN)
        assert too_hot.status_code == 422

        fine = await r.client.post("/api/print/temps", json={"nozzle": 220}, headers=ORIGIN)
        assert fine.status_code == 200
        assert (403, {"TempTargetNozzle": 220.0}) in r.fake.commands


async def test_read_only_bridge_exposes_no_write_routes(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch, control=False) as r:
        await login(r.client)
        snapshot = await online_state(r.client)
        assert snapshot["capabilities"]["control"] is False

        response = await r.client.post("/api/print/pause", headers=ORIGIN)
        assert response.status_code in (404, 405)
        assert not any(cmd == 129 for cmd, _ in r.fake.commands)


# --------------------------------------------------------------------------- files


async def test_file_list_and_history(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        await online_state(r.client)

        files = (await r.client.get("/api/files")).json()
        assert files["total"] == 2
        assert {f["filename"] for f in files["files"]} == {"cube.gcode", "benchy.gcode"}
        assert all(f["created_ts"] and f["created_ts"] < 1e11 for f in files["files"])

        history = (await r.client.get("/api/history")).json()
        assert history["jobs"][0]["status"] == "completed"
        assert history["jobs"][0]["name"] == "cube.gcode"


async def test_delete_protects_the_printing_file(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        await online_state(r.client)

        # cube.gcode is the active job: the bridge must refuse, not pull the file out
        # from under the motion stack.
        refused = await r.client.post(
            "/api/files/delete", json={"filenames": ["cube.gcode"]}, headers=ORIGIN
        )
        assert refused.status_code == 409
        assert "currently printing" in refused.json()["detail"]
        assert not any(cmd == 259 for cmd, _ in r.fake.commands)

        allowed = await r.client.post(
            "/api/files/delete", json={"filenames": ["benchy.gcode"]}, headers=ORIGIN
        )
        assert allowed.status_code == 200, allowed.text
        assert (259, {"FileList": ["/local/benchy.gcode"]}) in r.fake.commands


# --------------------------------------------------------------------------- upload


async def test_upload_streams_chunks_and_reports_progress(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        await online_state(r.client)

        payload = b"G28\n" * 4096
        response = await r.client.post(
            "/api/uploads",
            files={"file": ("cube.gcode", payload, "application/octet-stream")},
            data={"start": "false"},
            headers=ORIGIN,
        )
        assert response.status_code == 200, response.text
        upload_id = response.json()["upload_id"]

        async def done():
            body = (await r.client.get("/api/uploads")).json()
            entry = next((u for u in body["uploads"] if u["id"] == upload_id), None)
            if entry and entry["state"] in ("done", "error"):
                return entry
            return None

        entry = await wait_for(done)
        assert entry["state"] == "done", entry
        assert entry["error"] is None
        assert entry["bytes_sent"] == len(payload)
        assert r.fake.upload_bytes == len(payload)
        chunk = r.fake.upload_chunks[0]
        assert chunk["check"] == "1"
        assert chunk["offset"] == "0"
        assert chunk["total"] == str(len(payload))
        assert chunk["filename"] == "cube.gcode"
        assert chunk["md5"]


async def test_upload_can_start_the_job(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        await online_state(r.client)

        response = await r.client.post(
            "/api/uploads",
            files={"file": ("part.gcode", b"G1 X1\n", "application/octet-stream")},
            data={"start": "true"},
            headers=ORIGIN,
        )
        assert response.status_code == 200
        await wait_for(lambda: _has_start(r.fake))


async def _has_start(fake: FakePrinter):
    return any(cmd == 128 for cmd, _ in fake.commands) or None


# --------------------------------------------------------------------------- camera


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_camera_snapshot_returns_jpeg(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        await online_state(r.client)

        response = await r.client.get("/api/camera/snapshot")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "no-store"
        assert response.content.startswith(b"\xff\xd8")
        assert response.content.endswith(b"\xff\xd9")


async def test_snapshots_are_shared_between_viewers(tmp_path, monkeypatch) -> None:
    """A dashboard, a second tab and Home Assistant must not mean three printer reads."""
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        await online_state(r.client)

        await r.client.get("/api/camera/snapshot")  # resolves the URL and fills the cache
        before = r.fake.video_requests
        for _ in range(3):
            response = await r.client.get("/api/camera/snapshot")
            assert response.status_code == 200
            assert response.content.startswith(b"\xff\xd8")
        assert r.fake.video_requests == before

        info = (await r.client.get("/api/camera")).json()
        assert info["frame_age_s"] is not None


async def test_camera_snapshot_finds_the_camera_on_another_port(tmp_path, monkeypatch) -> None:
    """A wrong EC_CAMERA_PORT must not cost the user their camera feed."""
    from elegoo_bridge import camera as bridge_camera

    monkeypatch.setattr(bridge_camera, "CANDIDATE_PORTS", ())
    async with rig(tmp_path, monkeypatch, camera_port=free_port()) as r:
        await login(r.client)
        await online_state(r.client)
        monkeypatch.setattr(bridge_camera, "CANDIDATE_PORTS", (r.fake.http_port,))

        response = await r.client.get("/api/camera/snapshot")
        assert response.status_code == 200, response.text
        assert response.content.startswith(b"\xff\xd8")

        info = (await r.client.get("/api/camera")).json()
        assert info["url"].endswith(f":{r.fake.http_port}/video")


# --------------------------------------------------------------------------- metrics


async def test_metrics_need_a_session_and_report_what_the_printer_said(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        assert (await r.client.get("/metrics")).status_code == 401

        await login(r.client)
        await online_state(r.client)
        response = await r.client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")

        body = response.text
        assert "ec_bridge_up 1" in body
        assert "ec_printer_connected 1" in body
        assert 'ec_printer_state{state="printing"} 1' in body
        assert "ec_print_progress_percent 42" in body
        assert 'ec_temperature_celsius{heater="nozzle",kind="actual"} 214.9' in body
        assert 'ec_temperature_celsius{heater="nozzle",kind="target"} 215.0' in body
        assert "ec_usage_print_seconds_total 0" in body
        assert 'ec_usage_jobs_total{result="completed"} 0' in body
        assert body.endswith("\n")
        # Every series is preceded by its TYPE line, or Prometheus rejects the scrape.
        assert "# TYPE ec_printer_state gauge" in body


async def test_a_metrics_token_works_without_a_cookie(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch, metrics_token="s3cret") as r:
        assert (await r.client.get("/metrics")).status_code == 401
        wrong = await r.client.get("/metrics", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401

        bearer = await r.client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert bearer.status_code == 200
        query = await r.client.get("/metrics?token=s3cret")
        assert query.status_code == 200


# --------------------------------------------------------------------------- usage


def set_print_state(fake: FakePrinter, code: int, *, progress: int | None = None) -> None:
    """Move the fake printer to another state; its push loop carries it to the bridge."""
    fake.status["PrintInfo"]["Status"] = code
    fake.status["CurrentStatus"] = [code]
    if progress is not None:
        fake.status["PrintInfo"]["Progress"] = progress


async def wait_for_state(client: httpx.AsyncClient, slug: str) -> dict[str, Any]:
    async def check():
        snapshot = (await client.get("/api/state")).json()
        return snapshot if (snapshot.get("printer") or {}).get("state") == slug else None

    return await wait_for(check)


async def test_finished_jobs_are_counted_and_maintenance_can_be_reset(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        await online_state(r.client)

        empty = (await r.client.get("/api/usage")).json()
        assert empty["print_seconds"] == 0
        assert empty["jobs"]["completed"] == 0

        set_print_state(r.fake, 9, progress=100)
        await wait_for_state(r.client, "completed")

        usage = (await r.client.get("/api/usage")).json()
        assert usage["jobs"]["completed"] == 1
        assert usage["print_seconds"] == 1830  # CurrentTicks of the fake job
        assert usage["last_job"]["filename"] == "cube.gcode"

        # The counter can only be reset by an authorised, same-origin write.
        foreign = await r.client.post(
            "/api/maintenance/reset", headers={"Origin": "https://evil.example"}
        )
        assert foreign.status_code == 403
        reset = await r.client.post("/api/maintenance/reset", headers=ORIGIN)
        assert reset.status_code == 200, reset.text
        assert reset.json()["maintenance"]["hours_since"] == 0.0

        snapshot = (await r.client.get("/api/state")).json()
        assert snapshot["usage"]["jobs"]["completed"] == 1
        assert snapshot["notifications"]["enabled"] is False


async def test_a_print_that_was_already_running_is_not_counted_as_started(tmp_path, monkeypatch) -> None:
    """Boot the bridge mid-print: that is one job, not a new one and not a notification."""
    async with rig(tmp_path, monkeypatch) as r:
        await login(r.client)
        snapshot = await online_state(r.client)
        assert snapshot["printer"]["state"] == "printing"

        usage = (await r.client.get("/api/usage")).json()
        assert usage["jobs"]["started"] == 0
        assert not any("Started" in entry["message"] for entry in snapshot["log"])


async def test_read_only_bridge_does_not_offer_a_maintenance_reset(tmp_path, monkeypatch) -> None:
    async with rig(tmp_path, monkeypatch, control=False) as r:
        await login(r.client)
        await online_state(r.client)
        response = await r.client.post("/api/maintenance/reset", headers=ORIGIN)
        assert response.status_code in (404, 405)
        # Reading the counters stays available: it is not an action.
        assert (await r.client.get("/api/usage")).status_code == 200


# --------------------------------------------------------------------------- notifications


@contextlib.asynccontextmanager
async def notify_receiver():
    calls: list[dict[str, Any]] = []

    async def handle(request: web.Request) -> web.Response:
        calls.append({"body": await request.text(), "headers": dict(request.headers)})
        return web.Response(text="ok")

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


async def test_a_finished_print_sends_exactly_one_notification(tmp_path, monkeypatch) -> None:
    async with notify_receiver() as (calls, url):
        async with rig(tmp_path, monkeypatch, notify_url=url) as r:
            await login(r.client)
            await online_state(r.client)

            set_print_state(r.fake, 9, progress=100)
            await wait_for_state(r.client, "completed")

            async def delivered():
                return calls or None

            await wait_for(delivered)

            # The push loop keeps repeating the same status frame; P13 says that is not
            # a reason to ring a phone again.
            for _ in range(4):
                await asyncio.sleep(0.3)
            assert len(calls) == 1

            body = json.loads(calls[0]["body"])
            assert body["event"] == "finished"
            assert body["job"]["filename"] == "cube.gcode"

            status = (await r.client.get("/api/state")).json()["notifications"]
            assert status["enabled"] is True
            assert status["sent"] == 1
            assert status["last_sent_ts"] is not None
            assert url not in json.dumps(status)
