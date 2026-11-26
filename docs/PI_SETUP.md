# Raspberry Pi Zero 2 W — installation plan

This is the complete, ordered procedure for turning a bare Pi Zero 2 W into the
always-on bridge for the Centauri Carbon. Work through it top to bottom; every section
ends with a check that says what success looks like before you continue.

Paths and placeholders used throughout:

| Thing | Value used here | Notes |
|---|---|---|
| Pi username | `pi` | Whatever you create in Imager — adjust every path if you pick another |
| Pi hostname | `elegoo-pi` | Set in Imager or `raspi-config` |
| Pi address | `192.168.178.50` | **Placeholder.** Your router will assign one; reserve it in step 3 |
| Printer address | `192.168.178.129` | **Placeholder.** Confirm on the printer's network screen |
| Project directory | `/home/pi/Elegoo_connector` | `git clone` output |
| Bridge port | `8787` | `EC_PORT` |

---

## 0. What you need

Hardware:

- Raspberry Pi Zero 2 W (the quad-core one; the original Zero W is too slow and is
  `armv6`, which many prebuilt binaries no longer support).
- microSD card, **8 GB minimum, 16 GB comfortable, high-endurance or "A1/A2" rated**.
  This device writes logs 24/7 and will be power-cycled by accident; a cheap unbranded
  card is the most common cause of a bridge that dies after a month with a corrupted
  filesystem.
- Official 5 V/2.5 A micro-USB power supply. Undervoltage causes random WiFi drops that
  look exactly like software bugs.
- A micro-USB **data** cable for the initial flash/SSH work (many cables are charge-only).
- Your phone, on the same WiFi as the printer, for the remote-access tests.
- A laptop with an SD-card reader.

Software and accounts:

- Raspberry Pi Imager on the laptop.
- The printer's current LAN IP and, ideally, its `MainboardID` (the bridge can discover
  it, but having it written down helps).
- Optional but recommended: a free Tailscale account (step 7A). Only if you prefer a
  plain browser URL do you need a domain on Cloudflare (step 7B).

What the end state looks like, in ten minutes of reading:

1. The Pi boots headless, joins 2.4 GHz WiFi, and has a fixed IP.
2. The bridge runs as a systemd service, holds **one** WebSocket to the printer, and
   serves a dashboard on `http://<pi>:8787`.
3. The Pi is a Tailscale subnet router for the printer's address only, so your phone can
   open the dashboard — and the printer's own screen — from anywhere with no port
   forwarding.
4. Nothing on the printer is reachable from the internet directly.

---

## 1. Flash the microSD

1. Install and open Raspberry Pi Imager.
2. **Choose device**: Raspberry Pi Zero 2 W.
3. **Choose operating system**: Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**.
   Lite because there is no desktop to run on 512 MB, 64-bit because it is the
   best-supported target for `cloudflared`/Tailscale binaries if you ever need them.
   (32-bit works too; if you choose it, use `cloudflared-linux-armhf` in step 7B.)
4. **Choose storage**: your microSD card. Double-check you are not about to erase a USB
   stick with data on it.
5. Click **Next**, then **Edit Settings** (do not skip this — headless setup depends on
   it). On the **General** tab:
   - Set hostname `elegoo-pi`.
   - Set username `pi` and a strong password. Write the password down; it is also your
     SSH and sudo password.
   - Set the WiFi SSID and password, and set **Wireless LAN country** to your country
     (a wrong country code leaves the radio disabled — a classic silent failure).
   - Set locale and timezone. **Timezone matters**: every ETA and history timestamp in
     the dashboard is rendered from it.
6. **Services** tab: enable **SSH** with password authentication (or paste a public key
   if you already have one — better).
7. **Options**: leave telemetry off; click **Write** and wait for verification to finish.
8. Put the card in the Pi, connect power, and wait 90–120 seconds for first boot.

> **The number one first-time failure:** the Zero 2 W radio is **2.4 GHz only**. If your
> router publishes a single SSID that steers clients to 5 GHz, or you typed the 5 GHz
> SSID, the Pi will never appear on the network. Use a 2.4 GHz SSID, ideally a separate
> one, and make sure it is WPA2 (not WPA3-only) for the simplest path.

**Success looks like:** the green LED flickers for a while after boot, then goes mostly
quiet with occasional activity.

---

## 2. First boot and first SSH session

1. Find the Pi. Try, from the laptop:

   ```bash
   ping -c 3 elegoo-pi.local
   ```

   If mDNS is blocked on your network, open the router's DHCP client list and look for
   `elegoo-pi`.
