/**
 * Same-origin JSON API client. Adds the CSRF header to state-changing requests.
 * The CSRF token is held in memory only (never persisted).
 */

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message || code || 'Request failed');
    this.name = 'ApiError';
    this.status = status;
    this.code = code || 'internal';
  }
}

let csrfToken = null;
let onUnauthenticated = null;

export function setCsrfToken(token) {
  csrfToken = token || null;
}

export function setUnauthenticatedHandler(fn) {
  onUnauthenticated = fn;
}

async function request(method, path, { params, body, signal } = {}) {
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/')) {
    throw new ApiError(0, 'bad_request', 'Refusing a request outside the application API.');
  }
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
    }
  }
  const init = {
    method,
    credentials: 'same-origin',
    cache: 'no-store',
    signal,
    headers: { Accept: 'application/json' },
  };
  if (method === 'POST') {
    init.headers['Content-Type'] = 'application/json';
    init.headers['X-CSRF-Token'] = csrfToken || '';
    init.body = JSON.stringify(body || {});
  }

  let res;
  try {
    res = await fetch(url.toString(), init);
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError(0, 'network', 'The server could not be reached.');
  }

  let json = null;
  try {
    json = await res.json();
  } catch (e) {
    json = null;
  }

  if (!res.ok) {
    const e = (json && json.error) || {};
    const error = new ApiError(res.status, e.code || (res.status === 429 ? 'rate_limited' : 'internal'), e.message || res.statusText);
    if (res.status === 401 && onUnauthenticated) onUnauthenticated(error);
    throw error;
  }
  if (!json || typeof json !== 'object') throw new ApiError(res.status, 'internal', 'Malformed response');
  return { data: json.data, meta: json.meta || { warnings: [] } };
}

export const api = {
  get: (path, params, opts = {}) => request('GET', path, { params, signal: opts.signal }),
  post: (path, body, opts = {}) => request('POST', path, { body, signal: opts.signal }),
};

export function isAbort(err) {
  return err && err.name === 'AbortError';
}
