# Design philosophy

This is the decision record for the Elegoo Connector dashboard and bridge. It exists so
that a future argument about the interface — "should the progress bar animate?", "why
can't I see a temperature?", "can we add a big green play button?" — can be settled by
pointing at a principle rather than at taste.

Each principle states the rule, why it matters for a machine that heats to 250 °C and
moves, how this repository implements it, and where the idea comes from. Where a rule is
our own reasoning rather than an external standard, it is labelled **design decision**.

Scope: the original Centauri Carbon (CC1). Its local protocol is SDCP v3 over a WebSocket
on port 3030, its webcam is MJPEG on port 3031, and its own web interface has no
authentication. Those constraints are the soil everything below grows in; see
[PLAN.md](../PLAN.md) §1.

---

## 1. Architecture

### P1. The printer is the authority; the UI is a projection

**The rule.** Nothing in this system decides what the printer is doing. The firmware
reports, we display. When the two disagree, the firmware is right and the interface
updates.

**Why.** A dashboard that believes itself will eventually show a print at 90 % while the
printer sits in an error state. The user then does the worst possible thing: walks away.

**In this project.** `StateStore.set_status()` in `bridge/elegoo_bridge/state.py` accepts a
`Status` frame and replaces the projection wholesale; it never merges a previous job into a
fresh one. `normalise_status()` derives `job`, `temps`, `fans` and `state` only from fields
the printer sent. There is no client-side notion of "probably". Values the firmware does not
report stay `null` in the snapshot (`chamber` on firmware without a box thermistor), and the
UI renders them as "—" rather than inventing a plausible number.

