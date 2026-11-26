"""FastAPI application: the only thing a browser ever talks to.

Three rules hold this layer together:

1. Mutating routes exist only when ``EC_CONTROL=1``. Read-only is the default, and a
   disabled bridge cannot be talked into a command.
2. Commands are accepted, not executed inline: an SDCP write returns as soon as the
   printer acknowledges it, and the truth arrives later as a status frame.
3. Every route that changes something is same-origin checked in addition to the cookie.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders

from cc1client.camera import snapshot as camera_snapshot
from cc1client.client import ControlDisabledError, PrinterError, RequestTimeoutError
from cc1client.mjpeg_broadcast import CameraBroadcaster, CameraUnavailable
from cc1client.upload import upload_cc1

from .auth import (
    check_password,
    is_authenticated,
    issue_token,
    login_allowed,
    record_failed_login,
    require_same_origin,
    require_session,
    reset_login_attempts,
)
from .camera import CameraEndpoint, SnapshotCache
from .config import Config, load_config
from .notify import Notifier, NotifySettings, same_token
from .printer import Supervisor
from .state import StateStore
from .usage import UsageStore

log = logging.getLogger(__name__)

SPEED_MODES = {"silent": 50, "balanced": 100, "sport": 130, "ludicrous": 160}
METRICS_TYPE = "text/plain; version=0.0.4; charset=utf-8"
UPLOAD_CHUNK = 1024 * 1024
MIN_WRITE_INTERVAL_S = 0.25
HISTORY_STATUS = {1: "completed", 2: "cancelled", 3: "cancelled"}


# --------------------------------------------------------------------------- models


class LoginBody(BaseModel):
    password: str


class StartBody(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    storage: Literal["local", "udisk"] = "local"
    auto_leveling: bool = True
    timelapse: bool = False


class SpeedBody(BaseModel):
    mode: Literal["silent", "balanced", "sport", "ludicrous"]


class FansBody(BaseModel):
    model: int | None = Field(default=None, ge=0, le=100)
    auxiliary: int | None = Field(default=None, ge=0, le=100)
    chamber: int | None = Field(default=None, ge=0, le=100)


class TempsBody(BaseModel):
    nozzle: float | None = Field(default=None, ge=0, le=300)
    bed: float | None = Field(default=None, ge=0, le=110)
    chamber: float | None = Field(default=None, ge=0, le=60)


class LightBody(BaseModel):
    on: bool


class DeleteBody(BaseModel):
    filenames: list[str] = Field(min_length=1, max_length=50)
    storage: Literal["local", "udisk"] = "local"


# --------------------------------------------------------------------------- uploads


class UploadManager:
    """Runs one printer upload at a time, in the background, with visible progress."""

    def __init__(self, config: Config, store: StateStore, supervisor: Supervisor) -> None:
        self.config = config
        self.store = store
        self.supervisor = supervisor
        self.items: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    def public(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item["id"],
                "filename": item["filename"],
                "bytes_sent": item["bytes_sent"],
                "total_bytes": item["total_bytes"],
                "state": item["state"],
                "error": item["error"],
            }
            for item in sorted(self.items.values(), key=lambda i: i["created"], reverse=True)
        ]

    async def start(self, path: Path, filename: str, total: int, *, auto_start: bool) -> dict[str, Any]:
        async with self._lock:
            if any(item["state"] == "running" for item in self.items.values()):
                raise HTTPException(status_code=409, detail="another upload is already running")
            upload_id = uuid.uuid4().hex[:12]
            self.items[upload_id] = {
                "id": upload_id,
                "filename": filename,
                "bytes_sent": 0,
                "total_bytes": total,
                "state": "running",
                "error": None,
                "created": time.time(),
                "path": path,
                "auto_start": auto_start,
            }
            self._tasks[upload_id] = asyncio.create_task(self._run(upload_id), name=f"upload-{upload_id}")
            return self.items[upload_id]

    async def _run(self, upload_id: str) -> None:
        item = self.items[upload_id]
        path: Path = item["path"]
        try:
            def progress(sent: int, total: int) -> None:
                item["bytes_sent"] = sent
                item["total_bytes"] = total
                pct = int(sent * 100 / total) if total else 0
                if pct and pct % 25 == 0:
                    self.store.log(f"Upload {item['filename']}: {pct}%", kind="upload")

            name = await upload_cc1(
                self.config.printer_host,
                path,
                remote_name=item["filename"],
                progress=progress,
            )
            item["state"] = "done"
            item["bytes_sent"] = item["total_bytes"]
            self.store.log(f"Uploaded {name}", kind="upload")

            if item["auto_start"]:
                printer = self.supervisor.printer
                if printer is None:
                    raise PrinterError("printer went offline before the job could start")
                await printer.start_print(name)
                self.store.record_command(f"Start print: {name}")
        except asyncio.CancelledError:
            item["state"] = "cancelled"
            raise
        except Exception as err:  # noqa: BLE001 - surface to the UI
            item["state"] = "error"
            item["error"] = str(err) or err.__class__.__name__
            self.store.log(f"Upload failed: {item['error']}", level="error", kind="upload")
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
            self._tasks.pop(upload_id, None)

    async def cancel(self, upload_id: str) -> None:
        task = self._tasks.get(upload_id)
        if task is None:
            raise HTTPException(status_code=404, detail="no such upload")
        task.cancel()

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


# --------------------------------------------------------------------------- helpers


def _sse(payload: dict[str, Any]) -> str:
    import json

    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _ack_of(message: Any) -> int | None:
    try:
        ack = ((message.inner or {}).get("Data") or {}).get("Ack")
    except AttributeError:
        return None
    return ack if isinstance(ack, int) else None


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · Elegoo Connector</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:#121412; color:#e6e8e6;
         font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  form { width:min(22rem,90vw); display:grid; gap:.75rem; }
  h1 { font-size:1.05rem; font-weight:600; letter-spacing:.02em; margin:0 0 .25rem; color:#9aa39a; }
  label { font-size:.8rem; color:#9aa39a; text-transform:uppercase; letter-spacing:.08em; }
  input { padding:.85rem .9rem; border-radius:.5rem; border:1px solid #333833;
          background:#191c19; color:inherit; font:inherit; }
  input:focus { outline:2px solid #4c9a8a; outline-offset:2px; }
  button { padding:.85rem; border-radius:.5rem; border:0; background:#2f6f63; color:#f2fffb;
           font:inherit; font-weight:600; cursor:pointer; min-height:44px; }
  button:focus-visible { outline:2px solid #c9a227; outline-offset:2px; }
  p.err { margin:0; color:#e2725b; font-size:.9rem; min-height:1.2em; }
</style></head>
<body>
  <form method="post" action="/api/login" id="f">
    <h1>Elegoo Connector</h1>
    <label for="pw">Password</label>
    <input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
    <p class="err" id="err" role="alert"></p>
    <button type="submit">Sign in</button>
  </form>
  <script>
    const f = document.getElementById('f'), err = document.getElementById('err');
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      err.textContent = '';
      const res = await fetch('/api/login', {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({ password: document.getElementById('pw').value })
      });
      if (res.ok) { location.href = '/'; return; }
      err.textContent = res.status === 429 ? 'Too many attempts. Wait five minutes.'
                                           : 'Wrong password.';
    });
  </script>
</body></html>
"""


