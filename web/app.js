/**
 * Entry point: boot, routing, the SSE link, the camera controller and toasts.
 */

import { api, ApiError } from './api.js';
import { store } from './store.js';
import { views, esc as escapeHtml } from './views.js';

const root = document.getElementById('view');
const banner = document.getElementById('banner');
const bannerText = document.getElementById('banner-text');
const notice = document.getElementById('notice');
const noticeText = document.getElementById('notice-text');
const noticeDismiss = document.getElementById('notice-dismiss');
const tabs = document.getElementById('tabs');
const topbarConn = document.getElementById('topbar-conn-text');
const topbarChip = document.querySelector('.conn-chip');
const toastLayer = document.getElementById('toasts');

let currentView = null;
let currentRoot = null;
let source = null;
let reconnectAttempts = 0;
let reconnectTimer = null;

// --- camera -----------------------------------------------------------------

const camera = {
  container: null,
  img: null,
  empty: null,
  note: null,
  toggle: null,
  modeBadge: null,
  snapshotTimer: null,
  mode: 'off',
  frameAt: 0,
  failures: 0,

  attach(container) {
    this.detach();
    if (!container) return;
    this.container = container;
    this.img = container.querySelector('#camera-img');
    this.empty = container.querySelector('#camera-empty');
    this.note = container.querySelector('#camera-note');
    this.toggle = container.querySelector('#camera-toggle');
    this.modeBadge = container.querySelector('#camera-mode');

    this.toggle?.addEventListener('click', () => {
      if (this.mode === 'live') this.startSnapshots();
      else this.startLive();
    });

    document.addEventListener('visibilitychange', this.onVisibility);

    const snapshot = store.snapshot;
    if (!snapshot?.capabilities?.camera) {
      this.showUnavailable('Camera not available on this bridge');
      return;
    }
    this.startSnapshots();
  },

  detach() {
    this.stopTimers();
    document.removeEventListener('visibilitychange', this.onVisibility);
    this.container = null;
    this.img = null;
    this.empty = null;
    this.note = null;
    this.toggle = null;
    this.modeBadge = null;
    this.mode = 'off';
  },

  onVisibility: () => {
    if (document.hidden && camera.mode === 'live') {
      camera.startSnapshots();
      camera.showNote('Live view paused while the tab is hidden');
    }
  },

  stopTimers() {
    if (this.snapshotTimer) {
      clearInterval(this.snapshotTimer);
      this.snapshotTimer = null;
    }
  },

  showNote(text) {
    if (this.note) this.note.textContent = text;
  },

  showUnavailable(message) {
    this.stopTimers();
    this.mode = 'unavailable';
    if (this.img) {
      this.img.hidden = true;
      this.img.removeAttribute('src');
    }
    if (this.empty) {
      this.empty.hidden = false;
      this.empty.innerHTML = `<span>${message}</span><small>The rest of the dashboard still works.</small>`;
    }
    if (this.modeBadge) this.modeBadge.hidden = true;
    if (this.toggle) this.toggle.hidden = true;
  },

  startSnapshots() {
    this.stopTimers();
    this.mode = 'snapshot';
    this.failures = 0;
    if (this.toggle) this.toggle.textContent = 'Go live';
    if (this.modeBadge) this.modeBadge.hidden = true;
    if (this.img) {
      this.img.alt = 'Snapshot from the printer camera';
      this.img.hidden = false;
    }
    if (this.empty) this.empty.hidden = true;
    this.showNote('Snapshot every 3 s');
    this.grabSnapshot();
    this.snapshotTimer = setInterval(() => this.grabSnapshot(), 3000);
  },

  grabSnapshot() {
    const img = this.img;
    if (!img) return;

    img.onload = () => {
      camera.failures = 0;
      camera.frameAt = Date.now();
      img.hidden = false;
      if (camera.empty) camera.empty.hidden = true;
      camera.showNote('Snapshot every 3 s');
    };
    img.onerror = () => {
      camera.failures += 1;
      if (camera.failures >= 2) {
        img.hidden = true;
        if (camera.empty) {
          camera.empty.hidden = false;
          camera.empty.innerHTML =
            '<span>Camera unreachable</span><small>Retrying — printing and controls are unaffected.</small>';
        }
        camera.showNote('No frames');
      }
    };
    // One request per tick: assigning to the live <img> is the fetch.
    img.src = `/api/camera/snapshot?t=${Date.now()}`;
  },

  startLive() {
    if (!store.snapshot?.capabilities?.camera) {
      store.toast('Camera is disabled for this bridge', 'error');
      return;
    }
    this.stopTimers();
    this.mode = 'live';
    if (this.toggle) this.toggle.textContent = 'Stop live';
    if (this.modeBadge) this.modeBadge.hidden = false;
    if (this.empty) this.empty.hidden = true;
    if (this.img) {
      this.img.alt = 'Live view from the printer camera';
      this.img.hidden = false;
      this.img.src = `/api/camera/stream?t=${Date.now()}`;
    }
    this.showNote('Live stream — on mobile data this is roughly 2–3 GB per hour');
  },
};

