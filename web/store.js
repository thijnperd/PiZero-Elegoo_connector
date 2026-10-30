/**
 * The dashboard's single store.
 *
 * One snapshot from the bridge, a connection state, a set of in-flight commands and a
 * toast queue. Views subscribe once and re-read what they need; nothing else holds state.
 *
 * Pending commands are the defence against optimistic UI: a control is marked "sending"
 * when the request leaves, and is only cleared when the *printer* reports the new value
 * (an `expect` predicate runs against each incoming snapshot) or the request fails.
 */

const listeners = new Set();
let toastId = 0;

const PENDING_TIMEOUT_MS = 6000;

export const store = {
  /** Latest snapshot from GET /api/state or the SSE stream. */
  snapshot: null,
  /** 'live' | 'reconnecting' | 'offline' */
  connection: 'connecting',
  /** Active view id. */
  view: 'now',
  /** key -> { label, expect, started, timer } */
  pending: new Map(),
  /** [{ id, message, level }] */
  toasts: [],
  /** Last log timestamp we have already accounted for. */
  _seenLogTs: 0,

  subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },

  emit(reason = 'state') {
    for (const fn of [...listeners]) {
      try {
        fn(this, reason);
      } catch (err) {
        console.error('listener failed', err);
      }
    }
  },

  setSnapshot(snapshot) {
    this.snapshot = snapshot;
    if (this.connection !== 'offline') {
      this.connection = snapshot?.connection?.state === 'online' ? 'live' : 'reconnecting';
    }
    this._settlePending(snapshot);
    this._settleToasts(snapshot);
    this.emit('state');
  },

  setConnection(state, { reason = 'stream' } = {}) {
    if (this.connection === state) return;
    this.connection = state;
    this.emit(reason);
  },

  // --- pending commands ---------------------------------------------------

  /**
   * Mark a command in flight.
   * @param {string} key  stable id, also used by views to disable that control
   * @param {{label: string, expect?: (snapshot: object) => boolean}} options
   */
  startPending(key, { label, expect } = {}) {
    this.cancelPending(key, { silent: true });
    const entry = {
      label: label || key,
      expect: typeof expect === 'function' ? expect : null,
      started: Date.now(),
      timer: null,
    };
    entry.timer = setTimeout(() => {
      if (!this.pending.has(key)) return;
      this.pending.delete(key);
      this.toast(`${entry.label}: no confirmation from the printer`, 'error');
      this.emit('pending');
    }, PENDING_TIMEOUT_MS);
    this.pending.set(key, entry);
    this.emit('pending');
  },

  /** Clear a pending command without a message (confirmed or superseded). */
  cancelPending(key, { silent = false } = {}) {
    const entry = this.pending.get(key);
    if (!entry) return;
    clearTimeout(entry.timer);
    this.pending.delete(key);
    if (!silent) this.emit('pending');
  },

  /** Fail a pending command and surface the server's message. */
  failPending(key, message) {
    const entry = this.pending.get(key);
    if (entry) {
      clearTimeout(entry.timer);
      this.pending.delete(key);
    }
    this.toast(message || 'Command failed', 'error');
    this.emit('pending');
  },

  isPending(key) {
    return this.pending.has(key);
  },

  _settlePending(snapshot) {
    if (!this.pending.size || !snapshot) return;
    let changed = false;
    for (const [key, entry] of [...this.pending]) {
      let confirmed = false;
      if (entry.expect) {
        try {
          confirmed = Boolean(entry.expect(snapshot));
        } catch {
          confirmed = false;
        }
      }
      // A printer-reported error after the request also settles it (as a failure).
      if (!confirmed && this._logHasErrorSince(entry.started, snapshot)) {
        clearTimeout(entry.timer);
        this.pending.delete(key);
        this.toast(`${entry.label}: the printer reported an error`, 'error');
        changed = true;
        continue;
      }
      if (confirmed) {
        clearTimeout(entry.timer);
        this.pending.delete(key);
        changed = true;
      }
    }
    if (changed) this.emit('pending');
  },

  _logHasErrorSince(sinceMs, snapshot) {
    const log = snapshot?.log;
    if (!Array.isArray(log)) return false;
    const sinceS = sinceMs / 1000 - 0.5;
    return log.some((entry) => entry.ts >= sinceS && entry.level === 'error');
  },

  // --- toasts -------------------------------------------------------------

  toast(message, level = 'info', ttl = 5000) {
    if (!message) return;
    const id = ++toastId;
    this.toasts.push({ id, message: String(message), level });
    this.emit('toast');
    if (ttl > 0) {
      setTimeout(() => this.dismissToast(id), ttl);
    }
  },

  dismissToast(id) {
    const index = this.toasts.findIndex((t) => t.id === id);
    if (index === -1) return;
    this.toasts.splice(index, 1);
    this.emit('toast');
  },

  _settleToasts() {
    // Reserved: future transition-driven toasts (job finished, error) hook in here.
  },
};

/**
 * Run a command through the pending model.
 * @param {{key: string, label: string, request: () => Promise<any>, expect?: Function}} spec
 */
export function runCommand({ key, label, request, expect }) {
  store.startPending(key, { label, expect });
  return request().catch((err) => {
    store.failPending(key, err?.message || `${label} failed`);
  });
}
