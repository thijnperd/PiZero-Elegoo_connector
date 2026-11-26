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
    # Idle first: a store that opens on a printing frame must not call that a start, or
    # every bridge restart mid-print would announce a job that began hours ago.
    store.set_status(Status.from_payload(status_payload(state=0, filename=None)))
    store.set_status(Status.from_payload(status_payload(state=13)))
    store.set_status(Status.from_payload(status_payload(state=13, progress=43)))
    store.set_status(Status.from_payload(status_payload(state=6)))

    kinds = [entry["kind"] for entry in store.snapshot()["log"]]
    assert kinds.count("job_started") == 1
    assert "job_paused" in kinds
    # A progress update is not an event worth announcing.
    assert kinds.count("progress") == 0


def test_opening_on_a_running_print_announces_nothing() -> None:
    store = StateStore(printer_host="127.0.0.1", control=True)
    seen: list[dict[str, object]] = []
    store.on_event(seen.append)
    store.set_status(Status.from_payload(status_payload(state=13)))

    assert seen == []
    assert [entry["kind"] for entry in store.snapshot()["log"]] == []


def test_transitions_reach_subscribers_once() -> None:
    store = StateStore(printer_host="127.0.0.1", control=True)
    seen: list[dict[str, object]] = []
    store.on_event(seen.append)
    store.set_status(Status.from_payload(status_payload(state=0, filename=None)))
    store.set_status(Status.from_payload(status_payload(state=13)))
    store.set_status(Status.from_payload(status_payload(state=13, progress=60)))
    store.set_status(Status.from_payload(status_payload(state=9)))

    assert [item["event"] for item in seen] == ["started", "finished"]
    assert seen[1]["job"]["filename"] == "cube.gcode"


def test_a_broken_subscriber_does_not_break_the_link() -> None:
    store = StateStore(printer_host="127.0.0.1", control=True)

    def boom(_event: dict[str, object]) -> None:
        raise RuntimeError("subscriber bug")

    store.on_event(boom)
    store.set_status(Status.from_payload(status_payload(state=0, filename=None)))
    # The transition still happened and still reached the log.
    store.set_status(Status.from_payload(status_payload(state=13)))
    assert [entry["kind"] for entry in store.snapshot()["log"]] == ["job_started"]


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


def test_first_connection_is_not_a_reconnect() -> None:
    """A freshly started bridge reports zero reconnects."""
    store = StateStore(printer_host="printer", control=False)

    store.set_connection("online")
    assert store.snapshot()["connection"]["reconnects"] == 0

    store.set_connection("offline", error="timeout")
    store.set_connection("online")
    assert store.snapshot()["connection"]["reconnects"] == 1

    store.set_connection("online")  # unchanged state must not bump the counter
    assert store.snapshot()["connection"]["reconnects"] == 1
