#!/usr/bin/env python3
"""One command that answers "is this thing healthy?".

Every failure mode this project has actually hit is a check here: the bridge
process gone, the printer's WebSocket slots taken, a frozen status frame, a
camera that reports available but returns HTML, an SD card filling up, and an
undervoltage board that drops WiFi.

    python3 scripts/doctor.py                 # on the Pi, against localhost
    python3 scripts/doctor.py --url http://192.168.178.83:8787 --password ...

Exit status is 0 only when nothing FAILed, so it can run from cron:

    python3 scripts/doctor.py --quiet || echo "bridge unhealthy" | mail -s ...
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

OK = " OK "
WARN = "WARN"
FAIL = "FAIL"

JPEG_MAGIC = b"\xff\xd8"


@dataclass
class Result:
    level: str
    name: str
    detail: str


class Doctor:
    def __init__(self, url: str, password: str | None, printer_host: str | None) -> None:
        self.url = url.rstrip("/")
        self.password = password
        self.printer_host = printer_host
        self.results: list[Result] = []
        self.state: dict | None = None

    # --- plumbing ----------------------------------------------------------

    def add(self, level: str, name: str, detail: str = "") -> None:
        self.results.append(Result(level, name, detail))

    def get(self, path: str, *, timeout: float = 6.0) -> tuple[int, bytes]:
        request = urllib.request.Request(f"{self.url}{path}")
        if self.password:
            request.add_header("Cookie", self._cookie)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            return 0, str(err).encode()

    @property
    def _cookie(self) -> str:
        return getattr(self, "_session", "")

    def login(self) -> bool:
        if not self.password:
            return True
        payload = json.dumps({"password": self.password}).encode()
        request = urllib.request.Request(
            f"{self.url}/api/login",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                raw = response.headers.get("Set-Cookie", "")
                del response
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            return False
        self._session = raw.split(";", 1)[0]
        return bool(self._session)

    # --- checks ------------------------------------------------------------

    def check_process(self) -> None:
        pids = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if cmdline.rstrip().endswith("-m elegoo_bridge"):
                pids.append(entry.name)
        if pids:
            self.add(OK, "bridge process", f"pid {', '.join(pids)}")
        else:
            self.add(FAIL, "bridge process", "not running (systemctl/cron not started?)")

    def check_healthz(self) -> None:
        status, body = self.get("/healthz")
        if status != 200:
            self.add(FAIL, "healthz", f"HTTP {status or 'unreachable'}")
            return
        try:
            data = json.loads(body)
        except ValueError:
            self.add(FAIL, "healthz", "not JSON")
            return
        uptime = int(data.get("uptime_s") or 0)
        self.add(
            OK if data.get("ok") else FAIL,
            "healthz",
            f"connection {data.get('connection')}, printer {data.get('printer')}, "
            f"up {uptime // 60}m, control {'ON' if data.get('control') else 'off'}",
        )

    def check_state(self) -> None:
        status, body = self.get("/api/state")
        if status == 401:
            self.add(FAIL, "state", "401 - wrong password or no session")
            return
        if status != 200:
            self.add(FAIL, "state", f"HTTP {status or 'unreachable'}")
            return
        try:
            data = json.loads(body)
        except ValueError:
            self.add(FAIL, "state", "not JSON")
            return
        self.state = data
        connection = data.get("connection") or {}
        printer = data.get("printer")
        age = connection.get("last_frame_age_s")
        if connection.get("state") != "online":
            self.add(
                FAIL,
                "printer link",
                f"{connection.get('state')}: {connection.get('last_error') or 'no detail'}",
            )
            return
        if printer is None:
            self.add(FAIL, "printer link", "online but no status frame yet")
            return
        if age is None or age > 10:
            self.add(WARN, "status frame", f"last frame {age}s ago (stale threshold 10s)")
        else:
            self.add(OK, "printer link", f"{printer.get('state_label')}, frame {age}s old")
        if printer.get("stale"):
            self.add(WARN, "freshness", f"values marked stale ({printer.get('stale_for_s')}s)")
        if printer.get("attention"):
            self.add(WARN, "attention", (printer.get("error") or {}).get("message", "printer wants a human"))
        firmware = connection.get("firmware")
        self.add(OK, "firmware", f"{firmware or 'unknown'}, id {(connection.get('mainboard_id') or '?')[:12]}")

    def check_camera(self) -> None:
        status, body = self.get("/api/camera/snapshot", timeout=12)
        if status != 200:
            self.add(WARN, "camera", f"snapshot HTTP {status or 'unreachable'}")
            return
        if body[:2] != JPEG_MAGIC:
            self.add(FAIL, "camera", f"not a JPEG ({len(body)} bytes) - port or path wrong")
            return
        self.add(OK, "camera", f"JPEG {len(body) // 1024} KB")

    def check_ports(self) -> None:
        if not self.printer_host:
            self.add(WARN, "printer ports", "no EC_PRINTER_HOST known")
            return
        wanted = {80: "http", 3030: "sdcp", 3031: "mjpeg"}
        closed = []
        for port, name in wanted.items():
            try:
                with socket.create_connection((self.printer_host, port), timeout=3):
                    continue
            except OSError:
                closed.append(f"{port}/{name}")
        if closed:
            self.add(WARN, "printer ports", f"closed: {', '.join(closed)}")
        else:
            self.add(OK, "printer ports", "80, 3030, 3031 all open")

    def check_disk(self) -> None:
        stats = os.statvfs("/")
        total = stats.f_blocks * stats.f_frsize
        free = stats.f_bavail * stats.f_frsize
        if total == 0:
            return
        used = 100.0 * (1 - free / total)
        level = FAIL if used > 92 else WARN if used > 85 else OK
        self.add(level, "sd card", f"{used:.0f}% used, {free / 1e9:.1f} GB free of {total / 1e9:.1f} GB")

    def check_power(self) -> None:
        try:
            raw = subprocess.run(
                ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            self.add(WARN, "power", "vcgencmd missing")
            return
        if "0x0" in raw:
            self.add(OK, "power", "no undervoltage or throttling since boot")
        else:
            self.add(FAIL, "power", f"{raw} - check the 5V supply")

    def check_temperature(self) -> None:
        try:
            raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
            celsius = int(raw) / 1000.0
        except (OSError, ValueError):
            self.add(WARN, "cpu temp", "unreadable")
            return
        level = FAIL if celsius >= 80 else WARN if celsius >= 70 else OK
        self.add(level, "cpu temp", f"{celsius:.1f} C")

    def check_clock(self) -> None:
        try:
            raw = subprocess.run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            self.add(WARN, "clock", "timedatectl missing")
            return
        self.add(OK if raw == "yes" else WARN, "clock", f"ntp synced: {raw}, local {time.strftime('%H:%M:%S')}")

    def check_remote(self) -> None:
        """Remote access is optional: stay quiet unless Tailscale is actually installed."""
        try:
            done = subprocess.run(
                ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=6
            )
        except (OSError, subprocess.SubprocessError):
            return
        if done.returncode != 0:
            self.add(WARN, "remote access", "tailscale installed but not answering")
            return
        try:
            data = json.loads(done.stdout)
        except ValueError:
            self.add(WARN, "remote access", "unreadable tailscale status")
            return
        state = data.get("BackendState")
        if state != "Running":
            self.add(WARN, "remote access", f"tailscale {state} - not reachable from outside")
            return
        me = data.get("Self") or {}
        name = (me.get("DNSName") or "").rstrip(".") or "no name"
        ips = me.get("TailscaleIPs") or []
        port = self.url.rsplit(":", 1)[-1] if ":" in self.url else "8787"
        self.add(OK, "remote access", f"{name}, http://{ips[0]}:{port}" if ips else name)

    def check_panel(self) -> None:
        frames = sorted(Path("/dev").glob("fb*"))
        spi = sorted(Path("/dev").glob("spidev*"))
        if frames:
            self.add(OK, "lcd panel", f"{', '.join(str(f) for f in frames)} present")
        elif spi:
            self.add(WARN, "lcd panel", f"SPI on ({', '.join(map(str, spi))}) but no /dev/fbN - overlay missing")
        else:
            self.add(WARN, "lcd panel", "SPI disabled - no spidev, no framebuffer")

    # --- driver ------------------------------------------------------------

    def run(self, *, quiet: bool) -> int:
        self.check_process()
        self.check_healthz()
        if self.password and not self.login():
            self.add(FAIL, "login", "password rejected")
        self.check_state()
        self.check_camera()
        if self.state:
            host = (self.state.get("connection") or {}).get("printer_host")
            self.printer_host = self.printer_host or host
        self.check_ports()
        self.check_disk()
        self.check_power()
        self.check_temperature()
        self.check_clock()
        self.check_panel()
        self.check_remote()

        failures = sum(1 for r in self.results if r.level == FAIL)
        warnings = sum(1 for r in self.results if r.level == WARN)
        if not quiet or failures:
            print(f"\nElegoo Connector self-check  ({socket.gethostname()}  {time.strftime('%Y-%m-%d %H:%M')})")
            print("-" * 72)
            for result in self.results:
                print(f"  [{result.level}] {result.name:<16} {result.detail}")
            print("-" * 72)
            verdict = "healthy" if not failures else f"{failures} failure(s)"
            print(f"  {verdict}" + (f", {warnings} warning(s)" if warnings else "") + "\n")
        return 1 if failures else 0


def password_from_env_file() -> str | None:
    for candidate in (Path.home() / ".elegoo-bridge.env", Path("/etc/elegoo-bridge.env")):
        try:
            for line in candidate.read_text().splitlines():
                if line.startswith("EC_AUTH_PASSWORD="):
                    return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Self-check for the Elegoo bridge.")
    parser.add_argument("--url", default=os.environ.get("EC_DOCTOR_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--password", default=os.environ.get("EC_AUTH_PASSWORD") or password_from_env_file())
    parser.add_argument("--printer", default=os.environ.get("EC_PRINTER_HOST"))
    parser.add_argument("--quiet", action="store_true", help="print only when something failed")
    args = parser.parse_args(argv)

    doctor = Doctor(args.url, args.password, args.printer)
    try:
        return doctor.run(quiet=args.quiet)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
