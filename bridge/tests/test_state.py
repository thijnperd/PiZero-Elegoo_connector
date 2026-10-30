"""Normalisation and store behaviour — the honesty rules, in test form."""

from __future__ import annotations

import asyncio
import time

import pytest
from cc1client.models import Status

from elegoo_bridge.state import StateStore, normalise_status
from fake_printer import status_payload


def test_normalises_a_printing_job() -> None:
    now = 1_700_000_000.0
    status = Status.from_payload(status_payload())
    printer = normalise_status(status, now=now)

    assert printer["state"] == "printing"
    assert printer["state_label"] == "Printing"
    assert printer["attention"] is False
    assert printer["error"] is None

    job = printer["job"]
    assert job["filename"] == "cube.gcode"  # firmware sends "/local/cube.gcode"
    assert job["progress"] == 42
    assert job["layer"] == 120
    assert job["total_layers"] == 300
    assert job["elapsed_s"] == 1830
    assert job["remaining_s"] == 2500
    assert job["eta_ts"] == pytest.approx(now + 2500)
    assert job["speed_pct"] == 100

    assert printer["temps"]["nozzle"] == {"actual": 214.9, "target": 215.0}
    assert printer["temps"]["bed"] == {"actual": 59.8, "target": 60.0}
    assert printer["temps"]["chamber"] == {"actual": None, "target": None}
    assert printer["fans"] == {"model": 100, "auxiliary": 0, "chamber": 0}
    assert printer["light"] is True
    assert printer["position"] == [10.0, 20.0, 0.4]


def test_paused_needs_attention() -> None:
    printer = normalise_status(Status.from_payload(status_payload(state=6)))
    assert printer["state"] == "paused"
    assert printer["attention"] is True


def test_error_surfaces_code() -> None:
    payload = status_payload(state=14)
    payload["PrintInfo"]["ErrNum"] = 1042
    printer = normalise_status(Status.from_payload(payload))
    assert printer["state"] == "error"
    assert printer["attention"] is True
    assert printer["error"] == {"code": 1042, "message": "Printer error 1042"}


def test_idle_has_no_job() -> None:
    payload = status_payload(state=0, filename=None, progress=0)
    printer = normalise_status(Status.from_payload(payload))
    assert printer["state"] == "idle"
    assert printer["job"] is None


def test_unknown_state_code_does_not_crash() -> None:
    printer = normalise_status(Status.from_payload(status_payload(state=999)))
    assert printer["state"] == "unknown"
    assert printer["state_label"] == "Unknown (999)"


def test_temp_pair_format_is_understood() -> None:
    """Older firmware sends ``[target, actual]`` pairs with no explicit target."""
    payload = status_payload()
    payload.pop("TempTargetNozzle")
    payload["TempOfNozzle"] = [215.0, 214.9]
    printer = normalise_status(Status.from_payload(payload))
    assert printer["temps"]["nozzle"] == {"actual": 214.9, "target": 215.0}


def test_store_marks_stale_values() -> None:
    store = StateStore(printer_host="127.0.0.1", control=False)
    store.set_status(Status.from_payload(status_payload()))
    snapshot = store.snapshot()

    assert snapshot["printer"]["stale"] is False
    assert snapshot["connection"]["state"] == "connecting"
    assert snapshot["schema"] == 1

    store._last_frame_ts = time.time() - 30  # noqa: SLF001 - simulating a silent printer
    stale = store.snapshot()
    assert stale["printer"]["stale"] is True
    assert stale["printer"]["stale_for_s"] >= 29


def test_store_logs_job_transitions_only() -> None:
    store = StateStore(printer_host="127.0.0.1", control=True)
    store.set_status(Status.from_payload(status_payload(state=13)))
    store.set_status(Status.from_payload(status_payload(state=13, progress=43)))
    store.set_status(Status.from_payload(status_payload(state=6)))

    kinds = [entry["kind"] for entry in store.snapshot()["log"]]
    assert kinds.count("job_started") == 1
    assert "job_paused" in kinds
    # A progress update is not an event worth announcing.
    assert kinds.count("progress") == 0


async def test_subscribers_receive_coalesced_snapshots() -> None:
    """The SSE route is thin; this is the mechanism behind it."""
    store = StateStore(printer_host="127.0.0.1", control=False)
    store.start()
    queue = store.subscribe()
    try:
        store.set_status(Status.from_payload(status_payload()))
        snapshot = await asyncio.wait_for(queue.get(), timeout=3.0)
        assert snapshot["schema"] == 1
        assert snapshot["printer"]["state"] == "printing"
        assert snapshot["printer"]["job"]["progress"] == 42
    finally:
        store.unsubscribe(queue)
        await store.close()


def test_capabilities_follow_config() -> None:
    read_only = StateStore(printer_host="x", control=False)
    snapshot = read_only.snapshot()
    assert snapshot["capabilities"]["control"] is False
    assert snapshot["capabilities"]["upload"] is False

    controllable = StateStore(printer_host="x", control=True)
    assert controllable.snapshot()["capabilities"]["control"] is True
