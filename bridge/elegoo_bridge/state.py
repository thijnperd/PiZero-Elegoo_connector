"""The one canonical printer state, plus the normalisation rules.

Design rules this file encodes (see docs/DESIGN_PHILOSOPHY.md):

* The printer is the authority; this store is a projection. It never invents values.
* Requested != confirmed: we only publish what a status frame actually said.
* Stale is a state: after ``STALE_AFTER_S`` without a frame, values are marked stale
  instead of silently looking current.
* One object, sent whole. No deltas to reconcile.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from cc1client.models import Attributes, PrintStatus, Status

SCHEMA = 1
STALE_AFTER_S = 10.0
COALESCE_S = 0.4
LOG_MAX = 50

#: ``PrintInfo.Status`` -> (slug, label, needs-human). Codes 0-22 come from the
#: firmware's own enum (see cc1client/models.py); 23+ are resin leftovers that the
#: Centauri Carbon does not normally emit but costs nothing to map.
STATE_MAP: dict[int, tuple[str, str, bool]] = {
    0: ("idle", "Idle", False),
    1: ("homing", "Homing", False),
    2: ("dropping", "Dropping", False),
    3: ("exposing", "Exposing", False),
    4: ("lifting", "Lifting", False),
    5: ("pausing", "Pausing", False),
    6: ("paused", "Paused", True),
    7: ("stopping", "Stopping", False),
    8: ("stopped", "Stopped", False),
    9: ("completed", "Completed", False),
    10: ("checking", "Checking file", False),
    11: ("checking", "Checking printer", False),
    12: ("resuming", "Resuming", False),
    13: ("printing", "Printing", False),
    14: ("error", "Error", True),
    15: ("leveling", "Auto-levelling", False),
    16: ("preheating", "Preheating", False),
    17: ("calibrating", "Resonance testing", False),
    18: ("starting", "Starting print", False),
    19: ("leveling", "Levelling complete", False),
    20: ("preheating", "Preheating complete", False),
    21: ("homing", "Homing complete", False),
    22: ("calibrating", "Calibration complete", False),
    23: ("feeding", "Feeding filament", False),
    24: ("unloading", "Unloading filament", False),
    25: ("unloading", "Unloading abnormal", True),
    26: ("unloading", "Unloading paused", True),
}
UNKNOWN_STATE = ("unknown", "Unknown", False)

#: States in which a job is actually occupying the machine.
JOB_STATES = {
    "homing",
    "leveling",
    "preheating",
    "printing",
    "pausing",
    "paused",
    "resuming",
    "stopping",
    "starting",
    "calibrating",
    "checking",
}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _int(value: Any) -> int | None:
    got = _num(value)
    return int(got) if got is not None else None


def _temp(pair: Any, explicit_target: Any) -> dict[str, float | None]:
    """Normalise one temperature reading.

    Firmware ships two shapes (see cc1client/models.py): a ``[target, actual]`` pair in
    ``TempOf*`` with no separate target, or a scalar ``TempOf*`` plus ``TempTarget*``.
    """
    actual: float | None = None
    target: float | None = None
    if isinstance(pair, (list, tuple)) and len(pair) >= 2:
        target = _num(pair[0])
        actual = _num(pair[1])
    else:
        actual = _num(pair)
    explicit = _num(explicit_target)
    if explicit is not None:
        target = explicit
    return {"actual": actual, "target": target}


def state_for(code: int | None) -> tuple[str, str, bool]:
    if code is None:
        return UNKNOWN_STATE
    return STATE_MAP.get(int(code), ("unknown", f"Unknown ({code})", False))


def normalise_status(status: Status, *, now: float | None = None) -> dict[str, Any]:
    """Turn a wire ``Status`` into the stable shape the UI consumes."""
    now = time.time() if now is None else now
    info = status.print_info
    code = status.print_status if status.print_status is not None else status.state
    slug, label, needs_human = state_for(code)

    raw = status.raw or {}
    job: dict[str, Any] | None = None
    if info is not None:
        elapsed = _num(info.current_ticks)
        total = _num(info.total_ticks)
        remaining = None
        if elapsed is not None and total is not None and total >= elapsed > 0:
            remaining = total - elapsed
        progress = _num(info.progress)
        if progress is None and elapsed and total:
            progress = round(elapsed / total * 100.0, 1)
        has_job = bool(info.filename) and (slug in JOB_STATES or slug in {"completed", "stopped"})
        if has_job or (info.filename and code not in (0, None)):
            job = {
                "filename": (info.filename or "").rsplit("/", 1)[-1] or None,
                "progress": None if progress is None else max(0.0, min(100.0, round(progress, 1))),
                "layer": _int(info.current_layer),
                "total_layers": _int(info.total_layer),
                "elapsed_s": None if elapsed is None else int(elapsed),
                "remaining_s": None if remaining is None else int(remaining),
                "eta_ts": None if remaining is None else round(now + remaining, 1),
                "task_id": info.task_id,
                "speed_pct": _int(info.print_speed),
            }

    fans_raw = status.fan_speed or {}
    fans = {
        "model": _int(fans_raw.get("ModelFan")),
        "auxiliary": _int(fans_raw.get("AuxiliaryFan")),
        "chamber": _int(fans_raw.get("BoxFan")),
    }

    light_raw = status.light or {}
    light: bool | None = None
    if "SecondLight" in light_raw:
        light = bool(_int(light_raw.get("SecondLight")))

    error_code = _int(info.err_num) if info is not None else None
    error = None
    if error_code:
        error = {"code": error_code, "message": f"Printer error {error_code}"}

    return {
        "state": slug,
        "state_code": _int(code),
        "state_label": label,
        "attention": needs_human or bool(error_code),
        "error": error,
        "job": job,
        "temps": {
            "nozzle": _temp(raw.get("TempOfNozzle"), raw.get("TempTargetNozzle")),
            "bed": _temp(raw.get("TempOfHotbed"), raw.get("TempTargetHotbed")),
            "chamber": _temp(raw.get("TempOfBox"), raw.get("TempTargetBox")),
        },
        "fans": fans,
        "light": light,
        "position": list(status.coord) if status.coord else None,
    }


@dataclass
class LogEntry:
    ts: float
    level: str
    kind: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"ts": round(self.ts, 1), "level": self.level, "kind": self.kind, "message": self.message}


@dataclass
class _Conn:
    state: str = "connecting"
    since_ts: float = field(default_factory=time.time)
    firmware: str | None = None
    mainboard_id: str | None = None
    reconnects: int = 0
    last_error: str | None = None


class StateStore:
    """Holds the latest snapshot and fans changes out to SSE subscribers."""

    def __init__(self, *, printer_host: str, control: bool, camera: bool = True) -> None:
        self.printer_host = printer_host
        self.control = control
        self.camera_available = camera
        self._conn = _Conn()
        self._printer: dict[str, Any] | None = None
        self._last_frame_ts: float | None = None
        self._log: deque[LogEntry] = deque(maxlen=LOG_MAX)
        self._previous_slug: str | None = None
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._dirty = asyncio.Event()
        self._flusher: asyncio.Task[None] | None = None
        self._closing = False
        self._counter = {"frames": 0, "commands": 0, "reconnects": 0, "uploads": 0}

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._flusher is None or self._flusher.done():
            self._dirty.clear()
            self._flusher = asyncio.create_task(self._flush_loop(), name="state-flusher")

    async def close(self) -> None:
        self._closing = True
        if self._flusher is not None and not self._flusher.done():
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._flusher
        for queue in list(self._queues):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait({})
        self._queues.clear()

    # --- mutations ---------------------------------------------------------

    def log(self, message: str, *, level: str = "info", kind: str = "notice") -> None:
        self._log.appendleft(LogEntry(time.time(), level, kind, message))
        self._mark_dirty()

    def set_connection(self, state: str, *, error: str | None = None) -> None:
        if state != self._conn.state:
            self._conn.state = state
            self._conn.since_ts = time.time()
            if state == "online":
                self._conn.reconnects += 1
                self._counter["reconnects"] += 1
                self.log("Connected to printer", kind="connection")
            elif state == "offline":
                self.log(f"Printer unreachable{f': {error}' if error else ''}", level="warn", kind="connection")
        if error:
            self._conn.last_error = error
        elif state == "online":
            self._conn.last_error = None
        self._mark_dirty()

    def set_attributes(self, attributes: Attributes, mainboard_id: str | None = None) -> None:
        if attributes.firmware_version:
            self._conn.firmware = attributes.firmware_version
        if attributes.mainboard_id or mainboard_id:
            self._conn.mainboard_id = attributes.mainboard_id or mainboard_id
        self._mark_dirty()

    def set_status(self, status: Status) -> None:
        now = time.time()
        self._last_frame_ts = now
        self._counter["frames"] += 1
        printer = normalise_status(status, now=now)
        printer["stale"] = False
        printer["stale_for_s"] = 0
        self._printer = printer
        self._announce_transition(printer)
        self._mark_dirty()

    def record_command(self, message: str, *, level: str = "info") -> None:
        self._counter["commands"] += 1
        self.log(message, level=level, kind="command")

    # --- reads -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        age = None if self._last_frame_ts is None else max(0.0, now - self._last_frame_ts)
        printer = dict(self._printer) if self._printer else None
        if printer is not None:
            stale = age is None or age > STALE_AFTER_S
            printer["stale"] = stale
            printer["stale_for_s"] = 0 if age is None else round(age, 1)
        return {
            "schema": SCHEMA,
            "ts": round(now, 3),
            "connection": {
                "state": self._conn.state,
                "since_ts": round(self._conn.since_ts, 1),
                "printer_host": self.printer_host,
                "firmware": self._conn.firmware,
                "mainboard_id": self._conn.mainboard_id,
                "reconnects": self._conn.reconnects,
                "last_frame_age_s": None if age is None else round(age, 1),
                "last_error": self._conn.last_error,
            },
            "capabilities": {
                "control": self.control,
                "camera": self.camera_available,
                "files": True,
                "upload": self.control,
                "history": True,
            },
            "printer": printer,
            "log": [entry.as_dict() for entry in self._log],
        }

    def counters(self) -> dict[str, int]:
        return dict(self._counter)

    # --- fan-out -----------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(queue)

    def _mark_dirty(self) -> None:
        self._dirty.set()

    async def _flush_loop(self) -> None:
        while not self._closing:
            await self._dirty.wait()
            self._dirty.clear()
            await asyncio.sleep(COALESCE_S)
            snapshot = self.snapshot()
            for queue in list(self._queues):
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(snapshot)

    # --- transitions -------------------------------------------------------

    def _announce_transition(self, printer: dict[str, Any]) -> None:
        slug = printer["state"]
        previous = self._previous_slug
        self._previous_slug = slug
        if slug == previous:
            return
        job = printer.get("job") or {}
        name = job.get("filename") or "print"
        if slug == "printing" and previous in (None, "idle", "starting", "leveling", "preheating"):
            self.log(f"Started {name}", kind="job_started")
        elif slug == "completed":
            self.log(f"Finished {name}", kind="job_finished")
        elif slug == "error":
            self.log(f"Printer reported an error on {name}", level="error", kind="job_failed")
        elif slug == "paused":
            self.log("Print paused", level="warn", kind="job_paused")
        elif slug == "resuming":
            self.log("Resuming print", kind="job_resumed")
        elif slug == "stopped":
            self.log(f"Print stopped: {name}", level="warn", kind="job_stopped")
        elif slug == "preheating" and previous != "preheating":
            self.log("Preheating", kind="progress")
        elif slug == "leveling":
            self.log("Auto-levelling", kind="progress")
