/**
 * Locale-aware formatting helpers. All API values arrive as raw numbers / ISO strings.
 */
import { getLocale, t } from './i18n.js';

let timezone = 'UTC';
const cache = new Map();

export function setTimezone(tz) {
  timezone = tz || 'UTC';
  cache.clear();
}
export function getTimezone() {
  return timezone;
}

const intlLocale = () => (getLocale() === 'ko' ? 'ko-KR' : 'en-US');

function nf(opts, localeOverride) {
  const loc = localeOverride || intlLocale();
  const key = `n|${loc}|${JSON.stringify(opts)}`;
  if (!cache.has(key)) cache.set(key, new Intl.NumberFormat(loc, opts));
  return cache.get(key);
}

function df(opts) {
  const key = `d|${intlLocale()}|${JSON.stringify(opts)}`;
  if (!cache.has(key)) cache.set(key, new Intl.DateTimeFormat(intlLocale(), opts));
  return cache.get(key);
}

export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
export const DASH = '\u2014';

const ZERO_DECIMAL = new Set(['KRW', 'JPY', 'VND', 'IDR', 'CLP', 'HUF', 'TWD']);

export const fmt = {
  int(v) {
    return isNum(v) ? nf({ maximumFractionDigits: 0 }).format(v) : DASH;
  },
  num(v, digits = 1) {
    return isNum(v) ? nf({ maximumFractionDigits: digits }).format(v) : DASH;
  },
  compact(v) {
    if (!isNum(v)) return DASH;
    if (Math.abs(v) < 1000) return nf({ maximumFractionDigits: Math.abs(v) < 10 && v % 1 !== 0 ? 1 : 0 }).format(v);
    return nf({ notation: 'compact', maximumFractionDigits: 1 }, 'en-US').format(v);
  },
  currency(v, currency = 'USD', opts = {}) {
    if (!isNum(v)) return DASH;
    const ccy = (currency || 'USD').toUpperCase();
    const zero = ZERO_DECIMAL.has(ccy);
    if (opts.compact && Math.abs(v) >= 100000) {
      return nf({ style: 'currency', currency: ccy, notation: 'compact', maximumFractionDigits: 1 }).format(v);
    }
    const digits = zero ? 0 : (opts.precise && Math.abs(v) < 1 ? 4 : 2);
    return nf({ style: 'currency', currency: ccy, minimumFractionDigits: zero ? 0 : Math.min(2, digits), maximumFractionDigits: digits }).format(v);
  },
  pct(ratio, digits = 1) {
    if (!isNum(ratio)) return DASH;
    return nf({ style: 'percent', minimumFractionDigits: 0, maximumFractionDigits: digits }).format(ratio);
  },
  signedPct(ratio, digits = 1) {
    if (!isNum(ratio)) return DASH;
    const s = nf({ style: 'percent', minimumFractionDigits: 0, maximumFractionDigits: digits }).format(Math.abs(ratio));
    return `${ratio > 0 ? '+' : ratio < 0 ? '-' : ''}${s}`;
  },
  bytes(v) {
    if (!isNum(v)) return DASH;
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let i = 0;
    let n = v;
    while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return `${nf({ maximumFractionDigits: n < 10 && i > 0 ? 1 : 0 }).format(n)} ${units[i]}`;
  },
  duration(ms) {
    if (!isNum(ms)) return DASH;
    if (ms < 1000) return `${Math.round(ms)} ms`;
    const sec = ms / 1000;
    if (sec < 60) return `${nf({ maximumFractionDigits: 1 }).format(sec)} s`;
    return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
  },
  /** Calendar date string (YYYY-MM-DD) -> short label. */
  date(d, style = 'short') {
    const dt = parseDate(d);
    if (!dt) return d || DASH;
    const opts = style === 'long'
      ? { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' }
      : { month: 'short', day: 'numeric', timeZone: 'UTC' };
    return df(opts).format(dt);
  },
  dateRange(start, end) {
    const a = parseDate(start);
    const b = parseDate(end);
    if (!a || !b) return DASH;
    const sameYear = a.getUTCFullYear() === b.getUTCFullYear();
    const currentYear = sameYear && b.getUTCFullYear() === new Date().getUTCFullYear();
    const yearOpt = currentYear ? undefined : 'numeric';
    const left = df({ month: 'short', day: 'numeric', year: sameYear ? undefined : 'numeric', timeZone: 'UTC' }).format(a);
    const right = df({ month: 'short', day: 'numeric', year: yearOpt, timeZone: 'UTC' }).format(b);
    return `${left} \u2013 ${right}`;
  },
  /** ISO timestamp -> date/time in the workspace timezone. */
  dateTime(iso, withSeconds = false) {
    if (!iso) return DASH;
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return String(iso);
    const yearOf = (d) => df({ year: 'numeric', timeZone: timezone }).format(d);
    const sameYear = yearOf(dt) === yearOf(new Date());
    return df({
      year: sameYear ? undefined : 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      second: withSeconds ? '2-digit' : undefined, hour12: false, timeZone: timezone,
    }).format(dt);
  },
  time(iso) {
    if (!iso) return DASH;
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return String(iso);
    return df({ hour: '2-digit', minute: '2-digit', hour12: false, timeZone: timezone }).format(dt);
  },
  relative(iso) {
    if (!iso) return DASH;
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return String(iso);
    const diff = (dt.getTime() - Date.now()) / 1000;
    const rtf = new Intl.RelativeTimeFormat(intlLocale(), { numeric: 'auto' });
    const units = [['second', 1, 60], ['minute', 60, 60], ['hour', 3600, 24], ['day', 86400, 45], ['month', 86400 * 30, 13]];
    for (const [unit, size, limit] of units) {
      const n = Math.round(diff / size);
      if (Math.abs(n) < limit) return rtf.format(n, unit);
    }
    return rtf.format(Math.round(diff / (86400 * 365)), 'year');
  },
  days(n) {
    if (!isNum(n)) return DASH;
    return t('{n} d', { n: fmt.int(n) });
  },
};

export function parseDate(d) {
  if (typeof d !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(d)) return null;
  const dt = new Date(`${d}T00:00:00Z`);
  return Number.isNaN(dt.getTime()) ? null : dt;
}

export function isoDate(dt) {
  return dt.toISOString().slice(0, 10);
}

export function addDays(d, n) {
  const dt = parseDate(d);
  if (!dt) return d;
  dt.setUTCDate(dt.getUTCDate() + n);
  return isoDate(dt);
}

export function diffDays(a, b) {
  const da = parseDate(a);
  const db = parseDate(b);
  if (!da || !db) return 0;
  return Math.round((db - da) / 86400000);
}

/** Relative change between current and previous values. */
export function change(current, previous) {
  if (!isNum(current) || !isNum(previous)) return null;
  if (previous === 0) return current === 0 ? 0 : null;
  return (current - previous) / Math.abs(previous);
}

export function initials(text) {
  if (!text) return '?';
  const base = String(text).split('@')[0].replace(/[._\-]+/g, ' ').trim();
  const parts = base.split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  const chars = parts.length > 1 ? parts[0][0] + parts[1][0] : parts[0].slice(0, 2);
  return chars.toUpperCase();
}
