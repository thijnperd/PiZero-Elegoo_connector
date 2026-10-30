"""A fake Centauri Carbon for tests: SDCP WebSocket, upload endpoint and MJPEG.

It speaks just enough of the real wire format to exercise the bridge end to end —
the envelope shape, ``RequestID`` correlation, status pushes, the multipart upload
fields and the camera stream. Nothing here talks to real hardware.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import websockets
from aiohttp import web

MAINBOARD = "48551d180103147000001c0000000000"
FIRMWARE = "V1.1.46"

#: Smallest valid JPEG (1x1) so the bridge's SOI/EOI scan terminates.
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwcJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPDs0NDT/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def status_payload(
    *,
    state: int = 13,
    filename: str | None = "/local/cube.gcode",
    progress: int = 42,
    layer: int = 120,
    total_layers: int = 300,
    elapsed: float = 1830,
    total: float = 4330,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "CurrentStatus": [state],
        "TempOfNozzle": 214.9,
        "TempTargetNozzle": 215.0,
        "TempOfHotbed": 59.8,
        "TempTargetHotbed": 60.0,
        "CurrentFanSpeed": {"ModelFan": 100, "AuxiliaryFan": 0, "BoxFan": 0},
        "LightStatus": {"SecondLight": 1},
        "CurrenCoord": "10,20,0.4",
        "PrintInfo": {
            "Status": state,
            "Filename": filename or "",
            "CurrentLayer": layer,
            "TotalLayer": total_layers,
            "CurrentTicks": elapsed,
            "TotalTicks": total,
            "Progress": progress,
            "PrintSpeedPct": 100,
            "ErrNum": 0,
            "TaskId": "task-abc",
        },
    }
    return payload


class FakePrinter:
    def __init__(self) -> None:
        self.mainboard = MAINBOARD
        self.status = status_payload()
        self.commands: list[tuple[int, dict[str, Any]]] = []
        self.files: list[dict[str, Any]] = [
            {
                "name": "/local/cube.gcode",
                "FileSize": 123456,
                "TotalLayers": 300,
                "CreateTime": 1752300000,
            },
            {
                "name": "/local/benchy.gcode",
                "FileSize": 987654,
                "TotalLayers": 120,
                "CreateTime": 1752200000,
            },
        ]
        self.upload_chunks: list[dict[str, Any]] = []
        self.upload_bytes = 0
        self.connections = 0
        self.max_connections = 0
        self.ws_port = 0
        self.http_port = 0
        self.push_period_ms = 0
        self._ws_server: Any = None
        self._runner: web.AppRunner | None = None
        self._pusher: asyncio.Task[None] | None = None
        self._keep_alive = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> FakePrinter:
        self._ws_server = await websockets.serve(self._ws_handler, "127.0.0.1", 0)
        self.ws_port = self._ws_server.sockets[0].getsockname()[1]

        app = web.Application()
        app.router.add_post("/uploadFile/upload", self._handle_upload)
        app.router.add_get("/video", self._handle_video)
        app.router.add_get("/health", lambda _r: web.json_response({"ok": True}))
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.http_port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return self

    async def stop(self) -> None:
        if self._pusher is not None:
            self._pusher.cancel()
        self._keep_alive.set()
        if self._runner is not None:
            await self._runner.cleanup()
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()

    # -- message builders ---------------------------------------------------

    def _response_frame(self, cmd: int, payload: dict[str, Any], request_id: str) -> str:
        """A direct reply to a request: the payload nests under ``Data.Data``."""
        return json.dumps(
            {
                "Id": self.mainboard,
                "Topic": f"sdcp/response/{self.mainboard}",
                "Data": {
                    "Cmd": cmd,
                    "Data": payload,
                    "RequestID": request_id,
                    "MainboardID": self.mainboard,
                    "TimeStamp": int(time.time() * 1000),
                },
            }
        )

    def _push_frame(self, topic: str, payload: dict[str, Any], *, cmd: int) -> str:
        """An unsolicited push: the payload *is* ``Data``, with the routing fields mixed in."""
        data = dict(payload)
        data.update({"Cmd": cmd, "MainboardID": self.mainboard})
        return json.dumps(
            {
                "Id": self.mainboard,
                "Topic": f"sdcp/{topic}/{self.mainboard}",
                "Data": data,
            }
        )

    def status_frame(self) -> str:
        return self._push_frame("status", self.status, cmd=0)

    def attributes_frame(self) -> str:
        return self._push_frame(
            "attributes",
            {
                "Name": "Centauri Carbon",
                "MachineName": "Centauri Carbon",
                "BrandName": "ELEGOO",
                "FirmwareVersion": FIRMWARE,
                "ProtocolVersion": "3.0.0",
                "Capabilities": ["print", "file", "camera"],
            },
            cmd=1,
        )

    # -- websocket ----------------------------------------------------------

    async def _ws_handler(self, ws) -> None:
        self.connections += 1
        self.max_connections = max(self.max_connections, self.connections)
        try:
            await ws.send(self.attributes_frame())
            await ws.send(self.status_frame())
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                data = message.get("Data") or {}
                cmd = data.get("Cmd")
                payload = data.get("Data") or {}
                request_id = data.get("RequestID") or ""
                self.commands.append((int(cmd), payload))
                reply = self._respond(int(cmd), payload)
                if reply is not None:
                    await ws.send(self._response_frame(int(cmd), reply, request_id))
                if int(cmd) == 0:
                    await ws.send(self.status_frame())
                if int(cmd) == 512:
                    self.push_period_ms = int(payload.get("TimePeriod") or 0)
                    if self._pusher is None or self._pusher.done():
                        self._pusher = asyncio.create_task(self._push_loop(ws))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.connections -= 1

    async def _push_loop(self, ws) -> None:
        period = max(0.1, (self.push_period_ms or 1000) / 1000.0)
        try:
            while True:
                await asyncio.sleep(period)
                await ws.send(self.status_frame())
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            return

    def _respond(self, cmd: int, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Return the ``Data`` payload for the response envelope."""
        if cmd == 0:
            return {"Ack": 0}
        if cmd == 1:
            return {"Ack": 0}
        if cmd == 129:  # pause
            self.status["PrintInfo"]["Status"] = 6
            self.status["CurrentStatus"] = [6]
            return {"Ack": 0}
        if cmd == 131:  # resume
            self.status["PrintInfo"]["Status"] = 13
            self.status["CurrentStatus"] = [13]
            return {"Ack": 0}
        if cmd == 130:  # stop
            self.status["PrintInfo"]["Status"] = 8
            self.status["CurrentStatus"] = [8]
            return {"Ack": 0}
        if cmd == 128:  # start
            self.status["PrintInfo"]["Filename"] = payload.get("Filename", "")
            self.status["PrintInfo"]["Status"] = 13
            self.status["CurrentStatus"] = [13]
            self.status["PrintInfo"]["Progress"] = 0
            return {"Ack": 0}
        if cmd == 258:
            return {"Ack": 0, "FileList": list(self.files)}
        if cmd == 259:
            wanted = set(payload.get("FileList") or [])
            self.files = [f for f in self.files if f["name"] not in wanted]
            return {"Ack": 0}
        if cmd == 320:
            return {"Ack": 0, "HistoryData": ["task-1"]}
        if cmd == 321:
            return {
                "Ack": 0,
                "HistoryDetailList": [
                    {
                        "TaskName": "/local/cube.gcode",
                        "TaskStatus": 1,
                        "BeginTime": 1752300000,
                        "EndTime": 1752303600,
                        "AlreadyPrintLayer": 300,
                    }
                ],
            }
        if cmd == 512:
            return {"Ack": 0}
        if cmd == 403:
            return {"Ack": 0}
        return {"Ack": 0}

    # -- HTTP ---------------------------------------------------------------

    async def _handle_upload(self, request: web.Request) -> web.Response:
        form = await request.post()
        chunk = form.get("File")
        size = 0
        if chunk is not None and getattr(chunk, "file", None) is not None:
            size = len(chunk.file.read())
        self.upload_bytes += size
        self.upload_chunks.append(
            {
                "check": form.get("Check"),
                "md5": form.get("S-File-MD5"),
                "offset": form.get("Offset"),
                "uuid": form.get("Uuid"),
                "total": form.get("TotalSize"),
                "size": size,
                "filename": getattr(chunk, "filename", None),
            }
        )
        return web.json_response({"code": "000000", "messages": None, "data": {}, "success": True})

    async def _handle_video(self, _request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"},
        )
        await response.prepare(_request)
        try:
            for _ in range(3):
                await response.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(JPEG)).encode()
                    + b"\r\n\r\n"
                    + JPEG
                    + b"\r\n"
                )
                await asyncio.sleep(0.1)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        await response.write_eof()
        return response
