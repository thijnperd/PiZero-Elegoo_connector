"""Run the bridge against a fake printer — develop the dashboard with no hardware.

    python scripts/dev_fake.py            # http://127.0.0.1:8800 (no login)

It serves the real `web/` directory through the real bridge, so what you see is what
runs on the Pi; only the printer is simulated. Auth is off here on purpose.
"""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "bridge" / "tests"))

import cc1client.camera as cc1_camera  # noqa: E402
import cc1client.client as cc1_client  # noqa: E402
import cc1client.upload as cc1_upload  # noqa: E402
from elegoo_bridge.config import Config  # noqa: E402
from elegoo_bridge.server import create_app  # noqa: E402
from fake_printer import MAINBOARD, FakePrinter  # noqa: E402


async def main() -> None:
    fake = await FakePrinter().start()
    cc1_client.WS_PORT = fake.ws_port
    cc1_upload.UPLOAD_PORT = fake.http_port
    cc1_camera.CAMERA_PORT = fake.http_port

    config = Config(
        printer_host="127.0.0.1",
        mainboard_id=MAINBOARD,
        bind="127.0.0.1",
        port=8800,
        control=True,
        poll_ms=1500,
        push_ms=1000,
        camera_port=fake.http_port,
        camera_path="/video",
        state_dir=ROOT / ".dev-state",
        password=None,  # no login in dev
        admin_user="dev",
        session_ttl_s=3600,
        web_dir=ROOT / "web",
        log_level="INFO",
        upload_max_bytes=64 * 1024 * 1024,
    )
    server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=8800, log_level="info"))
    try:
        await server.serve()
    finally:
        await fake.stop()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
