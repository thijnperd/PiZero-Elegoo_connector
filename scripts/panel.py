#!/usr/bin/env python3
"""Draw the printer on the Waveshare LCD HAT that sits on the Pi's 40-pin header.

The HAT (1.44" 128x128 ST7735S or 1.3" 240x240 ST7789) is a bare SPI panel: no
touch, no controller memory of its own. Once the kernel drives it through an
``fbtft`` overlay it shows up as ``/dev/fbN`` and is a plain array of 16-bit
words — so the whole renderer fits in the standard library, with no Pillow, no
fonts to ship, and nothing to compile on a 512 MB board.

It is a small home screen, not a printer readout: the panel boots on **home**, a
2x2 launcher of apps (Elegoo, clock, weather, timer), each with a live value on its
tile. The printer is one of those apps, not the whole device. Weather comes from
Buienradar for one fixed place (Valkenswaard by default); the timer and the clock
need no network at all, so the panel still does something useful when the bridge is
away.

Two modes, one code path:

* ``--preview out.png`` renders a still and writes a PNG. Works on any machine,
  including the laptop, which is how the layout is checked without hardware.
* ``--fb /dev/fb1`` polls the bridge and blits frames onto the panel.

Rotation is ours, not the driver's: the fbtft overlay must run with ``rotate=0``.
With ``rotate=90`` the driver sets the ST7789 to MV|MY, which swaps the address
axes of the controller's 240x320 RAM; a 240x240 picture then lands offset and
leaves a band of stale memory on screen. ``--rotate`` turns the frame here.

The font is a 5x7 bitmap table below. It is deliberately uppercase-only: at
128 px wide there is room for about ten characters per row, and lowercase at
that size is mush.

    python3 scripts/panel.py --demo --preview /tmp/panel.png
    python3 scripts/panel.py --fb /dev/fb1 --url http://127.0.0.1:8787
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import math
import os
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- colour -----------------------------------------------------------------

RGB = tuple[int, int, int]

BG: RGB = (8, 10, 14)
FG: RGB = (236, 239, 244)
#: Secondary text at 7.2:1 on BG. WCAG asks 4.5:1 for small text; a cheap TN panel with
#: narrow viewing angles eats contrast, so this sits near the AAA target instead.
DIM: RGB = (147, 157, 171)
#: Differentiators only (rules between groups, empty progress tracks).
LINE: RGB = (42, 48, 58)
#: Boundaries that identify something you can select, at 3.3:1 on BG (WCAG 1.4.11).
LINE_STRONG: RGB = (89, 100, 111)
GREEN: RGB = (72, 196, 120)
CYAN: RGB = (86, 190, 236)
AMBER: RGB = (240, 178, 62)
RED: RGB = (232, 84, 84)

STATE_COLOUR = {
    "idle": GREEN,
    "completed": GREEN,
    "stopped": DIM,
    "printing": CYAN,
    "preheating": CYAN,
    "leveling": CYAN,
    "homing": CYAN,
    "starting": CYAN,
    "calibrating": CYAN,
    "checking": CYAN,
    "resuming": CYAN,
    "paused": AMBER,
    "pausing": AMBER,
    "error": RED,
    "unknown": DIM,
}

#: 5x7 glyphs, rows separated by '/'. '#' is ink. Uppercase only by design.
FONT = {
    "A": ".###./#...#/#...#/#####/#...#/#...#/#...#",
    "B": "####./#...#/#...#/####./#...#/#...#/####.",
    "C": ".###./#...#/#..../#..../#..../#...#/.###.",
    "D": "####./#...#/#...#/#...#/#...#/#...#/####.",
    "E": "#####/#..../#..../####./#..../#..../#####",
    "F": "#####/#..../#..../####./#..../#..../#....",
    "G": ".###./#...#/#..../#.###/#...#/#...#/.###.",
    "H": "#...#/#...#/#...#/#####/#...#/#...#/#...#",
    "I": "#####/..#../..#../..#../..#../..#../#####",
    "J": "..###/...#./...#./...#./...#./#..#./.##..",
    "K": "#...#/#..#./#.#../##.../#.#../#..#./#...#",
    "L": "#..../#..../#..../#..../#..../#..../#####",
    "M": "#...#/##.##/#.#.#/#...#/#...#/#...#/#...#",
    "N": "#...#/##..#/#.#.#/#..##/#...#/#...#/#...#",
    "O": ".###./#...#/#...#/#...#/#...#/#...#/.###.",
    "P": "####./#...#/#...#/####./#..../#..../#....",
    "Q": ".###./#...#/#...#/#...#/#.#.#/#..#./.##.#",
    "R": "####./#...#/#...#/####./#.#../#..#./#...#",
    "S": ".####/#..../#..../.###./....#/....#/####.",
    "T": "#####/..#../..#../..#../..#../..#../..#..",
    "U": "#...#/#...#/#...#/#...#/#...#/#...#/.###.",
    "V": "#...#/#...#/#...#/#...#/#...#/.#.#./..#..",
    "W": "#...#/#...#/#...#/#.#.#/#.#.#/##.##/#...#",
    "X": "#...#/#...#/.#.#./..#../.#.#./#...#/#...#",
    "Y": "#...#/#...#/.#.#./..#../..#../..#../..#..",
    "Z": "#####/....#/...#./..#../.#.../#..../#####",
    "0": ".###./#...#/#..##/#.#.#/##..#/#...#/.###.",
    "1": "..#../.##../..#../..#../..#../..#../.###.",
    "2": ".###./#...#/....#/...#./..#../.#.../#####",
    "3": "####./....#/....#/.###./....#/....#/####.",
    "4": "...#./..##./.#.#./#..#./#####/...#./...#.",
    "5": "#####/#..../####./....#/....#/#...#/.###.",
    "6": "..##./.#.../#..../####./#...#/#...#/.###.",
    "7": "#####/....#/...#./..#../.#.../.#.../.#...",
    "8": ".###./#...#/#...#/.###./#...#/#...#/.###.",
    "9": ".###./#...#/#...#/.####/....#/...#./.##..",
    " ": "...../...../...../...../...../...../.....",
    ".": "...../...../...../...../...../.##../.##..",
    ",": "...../...../...../...../.##../.##../.#...",
    ":": "...../.##../.##../...../.##../.##../.....",
    "%": "##..#/##.#./..#../.#.../#..##/...##/.....",
    "/": "....#/....#/...#./..#../.#.../#..../#....",
    "-": "...../...../...../#####/...../...../.....",
    "+": "...../..#../..#../#####/..#../..#../.....",
    ">": "#..../.#.../..#../...#./..#../.#.../#....",
    "<": "....#/...#./..#../.#.../..#../...#./....#",
    "!": "..#../..#../..#../..#../..#../...../..#..",
    "?": ".###./#...#/....#/...#./..#../...../..#..",
    "_": "...../...../...../...../...../...../#####",
    "\u00b0": ".##../#..#./.##../...../...../...../.....",
}


def rgb565(colour: RGB) -> bytes:
    """Pack one colour the way the kernel framebuffer expects it."""
    r, g, b = colour
    value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return struct.pack("<H", value)


def shorten(text: str, columns: int) -> str:
    text = (text or "").upper()
    return text if len(text) <= columns else text[:columns]


def human_seconds(value: float | int | None) -> str:
    if value is None:
        return "--"
    total = int(max(0, value))
    hours, rest = divmod(total, 3600)
    minutes, _ = divmod(rest, 60)
    if hours:
        return f"{hours}H{minutes:02d}"
    return f"{minutes}M{rest % 60:02d}"


@dataclass
class Canvas:
    """An RGB565 framebuffer we can blit and also turn into a PNG."""

    width: int
    height: int

    def __post_init__(self) -> None:
        self.scale = 2 if self.height <= 160 else 3
        self.advance = 6 * self.scale
        self.columns = max(1, (self.width - 8 * self.scale) // self.advance)
        # The header has to leave room for the link dot on the right.
        self.header_columns = max(1, (self.width - 15 * self.scale) // self.advance)
        self.buf = bytearray(self.width * self.height * 2)

    # --- primitives --------------------------------------------------------

    def clear(self, colour: RGB = BG) -> None:
        self.buf[:] = rgb565(colour) * (self.width * self.height)

    def _span(self, x: int, y: int, length: int, colour: RGB) -> None:
        if not (0 <= y < self.height):
            return
        x0 = max(0, x)
        x1 = min(self.width, x + length)
        if x1 <= x0:
            return
        self.buf[(y * self.width + x0) * 2 : (y * self.width + x1) * 2] = rgb565(colour) * (x1 - x0)

    def fill_rect(self, x: int, y: int, w: int, h: int, colour: RGB) -> None:
        for row in range(y, y + h):
            self._span(x, row, w, colour)

    def frame_rect(self, x: int, y: int, w: int, h: int, colour: RGB) -> None:
        self._span(x, y, w, colour)
        self._span(x, y + h - 1, w, colour)
        for row in range(y, y + h):
            self._span(x, row, 1, colour)
            self._span(x + w - 1, row, 1, colour)

    def disc(self, cx: int, cy: int, radius: int, colour: RGB) -> None:
        for dy in range(-radius, radius + 1):
            span = int((radius * radius - dy * dy) ** 0.5)
            self._span(cx - span, cy + dy, span * 2 + 1, colour)

    def line(self, x0: int, y0: int, x1: int, y1: int, colour: RGB, width: int = 1) -> None:
        """Bresenham. Icons are drawn every frame, and this is cheap at 240 px."""
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        step_x = 1 if x0 < x1 else -1
        step_y = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.fill_rect(x0, y0, width, width, colour)
            if x0 == x1 and y0 == y1:
                break
            doubled = 2 * err
            if doubled >= dy:
                err += dy
                x0 += step_x
            if doubled <= dx:
                err += dx
                y0 += step_y

    def ring(self, cx: int, cy: int, radius: int, colour: RGB, thickness: int = 1) -> None:
        """Circle outline: two spans per row rather than a distance test per pixel."""
        inner = max(0, radius - thickness)
        for dy in range(-radius, radius + 1):
            outer_span = int((radius * radius - dy * dy) ** 0.5)
            if abs(dy) < inner:
                inner_span = int((inner * inner - dy * dy) ** 0.5)
                self._span(cx - outer_span, cy + dy, outer_span - inner_span, colour)
                self._span(cx + inner_span + 1, cy + dy, outer_span - inner_span, colour)
            else:
                self._span(cx - outer_span, cy + dy, outer_span * 2 + 1, colour)

    def border(self, x: int, y: int, w: int, h: int, colour: RGB, thickness: int = 1) -> None:
        """frame_rect is one pixel; a focused tile needs a visible edge at scale 3."""
        self.fill_rect(x, y, w, thickness, colour)
        self.fill_rect(x, y + h - thickness, w, thickness, colour)
        self.fill_rect(x, y, thickness, h, colour)
        self.fill_rect(x + w - thickness, y, thickness, h, colour)

    def glyph(self, char: str, x: int, y: int, scale: int, colour: RGB) -> None:
        rows = FONT.get(char.upper(), FONT["?"]).split("/")
        for ry, bits in enumerate(rows):
            for rx, bit in enumerate(bits):
                if bit == "#":
                    self.fill_rect(x + rx * scale, y + ry * scale, scale, scale, colour)

    def text(self, x: int, y: int, value: str, colour: RGB = FG, *, scale: int | None = None) -> int:
        step = 6 * (scale or self.scale)
        for index, char in enumerate(value.upper()):
            self.glyph(char, x + index * step, y, scale or self.scale, colour)
        return x + len(value) * step

    def rotated(self, degrees: int) -> "Canvas":
        """Turn the finished frame clockwise. Square panels only, which is the HAT."""
        degrees %= 360
        if degrees == 0:
            return self
        if degrees not in (90, 180, 270):
            raise ValueError("rotation must be 0, 90, 180 or 270")
        out = Canvas(self.width, self.height)
        width, height = self.width, self.height
        for y in range(height):
            for x in range(width):
                if degrees == 180:
                    nx, ny = width - 1 - x, height - 1 - y
                elif degrees == 90:
                    nx, ny = height - 1 - y, x
                else:
                    nx, ny = y, width - 1 - x
                src = (y * width + x) * 2
                dst = (ny * width + nx) * 2
                out.buf[dst : dst + 2] = self.buf[src : src + 2]
        return out

    # --- png ---------------------------------------------------------------

    def row_bytes(self, y: int) -> bytes:
        out = bytearray(self.width * 3)
        base = y * self.width * 2
        for index in range(self.width):
            (value,) = struct.unpack_from("<H", self.buf, base + index * 2)
            r = (value >> 11) & 0x1F
            g = (value >> 5) & 0x3F
            b = value & 0x1F
            out[index * 3] = (r << 3) | (r >> 2)
            out[index * 3 + 1] = (g << 2) | (g >> 4)
            out[index * 3 + 2] = (b << 3) | (b >> 2)
        return bytes(out)

    def write_png(self, path: str) -> None:
        raw = bytearray()
        for y in range(self.height):
            raw.append(0)  # filter: none
            raw += self.row_bytes(y)

        def chunk(tag: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + tag
                + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
            )

        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
            handle.write(chunk(b"IHDR", header))
            handle.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
            handle.write(chunk(b"IEND", b""))


# --- rendering --------------------------------------------------------------


def _temp(temp: dict[str, Any] | None) -> str:
    """Formatted with the degree mark, or ``--`` when the firmware is silent."""
    actual = (temp or {}).get("actual")
    if not isinstance(actual, (int, float)):
        return "--"
    return f"{actual:.1f}\u00b0"


def _header(canvas: Canvas, label: str, colour: RGB, online: bool) -> int:
    """Title with the link dot on the right and a rule underneath; returns the next y."""
    k = canvas.scale
    margin = 4 * k
    canvas.text(margin, 2 * k, shorten(label, canvas.header_columns), colour)
    radius = 2 * k
    cx, cy = canvas.width - margin - radius, 3 * k + radius
    if online:
        canvas.disc(cx, cy, radius, colour)
    else:
        # Shape as well as colour: a hollow, struck-through dot reads as "no link" even
        # when red and green look the same to the person in front of it (WCAG 1.4.1).
        canvas.ring(cx, cy, radius, RED, max(1, k // 2))
        canvas.line(cx - radius, cy + radius, cx + radius, cy - radius, RED, max(1, k // 2))
    canvas.fill_rect(margin, 11 * k, canvas.width - 2 * margin, max(1, k // 2), LINE)
    return 13 * k


def _footer(canvas: Canvas, online: bool, stale: bool, colour: RGB, *, status: bool = True) -> None:
    if canvas.height < 200:
        return
    k = canvas.scale
    margin = 4 * k
    # 7k of glyph plus a little breathing room; height - 6k clipped the descenders.
    footer = canvas.height - 8 * k
    canvas.text(margin, footer, time.strftime("%H:%M"), DIM)
    if not status:
        # The offline page says OFFLINE in the header; repeating it here collides
        # with the clock at this width.
        return
    status_text = "LIVE" if online and not stale else ("STALE" if online else "OFFLINE")
    canvas.text(
        canvas.width - margin - len(status_text) * canvas.advance,
        footer,
        status_text,
        colour if online else RED,
    )


def _bridge_up(state: dict[str, Any]) -> bool:
    """Did the bridge itself answer? A printer that is switched off still counts.

    `/api/state` always carries a timestamp; the fallback frame this panel invents when
    the request failed does not. That is the difference between "no printer" and "no
    bridge", and the dot on home should not confuse the two.
    """
    return "ts" in state


def _facts(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], bool, str, RGB]:
    connection = state.get("connection") or {}
    printer = state.get("printer") or {}
    online = connection.get("state") == "online"
    slug = printer.get("state") or "unknown"
    colour = STATE_COLOUR.get(slug, DIM) if online else RED
    return connection, printer, online, slug, colour


def render_status(state: dict[str, Any], width: int, height: int) -> Canvas:
    canvas = Canvas(width, height)
    canvas.clear()
    k = canvas.scale
    margin = 4 * k
    row = 9 * k

    connection, printer, online, slug, colour = _facts(state)
    label = (printer.get("state_label") or slug).upper()

    y = _header(canvas, label, colour, online)

    temps = printer.get("temps") or {}
    y += 2 * k
    for key, name in (("nozzle", "NOZ"), ("bed", "BED"), ("chamber", "CHM")):
        if key == "chamber" and height < 200:
            continue
        canvas.text(margin, y, name, DIM)
        canvas.text(margin + 4 * canvas.advance, y, _temp(temps.get(key)), FG)
        y += row

    y += k
    canvas.fill_rect(margin, y, width - 2 * margin, max(1, k // 2), LINE)
    y += 2 * k

    job = printer.get("job")
    stale = printer.get("stale")
    if job and job.get("filename"):
        canvas.text(margin, y, shorten(job["filename"], canvas.columns), FG)
        y += row
        bar_h = 4 * k
        bar_w = width - 2 * margin
        canvas.fill_rect(margin, y, bar_w, bar_h, LINE)
        progress = job.get("progress")
        ratio = 0.0 if progress is None else max(0.0, min(100.0, float(progress))) / 100.0
        if ratio > 0:
            canvas.fill_rect(margin, y, max(1, int(bar_w * ratio)), bar_h, CYAN)
        y += bar_h + 2 * k
        percent = "--" if progress is None else f"{float(progress):.0f}%"
        eta = human_seconds(job.get("remaining_s"))
        canvas.text(margin, y, percent, FG)
        canvas.text(width - margin - len(eta) * canvas.advance, y, eta, DIM)
    else:
        message = "READY" if online else "NO LINK"
        canvas.text(margin, y, message, colour if online else RED)
        y += row
        if stale:
            # A stale frame is the one thing worth shouting about when idle.
            canvas.text(margin, y, "STALE", AMBER)
        else:
            firmware = connection.get("firmware") or "NO FIRMWARE"
            canvas.text(margin, y, shorten(firmware, canvas.columns), DIM)

    _footer(canvas, online, stale, colour)
    return canvas


def render_temps(state: dict[str, Any], width: int, height: int) -> Canvas:
    """The temperatures big enough to read from across the room.

    The value is coloured by how far it is from its target: dim while the heater is
    still climbing and cool enough, amber once it is within striking distance, green
    when it sits on target. No target from the firmware means no colour at all.
    """
    canvas = Canvas(width, height)
    canvas.clear()
    k = canvas.scale
    margin = 4 * k
    big = k + 1
    step = 12 * big

    _connection, printer, online, _slug, colour = _facts(state)
    y = _header(canvas, "TEMPS", colour, online)
    temps = printer.get("temps") or {}
    value_x = margin + 4 * canvas.advance

    for key, name in (("nozzle", "NOZ"), ("bed", "BED"), ("chamber", "CHM")):
        entry = temps.get(key) or {}
        if key == "chamber" and height < 200:
            continue
        canvas.text(margin, y + 2 * k, name, DIM)
        actual = entry.get("actual")
        target = entry.get("target")
        tint = FG
        if isinstance(actual, (int, float)) and isinstance(target, (int, float)) and target:
            gap = abs(float(actual) - float(target))
            tint = GREEN if gap <= 2 else (AMBER if gap <= 20 else DIM)
        # At 128 px the label leaves 64 px for the value, and "214.6°" at scale 3 is
        # 108: it used to run off the edge. Round to a whole degree there instead of
        # clipping a digit, and pick the largest scale that actually fits.
        if width < 180:
            value = "--" if not isinstance(actual, (int, float)) else f"{float(actual):.0f}"
        else:
            value = _temp(entry)
        scale = big
        while scale > 1 and len(value) * 6 * scale > width - margin - value_x:
            scale -= 1
        canvas.text(value_x, y, value, tint, scale=scale)
        y += step

    _footer(canvas, online, printer.get("stale"), colour)
    return canvas


_cached_ip: list[Any] = [0.0, "NO IP"]


def local_ip() -> str:
    """This Pi's LAN address. Cached: it only changes when the link does."""
    if time.monotonic() - _cached_ip[0] > 60:
        _cached_ip[0] = time.monotonic()
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("192.168.178.1", 53))
            _cached_ip[1] = probe.getsockname()[0]
            probe.close()
        except OSError:
            _cached_ip[1] = "NO IP"
    return str(_cached_ip[1])


