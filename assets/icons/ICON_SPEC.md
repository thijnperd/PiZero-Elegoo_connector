# Icon spec — 1.44"–1.8" TFT status UI

The set is sourced from **[Tabler Icons](https://tabler.io/icons)** (MIT, Paweł Kuna),
not drawn by hand. 43 glyphs: 38 required, 5 optional — a status row, an action set, and a
set of app tiles.

Tabler is the right fit because it is already built on a **24×24 grid with a 2 px stroke**,
which is exactly the geometry this UI wants: at 24 px the stroke lands on whole pixels with
no conversion at all, and at 16 px the same 1.33 px stroke resolves cleanly to a 1 px hairline.

## 1. Hard constraints

| Item | Value |
|---|---|
| Panel width | **128 px** (16/24 px icons work on both 128×128 and 128×160) |
| Canvas sizes | **16×16**, **24×24**, and one **32×32** boot mark |
| Colour | **1-bit.** Rendered white on transparent, thresholded to on/off |
| Inner detail | Tabler **outline** style only — no filled/hybrid mixes in one set |

Each icon becomes a 1-bit mask and is tinted at draw time (white = active, `#5A6172` =
inactive). One file, two visual states, 32 bytes at 16 px, 72 bytes at 24 px, 128 at 32 px.

## 2. The set and its Tabler source

### 16×16 — status row

| Our name | Tabler icon | Required |
|---|---|---|
| `clock` | `clock` | yes |
| `link` | `wifi` | yes |
| `firmware` | `cpu` | yes |
| `offline` | `wifi-off` | yes |
| `battery` | `battery` | optional |
| `sd` | `device-sd-card` | optional |
| `usb` | `usb` | optional |
| `lock` | `lock` | optional |

### 24×24 — menu / actions

| Our name | Tabler icon | | Our name | Tabler icon |
|---|---|---|---|---|
| `settings` | `settings` | | `check` | `check` |
| `menu` | `menu-2` | | `warn` | `alert-triangle` |
| `back` | `arrow-left` | | `info` | `info-circle` |
| `up` | `arrow-up` | | `power` | `power` |
| `down` | `arrow-down` | | `search` | `search` |
| `enter` | `corner-down-left` | | `save` | `device-floppy` |
| `refresh` | `refresh` | | `cross` | `x` |

### 24×24 — app tiles

| Our name | Tabler icon | | Our name | Tabler icon |
|---|---|---|---|---|
| `weather` | `sun` | | `clock` | `clock` |
| `cloud` | `cloud` | | `printer` | `printer` |
| `rain` | `cloud-rain` | | `terminal` | `terminal-2` |
| `snow` | `cloud-snow` | | `music` | `music` |
| `storm` | `cloud-storm` | | `folder` | `folder` |
| `fog` | `cloud-fog` | | `download` | `download` |
| `moon` | `moon` | | `upload` | `upload` |
| `temperature` | `temperature` | | `bell` | `bell` |
| `wind` | `wind` | | | |
| `timer` | `stopwatch` | | | |
| `alarm` | `alarm` | | | |
| `calendar` | `calendar` | | | |

`weather` is Tabler's `sun`: the set has no `cloud-sun`, and no `weather-*` glyphs at all,
so the clear-sky glyph carries the app tile and each condition gets its own name. `clock`
appears at both sizes on purpose — 16 px for the status row, 24 px for the tile.

### 32×32 — boot

| Our name | Tabler icon | Required |
|---|---|---|
| `boot` | `rocket` | optional — swap for a device mark later |

`link`/`offline` are deliberately a matched pair (`wifi` / `wifi-off`): same silhouette,
same weight, one slashed. That is what makes the state read as a state.

## 3. Pipeline

```
tools/svg2mask/svg/*.svg     Tabler source, committed, 24×24 viewBox
        ↓  node build.mjs
assets/icons/generated/icons.h    row-packed 1-bit masks, MSB first
assets/icons/PROOF.png            labelled sheet, drawn from the mask bits
```

Run it:

```bash
cd tools/svg2mask && node build.mjs
```

Steps performed per icon: rewrite `stroke="currentColor"` to white, force the render size
to the target pixel size, rasterise 1:1 (no supersampling — it was tried and turned strokes
dotted), threshold at 50 % alpha coverage, then clear the outer 1 px ring.

Dependencies live in `tools/svg2mask/node_modules` (`@resvg/resvg-js`, `pngjs`) and are
build-only — nothing is shipped to the MCU.

To re-pull a source SVG:

```bash
curl -L -o tools/svg2mask/svg/clock.svg \
  https://unpkg.com/@tabler/icons@latest/icons/outline/clock.svg
```

## 4. Layout in the header

```c
#include "icons.h"

// draw with tint: 1-bits take fg, 0-bits take bg (pass bg = screen background)
// stride is ceil(size/8) bytes per row, MSB first
```

`icon_t` carries `data`, `w`, `h` and `name`, and `ICONS[]` / `ICON_COUNT` enumerate the
whole set, so a menu can be data-driven. `ICON_FLASH` expands to `PROGMEM` on AVR and to
nothing elsewhere, so the same header works on AVR or ESP32.

## 5. Keep-clear ring

Nothing may touch the outer 1 px border — the bezel clips 1–2 px and that is what is
currently eating the "I" in "TIME". The builder enforces this by zeroing that ring, so an
icon that looks tight in the proof sheet is tight because Tabler drew it there, not because
of a scaling accident.

Icons are stored **upright**. Never pre-rotate them for the landscape mounting — rotation
is handled in code together with the panel geometry fix.

## 6. Palette the firmware draws (never baked into the artwork)

| Role | Hex | RGB565 |
|---|---|---|
| Background | `#0B0B0F` | `0x0841` |
| Active icon | `#FFFFFF` | `0xFFFF` |
| Inactive icon | `#5A6172` | `0x5B0E` |
| Accent rule | `#4A79C8` | `0x4BCE` |
| Warning | `#E0A030` | `0xE506` |
| Offline | `#D04040` | `0xD208` |

## 7. Hand-drawn overrides

`assets/icons/16|24|32/*.png` are blank templates and `SHEET.png` is their reference grid.
They are only needed if a specific glyph must be replaced with hand-drawn art (the boot
mark is the likely candidate). Tabler is MIT, so attribution is the only obligation —
`tools/svg2mask/svg/LICENSE.tabler.txt` is kept alongside the sources.

If you do override one, draw 1:1 at the exact canvas size with a hard-edged pencil, keep
the outer 1 px ring empty, export as RGBA PNG, and it can be thresholded with the same rule.

## 8. Next step

Wire `icons.h` into the status renderer with a tint helper, then fix the panel geometry that
is currently clipping text and leaving the bottom third uninitialised.
