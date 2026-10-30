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
| `GET` | `/healthz` | — | `{"ok": true, "auth": "enabled"\|"disabled"}` |

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
| `GET` | `/api/camera/snapshot` | single `image/jpeg`, `Cache-Control: no-store` |
| `GET` | `/api/camera` | `{"available": true, "viewers": 1, "frame_age_s": 0.3}` |

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