def system_info() -> dict[str, str]:
    """The handful of numbers worth a page of their own."""
    info = {"ip": local_ip(), "uptime": "--", "cpu": "--"}
    try:
        info["uptime"] = human_seconds(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    try:
        milli = float(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
        info["cpu"] = f"{milli / 1000:.1f}\u00b0"
    except (OSError, ValueError):
        pass
    return info


def render_system(state: dict[str, Any], width: int, height: int) -> Canvas:
    """Pi and link facts: address, uptime, CPU temperature, printer address."""
    canvas = Canvas(width, height)
    canvas.clear()
    k = canvas.scale
    margin = 4 * k
    row = 9 * k

    connection, printer, online, _slug, colour = _facts(state)
    y = _header(canvas, "SYSTEM", colour, online)
    info = system_info()

    # Label above value: an IPv4 address is 14 characters and will not fit beside a
    # label at this scale, but it does fit on a line of its own.
    small = max(1, k - 1)
    for name, value in (
        ("IP", info["ip"]),
        ("UPTIME", info["uptime"]),
        ("CPU", info["cpu"]),
        ("PRINTER", connection.get("printer_host") or "--"),
    ):
        canvas.text(margin, y, name, DIM, scale=small)
        y += 9 * small
        canvas.text(margin, y, str(value), FG, scale=small)
        y += 11 * small

    _footer(canvas, online, printer.get("stale"), colour)
    return canvas


@dataclass
class ControlState:
    """What the control page carries between frames: cursor, last result, stop prompt."""

    selected: int = 0
    message: str = ""
    message_until: float = 0.0
    confirm_until: float = 0.0

    def say(self, text: str, seconds: float = 4.0) -> None:
        self.message = text
        self.message_until = time.monotonic() + seconds

    def note(self, now: float | None = None) -> str:
        """The one line under the list; a stop confirmation outranks a result."""
        now = time.monotonic() if now is None else now
        if self.confirm_until > now:
            return "PRESS AGAIN TO STOP"
        if self.message and self.message_until > now:
            return self.message
        return ""


def preset_label(key: str) -> str:
    """`PLA 215/60` — the numbers are the point, the word PREHEAT is on the page."""
    name, nozzle, bed = PRESETS[key]
    return f"{name} {nozzle}/{bed}"


def control_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The five rows of the control page, each with its own enable rule.

    Enabled answers "would the firmware take this now". Whether the bridge allows
    writes at all is a separate question, drawn as READ ONLY.
    """
    printer = state.get("printer") or {}
    slug = printer.get("state") or "unknown"
    paused = slug == "paused"
    active = slug in ACTIVE_STATES
    job = bool(printer.get("job")) or active
    light = printer.get("light")
    idle = not (active or job)
    return [
        {
            "id": "resume" if paused else "pause",
            "label": "RESUME" if paused else "PAUSE",
            "short": "RESUME" if paused else "PAUSE",
            "enabled": paused or (active and slug not in ("pausing", "stopping")),
        },
        {
            "id": "light",
            "label": "LIGHT OFF" if light else "LIGHT ON",
            "short": "LIGHT OFF" if light else "LIGHT ON",
            "enabled": isinstance(light, bool),
        },
        {"id": "pla", "label": preset_label("pla"), "short": "PLA", "enabled": idle},
        {"id": "petg", "label": preset_label("petg"), "short": "PETG", "enabled": idle},
        {"id": "stop", "label": "STOP PRINT", "short": "STOP", "enabled": job},
    ]


def render_control(
    state: dict[str, Any], width: int, height: int, *, ui: ControlState | None = None
) -> Canvas:
    """Pause, light, warm-up presets and stop, reachable without a phone.

    The joystick moves the cursor and pressing it runs the row. Stop asks a second
    time, because the one thing a mis-press must not do is throw away a print.
    """
    canvas = Canvas(width, height)
    canvas.clear()
    k = canvas.scale
    margin = 4 * k
    row = 8 * k
    ui = ui or ControlState()

    _connection, printer, online, slug, colour = _facts(state)
    capabilities = state.get("capabilities") or {}
    writable = capabilities.get("control") is True
    actions = control_actions(state)
    selected = ui.selected % len(actions)

    y = _header(canvas, "CONTROL", colour, online)
    if canvas.height >= 200:
        canvas.text(margin, y, shorten(printer.get("state_label") or slug, canvas.columns - 3), DIM)
        if not writable:
            canvas.text(
                canvas.width - margin - 9 * canvas.advance, y, "READ ONLY", RED
            )
        y += row

    narrow = canvas.columns < 11
    for index, action in enumerate(actions):
        usable = writable and action["enabled"]
        if index == selected:
            # The band is the cursor; an arrow as well would cost a character of width.
            canvas.fill_rect(margin - k, y - k, canvas.width - 2 * margin + 2 * k, row - k, LINE)
        canvas.text(
            margin,
            y,
            shorten(action["short"] if narrow else action["label"], canvas.columns),
            FG if usable else DIM,
        )
        # Usable is not only a colour: the header already speaks "filled means the
        # link is there", so the row repeats it as a shape (WCAG 1.4.1).
        mark = max(1, k - 1)
        mark_x = canvas.width - margin - mark
        if usable:
            canvas.disc(mark_x, y + 3 * k, mark, FG)
        else:
            canvas.ring(mark_x, y + 3 * k, mark, DIM, max(1, k // 2))
        y += row

    note = ui.note()
    if not note:
        note = "PRESS RUNS" if writable else "EC_CONTROL IS OFF"
    canvas.text(margin, y, shorten(note, canvas.columns), AMBER if ui.note() else DIM)
    _footer(canvas, online, printer.get("stale"), colour)
    return canvas


def render_service(state: dict[str, Any], width: int, height: int) -> Canvas:
    """Wear and the maintenance reminder, so the machine's age is not a web-only fact."""
    canvas = Canvas(width, height)
    canvas.clear()
    k = canvas.scale
    margin = 4 * k
    row = 9 * k

    _connection, printer, online, _slug, colour = _facts(state)
    usage = state.get("usage") or {}
    maintenance = usage.get("maintenance") or {}
    due = bool(maintenance.get("due"))
    y = _header(canvas, "SERVICE", RED if due else colour, online)

    hours_since = maintenance.get("hours_since")
    total_hours = usage.get("print_hours")
    completed = (usage.get("jobs") or {}).get("completed")
    interval = maintenance.get("interval_hours") or 0

    # A 128 px panel has no room for label and value at full size; drop a step there.
    small = 1 if canvas.width < 180 else None
    step = 6 if small else canvas.advance

    def pair(name: str, value: str, tint: RGB = FG) -> None:
        nonlocal y
        canvas.text(margin, y, name, DIM, scale=small)
        canvas.text(canvas.width - margin - len(value) * step, y, value, tint, scale=small)
        y += row

    pair(
        "SINCE",
        "--" if hours_since is None else f"{float(hours_since):.1f}H",
        AMBER if due else FG,
    )
    pair("TOTAL", "--" if total_hours is None else f"{float(total_hours):.1f}H")
    pair("JOBS DONE", "--" if completed is None else str(completed))
    pair("REMINDER", f"{float(interval):.0f}H" if interval else "OFF")

    # The note is set at `small` scale, so the column count has to follow that scale:
    # `canvas.columns` is measured in full-size glyphs and cut "NEXT AT 50H" to 9.
    note_columns = (canvas.width - 2 * margin) // (6 * (small or k))
    note_scale = small or k
    if due:
        canvas.text(margin, y + k, "SERVICE DUE", RED, scale=note_scale)
    elif interval:
        canvas.text(
            margin,
            y + k,
            shorten(f"NEXT AT {float(interval):.0f}H", note_columns),
            DIM,
            scale=note_scale,
        )
    else:
        canvas.text(margin, y + k, "SET EC_MAINT HOURS", DIM, scale=note_scale)
    _footer(canvas, online, printer.get("stale"), colour)
    return canvas


# --- icons ------------------------------------------------------------------

#: The screens are 240 px wide, so every icon is a stroke drawing scaled to its box.
#: Same language as the web UI (24x24 line art, round caps), just rasterised here.


def _stroke(size: int) -> int:
    """One outline unit at this box size: 3 px at 48, 1 px at 16."""
    return max(1, size // 16)


def icon_printer(canvas: "Canvas", x: int, y: int, size: int, colour: RGB) -> None:
    """An FDM printer from the front: gantry, nozzle, bed."""
    t = _stroke(size)
    left, right = x + 3 * t, x + size - 3 * t
    top, bottom = y + 2 * t, y + size - 2 * t
    canvas.line(left, top, right, top, colour, t)
    canvas.line(left, top, left, bottom - 4 * t, colour, t)
    canvas.line(right, top, right, bottom - 4 * t, colour, t)
    middle = (left + right) // 2
    canvas.line(middle, top, middle, top + size // 3, colour, t)
    canvas.line(middle - 3 * t, top + size // 3, middle + 3 * t, top + size // 3, colour, t)
    canvas.line(left - t, bottom - 4 * t, right + t, bottom - 4 * t, colour, t)
    canvas.line(left, bottom, right, bottom, colour, t)


def icon_clock(canvas: "Canvas", x: int, y: int, size: int, colour: RGB) -> None:
    t = _stroke(size)
    cx, cy = x + size // 2, y + size // 2
    radius = max(4, size // 2 - 2 * t)
    canvas.ring(cx, cy, radius, colour, t)
    canvas.line(cx, cy, cx, cy - radius // 2, colour, t)
    canvas.line(cx, cy, cx + radius // 2, cy, colour, t)


def icon_timer(canvas: "Canvas", x: int, y: int, size: int, colour: RGB) -> None:
    """An hourglass: two caps and an X, which reads at any size."""
    t = _stroke(size)
    left, right = x + 3 * t, x + size - 3 * t
    top, bottom = y + 2 * t, y + size - 2 * t
    canvas.line(left, top, right, top, colour, t)
    canvas.line(left, bottom, right, bottom, colour, t)
    canvas.line(left, top, right, bottom, colour, t)
    canvas.line(right, top, left, bottom, colour, t)


def _cloud(canvas: "Canvas", x: int, y: int, size: int, colour: RGB) -> None:
    """Filled silhouette: at 40 px an outlined cloud turns to mush."""
    t = _stroke(size)
    cx = x + size // 2
    base = y + size - 5 * t
    canvas.disc(cx - size // 5, base, max(2, size // 7), colour)
    canvas.disc(cx + size // 5, base, max(2, size // 8), colour)
    canvas.disc(cx, base - size // 7, max(3, size // 5), colour)
    canvas.fill_rect(x + size // 8, base, size - size // 4, max(2, size // 7), colour)


def icon_sun(canvas: "Canvas", x: int, y: int, size: int, colour: RGB) -> None:
    t = _stroke(size)
    cx, cy = x + size // 2, y + size // 2
    radius = max(3, size // 6)
    canvas.disc(cx, cy, radius, colour)
    reach = size // 2 - 2 * t
    start = radius + 2 * t
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
        canvas.line(
            cx + dx * start, cy + dy * start, cx + dx * reach, cy + dy * reach, colour, t
        )


def icon_cloud(canvas: "Canvas", x: int, y: int, size: int, colour: RGB) -> None:
    _cloud(canvas, x, y, size, colour)


def icon_rain(canvas: "Canvas", x: int, y: int, size: int, colour: RGB) -> None:
    t = _stroke(size)
    _cloud(canvas, x, y, int(size * 0.9), colour)
    base = y + int(size * 0.85)
    for step in range(3):
        drop_x = x + size // 4 + step * size // 4
        canvas.line(drop_x, base, drop_x - 2 * t, base + 4 * t, colour, t)


def icon_snow(canvas: "Canvas", x: int, y: int, size: int, colour: RGB) -> None:
    t = _stroke(size)
    _cloud(canvas, x, y, int(size * 0.9), colour)
    base = y + int(size * 0.88)
    for step in range(3):
        cx = x + size // 4 + step * size // 4
        canvas.line(cx - 2 * t, base, cx + 2 * t, base, colour, t)
        canvas.line(cx, base - 2 * t, cx, base + 2 * t, colour, t)


def icon_fog(canvas: "Canvas", x: int, y: int, size: int, colour: RGB) -> None:
    t = _stroke(size)
    _cloud(canvas, x, y, int(size * 0.9), colour)
    for step in range(2):
        line_y = y + int(size * 0.88) + step * 4 * t
        canvas.line(x + size // 5, line_y, x + size - size // 6, line_y, colour, t)


def icon_storm(canvas: "Canvas", x: int, y: int, size: int, colour: RGB) -> None:
    t = _stroke(size)
    _cloud(canvas, x, y, size, colour)
    cx = x + size // 2
    top = y + size - 6 * t
    canvas.line(cx + 2 * t, top, cx - 2 * t, top + 5 * t, colour, t)
    canvas.line(cx - 2 * t, top + 5 * t, cx + 2 * t, top + 5 * t, colour, t)
    canvas.line(cx + 2 * t, top + 5 * t, cx - t, top + 10 * t, colour, t)


def icon_partly(canvas: "Canvas", x: int, y: int, size: int, colour: RGB) -> None:
    t = _stroke(size)
    small = int(size * 0.55)
    icon_sun(canvas, x, y, small, colour)
    _cloud(canvas, x + size - small - 4 * t, y + size - small + t, small, colour)


GLYPHS = {
    "printer": icon_printer,
    "clock": icon_clock,
    "timer": icon_timer,
    "sun": icon_sun,
    "cloud": icon_cloud,
    "partly": icon_partly,
    "rain": icon_rain,
    "snow": icon_snow,
    "fog": icon_fog,
    "storm": icon_storm,
}


def draw_icon(canvas: "Canvas", name: str, x: int, y: int, size: int, colour: RGB) -> None:
    GLYPHS.get(name, icon_cloud)(canvas, x, y, size, colour)


# --- weather ----------------------------------------------------------------

#: Valkenswaard. Buienradar wants a coordinate pair, not a place name; the public feed
#: has no station here, so the nearest one (Eindhoven, ~12 km) supplies the readings
#: while the rain forecast is already interpolated for these exact coordinates.
DEFAULT_LAT = 51.3506
DEFAULT_LON = 5.4592
WEATHER_FEED = "https://data.buienradar.nl/2.0/feed/json"
RAIN_FEED = "https://gadgets.buienradar.nl/data/raintext/?lat={lat}&lon={lon}"
WEATHER_TTL_S = 600.0

#: mm/h = 10 ** ((code - 109) / 32). 80 is about 0.24 mm/h: the first value worth
#: calling rain, rather than drizzle that never reaches the ground.
RAIN_CODE = 80


@dataclass
class Weather:
    fetched: float = 0.0
    ok: bool = False
    error: str = ""
    station: str = ""
    station_km: float = 0.0
    temp: float | None = None
    feels: float | None = None
    description: str = ""
    icon: str = "cloud"
    humidity: float | None = None
    wind_bft: int | None = None
    wind_dir: str = ""
    min_temp: float | None = None
    max_temp: float | None = None
    rain_chance: int | None = None
    sunrise: str = ""
    sunset: str = ""
    #: 24 five-minute steps of rain intensity code, with the clock time Buienradar gives.
    rain: list[tuple[str, int]] = field(default_factory=list)


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clock_part(stamp: Any) -> str:
    """'2026-10-31T17:12:00' -> '17:12'; anything else -> ''."""
    text = str(stamp or "")
    return text[11:16] if len(text) >= 16 and text[10] == "T" else ""


def _km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def weather_icon(description: str) -> str:
    """Buienradar's icon letters change; its Dutch descriptions are stable."""
    text = (description or "").lower()
    if "onweer" in text:
        return "storm"
    if "sneeuw" in text or "hagel" in text:
        return "snow"
    if any(word in text for word in ("regen", "bui", "motregen")):
        return "rain"
    if "mist" in text or "nevel" in text:
        return "fog"
    if "zonnig" in text or text.startswith("zon") or "held" in text:
        return "sun"
    if any(word in text for word in ("licht", "half", "afwisselend", "opklaring")):
        return "partly"
    return "cloud"


#: What a Buienradar condition looks like in words this panel can draw. The feed's own
#: description is Dutch, which would be the only Dutch on an otherwise English screen.
ICON_LABEL = {
    "sun": "CLEAR",
    "partly": "PARTLY CLOUDY",
    "cloud": "CLOUDY",
    "rain": "RAIN",
    "snow": "SNOW",
    "fog": "FOG",
    "storm": "THUNDER",
}


def rain_summary(weather: Weather) -> str:
    """One line: is it about to rain, is it raining, or is the next two hours dry."""
    codes = [code for _clock, code in weather.rain]
    if not codes:
        return ""
    if codes[0] >= RAIN_CODE:
        dry = next((clock for clock, code in weather.rain if code < RAIN_CODE), "")
        return f"RAIN TIL {dry}" if dry else "RAIN 2H"
    wet = next((clock for clock, code in weather.rain if code >= RAIN_CODE), "")
    return f"RAIN {wet}" if wet else "DRY 2H"


def _fetch_json(url: str, timeout: float = 8.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "elegoo-panel/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def fetch_weather(lat: float, lon: float) -> Weather:
    """One Buienradar round trip, plus the 2-hour rain graph. Never raises."""
    weather = Weather(fetched=time.time())
    try:
        feed = _fetch_json(WEATHER_FEED)
        actual = feed.get("actual") or {}
        stations = actual.get("stationmeasurements") or []
        nearest = None
        if stations:
            nearest = min(
                stations,
                key=lambda s: _km_between(
                    lat, lon, _num(s.get("lat")) or 0.0, _num(s.get("lon")) or 0.0
                ),
            )
        if nearest:
            weather.station = str(nearest.get("stationname") or "").replace(
                "Meetstation ", ""
            ).upper()
            weather.station_km = _km_between(
                lat, lon, _num(nearest.get("lat")) or 0.0, _num(nearest.get("lon")) or 0.0
            )
            weather.temp = _num(nearest.get("temperature"))
            weather.feels = _num(nearest.get("feeltemperature"))
            weather.humidity = _num(nearest.get("humidity"))
            weather.wind_bft = int(_num(nearest.get("windspeedBft")) or 0) or None
            weather.wind_dir = str(nearest.get("winddirection") or "").upper()
            weather.description = str(nearest.get("weatherdescription") or "").upper()
            weather.icon = weather_icon(weather.description)
        weather.sunrise = _clock_part(actual.get("sunrise"))
        weather.sunset = _clock_part(actual.get("sunset"))
        days = (feed.get("forecast") or {}).get("fivedayforecast") or []
        if days:
            today = days[0]
            weather.min_temp = _num(today.get("mintemperature"))
            weather.max_temp = _num(today.get("maxtemperature"))
            weather.rain_chance = _num(today.get("rainChance"))
            weather.rain_chance = int(weather.rain_chance) if weather.rain_chance is not None else None
        weather.ok = True
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as err:
        weather.error = str(err)[:40]

    try:
        text = _fetch_json_text(RAIN_FEED.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"))
        for line in text.splitlines():
            code, _, clock = line.partition("|")
            if clock.strip():
                weather.rain.append((clock.strip(), int(code)))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as err:
        weather.error = weather.error or str(err)[:40]
    return weather


def _fetch_json_text(url: str, timeout: float = 8.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "elegoo-panel/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


_weather = Weather()
_weather_lock = threading.Lock()


def weather_snapshot() -> Weather:
    """A copy, so a render never sees a half-updated Weather."""
    with _weather_lock:
        return Weather(**{**vars(_weather), "rain": list(_weather.rain)})


def _weather_loop(lat: float, lon: float) -> None:
    global _weather
    while True:
        fresh = fetch_weather(lat, lon)
        with _weather_lock:
            if fresh.ok:
                _weather = fresh
            elif not _weather.ok:
                # Nothing to show yet: keep the error so the page can say why.
                _weather = fresh
            else:
                # Keep the last good reading and try again sooner than the TTL.
                _weather.error = fresh.error
        time.sleep(WEATHER_TTL_S if fresh.ok else 120.0)


def start_weather(lat: float, lon: float) -> None:
    thread = threading.Thread(target=_weather_loop, args=(lat, lon), daemon=True)
    thread.name = "panel-weather"
    thread.start()


def weather_coordinates() -> tuple[float, float]:
    """`EC_PANEL_LAT`/`EC_PANEL_LON`, in the shell or in ~/.elegoo-bridge.env."""
    def read(name: str) -> float | None:
        return _num(os.environ.get(name) or env_file_value(name))

    return (
        read("EC_PANEL_LAT") or DEFAULT_LAT,
        read("EC_PANEL_LON") or DEFAULT_LON,
    )


def demo_weather() -> Weather:
    """Deterministic sample for --demo previews: rain starts in twenty minutes."""
    steps: list[tuple[str, int]] = []
    base = time.localtime()
    for index in range(24):
        minute = (base.tm_min // 5 * 5 + index * 5) % 60
        hour = (base.tm_hour + (base.tm_min // 5 * 5 + index * 5) // 60) % 24
        steps.append((f"{hour:02d}:{minute:02d}", 150 if 3 <= index <= 12 else 0))
    return Weather(
        fetched=time.time(),
        ok=True,
        station="EINDHOVEN",
        station_km=11.9,
        temp=6.5,
        feels=5.2,
        description="ZWAAR BEWOLKT",
        icon="cloud",
        humidity=99.0,
        wind_bft=2,
        wind_dir="O",
        min_temp=6.0,
        max_temp=10.0,
        rain_chance=40,
        sunrise="07:41",
        sunset="17:12",
        rain=steps,
    )


# --- timer ------------------------------------------------------------------

TIMER_MIN_S = 60
TIMER_MAX_S = 3 * 3600


@dataclass
class TimerState:
    """A kitchen timer: set it, start it, and it keeps time across screens."""

    seconds: int = 600
    remaining: float = 600.0
    deadline: float | None = None
    finished_at: float = 0.0

    @property
    def running(self) -> bool:
        return self.deadline is not None

    def left(self, now: float | None = None) -> float:
        if self.deadline is not None:
            return max(0.0, self.deadline - (time.monotonic() if now is None else now))
        return max(0.0, self.remaining)

    def toggle(self) -> None:
        if self.deadline is not None:
            self.remaining = self.left()
            self.deadline = None
            return
        if self.remaining <= 0:
            self.remaining = float(self.seconds)
        self.finished_at = 0.0
        self.deadline = time.monotonic() + self.remaining

    def adjust(self, minutes: int) -> None:
        """UP/DOWN while stopped sets the duration; while running it moves the end."""
        delta = minutes * 60
        new_seconds = int(min(TIMER_MAX_S, max(TIMER_MIN_S, self.seconds + delta)))
        if self.deadline is not None:
            self.deadline = max(time.monotonic(), self.deadline + (new_seconds - self.seconds))
        else:
            self.remaining = max(0.0, self.remaining + (new_seconds - self.seconds))
            self.finished_at = 0.0
        self.seconds = new_seconds

    def reset(self) -> None:
        self.deadline = None
        self.remaining = float(self.seconds)
        self.finished_at = 0.0

    def expired(self, now: float | None = None) -> bool:
        """Called from the render loop: a finished timer must not keep ticking."""
        if self.deadline is None:
            return False
        if (time.monotonic() if now is None else now) < self.deadline:
            return False
        self.deadline = None
        self.remaining = 0.0
        self.finished_at = time.monotonic()
        return True


def fmt_duration(total: float) -> str:
    whole = max(0, int(total + 0.5))
    hours, rest = divmod(whole, 3600)
    if hours:
        return f"{hours}:{rest // 60:02d}:{rest % 60:02d}"
    return f"{rest // 60:02d}:{rest % 60:02d}"


#: English, like the rest of the panel and the dashboard. Only Buienradar's numbers are
#: imported; its Dutch descriptions are translated through ICON_LABEL.
DAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
MONTH_NAMES = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def date_line(when: float | None = None) -> str:
    local = time.localtime(when)
    return f"{DAY_NAMES[local.tm_wday]} {local.tm_mday} {MONTH_NAMES[local.tm_mon - 1]}"


def centered(canvas: "Canvas", y: int, value: str, colour: RGB, scale: int) -> None:
    canvas.text((canvas.width - len(value) * 6 * scale) // 2, y, value, colour, scale=scale)


# --- the screens ------------------------------------------------------------

SCREEN_HOME = "home"
SCREEN_ELEGOO = "elegoo"
SCREEN_CLOCK = "clock"
SCREEN_WEATHER = "weather"
SCREEN_TIMER = "timer"

#: The launcher grid, read left-to-right then top-to-bottom. Elegoo first: it is the
#: app you reach for when something is wrong, and home is one press away from it.
APP_ORDER = (SCREEN_ELEGOO, SCREEN_CLOCK, SCREEN_WEATHER, SCREEN_TIMER)
APP_LABEL = {
    SCREEN_ELEGOO: "ELEGOO",
    SCREEN_CLOCK: "CLOCK",
    SCREEN_WEATHER: "WEATHER",
    SCREEN_TIMER: "TIMER",
}


def short_state(state: dict[str, Any]) -> str:
    """Eight characters is the widest a tile holds at scale 2."""
    _connection, printer, online, slug, _colour = _facts(state)
    if not online:
        return "OFFLINE"
    label = (printer.get("state_label") or slug or "--").upper()
    return shorten(label, 8)


def tile_content(
    app: str, nav: "Navigator", state: dict[str, Any], weather: Weather
) -> tuple[str, str, float | None]:
    """Icon, value and progress for one tile.

    Only three things per tile on purpose: at 104x89 px a fourth line has to be set at
    7 px, which is below anyone's idea of readable. The detail lives one press away.
    """
    _connection, printer, online, _slug, _colour = _facts(state)
    if app == SCREEN_ELEGOO:
        job = printer.get("job") or {}
        progress = job.get("progress")
        ratio = None
        value = short_state(state)
        if online and isinstance(progress, (int, float)):
            ratio = max(0.0, min(100.0, float(progress))) / 100.0
            value = f"{float(progress):.0f}%"
        return "printer", value, ratio
    if app == SCREEN_CLOCK:
        return "clock", time.strftime("%H:%M"), None
    if app == SCREEN_WEATHER:
        temp = "--" if weather.temp is None else f"{weather.temp:.1f}\u00b0"
        return weather.icon if weather.ok else "cloud", temp, None
    timer = nav.timer
    if timer.finished_at and time.monotonic() - timer.finished_at < 600:
        return "timer", "00:00", 0.0
    return "timer", fmt_duration(timer.left()), None


def _draw_tile(
    canvas: "Canvas",
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    focused: bool,
    icon: str,
    label: str,
    value: str,
    ratio: float | None,
    tint: RGB,
) -> None:
    """One app tile: icon, name, live value, and a progress bar when there is a job."""
    k = canvas.scale
    # A selectable tile is a component boundary, so it gets the 3:1 line, not the rule one.
    canvas.border(x, y, w, h, FG if focused else LINE_STRONG, k if focused else max(1, k // 2))
    if focused:
        # A second, dimmer frame: the launcher has to say "this one will open".
        canvas.border(x + k, y + k, w - 2 * k, h - 2 * k, LINE, max(1, k // 2))

    if h < 70:
        # At 128 px the name would eat the value; the icon carries the identity.
        label = ""
    label_scale = 2 if w >= 90 else 1
    # Five characters have to fit: 10:27 at scale 3 is 90 px, at scale 2 it is 60.
    value_scale = 3 if w >= 96 else (2 if w >= 66 else 1)
    label_h = 7 * label_scale if label else 0
    value_h = 7 * value_scale
    bar = 3 * k if ratio is not None else 0

    icon_size = max(14, min(w - 4 * k, h // 3))
    stack = icon_size + k + value_h + bar
    if label:
        stack += label_h + k // 2
    top = y + max(2 * k, (h - stack) // 2)

    draw_icon(canvas, icon, x + (w - icon_size) // 2, top, icon_size, tint if focused else DIM)
    top += icon_size + k
    if label:
        canvas.text(x + (w - len(label) * 6 * label_scale) // 2, top, label, DIM, scale=label_scale)
        top += label_h + k // 2
    # Drop a step rather than truncate: OFFLINE is worth more than a bigger number.
    while value_scale > 1 and len(value) * 6 * value_scale > w - 2 * k:
        value_scale -= 1
    value = shorten(value, max(3, (w - 2 * k) // (6 * value_scale)))
    canvas.text(x + (w - len(value) * 6 * value_scale) // 2, top, value, tint, scale=value_scale)
    if ratio is not None and bar:
        bar_y = y + h - 3 * k
        bar_w = w - 4 * k
        canvas.fill_rect(x + 2 * k, bar_y, bar_w, max(2, k), LINE)
        filled = int(bar_w * ratio)
        if filled:
            canvas.fill_rect(x + 2 * k, bar_y, filled, max(2, k), CYAN)


def render_home(
    nav: "Navigator", state: dict[str, Any], weather: Weather, width: int, height: int
) -> Canvas:
    """The launcher: four apps, each showing its own live value."""
    canvas = Canvas(width, height)
    canvas.clear()
    k = canvas.scale
    margin = 4 * k
    _connection, _printer, _online, _slug, colour = _facts(state)
    bridge = _bridge_up(state)
    top = _header(canvas, "HOME", CYAN if bridge else RED, bridge) + k

    gap = 2 * k
    tile_w = (width - 2 * margin - gap) // 2
    tile_h = (height - top - margin - gap) // 2
    for index, app in enumerate(APP_ORDER):
        x = margin + (index % 2) * (tile_w + gap)
        y = top + (index // 2) * (tile_h + gap)
        focused = index == nav.focus
        icon, value, ratio = tile_content(app, nav, state, weather)
        tint = colour if app == SCREEN_ELEGOO else (FG if focused else DIM)
        _draw_tile(
            canvas,
            x,
            y,
            tile_w,
            tile_h,
            focused=focused,
            icon=icon,
            label=APP_LABEL[app],
            value=value,
            ratio=ratio,
            tint=tint,
        )
    return canvas


def _rain_bars(
    canvas: "Canvas", x: int, y: int, w: int, h: int, rain: list[tuple[str, int]]
) -> None:
    """The next two hours as 24 bars; the height is the square root of the intensity.

    Rain code 255 is a downpour, so a linear scale would leave drizzle invisible.
    """
    if not rain:
        canvas.fill_rect(x, y + h - 1, w, 1, LINE)
        return
    gap = 1
    count = min(24, len(rain))
    bar_w = max(1, (w - (count - 1) * gap) // count)
    for index, (_clock, code) in enumerate(rain[:count]):
        bar_x = x + index * (bar_w + gap)
        if code <= 0:
            canvas.fill_rect(bar_x, y + h - 1, bar_w, 1, LINE)
            continue
        bar_h = max(2, int(h * (min(255, code) / 255.0) ** 0.5))
        canvas.fill_rect(bar_x, y + h - bar_h, bar_w, bar_h, CYAN if code >= RAIN_CODE else DIM)


def render_clock(state: dict[str, Any], weather: Weather, width: int, height: int) -> Canvas:
    """The clock, with the two numbers you cannot look up by glancing outside."""
    canvas = Canvas(width, height)
    canvas.clear()
    k = canvas.scale
    margin = 4 * k
    bridge = _bridge_up(state)
    y = _header(canvas, "CLOCK", CYAN if bridge else RED, bridge)
    small = 2 if width >= 200 else 1

    big = 6 if width >= 200 else 4
    centered(canvas, y + 2 * k, time.strftime("%H:%M"), FG, big)
    y += 7 * big + 6 * k
    centered(canvas, y, date_line(), FG if width >= 200 else DIM, 3 if width >= 200 else 2)
    y += 7 * (3 if width >= 200 else 2) + 3 * k
    canvas.fill_rect(margin, y, width - 2 * margin, max(1, k // 2), LINE)
    y += 3 * k

    for label, value in (
        ("SUNRISE", weather.sunrise or "--:--"),
        ("SUNSET", weather.sunset or "--:--"),
    ):
        canvas.text(margin, y, label, DIM, scale=small)
        canvas.text(width - margin - len(value) * 6 * small, y, value, FG, scale=small)
        y += 9 * small
    week = time.strftime("%V")
    canvas.text(margin, y, "WEEK", DIM, scale=small)
    canvas.text(width - margin - len(week) * 6 * small, y, week, FG, scale=small)
    y += 9 * small
    summary = rain_summary(weather)
    if summary:
        # 14 characters at scale 2 is 168 px; shorter than a 240 px panel, no clipping.
        scale = 2 if width >= 200 else 1
        canvas.text(margin, y, shorten(summary, (width - 2 * margin) // (6 * scale)), AMBER if "DROOG" not in summary else DIM, scale=scale)
    return canvas


def render_weather(state: dict[str, Any], weather: Weather, width: int, height: int) -> Canvas:
    """Now, today, and the next two hours of rain for one fixed place."""
    canvas = Canvas(width, height)
    canvas.clear()
    k = canvas.scale
    margin = 4 * k
    bridge = _bridge_up(state)
    y = _header(canvas, "WEATHER", CYAN if bridge else RED, bridge)
    small = 2 if width >= 200 else 1

    station = weather.station or "NO DATA"
    canvas.text(margin, y, shorten(station, 12), DIM, scale=small)
    if weather.station:
        distance = f"{weather.station_km:.0f}KM"
        canvas.text(width - margin - len(distance) * 6 * small, y, distance, DIM, scale=small)
    y += 9 * small

    icon_size = max(20, int(height * 0.24))
    draw_icon(canvas, weather.icon if weather.ok else "cloud", margin, y, icon_size, FG if weather.ok else DIM)

    value_scale = 4 if width >= 200 else 2
    text_x = margin + icon_size + 2 * k
    canvas.text(text_x, y, "--" if weather.temp is None else f"{weather.temp:.1f}\u00b0", FG, scale=value_scale)
    description_y = y + 7 * value_scale + 2 * k
    condition = ICON_LABEL.get(weather.icon, "") if weather.ok else "NO DATA"
    for line in wrap(condition, max(6, (width - text_x - margin) // (6 * small)), 2):
        canvas.text(text_x, description_y, line, FG, scale=small)
        description_y += 9 * small

    y = max(y + icon_size, description_y) + 3 * k
    canvas.fill_rect(margin, y, width - 2 * margin, max(1, k // 2), LINE)
    y += 3 * k
    low = "--" if weather.min_temp is None else f"{weather.min_temp:.0f}"
    high = "--" if weather.max_temp is None else f"{weather.max_temp:.0f}"
    canvas.text(margin, y, f"MIN {low} MAX {high}", FG, scale=small)
    if weather.rain_chance is not None:
        chance = f"{weather.rain_chance}%"
        canvas.text(width - margin - len(chance) * 6 * small, y, chance, DIM, scale=small)
    y += 9 * small

    summary = rain_summary(weather)
    if summary:
        canvas.text(margin, y, summary, AMBER if "DROOG" not in summary else DIM, scale=small)
        y += 9 * small

    _rain_bars(canvas, margin, y, width - 2 * margin, 4 * k, weather.rain)
    y += 4 * k + 2 * k
    if weather.rain and height >= 200:
        # The window the bars cover; on a 128 px panel there is no room for it.
        first, last = weather.rain[0][0], weather.rain[-1][0]
        canvas.text(margin, y, f"NOW {first}", DIM, scale=small)
        canvas.text(width - margin - len(last) * 6 * small, y, last, DIM, scale=small)
        y += 9 * small
    if weather.wind_bft or weather.wind_dir:
        # At 128 px one step down fits the whole line instead of "WIND 2 BF".
        wind_scale = small if height >= 200 else 1
        canvas.text(
            margin,
            y,
            shorten(
                f"WIND {weather.wind_bft or '-'} BFT {weather.wind_dir}",
                (width - 2 * margin) // (6 * wind_scale),
            ),
            DIM,
            scale=wind_scale,
        )
    return canvas


def render_timer(nav: "Navigator", state: dict[str, Any], width: int, height: int) -> Canvas:
    """A kitchen timer that keeps running while you watch the printer."""
    canvas = Canvas(width, height)
    canvas.clear()
    k = canvas.scale
    margin = 4 * k
    bridge = _bridge_up(state)
    timer = nav.timer
    y = _header(canvas, "TIMER", CYAN if bridge else RED, bridge)
    small = 2 if width >= 200 else 1
    just_finished = bool(timer.finished_at) and time.monotonic() - timer.finished_at < 600

    tint = AMBER if just_finished else (CYAN if timer.running else FG)
    big = 6 if width >= 200 else 4
    centered(canvas, y + 2 * k, fmt_duration(timer.left()), tint, big)
    y += 7 * big + 6 * k

    if just_finished:
        status = "TIME UP"
    elif timer.running:
        status = "RUNNING"
    elif timer.remaining < timer.seconds:
        status = "PAUSED"
    else:
        status = f"SET {timer.seconds // 60} MIN"
    centered(canvas, y, status, AMBER if just_finished else DIM, 3 if width >= 200 else 2)
    y += 7 * (3 if width >= 200 else 2) + 3 * k

    bar_h = max(2, 2 * k)
    bar_w = width - 2 * margin
    ratio = 0.0 if not timer.seconds else max(0.0, min(1.0, 1 - timer.left() / timer.seconds))
    canvas.fill_rect(margin, y, bar_w, bar_h, LINE)
    if ratio > 0:
        canvas.fill_rect(margin, y, max(1, int(bar_w * ratio)), bar_h, CYAN)
    y += bar_h + 4 * k

    #: At 128 px three legend rows leave no room for the call to action, so the two
    #: reset keys share the last row there; the joystick press gets its own line.
    legend = (("UP/DOWN", "1 MIN"), ("KEY1", "RESET"), ("KEY2", "+5 MIN"))
    if small == 1:
        legend = (("UP/DOWN", "1 MIN"), ("KEY1 RST", "KEY2 +5"))
    for label, value in legend:
        canvas.text(margin, y, label, DIM, scale=small)
        canvas.text(width - margin - len(value) * 6 * small, y, value, FG, scale=small)
        y += 9 * small
    y += k
    if y + 7 * small <= height:
        centered(canvas, y, "PRESS PAUSE" if timer.running else "PRESS START", DIM, small)
    return canvas


def post_json(
    opener: urllib.request.OpenerDirector, url: str, path: str, payload: dict[str, Any] | None = None
) -> tuple[bool, str]:
    """One write to the bridge; the message is short enough for the panel."""
    request = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=8):
            return True, "SENT"
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            detail = str(json.loads(err.read() or b"{}").get("detail") or "")
        except (ValueError, OSError):
            detail = ""
        return False, shorten(detail or f"HTTP {err.code}", 20)
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, "NO LINK"


def perform_action(
    opener: urllib.request.OpenerDirector, url: str, state: dict[str, Any], action_id: str
) -> tuple[bool, str]:
    """Map one control row onto the bridge's write routes."""
    printer = state.get("printer") or {}
    if action_id in ("pause", "resume", "stop"):
        return post_json(opener, url, f"/api/print/{action_id}")
    if action_id == "light":
        return post_json(opener, url, "/api/print/light", {"on": not bool(printer.get("light"))})
    preset = PRESETS.get(action_id)
    if preset is not None:
        _name, nozzle, bed = preset
        return post_json(opener, url, "/api/print/temps", {"nozzle": nozzle, "bed": bed})
    return False, "UNKNOWN"


# --- navigation -------------------------------------------------------------

#: The three apps you page through with left/right, in the order they sit on home.
PAGED_APPS = (SCREEN_CLOCK, SCREEN_WEATHER, SCREEN_TIMER)


@dataclass
class Navigator:
    """Where we are and what the last press meant. One object, passed to render."""

    screen: str = SCREEN_HOME
    #: Index into PAGES; 0 is the status tab (PAGE_STATUS is defined below this class).
    tab: int = 0
    focus: int = 0
    ui: ControlState = field(default_factory=ControlState)
    timer: TimerState = field(default_factory=TimerState)


def _elegoo_event(
    nav: Navigator,
    action: str,
    state: dict[str, Any],
    opener: urllib.request.OpenerDirector | None,
    url: str,
) -> bool:
    """The printer app keeps the cursor, the stop confirmation and the tab order it had."""
    if nav.tab == PAGE_CONTROL:
        actions = control_actions(state)
        if action in ("left", "up"):
            nav.ui.selected = (nav.ui.selected - 1) % len(actions)
            return True
        if action in ("right", "down"):
            nav.ui.selected = (nav.ui.selected + 1) % len(actions)
            return True
        if action in ("key1", "key2"):
            nav.tab = (nav.tab + (-1 if action == "key1" else 1)) % len(PAGES)
            return True
        if action != "press":
            return False

        chosen = actions[nav.ui.selected % len(actions)]
        writable = (state.get("capabilities") or {}).get("control") is True
        if not writable:
            nav.ui.say("READ ONLY")
            return True
        if not chosen["enabled"]:
            nav.ui.say("NOT NOW")
            return True
        if chosen["id"] == "stop" and nav.ui.confirm_until <= time.monotonic():
            nav.ui.message = ""
            nav.ui.confirm_until = time.monotonic() + 4.0
            return True
        if opener is None:
            nav.ui.say("DEMO")
            return True
        nav.ui.confirm_until = 0.0
        # Say something before the round trip: an 8 s timeout with a frozen screen reads
        # as a dead button (NN/g: >1 s needs visible proof that work is happening).
        nav.ui.say("SENDING", 8.0)
        _ok, message = perform_action(opener, url, state, chosen["id"])
        nav.ui.say(message, 5.0)
        return True

    if action in ("right", "down", "key2"):
        nav.tab = (nav.tab + 1) % len(PAGES)
        return True
    if action in ("left", "up", "key1"):
        nav.tab = (nav.tab - 1) % len(PAGES)
        return True
    if action == "press":
        nav.tab = PAGE_STATUS
        return True
    return False


def apply_event(
    nav: Navigator,
    action: str,
    down: bool,
    state: dict[str, Any],
    opener: urllib.request.OpenerDirector | None,
    url: str,
    held: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """One button edge, wherever it lands. Returns True when the screen changed."""
    if not down:
        return False

    # KEY3 and the joystick together is "home", from any app and any tab.
    if action == "press" and "key3" in held:
        if nav.screen == SCREEN_HOME:
            return False
        nav.screen = SCREEN_HOME
        return True

    if nav.screen == SCREEN_HOME:
        if action in ("left", "right", "key1", "key2"):
            nav.focus ^= 1
            return True
        if action in ("up", "down"):
            nav.focus ^= 2
            return True
        if action == "press":
            nav.screen = APP_ORDER[nav.focus % len(APP_ORDER)]
            return True
        return False

    # KEY3 alone leaves an app too: the tile remembers the tab you were on.
    if action == "key3":
        nav.screen = SCREEN_HOME
        return True

    if nav.screen == SCREEN_ELEGOO:
        return _elegoo_event(nav, action, state, opener, url)

    if nav.screen == SCREEN_TIMER:
        if action in ("up", "down"):
            nav.timer.adjust(1 if action == "up" else -1)
            return True
        if action == "press":
            nav.timer.toggle()
            return True
        if action == "key1":
            nav.timer.reset()
            return True
        if action == "key2":
            nav.timer.adjust(5)
            return True

    step = 1 if action in ("right", "down", "key2") else -1
    if action in ("left", "right", "up", "down", "key1", "key2"):
        try:
            index = PAGED_APPS.index(nav.screen)
        except ValueError:
            index = 0
        nav.screen = PAGED_APPS[(index + step) % len(PAGED_APPS)]
        return True
    if action == "press":
        nav.screen = SCREEN_HOME
        return True
    return False


def wrap(text: str, columns: int, limit: int = 3) -> list[str]:
    """Greedy word wrap, because a printer error has no respect for the panel width."""
    lines: list[str] = []
    current = ""
    for word in (text or "").upper().split():
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= columns:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:limit]


#: Pages you can reach with the buttons. -1 is the offline page, shown on its own.
PAGES = ("status", "temps", "control", "service", "system")
PAGE_STATUS, PAGE_TEMPS, PAGE_CONTROL, PAGE_SERVICE, PAGE_SYSTEM = range(len(PAGES))
OFFLINE_PAGE = -1

#: Firmware states in which a job occupies the machine; the presets refuse to fight it.
ACTIVE_STATES = frozenset(
    {
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
)

#: Nozzle and bed targets behind one press. The two filaments almost everybody has.
PRESETS: dict[str, tuple[str, int, int]] = {
    "pla": ("PLA", 215, 60),
    "petg": ("PETG", 240, 80),
}


def render_offline(state: dict[str, Any], width: int, height: int) -> Canvas:
    """A printer that is switched off gets its own screen instead of a broken one.

    The status page would show a red dot and `NO LINK` in the corner; this says it
    out loud, plus which printer is being waited for, since when, and what the
    bridge last complained about.
    """
    canvas = Canvas(width, height)
    canvas.clear()
    k = canvas.scale
    margin = 4 * k
    small = max(1, k - 1)
    columns = (width - 2 * margin) // (6 * small)

    connection = state.get("connection") or {}
    printer = state.get("printer") or {}
    host = connection.get("printer_host")
    y = _header(canvas, "NO BRIDGE" if not host else "OFFLINE", RED, False)

    # "NO LINK" at scale 3 is 126 px of a 128 px panel: it needs the width check, or
    # the "K" lands half outside the glass.
    headline = "NO LINK"
    headline_scale = k + 1
    while headline_scale > 1 and len(headline) * 6 * headline_scale > width - 2 * margin:
        headline_scale -= 1
    canvas.text(margin, y + 2 * k, headline, RED, scale=headline_scale)
    y += 7 * headline_scale + 7 * k

    if host:
        canvas.text(margin, y, "PRINTER", DIM, scale=small)
        y += 9 * small
        canvas.text(margin, y, shorten(str(host), columns), FG, scale=small)
        y += 11 * small

    since = connection.get("since_ts")
    if isinstance(since, (int, float)) and since:
        canvas.text(margin, y, "SINCE", DIM, scale=small)
        canvas.text(
            margin + 6 * 6 * small,
            y,
            time.strftime("%H:%M", time.localtime(since)),
            FG,
            scale=small,
        )
        y += 12 * small

    error = (connection.get("last_error") or "").strip()
    for line in wrap(error, columns):
        if y > height - 11 * k:
            break
        canvas.text(margin, y, line, AMBER, scale=small)
        y += 10 * small

    canvas.text(margin, height - 14 * k, "STILL TRYING", DIM, scale=small)
    _footer(canvas, False, bool(printer.get("stale")), RED, status=False)
    return canvas


def _tab_badge(canvas: Canvas, tab: int) -> None:
    """Five dots in the header, the current one filled: "you are here, 2 of 5".

    Dots rather than "2/5" because the header title is the printer state, and "PRINTING
    2/5" does not fit at 240 px without truncating the word that matters. Which dot is
    filled is a position, not a colour: it reads without colour vision.
    """
    #: Below 180 px the five dots are 3 px specks that overlap the page title, and the
    #: title already names the page. Nothing is lost by leaving them out there.
    if canvas.width < 180:
        return
    k = canvas.scale
    radius = max(1, k - 1)
    step = 2 * radius + max(2, k)
    total = len(PAGES) * 2 * radius + (len(PAGES) - 1) * max(2, k)
    x = canvas.width - 9 * k - total + radius
    y = 3 * k + 2 * k
    for index in range(len(PAGES)):
        if index == tab:
            canvas.disc(x, y, radius, FG)
        else:
            canvas.ring(x, y, radius, LINE_STRONG, 1)
        x += step


def render(
    nav: Navigator, state: dict[str, Any], weather: Weather, width: int, height: int
) -> Canvas:
    """Every screen goes through here, so the rotation in main() covers all of them."""
    if nav.screen == SCREEN_ELEGOO:
        if (state.get("connection") or {}).get("state") != "online":
            # The printer app is the only screen a missing link can take over; home
            # keeps its clock, weather and timer, which is the point of a home screen.
            return render_offline(state, width, height)
        if nav.tab == PAGE_TEMPS:
            canvas = render_temps(state, width, height)
        elif nav.tab == PAGE_CONTROL:
            canvas = render_control(state, width, height, ui=nav.ui)
        elif nav.tab == PAGE_SERVICE:
            canvas = render_service(state, width, height)
        elif nav.tab == PAGE_SYSTEM:
            canvas = render_system(state, width, height)
        else:
            canvas = render_status(state, width, height)
        _tab_badge(canvas, nav.tab)
        return canvas
    if nav.screen == SCREEN_CLOCK:
        return render_clock(state, weather, width, height)
    if nav.screen == SCREEN_WEATHER:
        return render_weather(state, weather, width, height)
    if nav.screen == SCREEN_TIMER:
        return render_timer(nav, state, width, height)
    return render_home(nav, state, weather, width, height)


def demo_state() -> dict[str, Any]:
    now = time.time()
    return {
        "ts": now,
        "connection": {
            "state": "online",
            "printer_host": "192.168.178.129",
            "last_frame_age_s": 2.4,
            "since_ts": now - 300,
        },
        "capabilities": {"control": True, "camera": True, "files": True, "upload": True},
        "usage": {
            "print_hours": 48.2,
            "today_seconds": 5400,
            "jobs": {"started": 9, "completed": 7, "failed": 1, "stopped": 1},
            "maintenance": {
                "interval_hours": 50.0,
                "hours_since": 46.4,
                "due": False,
                "last_reset_ts": now - 86400 * 12,
            },
        },
        "printer": {
            "state": "printing",
            "state_label": "Printing",
            "stale": False,
            "light": False,
            "temps": {
                "nozzle": {"actual": 214.6, "target": 215.0},
                "bed": {"actual": 60.1, "target": 60.0},
                "chamber": {"actual": 31.4, "target": 0.0},
            },
            "job": {
                "filename": "benchy_v2.gcode",
                "progress": 42.0,
                "elapsed_s": 3120,
                "remaining_s": 4380,
            },
        },
    }


# --- talking to the bridge --------------------------------------------------


def env_file_value(name: str) -> str | None:
    """Same source as doctor.py: cron starts us without a shell environment."""
    for candidate in (Path.home() / ".elegoo-bridge.env", Path("/etc/elegoo-bridge.env")):
        try:
            for line in candidate.read_text().splitlines():
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return None


def password_from_env_file() -> str | None:
    return env_file_value("EC_AUTH_PASSWORD")


def default_rotate() -> int:
    """Shell first, then the shared env file, so the launcher need not pass it."""
    raw = os.environ.get("EC_PANEL_ROTATE") or env_file_value("EC_PANEL_ROTATE") or "0"
    try:
        value = int(str(raw).strip())
    except ValueError:
        return 0
    return value if value in (0, 90, 180, 270) else 0


class AuthError(RuntimeError):
    """The bridge wants a password and did not accept the one we had.

    Worth stopping for: a panel started by the wrong user (root cannot read
    ~/.elegoo-bridge.env) would otherwise draw "offline" forever, which looks
    exactly like a broken printer link.
    """


def make_opener(url: str, password: str | None) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    if password:
        payload = json.dumps({"password": password}).encode()
        request = urllib.request.Request(
            f"{url.rstrip('/')}/api/login",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            opener.open(request, timeout=8).close()
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise AuthError(f"login refused ({err.code})") from err
            sys.stderr.write(f"panel: login failed ({err.code})\n")
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            # At boot the bridge may still be starting. Drawing the home screen while we
            # wait beats exiting with a traceback and staying dark until the next cron.
            sys.stderr.write(f"panel: bridge not answering yet ({err}); will retry the login\n")
    return opener


def read_state(opener: urllib.request.OpenerDirector, url: str) -> dict[str, Any] | None:
    try:
        with opener.open(f"{url.rstrip('/')}/api/state", timeout=6) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        # HTTPError is a URLError, so this has to come first.
        if err.code in (401, 403):
            raise AuthError(f"state refused ({err.code})") from err
        return None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def offline_state(error: str = "bridge unreachable") -> dict[str, Any]:
    return {
        "connection": {"state": "offline", "last_error": error},
        "printer": {"state": "unknown", "state_label": "Offline", "temps": {}},
    }


# --- framebuffer ------------------------------------------------------------


def detect_size(fb_path: str) -> tuple[int, int] | None:
    """Ask the kernel how big the panel is instead of trusting a flag."""
    base = os.path.basename(fb_path)
    try:
        with open(f"/sys/class/graphics/{base}/virtual_size") as handle:
            width, height = handle.read().strip().split(",")
        return int(width), int(height)
    except (OSError, ValueError):
        return None


def blit(path: str, canvas: Canvas) -> bool:
    """Copy a finished frame onto the kernel framebuffer.

    The frame is exactly the size the kernel reports: fbtft sets the controller's
    address window itself, so there is nothing to offset. Rotation is *not* left to
    the driver either — ``rotate=90`` in the overlay sets the ST7789's MADCTL to
    MV|MY, which swaps the address axes of a 240x320 GRAM and shifts a 240x240
    picture, leaving a band of stale memory on screen. We draw the rotation here.
    """
    try:
        with open(path, "r+b", buffering=0) as device:
            device.seek(0)
            device.write(canvas.buf)
        return True
    except OSError as err:
        sys.stderr.write(f"panel: cannot write {path}: {err}\n")
        return False


#: The HAT's inputs, by BCM number (see docs/PANEL.md for the full pinout).
BUTTONS = {
    "up": 6,
    "down": 19,
    "left": 5,
    "right": 26,
    "press": 13,
    "key1": 21,
    "key2": 20,
    "key3": 16,
}


class Input:
    """Joystick and keys, read as falling edges on the GPIO character device.

    Needs libgpiod's python bindings, which live in the *system* python and not in
    the bridge's virtualenv. This script imports nothing outside the standard
    library, so panel.sh runs it with /usr/bin/python3 and gets them.
    """

    def __init__(self, chip: str = "/dev/gpiochip0", debounce_ms: int = 40) -> None:
        from datetime import timedelta

        import gpiod  # noqa: PLC0415 - optional, so the laptop can still render previews

        settings = gpiod.LineSettings(
            direction=gpiod.line.Direction.INPUT,
            # Both edges: KEY3 has to be known as *held* while the joystick is pressed,
            # which is how you get home from an app.
            edge_detection=gpiod.line.Edge.BOTH,
            bias=gpiod.line.Bias.PULL_UP,
            debounce_period=timedelta(milliseconds=debounce_ms),
        )
        self._names = {offset: name for name, offset in BUTTONS.items()}
        self.held: set[str] = set()
        self._gpiod = gpiod
        self._request = gpiod.request_lines(
            chip,
            consumer="elegoo-panel",
            config={tuple(BUTTONS.values()): settings},
        )

    def events(self, timeout: float = 0.0) -> list[tuple[str, bool]]:
        """(button, pressed) pairs, in the order the edges happened."""
        try:
            if not self._request.wait_edge_events(timeout):
                return []
            raw = self._request.read_edge_events()
        except OSError:
            return []
        out: list[tuple[str, bool]] = []
        for event in raw:
            name = self._names.get(event.line_offset)
            if name is None:
                continue
            # python-libgpiod names this EdgeEvent.Type, not EdgeEventType.
            down = event.event_type == self._gpiod.EdgeEvent.Type.FALLING_EDGE
            if down:
                self.held.add(name)
            else:
                self.held.discard(name)
            out.append((name, down))
        return out

    def close(self) -> None:
        try:
            self._request.release()
        except Exception:  # noqa: BLE001 - releasing is best effort at shutdown
            pass


def claim_pidfile(path: str | None) -> bool:
    """One panel per panel: two writers would take turns tearing the display."""
    if not path:
        return True
    target = Path(path)
    try:
        previous = int(target.read_text().strip())
    except (OSError, ValueError):
        previous = None
    if previous and previous != os.getpid():
        try:
            os.kill(previous, 0)
        except OSError:
            pass
        else:
            sys.stderr.write(f"panel: already running as pid {previous}\n")
            return False
    target.write_text(f"{os.getpid()}\n")
    return True


def release_pidfile(path: str | None) -> None:
    if not path:
        return
    target = Path(path)
    try:
        if target.read_text().strip() == str(os.getpid()):
            target.unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Printer status on a Waveshare SPI LCD HAT.")
    parser.add_argument("--url", default=os.environ.get("EC_PANEL_URL", "http://127.0.0.1:8787"))
    parser.add_argument(
        "--password",
        default=os.environ.get("EC_AUTH_PASSWORD") or password_from_env_file(),
        help="dashboard password (or set EC_AUTH_PASSWORD, or leave it in ~/.elegoo-bridge.env)",
    )
    parser.add_argument("--fb", help="framebuffer device, e.g. /dev/fb1")
    parser.add_argument("--preview", help="write a single PNG instead of driving a panel")
    parser.add_argument(
        "--size",
        default="auto",
        help="panel size ('auto' reads it from the framebuffer), e.g. 128x128 or 240x240",
    )
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--frames", type=int, default=0, help="stop after N frames (0 = forever)")
    parser.add_argument(
        "--rotate",
        type=int,
        default=default_rotate(),
        choices=(0, 90, 180, 270),
        help="turn the picture clockwise; the overlay must use rotate=0",
    )
    parser.add_argument("--demo", action="store_true", help="render sample data, no bridge needed")
    parser.add_argument(
        "--page",
        default=SCREEN_HOME,
        choices=(SCREEN_HOME, *PAGES, SCREEN_CLOCK, SCREEN_WEATHER, SCREEN_TIMER, "offline"),
        help="start on this screen: home (the launcher), an elegoo tab, clock, weather, timer",
    )
    parser.add_argument("--pidfile", help="refuse to start twice, and let a launcher find us")
    args = parser.parse_args(argv)
    nav = Navigator()
    if args.page in PAGES:
        nav.screen, nav.tab = SCREEN_ELEGOO, PAGES.index(args.page)
    elif args.page in (SCREEN_CLOCK, SCREEN_WEATHER, SCREEN_TIMER):
        nav.screen = args.page
    elif args.page == "offline":
        # Previewed with an offline state below; the offline page is not navigable.
        nav.screen = SCREEN_ELEGOO

    fb_size = detect_size(args.fb) if args.fb else None
    if args.size == "auto":
        if fb_size is None:
            width, height = (128, 128)
        elif fb_size[1] > fb_size[0]:
            # A 240x320 controller driving a 240x240 panel: draw a square picture.
            width, height = (fb_size[0], fb_size[0])
        else:
            width, height = fb_size
    else:
        try:
            width, height = (int(part) for part in args.size.lower().split("x"))
        except ValueError:
            parser.error("--size must look like 128x128, or be 'auto'")

    if args.preview and not args.fb:
        if args.demo:
            state, weather = demo_state(), demo_weather()
        elif args.page == "offline":
            state, weather = offline_state(), Weather()
        else:
            try:
                opener = make_opener(args.url, args.password)
                state = read_state(opener, args.url) or offline_state()
            except AuthError as err:
                sys.stderr.write(f"panel: {err} - no password, or the wrong user's env file\n")
                return 4
            weather = fetch_weather(*weather_coordinates())
        canvas = render(nav, state, weather, width, height).rotated(args.rotate)
        canvas.write_png(args.preview)
        print(f"wrote {args.preview} ({width}x{height}, scale {canvas.scale}, page {args.page})")
        return 0

    if not args.fb:
        parser.error("give --fb /dev/fbN to drive the panel, or --preview for a PNG")

    if not claim_pidfile(args.pidfile):
        return 3
    sys.stderr.write(
        f"panel: fb={args.fb} {width}x{height} rotate={args.rotate} interval={args.interval}s\n"
    )

    try:
        opener = None if args.demo else make_opener(args.url, args.password)
    except AuthError as err:
        sys.stderr.write(f"panel: {err} - no password, or the wrong user's env file\n")
        return 4
    inputs = None
    try:
        inputs = Input()
    except Exception as err:  # noqa: BLE001 - no gpiod, or the pins belong to someone else
        sys.stderr.write(f"panel: buttons unavailable ({err}); showing the display only\n")

    # The weather is fetched off the render path: Buienradar can be slow, and a panel
    # that freezes on a slow HTTPS round trip looks broken.
    start_weather(*weather_coordinates())

    state = demo_state() if opener is None else offline_state("no frame yet")
    weather = weather_snapshot()
    dirty = True
    last_fetch = 0.0
    last_weather = weather.fetched
    last_tick = ""
    last_repeat = 0.0
    needs_login = False
    drawn = 0
    interval = max(0.2, args.interval)
    try:
        while True:
            now = time.monotonic()

            # Buttons are edge-triggered, so this costs nothing while nobody presses.
            if inputs is not None:
                for action, down in inputs.events():
                    apply_event(nav, action, down, state, opener, args.url, inputs.held)
                    if down and action in ("up", "down"):
                        # A held key waits a beat before it starts repeating.
                        last_repeat = now
                    dirty = True
                # Holding up/down on the timer spins it, like a real kitchen timer.
                if nav.screen == SCREEN_TIMER:
                    direction = 1 if "up" in inputs.held else (-1 if "down" in inputs.held else 0)
                    if direction and now - last_repeat > 0.40:
                        nav.timer.adjust(direction)
                        last_repeat = now
                        dirty = True

            if nav.timer.expired():
                dirty = True

            # A result and a stop prompt are both only true for a few seconds.
            if nav.ui.message and now > nav.ui.message_until:
                nav.ui.message = ""
                dirty = True
            if nav.ui.confirm_until and now > nav.ui.confirm_until:
                nav.ui.confirm_until = 0.0
                dirty = True

            if needs_login and opener is not None:
                # The bridge came up after us, or restarted and dropped our session.
                try:
                    opener = make_opener(args.url, args.password)
                    needs_login = False
                except AuthError as err:
                    sys.stderr.write(f"panel: {err} - restart it as the right user\n")
                    return 4
                except (urllib.error.URLError, TimeoutError, OSError):
                    pass

            if now - last_fetch >= interval:
                try:
                    state = demo_state() if opener is None else (read_state(opener, args.url) or offline_state())
                except AuthError:
                    # Keep the screen lit and try the login again on the next frame.
                    needs_login = True
                    state = offline_state("login required")
                last_fetch = now
                dirty = True

            fresh = weather_snapshot()
            if fresh.fetched != last_weather:
                last_weather = fresh.fetched
                weather = fresh
                dirty = True

            # A clock that only redraws when the printer talks is not a clock.
            tick = str(int(time.time())) if nav.screen in (SCREEN_HOME, SCREEN_CLOCK, SCREEN_TIMER) else time.strftime("%H:%M")
            if tick != last_tick:
                last_tick = tick
                dirty = True

            if dirty:
                if not blit(args.fb, render(nav, state, weather, width, height).rotated(args.rotate)):
                    return 1
                dirty = False
                drawn += 1
                if args.frames and drawn >= args.frames:
                    return 0
            time.sleep(0.05)
    except KeyboardInterrupt:
        return 0
    finally:
        if inputs is not None:
            inputs.close()
        release_pidfile(args.pidfile)


if __name__ == "__main__":
    raise SystemExit(main())
