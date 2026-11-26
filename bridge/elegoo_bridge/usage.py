"""How many hours the machine has actually run, and when it was last serviced.

The printer reports a job's elapsed time but forgets it as soon as the next job starts,
so the only place a wear counter can live is here. Two questions this answers:

* *How much has this machine worked?* Total print hours, jobs completed/failed/stopped,
  today, and the last seven days as a small bar chart.
* *Is it time to look at the machine?* With ``EC_MAINTENANCE_HOURS`` set, the bridge
  tracks hours since the last acknowledgement and says so once, in the log, when the
  interval is reached. It is a reminder, not a schedule the firmware knows about.

The file is a single small JSON document, rewritten only on a transition, so a print
that runs overnight writes it twice. A corrupt file must not stop the bridge: it is
logged and replaced.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

VERSION = 1
#: Keep a month of daily buckets: enough to see a trend, small enough to stay in RAM.
KEEP_DAYS = 31
DAYS_SHOWN = 7


def _empty() -> dict[str, Any]:
    return {
        "version": VERSION,
        "print_seconds": 0.0,
        "jobs_started": 0,
        "jobs_completed": 0,
        "jobs_failed": 0,
        "jobs_stopped": 0,
        "days": {},
        "last_job": None,
        "maintenance": {"seconds_at_reset": 0.0, "last_reset_ts": None},
    }


def _day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


class UsageStore:
    """Persistent counters fed by the same transitions the notifier sees."""

    def __init__(
        self,
        path: Path,
        *,
        maintenance_hours: float = 0.0,
        on_log: Callable[..., None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.maintenance_hours = max(0.0, float(maintenance_hours))
        self._on_log = on_log
        self._data = self._read()
        self._due_announced = self._is_due()

    # --- persistence -------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return _empty()
        except (OSError, ValueError) as err:
            log.warning("usage file unreadable, starting over: %r", err)
            return _empty()
        if not isinstance(raw, dict) or raw.get("version") != VERSION:
            return _empty()
        data = _empty()
        for key in ("print_seconds", "jobs_started", "jobs_completed", "jobs_failed", "jobs_stopped"):
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                data[key] = float(value)
        days = raw.get("days")
        if isinstance(days, dict):
            data["days"] = {
                str(k): float(v)
                for k, v in days.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
        if isinstance(raw.get("last_job"), dict):
            data["last_job"] = raw["last_job"]
        maintenance = raw.get("maintenance")
        if isinstance(maintenance, dict):
            seconds = maintenance.get("seconds_at_reset")
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                data["maintenance"]["seconds_at_reset"] = float(seconds)
            reset_ts = maintenance.get("last_reset_ts")
            if isinstance(reset_ts, (int, float)) and not isinstance(reset_ts, bool):
                data["maintenance"]["last_reset_ts"] = float(reset_ts)
        return data

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, separators=(",", ":")), "utf-8")
            tmp.replace(self.path)
        except OSError as err:  # a read-only FS must not stop printing
            log.warning("could not save usage: %r", err)

    # --- intake ------------------------------------------------------------

    def handle(self, event: dict[str, Any], *, now: float | None = None) -> None:
        """Apply one transition. Unknown events are ignored, not guessed at."""
        now = time.time() if now is None else now
        kind = event.get("event")
        job = event.get("job") or {}
        seconds = job.get("elapsed_s")
        seconds = float(seconds) if isinstance(seconds, (int, float)) and seconds > 0 else 0.0

        if kind == "started":
            self._data["jobs_started"] += 1
            self._data["last_job"] = {
                "filename": job.get("filename"),
                "state": "started",
                "ended_ts": round(now, 1),
                "seconds": None,
            }
        elif kind in ("finished", "failed", "stopped"):
            counter = {"finished": "jobs_completed", "failed": "jobs_failed", "stopped": "jobs_stopped"}[kind]
            self._data[counter] += 1
            self._add_seconds(seconds, now)
            self._data["last_job"] = {
                "filename": job.get("filename"),
                "state": kind,
                "ended_ts": round(now, 1),
                "seconds": round(seconds, 1) if seconds else None,
            }
        else:
            return

        self._write()
        if self._is_due() and not self._due_announced:
            self._due_announced = True
            self._log(
                f"Maintenance due: {self.hours_since_service():.1f} h since the last service "
                f"(interval {self.maintenance_hours:.0f} h)",
                level="warn",
                kind="maintenance",
            )

    def _add_seconds(self, seconds: float, now: float) -> None:
        if seconds <= 0:
            return
        self._data["print_seconds"] += seconds
        bucket = _day(now)
        self._data["days"][bucket] = self._data["days"].get(bucket, 0.0) + seconds
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = _day(now - KEEP_DAYS * 86400)
        for key in [k for k in self._data["days"] if k < cutoff]:
            self._data["days"].pop(key, None)

    # --- maintenance -------------------------------------------------------

    def hours_since_service(self) -> float:
        since = self._data["print_seconds"] - self._data["maintenance"]["seconds_at_reset"]
        return round(max(0.0, since) / 3600.0, 1)

    def _is_due(self) -> bool:
        return self.maintenance_hours > 0 and self.hours_since_service() >= self.maintenance_hours

    def reset_maintenance(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        self._data["maintenance"] = {
            "seconds_at_reset": self._data["print_seconds"],
            "last_reset_ts": round(now, 1),
        }
        self._due_announced = False
        self._write()
        self._log("Maintenance counter reset", kind="maintenance")
        return self.snapshot(now=now)["maintenance"]

    # --- reads -------------------------------------------------------------

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        today = _day(now)
        base = date(*time.localtime(now)[:3])
        days = [
            {
                "date": (base - timedelta(days=offset)).isoformat(),
                "seconds": int(self._data["days"].get((base - timedelta(days=offset)).isoformat(), 0.0)),
            }
            for offset in range(DAYS_SHOWN - 1, -1, -1)
        ]
        total = float(self._data["print_seconds"])
        return {
            "print_seconds": int(total),
            "print_hours": round(total / 3600.0, 1),
            "today_seconds": int(self._data["days"].get(today, 0.0)),
            "jobs": {
                "started": int(self._data["jobs_started"]),
                "completed": int(self._data["jobs_completed"]),
                "failed": int(self._data["jobs_failed"]),
                "stopped": int(self._data["jobs_stopped"]),
            },
            "days": days,
            "last_job": self._data["last_job"],
            "maintenance": {
                "interval_hours": round(self.maintenance_hours, 1),
                "hours_since": self.hours_since_service(),
                "due": self._is_due(),
                "last_reset_ts": self._data["maintenance"]["last_reset_ts"],
            },
        }

    def _log(self, message: str, *, level: str = "info", kind: str = "usage") -> None:
        if self._on_log is None:
            return
        with contextlib.suppress(Exception):
            self._on_log(message, level=level, kind=kind)