2. Connect:

   ```bash
   ssh pi@elegoo-pi.local
   ```

   Accept the host key on first connect.
3. Update the base system:

   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo reboot
   ```

   Reconnect after the reboot.
4. Install the tools the rest of this guide uses:

   ```bash
   sudo apt install -y git python3-venv python3-pip curl
   ```

5. Worth setting once, in `sudo raspi-config`:
   - **System Options → Wireless LAN**: confirm the 2.4 GHz network.
   - **Localisation Options → Timezone / Locale**: correct these; they feed the ETAs.
   - **System Options → Boot / Auto Login**: "Console Autologin" is not required; leave
     the default.
   - **Performance Options → GPU memory**: set to **16 MB**. There is no display; the
     default wastes RAM the bridge can use.
   - **Display Options → Splash Screen**: disable (it only obscures boot errors).
6. Confirm the clock is right — a wrong clock breaks TLS and makes every timestamp wrong:

   ```bash
   timedatectl
   ```

   **Success looks like:** `System clock synchronized: yes` and the correct
   `Time zone`.

**Success looks like:** `ssh` works, `sudo apt full-upgrade` completed without held-back
packages, and `python3 --version` reports 3.9 or newer.

---

## 3. Reserve both addresses in the router

This is not optional. The bridge caches the printer's `MainboardID`, but it still needs
to reach the printer at a stable IP, and you need a stable address for the dashboard.

1. Open the router's admin page → DHCP / LAN → **address reservation** (wording varies:
   "static lease", "bind IP", "DHCP reservation").
2. Reserve the Pi's current address for the Pi's MAC address.
3. Reserve the printer's current address for the printer's MAC address. Find the MAC on
   the printer's touchscreen under network settings, or in the router's client list.
4. Reboot the router or renew leases, then confirm on the Pi:

   ```bash
   ip -4 addr show wlan0 | grep inet
   ping -c 3 192.168.178.129
   ```

5. Write both addresses down somewhere you will find them later.

**Success looks like:** `ping` to the printer replies in under ~10 ms, and the Pi's
address is the one you reserved.

> If `ping` fails: the printer may be off, on a different subnet, or on a guest/IoT SSID
> that isolates clients. Client isolation is fatal for this project — the Pi must be able
> to open TCP connections to the printer on ports 80, 3030 and 3031.

---

## 4. Put the project on the Pi

Option A — clone from GitHub (recommended; makes updates trivial):

```bash
cd /home/pi
git clone https://github.com/thijnperd/Elegoo_connector.git
cd Elegoo_connector
```

Option B — copy from your Windows machine without GitHub (from the laptop, in the folder
that contains the project):

```bash
scp -r Elegoo_connector pi@elegoo-pi.local:/home/pi/
```

Then, on the Pi:

```bash
cd /home/pi/Elegoo_connector
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r bridge/requirements.txt
```

> If `bridge/requirements.txt` is missing or differs from what you expect, trust the file
> in the repository over any list in documentation. It should cover the web framework,
> the WebSocket client, the HTTP client and multipart form parsing.

**Success looks like:** `pip install` finishes with "Successfully installed ..." and no
compiler errors. If pip starts building wheels from source on this hardware, stop — the
dependency set is meant to be pure-Python wheels.

Now a first foreground run, with the settings passed as environment variables. Adjust the
two IP-dependent values:

```bash
cd /home/pi/Elegoo_connector
. .venv/bin/activate
export EC_PRINTER_HOST=192.168.178.129
export EC_BIND=0.0.0.0
export EC_PORT=8787
export EC_AUTH_PASSWORD='choose-something-long'
export EC_CONTROL=0
export EC_LOG_LEVEL=INFO
# Optional, and off unless you set them:
# export EC_NOTIFY_URL='https://ntfy.sh/a-long-random-topic'
# export EC_NOTIFY_EVENTS='finished,failed,paused'
# export EC_MAINTENANCE_HOURS=200
cd bridge
python -m elegoo_bridge
```

Notes on those variables:

- `EC_BIND=0.0.0.0` is required for the dashboard to be reachable from your laptop. The
  default, `127.0.0.1`, only listens on the Pi itself.
- `EC_CONTROL=0` keeps the bridge **read-only**: no pause, stop, temperature or upload
  routes exist. Prove monitoring first; switch to `1` in step 6 only when you are happy.
- `EC_MAINBOARD_ID` is optional. If you know it (from a previous discovery run or the
  printer), set it — it saves the bridge from waiting for the printer's attributes push,
  which the firmware withholds in paused and error states.
- Leave `EC_CAMERA_PORT` alone. It is the port the bridge tries **first**, not the only one it
  tries: if nothing answers on 3031 it probes 8080, and it remembers whichever returned image
  bytes. `GET /api/camera` reports the URL it settled on.
- `EC_NOTIFY_URL` turns on push messages. ntfy, Telegram, Discord and a plain JSON webhook
  are supported; the payload shape is inferred from the URL unless `EC_NOTIFY_FORMAT` says
  otherwise. Default events are `finished,failed`; `EC_NOTIFY_EVENTS=all` is noisier than it
  sounds, and `none` disables delivery without removing the URL.
- `EC_MAINTENANCE_HOURS=200` gives you one line in the log (and a "Service due" badge) once
  the counted print hours since the last reset pass that mark. It is a reminder; the
  printer's own maintenance schedule still governs the machine.

In a second SSH session:

```bash
curl -s localhost:8787/healthz
curl -s localhost:8787/api/state | head -c 600
```

**Success looks like:** `{"ok":true,...}` and a JSON snapshot whose
`connection.state` becomes `online` within a few seconds and whose `printer` object is
not `null`. Press `Ctrl-C` in the first session when you are done.

---

## 5. Smoke test from a laptop

1. On the laptop, on the same WiFi: open `http://192.168.178.50:8787`.
2. Log in with the `EC_AUTH_PASSWORD` you exported.
3. Confirm the dashboard shows the printer's state, temperatures, and a camera snapshot.
4. Leave it open for a minute: values should update every few seconds.

