/**
 * Application state: bootstrap payload, global date range, user preferences.
 * Only non-sensitive UI preferences are persisted to localStorage (never tokens).
 */
import { addDays, parseDate, isoDate } from './format.js';

const PREFS_KEY = 'cp.prefs';
const listeners = new Map();

export const store = {
  boot: null,
  range: { preset: '14d', start: null, end: null },
  granularity: 'day',
  project: 'all',
  model: null,
  locale: 'en',
  sidebarCollapsed: false,
  lastUpdated: null,
};

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event).delete(fn);
}

export function emit(event, payload) {
  const set = listeners.get(event);
  if (set) for (const fn of [...set]) {
    try { fn(payload); } catch (e) { /* listener errors must not break the app */ }
  }
}

function readPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch (e) {
    return {};
  }
}

export function savePrefs() {
  try {
    const prefs = {
      preset: store.range.preset,
      start: store.range.preset === 'custom' ? store.range.start : null,
      end: store.range.preset === 'custom' ? store.range.end : null,
      granularity: store.granularity,
      project: store.project,
      model: store.model,
      locale: store.locale,
      sidebarCollapsed: store.sidebarCollapsed,
    };
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch (e) { /* storage unavailable: preferences stay in memory */ }
}

export const PRESETS = ['7d', '14d', '30d', '90d', 'mtd', 'prev_month'];

export function today() {
  return (store.boot && store.boot.workspace && store.boot.workspace.today) || isoDate(new Date());
}

export function computeRange(preset, anchor = today()) {
  switch (preset) {
    case '7d': return { start: addDays(anchor, -6), end: anchor };
    case '14d': return { start: addDays(anchor, -13), end: anchor };
    case '30d': return { start: addDays(anchor, -29), end: anchor };
    case '90d': return { start: addDays(anchor, -89), end: anchor };
    case 'mtd': return { start: `${anchor.slice(0, 8)}01`, end: anchor };
    case 'prev_month': {
      const first = parseDate(`${anchor.slice(0, 8)}01`);
      first.setUTCDate(0); // last day of previous month
      const end = isoDate(first);
      return { start: `${end.slice(0, 8)}01`, end };
    }
    default: return null;
  }
}

export function initState(boot) {
  store.boot = boot;
  const prefs = readPrefs();
  const defaultDays = (boot.workspace && boot.workspace.defaultRangeDays) || 14;
  const defaultPreset = [7, 14, 30, 90].includes(defaultDays) ? `${defaultDays}d` : '14d';
  store.locale = prefs.locale || (boot.branding && boot.branding.defaultLocale) || 'en';
  store.granularity = prefs.granularity === 'week' ? 'week' : 'day';
  store.sidebarCollapsed = Boolean(prefs.sidebarCollapsed);
  const wsProjects = (boot.workspace && Array.isArray(boot.workspace.projects) && boot.workspace.projects) || [];
  store.project = prefs.project && prefs.project !== 'all' && wsProjects.includes(prefs.project) ? prefs.project : 'all';
  const allowed = (boot.aiEngine && boot.aiEngine.models) || [];
  store.model = prefs.model && allowed.includes(prefs.model) ? prefs.model : (boot.aiEngine && boot.aiEngine.defaultModel) || null;

  if (prefs.preset === 'custom' && parseDate(prefs.start) && parseDate(prefs.end) && prefs.start <= prefs.end) {
    store.range = { preset: 'custom', start: prefs.start, end: prefs.end };
  } else {
    const preset = PRESETS.includes(prefs.preset) ? prefs.preset : defaultPreset;
    store.range = { preset, ...computeRange(preset) };
  }
}

export function setRange(preset, start, end) {
  if (preset === 'custom') {
    store.range = { preset, start, end };
  } else {
    store.range = { preset, ...computeRange(preset) };
  }
  savePrefs();
  emit('range', store.range);
}

export function setGranularity(g) {
  store.granularity = g === 'week' ? 'week' : 'day';
  savePrefs();
  emit('range', store.range);
}

export function setProject(project) {
  store.project = project || 'all';
  savePrefs();
  emit('project', store.project);
  emit('range', store.range);
}

export function setModel(model) {
  store.model = model;
  savePrefs();
  emit('model', model);
}

export function rangeParams(extra = {}) {
  const base = { start: store.range.start, end: store.range.end, granularity: store.granularity };
  if (store.project && store.project !== 'all') base.project = store.project;
  return { ...base, ...extra };
}

export function isModuleEnabled(id) {
  const mods = (store.boot && store.boot.modules) || [];
  const m = mods.find((x) => x.id === id);
  return m ? Boolean(m.enabled) : false;
}

export function session() {
  return (store.boot && store.boot.session) || { role: 'viewer', canWrite: false };
}

export function isAdmin() {
  const s = session();
  return s.role === 'admin' && s.canWrite !== false;
}

export function currency() {
  return store.currency || 'USD';
}