# --------------------------------------------------------------------------- app


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    store = StateStore(printer_host=config.printer_host, control=config.control)
    supervisor = Supervisor(config, store)
    camera_endpoint = CameraEndpoint(
        config.printer_host, port=config.camera_port, path=config.camera_path
    )
    camera = CameraBroadcaster(lambda: camera_endpoint.current_url)
    uploads = UploadManager(config, store, supervisor)
    snapshots = SnapshotCache(config.snapshot_cache_ms / 1000.0)
    usage = UsageStore(
        Path(config.state_dir) / "usage.json",
        maintenance_hours=config.maintenance_hours,
        on_log=store.log,
    )
    notifier = Notifier(
        NotifySettings(
            url=config.notify_url or "",
            format=config.notify_format,
            events=config.notify_events,
            token=config.notify_token,
            target=config.notify_target,
        )
        if config.notify_url
        else None,
        on_log=store.log,
    )
    # Both subscribers only count or queue; the printer loop never waits on the network.
    store.on_event(usage.handle)
    store.on_event(notifier.handle)
    store.set_extra(lambda: {"usage": usage.snapshot(), "notifications": notifier.status()})
    started_at = time.time()
    write_lock = asyncio.Lock()
    last_write = {"ts": 0.0}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.start()
        notifier.start()
        supervisor.start()
        log.info(
            "bridge up: printer=%s control=%s auth=%s web=%s",
            config.printer_host or "<unset>",
            config.control,
            "on" if config.auth_enabled else "off",
            config.web_dir,
        )
        try:
            yield
        finally:
            await supervisor.close()
            await uploads.close()
            await camera.close()
            await notifier.close()
            await store.close()

    app = FastAPI(title="Elegoo Connector", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.store = store
    app.state.supervisor = supervisor
    app.state.camera = camera
    app.state.uploads = uploads
    app.state.usage = usage
    app.state.notifier = notifier

    app.add_middleware(ShellRevalidationMiddleware)

    session = Depends(require_session(config))

    # -- auth ---------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """Unauthenticated liveness plus enough context for a tunnel or uptime check."""
        printer = store._printer  # noqa: SLF001 - read-only peek, no snapshot copy needed
        return {
            "ok": True,
            "auth": "enabled" if config.auth_enabled else "disabled",
            "control": config.control,
            "uptime_s": int(time.time() - started_at),
            "printer": None if printer is None else printer.get("state"),
            "connection": store._conn.state,  # noqa: SLF001
        }

    async def metrics_auth(request: Request) -> None:
        """A scraper has no cookie jar: a bearer token, or the session, or nothing."""
        if config.metrics_token:
            header = request.headers.get("authorization", "")
            token = header[7:].strip() if header.lower().startswith("bearer ") else request.query_params.get("token")
            if not same_token(config.metrics_token, token):
                raise HTTPException(status_code=401, detail="invalid metrics token")
            return
        if not is_authenticated(config, request):
            raise HTTPException(status_code=401, detail="not authenticated")

    @app.get("/metrics", dependencies=[Depends(metrics_auth)])
    async def metrics() -> PlainTextResponse:
        snapshot = store.snapshot()
        body = render_metrics(
            snapshot=snapshot,
            counters=store.counters(),
            usage=snapshot.get("usage") or usage.snapshot(),
            notifications=snapshot.get("notifications") or notifier.status(),
            camera_viewers=len(getattr(camera, "_subscribers", ()) or ()),
            uptime_s=int(time.time() - started_at),
        )
        return PlainTextResponse(body, media_type=METRICS_TYPE)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        if not config.auth_enabled or is_authenticated(config, request):
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8>"
                "<meta http-equiv=refresh content='0;url=/'>"
                "<p>Signed in. <a href='/'>Continue</a>.",
                status_code=200,
            )
        return HTMLResponse(LOGIN_PAGE)

    @app.post("/api/login")
    async def login(request: Request, body: LoginBody) -> Response:
        key = request.headers.get("cf-connecting-ip") or (
            request.client.host if request.client else "unknown"
        )
        if not login_allowed(key):
            raise HTTPException(status_code=429, detail="too many attempts; try again later")
        if not check_password(config, body.password):
            record_failed_login(key)
            raise HTTPException(status_code=401, detail="invalid password")
        reset_login_attempts(key)
        response = Response(status_code=204)
        response.set_cookie(
            config.cookie_name,
            issue_token(config),
            max_age=config.session_ttl_s,
            httponly=True,
            samesite="lax",
            secure=False,  # the tunnel terminates TLS; loopback and LAN are http
            path="/",
        )
        return response

    @app.post("/api/logout")
    async def logout() -> Response:
        response = Response(status_code=204)
        response.delete_cookie(config.cookie_name, path="/")
        return response

    # -- state --------------------------------------------------------------

    @app.get("/api/state", dependencies=[session])
    async def get_state() -> dict[str, Any]:
        return store.snapshot()

    @app.get("/api/events", dependencies=[session])
    async def events(request: Request) -> StreamingResponse:
        queue = store.subscribe()

        async def stream():
            try:
                yield _sse(store.snapshot())
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if not item:
                        return
                    yield _sse(item)
            finally:
                store.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.get("/api/usage", dependencies=[session])
    async def get_usage() -> dict[str, Any]:
        return usage.snapshot()

    @app.get("/api/diagnostics", dependencies=[session])
    async def diagnostics() -> dict[str, Any]:
        return {
            "snapshot": store.snapshot(),
            "counters": store.counters(),
            "usage": usage.snapshot(),
            "notifications": notifier.status(),
            "config": {
                "printer_host": config.printer_host,
                "control": config.control,
                "bind": config.bind,
                "port": config.port,
                "poll_ms": config.poll_ms,
                "push_ms": config.push_ms,
                "auth": "enabled" if config.auth_enabled else "disabled",
                "state_dir": str(config.state_dir),
                "web_dir": str(config.web_dir),
            },
        }

    # -- camera -------------------------------------------------------------

    def _camera_available() -> bool:
        return bool(config.printer_host)

    @app.get("/api/camera", dependencies=[session])
    async def camera_info() -> dict[str, Any]:
        viewers = len(getattr(camera, "_subscribers", ()) or ())
        return {
            "available": _camera_available(),
            "viewers": viewers,
            "frame_age_s": snapshots.frame_age_s,
            "url": camera_endpoint.current_url,
        }

    @app.get("/api/camera/snapshot", dependencies=[session])
    async def camera_snapshot_route() -> Response:
        if not _camera_available():
            raise HTTPException(status_code=502, detail="no printer configured")
        url = await camera_endpoint.resolve()
        try:
            frame = await snapshots.get(lambda: camera_snapshot(url=url))
        except Exception as err:  # noqa: BLE001 - httpx errors vary by version
            # The cached address may be stale (firmware change, printer reboot on a
            # new port): forget both, so the next request probes and reads again.
            camera_endpoint.invalidate()
            snapshots.invalidate()
            raise HTTPException(status_code=502, detail=f"camera unavailable: {err}") from err
        return Response(content=frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/camera/stream", dependencies=[session])
    async def camera_stream() -> StreamingResponse:
        if not _camera_available():
            raise HTTPException(status_code=502, detail="no printer configured")
        await camera_endpoint.resolve()
        try:
            media_type, chunks = await camera.subscribe()
        except CameraUnavailable as err:
            camera_endpoint.invalidate()
            raise HTTPException(status_code=502, detail=str(err)) from err

        async def stream():
            async for chunk in chunks:
                yield chunk

        return StreamingResponse(
            stream(),
            media_type=media_type,
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # -- files / history ----------------------------------------------------

    @app.get("/api/files", dependencies=[session])
    async def list_files(storage: Literal["local", "udisk"] = "local") -> dict[str, Any]:
        printer = _printer_or_502(supervisor)
        try:
            raw = await printer.list_files(storage)
        except (PrinterError, RequestTimeoutError) as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        files = [
            {
                "filename": entry.get("filename"),
                "size": entry.get("size"),
                "layers": entry.get("layer"),
                "created_ts": _as_ts(entry.get("create_time")),
            }
            for entry in raw.get("file_list", [])
        ]
        return {"files": files, "total": raw.get("total", len(files))}

    @app.get("/api/history", dependencies=[session])
    async def history() -> dict[str, Any]:
        printer = _printer_or_502(supervisor)
        try:
            raw = await printer.print_history()
        except (PrinterError, RequestTimeoutError) as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        jobs = [
            {
                "name": entry.get("task_name"),
                "status": HISTORY_STATUS.get(entry.get("task_status"), "unknown"),
                "started_ts": _as_ts(entry.get("begin_time")),
                "ended_ts": _as_ts(entry.get("end_time")),
            }
            for entry in raw.get("history_task_list", [])
        ]
        jobs.reverse()  # newest first for the UI
        return {"jobs": jobs}

    # -- uploads ------------------------------------------------------------

    @app.get("/api/uploads", dependencies=[session])
    async def list_uploads() -> dict[str, Any]:
        return {"uploads": uploads.public()}

    @app.delete("/api/uploads/{upload_id}", dependencies=[session])
    async def cancel_upload(request: Request, upload_id: str) -> dict[str, Any]:
        _require_control(config)
        require_same_origin(request)
        await uploads.cancel(upload_id)
        return {"cancelled": upload_id}

    @app.post("/api/uploads", dependencies=[session])
    async def create_upload(
        request: Request,
        file: UploadFile = File(...),
        start: bool = Form(False),
    ) -> dict[str, Any]:
        _require_control(config)
        require_same_origin(request)
        name = Path(file.filename or "upload.gcode").name
        temp_dir = Path(config.state_dir) / "uploads"
        temp_dir.mkdir(parents=True, exist_ok=True)
        target = temp_dir / f"{uuid.uuid4().hex[:10]}-{name}"
        total = 0
        try:
            with target.open("wb") as handle:
                while chunk := await file.read(UPLOAD_CHUNK):
                    total += len(chunk)
                    if total > config.upload_max_bytes:
                        raise HTTPException(status_code=413, detail="file too large")
                    handle.write(chunk)
            if total == 0:
                raise HTTPException(status_code=422, detail="empty file")
            item = await uploads.start(target, name, total, auto_start=start)
        except HTTPException:
            target.unlink(missing_ok=True)
            raise
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {"upload_id": item["id"], "filename": item["filename"]}

    # -- mutations (only when control is enabled) ---------------------------

    if config.control:

        @app.post("/api/print/start", dependencies=[session])
        async def start_print(request: Request, body: StartBody) -> dict[str, Any]:
            require_same_origin(request)
            printer = _printer_or_502(supervisor)
            return await _command(
                "start print",
                lambda: printer.start_print(
                    body.filename, storage=body.storage, auto_leveling=body.auto_leveling, timelapse=body.timelapse
                ),
            )

        @app.post("/api/print/pause", dependencies=[session])
        async def pause(request: Request) -> dict[str, Any]:
            require_same_origin(request)
            printer = _printer_or_502(supervisor)
            return await _command("pause", printer.pause)

        @app.post("/api/print/resume", dependencies=[session])
        async def resume(request: Request) -> dict[str, Any]:
            require_same_origin(request)
            printer = _printer_or_502(supervisor)
            return await _command("resume", printer.resume)

        @app.post("/api/print/stop", dependencies=[session])
        async def stop(request: Request) -> dict[str, Any]:
            require_same_origin(request)
            printer = _printer_or_502(supervisor)
            return await _command("stop", printer.stop)

        @app.post("/api/print/speed", dependencies=[session])
        async def speed(request: Request, body: SpeedBody) -> dict[str, Any]:
            require_same_origin(request)
            printer = _printer_or_502(supervisor)
            return await _command(f"speed {body.mode}", lambda: printer.set_print_speed(body.mode))

        @app.post("/api/print/fans", dependencies=[session])
        async def fans(request: Request, body: FansBody) -> dict[str, Any]:
            require_same_origin(request)
            if body.model is None and body.auxiliary is None and body.chamber is None:
                raise HTTPException(status_code=422, detail="specify at least one fan")
            printer = _printer_or_502(supervisor)
            return await _command(
                "fan speeds",
                lambda: printer.set_fan_speed(model=body.model, auxiliary=body.auxiliary, chamber=body.chamber),
            )

        @app.post("/api/print/temps", dependencies=[session])
        async def temps(request: Request, body: TempsBody) -> dict[str, Any]:
            require_same_origin(request)
            if body.nozzle is None and body.bed is None and body.chamber is None:
                raise HTTPException(status_code=422, detail="specify at least one target")
            printer = _printer_or_502(supervisor)
            return await _command(
                "heater targets",
                lambda: printer.set_temperatures(nozzle=body.nozzle, bed=body.bed, chamber=body.chamber),
            )

        @app.post("/api/print/light", dependencies=[session])
        async def light(request: Request, body: LightBody) -> dict[str, Any]:
            require_same_origin(request)
            printer = _printer_or_502(supervisor)
            return await _command("light", lambda: printer.set_light(body.on))

        @app.post("/api/maintenance/reset", dependencies=[session])
        async def reset_maintenance(request: Request) -> dict[str, Any]:
            require_same_origin(request)
            result = usage.reset_maintenance()
            return {"maintenance": result}

        @app.post("/api/files/delete", dependencies=[session])
        async def delete_files(request: Request, body: DeleteBody) -> dict[str, Any]:
            require_same_origin(request)
            printer = _printer_or_502(supervisor)
            try:
                result = await printer.delete_files(body.filenames, body.storage)
            except PrinterError as err:
                raise HTTPException(status_code=409, detail=str(err)) from err
            store.record_command(f"Deleted {len(body.filenames)} file(s)")
            return result

    # -- the write path -----------------------------------------------------

    async def _command(description: str, action) -> dict[str, Any]:
        printer = supervisor.printer
        if printer is None:
            raise HTTPException(status_code=502, detail="printer is offline")
        async with write_lock:
            gap = time.monotonic() - last_write["ts"]
            if gap < MIN_WRITE_INTERVAL_S:
                await asyncio.sleep(MIN_WRITE_INTERVAL_S - gap)
            try:
                message = await action()
            except ControlDisabledError as err:
                raise HTTPException(status_code=403, detail=str(err)) from err
            except (ValueError, AssertionError) as err:
                raise HTTPException(status_code=422, detail=str(err)) from err
            except (PrinterError, RequestTimeoutError) as err:
                raise HTTPException(status_code=502, detail=str(err)) from err
            last_write["ts"] = time.monotonic()
        store.record_command(description.capitalize())
        ack = _ack_of(message)
        if ack not in (0, None):
            raise HTTPException(status_code=409, detail=f"printer rejected the command (Ack={ack})")
        return {"accepted": True, "request_id": _request_id_of(message), "status": "sent"}

    # -- static shell (mounted last so /api wins) ---------------------------

    if config.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(config.web_dir), html=True), name="web")
    else:

        @app.get("/")
        async def missing_shell() -> JSONResponse:
            return JSONResponse(
                {"detail": f"web directory not found: {config.web_dir}"}, status_code=500
            )

    return app