// --- commands ---------------------------------------------------------------

function command({ key, label, request, expect }) {
  store.startPending(key, { label, expect });
  request().catch((err) => {
    const detail = err instanceof ApiError ? err.message : String(err?.message || err);
    store.failPending(key, detail);
  });
}

function navigate(id) {
  const target = views[id] ? id : 'now';
  if (currentViewId() === target) renderView('route');
  else window.location.hash = `#/${target}`;
}

const ctx = { camera, command, readOnly: false, navigate };

// --- rendering --------------------------------------------------------------

function currentViewId() {
  const hash = window.location.hash.replace(/^#\/?/, '');
  const id = hash.split('?')[0].split('/')[0] || 'now';
  return views[id] ? id : 'now';
}

function applySnapshotToView() {
  if (!currentRoot || !currentView) return;
  currentView.update?.(currentRoot, store.snapshot);
}

function renderView(reason) {
  const id = currentViewId();
  const changed = id !== currentView?.id;
  // The first paint is not a navigation, so it must not pull focus away from the page.
  const initial = !currentView;
  if (changed) {
    camera.detach();
    currentView = views[id];
    store.view = id;
    root.innerHTML = currentView.render();
    currentRoot = root;
    for (const link of tabs.querySelectorAll('a[data-view]')) {
      const active = link.dataset.view === id;
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    ctx.readOnly = store.snapshot?.capabilities?.control !== true;
    currentView.mount?.(root, ctx);
    applySnapshotToView();
    window.scrollTo(0, 0);
    // A hash change swaps the whole view; without this, focus stays on the tab link and
    // the next Tab press walks the old page. #view already has tabindex="-1".
    if (!initial && reason === 'route') root.focus({ preventScroll: true });
  } else if (reason !== 'pending') {
    applySnapshotToView();
  }

  const snapshot = store.snapshot;
  ctx.readOnly = snapshot?.capabilities?.control !== true;
  if (id === 'control' && reason === 'pending') applySnapshotToView();
}

/**
 * The maintenance reminder, on every screen instead of only in About.
 * Dismissal is keyed to the last reset timestamp: acknowledging one reminder does
 * not silence the next one after the counter has been reset.
 */
const NOTICE_KEY = 'ec.notice.dismissed';

function paintNotice(snapshot) {
  if (!notice) return;
  const maintenance = snapshot?.usage?.maintenance;
  let dismissed = '';
  try {
    dismissed = localStorage.getItem(NOTICE_KEY) || '';
  } catch {
    dismissed = '';
  }
  const stamp = String(maintenance?.last_reset_ts ?? 0);
  const due = Boolean(maintenance?.due) && dismissed !== stamp;
  notice.hidden = !due;
  if (!due || !noticeText) return;
  const hours = Number(maintenance.hours_since ?? 0).toFixed(1);
  noticeText.textContent = `Service due — ${hours} h since the last service (reminder every ${maintenance.interval_hours} h).`;
}

noticeDismiss?.addEventListener('click', () => {
  const maintenance = store.snapshot?.usage?.maintenance;
  try {
    localStorage.setItem(NOTICE_KEY, String(maintenance?.last_reset_ts ?? 0));
  } catch {
    /* private mode: the banner simply comes back next load */
  }
  paintNotice(store.snapshot);
});

function paintConnection() {
  const connection = store.connection;
  const snapshot = store.snapshot;
  const labels = { live: 'Live', reconnecting: 'Reconnecting…', offline: 'Offline', connecting: 'Connecting…' };
  const text = labels[connection] || 'Offline';

  topbarConn.textContent = text;
  if (topbarChip) topbarChip.dataset.conn = connection;

  const printerState = snapshot?.printer?.state_label;
  const showBanner = connection !== 'live';
  banner.hidden = !showBanner;
  banner.dataset.conn = connection;
  if (showBanner) {
    // How old the numbers are matters more than the fact that the link is down: a
    // frozen temperature that looks live is the one thing worth spelling out.
    const rawAge = snapshot?.connection?.last_frame_age_s;
    const age = Number(rawAge);
    // Number(null) is 0, which would read as "just now" for a frame we know nothing about.
    const ageText =
      rawAge != null && Number.isFinite(age)
        ? age < 60
          ? `${Math.round(age)} s`
          : `${Math.round(age / 60)} min`
        : '';
    const ageNote = ageText ? ` Last update ${ageText} ago.` : '';
    bannerText.textContent =
      connection === 'offline'
        ? `No contact with the bridge${printerState ? ` — last known: ${printerState}` : ''}. Values below are stale.${ageNote}`
        : connection === 'reconnecting'
          ? `Reconnecting to the bridge — showing the last known values.${ageNote}`
          : 'Connecting to the bridge…';
  }

  paintNotice(snapshot);
}

function paintToasts() {
  if (!toastLayer) return;
  toastLayer.innerHTML = store.toasts
    .map(
      (toast) => `
      <div class="toast" data-level="${toast.level}" role="status">
        <span class="toast-msg">${escapeHtml(toast.message)}</span>
        <button type="button" class="toast-close" data-toast-id="${toast.id}" aria-label="Dismiss">×</button>
      </div>`,
    )
    .join('');
}

toastLayer?.addEventListener('click', (event) => {
  const button = event.target.closest('[data-toast-id]');
  if (!button) return;
  store.dismissToast(Number(button.dataset.toastId));
});

// --- live link --------------------------------------------------------------

function connect() {
  if (source) {
    source.close();
    source = null;
  }
  clearTimeout(reconnectTimer);

  source = api.events();
  source.onopen = () => {
    reconnectAttempts = 0;
    store.setConnection('live');
  };
  source.onmessage = (event) => {
    if (!event.data) return;
    let snapshot;
    try {
      snapshot = JSON.parse(event.data);
    } catch {
      return;
    }
    lastMessageAt = Date.now();
    store.setConnection(snapshot?.connection?.state === 'online' ? 'live' : store.connection);
    store.setSnapshot(snapshot);
  };
  source.onerror = () => {
    source?.close();
    source = null;
    reconnectAttempts += 1;
    const offline = reconnectAttempts >= 3;
    store.setConnection(offline ? 'offline' : 'reconnecting');
    const delay = Math.min(1000 * 2 ** Math.min(reconnectAttempts, 5), 20000);
    reconnectTimer = setTimeout(connect, delay * (0.7 + Math.random() * 0.6));
  };
}

let lastMessageAt = 0;
let watchdog = null;

function startWatchdog() {
  watchdog = setInterval(() => {
    if (!source) return;
    if (lastMessageAt && Date.now() - lastMessageAt > 30000) {
      // The stream is open but silent: the bridge sends a heartbeat every 15 s, so this
      // means the link is wedged rather than idle.
      source.close();
      source = null;
      store.setConnection('reconnecting');
      connect();
    }
  }, 15000);
}

// --- boot -------------------------------------------------------------------

async function boot() {
  store.subscribe((_, reason) => {
    if (reason === 'state') {
      renderView('state');
      paintConnection();
    } else if (reason === 'pending') {
      renderView('pending');
    } else if (reason === 'toast') {
      paintToasts();
    } else {
      paintConnection();
      applySnapshotToView();
    }
  });

  window.addEventListener('hashchange', () => {
    if (currentViewId() !== currentView?.id) renderView('route');
  });

  try {
    const snapshot = await api.state();
    store.setSnapshot(snapshot);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      window.location.href = '/login';
      return;
    }
    store.setConnection('reconnecting');
    store.toast(err.message || 'Could not reach the bridge', 'error');
  }

  ctx.readOnly = store.snapshot?.capabilities?.control !== true;
  renderView('boot');
  paintConnection();
  paintToasts();

  connect();
  startWatchdog();

  // Keep relative times ("2m ago") honest without re-rendering the world.
  setInterval(() => {
    if (currentView?.id === 'about') applySnapshotToView();
  }, 30000);

  registerServiceWorker();
}

function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('./sw.js').catch(() => {
      /* offline shell is a nicety; ignore failures (e.g. plain http on a LAN) */
    });
  });
}

boot();
