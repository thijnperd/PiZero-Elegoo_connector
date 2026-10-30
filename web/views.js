/**
 * One render function per view.
 *
 * Each view exposes:
 *   render()            -> HTML string for the view's static shell
 *   mount(root, ctx)    -> bind handlers, kick off any fetch
 *   update(root, snap)  -> patch the dynamic parts; never touches the camera element
 *
 * The split exists so a snapshot arriving every 2 s does not blow away focus, slider
 * positions or the MJPEG <img> — the camera and inputs survive because update() only
 * writes text/attributes.
 */

import { api } from './api.js';
import { store, runCommand } from './store.js';

const TEMP_SCALE = { nozzle: 300, bed: 120, chamber: 60 };
const SPEED_MODES = [
  { id: 'silent', label: 'Silent', pct: 50 },
  { id: 'balanced', label: 'Balanced', pct: 100 },
  { id: 'sport', label: 'Sport', pct: 130 },
  { id: 'ludicrous', label: 'Ludicrous', pct: 160 },
];
const ACTIVE_STATES = new Set(['printing', 'starting', 'preheating', 'homing', 'leveling', 'calibrating']);

// --- formatting -------------------------------------------------------------

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[c]);
}

export function fmtDuration(totalSeconds) {
  if (totalSeconds === null || totalSeconds === undefined || Number.isNaN(totalSeconds)) return '—';
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(sec).padStart(2, '0')}s`;
  return `${sec}s`;
}

export function fmtClock(unixSeconds) {
  if (!unixSeconds) return '—';
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function fmtAgo(unixSeconds) {
  if (!unixSeconds) return '—';
  const diff = Math.max(0, Date.now() / 1000 - unixSeconds);
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

export function fmtDate(unixSeconds) {
  if (!unixSeconds) return '—';
  return new Date(unixSeconds * 1000).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function fmtBytes(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/** Normalise a timestamp that might be seconds or milliseconds. */
function toSeconds(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n > 1e11 ? n / 1000 : n;
}

function el(root, selector) {
  return root.querySelector(selector);
}

function setText(root, field, value) {
  const node = el(root, `[data-f="${field}"]`);
  if (node && node.textContent !== String(value)) node.textContent = String(value);
}

function setAttr(root, selector, name, value) {
  const node = el(root, selector);
  if (node) node.setAttribute(name, value);
}

/** Shared "is this data current?" calculation. */
function staleness(snapshot) {
  const printer = snapshot?.printer ?? null;
  const connection = snapshot?.connection ?? null;
  const stale =
    !printer || printer.stale === true || !connection || connection.state !== 'online';
  return { printer, connection, stale };
}

function gaugeMarkup(key, label) {
  return `
    <div class="gauge" data-gauge="${key}">
      <div class="gauge-head">
        <span class="gauge-label">${label}</span>
        <span class="gauge-value"><span data-f="${key}-actual">—</span><span class="gauge-unit">°C</span></span>
      </div>
      <div class="gauge-track">
        <span class="gauge-fill" data-f="${key}-fill"></span>
        <span class="gauge-target" data-f="${key}-marker" hidden></span>
      </div>
      <div class="gauge-foot"><span data-f="${key}-target">target —</span></div>
    </div>`;
}

function paintGauge(root, key, reading) {
  const actual = reading && typeof reading.actual === 'number' ? reading.actual : null;
  const target = reading && typeof reading.target === 'number' ? reading.target : null;
  const scale = TEMP_SCALE[key] || 300;

  setText(root, `${key}-actual`, actual === null ? '—' : actual.toFixed(1));
  const fill = el(root, `[data-f="${key}-fill"]`);
  if (fill) fill.style.width = actual === null ? '0%' : `${Math.min(100, (actual / scale) * 100)}%`;

  const marker = el(root, `[data-f="${key}-marker"]`);
  if (marker) {
    if (target === null || target <= 0) {
      marker.hidden = true;
    } else {
      marker.hidden = false;
      marker.style.left = `${Math.min(100, (target / scale) * 100)}%`;
    }
  }

  setText(root, `${key}-target`, target === null ? 'target —' : `target ${Math.round(target)}°`);

  const gauge = el(root, `[data-gauge="${key}"]`);
  if (gauge) {
    const overshoot = actual !== null && target !== null && target > 0 && actual - target > 12;
    gauge.classList.toggle('is-deviation', overshoot);
  }
}

// --- Now --------------------------------------------------------------------

export const nowView = {
  id: 'now',

  render() {
    return `
      <section class="stack">
        <div class="card status-card">
          <div class="card-head">
            <span class="state-pill" data-f="state-pill">
              <span class="state-dot" aria-hidden="true"></span>
              <span data-f="state-label">Connecting</span>
            </span>
            <span class="stale-tag" data-f="stale-tag" hidden>stale</span>
          </div>
          <p class="job-name" data-f="job-name">—</p>
          <div class="progress">
            <div class="bar bar-lg"><span class="fill" data-f="progress-fill"></span></div>
            <div class="progress-meta">
              <span data-f="progress-pct">—</span>
              <span data-f="layer">—</span>
            </div>
          </div>
          <dl class="times">
            <div><dt>Elapsed</dt><dd data-f="elapsed">—</dd></div>
            <div><dt>Remaining</dt><dd data-f="remaining">—</dd></div>
            <div><dt>ETA</dt><dd data-f="eta">—</dd></div>
          </dl>
        </div>

        <div class="card">
          <div class="card-head"><h2>Temperatures</h2></div>
          <div class="gauges">
            ${gaugeMarkup('nozzle', 'Nozzle')}
            ${gaugeMarkup('bed', 'Bed')}
            ${gaugeMarkup('chamber', 'Chamber')}
          </div>
        </div>

        <figure class="card camera" id="camera">
          <div class="camera-frame">
            <img id="camera-img" alt="View from the printer's camera" hidden>
            <div class="camera-empty" id="camera-empty">
              <span>Camera idle</span>
              <small>Snapshots only, to save data</small>
            </div>
          </div>
          <figcaption class="camera-bar">
            <span class="badge badge-live" id="camera-mode" hidden>LIVE</span>
            <span class="muted small" id="camera-note">Snapshots every 3 s</span>
            <button type="button" class="btn btn-small" id="camera-toggle">Go live</button>
          </figcaption>
        </figure>

        <div class="card">
          <div class="card-head"><h2>Machine</h2></div>
          <dl class="kv">
            <div><dt>Fans</dt><dd data-f="fans">—</dd></div>
            <div><dt>Speed</dt><dd data-f="speed">—</dd></div>
            <div><dt>Light</dt><dd data-f="light">—</dd></div>
            <div><dt>Position</dt><dd data-f="position">—</dd></div>
          </dl>
        </div>
      </section>`;
  },

  mount(root, ctx) {
    ctx.camera.attach(el(root, '#camera'));
  },

  update(root, snapshot) {
    const { printer, connection, stale } = staleness(snapshot);
    root.classList.toggle('is-stale', stale);
    const tag = el(root, '[data-f="stale-tag"]');
    if (tag) tag.hidden = !stale;

    const pill = el(root, '[data-f="state-pill"]');
    if (pill) {
      const state = printer?.state || (connection?.state === 'online' ? 'unknown' : 'offline');
      pill.dataset.state = state;
      pill.dataset.attention = printer?.attention ? 'true' : 'false';
    }
    setText(root, 'state-label', printer?.state_label || (connection?.state === 'online' ? 'Unknown' : 'No contact'));

    const job = printer?.job || null;
    setText(root, 'job-name', job?.filename || (printer ? 'Nothing printing' : 'Waiting for the printer'));

    const pct = job && typeof job.progress === 'number' ? job.progress : null;
    const fill = el(root, '[data-f="progress-fill"]');
    if (fill) fill.style.width = pct === null ? '0%' : `${Math.max(0, Math.min(100, pct))}%`;
    setText(root, 'progress-pct', pct === null ? '—' : `${Math.round(pct)}%`);

    if (job && (job.layer !== null || job.total_layers !== null)) {
      const layer = job.layer ?? '—';
      const total = job.total_layers ?? '—';
      setText(root, 'layer', `Layer ${layer} / ${total}`);
    } else {
      setText(root, 'layer', '—');
    }

    setText(root, 'elapsed', job ? fmtDuration(job.elapsed_s) : '—');
    setText(root, 'remaining', job ? fmtDuration(job.remaining_s) : '—');
    setText(root, 'eta', job?.eta_ts ? fmtClock(job.eta_ts) : '—');

    const temps = printer?.temps || {};
    for (const key of ['nozzle', 'bed', 'chamber']) paintGauge(root, key, temps[key]);

    const fans = printer?.fans || {};
    const fanText = [
      fans.model !== null && fans.model !== undefined ? `Model ${fans.model}%` : null,
      fans.auxiliary !== null && fans.auxiliary !== undefined ? `Auxiliary ${fans.auxiliary}%` : null,
      fans.chamber !== null && fans.chamber !== undefined ? `Chamber ${fans.chamber}%` : null,
    ]
      .filter(Boolean)
      .join(' · ');
    setText(root, 'fans', fanText || '—');

    const speedPct = job?.speed_pct;
    const mode = SPEED_MODES.find((m) => m.pct === speedPct);
    setText(root, 'speed', mode ? `${mode.label} (${speedPct}%)` : speedPct ? `${speedPct}%` : '—');

    setText(root, 'light', printer?.light === true ? 'On' : printer?.light === false ? 'Off' : '—');

    const pos = printer?.position;
    setText(
      root,
      'position',
      Array.isArray(pos) && pos.length === 3
        ? `X ${pos[0].toFixed(1)} · Y ${pos[1].toFixed(1)} · Z ${pos[2].toFixed(1)} mm`
        : '—',
    );

    if (printer?.error?.message) {
      setText(root, 'state-label', `Error: ${printer.error.message}`);
    }
  },
};

// --- Control ----------------------------------------------------------------

function heaterMarkup(key, label, max) {
  return `
    <div class="stepper" data-heater="${key}">
      <div class="stepper-head">
        <span>${label}</span>
        <span class="muted small" data-f="${key}-cur">—</span>
      </div>
      <div class="stepper-row">
        <button type="button" class="btn btn-square" data-step="-5" aria-label="Decrease ${label} target">−</button>
        <input type="number" inputmode="numeric" min="0" max="${max}" step="5" value="0"
               data-input="${key}" aria-label="${label} target temperature in degrees">
        <button type="button" class="btn btn-square" data-step="5" aria-label="Increase ${label} target">+</button>
        <button type="button" class="btn btn-small" data-set="${key}" data-pending-key="temp-${key}">Set</button>
      </div>
    </div>`;
}

export const controlView = {
  id: 'control',

  render() {
    return `
      <section class="stack">
        <div class="card">
          <div class="card-head">
            <h2>Job</h2>
            <span class="muted small" data-f="control-state">—</span>
          </div>
          <div class="row wrap">
            <button type="button" class="btn" data-cmd="pause" data-pending-key="pause">Pause</button>
            <button type="button" class="btn" data-cmd="resume" data-pending-key="resume">Resume</button>
            <button type="button" class="btn btn-danger" data-cmd="stop" data-pending-key="stop">Stop print</button>
          </div>
          <p class="muted small readonly-note" data-f="readonly-note" hidden>
            This bridge is read-only. Commands are disabled — restart it with control enabled to allow changes.
          </p>
        </div>

        <figure class="card camera" id="camera">
          <div class="camera-frame">
            <img id="camera-img" alt="View from the printer's camera" hidden>
            <div class="camera-empty" id="camera-empty">
              <span>Camera idle</span>
              <small>Snapshots only, to save data</small>
            </div>
          </div>
          <figcaption class="camera-bar">
            <span class="badge badge-live" id="camera-mode" hidden>LIVE</span>
            <span class="muted small" id="camera-note">Snapshots every 3 s</span>
            <button type="button" class="btn btn-small" id="camera-toggle">Go live</button>
          </figcaption>
        </figure>

        <div class="card">
          <div class="card-head"><h2>Print speed</h2></div>
          <div class="seg" role="group" aria-label="Print speed mode">
            ${SPEED_MODES.map(
              (mode) => `
              <button type="button" data-speed="${mode.id}" data-pending-key="speed" aria-pressed="false">
                <span>${mode.label}</span><small>${mode.pct}%</small>
              </button>`,
            ).join('')}
          </div>
          <p class="muted small">Only these four values are accepted by the firmware. Speed changes apply while printing.</p>
        </div>

        <div class="card">
          <div class="card-head"><h2>Fans</h2></div>
          ${fanSlider('model', 'Model')}
          ${fanSlider('auxiliary', 'Auxiliary')}
          ${fanSlider('chamber', 'Chamber')}
        </div>

        <div class="card">
          <div class="card-head"><h2>Heaters</h2></div>
          ${heaterMarkup('nozzle', 'Nozzle', 300)}
          ${heaterMarkup('bed', 'Bed', 110)}
          ${heaterMarkup('chamber', 'Chamber', 60)}
          <p class="muted small">0 turns a heater off. Limits: nozzle 0–300 °C, bed 0–110 °C, chamber 0–60 °C.</p>
        </div>

        <div class="card">
          <div class="card-head"><h2>Chamber light</h2></div>
          <button type="button" class="btn btn-wide" data-toggle-light data-pending-key="light" aria-pressed="false">Off</button>
        </div>

        <div class="card">
          <a class="btn btn-wide" href="#/files">Start a file…</a>
        </div>
      </section>`;
  },

  mount(root, ctx) {
    // Same camera as the Now view: watching the print while changing a heater is the
    // point of this screen. Only one view is mounted at a time, so the controller's
    // single snapshot timer serves whichever one is visible.
    ctx.camera.attach(el(root, '#camera'));

    root.addEventListener('click', (event) => {
      const cmdButton = event.target.closest('[data-cmd]');
      if (cmdButton) return handleJobCommand(cmdButton, ctx);

      const speedButton = event.target.closest('[data-speed]');
      if (speedButton) return handleSpeed(speedButton);

      const stepButton = event.target.closest('[data-step]');
      if (stepButton) return handleStep(root, stepButton);

      const setButton = event.target.closest('[data-set]');
      if (setButton) return handleHeater(root, setButton);

      const lightButton = event.target.closest('[data-toggle-light]');
      if (lightButton) return handleLight(root);
      return undefined;
    });

    root.addEventListener('change', (event) => {
      const slider = event.target.closest('[data-fan]');
      if (slider) handleFan(slider);
    });

    root.addEventListener('input', (event) => {
      const slider = event.target.closest('[data-fan]');
      if (!slider) return;
      const out = el(root, `[data-f="fan-${slider.dataset.fan}-out"]`);
      if (out) out.textContent = `${slider.value}%`;
    });
  },

  update(root, snapshot) {
    const { printer, connection, stale } = staleness(snapshot);
    root.classList.toggle('is-stale', stale);

    const control = snapshot?.capabilities?.control === true;
    const note = el(root, '[data-f="readonly-note"]');
    if (note) note.hidden = control;

    const state = printer?.state || 'unknown';
    setText(
      root,
      'control-state',
      printer?.state_label || (connection?.state === 'online' ? 'Waiting for status' : 'Printer offline'),
    );

    const hasJob = Boolean(printer?.job) || ACTIVE_STATES.has(state) || state === 'paused';

    // Every enabled/disabled decision is recomputed here on each snapshot and each
    // pending change, so a control can never get stuck disabled.
    const pauseBtn = el(root, '[data-cmd="pause"]');
    const resumeBtn = el(root, '[data-cmd="resume"]');
    const stopBtn = el(root, '[data-cmd="stop"]');
    if (pauseBtn) pauseBtn.disabled = !control || !ACTIVE_STATES.has(state) || store.isPending('pause');
    if (resumeBtn) resumeBtn.disabled = !control || state !== 'paused' || store.isPending('resume');
    if (stopBtn) stopBtn.disabled = !control || !hasJob || store.isPending('stop');

    const speedPct = printer?.job?.speed_pct ?? null;
    for (const button of root.querySelectorAll('[data-speed]')) {
      const mode = SPEED_MODES.find((m) => m.id === button.dataset.speed);
      const active = Boolean(mode && speedPct !== null && mode.pct === speedPct);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
      button.disabled = !control || store.isPending('speed');
    }

    const fans = printer?.fans || {};
    for (const key of ['model', 'auxiliary', 'chamber']) {
      const slider = el(root, `[data-fan="${key}"]`);
      const out = el(root, `[data-f="fan-${key}-out"]`);
      const value = fans[key];
      if (slider) {
        if (document.activeElement !== slider && typeof value === 'number') slider.value = String(value);
        slider.disabled = !control || store.isPending(`fan-${key}`);
      }
      if (out) out.textContent = typeof value === 'number' ? `${value}%` : '—';
    }

    const temps = printer?.temps || {};
    for (const key of ['nozzle', 'bed', 'chamber']) {
      const reading = temps[key] || {};
      setText(root, `${key}-cur`, typeof reading.actual === 'number' ? `${reading.actual.toFixed(1)} °C` : '—');
      const busy = store.isPending(`temp-${key}`);
      const input = el(root, `[data-input="${key}"]`);
      if (input) {
        if (document.activeElement !== input && typeof reading.target === 'number' && !busy) {
          input.value = String(Math.round(reading.target));
        }
        input.disabled = !control || busy;
      }
      const stepper = el(root, `[data-heater="${key}"]`);
      if (stepper) {
        for (const stepBtn of stepper.querySelectorAll('[data-step]')) stepBtn.disabled = !control || busy;
      }
      const setBtn = el(root, `[data-set="${key}"]`);
      if (setBtn) setBtn.disabled = !control || busy;
    }

    const lightBtn = el(root, '[data-toggle-light]');
    if (lightBtn) {
      const on = printer?.light === true;
      lightBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
      lightBtn.textContent = on ? 'On' : 'Off';
      lightBtn.disabled = !control || store.isPending('light');
    }

    applyPendingMarks(root);
  },
};

function fanSlider(key, label) {
  return `
    <label class="slider">
      <span class="slider-label">${label}</span>
      <input type="range" min="0" max="100" step="5" value="0"
             data-fan="${key}" data-pending-key="fan-${key}" aria-label="${label} fan speed percentage">
      <output data-f="fan-${key}-out">—</output>
    </label>`;
}

/** Visual only — the `disabled` decisions belong to each view's update(). */
function applyPendingMarks(root) {
  for (const node of root.querySelectorAll('[data-pending-key]')) {
    const busy = store.isPending(node.dataset.pendingKey);
    node.setAttribute('aria-busy', busy ? 'true' : 'false');
    node.classList.toggle('is-pending', busy);
  }
}

function handleJobCommand(button, ctx) {
  const cmd = button.dataset.cmd;
  const snapshot = store.snapshot;
  const filename = snapshot?.printer?.job?.filename || 'the current print';

  if (cmd === 'stop') {
    const ok = window.confirm(
      `Stop ${filename}?\n\nThe print is cancelled at the current layer and cannot be resumed. The part is usually scrap.`,
    );
    if (!ok) return;
  }

  const requests = {
    pause: () => api.pause(),
    resume: () => api.resume(),
    stop: () => api.stop(),
  };
  const expects = {
    pause: (snap) => ['pausing', 'paused'].includes(snap?.printer?.state),
    resume: (snap) => ['resuming', 'printing', 'preheating'].includes(snap?.printer?.state),
    stop: (snap) => ['stopped', 'idle', 'completed'].includes(snap?.printer?.state),
  };
  const labels = { pause: 'Pause', resume: 'Resume', stop: 'Stop' };
  if (!requests[cmd]) return;
  ctx.command({
    key: cmd,
    label: labels[cmd],
    request: requests[cmd],
    expect: expects[cmd],
  });
}

function handleSpeed(button) {
  const mode = SPEED_MODES.find((m) => m.id === button.dataset.speed);
  if (!mode) return;
  runCommand({
    key: 'speed',
    label: `Speed ${mode.label}`,
    request: () => api.setSpeed(mode.id),
    expect: (snap) =>
      snap?.printer?.job?.speed_pct === mode.pct || snap?.printer?.state !== 'printing',
  });
}

function handleFan(slider) {
  const key = slider.dataset.fan;
  const value = Number(slider.value);
  runCommand({
    key: `fan-${key}`,
    label: `${key} fan ${value}%`,
    request: () => api.setFans({ [key]: value }),
    expect: (snap) => snap?.printer?.fans?.[key] === value,
  });
}

function handleStep(root, button) {
  const stepper = button.closest('[data-heater]');
  if (!stepper) return;
  const input = el(stepper, 'input[type="number"]');
  if (!input) return;
  const max = Number(input.max) || 300;
  const next = Math.max(0, Math.min(max, Number(input.value || 0) + Number(button.dataset.step)));
  input.value = String(next);
}

function handleHeater(root, button) {
  const key = button.dataset.set;
  const input = el(root, `[data-input="${key}"]`);
  if (!input) return;
  const value = Number(input.value);
  if (!Number.isFinite(value) || value < 0) {
    store.toast('Enter a temperature in °C', 'error');
    return;
  }
  runCommand({
    key: `temp-${key}`,
    label: `${key} target ${Math.round(value)} °C`,
    request: () => api.setTemps({ [key]: value }),
    expect: (snap) => {
      const target = snap?.printer?.temps?.[key]?.target;
      return typeof target === 'number' && Math.abs(target - value) < 0.6;
    },
  });
}

function handleLight(root) {
  const on = store.snapshot?.printer?.light === true;
  runCommand({
    key: 'light',
    label: on ? 'Light off' : 'Light on',
    request: () => api.setLight(!on),
    expect: (snap) => snap?.printer?.light === !on,
  });
}

// --- Files ------------------------------------------------------------------

export const filesView = {
  id: 'files',

  render() {
    return `
      <section class="stack">
        <div class="card">
          <div class="card-head"><h2>Upload</h2></div>
          <div class="dropzone" id="dropzone" tabindex="0" role="button"
               aria-label="Choose a G-code file to upload, or drop one here">
            <input type="file" id="file-input" accept=".gcode,.gco,.g,.zip" hidden>
            <p class="dz-title">Choose a file or drop it here</p>
            <p class="muted small">G-code, up to 2 GiB. Uploads to the printer's internal storage.</p>
          </div>
          <div class="upload-state" id="upload-state" hidden>
            <div class="bar"><span class="fill" id="upload-fill"></span></div>
            <div class="row between small muted">
              <span id="upload-name">—</span>
              <span id="upload-meta">—</span>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>On the printer</h2>
            <button type="button" class="btn btn-small" id="files-refresh">Refresh</button>
          </div>
          <div id="files-list" class="table-wrap" aria-live="polite">
            <p class="muted">Loading…</p>
          </div>
        </div>
      </section>`;
  },

  mount(root, ctx) {
    const dropzone = el(root, '#dropzone');
    const input = el(root, '#file-input');
    const refresh = el(root, '#files-refresh');

    const load = () => loadFiles(root, ctx);

    dropzone.addEventListener('click', () => input.click());
    dropzone.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        input.click();
      }
    });
    dropzone.addEventListener('dragover', (event) => {
      event.preventDefault();
      dropzone.classList.add('is-over');
    });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('is-over'));
    dropzone.addEventListener('drop', (event) => {
      event.preventDefault();
      dropzone.classList.remove('is-over');
      const file = event.dataTransfer?.files?.[0];
      if (file) uploadFile(root, file, load);
    });
    input.addEventListener('change', () => {
      const file = input.files?.[0];
      if (file) uploadFile(root, file, load);
      input.value = '';
    });
    refresh.addEventListener('click', load);

    el(root, '#files-list')?.addEventListener('click', (event) => {
      const action = event.target.closest('[data-file-action]');
      if (!action) return;
      const name = action.dataset.name;
      if (action.dataset.fileAction === 'print') {
        const ok = window.confirm(
          `Start ${name}?\n\nThe printer will heat the nozzle and bed, home, and begin printing.`,
        );
        if (!ok) return;
        ctx.command({
          key: 'start',
          label: `Start ${name}`,
          request: () => api.printStart(name),
          expect: (snap) =>
            ['printing', 'starting', 'preheating', 'leveling', 'homing'].includes(snap?.printer?.state) &&
            snap?.printer?.job?.filename === name,
        });
      } else if (action.dataset.fileAction === 'delete') {
        const ok = window.confirm(`Delete ${name} from the printer?\n\nThis cannot be undone.`);
        if (!ok) return;
        // Deletion produces no confirming status frame, so it is a plain request + refresh
        // rather than a pending command that would time out at 6 s.
        action.disabled = true;
        api
          .deleteFiles([name])
          .then(() => {
            store.toast(`Deleted ${name}`, 'info');
            setTimeout(load, 800);
          })
          .catch((err) => {
            action.disabled = false;
            store.toast(err?.message || 'Delete failed', 'error');
          });
      }
    });

    load();
  },
};

async function loadFiles(root, ctx) {
  const target = el(root, '#files-list');
  if (!target) return;
  target.innerHTML = '<p class="muted">Loading…</p>';
  try {
    const data = await api.files();
    const files = Array.isArray(data?.files) ? data.files : [];
    if (!files.length) {
      target.innerHTML = '<p class="muted">No files on the printer yet.</p>';
      return;
    }
    const readOnly = ctx.readOnly;
    target.innerHTML = `
      <table class="table">
        <thead>
          <tr><th scope="col">File</th><th scope="col">Size</th><th scope="col">Layers</th><th scope="col">Actions</th></tr>
        </thead>
        <tbody>
          ${files
            .map(
              (file) => `
            <tr>
              <td class="cell-name">${esc(file.filename)}</td>
              <td class="mono">${fmtBytes(file.size)}</td>
              <td class="mono">${file.layers ?? '—'}</td>
              <td class="cell-actions">
                <button type="button" class="btn btn-small" data-file-action="print" data-name="${esc(file.filename)}" ${readOnly ? 'disabled' : ''}>Print</button>
                <button type="button" class="btn btn-small btn-danger" data-file-action="delete" data-name="${esc(file.filename)}" ${readOnly ? 'disabled' : ''}>Delete</button>
              </td>
            </tr>`,
            )
            .join('')}
        </tbody>
      </table>`;
  } catch (err) {
    target.innerHTML = `<p class="error-text">${esc(err.message || 'Could not load files')}</p>`;
  }
}

function uploadFile(root, file, onDone) {
  const state = el(root, '#upload-state');
  const fill = el(root, '#upload-fill');
  const name = el(root, '#upload-name');
  const meta = el(root, '#upload-meta');

  if (state) state.hidden = false;
  if (name) name.textContent = file.name;
  if (meta) meta.textContent = `${fmtBytes(file.size)} · sending to the Pi…`;
  if (fill) fill.style.width = '0%';

  api
    .uploadFile(file, {
      onProgress: (sent, total) => {
        if (!total) return;
        const pct = Math.round((sent / total) * 100);
        if (fill) fill.style.width = `${pct}%`;
        if (meta) meta.textContent = `${fmtBytes(sent)} / ${fmtBytes(total)} · ${pct}% to the Pi`;
      },
    })
    .then((result) => {
      if (meta) meta.textContent = 'Queued on the printer — finishing transfer…';
      store.toast(`Uploaded ${file.name}`, 'info');
      pollTransfer(result?.upload_id, meta, fill, onDone);
    })
    .catch((err) => {
      if (fill) fill.style.width = '0%';
      if (meta) meta.textContent = 'Upload failed';
      store.toast(err.message || 'Upload failed', 'error');
      if (state) state.hidden = true;
    });
}

function pollTransfer(id, meta, fill, onDone) {
  if (!id) {
    if (meta) meta.textContent = 'Done';
    if (fill) fill.style.width = '100%';
    onDone?.();
    return;
  }
  let attempts = 0;
  const timer = setInterval(async () => {
    attempts += 1;
    if (attempts > 400) {
      clearInterval(timer);
      return;
    }
    try {
      const list = await api.uploads();
      const transfer = (list?.uploads || []).find((item) => item.id === id);
      if (!transfer) return;
      const pct = transfer.total_bytes ? Math.round((transfer.bytes_sent / transfer.total_bytes) * 100) : 0;
      if (fill) fill.style.width = `${pct}%`;
      if (meta) {
        meta.textContent =
          transfer.state === 'error'
            ? `Printer rejected the file: ${transfer.error || 'unknown error'}`
            : `${fmtBytes(transfer.bytes_sent)} / ${fmtBytes(transfer.total_bytes)} · ${pct}% on the printer`;
      }
      if (transfer.state === 'done' || transfer.state === 'error') {
        clearInterval(timer);
        if (transfer.state === 'done') store.toast(`Transfer complete: ${transfer.filename}`, 'info');
        else store.toast(transfer.error || 'Transfer failed', 'error');
        onDone?.();
      }
    } catch {
      clearInterval(timer);
    }
  }, 1500);
}

// --- History ----------------------------------------------------------------

const HISTORY_STATUS = { 1: 'Completed', 2: 'Cancelled', 3: 'Stopped' };

export const historyView = {
  id: 'history',

  render() {
    return `
      <section class="stack">
        <div class="card">
          <div class="card-head">
            <h2>Print history</h2>
            <button type="button" class="btn btn-small" id="history-refresh">Refresh</button>
          </div>
          <div id="history-list" class="table-wrap" aria-live="polite">
            <p class="muted">Loading…</p>
          </div>
        </div>
      </section>`;
  },

  mount(root) {
    const load = async () => {
      const target = el(root, '#history-list');
      if (!target) return;
      target.innerHTML = '<p class="muted">Loading…</p>';
      try {
        const data = await api.history();
        const jobs = Array.isArray(data?.jobs) ? [...data.jobs] : [];
        jobs.sort((a, b) => (toSeconds(b.started_ts) || 0) - (toSeconds(a.started_ts) || 0));
        if (!jobs.length) {
          target.innerHTML = '<p class="muted">No finished jobs recorded.</p>';
          return;
        }
        target.innerHTML = `
          <table class="table">
            <thead><tr><th scope="col">Job</th><th scope="col">Result</th><th scope="col">Started</th><th scope="col">Duration</th></tr></thead>
            <tbody>
              ${jobs
                .map((job) => {
                  const started = toSeconds(job.started_ts);
                  const ended = toSeconds(job.ended_ts);
                  const duration = started && ended ? ended - started : null;
                  const status = HISTORY_STATUS[job.status] || `Status ${job.status ?? '—'}`;
                  return `
                    <tr>
                      <td class="cell-name">${esc(job.name || '—')}</td>
                      <td>${esc(status)}</td>
                      <td class="mono">${started ? fmtDate(started) : '—'}</td>
                      <td class="mono">${duration ? fmtDuration(duration) : '—'}</td>
                    </tr>`;
                })
                .join('')}
            </tbody>
          </table>`;
      } catch (err) {
        target.innerHTML = `<p class="error-text">${esc(err.message || 'Could not load history')}</p>`;
      }
    };
    el(root, '#history-refresh')?.addEventListener('click', load);
    load();
  },
};

// --- About ------------------------------------------------------------------

export const aboutView = {
  id: 'about',

  render() {
    return `
      <section class="stack">
        <div class="card">
          <div class="card-head"><h2>Connection</h2></div>
          <dl class="kv">
            <div><dt>Printer</dt><dd data-f="about-host">—</dd></div>
            <div><dt>Firmware</dt><dd data-f="about-firmware">—</dd></div>
            <div><dt>Mainboard ID</dt><dd class="mono" data-f="about-mainboard">—</dd></div>
            <div><dt>Link</dt><dd data-f="about-link">—</dd></div>
            <div><dt>Reconnects</dt><dd data-f="about-reconnects">—</dd></div>
            <div><dt>Last frame</dt><dd data-f="about-frame">—</dd></div>
            <div><dt>Last error</dt><dd data-f="about-error">—</dd></div>
          </dl>
        </div>

        <div class="card">
          <div class="card-head"><h2>Capabilities</h2></div>
          <dl class="kv">
            <div><dt>Control</dt><dd data-f="about-control">—</dd></div>
            <div><dt>Camera</dt><dd data-f="about-camera">—</dd></div>
            <div><dt>Files</dt><dd data-f="about-files">—</dd></div>
            <div><dt>Upload</dt><dd data-f="about-upload">—</dd></div>
            <div><dt>History</dt><dd data-f="about-history">—</dd></div>
          </dl>
        </div>

        <div class="card">
          <div class="card-head"><h2>Activity</h2></div>
          <ol class="log" id="about-log">
            <li class="muted">Nothing yet.</li>
          </ol>
        </div>

        <div class="card">
          <button type="button" class="btn btn-wide" id="signout">Sign out</button>
        </div>
      </section>`;
  },

  mount(root) {
    el(root, '#signout')?.addEventListener('click', async () => {
      try {
        await api.logout();
      } finally {
        window.location.reload();
      }
    });
  },

  update(root, snapshot) {
    const connection = snapshot?.connection || {};
    const capabilities = snapshot?.capabilities || {};

    setText(root, 'about-host', connection.printer_host || '—');
    setText(root, 'about-firmware', connection.firmware || '—');
    setText(root, 'about-mainboard', connection.mainboard_id || '—');
    setText(
      root,
      'about-link',
      connection.state === 'online'
        ? `Online since ${fmtClock(connection.since_ts)}`
        : connection.state === 'connecting'
          ? 'Connecting'
          : 'Offline',
    );
    setText(root, 'about-reconnects', connection.reconnects ?? '—');
    setText(
      root,
      'about-frame',
      connection.last_frame_age_s === null || connection.last_frame_age_s === undefined
        ? '—'
        : `${connection.last_frame_age_s}s ago`,
    );
    setText(root, 'about-error', connection.last_error || 'none');

    const yesNo = (value) => (value ? 'Enabled' : 'Disabled');
    setText(root, 'about-control', yesNo(capabilities.control));
    setText(root, 'about-camera', yesNo(capabilities.camera));
    setText(root, 'about-files', yesNo(capabilities.files));
    setText(root, 'about-upload', yesNo(capabilities.upload));
    setText(root, 'about-history', yesNo(capabilities.history));

    const log = el(root, '#about-log');
    if (!log) return;
    const entries = Array.isArray(snapshot?.log) ? snapshot.log : [];
    if (!entries.length) {
      log.innerHTML = '<li class="muted">Nothing yet.</li>';
      return;
    }
    log.innerHTML = entries
      .map(
        (entry) => `
        <li class="log-item" data-level="${esc(entry.level || 'info')}">
          <span class="log-time mono">${fmtAgo(entry.ts)}</span>
          <span class="log-level">${esc(entry.level === 'error' ? 'error' : entry.level === 'warn' ? 'warn' : 'info')}</span>
          <span class="log-msg">${esc(entry.message)}</span>
        </li>`,
      )
      .join('');
  },
};

export const views = {
  now: nowView,
  control: controlView,
  files: filesView,
  history: historyView,
  about: aboutView,
};