**Source.** OctoPrint separates printer profile, live printer state and job state as
distinct API resources — [printer operations](https://docs.octoprint.org/en/main/api/printer.html),
[job operations](https://docs.octoprint.org/en/main/api/job.html). Fluidd states plainly
that the browser speaks to Moonraker, never to Klipper —
[Fluidd development](https://docs.fluidd.xyz/development/).

### P2. Never make printer communication wait on UI work

**The rule.** Rendering, camera decoding, file lists and history are off the critical path.
A slow image or a hung tab must never delay a command or a status frame.

**Why.** The control channel here is a single WebSocket with a hard idle timeout and a
five-client limit. If one expensive consumer can block the read loop, it can stall every
other client and eventually lose the connection mid-print.

**In this project.** One asyncio event loop, and every blocking operation is pushed off it:
file reads during upload run through `asyncio.to_thread` in `bridge/cc1client/upload.py`,
history and file listing are separate request/response round trips that cannot hold the
reader, and the camera has its own task (`CameraBroadcaster`) feeding subscriber queues,
never the command path. Status fan-out is coalesced at `COALESCE_S = 0.4` s in
`state.py`, so a burst of frames cannot turn into a burst of writes.

**Source.** OctoPrint returns an empty success for a command rather than waiting for the
printer, because a synchronous wait would block its web server —
[printer operations](https://docs.octoprint.org/en/main/api/printer.html); its plugin
documentation warns that long work in communication hooks blocks the receive loop —
[plugin hooks](https://docs.octoprint.org/en/main/plugins/hooks.html).

### P3. One connection owner per printer, many viewers

**The rule.** Exactly one process opens the printer's WebSocket and exactly one opens its
camera. Browsers connect to the bridge, never to the printer.

**Why.** The CC1 firmware accepts roughly five concurrent WebSocket clients and closes idle
sockets after about a minute; camera slots are scarcer still and leak after a dirty
disconnect. A page that dials the printer directly works beautifully for one tab and breaks
the moment there are three, or the phone sleeps. Measured behaviour and the firmware's
"too many client" response are recorded in [PLAN.md](../PLAN.md) §1.5.

**In this project.** `bridge/elegoo_bridge/printer.py` owns the single upstream connection
with reconnect and backoff; `CameraBroadcaster` (vendored from pycentauri, Apache-2.0) holds
one upstream MJPEG reader and fans raw bytes out to any number of `/api/camera/stream`
consumers. Subscribers that fall behind lose old frames, not the reader.

### P4. One shared state model with domain boundaries; no deltas

**The rule.** The API publishes one snapshot object containing connection, capabilities,
printer and log. Clients replace their state with it. There are no incremental patches to
reconcile and no ordering to get wrong.

**Why.** Deltas are how dashboards drift: a dropped message leaves a temperature frozen
forever, and a reconnect produces a UI that is half-old and half-new with nothing to tell
you which half. A whole snapshot is idempotent — apply it twice, get the same screen.

**In this project.** The snapshot contract is [docs/API.md](API.md) §1; `StateStore.snapshot()`
builds it on every send. Boundaries are explicit: `connection` describes the bridge's link to
the printer, `printer` describes the machine, `log` describes notable transitions. Nothing
in the interface has to infer one from another.

**Source.** Fluidd's store is namespaced to mirror the Klipper and Moonraker domains, with
ephemeral events kept out of the store — [Fluidd development](https://docs.fluidd.xyz/development/).
Moonraker's role as the single API surface in front of the printer is described in the
[API introduction](https://moonraker.readthedocs.io/en/latest/external_api/introduction/).

---

## 2. Honesty about state

### P5. Connection state is a first-class state, not a footnote

**The rule.** "Is the dashboard talking to the printer?" is displayed with the same
prominence as the print state, and is never inferred from whether a number looks plausible.

**Why.** Wi-Fi drops, the printer reboots and the Pi's tunnel sleeps. In all three cases the
last good reading is still sitting in memory, looking perfectly current. Users cannot
distinguish "cooling down" from "nobody is home" without being told.

**In this project.** `connection.state` (`connecting` / `online` / `offline`) plus
`since_ts`, `reconnects` and `last_error` in every snapshot. The bridge sets it from the
supervisor's own connect/disconnect events, not from frame contents, and logs transitions
with `kind: "connection"`.

**Source.** OctoPrint's push API distinguishes the connection message, initial state,
rate-limited updates and events — [push API](https://docs.octoprint.org/en/main/api/push.html).
Fluidd's troubleshooting separates a Moonraker link problem from a printer problem —
[Fluidd FAQ](https://docs.fluidd.xyz/faq/).

### P6. Stale is a state: never present an old temperature as current

**The rule.** After ten seconds without a status frame, every value is marked stale and the
interface de-emphasises it. Stale data is not hidden — it is labelled.

**Why.** A 214 °C reading is a fact about the past. Showing it in the same typeface as a live
reading is the most common lie a printer dashboard tells, and it is the one that gets
someone to reach into a still-hot enclosure.

**In this project.** `STALE_AFTER_S = 10.0` in `state.py`; the snapshot carries
`printer.stale` and `printer.stale_for_s`, and the dashboard dims temperatures, progress and
camera poster while `stale` is true. The API contract makes this a rule, not a suggestion —
[docs/API.md](API.md) §1.

**Design decision**, informed by the practical gap between the printer's ~1 s push rate and
any plausible network stall.

### P7. Requested is not confirmed — optimistic UI is a lie

**The rule.** Pressing Pause does not make the interface say "Paused". It says "Pausing…"
until a status frame says otherwise, and reverts with the printer's answer if one never
comes.

**Why.** The command channel and the status channel are different paths. A tap can be
accepted by the firmware and still do nothing — speed changes at idle are a documented
no-op, and a rejected command is silent unless you look. Optimism converts a failed command
into a confident falsehood.

**In this project.** Every mutating route returns `{"accepted": true, "request_id": ...}`
and the outcome arrives asynchronously in the snapshot ([docs/API.md](API.md) §2); controls
hold a pending state until the next snapshot confirms the new value, with a timeout that
surfaces the error rather than pretending success.

**Source.** OctoPrint's command responses are deliberately decoupled from the printer's
answer ([printer operations](https://docs.octoprint.org/en/main/api/printer.html)); the
underlying caution — that a client must not assume a state change from an accepted request —
is standard in Moonraker-style APIs
([Moonraker API](https://moonraker.readthedocs.io/en/latest/external_api/introduction/)).

---

## 3. Situation awareness

### P8. Grey is good; colour carries meaning only

**The rule.** The interface is near-monochrome at rest. Colour appears only when something
deserves attention, one colour means one thing, and colour is always paired with a word and
an icon so it is never the sole carrier of meaning.

**Why.** A screen that is already saturated has nowhere to escalate to. When normal
operation is quiet, "paused" or "error" reads instantly from across the room — which is the
only way this dashboard is ever used.

**In this project.** Neutral greys for chrome and normal state; a single amber accent driven
by `printer.attention`; red reserved for `error`; the progress fill is the only large
saturated area and it is always accompanied by a percentage. State is always rendered as
`state_label` text, never as a coloured dot alone.

**Source.** The ANSI/ISA-101 standard addresses HMI design as a lifecycle and its committee
explicitly avoided mandating exact colours — [ISA-101](https://www.isa.org/standards-and-publications/isa-standards/isa-101-standards),
[Automation World, "New HMI Standard Almost Ready for Viewing"](https://www.automationworld.com/products/control/blog/13312225/new-hmi-standard-almost-ready-for-viewing).
The grey-screen, limited-colour approach is High-Performance HMI practice derived from
abnormal-situation-management human-factors work, not an ISA-101 colour rule —
["The Great Graphics Debate"](https://www.automationworld.com/products/control/article/13316377/the-great-graphics-debate-is-going-gray-good-for-hmis).

### P9. Alarm, event and notice are different things

**The rule.** An alarm requires a timely operator response. A completion is a notice.
Everything else is an event in the log. We rationalise before we annunciate, and we do not
raise alarms we cannot act on.

**Why.** Alert fatigue is a safety mechanism failing, not a UI annoyance: a person who has
dismissed two hundred meaningless "still printing" messages will dismiss the one that
matters. A print finishing is not a failure; it does not get red.

**In this project.** Log entries carry both a severity (`level`) and a meaning (`kind`):
`job_failed` and `job_paused` are attention-worthy; `job_finished`, `job_started`,
`job_stopped`, `progress`, `connection` and `notice` are not. `state.py` emits them on state
transitions only, never on poll ticks.

**Source.** ISA-18.2 defines an alarm as an abnormal condition requiring timely response,
and distinguishes alerts, prompts and notices from alarms —
[ISA-18 series](https://www.isa.org/standards-and-publications/isa-standards/isa-18-series-of-standards),
[ISA, "Alarm management questions that everyone asks"](https://www.isa.org/intech-home/2020/march-april/features/alarm-management-questions-that-everyone-asks),
[exida, "When is an Alarm not an Alarm?"](https://www.exida.com/Blog/when-is-an-alarm-not-an-alarm).

### P10. Situation awareness over decoration; overview first, then drill down

**The rule.** The first screen answers three questions in one glance: is the job running
normally, is anything drifting out of range, and does anything need me? Detail lives one tap
away.

**Why.** The dashboard is consulted for two seconds at a time, usually from a phone, often
in a hurry. Ornament — animated printers, spinning fans, a 3D preview — costs attention and
returns nothing at that cadence.

**In this project.** "Now printing" is the default view and contains state, job name,
progress, layer, remaining time, three temperature readouts, and nothing else. Temperature is
always shown as target **and** actual **and** deviation (the `temps.*.target` and
`temps.*.actual` pair from the snapshot); the deviation is what makes 214 °C next to a 215 °C
target readable as "fine" rather than "a number".

**Source.** Yokogawa's account of ISA-101 describes task-based displays and an HMI hierarchy
that opens on an overview and drills into detail —
[Yokogawa, "ISA-101: Toward a More Effective HMI Strategy"](https://www.yokogawa.com/us/library/resources/media-publications/isa-101-toward-a-more-effective-hmi-strategy/).

---

## 4. Remote access

### P11. Separate visibility from authority

**The rule.** Showing the printer and commanding the printer are different privileges. The
bridge defaults to read-only; the mutating routes do not exist unless the operator turns
them on.

**Why.** Most remote use is looking. The value of a remote view is reassurance, and the risk
is concentrated entirely in the handful of writes. A default that can only watch cannot
ruin a print at 03:00 from a train.

**In this project.** `EC_CONTROL` defaults to `False` in `bridge/elegoo_bridge/config.py`;
when it is off, `capabilities.control` and `capabilities.upload` are false and every
`POST /api/print/*` and `/api/uploads` route returns `403`. Turning it on is a deliberate
edit to the systemd unit, which is visible in the deployment documentation.

**Source.** CISA's guidance on securing remote access software and NIST SP 800-82 Rev. 3 on
OT constraints both treat remote authority as something to be granted narrowly —
[CISA](https://www.cisa.gov/resources-tools/resources/guide-securing-remote-access-software),
[NIST SP 800-82r3](https://csrc.nist.gov/pubs/sp/800/82/r3/final). Prusa documents a
read-only team role as a first-class concept —
[Prusa Connect](https://blog.prusa3d.com/prusa-connect-a-network-solution-for-remote-control-of-3d-printers_90364/).

### P12. Remote monitoring leads with machine state, not video

**The rule.** The poster frame is still, aged and labelled with its age. Live video starts
on an explicit action, shows "LIVE" only while it is actually streaming, and stops when the
tab is hidden.

**Why.** MJPEG is expensive — roughly 6 Mbps at 720p, about 2.7 GB per hour — and a phone on
mobile data will notice. Video is also the slowest thing on the screen, so leading with it
delays the one fact the user came for.

**In this project.** `/api/camera/snapshot` is the default (`Cache-Control: no-store`);
`/api/camera/stream` is opened only by the "Live" control and closed on
`visibilitychange`. The API contract requires this behaviour ([docs/API.md](API.md) §2), and
`/api/camera` exposes `viewers` so the bridge's single upstream connection can be verified.

**Source.** Printago separates aged poster snapshots from on-demand live video and shows a
Live indicator only while streaming —
[Printago camera streaming](https://docs.printago.io/docs/printing/printer-management/camera-streaming).
Bambu Handy offers autoplay on any network, Wi-Fi only, or never —
[Bambu Handy guide](https://wiki.bambulab.com/en/studio-handy/handy/bambu-handy-quick-start).
Obico stops the stream when the app is backgrounded or the phone is locked —
[Obico streaming](https://www.obico.io/docs/user-guides/webcam-streaming-for-human-eyes/).

### P13. Notify on transitions, not on polls

**The rule.** One message per meaningful state change: started, finished, failed, paused,
offline. Never one per progress tick. Per-event preferences and quiet hours are the goal,
not an afterthought.

**Why.** A notification is an interruption with a cost. A printer produces thousands of
status frames per print and perhaps five facts worth waking someone for.

**In this project.** The log is the single event stream, deduplicated by construction: a
log entry is written in `_announce_transition()` when the state slug changes, so repeated
frames in the same state produce nothing. `LOG_MAX = 50` bounds memory on a 512 MB Pi.

**Source.** Android's notification guidance asks for brief, timely, relevant messages with
user control over categories —
[Android notifications](https://developer.android.com/design/ui/mobile/guides/home-screen/notifications);
Apple reserves Live Activities for ongoing, user-initiated activity —
[Apple HIG](https://developer.apple.com/design/human-interface-guidelines/live-activities);
Alertmanager exists precisely to deduplicate and group —
[Prometheus Alertmanager](https://prometheus.io/docs/alerting/latest/alertmanager/). GOV.UK
advises showing only the highest-priority banner at once —
[GOV.UK notification banner](https://design-system.service.gov.uk/components/notification-banner/).
Per-event, per-printer preference design follows Printago —
[Printago notifications](https://docs.printago.io/docs/tools/notifications).

### P14. Never expose the unauthenticated printer

**The rule.** The bridge is the only thing that faces the internet, and it adds a session
cookie, same-origin checks on mutations and rate-limited login. The printer's own port 80
and 3030 are never proxied, and never reachable from outside the LAN.

**Why.** The printer's interface has no authentication whatsoever. Publishing it publishes a
hot machine and a live camera to anyone who finds the hostname — and its WebSocket can be
driven by a single curl command.

**In this project.** `docs/API.md` §4 states the model; `bridge/elegoo_bridge/auth.py`
implements the cookie and origin check; `bridge/elegoo_bridge/server.py` registers mutating
routes only when control is enabled. Remote reachability is Tailscale (private) or a
Cloudflare tunnel with an Access policy, both documented in
[docs/PI_SETUP.md](PI_SETUP.md).

**Source.** OctoPrint's safe remote access guidance warns explicitly against blind port
forwarding and recommends tunnelling or a VPN —
[OctoPrint, "A Guide to Safe Remote Access"](https://octoprint.org/blog/2018/09/03/safe-remote-access/).

---

## 5. Safety

### P15. Confirmation is not a safety control

**The rule.** A confirm dialog prevents a fat finger. It does not make an action safe, and
this dashboard is not an emergency stop. Interlocks belong to the firmware, and the physical
machine remains the authority on what it will allow.

**Why.** Software that implies safety it does not provide is worse than software that
admits its limits. A user who believes the Stop button is an e-stop may reach into a machine
that is still moving. Conversely, a confirm dialog in front of something genuinely urgent
costs seconds that matter. ISO 13850 requires the emergency-stop function to be a single
human action, with no restart until it is reset — a web page over Wi-Fi can never satisfy
that.

**In this project.** Stop is a two-tap confirm inside the app; it is labelled "Stop print",
never "Emergency stop"; and the About view states that the printer's own controls and
interlocks govern. The bridge refuses commands it knows are invalid (deleting the file
currently printing, per `delete_files`, out-of-range heater targets per `/api/print/temps`)
rather than passing them through and hoping.

**Source.** [ANSI on ISO 13850](https://blog.ansi.org/ansi/iso-13850-safety-of-machinery-emergency-stop/).
The framing of confirmation as convenience rather than safeguard is a **design decision**.

---

## Anti-patterns this project refuses

- **A notification or log line per poll.** Trains the user to ignore the channel (P13).
- **A "LIVE" badge on a stale frame.** The one label that must never be approximate (P6, P12).
- **Optimistic controls.** Saying "Paused" because a request was accepted (P7).
- **Feature theatre.** Anything that looks like AI failure detection, print-quality scoring or
  a slicer preview but is not actually implemented. A convincing fake in a safety-adjacent
  surface is worse than a missing feature.
- **One permission for viewing and controlling.** Especially over a tunnel (P11).
- **Colour as the only carrier of meaning.** Deuteranopia is common; the printer does not
  care (P8).
- **Decoration that moves.** Animation competes with the state change that matters (P10).
- **Delivering the MJPEG stream to a phone by default.** (P12).

---

## What we deliberately do not build

| Not built | Reason |
|---|---|
| Levelling / calibration / self-test UI | The firmware owns these routines and their preconditions. A partial reimplementation would let a user start a motion sequence the machine is not ready for. Buttons on the touchscreen remain the only way. |
| Firmware update | Highest-consequence, lowest-frequency operation on the device, with a real brick risk and no way for us to verify the image. Out of scope permanently. |
| G-code preview / layer viewer | Requires parsing and rendering a file the printer already parsed, on a 512 MB Pi, to answer a question the user asks at the desk rather than from a phone. The firmware's own thumbnail is shown instead. |
| AI failure detection | We have no trained model, no detection hours and no honest confidence figure. Shipping a switch that implies supervision we do not provide would be exactly the feature theatre listed above. |
| Emergency stop | Cannot be a network round trip, and must not be presented as one (P15). |
| Multi-printer fleet management | One printer, one bridge, one state store. Every abstraction added for a second machine is complexity the first one pays for. |

---

## Open questions

1. **Notification delivery.** The log is the event source, but there is no push channel yet.
   Web Push via the PWA service worker is the obvious candidate; ntfy or a Telegram bot is
   the low-effort one. Which one depends on whether the phone keeps the app installed.
2. **Camera framing.** One shared upstream MJPEG at the printer's native resolution is
   simple, but we cannot downscale on the Pi without CPU cost. Is a 1 fps snapshot poll
   (±3 s freshness) genuinely good enough as the default, or does it feel broken when
   something is going wrong?
3. **Remote control by default.** `EC_CONTROL=0` is the safe default, but the user's stated
   goal is to check on prints from anywhere. The open question is whether remote *pause*
   (intervention) should be separable from remote *start* (new work) as two flags.
4. **Multi-client command authority.** With two phones open, both can press Stop. A lease —
   "you have control" — is the standard answer, but it is real complexity for a one-person
   household.
5. **Stale threshold.** Ten seconds is a guess tuned to the printer's ~1 s push rate. If the
   push scheduler goes dormant at idle (a known CC1 firmware behaviour), the interface would
   spend most of its life looking stale. The honest fix is bridge-side polling; the open
   question is the poll interval that keeps the screen live without making the printer's
   single socket chatty.
