# Elegoo Connector — Plan

**Goal:** monitor (and control) an Elegoo Centauri Carbon (CC1) from anywhere, using a
Raspberry Pi Zero 2 W as the always-on local gateway, plus a custom web page that keeps
every function the stock web UI offers.

**Status:** research + design plan only. No application code written yet.

**Target project location:** this repo (`Elegoo_connector/`).

---

## 0. TL;DR — the recommended setup

Two separate but stackable pieces:

| Piece | What it does | Where it runs |
|---|---|---|
| **A. `elegoo-bridge`** (small daemon) | Owns the *single* WebSocket + the *single* camera connection to the printer, normalises state, exposes a clean HTTP + WebSocket/SSE API, and serves the custom UI | Raspberry Pi Zero 2 W, on the printer's LAN |
| **B. Remote access** | Lets your phone reach the Pi from outside the house **without any inbound port** | **Tailscale** (primary, private VPN) or **Cloudflare Tunnel + Access** (if you don't want a VPN app on your phone) |

Why a daemon and not "just expose the printer's port 80":

1. The printer's interface has **no authentication at all**. Publishing it is publishing a
   hot machine with a 250 °C nozzle to the internet.
2. The printer allows only **~5 concurrent WebSocket clients** and closes idle sockets after
   **~60 s**. Every browser tab opening its own connection will break your setup. One owner,
   many viewers.
3. The stock page at `/network-device-manager/network/control` is an Angular **UI route**,
   not an API. Its WebSocket target is hard-coded to a LAN IP, so serving that page over
   HTTPS from a public hostname produces a mixed-content, unroutable mess. You need a
   server-side proxy anyway — which is the daemon.

**Not** recommended for v1: port forwarding on the router, public Docker dashboards,
`ngrok` (1 GB/month), or exposing the camera on a public URL.

---

## 1. What the printer actually is (verified facts)

### 1.1 Ports and services

| Port | Service | Notes |
|---|---|---|
| `80/tcp` | Stock Angular web UI + G-code upload endpoint | This is what you opened in the browser |
| `3000/udp` | Discovery | Send ASCII `M99999` broadcast; printer answers with JSON containing IP, firmware, `MainboardID` |
| `3030/tcp` | **SDCP v3 over WebSocket** at `/websocket` | The real control channel |
| `3031/tcp` | MJPEG camera at `/video` | `multipart/x-mixed-replace`, JPEG frames |

> Source conflicts exist on upload port (80 vs 3030) and camera port (3031 vs 8080 on other
> models). Verify on **your** firmware — see §7.

### 1.2 The control protocol (SDCP v3, JSON over WebSocket)

```
ws://192.168.178.129:3030/websocket
```

Request envelope (all requests):

```json
{
  "Id": "<MainboardID>",
  "Topic": "sdcp/request/<MainboardID>",
  "Data": {
    "Cmd": 0,
    "Data": {},
    "RequestID": "<fresh UUID per request>",
    "MainboardID": "<MainboardID>",
    "TimeStamp": 1752333800479,
    "From": 1
  }
}
```

Responses come back on `sdcp/response/<MainboardID>` and are matched by **`RequestID`**.
Status/attributes arrive on `sdcp/status/<MainboardID>` and `sdcp/attributes/<MainboardID>`.

### 1.3 Command map (CC1)

| `Cmd` | Function | Payload notes |
|---:|---|---|
| `0` | Request status | `{}` — one-shot poll; the reliable primitive |
| `1` | Request attributes | `{}` — identity, firmware, capabilities |
| `128` | Start print | `{"Filename": "/local/x.gcode", "StartLayer": 0}` + slicer options (`Calibration_switch`, `PrintPlatformType`, `Tlp_Switch`) |
| `129` | Pause | `{}` |
| `130` | Stop / cancel job | `{}` |
| `131` | Resume | `{}` |
| `255` | Cancel an in-flight file transfer | transfer UUID + filename |
| `258` | List files | `{"Url": "/local"}` (also `/udisk`) |
| `259` | Delete files | `{"FileList": ["/local/x.gcode"]}` |
| `320` | Print history (task list) | `{}` |
| `321` | History details | `{"Id": ["<task-uuid>"]}` — thumbnails, timelapse refs |
| `386` | Enable camera / get URL | `{"Enable": 1}` — **conflicting reports; verify** |
| `387` | Timelapse capture on/off | `{"Enable": 0|1}` |
| `403` | Change print parameters | **Overloaded**: `PrintSpeedPct`, `TargetFanSpeed`, temperature targets, `LightStatus` |
| `512` | Subscribe to status pushes | `{"TimePeriod": <ms>}` — works on some firmware only |

### 1.4 Upload (not a WebSocket command)

```
POST http://192.168.178.129/uploadFile/upload
multipart/form-data:
  Check       = 1
  S-File-MD5  = <whole-file MD5 hex>
  Offset      = <byte offset of this chunk>
  Uuid        = <transfer UUID, same for all chunks>
  TotalSize   = <total bytes>
  File        = <chunk, 1 MiB> (filename included)
```

Success: `{"code":"000000","data":{},"success":true}`.
Then start it with `Cmd 128`.

### 1.5 Hard constraints and risks

- **No authentication, no pairing** on the LAN interface. Anyone on your WiFi can control the printer.
- **Max ~5 concurrent WebSocket clients** (6th → HTTP 500 "too many client").
- **Idle WebSocket closes after ~60 s**; a literal text frame `ping` keeps it alive.
- **Camera slots are limited and leak** on disconnect → multiplex one upstream stream.
- **Unknown/malformed commands can crash the printer's daemon** and can interrupt an active
  print. Only use the allowlisted commands above; never fuzz, especially while printing.
- **No Klipper, no Fluidd/Mainsail, no config access.** Leveling, full calibration, firmware
  update and machine settings stay on the touchscreen. Don't promise them in the UI.
- Protocol details come from community reverse engineering and **vary by firmware**. Record
  your firmware version in the project and re-test writes after every update.

### 1.6 Prior art worth reading before writing any code

| Repo | License | Why it matters |
|---|---|---|
| [bjan/pycentauri](https://github.com/bjan/pycentauri) | Apache-2.0 | Most complete CC1 client + REST/SSE server; its `docs/PROTOCOL.md` is the best field reference. **Reuse this before writing your own protocol layer.** |
| [elegooofficial/CentauriCarbon](https://github.com/elegooofficial/CentauriCarbon) | — | Official issue tracker; known WS-client-limit bug (#30) |
| [elegooofficial/elegoo-link](https://github.com/elegooofficial/elegoo-link) | Apache-2.0 | Official C++ SDK; cross-check command mappings |
| [danielcherubini/elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant) | MIT | Working SDCP client; entity model to copy |
| [J-Lindvig/Centauri-Carbon-Manager](https://github.com/J-Lindvig/Centauri-Carbon-Manager) | **none found** | Single-file browser UI for files/history/camera; good UX reference, **do not copy code** without a license |
| [runnane/elegoo-web](https://github.com/runnane/elegoo-web) | MIT | CC2-only, but the proxy + state-store + camera fan-out architecture is exactly right |
| [blakejrobinson/sdcp](https://github.com/blakejrobinson/sdcp) | ISC | Node SDCP client if you prefer JS/TS |
| [cbd-tech SDCP 3.0 spec](https://github.com/cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0) | — | Generic protocol baseline (resin-flavoured; don't trust it blindly for CC1) |

---

## 2. Part A — Getting it online with a Raspberry Pi Zero 2 W

### 2.1 Honest assessment of the hardware

Quad-core 1 GHz Cortex-A53, **512 MB RAM**, 2.4 GHz-only WiFi (no 5 GHz), no Ethernet, microSD.

- Plenty for `tailscaled`/`cloudflared` + a Python daemon + Caddy. This is a *bridge*, not a
  media server.
- **Do not** run Docker, Authelia/Authentik, or image analysis on it. 512 MB is the real limit.
- Use **Raspberry Pi OS Lite (64-bit)** — recommended, both widths work, but 64-bit gets you
  official `cloudflared-linux-arm64` and `wg-easy` compatibility if you ever want them.
- 2.4 GHz-only is the weak point: decent signal, **DHCP reservation**, and consider a small
  USB OTG Ethernet adapter if you see drops. UDP discovery (`M99999`) is the first thing that
  breaks on flaky WiFi.
- microSD + 24/7 + power cuts = corruption. High-endurance card, log rotation / journald
  `Storage=volatile`, and ideally a UPS or at least a clean shutdown habit.

### 2.2 Critical architectural point

The daemon **must** run on the LAN (it talks to the printer's raw WebSocket and MJPEG). "Online"
therefore means: *dashboard lives on the Pi; the internet only relays to it.* Do not try to
host the bridge in the cloud — the printer is unreachable from there, and you'd have to punch a
reverse tunnel out of your house anyway, which is more work and more attack surface.

### 2.3 Options compared

| Option | Phone experience | Cost | WebSocket | Verdict |
|---|---|---|---|---|
| **Tailscale + subnet router** | Install Tailscale app, then open the printer's LAN IP or the Pi's dashboard | Free (personal: 6 users, unlimited devices) | Native — it's a VPN, WS just works | **Recommended.** Zero inbound ports, zero public surface |
| **Cloudflare Tunnel + Access** | Normal browser, `https://printer.yourdomain.com`, email OTP | Needs a domain on Cloudflare; Access free tier ≈ 50 users | Supported (keepalives required) | **Recommended if no VPN app on the phone** |
| Tailscale **Funnel** | Public browser, no login | Free | Untested guarantee | **No** — it's public, beta, no auth wall |
| WireGuard / wg-easy | VPN app | Free | Works | More maintenance; `wg-easy` needs arm64; needs router port forward |
| **ngrok** free | Browser + interstitial | 1 GB/mo, 20k req/mo | Explicitly supported | Camera kills the quota in ~20 min |
| Pinggy / zrok / localtunnel / bore | Browser | quota / rotating URL | Not verified for long-lived WS | Testing only |
| Caddy + port forward + Let's Encrypt + basic auth | Browser | Free + domain | Works (needs `Upgrade` headers) | Works, but exposes your home IP and needs open 443 |

### 2.4 Path A1 — Tailscale (do this first)

Why: no public URL exists, so the printer's missing auth doesn't matter to the internet. It also
solves the "stock page hard-codes `ws://192.168.x.x`" problem, because the phone really is on
your LAN.

Steps:

1. Flash Raspberry Pi OS Lite 64-bit, enable SSH, join WiFi, `sudo apt update && sudo apt full-upgrade`.
2. DHCP reservation for the Pi **and** for the printer (both must be stable).
3. ```sh
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
4. Subnet router, least privilege (printer only):
   ```sh
   echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-tailscale.conf
   sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
   sudo tailscale set --advertise-routes=192.168.178.129/32
   ```
5. Approve the route in the Tailscale admin console; add an ACL/grant limited to the printer's
   ports (80, 3030, 3031) from your phone's device.
6. Install Tailscale on the phone and sign into the same tailnet.
7. Away from home: enable Tailscale → open the dashboard at the Pi's tailnet name, or the
   printer UI directly at `http://192.168.178.129/`.

Optional but nice: `tailscale serve` on the Pi to give the dashboard a proper `https://` name
inside the tailnet (no public exposure).

Downside: one extra app on every device that needs access. For one printer + one phone, that's
a fine trade.

### 2.5 Path A2 — Cloudflare Tunnel + Access (no VPN app)

Prerequisites: a domain whose nameservers point at Cloudflare. Free plan is fine.

1. Zero Trust → Networks → Tunnels → create tunnel (`cloudflared`), copy the token.
2. **Create the Access application first** (self-hosted, hostname `printer.yourdomain.com`,
   policy = allow only your email, one-time PIN). If you publish before this exists, the app is public.
3. On the Pi:
   ```sh
   # 64-bit OS:
   curl -fL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o cloudflared.deb
   sudo dpkg -i cloudflared.deb
   # 32-bit OS: swap arm64 -> armhf.  (Zero 2 W is NOT armv6 — ignore old Zero W guides.)
   sudo cloudflared service install '<TUNNEL_TOKEN>'   # treat token as a secret
   ```
4. Tunnel public hostname → `http://localhost:8080` (the **daemon**), *not* the printer's IP
   and *not* port 80 of the printer.
5. Ingress lives in the daemon: it proxies `/` (UI), `/api/*`, `/ws` (browser socket),
   `/video` (camera MJPEG). The tunnel never learns about `3030`/`3031`.

Gotchas:
- WebSockets over Cloudflare are supported; **idle sockets get closed** without traffic — the
  daemon's 20–30 s heartbeat covers this.
- The famous "125 s" is Cloudflare's origin **HTTP response** timeout (524) — not a WS lifetime cap.
- Do **not** put a service token in a browser app, and do not use a subpath
  (`example.com/printer/`) — the printer UI uses root-absolute paths. Use a subdomain.
- Camera on mobile data is the real cost: MJPEG 720p ≈ 6 Mbps ≈ **2.7 GB/hour**. Add a
  low-bandwidth mode (snapshot every 2–5 s) and don't leave the camera open.

### 2.6 Router / network hygiene

- No port forwards for 80/3030/3031. Ever.
- Put the printer on an **IoT VLAN/SSID** if your router supports it; allow only the Pi to reach
  the printer's ports, and block printer → LAN/PC/NAS.
- Static DHCP for printer + Pi (the daemon stores `MainboardID`, not an IP, so re-discovery
  still works if the lease changes).

### 2.7 Turnkey alternative (decide before building anything)

If the only goal is "see my print from my phone" and you don't care about a custom UI:
`pycentauri` already ships an async client, a CLI, a REST/SSE server and a web dashboard with
camera, files, history and controls. Install it on the Pi, put Tailscale (or a Cloudflare tunnel)
in front of it, done in an evening. Use this as a **fallback** and as a **reference
implementation** — the plan below still applies if you want your own page.

---

## 3. Part B — Designing your own page (full feature parity)

### 3.1 Architecture

```
PHONE / BROWSER (anywhere)
   │  HTTPS  +  WSS  +  MJPEG-over-HTTP
   ▼
[ Tailscale  or  Cloudflare Tunnel + Access ]     ← the only internet-facing thing
   ▼
┌─────────────── Raspberry Pi Zero 2 W ───────────────┐
│  Caddy (optional: TLS in tailnet, static assets)     │
│                                                     │
│  elegoo-bridge  ─────────────────────────────────┐   │
│   • upstream: ONE ws://printer:3030/websocket    │   │
│   • upstream: ONE http://printer:3031/video      │   │
│   • state store (single source of truth)         │   │
│   • RequestID ↔ pending-request map              │   │
│   • command queue + rate limit + allowlist       │   │
│   • HTTP API (REST) for commands/uploads         │   │
│   • WS/SSE fan-out for telemetry                 │   │
│   • MJPEG fan-out for camera                     │   │
│   • upload chunker (1 MiB, MD5, resumable)       │   │
│   • auth (cookie/token) for LAN clients          │   │
└──────────────────────────────────────────────────┘   │
   ▼                                                   │
Elegoo Centauri Carbon  (HTTP :80 · SDCP :3030 · MJPEG :3031)
```

Rules that make or break this design:

1. **One connection owner.** Only the daemon talks to `:3030`. Never let a browser open it.
   (5-client limit + 60 s idle close.)
2. **One canonical state store.** Merge status + attributes, normalise, timestamp, and
   broadcast. On client reconnect, send a **full snapshot** first, then deltas.
3. **Correlate by RequestID.** Daemon generates UUIDs; browsers never see or choose them.
   Late/stale responses are dropped, not attributed to whatever the user clicked last.
4. **No optimistic lies.** Sliders show `pending` until ACK + the next status confirms; revert
   on timeout with the printer's `Ack` code visible.
5. **Reconnect** with exponential backoff + jitter, fresh status (`Cmd 0`) and attributes
   (`Cmd 1`) after every reconnect — the printer may have restarted.
6. **Heartbeat** a text `ping` every 20–30 s (well under the ~60 s cut-off).
7. **Polling is daemon-owned.** `Cmd 512` pushes when they work (2 s period), plus a `Cmd 0`
   fallback every 5 s when idle / 2 s while printing. One poll loop, never per-browser.
8. **Multiplex the camera.** Single upstream MJPEG reader, fan frames out; close upstream when
   the last viewer leaves and guard against leaked slots.
9. **Strict command allowlist.** Anything not in §1.3 is rejected by the bridge. This is a
   safety feature: a bad frame can crash the printer mid-print.
10. **Uploads from the daemon, not the browser.** The browser streams to the Pi (it may be on
    mobile data); the Pi chunks to the printer.

### 3.2 Suggested bridge API (design, not code)

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/state` | Full snapshot: status, attributes, capabilities, connection health |
| `GET` | `/api/events` (SSE) **or** `/ws` | Live deltas + connection state + log of printer notices |
| `POST` | `/api/print/start` `{filename, startLayer, options}` | → `Cmd 128` |
| `POST` | `/api/print/pause` \| `/resume` \| `/stop` | → `Cmd 129` / `131` / `130` |
| `PATCH` | `/api/settings` `{speedPct, fanPct, nozzleTarget, bedTarget, light}` | → `Cmd 403` variants |
| `GET` | `/api/files?path=/local` | → `Cmd 258` |
| `DELETE` | `/api/files` `{paths[]}` | → `Cmd 259` |
| `POST` | `/api/uploads` (multipart, chunked, resumable) | → `POST /uploadFile/upload` |
| `DELETE` | `/api/uploads/{uuid}` | → `Cmd 255` |
| `GET` | `/api/history` · `/api/history/{id}` | → `Cmd 320` / `321` |
| `GET` | `/api/camera/stream` · `/api/camera/snapshot` | MJPEG fan-out / single JPEG |
| `PATCH` | `/api/camera` `{timelapse: bool}` | → `Cmd 387` |
| `GET` | `/api/diagnostics` | Firmware, `MainboardID`, last ACK codes, reconnect count, WS client estimate |

Design notes: commands return `202 {requestId, status: "accepted"}` and the outcome arrives on
the event stream (`ack` / `error` / `state`), so the UI never blocks on the printer. Every
mutating route is rate-limited (e.g. 1 write / 300 ms) and idempotent where possible.

### 3.3 Feature parity matrix (stock UI → your page)

| Stock UI function | Your source | Coverage |
|---|---|---|
| Live status, progress, layer, filename, ETA | `Cmd 0` / `512` status topic | Full |
| Nozzle / bed temps, chamber, fans | status topic | Full (read) |
| Temperature & fan & speed & light set | `Cmd 403` | Full where firmware allows; verify per firmware |
| Start / pause / resume / stop | `128` / `129` / `131` / `130` | Full |
| File list (`/local`, USB) | `258` | Full |
| Delete file | `259` | Full |
| G-code upload | `POST /uploadFile/upload` | Full (1 MiB chunks + MD5) |
| Print history + thumbnails + timelapse links | `320` / `321` | Full |
| Camera | MJPEG `:3031/video` | Full (plus snapshot mode) |
| Timelapse capture toggle | `387` | Likely; verify |
| Firmware / network settings pages | stock UI | **Link out**, don't reimplement |
| Leveling / calibration / self-test | touchscreen only | **Out of scope** — document as such |
| Firmware update | touchscreen / stock UI | **Out of scope** (safety) |

The honest framing: the custom page can **match or beat** the stock page on monitoring, files,
history, camera and job control. It cannot replace touchscreen calibration, and it should not
pretend to.

### 3.4 UI design

Mobile-first, single page with tabs (it will be used from a phone):

- **Now printing** — big progress ring, layer `x/y`, ETA + elapsed, thumbnail, temps
  (nozzle/bed/chamber) as gauges, live camera inline (default: snapshot at 1 fps), Pause/Stop
  as the only primary buttons.
- **Control** — speed %, fan %, nozzle/bed target steppers, light toggle, jog/axis controls if
  firmware exposes them. Each control is a *pending → confirmed* pill.
- **Files** — list with thumbnails/size/date, upload (drag-drop, progress, cancel), delete with
  confirm, "print this" action.
- **History** — completed/stopped/failed jobs, duration, filament if reported, thumbnail + timelapse.
- **Status / About** — connection health, firmware, `MainboardID`, last error, "printer asleep?"
  banner when the WS is down.

Cross-cutting:

- Connection banner: `Live` / `Reconnecting…` / `Offline` — never show stale temps as if current
  (grey them out after ~10 s without an update).
- Destructive actions (Stop) require a confirm; Start requires showing the chosen file + options.
- Add to Home Screen (PWA manifest + icons) — that's how it stops feeling like a website and
  starts feeling like an app, with zero app-store work.
- Dark theme default, large touch targets, no hover-only affordances.
- Optional later: Web Push / ntfy notification on `state → finished` or `error`.

### 3.5 Stack recommendation

| Layer | Pick | Why |
|---|---|---|
| Bridge | **Python 3.11 + FastAPI** (or reuse `pycentauri` as a library) | Its CC1 protocol notes and clients already encode the firmware quirks; `websockets`/`httpx` handle WS + chunked upload cleanly |
| Frontend | **SvelteKit/Vite SPA** or plain **React + Vite**, built to static files | Served by the bridge; no SSR needed on a 512 MB Pi |
| Styling | Tailwind or plain CSS | Keep bundle small; it's loaded over mobile data sometimes |
| Transport to browser | WS if you want interactive commands + events, otherwise SSE + REST | SSE survives proxies better; WS is fine behind Tailscale |
| Process mgmt | `systemd` units (bridge + cloudflared/tailscaled) | No Docker on a Zero 2 W |
| Auth (LAN/edge) | Tailscale identity, or Cloudflare Access; plus a bridge-issued cookie for the UI | Never rely on the printer's nonexistent auth |

Alternative: Node/TS with `blakejrobinson/sdcp` if you'd rather have one language — but then
you re-derive the firmware quirks yourself.

### 3.6 Milestones

| # | Milestone | Deliverable | Notes |
|---|---|---|---|
| 0 | **Probe the printer** | §7 checklist filled in: ports, firmware, WS frame, upload, camera | No code, 30 min. Blocks everything else |
| 1 | Pi up | OS Lite, static DHCP, Tailscale subnet route working from cellular | Prove reachability before building |
| 2 | Protocol layer | Discover → connect → `Cmd 0/1` → normalised state printed to console | Reconnect + heartbeat + 5-client awareness |
| 3 | Read-only API + minimal UI | Status, temps, progress, camera snapshot | Already useful; ship this before controls |
| 4 | Job control | Start/pause/resume/stop with ACK handling and confirms | Test only on an **idle** printer first |
| 5 | Files + upload | List, delete, chunked MD5 upload, start-job-from-file | Test a small file; verify offsets on retry |
| 6 | History + timelapse | `320`/`321`, thumbnails, timelapse links, `387` toggle | |
| 7 | Remote hardening | Access policy / ACL, no public ports, rate limits, logs | Re-test from cellular |
| 8 | Polish | PWA, low-bandwidth camera mode, notifications, backup of config | |
| 9 | Optional | Home Assistant integration, 3rd-party print monitor (Obico-style) | |

### 3.7 Risks and mitigations

| Risk | Mitigation |
|---|---|
| Bad command crashes printer daemon / ruins a print | Allowlist only; never probe during a print; test each write on idle hardware first |
| Firmware update changes behaviour | Pin firmware version in docs; smoke-test after updates; capability matrix in the bridge |
| WS client limit (5) hit | Single owner + snapshot-on-reconnect; surface count in diagnostics |
| Camera slot leak | One upstream reader, refcount viewers, timeout + reconnect, snapshot mode |
| SD card corruption | High-endurance card, volatile journald, clean shutdown, config in git |
| Remote access misconfigured (public printer) | Access app created *before* tunnel; no port forwards; verify from cellular + a logged-out browser |
| Mobile data blowout from MJPEG | Snapshot mode default on cellular, explicit "live" button with a data warning |
| Bridge on Pi is a single point of failure | systemd `Restart=always`, health endpoint, Tailscale SSH for recovery |

---

## 4. Reference configuration (not application code)

Only the deployment plumbing, so the plan is actionable later.

**cloudflared ingress** (Zero Trust dashboard form): hostname `printer.example.com` →
service `http://localhost:8080` (bridge). No path, no extra ports.

**Caddyfile** (only if you want TLS inside the tailnet / LAN):

```
printer.lan {
    reverse_proxy localhost:8080
}
```

**systemd unit shape**

```ini
[Unit]
Description=Elegoo bridge
After=network-online.target
[Service]
ExecStart=/opt/elegoo-bridge/venv/bin/python -m bridge
Restart=always
RestartSec=5
User=elegoo
[Install]
WantedBy=multi-user.target
```

**Discovery probe**

```sh
echo -n M99999 | nc -u -b 255.255.255.255 3000
```

---

## 5. Verification checklist (run before milestone 1)

Run these on the Pi, output pasted into `docs/probe.md` in this repo:

1. `nmap -Pn -p 80,3000,3030,3031 192.168.178.129` → confirm which ports are actually open.
2. `curl -s http://192.168.178.129/ | head` → is the UI a SPA shell? Which JS bundles?
3. Discovery: `M99999` broadcast → capture `MainboardID` + firmware string.
4. WebSocket: connect to `:3030/websocket`, log every frame for 60 s *without sending anything*
   (attributes/status may be pushed on connect). Do this with the printer **idle**.
5. Send `Cmd 0` → confirm `RequestID` echo and status field names.
6. Send `Cmd 1` → save the full attribute payload (this defines your capability matrix).
7. `curl -sI http://192.168.178.129:3031/video` → confirm MJPEG content type.
8. Upload a tiny `.gcode` via `curl -F` and confirm chunk fields + `success:true`; then
   `Cmd 258` to see it listed. Delete with `Cmd 259`.
9. Open 6 simultaneous WS clients to confirm the 5-client limit on *your* firmware.
10. Idle-time test: leave one WS open with no traffic → record exact close time; then with
    `ping` heartbeats every 20 s → confirm it survives an hour.

Do 9–10 before designing anything around connection counts.

---

## 6. Decisions still open

1. **Tailscale vs Cloudflare Access** — Tailscale if a VPN app on your phone is acceptable
   (recommended); Cloudflare if you want a plain browser URL and already own a domain.
2. **Build vs adopt** — adopt `pycentauri` on the Pi for immediate value, or build the bridge
   with a custom UI for full control. (They're not exclusive: run pycentauri first, build later.)
3. **Camera by default** — live MJPEG, or 1 fps snapshots with an opt-in "live" button?
4. **Write access from the internet** — full control remotely, or read-only monitoring while
   away and control only at home? (Meaningfully changes the blast radius.)
5. **Where the Pi lives** — next to the router (better WiFi) vs next to the printer (better
   signal to the printer, worse to the AP). Test both before committing.

---

## 7. Sources

- Printer protocol: [pycentauri `docs/PROTOCOL.md`](https://github.com/bjan/pycentauri/blob/main/docs/PROTOCOL.md),
  [OpenCentauri API docs](https://docs.opencentauri.cc/software/api/),
  [sdcp-centauri-carbon guide](https://github.com/WalkerFrederick/sdcp-centauri-carbon),
  [SDCP v3.0 spec](https://github.com/cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0),
  [elegoo-link](https://github.com/elegooofficial/elegoo-link),
  [CC firmware WS-client issue](https://github.com/elegooofficial/CentauriCarbon/issues/30)
- Dashboards / prior art: [elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant),
  [Centauri-Carbon-Manager](https://github.com/J-Lindvig/Centauri-Carbon-Manager),
  [elegoo-web](https://github.com/runnane/elegoo-web),
  [OctoPrint push API](https://docs.octoprint.org/en/main/api/push.html),
  [Fluidd/Moonraker architecture](https://docs.fluidd.xyz/development/)
- Hardware / hosting: [Pi Zero 2 W specs](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/),
  [Cloudflare Tunnel docs](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/),
  [Cloudflare WebSockets](https://developers.cloudflare.com/network/websockets/),
  [Cloudflare One-time PIN](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/),
  [Tailscale subnet routers](https://tailscale.com/docs/features/subnet-routers),
  [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel),
  [ngrok free limits](https://ngrok.com/docs/pricing-limits/free-plan-limits),
  [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
- Bandwidth: [MJPEG vs H.264 estimates](https://reolink.com/blog/ip-camera-bandwidth-calculation/)