# --------------------------------------------------------------------------- utils


class ShellRevalidationMiddleware:
    """Force revalidation of the app shell.

    The UI is updated with ``git pull`` on the Pi, so a heuristically cached
    ``index.html``/``app.js`` would keep a phone on the previous dashboard for hours.
    The API and camera routes set their own cache headers.

    Deliberately a pure-ASGI middleware rather than ``@app.middleware("http")``:
    Starlette's ``BaseHTTPMiddleware`` wraps every request in an anyio task group, which
    deadlocks under an in-process ASGI transport (and costs an extra task per stream).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if method not in ("GET", "HEAD") or path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("cache-control", "no-cache")
            await send(message)

        await self.app(scope, receive, send_with_header)


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_metrics(
    *,
    snapshot: dict[str, Any],
    counters: dict[str, int],
    usage: dict[str, Any],
    notifications: dict[str, Any],
    camera_viewers: int,
    uptime_s: int,
) -> str:
    """Prometheus text exposition (0.0.4), built from the snapshot we already have.

    Only facts the printer reported: a gauge is absent rather than zero when the value
    is unknown, because a graph of "0 °C" while the link is down is a lie (P6).
    """
    connection = snapshot.get("connection") or {}
    printer = snapshot.get("printer") or None
    job = (printer or {}).get("job") or {}
    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")

    def sample(name: str, value: Any, **labels: Any) -> None:
        if value is None:
            return
        if labels:
            rendered = ",".join(f'{key}="{_label(val)}"' for key, val in labels.items())
            lines.append(f"{name}{{{rendered}}} {value}")
        else:
            lines.append(f"{name} {value}")

    metric("ec_bridge_up", "gauge", "1 while the bridge is serving requests.")
    sample("ec_bridge_up", 1)
    metric("ec_bridge_uptime_seconds", "gauge", "Seconds since the bridge started.")
    sample("ec_bridge_uptime_seconds", uptime_s)
    metric("ec_bridge_frames_total", "counter", "Status frames received from the printer.")
    sample("ec_bridge_frames_total", counters.get("frames"))
    metric("ec_bridge_commands_total", "counter", "Commands the bridge accepted.")
    sample("ec_bridge_commands_total", counters.get("commands"))
    metric("ec_bridge_reconnects_total", "counter", "Printer link reconnects.")
    sample("ec_bridge_reconnects_total", counters.get("reconnects"))

    metric("ec_printer_connected", "gauge", "1 when the printer WebSocket is online.")
    sample("ec_printer_connected", 1 if connection.get("state") == "online" else 0)
    metric("ec_printer_stale", "gauge", "1 when the last status frame is older than the stale threshold.")
    sample("ec_printer_stale", None if printer is None else int(bool(printer.get("stale"))))
    metric("ec_printer_state", "gauge", "Current printer state; 1 for the active slug.")
    if printer and printer.get("state"):
        lines.append(f'ec_printer_state{{state="{_label(printer["state"])}"}} 1')
    metric("ec_job_active", "gauge", "1 while a job occupies the printer.")
    sample("ec_job_active", 1 if job else 0)
    metric("ec_print_progress_percent", "gauge", "Progress of the current job.")
    sample("ec_print_progress_percent", job.get("progress"))
    metric("ec_print_layer", "gauge", "Current layer of the active job.")
    sample("ec_print_layer", job.get("layer"))
    metric("ec_print_layers_total", "gauge", "Layer count of the active job.")
    sample("ec_print_layers_total", job.get("total_layers"))
    metric("ec_print_remaining_seconds", "gauge", "Seconds remaining on the active job.")
    sample("ec_print_remaining_seconds", job.get("remaining_s"))

    metric("ec_temperature_celsius", "gauge", "Temperature per heater, actual or target.")
    for heater, reading in ((printer or {}).get("temps") or {}).items():
        for kind in ("actual", "target"):
            value = (reading or {}).get(kind)
            if value is not None:
                lines.append(f'ec_temperature_celsius{{heater="{_label(heater)}",kind="{kind}"}} {value}')
    metric("ec_fan_percent", "gauge", "Fan speed per fan, as the printer reports it.")
    for fan, value in ((printer or {}).get("fans") or {}).items():
        if value is not None:
            lines.append(f'ec_fan_percent{{fan="{_label(fan)}"}} {value}')

    metric("ec_camera_viewers", "gauge", "Browsers currently watching the MJPEG stream.")
    sample("ec_camera_viewers", camera_viewers)

    metric("ec_usage_print_seconds_total", "counter", "Print time the bridge has counted, in seconds.")
    sample("ec_usage_print_seconds_total", usage.get("print_seconds"))
    metric("ec_usage_jobs_total", "counter", "Jobs by result.")
    for result, value in (usage.get("jobs") or {}).items():
        lines.append(f'ec_usage_jobs_total{{result="{_label(result)}"}} {value}')
    metric("ec_maintenance_due", "gauge", "1 when the configured service interval has passed.")
    maintenance = usage.get("maintenance") or {}
    sample("ec_maintenance_due", 1 if maintenance.get("due") else 0)
    metric("ec_maintenance_hours_since_service", "gauge", "Print hours since the counter was reset.")
    sample("ec_maintenance_hours_since_service", maintenance.get("hours_since"))

    metric("ec_notifications_sent_total", "counter", "Delivered push notifications.")
    sample("ec_notifications_sent_total", notifications.get("sent"))
    metric("ec_notifications_failed_total", "counter", "Notifications that could not be delivered.")
    sample("ec_notifications_failed_total", notifications.get("failed"))
    metric("ec_notifications_dropped_total", "counter", "Notifications dropped because the queue was full.")
    sample("ec_notifications_dropped_total", notifications.get("dropped"))

    return "\n".join(lines) + "\n"


def _printer_or_502(supervisor: Supervisor):
    if supervisor.printer is None:
        raise HTTPException(status_code=502, detail="printer is offline")
    return supervisor.printer


def _require_control(config: Config) -> None:
    if not config.control:
        raise HTTPException(
            status_code=403, detail="control is disabled (start the bridge with EC_CONTROL=1)"
        )


def _request_id_of(message: Any) -> str | None:
    try:
        return (message.inner or {}).get("RequestID")
    except AttributeError:
        return None


def _as_ts(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Some firmware sends milliseconds, some seconds.
        return float(value) / 1000.0 if value > 1e11 else float(value)
    return None


def app() -> FastAPI:  # uvicorn elegoo_bridge.server:app
    return create_app()