If it does not work, check these three in order — they cover almost every failure:

| Symptom | Cause | Fix |
|---|---|---|
| Dashboard loads, `connection.state` stays `connecting`/`offline`, `printer` is `null` | Wrong `EC_PRINTER_HOST`, or the printer is on an isolated SSID, or all five of the printer's WebSocket slots are taken | `ping` the printer from the Pi; `curl -s -o /dev/null -w '%{http_code}' http://<printer>/`; close the printer's own web page in every browser tab and retry |
| State shows but the camera panel stays empty | The printer's camera slots are leaked by previous clients (the bridge already tries both known ports), or this model has no camera | `curl -s localhost:8787/api/camera` shows the URL the bridge settled on; open it directly in a browser. Power-cycle the printer if a slot is stuck |
| The browser asks for a password forever / immediate `401` | The password was exported in the shell but not in the systemd unit (step 6), or it contains a `#` or space that needs quoting | Compare `/etc/elegoo-bridge.env` with what you type; quote the value |

**Success looks like:** live temperatures that change, a state label matching the
printer's screen, and a camera image.

> Optional extra: a Waveshare LCD HAT on the 40-pin header shows the same state without a
> phone. Setup, pinout and the joystick caveat are in [`PANEL.md`](PANEL.md).

---

## 6. Run it as a service

> **Without root.** `scripts/start-bridge.sh` reads the settings from
> `~/.elegoo-bridge.env` (mode 600) and starts the bridge once — it refuses to start a
> second copy and waits for a stopping instance to actually exit. Two crontab lines give
> boot persistence *and* a restart if it dies, without a single `sudo`:
>
> ```cron
> @reboot /home/thijn/Elegoo_connector/scripts/start-bridge.sh start >>/home/thijn/elegoo-bridge.log 2>&1
> */2 * * * * /home/thijn/Elegoo_connector/scripts/start-bridge.sh ensure >>/home/thijn/elegoo-bridge.log 2>&1
> ```
>
> `ensure` is silent when the bridge is already up, so the watchdog costs no log lines. The
> panel gets the same pair (`@reboot /bin/sh /home/thijn/panel.sh ensure` and the same
> `*/2`). The systemd unit below is still the tidier end state, and worth the one `sudo`
> when you have a terminal open.

