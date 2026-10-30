"""Unit tests for camera endpoint discovery.

The resolver is what makes the camera work on a CC1 whose stream is not on the
documented port, so the fallback path is the interesting one.
"""

from __future__ import annotations

import socket

import pytest

from elegoo_bridge import camera as bridge_camera
from fake_printer import FakePrinter


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def fake():
    printer = FakePrinter()
    await printer.start()
    try:
        yield printer
    finally:
        await printer.stop()


async def test_configured_port_wins(fake) -> None:
    endpoint = bridge_camera.CameraEndpoint("127.0.0.1", port=fake.http_port)
    assert await endpoint.resolve() == f"http://127.0.0.1:{fake.http_port}/video"
    assert endpoint.current_url == f"http://127.0.0.1:{fake.http_port}/video"


async def test_falls_back_to_an_alternative_port(fake, monkeypatch) -> None:
    monkeypatch.setattr(bridge_camera, "CANDIDATE_PORTS", (fake.http_port,))
    endpoint = bridge_camera.CameraEndpoint("127.0.0.1", port=free_port())
    assert await endpoint.resolve() == f"http://127.0.0.1:{fake.http_port}/video"


async def test_nothing_answering_keeps_the_configured_url(monkeypatch) -> None:
    monkeypatch.setattr(bridge_camera, "CANDIDATE_PORTS", ())
    monkeypatch.setattr(bridge_camera, "PROBE_TIMEOUT_S", 0.5)
    port = free_port()
    endpoint = bridge_camera.CameraEndpoint("127.0.0.1", port=port)
    assert await endpoint.resolve() == f"http://127.0.0.1:{port}/video"
    assert endpoint.current_url == f"http://127.0.0.1:{port}/video"


async def test_invalidate_drops_the_cached_url(fake, monkeypatch) -> None:
    dead = free_port()
    monkeypatch.setattr(bridge_camera, "CANDIDATE_PORTS", (fake.http_port,))
    endpoint = bridge_camera.CameraEndpoint("127.0.0.1", port=dead)
    assert (await endpoint.resolve()).endswith(f":{fake.http_port}/video")

    endpoint.invalidate()
    # Back to the configured address until the next probe says otherwise.
    assert endpoint.current_url.endswith(f":{dead}/video")
