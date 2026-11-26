# The LCD HAT on the 40-pin header

A Waveshare HAT with a small SPI screen, a 5-way joystick and three push buttons is a
natural fit for a printer that lives on a shelf: glanceable state without unlocking a
phone. This page is the setup and the honest limits.

The renderer is [`scripts/panel.py`](../scripts/panel.py). It is deliberately plain Python:
no Pillow, no font files, no build step. The kernel does the panel driving through `fbtft`,
so the script only has to write 16-bit pixels to `/dev/fbN` — cheap enough for a Zero 2 W.

## Why the screen glows dark blue and shows nothing

That glow is the backlight. The panel itself has no controller memory and does nothing
until something sends it an init sequence, and out of the box **SPI is disabled** on
Raspberry Pi OS. Both the wiring and the driver are missing, so the display stays blank
while the backlight burns.

Confirmed on this Pi before any change:

```
/dev/spidev*        missing
dmesg | grep fb     only the firmware's simplefb, which vc4 then takes over
config.txt          no dtparam=spi=on, no display overlay
```

## 1. Enable SPI and the panel driver

Both Waveshare variants use the **same pins** (they differ only in controller and size):

| Function | BCM | | Function | BCM |
|---|---|---|---|---|
| LCD CS / CE0 | 8 | | Joystick up | 6 |
| LCD DC | 25 | | Joystick down | 19 |
| LCD RST | 27 | | Joystick left | 5 |
| LCD backlight | 24 | | Joystick right | 26 |
| LCD MOSI | 10 | | Joystick press | 13 |
| LCD SCLK | 11 | | KEY1 / KEY2 / KEY3 | 21 / 20 / 16 |

Add this to `/boot/firmware/config.txt` (the `[all]` section), then reboot:

```ini
dtparam=spi=on
# Pull-ups for the joystick and the three buttons, as Waveshare's own pages specify.
gpio=6,19,5,26,13,21,20,16=pu

# 1.44 inch, 128x128, ST7735S — the usual "LCD HAT"
dtoverlay=fbtft,spi0-0,st7735r,width=128,height=128,speed=32000000,rotate=0,reset_pin=27,dc_pin=25,led_pin=24

# 1.3 inch, 240x240, ST7789VW — use ONE of these two lines, never both
#dtoverlay=fbtft,spi0-0,st7789v,width=240,height=240,speed=32000000,rotate=0,reset_pin=27,dc_pin=25,led_pin=24
```