1. Create the environment file (root-owned; it contains the password):

   ```bash
   sudo tee /etc/elegoo-bridge.env >/dev/null <<'EOF'
   # /etc/elegoo-bridge.env
   EC_PRINTER_HOST=192.168.178.129
   EC_BIND=0.0.0.0
   EC_PORT=8787
   EC_AUTH_USER=operator
   EC_AUTH_PASSWORD=REPLACE_WITH_A_LONG_PASSWORD
   # 0 = read-only monitoring, 1 = allow control and uploads
   EC_CONTROL=0
   EC_PUSH_MS=2000
   EC_POLL_MS=5000
   EC_LOG_LEVEL=INFO
   EC_UPLOAD_MAX_MB=2048
   # Optional:
   # EC_NOTIFY_URL=https://ntfy.sh/REPLACE_WITH_A_LONG_RANDOM_TOPIC
   # EC_NOTIFY_EVENTS=finished,failed
   # EC_MAINTENANCE_HOURS=200
   # EC_METRICS_TOKEN=REPLACE_WITH_A_LONG_RANDOM_VALUE
   EOF
   sudo chmod 600 /etc/elegoo-bridge.env
   sudo chown root:root /etc/elegoo-bridge.env
   ```

   Edit `EC_AUTH_PASSWORD` in place with `sudo nano /etc/elegoo-bridge.env`. Do not leave
   the placeholder.

2. Create the unit:

   ```bash
   sudo tee /etc/systemd/system/elegoo-bridge.service >/dev/null <<'EOF'
   [Unit]
   Description=Elegoo Centauri Carbon bridge
   Wants=network-online.target
   After=network-online.target

   [Service]
   Type=simple
   User=pi
   Group=pi
   WorkingDirectory=/home/pi/Elegoo_connector/bridge
   EnvironmentFile=/etc/elegoo-bridge.env
   ExecStart=/home/pi/Elegoo_connector/.venv/bin/python -m elegoo_bridge
   Restart=always
   RestartSec=5
   # Keep the SD card alive: logs go to RAM.
   StandardOutput=journal
   StandardError=journal

   [Install]
   WantedBy=multi-user.target
   EOF
   ```

   If your username is not `pi`, change `User`, `Group`, `WorkingDirectory` and the
   `ExecStart` path.

3. Enable and start it:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now elegoo-bridge
   systemctl status elegoo-bridge --no-pager
   ```

4. Watch the log while it connects:

   ```bash
   journalctl -u elegoo-bridge -f
   ```

5. Only once monitoring is proven over a day or two, allow control:

   ```bash
   sudo sed -i 's/^EC_CONTROL=0/EC_CONTROL=1/' /etc/elegoo-bridge.env
   sudo systemctl restart elegoo-bridge
   ```

   Read `docs/API.md` §4 first: with `EC_CONTROL=1` the dashboard — and therefore anyone
   who can log in or reach the port — can pause or stop a running print.

**Success looks like:** `systemctl status` shows `active (running)`, the journal shows a
connection and periodic status frames, and `http://<pi>:8787` still works after
`sudo reboot`.

---

## 7. Remote accessTwo supported paths. Pick **one**; you can add the other later. The first is scripted,
the second is manual because it depends on your own domain.

### 7A. Tailscale (recommended: no public URL exists, so the printer's missing
authentication cannot be reached from the internet)

On the Pi, one command does the install and the configuration:

```bash
cd /home/thijn/Elegoo_connector
sudo sh scripts/remote-access.sh setup
```

It installs Tailscale, brings it up (it prints a URL — open it on any device once, sign in,
approve the Pi), turns on IPv4 forwarding, and publishes the dashboard on
`https://<pi>.<tailnet>.ts.net`. It finishes by printing the same report `status` gives:

```bash
sh scripts/remote-access.sh status
```

```
tailscale     1.86.2
              Running, elegoo-link.tail1234.ts.net, 100.64.0.12
dashboard     http://100.64.0.12:8787
serve         https://elegoo-link.tail1234.ts.net (tailnet only)
```

`scripts/doctor.py` reports the same thing on a `remote access` row, so a broken tunnel
shows up in the routine check rather than when you are away from home.

**Optional — also reach the printer's own page.** Off by default, because it widens what
an approved device can touch:

```bash
sudo sh scripts/remote-access.sh printer
```

Then approve the route: admin console → **Machines** → this Pi → **Edit route settings**.
Only the printer is advertised (`/32`), never your whole LAN.

Restrict who may use that route. In **Access controls**, add a grant limited to the
printer's ports and to your own devices, for example:

   ```json
   {
     "grants": [
       {
         "src": ["autogroup:member"],
         "dst": ["192.168.178.129"],
         "ip": ["tcp:80", "tcp:3030", "tcp:3031"]
       }
     ]
   }
   ```

   This lets your devices reach the printer's own UI and camera, not your NAS or laptop.

Then install the Tailscale app on your phone and sign in to the same tailnet.

