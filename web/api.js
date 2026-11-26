/**
 * Fetch wrapper + the bridge's HTTP surface (see docs/API.md).
 *
 * Every failure becomes an ApiError carrying the server's `detail` text, because the
 * server's message is the only thing worth showing the user.
 */

export class ApiError extends Error {
  constructor(message, status = 0, detail = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, { method = 'GET', body, signal } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError('Bridge unreachable — check that the Pi is online.', 0);
  }

  if (res.status === 204) return null;

  const text = await res.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!res.ok) {
    const raw = payload && (payload.detail ?? payload.message);
    const message =
      typeof raw === 'string'
        ? raw
        : raw
          ? JSON.stringify(raw)
          : `Request failed (HTTP ${res.status})`;
    throw new ApiError(message, res.status, raw ?? null);
  }

  return payload;
}

export const api = {
  // State
  state: () => request('/api/state'),
  diagnostics: () => request('/api/diagnostics'),
  usage: () => request('/api/usage'),
  resetMaintenance: () => request('/api/maintenance/reset', { method: 'POST' }),

  events() {
    return new EventSource('/api/events');
  },

  // Auth
  login: (password) => request('/api/login', { method: 'POST', body: { password } }),
  logout: () => request('/api/logout', { method: 'POST' }),

  // Job control
  printStart: (filename, { storage = 'local', autoLeveling = true, timelapse = false } = {}) =>
    request('/api/print/start', {
      method: 'POST',
      body: {
        filename,
        storage,
        auto_leveling: autoLeveling,
        timelapse,
      },
    }),
  pause: () => request('/api/print/pause', { method: 'POST' }),
  resume: () => request('/api/print/resume', { method: 'POST' }),
  stop: () => request('/api/print/stop', { method: 'POST' }),
  setSpeed: (mode) => request('/api/print/speed', { method: 'POST', body: { mode } }),
  setFans: (fans) => request('/api/print/fans', { method: 'POST', body: fans }),
  setTemps: (temps) => request('/api/print/temps', { method: 'POST', body: temps }),
  setLight: (on) => request('/api/print/light', { method: 'POST', body: { on } }),

  // Files
  files: (storage = 'local') => request(`/api/files?storage=${encodeURIComponent(storage)}`),
  deleteFiles: (filenames, storage = 'local') =>
    request('/api/files/delete', { method: 'POST', body: { filenames, storage } }),
  history: () => request('/api/history'),
  uploads: () => request('/api/uploads'),
  cancelUpload: (id) => request(`/api/uploads/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /**
   * Upload a file with real progress. fetch() cannot report upload progress, so this
   * one route uses XHR. Resolves with the server's JSON, rejects with ApiError.
   */
  uploadFile(file, { onProgress, start = false } = {}) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append('file', file, file.name);
      if (start) form.append('start', 'true');

      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/uploads');
      xhr.withCredentials = true;
      xhr.responseType = 'text';

      xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable && onProgress) onProgress(event.loaded, event.total);
      });

      xhr.addEventListener('load', () => {
        let payload = null;
        try {
          payload = xhr.responseText ? JSON.parse(xhr.responseText) : null;
        } catch {
          payload = null;
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload);
          return;
        }
        const raw = payload && (payload.detail ?? payload.message);
        const message =
          typeof raw === 'string'
            ? raw
            : raw
              ? JSON.stringify(raw)
              : `Upload failed (HTTP ${xhr.status})`;
        reject(new ApiError(message, xhr.status, raw ?? null));
      });

      xhr.addEventListener('error', () => reject(new ApiError('Upload failed — network error.', 0)));
      xhr.addEventListener('abort', () => reject(new ApiError('Upload cancelled.', 0, 'aborted')));

      xhr.send(form);
    });
  },
};