Which one is fitted cannot be detected in software: the HAT wires no MISO, so there is no
controller ID to read. Measure it — 128 px (1.44", square, quite coarse) or 240 px
(1.3", noticeably finer) — or try the other line if the first gives a blank screen.

**Keep `rotate=0` in the overlay, always.** fbtft does not rotate in software: it writes
the controller's MADCTL register. `rotate=90` sets `MV|MY`, which swaps the address axes
of a 240x320 GRAM, so a 240x240 picture is drawn into an address space whose visible
window starts 80 lines in. The result is a picture shifted to one side with a band of
stale RAM — coloured stripes — along the opposite edge. Rotation belongs to
`scripts/panel.py --rotate`, which turns the finished frame on the CPU.

## 2. Check the framebuffer appeared

```bash
ls -l /dev/fb*            # expect fb0 and fb1, or just fb1
cat /sys/class/graphics/fb1/virtual_size
```

`/dev/fb1` is the HAT; `fb0` is whatever the HDMI side created. The user running the
panel script needs the `video` group — the default Pi user already has it.

## 3. Run it

```bash
python3 scripts/panel.py --fb /dev/fb1          # 240x240 is detected from sysfs
```

It polls `/api/state` every two seconds and blits the frame. `--size auto` (the default)
reads the geometry from `/sys/class/graphics/fbN/virtual_size`, so a wrong `--size` cannot
squash the layout. `--rotate 90` turns the frame clockwise when the HAT is mounted
sideways; `EC_PANEL_ROTATE` sets the same value without a flag and is read from the shell
first, then from `~/.elegoo-bridge.env` — so `EC_PANEL_ROTATE=90` there is the one place the
mounting orientation lives.

Without hardware, or to check the layout from a laptop, the same code renders a PNG:

```bash
python3 scripts/panel.py --demo --preview panel.png          # 128x128 layout
python3 scripts/panel.py --demo --preview panel.png --size 240x240 --rotate 90
python3 scripts/panel.py --preview live.png --password ...   # real state, real weather

# --page picks the screen: home (the default), clock, weather, timer, a tab name,
# or offline to preview that page on its own.
python3 scripts/panel.py --demo --preview clock.png --size 240x240 --page clock
```

What it shows: a home screen. Four tiles, each with an icon and a live value — the printer
(state or percentage, plus its progress bar), the clock, the weather and the timer. The dot
top right is the bridge: green while it answers, red when it does not. From there the
joystick opens an app, and KEY3 comes back.

The Elegoo app is where the older pages live. `status` is the state word, nozzle/bed (and
chamber at 240 px), the job file name with a progress bar and remaining time, or `READY`
plus the firmware version when idle. A stale frame is labelled `STALE` in amber rather than
shown as if it were live.

When the printer is offline **and you are in the Elegoo app**, that app switches to a page
of its own: `OFFLINE`, `NO LINK`, which printer it is waiting for, since when, and the
bridge's last error, wrapped. If the *bridge* is the unreachable side the title reads
`NO BRIDGE`. Home is deliberately not taken over — a clock, a weather line and a timer are
still worth having when the printer is switched off.

Start it with the pinned-down script, which keeps one instance running and reads the
buttons:

```bash
sh ~/panel.sh        # rotation from EC_PANEL_ROTATE
sh ~/panel.sh 270    # override for this run only
```

At boot, put that in cron next to the bridge — the launcher waits up to a minute for
`/dev/fb1` to appear, so it is safe at `@reboot`:

```cron
@reboot /bin/sh /home/thijn/panel.sh >> /home/thijn/elegoo-panel.log 2>&1
```

It must run as `thijn`, never as root: root's `HOME` is `/root`, so it cannot read
`~/.elegoo-bridge.env`, gets a 401 and the panel now exits with that message instead of
drawing `OFFLINE` for ever.

Starting the panel before the bridge is fine: if `/api/login` is refused it keeps drawing
home and retries the login, instead of exiting dark and waiting for the next cron. Two panels writing the same framebuffer is the other trap —
they take turns tearing the screen — which is what the pidfile guard prevents.

## 4. The joystick and buttons

They are wired (pins above), pulled up, and read by `panel.py` through libgpiod. Reading
needs the python bindings from the *system* interpreter (`python3-libgpiod`), which is why
`panel.sh` runs `/usr/bin/python3` and not the bridge's virtualenv.

The panel boots on **home**, a 2x2 launcher. Left/right (or KEY1/KEY2) moves along a row,
up/down moves between rows, and pressing the joystick opens the tile you are on.

| Input | On home | Inside an app |
|---|---|---|
| joystick left/right, KEY1/KEY2 | move between tiles | next / previous tab (Elegoo), or next / previous app (clock, weather, timer) |
| joystick up/down | move between rows | control cursor; on the timer, ±1 minute |
| joystick press | open the focused app | run the selected row; start / pause the timer; back home |
| KEY3, or KEY3 + joystick press | — | back to home |

KEY3 and the joystick together is the way out of anywhere, including the middle of a tab.
KEY3 alone does the same thing, which is one press fewer; both are there because "press the
third button and click" is how this was asked for. The Elegoo app remembers the tab you left
it on, so leaving `control` and coming back does not send you to `status` first.

Any press redraws immediately. The printer state refreshes every two seconds, the weather
every ten minutes, and the clock on its own minute (seconds while a timer is on screen).

### Home

Four tiles, each with a live value rather than a static icon:

| Tile | Shows |
|---|---|
| **ELEGOO** | the state word, or the job percentage with its progress bar when printing |
| **CLOCK** | the time |
| **WEATHER** | the condition icon and the temperature |
| **TIMER** | what is left |

Three things per tile, not four: a fourth line only fits at 7 px, which is below any
readable size, so the file name and the rain summary live one press away in the app they
belong to. The focused tile gets a white frame and a second inner frame; the others are
dim. The first tile is the printer, so home is one press away from the thing you came to
check.

Five dots sit in the header of every Elegoo tab with the current one filled — "you are
here, 2 of 5" — because five tabs without a position indicator is a maze. Which dot is
filled is a position rather than a colour, so it reads without colour vision, and the link
dot is hollow with a slash through it when there is no link, for the same reason.

On a 128 px panel those dots are left out: at that width they are two-pixel specks that
overlap the end of the page title, and the title already names the page. The rule is
`width < 180` in `_tab_badge`, so a 240 px HAT keeps them.

### Weather

Buienradar, for one fixed place — Valkenswaard by default. The public feed
(`data.buienradar.nl/2.0/feed/json`) has no station in Valkenswaard, so the nearest one is
picked by distance and named on the page (`EINDHOVEN 12KM`), while the rain forecast comes
from `gadgets.buienradar.nl/data/raintext` for the exact coordinates: 24 five-minute steps
covering the next two hours, drawn as bars.

The summary line is one of `DRY 2H`, `RAIN 14:35`, `RAIN NOW` or `RAIN TIL 15:10`. Code 80
is the threshold, about 0.24 mm/h — drizzle that never reaches the ground should not read as
rain. The panel is in English like the dashboard, so Buienradar's Dutch description is not
printed; its icon letter is translated to `CLEAR`, `PARTLY CLOUDY`, `CLOUDY`, `RAIN`,
`SNOW`, `FOG` or `THUNDER` instead. Only the numbers and the station name come from the
feed.

It is fetched in a background thread, never on the render path: a slow HTTPS round trip must
not freeze the panel. Success refreshes every ten minutes; a failure keeps the last good
reading and retries after two minutes. No reading at all shows `NO DATA` rather than an
invented number.

Override the place without touching the code — shell or `~/.elegoo-bridge.env`:

```ini
EC_PANEL_LAT=51.3506
EC_PANEL_LON=5.4592
```

### Timer

Set it, start it, walk away. `up`/`down` changes the duration in one-minute steps (hold it
to spin faster, up to three hours); pressing starts and pauses; `KEY1` resets to the set
duration; `KEY2` adds five minutes, which also works while it runs and simply moves the end.

The key legend is three rows on a tall panel and two on a 128 px one (`KEY1 RST` and
`KEY2 +5` share the last line), which is what leaves room for `PRESS START` underneath.
Without that line the one action the screen exists for was the only thing not written down.

The HAT has no buzzer, so `TIME UP` is amber and the tile keeps saying it for ten minutes.
It is a glanceable reminder, not an alarm.

### The Elegoo app

The pages are **status** (state, temperatures, job and progress), **temps** (large values,
coloured by distance from the target), **control**, **service** (wear and the maintenance
reminder) and **system** (Pi address, uptime, CPU temperature, printer address).

Every value is sized to the space it actually has. On the temps page a 128 px panel shows
`215` rather than `214.6°`, because the decimal and the degree mark at that scale ran 36 px
past the right edge; the value picks the largest glyph scale that fits instead of clipping.
The same check guards the `NO LINK` headline on the offline page.

### The control page

A cursor over five rows: `PAUSE`/`RESUME`, `LIGHT ON/OFF`, `PLA 215/60`, `PETG 240/80` and
`STOP PRINT`. Left/right (or up/down) moves the cursor, pressing it runs the row; KEY1 and
KEY2 still change tab, KEY3 goes back to home.

**Stop asks twice.** The first press turns the line into `PRESS AGAIN TO STOP` and arms the
command for four seconds; anything else cancels it. A mis-press on the joystick must not be
able to throw away a print.

Rows the firmware would refuse right now are dimmed and answer `NOT NOW`, and each row
carries a marker at its right edge — filled means the firmware will take it now, hollow
means it will not. That is the same shape language as the link dot in the header, and it is
there because dim-versus-bright text alone is a contrast difference of about 2.4:1, which is
not enough to carry the meaning on its own. A press says `SENDING` until the bridge answers,
because an eight-second timeout with a frozen screen reads as a dead button. The two presets are only offered while the machine is idle — a
running job keeps the targets it started with. The web Control page offers the same four preset pairs, so the two screens cannot
disagree about what "PLA" means.

Control needs the bridge started with `EC_CONTROL=1`. Without it the page still shows the
state and the rows, marked `READ ONLY`, and pressing sends nothing.

### The service page

Hours since the last service, total print hours, completed jobs and the reminder interval,
all from the same `usage` block the About page reads. When `EC_MAINTENANCE_HOURS` is set and
reached, the header and `SERVICE DUE` turn red; without it the page says `REMINDER OFF` and
`SET EC_MAINT HOURS` instead of inventing a schedule.

To verify the hardware, watch the lines while pressing:

```bash
watch -n0.3 'pinctrl get 6,19,5,26,13,21,20,16'
```

An unpressed line reads `hi` (pull-up active); a pressed one goes `lo`. If all eight stay
`hi` while you press, the HAT is not seated far enough down the header.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Still dark blue after reboot, no `/dev/fb1` | Overlay not applied | `dmesg | grep -i fbtft` and `grep -i fbtft /boot/firmware/config.txt`; overlay typos are silent |
| Blank screen on the 1.3" HAT | Wrong controller selected | Swap `st7735r` for `st7789v` (and the width/height) |
| Colours look inverted / red and blue swapped | ST7735 panels vary | append `,bgr` to the `dtoverlay` line |
| Image shifted, band of coloured stripes at one edge | `rotate=90` in the overlay (MADCTL `MV` puts a 240x240 picture into a 240x320 window) | set `rotate=0` in the overlay and rotate with `panel.py --rotate 90` instead |
| Tearing or speckle | SPI clock too high for the wiring | drop `speed` to `16000000` |
| `/dev/fb1` exists but writes do nothing | Writing to `fb0` | pass the right device; `cat /sys/class/graphics/fb*/name` labels them |
| Picture alternates between two layouts, or between ONLINE and OFFLINE | Two panels writing the same framebuffer | `ps -eo user,pid,args \| grep panel.py`; kill the stray, then `sh ~/panel.sh 90` |
| `OFFLINE` on the panel while the dashboard is fine | Started as root: no `~/.elegoo-bridge.env`, so every request is a 401 | run `sh ~/panel.sh` as `thijn`; the panel now exits with that message instead |
| Permission denied on `/dev/fb1` | Not in the `video` group | `sudo adduser thijn video`, then log in again |