Test properly: **turn WiFi off on the phone** (mobile data), open Tailscale, then browse to
the `dashboard` URL from the report. The dashboard should load exactly as it does at home;
with the printer route approved, `http://192.168.178.129/` works too.

#### Away from home: the two-minute checklist

Do this once at home, then it is the same every time:

1. On the Pi: `sudo sh scripts/remote-access.sh setup`, open the printed URL on the phone,
   sign in, approve the Pi.
2. Install the Tailscale app on the phone and sign in with the same account.
3. On the Pi: `sh scripts/remote-access.sh status` and note the `dashboard` line. Add it to
   the phone's home screen; you will want it without thinking.
4. Prove it at home first: WiFi **off**, mobile data on, Tailscale connected, open the URL,
   log in with `EC_AUTH_PASSWORD` from `~/.elegoo-bridge.env`.

Away from home:

1. Power the Pi. The bridge and the panel start themselves; cron re-checks both every two
   minutes, so a crash comes back on its own.
2. Open Tailscale on the phone and confirm the switch is on and the Pi is listed.
3. Open the dashboard URL and log in.

If it does not load: Tailscale on the phone off or logged out (most common), the Pi is not
powered or has no WiFi, or you are on a network that blocks outbound UDP — switch the phone
to mobile data and try again.

Do **not** use `tailscale funnel` — that publishes to the public internet with no login
wall, which is the one thing this design avoids. `sudo sh scripts/remote-access.sh off`
stops accepting tailnet traffic and leaves everything else in place.

### 7B. Cloudflare Tunnel + Access (no VPN app on the phone)

Prerequisites: a domain whose nameservers are already on Cloudflare (the free plan is
fine).

1. In Cloudflare Zero Trust, create the **Access application first**:
   - Applications → Add an application → **Self-hosted**.
   - Application domain: `printer.yourdomain.com` (a **subdomain**, not a path — the
     dashboard uses root-absolute asset paths).
   - Policy: action **Allow**, include **Emails** → your email address only.
   - Authentication: **One-time PIN** is enough.
   - Save.
2. Create the tunnel: Networks → Tunnels → **Create a tunnel** → Cloudflared → name it
   `elegoo-pi`. Copy the token.
3. On the Pi, install and register the connector:

   ```bash
   curl -fL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o cloudflared.deb
   sudo dpkg -i cloudflared.deb
   sudo cloudflared service install '<TUNNEL_TOKEN>'
   ```

   On a 32-bit OS use `cloudflared-linux-armhf.deb` instead.

4. Back in the tunnel's **Public hostname** tab, add:
   - Subdomain `printer`, domain `yourdomain.com`.
   - Service: **HTTP** → `localhost:8787`.

   It must point at the **bridge**, never at the printer's IP and never at port 80 or
   3030. The bridge is the only thing that has an authentication system.
5. Test from cellular data with WiFi off. You should get the Cloudflare login page first,
   then the dashboard.

Two things people get wrong here:

- The often-quoted "125 seconds" is Cloudflare's **HTTP response** timeout (a 524 when an
  origin never sends headers). It is not a limit on WebSocket lifetime. Long-lived
  sockets stay up as long as traffic flows; the bridge sends status pushes and SSE
  heartbeats for exactly that reason.
- Once the Access application exists, the tunnel is not public — but only for that
  hostname. Do not add a second public hostname "just for testing".

**Success looks like:** both paths end with the dashboard loading over mobile data, and
`curl` from an unrelated network returns a Cloudflare login page rather than your data.

---

## 8. Hardening checklist

- **No port forwards.** Not 80, not 3030, not 3031, not 8787. If you forwarded anything
  to the printer while experimenting, remove it now.
- **Printer on an IoT SSID/VLAN** if the router supports it, allowing only the Pi to reach
  it. The printer's interface has no password at all.
- **Unattended security updates:**

  ```bash
  sudo apt install -y unattended-upgrades
  sudo dpkg-reconfigure -plow unattended-upgrades
  ```

- **Protect the SD card.** Keep the journal in RAM and cap its size:

  ```bash
  sudo mkdir -p /etc/systemd/journald.conf.d
  printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=50M\n' | sudo tee /etc/systemd/journald.conf.d/99-elegoo.conf
  sudo systemctl restart systemd-journald
  ```

  Note that this makes logs disappear across reboots; if you need history, use
  `Storage=persistent` with `SystemMaxUse=50M` instead and accept the writes.
- **Keep a recovery path.** An SSH key plus `sudo tailscale ssh pi@elegoo-pi` (or plain
  SSH over the tailnet) means you can fix a broken bridge without touching the SD card.
  Test it before you need it.
