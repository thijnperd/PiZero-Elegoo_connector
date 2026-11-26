# HTTP + SSE API contract

The bridge is the only thing that talks to the printer. Every browser talks to the
bridge. This document is the contract between the two halves of the project; if you
change a field name here, change `bridge/elegoo_bridge/state.py` in the same commit.

Base URL: `http://<pi>:8787` (behind Tailscale or a Cloudflare tunnel in production).

All responses are JSON unless noted. All routes except `POST /api/login`,
`GET /login` and `GET /healthz` require the session cookie when
`EC_AUTH_PASSWORD` is configured.

---

## 1. The snapshot object

Everything the UI renders comes from one object. It is sent whole — never as a delta —
so a reconnecting client can replace its state atomically.

```json
{
  "schema": 1,
  "ts": 1761820000.123,
  "connection": {
    "state": "online",
    "since_ts": 1761819000.0,
    "printer_host": "192.168.178.129",
    "firmware": "V1.1.46",
    "mainboard_id": "48551d180103147000001c0000000000",
    "reconnects": 2,
    "last_frame_age_s": 1.2,
    "last_error": null
  },
  "capabilities": {
    "control": true,
    "camera": true,
    "files": true,
    "upload": true,
    "history": true
  },
  "printer": {
    "state": "printing",
    "state_code": 13,
    "state_label": "Printing",
    "attention": false,
    "error": null,
    "stale": false,
    "stale_for_s": 0,
    "job": {
      "filename": "cube.gcode",
      "progress": 42,
      "layer": 120,
      "total_layers": 300,
      "elapsed_s": 1830,
      "remaining_s": 2500,
      "eta_ts": 1761822500.0,
      "task_id": "0f8e...",
      "speed_pct": 100
    },
    "temps": {
      "nozzle":  { "actual": 214.9, "target": 215.0 },
      "bed":     { "actual": 59.8,  "target": 60.0  },
      "chamber": { "actual": null,  "target": null  }
    },
    "fans": { "model": 100, "auxiliary": 0, "chamber": 0 },
    "light": true,
    "position": [10.0, 20.0, 0.4]
  },
  "usage": {
    "print_seconds": 41220,
    "print_hours": 11.5,
    "today_seconds": 1800,
    "jobs": { "started": 9, "completed": 7, "failed": 1, "stopped": 1 },
    "days": [{ "date": "2026-10-24", "seconds": 3600 }],
    "last_job": { "filename": "cube.gcode", "state": "finished", "ended_ts": 1761819990.0, "seconds": 2480 },
    "maintenance": { "interval_hours": 200.0, "hours_since": 11.5, "due": false, "last_reset_ts": 1761800000.0 }
  },
  "notifications": {
    "enabled": true,
    "format": "ntfy",
    "events": ["failed", "finished"],
    "sent": 2,
    "failed": 0,
    "dropped": 0,
    "queued": 0,
    "last_sent_ts": 1761819992.0,
    "last_error": null
  },
  "log": [
    { "ts": 1761819990.0, "level": "info", "kind": "job_started", "message": "Started cube.gcode" }
  ]
}
```

Rules the server guarantees:

- `connection.state` is one of `connecting`, `online`, `offline`.
- `printer` is `null` until the first status frame arrives.
- `job` is `null` when nothing is loaded/printing.
- `printer.stale` becomes `true` when no status frame has arrived for > 10 s; the UI
  must visibly de-emphasise values in that case rather than present them as current.
- `printer.state` slugs: `idle`, `homing`, `leveling`, `preheating`, `printing`,
  `pausing`, `paused`, `resuming`, `stopping`, `stopped`, `completed`, `error`,
  `checking`, `calibrating`, `unknown`.
- `printer.attention` is `true` for `error`, `paused`, and firmware states that need a
  human; it drives the single accent colour in the UI.
- `log` is newest-first, capped at 50 entries.
- `usage` and `notifications` are additive and always present. `usage.days` holds seven
  entries, oldest first; `usage.jobs.started` counts jobs begun, not files uploaded.
- `notifications.enabled` is false when `EC_NOTIFY_URL` is unset. The URL, token and chat
  id are never part of any response.
- `notifications.events` lists the transitions that will be pushed. A transition is
  announced once per state change, so a repeated status frame produces nothing here.

### Temperature fields

`actual` and `target` are numbers in °C or `null`. `chamber` is `null` on firmware that
does not report a box temperature. Never infer a target from an actual.

---

## 2. Routes

### Auth

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/login` | — | HTML login page (only when auth is enabled) |
| `POST` | `/api/login` | `{"password": "..."}` | `204` + `Set-Cookie: ec_session=...` |
| `POST` | `/api/logout` | — | `204` |
| `GET` | `/healthz` | — | `{"ok": true, "auth": "enabled"\|"disabled", "control": true, "uptime_s": 120, "printer": "printing"\|"idle"\|null, "connection": "online"}` |

Failed login: `401 {"detail": "invalid password"}`. Rate limited to 5 attempts / 5 min
per client address.

### State

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/state` | the snapshot |
| `GET` | `/api/events` | SSE stream; every event is `data: <snapshot>`; comment heartbeat every 15 s |
| `GET` | `/api/diagnostics` | `{"snapshot": {...}, "counters": {...}, "config": {...}}` (secrets redacted) |

The SSE stream is the primary update channel. It sends one snapshot immediately on
connect, then on every change, throttled to at most 2 per second.

