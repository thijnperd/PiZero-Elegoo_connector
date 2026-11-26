"""Usage counters and the maintenance reminder: small, persistent, honest."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from elegoo_bridge.usage import UsageStore

#: A fixed instant, so the daily buckets do not depend on when the suite runs.
NOW = 1_761_820_000.0


def event(kind: str, *, seconds: int | None = 3600, filename: str = "cube.gcode") -> dict[str, Any]:
    return {
        "event": kind,
        "state": kind,
        "ts": NOW,
        "job": {"filename": filename, "elapsed_s": seconds, "task_id": "task-abc"},
        "error": {"code": 7, "message": "boom"} if kind == "failed" else None,
    }


def test_a_finished_job_adds_print_time_and_a_day_bucket(tmp_path: Path) -> None:
    usage = UsageStore(tmp_path / "usage.json")
    usage.handle(event("finished"), now=NOW)

    snapshot = usage.snapshot(now=NOW)
    assert snapshot["print_seconds"] == 3600
    assert snapshot["print_hours"] == 1.0
    assert snapshot["today_seconds"] == 3600
    assert snapshot["jobs"] == {"started": 0, "completed": 1, "failed": 0, "stopped": 0}
    assert snapshot["last_job"]["filename"] == "cube.gcode"
    assert snapshot["last_job"]["state"] == "finished"
    assert len(snapshot["days"]) == 7
    assert snapshot["days"][-1]["seconds"] == 3600


def test_counters_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    UsageStore(path).handle(event("finished"), now=NOW)
    UsageStore(path).handle(event("failed", seconds=600), now=NOW)

    snapshot = UsageStore(path).snapshot(now=NOW)
    assert snapshot["print_seconds"] == 4200
    assert snapshot["jobs"]["completed"] == 1
    assert snapshot["jobs"]["failed"] == 1


def test_a_started_job_counts_but_adds_no_time(tmp_path: Path) -> None:
    usage = UsageStore(tmp_path / "usage.json")
    usage.handle(event("started", seconds=None), now=NOW)
    snapshot = usage.snapshot(now=NOW)
    assert snapshot["jobs"]["started"] == 1
    assert snapshot["print_seconds"] == 0


def test_unknown_events_are_ignored_and_write_nothing(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    usage = UsageStore(path)
    usage.handle(event("paused"), now=NOW)
    assert usage.snapshot(now=NOW)["print_seconds"] == 0
    assert not path.exists()


def test_maintenance_becomes_due_and_can_be_reset(tmp_path: Path) -> None:
    logs: list[str] = []
    usage = UsageStore(
        tmp_path / "usage.json",
        maintenance_hours=1.0,
        on_log=lambda message, **_: logs.append(message),
    )
    assert usage.snapshot(now=NOW)["maintenance"]["due"] is False

    usage.handle(event("finished", seconds=3600), now=NOW)
    maintenance = usage.snapshot(now=NOW)["maintenance"]
    assert maintenance["due"] is True
    assert maintenance["hours_since"] == 1.0
    assert maintenance["interval_hours"] == 1.0
    assert logs and "Maintenance due" in logs[0]

    reset = usage.reset_maintenance(now=NOW)
    assert reset["due"] is False
    assert reset["hours_since"] == 0.0
    assert reset["last_reset_ts"] == NOW
    assert "Maintenance counter reset" in logs[-1]


def test_the_reminder_fires_once_per_interval(tmp_path: Path) -> None:
    logs: list[str] = []
    usage = UsageStore(
        tmp_path / "usage.json",
        maintenance_hours=1.0,
        on_log=lambda message, **_: logs.append(message),
    )
    usage.handle(event("finished", seconds=3600), now=NOW)
    usage.handle(event("finished", seconds=3600), now=NOW)
    assert sum("Maintenance due" in line for line in logs) == 1
    # A restart must not re-announce a reminder that is already due, because the
    # operator has been told and the counter is still waiting for a reset.
    fresh = UsageStore(tmp_path / "usage.json", maintenance_hours=1.0, on_log=lambda m, **_: logs.append(m))
    fresh.handle(event("finished", seconds=60), now=NOW)
    assert sum("Maintenance due" in line for line in logs) == 1


def test_no_interval_means_no_reminder(tmp_path: Path) -> None:
    usage = UsageStore(tmp_path / "usage.json", maintenance_hours=0)
    usage.handle(event("finished", seconds=10_000), now=NOW)
    assert usage.snapshot(now=NOW)["maintenance"]["due"] is False


def test_a_corrupt_file_starts_over_instead_of_crashing(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    path.write_text("{not json", encoding="utf-8")
    usage = UsageStore(path)
    assert usage.snapshot(now=NOW)["print_seconds"] == 0

    path.write_text('{"version": 99, "print_seconds": 5}', encoding="utf-8")
    assert UsageStore(path).snapshot(now=NOW)["print_seconds"] == 0


def test_old_day_buckets_are_pruned(tmp_path: Path) -> None:
    usage = UsageStore(tmp_path / "usage.json")
    usage.handle(event("finished"), now=NOW)
    # Forty days later the old bucket is gone from the file, but never from the total.
    usage.handle(event("finished"), now=NOW + 40 * 86400)
    snapshot = usage.snapshot(now=NOW + 40 * 86400)
    assert snapshot["print_seconds"] == 7200
    assert snapshot["today_seconds"] == 3600