- **A monthly habit:** `sudo apt update && sudo apt full-upgrade -y`, then
  `cd /home/pi/Elegoo_connector && git pull && sudo systemctl restart elegoo-bridge`.
- **Do not run Docker, a desktop, or an identity server on this board.** 512 MB is the
  real constraint; the bridge plus one tunnel client is the intended load.

---

## 9. Verify-from-anywhere acceptance tests

Run these in order after step 7, with the phone on **mobile data only**:

1. **Live state.** Open the dashboard; state and temperatures match the printer's screen
   and update within about 5 seconds.
2. **Staleness.** Set `EC_POLL_MS=5000`; the values should tick. Unplug the printer's
   network (or power) and confirm the UI marks the data stale/offline within ~20 seconds
   rather than showing old numbers as current.
3. **Camera.** The snapshot loads on cellular. If you enable the live stream, it stops
   when you leave the tab.
4. **Control (only with `EC_CONTROL=1`).** Pause a running print from the phone and watch
   the printer acknowledge on its screen; resume it. This is the test most likely to
   reveal a wrong `MainboardID`.
5. **Auth.** In a private browser window with no cookie, the dashboard must ask for the
   password; a wrong password must return `401` and not a partial page.
6. **Nothing else is exposed.** From the same phone, `http://192.168.178.129:3030` must be
   unreachable unless you deliberately allowed the printer route in Tailscale; the
   Cloudflare hostname must never serve the printer's own UI.
7. **Survives a reboot.** `sudo reboot` the Pi; the dashboard is back within a minute
   without you logging in.

---

## 10. Updating and rolling back

Update:

```bash
cd /home/pi/Elegoo_connector
git pull --ff-only
. .venv/bin/activate && pip install -r bridge/requirements.txt
sudo systemctl restart elegoo-bridge
journalctl -u elegoo-bridge -n 50 --no-pager
```

If the new version misbehaves, go back one commit:

```bash
git log --oneline -5
git checkout <previous-commit>
sudo systemctl restart elegoo-bridge
```

Snapshot the SD card before a big change: power the Pi off, put the card in the laptop,
and use Raspberry Pi Imager's **Use custom image → read from device** (or `dd` /
Win32DiskImager) to write an `.img` you keep aside. It is the only reliable rollback for
a corrupted filesystem.

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Pi never appears on the network | 5 GHz-only SSID, wrong country code, or WPA3-only | Reflash with a 2.4 GHz SSID and the correct country; or use a USB OTG Ethernet adapter |
| Bridge logs `too many clients` / WS closes immediately | The printer allows ~5 WebSocket clients; the printer's own web UI, a phone app and old tabs all count | Close every other client; keep exactly one bridge instance running (`systemctl status` for duplicates) |
| State freezes at idle but resumes when a print starts | The firmware's status-push scheduler can go dormant at idle while `Cmd 0` still works | Expected: the bridge falls back to polling. Do not "fix" it by shortening `EC_POLL_MS` below 2 s |
| Some fields empty (chamber temperature, light) | Your firmware does not report them | Not a bug — the UI shows them as unknown |
| Camera works, then dies until restart | The printer leaks camera connection slots; a stale client keeps holding one | Power-cycle the printer, then reload the dashboard. The bridge already multiplexes one upstream connection |
| `pip install` tries to compile C code | Wrong dependency for armhf or a missing wheel | Check `python3 --version` and architecture with `uname -m`; a 64-bit OS on the Zero 2 W is the supported path |
| Service restarts in a loop | Bad value in `/etc/elegoo-bridge.env`, or a wrong `WorkingDirectory` | `journalctl -u elegoo-bridge -n 80 --no-pager` shows the traceback |
| Works on LAN, not on cellular | Tunnel points at the printer instead of the bridge, Access policy blocks you, or Tailscale route unapproved | Re-check the public hostname target (`localhost:8787`) and the route approval |
| `401` on every request from the phone but fine on the laptop | Old browser cookie for a different password | Log out, clear the site data, log in again |
| Filesystem goes read-only, boot loops, or `dmesg` shows I/O errors | SD card wear or corruption | Reflash; move to a high-endurance card and apply the journal settings in step 8 |
| Dashboard works, but the Pi disappears from WiFi after hours | Undervoltage or 2.4 GHz congestion | Check `vcgencmd get_throttled` (`0x0` is healthy); use the official PSU; move the Pi nearer the AP |