### Usage and maintenance

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/usage` | the `usage` object on its own, if a poller does not want the whole snapshot |
| `POST` | `/api/maintenance/reset` | control + same-origin only. Sets the wear counter back to zero and returns `{"maintenance": {...}}`. It changes nothing on the printer. |

The counters live in `usage.json` in the state directory and are written on a transition,
not on a timer. Deleting that file resets history, which is the intended way to do it.

### Metrics

| Method | Path | Notes |
|---|---|---|
| `GET` | `/metrics` | Prometheus text exposition (`version=0.0.4`) |

Requires the session cookie, or — because a scraper has no cookie jar — the bearer token
from `EC_METRICS_TOKEN` (`Authorization: Bearer …`, or `?token=…` for a scraper that cannot
set headers). Values come from the same snapshot the UI renders, and a value the printer
did not report is **absent** rather than zero. Series: `ec_bridge_up`,
`ec_bridge_uptime_seconds`, `ec_bridge_frames_total`, `ec_bridge_commands_total`,
`ec_bridge_reconnects_total`, `ec_printer_connected`, `ec_printer_stale`,
`ec_printer_state{state}`, `ec_job_active`, `ec_print_progress_percent`, `ec_print_layer`,
`ec_print_layers_total`, `ec_print_remaining_seconds`,
`ec_temperature_celsius{heater,kind}`, `ec_fan_percent{fan}`, `ec_camera_viewers`,
`ec_usage_print_seconds_total`, `ec_usage_jobs_total{result}`,
`ec_maintenance_due`, `ec_maintenance_hours_since_service`, `ec_notifications_sent_total`,
`ec_notifications_failed_total`, `ec_notifications_dropped_total`.

### Job control — all gated on `capabilities.control`

Every accepted command returns:

```json
{ "accepted": true, "request_id": "a1b2", "status": "sent" }
```

and the outcome arrives asynchronously in the snapshot (`log` + `printer.state`). A
rejected command returns `409` with `{"detail": "..."}`.

| Method | Path | Body |
|---|---|---|
| `POST` | `/api/print/start` | `{"filename": "cube.gcode", "storage": "local", "auto_leveling": true, "timelapse": false}` |
| `POST` | `/api/print/pause` | — |
| `POST` | `/api/print/resume` | — |
| `POST` | `/api/print/stop` | — |
| `POST` | `/api/print/speed` | `{"mode": "silent"\|"balanced"\|"sport"\|"ludicrous"}` |
| `POST` | `/api/print/fans` | `{"model": 0-100, "auxiliary": 0-100, "chamber": 0-100}` (any subset) |
| `POST` | `/api/print/temps` | `{"nozzle": 0-300, "bed": 0-110, "chamber": 0-60}` (any subset, °C, 0 = off) |
| `POST` | `/api/print/light` | `{"on": true}` |

Server-side validation is authoritative: fan `0..100`, nozzle `0..300`, bed `0..110`,
chamber `0..60`, speed only the four firmware-accepted modes. Out-of-range → `422`.

### Files

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/files?storage=local` | `{"files": [{"filename","size","layers","created_ts"}], "total": 3}` |
| `POST` | `/api/files/delete` | `{"filenames": ["a.gcode"], "storage": "local"}` → `{"deleted": [...]}`; refuses the printing file (`409`) |
| `GET` | `/api/history` | `{"jobs": [{"name","status","started_ts","ended_ts"}]}` newest-first |

### Upload

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/uploads` | `multipart/form-data` with `file` (+ optional `start=true`). Streams to disk, then uploads to the printer in 1 MiB chunks. Returns `{"upload_id": "...", "filename": "..."}`; progress appears in the snapshot `log` and at `GET /api/uploads/{id}`. |
| `GET` | `/api/uploads` | list of in-flight/recent transfers with `{"id","filename","bytes_sent","total_bytes","state","error"}` |
| `DELETE` | `/api/uploads/{id}` | cancel a queued transfer (the printer-side chunk loop stops between chunks) |

Limits: 2 GiB per file, one transfer at a time per client, and the bridge refuses
uploads when `capabilities.upload` is false.

### Camera

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/camera/stream` | `multipart/x-mixed-replace` MJPEG, fanned out from **one** upstream connection. Cache-busting query allowed. |
| `GET` | `/api/camera/snapshot` | single `image/jpeg`, `Cache-Control: no-store`. Frames are shared for `EC_SNAPSHOT_CACHE_MS` (default 800 ms), so several viewers cost one camera read and concurrent requests wait for the read already in flight. |
| `GET` | `/api/camera` | `{"available": true, "viewers": 1, "frame_age_s": null, "url": "http://<printer>:3031/video"}` |

The camera's port is the one part of the protocol that differs between firmware builds, so
the bridge probes the configured port first and then 8080, caching whichever answers with
image bytes. `url` is the address it settled on — the field to read when the camera panel is
empty. A failed read drops the cache and the next request probes again.

The UI must default to snapshots and only open the stream on an explicit action
(data usage), and must close it when the tab is hidden.

---

## 3. Errors

| Status | Meaning |
|---|---|
| `401` | not authenticated / wrong password |
| `403` | control disabled (`EC_CONTROL=0`) or origin check failed |
| `409` | command rejected by bridge policy (wrong state, busy, protected file) |
| `422` | validation error |
| `502` | printer unreachable while handling a command |

Error bodies are always `{"detail": "human readable message"}`.

---

## 4. Security model

- The printer's own interface has **no authentication**. The bridge never proxies it.
- The bridge only accepts mutations from same-origin requests (`Origin`/`Referer`
  check) in addition to the session cookie, so a stray link cannot drive the printer.
- `EC_CONTROL=0` (the default) registers no mutating routes at all.
- Remote access is a private network (Tailscale) or an authenticated tunnel
  (Cloudflare Access). Never port-forward the bridge or the printer.
- `/metrics` is the one route with a second way in, because Prometheus cannot complete a
  login form. Set `EC_METRICS_TOKEN` to a long random value and scrape over the tunnel; with
  the variable unset, the endpoint only accepts a browser session like every other route.
