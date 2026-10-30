# Elegoo Connector

A local-first dashboard for the **Elegoo Centauri Carbon (CC1)**, reachable from anywhere
through a Raspberry Pi Zero 2 W — without exposing the printer itself.

The printer's own web UI has **no authentication**, accepts roughly **five WebSocket
clients**, and drops idle sockets after about a minute. So this project puts one small
daemon between the printer and the world:

![Architecture: a phone or laptop reaches Tailscale or Cloudflare Tunnel + Access over HTTPS, SSE and MJPEG; that is the only internet-facing thing. Behind it, on a Raspberry Pi Zero 2 W, elegoo_bridge holds one WebSocket and one MJPEG reader to the Elegoo Centauri Carbon and fans REST, SSE and camera out to every browser.](docs/diagrams/architecture.png)

Status: works against a simulated printer (30 tests); the protocol layer is the field-proven
one from [pycentauri](https://github.com/bjan/pycentauri). Treat the first run against real
hardware as a commissioning step — see `docs/PI_SETUP.md` §5 and the probe checklist in
`PLAN.md` §5.

## What you get

- **Now** — state, job, progress, layer, elapsed/remaining/ETA, nozzle/bed/chamber actual vs
  target, fans, light, position, and a camera that is snapshot-first (one tap to go live).
- **Control** — pause/resume/stop, the four firmware-accepted speed modes, fan sliders,
  heater targets with server-side bounds, chamber light.
- **Files** — list, delete (refuses the file that is printing), resumable-feeling chunked
  upload with real progress, start a job straight from an upload.
- **History** — completed/stopped jobs from the printer's own task history.
- **About** — firmware, `MainboardID`, reconnect count, last error, and the event log.
- **Read-only by default.** `EC_CONTROL` is off until you turn it on; when it is off the
  mutating routes are not registered at all.

## Quick start (no printer needed)

```bash
cd bridge
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt

cd ..
python scripts/dev_fake.py                          # http://127.0.0.1:8800
```

That serves the real `web/` directory through the real bridge against a simulated Centauri
Carbon — full UI, fake printer, auth disabled.

## Quick start (real printer)

```bash
export EC_PRINTER_HOST=192.168.178.129
export EC_BIND=0.0.0.0          # loopback-only otherwise
export EC_AUTH_PASSWORD=...     # without this there is no login at all
export EC_CONTROL=1             # omit for read-only
python -m elegoo_bridge         # from the bridge/ directory
```

Then open `http://<pi-ip>:8787`. The full step-by-step — flashing the Pi, systemd, Tailscale
or Cloudflare, hardening, acceptance tests, troubleshooting — is in
[`docs/PI_SETUP.md`](docs/PI_SETUP.md).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `EC_PRINTER_HOST` | *(required)* | Printer IP. Reserve it in DHCP. |
| `EC_MAINBOARD_ID` | auto | Skips UDP discovery. Discovered once and cached in the state dir. |
| `EC_BIND` | `127.0.0.1` | Set `0.0.0.0` to reach it from the LAN. |
| `EC_PORT` | `8787` | Bridge port. |
| `EC_CONTROL` | `0` | `1` registers the mutating routes. |
| `EC_AUTH_PASSWORD` | — | Enables the login page and session cookie. |
| `EC_AUTH_USER` | `operator` | Label only. |
| `EC_SESSION_TTL_S` | 30 days | Cookie lifetime. |
| `EC_POLL_MS` | `5000` | Fallback `Cmd 0` polling period. |
| `EC_PUSH_MS` | `2000` | Requested status-push period (`Cmd 512`). |
| `EC_STATE_DIR` | `~/.local/state/elegoo-bridge` | Identity cache + upload staging. |
| `EC_UPLOAD_MAX_MB` | `2048` | Per-file upload cap. |
| `EC_CAMERA_PORT` | `3031` | MJPEG port to try **first**. If nothing answers there the bridge probes 8080 and remembers whichever works, so a wrong guess costs a second, not the camera. |
| `EC_CAMERA_PATH` | `/video` | MJPEG path on that port. |
| `EC_LOG_LEVEL` | `INFO` | |

## Tests

```bash
cd bridge
.venv/bin/python -m pytest -q      # 30 passed
```

`bridge/tests/fake_printer.py` implements enough of SDCP (envelope, `RequestID`
correlation, status/attribute pushes, `Cmd 0/1/128-131/258/259/320/321/403/512`), the
multipart upload endpoint and an MJPEG stream to exercise the bridge end to end. Nothing in
the suite touches real hardware.

## Documentation

| File | What it is |
|---|---|
| [`PLAN.md`](PLAN.md) | Research + architecture plan: ports, protocol, hosting options, feature-parity matrix, probe checklist |
| [`docs/PI_SETUP.md`](docs/PI_SETUP.md) | Step-by-step Pi installation, remote access, hardening, acceptance tests |
| [`docs/DESIGN_PHILOSOPHY.md`](docs/DESIGN_PHILOSOPHY.md) | The 15 named principles the UI and bridge are built on, with sources |
| [`docs/API.md`](docs/API.md) | The bridge's HTTP + SSE contract (the snapshot object, every route) |

## Deliberately out of scope

No calibration/levelling UI, no firmware update, no G-code preview, no AI failure detection,
and **no emergency stop** — the printer's firmware owns its interlocks and this dashboard is
not a safety device. Reasons are in `docs/DESIGN_PHILOSOPHY.md`.

## Repo map

```
bridge/       the daemon (FastAPI) and its tests
  cc1client/  vendored CC1 protocol core — see NOTICE
  elegoo_bridge/
    config.py   env-driven settings
    state.py    normalisation + the canonical snapshot
    printer.py  single-owner connection, reconnect, polling fallback
    server.py   REST, SSE, camera, uploads, auth wiring
    auth.py     session cookie, login limiter, same-origin check
web/          no-build SPA (vanilla ES modules, no CDN, PWA shell)
scripts/      dev_fake.py — the simulated printer harness
docs/         setup, design, API
  diagrams/   Mermaid sources + the rendered PNGs used in this README
```

## Attribution

`bridge/cc1client/` is vendored from [pycentauri](https://github.com/bjan/pycentauri)
(Apache-2.0) — the CC1 protocol, wire models, discovery, chunked upload, MJPEG fan-out and
WebSocket client. It was renamed and trimmed; changes are listed in [`NOTICE`](NOTICE).
The architecture was informed by `runnane/elegoo-web` and `J-Lindvig/Centauri-Carbon-Manager`
(studied, not copied). No license has been chosen yet for the original code in this repo.
