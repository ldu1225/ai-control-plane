/**
 * UI component kit. Every component renders through the safe DOM helpers.
 */
import { h, append, mount, debounce, downloadText, toCsv, copyText } from './dom.js';
import { icon } from './icons.js';
import { t } from './i18n.js';
import { fmt, isNum, initials, DASH } from './format.js';
import { isAbort } from './api.js';
import { session, isAdmin, store } from './state.js';
import { sparkline } from './charts.js';
import { noticeText, serverText } from './messages.js';

/* ------------------------------------------------------------------ layout */

export function pageHeader({ title, desc, actions, project }) {
  const ws = (store && store.boot && store.boot.workspace) || {};
  const projects = (Array.isArray(ws.projects) && ws.projects.length ? ws.projects : [ws.projectId]).filter(Boolean);
  const sel = store ? store.selectedProject : null;
  let scopeBadge = null;
  if (!project) {
    if (sel) {
      scopeBadge = badge(t('Scope: Single project ({id})', { id: sel }), 'info', { size: 'sm', dot: true });
    } else if (projects.length > 1) {
      scopeBadge = badge(t('All {n} projects integrated', { n: projects.length }), 'success', { size: 'sm', dot: true, title: projects.join(', ') });
    }
  }
  return h('div', { class: 'page-header' },
    h('div', { class: 'page-heading' },
      h('div', { class: 'row', style: { gap: '8px', alignItems: 'center', flexWrap: 'wrap' } },
        h('h1', { class: 'page-title' }, title),
        scopeBadge),
      desc ? h('p', { class: 'page-desc' }, desc) : null),
    actions ? h('div', { class: 'page-actions' }, actions) : null);
}

export function card({ title, subtitle, actions, body, footer, source, cls, flush, bordered, id }) {
  const bodyEl = h('div', { class: ['card-body', flush ? 'flush' : ''] });
  if (body) append(bodyEl, [body]);
  const header = (title || actions) ? h('div', { class: ['card-header', bordered ? 'bordered' : ''] },
    h('div', { class: 'card-heading' },
      title ? h('h3', { class: 'card-title', title: typeof title === 'string' ? title : null }, title) : null,
      subtitle ? h('p', { class: 'card-subtitle', title: typeof subtitle === 'string' ? subtitle : null }, subtitle) : null),
    actions ? h('div', { class: 'card-actions' }, actions) : null) : null;
  const foot = (footer || source) ? h('div', { class: 'card-footer' },
    source ? sourceNote(source) : h('span'),
    footer || null) : null;
  const el = h('section', { class: ['card', cls], id: id || null }, header, bodyEl, foot);
  el.bodyEl = bodyEl;
  return el;
}

export function sourceNote(text) {
  return h('span', { class: 'card-source', title: text }, icon('database', { size: 12 }), h('span', null, text));
}

export function grid(children, cls = '') {
  return h('div', { class: ['grid', cls] }, children);
}

export function span(n, child) {
  child.classList.add(`span-${n}`);
  return child;
}

/* ------------------------------------------------------------------ primitives */

export function button({ label, icon: iconName, variant = 'secondary', size, onClick, disabled, title, type = 'button', cls, iconRight }) {
  const btn = h('button', {
    type,
    class: ['btn', `btn-${variant}`, size ? `btn-${size}` : '', !label ? 'btn-icon' : '', cls],
    title: title || null,
    'aria-label': !label ? (title || null) : null,
    disabled: disabled || null,
    onclick: onClick || null,
  }, iconName ? icon(iconName, { size: size === 'xs' ? 13 : 15 }) : null, label || null, iconRight ? icon(iconRight, { size: 14 }) : null);
  return btn;
}

export function iconButton(iconName, { title, onClick, bordered, size = 16 } = {}) {
  return h('button', { type: 'button', class: ['icon-btn', bordered ? 'bordered' : ''], title, 'aria-label': title, onclick: onClick }, icon(iconName, { size }));
}

export function setBusy(btn, busy, busyLabel) {
  if (!btn) return;
  if (busy) {
    if (!btn._origNodes) btn._origNodes = Array.from(btn.childNodes);
    btn.disabled = true;
    mount(btn, h('span', { class: 'spinner' }), busyLabel || btn.textContent);
  } else {
    btn.disabled = false;
    if (btn._origNodes) { mount(btn, btn._origNodes); btn._origNodes = null; }
  }
}

export function badge(text, tone = 'neutral', { dot = false, size, title } = {}) {
  return h('span', { class: ['badge', `badge-${tone}`, size === 'sm' ? 'sm' : ''], title: title || null },
    dot ? h('span', { class: ['dot', `dot-${tone}`] }) : null, text);
}

export function dot(tone = 'neutral') {
  return h('span', { class: ['dot', `dot-${tone}`] });
}

export function chip(text, { pressed, onClick, title } = {}) {
  if (onClick) {
    return h('button', { type: 'button', class: 'chip', 'aria-pressed': pressed ? 'true' : 'false', onclick: onClick, title: title || null }, text);
  }
  return h('span', { class: 'chip', title: title || null }, text);
}

export function progress(ratio, tone) {
  const fill = h('div', { class: ['progress-fill', tone || ''] });
  fill.style.setProperty('width', `${Math.max(0, Math.min(1, isNum(ratio) ? ratio : 0)) * 100}%`);
  return h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': Math.round((ratio || 0) * 100) }, fill);
}

export function avatar(text, cls = '') {
  return h('span', { class: ['avatar', cls], 'aria-hidden': 'true' }, initials(text));
}

export function userCell(email, sub, { masked, compact } = {}) {
  const label = email || t('Unattributed');
  return h('div', { class: ['user-cell', compact ? 'compact' : ''] },
    email ? avatar(label) : h('span', { class: 'avatar muted', 'aria-hidden': 'true' }, icon('user', { size: 14 })),
    h('div', { class: 'cell-stack' },
      h('span', { class: ['cell-main', 'truncate'], title: label }, label),
      sub ? h('span', { class: ['cell-sub', 'truncate'], title: sub }, sub) : null,
      masked ? h('span', { class: 'cell-sub' }, t('Masked for viewer role')) : null));
}

/**
 * Compact identity cell. Service accounts render as "<name>" over "<kind> · <project>";
 * other principals render the full address over the kind. The full value stays in the tooltip.
 */
export function principalCell(principal, kind) {
  if (!principal) return h('span', { class: 'text-muted' }, DASH);
  const at = principal.indexOf('@');
  const domain = at > 0 ? principal.slice(at + 1) : '';
  if (domain.endsWith('.gserviceaccount.com')) {
    const scope = domain.replace(/(\.iam)?\.gserviceaccount\.com$/, '');
    return h('div', { class: 'cell-stack principal-cell', title: principal },
      h('span', { class: 'cell-main cell-mono truncate' }, principal.slice(0, at)),
      h('span', { class: 'cell-sub truncate' }, [kind, scope].filter(Boolean).join(' \u00b7 ')));
  }
  return h('div', { class: 'cell-stack principal-cell', title: principal },
    h('span', { class: 'cell-main cell-mono truncate' }, principal),
    kind ? h('span', { class: 'cell-sub' }, kind) : null);
}

/**
 * Fixed-order on/off tags for dense tables. items: [{ key, short, label }].
 * Keys present in `active` but unknown to `items` are appended so nothing is hidden.
 */
export function tagMatrix(items, active, { on = t('In use'), off = t('Not used') } = {}) {
  const used = new Set(active || []);
  const known = new Set(items.map((i) => i.key));
  const extra = [...used].filter((k) => !known.has(k)).map((k) => ({ key: k, short: String(k).slice(0, 3).toUpperCase(), label: String(k) }));
  return h('div', { class: 'surface-matrix', role: 'list' }, [...items, ...extra].map((it) => {
    const isOn = used.has(it.key);
    const text = `${it.label}: ${isOn ? on : off}`;
    return h('span', { class: ['surface-tag', isOn ? 'on' : ''], role: 'listitem', title: text, 'aria-label': text }, it.short);
  }));
}

export function tagLegend(items) {
  return items.map((i) => `${i.short} = ${i.label}`).join('  \u00b7  ');
}

/** Multi-value cell: first item plus a "+N" counter; the full list stays in the tooltip. */
export function listCell(items, { mono = false } = {}) {
  const list = (items || []).filter(Boolean);
  if (!list.length) return h('span', { class: 'text-muted' }, DASH);
  return h('span', { class: 'list-cell', title: list.join(', ') },
    h('span', { class: ['truncate', mono ? 'cell-mono' : ''] }, list[0]),
    list.length > 1 ? h('span', { class: 'list-more' }, `+${list.length - 1}`) : null);
}

export function kv(pairs) {
  return h('dl', { class: 'kv' }, pairs.filter(Boolean).flatMap(([k, v]) => [h('dt', null, k), h('dd', null, v === null || v === undefined || v === '' ? DASH : v)]));
}

export function codeBlock(code, { wrap = false, copy = true } = {}) {
  const pre = h('pre', { class: ['code-block', wrap ? 'wrap' : ''] }, code || '');
  if (!copy) return pre;
  const wrapEl = h('div', { class: 'code-wrap' });
  wrapEl.style.setProperty('position', 'relative');
  const btn = iconButton('copy', {
    title: t('Copy'),
    onClick: async () => {
      try { await copyText(code || ''); toast({ title: t('Copied to clipboard'), tone: 'success' }); } catch (e) { toast({ title: t('Copy failed'), tone: 'danger' }); }
    },
  });
  append(wrapEl, [pre, h('div', { class: 'code-actions' }, btn)]);
  return wrapEl;
}

export function stat(label, value, sub) {
  return h('div', { class: 'stat' }, h('span', { class: 'stat-label' }, label), h('span', { class: 'stat-value' }, value), sub ? h('span', { class: 'stat-sub' }, sub) : null);
}

/* ------------------------------------------------------------------ KPI */

export function deltaBadge(ratio, goodWhen = 'up') {
  if (!isNum(ratio)) return null;
  const up = ratio > 0.0005;
  const down = ratio < -0.0005;
  let tone = '';
  if (goodWhen && (up || down)) tone = (up === (goodWhen === 'up')) ? 'good' : 'bad';
  return h('span', { class: ['delta', tone], title: t('Change vs previous period') },
    up ? icon('arrow-up', { size: 11, strokeWidth: 2.5 }) : down ? icon('arrow-down', { size: 11, strokeWidth: 2.5 }) : null,
    fmt.pct(Math.abs(ratio)));
}

export function kpi({ label, value, unit, delta, goodWhen = 'up', meta, spark, sparkColor, tip, onClick, disabled }) {
  const el = h('div', { class: ['kpi', disabled ? 'disabled' : '', onClick ? 'clickable' : ''], tabindex: onClick ? 0 : null, role: onClick ? 'link' : null },
    h('div', { class: 'kpi-head' },
      h('div', { class: 'kpi-label' }, h('span', { title: typeof label === 'string' ? label : null }, label),
        tip ? h('span', { class: 'info', 'data-tip': tip, tabindex: 0, 'aria-label': tip }, icon('info', { size: 13 })) : null),
      spark && !disabled ? sparkline(spark, sparkColor) : null),
    h('div', { class: 'kpi-value', title: typeof value === 'string' ? value : null }, disabled ? t('Module disabled') : value, unit && !disabled ? h('span', { class: 'unit' }, unit) : null),
    h('div', { class: 'kpi-meta' }, disabled ? null : deltaBadge(delta, goodWhen), disabled ? null : meta));
  if (onClick) {
    el.addEventListener('click', onClick);
    el.addEventListener('keydown', (e) => { if (e.key === 'Enter') onClick(); });
  }
  return el;
}

export function kpiSkeleton() {
  return h('div', { class: 'kpi' },
    h('div', { class: ['skeleton', 'skeleton-line'] }),
    h('div', { class: ['skeleton', 'skeleton-kpi'] }),
    h('div', { class: ['skeleton', 'skeleton-line'] }));
}

/* ------------------------------------------------------------------ inputs */

export function segmented(options, value, onChange, { label } = {}) {
  const group = h('div', { class: 'segmented', role: 'group', 'aria-label': label || null });
  const render = (val) => {
    mount(group, options.map((o) => h('button', {
      type: 'button',
      'aria-pressed': o.value === val ? 'true' : 'false',
      onclick: () => { if (o.value !== val) { render(o.value); onChange(o.value); } },
      title: o.title || null,
    }, o.label)));
  };
  render(value);
  return group;
}

export function tabs(items, active, onChange) {
  const el = h('div', { class: 'tabs', role: 'tablist' });
  const render = (cur) => {
    mount(el, items.map((it) => h('button', {
      type: 'button', class: 'tab', role: 'tab', 'aria-selected': it.id === cur ? 'true' : 'false',
      onclick: () => { render(it.id); onChange(it.id); },
    }, it.label, isNum(it.count) ? h('span', { class: 'count' }, fmt.int(it.count)) : null)));
  };
  render(active);
  return el;
}

export function selectInput(options, value, onChange, attrs = {}) {
  const sel = h('select', { class: 'select', ...attrs, onchange: (e) => onChange && onChange(e.target.value) },
    options.map((o) => h('option', { value: o.value, selected: o.value === value ? true : null }, o.label)));
  return h('span', { class: 'select-wrap' }, sel, h('span', { class: 'chev' }, icon('chevron-down', { size: 14 })));
}

export function switchInput({ checked, onChange, disabled, label, id }) {
  const input = h('input', {
    type: 'checkbox', class: 'switch', role: 'switch', id: id || null,
    'aria-label': label || null, checked: Boolean(checked), disabled: disabled || null,
    onchange: (e) => onChange && onChange(e.target.checked, e.target),
  });
  input.setAttribute('aria-checked', checked ? 'true' : 'false');
  input.addEventListener('change', () => input.setAttribute('aria-checked', input.checked ? 'true' : 'false'));
  return input;
}

const LABELABLE = 'input:not([type="hidden"]), select, textarea, button, meter, output, progress';
let fieldSeq = 0;

/**
 * A captioned form field. A single input, select or textarea is wrapped in a <label>, so a click on the
 * caption focuses it. Anything else (preset buttons, radio or checkbox lists, a code block with a copy button)
 * gets a role="group" container: a <label> forwards clicks on its caption, help text and empty space to its
 * first labelable descendant, which would press the first preset button.
 */
export function field({ label, help, control, error }) {
  const targets = control instanceof Element ? [control, ...control.querySelectorAll(LABELABLE)].filter((el) => el.matches(LABELABLE)) : [];
  const single = targets.length === 1 && targets[0].tagName !== 'BUTTON';
  const id = single || !label ? null : `field-caption-${++fieldSeq}`;
  return h(single ? 'label' : 'div', { class: 'field', role: single ? null : 'group', 'aria-labelledby': id },
    label ? h('span', { class: 'field-label', id }, label) : null,
    control,
    help ? h('span', { class: 'field-help' }, help) : null,
    error ? h('span', { class: 'field-error' }, error) : null);
}

export function textInput({ value = '', placeholder, onInput, type = 'text', mono, attrs = {} }) {
  return h('input', { class: ['input', mono ? 'mono' : ''], type, value, placeholder: placeholder || null, oninput: onInput ? (e) => onInput(e.target.value) : null, ...attrs });
}

export function searchBox({ placeholder, onInput, value = '' }) {
  const input = h('input', { class: 'input', type: 'search', placeholder: placeholder || t('Search'), value, 'aria-label': placeholder || t('Search') });
  if (onInput) input.addEventListener('input', debounce(() => onInput(input.value), 160));
  return h('div', { class: 'search-input' }, h('span', { class: 'icon' }, icon('search', { size: 14 })), input);
}

/* ------------------------------------------------------------------ states */

const ERROR_COPY = {
  module_disabled: ['Module disabled', 'This module is turned off for this workspace. An administrator can enable it in Settings > Modules.', 'power'],
  not_configured: ['Workspace not configured', 'Connect a Google Cloud project and its data sources in Settings > Workspace to start collecting data.', 'database'],
  forbidden: ['Access restricted', 'Your role does not permit this view or action.', 'lock'],
  csrf_failed: ['Session expired', 'Reload the page to refresh your session and try again.', 'lock'],
  unauthenticated: ['Sign-in required', 'Your session is not authenticated. Reload the page to sign in again.', 'lock'],
  rate_limited: ['Too many requests', 'The request rate limit was reached. Wait a moment and try again.', 'clock'],
  upstream_error: ['Data source unavailable', 'A Google Cloud data source did not respond as expected.', 'alert-triangle'],
  network: ['Connection problem', 'The server could not be reached. Check your connection and retry.', 'globe'],
  not_found: ['Not found', 'The requested item no longer exists or is outside the selected period.', 'search'],
};

export function emptyState({ icon: iconName = 'info', title, body, action, tone, compact }) {
  return h('div', { class: ['empty', tone || '', compact ? 'compact' : ''] },
    h('div', { class: 'empty-icon' }, icon(iconName, { size: 18 })),
    title ? h('div', { class: 'empty-title' }, title) : null,
    body ? h('div', { class: 'empty-body' }, body) : null,
    action || null);
}

export function errorState(err, { onRetry, compact } = {}) {
  const code = (err && err.code) || 'internal';
  const copy = ERROR_COPY[code] || ['Something went wrong', 'The request could not be completed.', 'alert-circle'];
  const detail = err && err.message && code === 'upstream_error' ? serverText(err.message) : null;
  const actions = [];
  if (code === 'module_disabled' || code === 'not_configured') {
    actions.push(button({ label: t('Open settings'), size: 'sm', onClick: () => { window.location.hash = code === 'module_disabled' ? '#/settings/modules' : '#/settings/workspace'; } }));
  }
  if (onRetry && !['module_disabled', 'not_configured', 'forbidden'].includes(code)) {
    actions.push(button({ label: t('Retry'), icon: 'refresh', size: 'sm', onClick: onRetry }));
  }
  return emptyState({
    icon: copy[2],
    tone: ['module_disabled', 'not_configured'].includes(code) ? 'warning' : 'error',
    title: t(copy[0]),
    body: detail ? `${t(copy[1])} ${detail}` : t(copy[1]),
    action: actions.length ? h('div', { class: 'row' }, actions) : null,
    compact,
  });
}

export function skeleton(kind = 'block', height) {
  if (kind === 'lines') {
    return h('div', null, [0, 1, 2, 3].map((i) => {
      const l = h('div', { class: ['skeleton', 'skeleton-line'] });
      l.style.setProperty('width', `${90 - i * 12}%`);
      return l;
    }));
  }
  if (kind === 'table') {
    return h('div', { class: 'card-body' }, [0, 1, 2, 3, 4, 5].map(() => h('div', { class: ['skeleton', 'skeleton-line'] })));
  }
  const el = h('div', { class: ['skeleton', 'skeleton-block'] });
  if (height) el.style.setProperty('height', `${height}px`);
  return el;
}

/** Render async content into a target with skeleton + structured error handling. */
export async function load(target, fetcher, render, { skeletonKind = 'block', height, signal } = {}) {
  mount(target, skeleton(skeletonKind, height));
  try {
    const res = await fetcher();
    if (signal && signal.aborted) return null;
    mount(target);
    render(res);
    return res;
  } catch (err) {
    if (isAbort(err) || (signal && signal.aborted)) return null;
    mount(target, errorState(err, { onRetry: () => load(target, fetcher, render, { skeletonKind, height, signal }) }));
    return null;
  }
}

/** Data source notices from meta.warnings. */
export function warningsNotice(warnings) {
  const seen = new Set();
  const list = (warnings || []).filter(Boolean).filter((w) => {
    const key = `${w.source || ''}|${w.code || ''}|${w.message || ''}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  if (!list.length) return null;
  const LIMIT = 5;
  const item = (w) => {
    const text = noticeText(w);
    const original = w.message && w.message !== text ? w.message : null;
    return h('li', original ? { title: original } : null, w.source ? h('code', null, w.source) : null, w.source ? ' \u2014 ' : null, text);
  };
  const body = h('div', { class: 'banner-body' },
    h('div', { class: 'banner-title' }, t('{n} data source notices', { n: list.length })),
    h('ul', { class: 'notice-list' }, list.slice(0, LIMIT).map(item)),
    list.length > LIMIT ? h('details', { class: 'notice-more' },
      h('summary', null, t('Show {n} more', { n: list.length - LIMIT })),
      h('ul', { class: 'notice-list' }, list.slice(LIMIT).map(item))) : null);
  return h('div', { class: ['banner', 'banner-warning'], role: 'status' }, h('span', { class: 'banner-icon' }, icon('alert-triangle', { size: 16 })), body);
}

export function banner({ tone = 'info', title, text, actions, iconName }) {
  const icons = { info: 'info', warning: 'alert-triangle', danger: 'alert-circle', success: 'check-circle', neutral: 'info' };
  return h('div', { class: ['banner', `banner-${tone}`], role: 'status' },
    h('span', { class: 'banner-icon' }, icon(iconName || icons[tone] || 'info', { size: 16 })),
    h('div', { class: 'banner-body' }, title ? h('div', { class: 'banner-title' }, title) : null, text ? h('div', { class: 'banner-text' }, text) : null),
    actions ? h('div', { class: 'banner-actions' }, actions) : null);
}

/* ------------------------------------------------------------------ data table */

export function dataTable(opts) {
  const {
    columns, pageSize = 10, searchable = true, searchKeys = null, exportName = null,
    toolbar = null, onRowClick = null, initialSort = null, maxHeight = null, title = null,
  } = opts;
  // rows === null means the backing source could not be read (distinct from an empty result).
  const unavailable = opts.rows === null;
  const rows = Array.isArray(opts.rows) ? opts.rows : [];
  const emptyText = unavailable
    ? (opts.unavailableText || t('Data source unavailable. See the data source notices above.'))
    : (opts.emptyText || null);
  const state = { q: '', sortKey: initialSort ? initialSort.key : null, sortDir: initialSort ? initialSort.dir : 'desc', page: 0 };
  const root = h('div', { class: 'data-table' });
  const tbody = h('tbody');
  const thead = h('thead');
  const pager = h('div', { class: 'table-pager' });
  const wrap = h('div', { class: 'table-wrap' }, h('table', { class: 'table' }, thead, tbody));
  if (maxHeight) wrap.style.setProperty('--table-max-h', `${maxHeight}px`);

  const valueOf = (row, col) => (col.value ? col.value(row) : row[col.key]);
  const textOf = (row) => {
    const keys = searchKeys || columns.map((c) => c.key);
    return keys.map((k) => {
      const col = columns.find((c) => c.key === k);
      const v = col && col.value ? col.value(row) : row[k];
      return v === null || v === undefined ? '' : (typeof v === 'object' ? JSON.stringify(v) : String(v));
    }).join(' ').toLowerCase();
  };

  const filtered = () => {
    let list = rows;
    if (state.q) {
      const q = state.q.toLowerCase();
      list = list.filter((r) => textOf(r).includes(q));
    }
    if (state.sortKey) {
      const col = columns.find((c) => c.key === state.sortKey);
      if (col) {
        const dir = state.sortDir === 'asc' ? 1 : -1;
        list = list.slice().sort((a, b) => {
          const va = valueOf(a, col);
          const vb = valueOf(b, col);
          if (va === vb) return 0;
          if (va === null || va === undefined || va === '') return 1;
          if (vb === null || vb === undefined || vb === '') return -1;
          if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
          return String(va).localeCompare(String(vb)) * dir;
        });
      }
    }
    return list;
  };

  const renderHead = () => {
    mount(thead, h('tr', null, columns.map((col) => {
      const sortable = col.sortable !== false && !col.actions;
      const sorted = state.sortKey === col.key;
      const th = h('th', {
        class: [col.align || '', sortable ? 'sortable' : ''],
        scope: 'col',
        title: col.title || null,
        'aria-sort': sorted ? (state.sortDir === 'asc' ? 'ascending' : 'descending') : null,
      }, col.label, sortable ? h('span', { class: 'sort-ind' }, icon(sorted ? (state.sortDir === 'asc' ? 'chevron-up' : 'chevron-down') : 'chevrons-up-down', { size: 12 })) : null);
      if (col.width) th.style.setProperty('width', col.width);
      if (sortable) {
        th.tabIndex = 0;
        const toggle = () => {
          if (state.sortKey === col.key) state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
          else { state.sortKey = col.key; state.sortDir = col.align === 'num' ? 'desc' : 'asc'; }
          state.page = 0;
          renderHead();
          renderBody();
        };
        th.addEventListener('click', toggle);
        th.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
      }
      return th;
    })));
  };

  const renderBody = () => {
    const list = filtered();
    const pages = Math.max(1, Math.ceil(list.length / pageSize));
    state.page = Math.min(state.page, pages - 1);
    const slice = pageSize ? list.slice(state.page * pageSize, state.page * pageSize + pageSize) : list;
    if (!slice.length) {
      mount(tbody, h('tr', null, h('td', { colspan: columns.length, class: 'table-empty' }, state.q ? t('No results match your search.') : (emptyText || t('No records in this period.')))));
    } else {
      mount(tbody, slice.map((row) => {
        const tr = h('tr', { class: onRowClick ? 'row-click' : null }, columns.map((col) => {
          let content;
          if (col.render) content = col.render(row);
          else {
            const v = row[col.key];
            content = v === null || v === undefined || v === '' ? DASH : (col.format ? col.format(v, row) : String(v));
          }
          return h('td', { class: [col.align || '', col.wrap ? 'wrap' : ''] }, content);
        }));
        if (onRowClick) {
          tr.tabIndex = 0;
          tr.addEventListener('click', (e) => { if (!e.target.closest('button, a, input, select')) onRowClick(row); });
          tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target === tr) onRowClick(row); });
        }
        return tr;
      }));
    }
    if (pageSize && list.length > pageSize) {
      mount(pager,
        h('span', null, t('{from}-{to} of {total}', { from: fmt.int(state.page * pageSize + 1), to: fmt.int(Math.min(list.length, (state.page + 1) * pageSize)), total: fmt.int(list.length) })),
        h('div', { class: 'pager-btns' },
          button({ icon: 'chevron-left', size: 'sm', title: t('Previous page'), disabled: state.page === 0, onClick: () => { state.page -= 1; renderBody(); } }),
          button({ icon: 'chevron-right', size: 'sm', title: t('Next page'), disabled: state.page >= pages - 1, onClick: () => { state.page += 1; renderBody(); } })));
      pager.hidden = false;
    } else {
      pager.hidden = true;
      if (list.length) mount(pager, h('span', null, t('{total} records', { total: fmt.int(list.length) })));
    }
  };

  const toolbarLeft = h('div', { class: 'left' });
  const toolbarRight = h('div', { class: 'right' });
  if (title) append(toolbarLeft, [h('span', { class: 'fw-600' }, title)]);
  if (searchable) {
    append(toolbarLeft, [searchBox({ onInput: (q) => { state.q = q; state.page = 0; renderBody(); } })]);
  }
  if (toolbar) append(toolbarLeft, [toolbar]);
  if (exportName) {
    append(toolbarRight, [button({
      label: t('Export CSV'), icon: 'download', size: 'sm',
      onClick: () => downloadText(`${exportName}.csv`, toCsv(columns.filter((c) => !c.actions && c.csv !== false), filtered())),
    })]);
  }
  if (searchable || toolbar || exportName || title) {
    append(root, [h('div', { class: 'table-toolbar' }, toolbarLeft, toolbarRight)]);
  }
  append(root, [wrap, pager]);
  renderHead();
  renderBody();
  root.setRows = (newRows) => { opts.rows = newRows; rows.length = 0; rows.push(...newRows); renderBody(); };
  return root;
}

/* ------------------------------------------------------------------ overlays */

function attachLightDismiss(dialog) {
  if (!('closedBy' in HTMLDialogElement.prototype)) {
    dialog.addEventListener('click', (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      const inside = rect.top <= event.clientY && event.clientY <= rect.top + rect.height
        && rect.left <= event.clientX && event.clientX <= rect.left + rect.width;
      if (!inside) dialog.close();
    });
  }
}

export function openDrawer({ title, subtitle, lead, width, content, actions }) {
  const body = h('div', { class: 'drawer-body' });
  const dialog = h('dialog', { class: 'drawer', closedby: 'any', 'aria-label': typeof title === 'string' ? title : t('Details') },
    h('div', { class: 'drawer-header' },
      h('div', { class: 'drawer-heading' }, lead || null,
        h('div', { class: 'cell-stack' }, h('div', { class: 'drawer-title' }, title), subtitle ? h('div', { class: 'drawer-subtitle' }, subtitle) : null)),
      h('div', { class: 'row' }, actions || null, iconButton('x', { title: t('Close'), onClick: () => dialog.close() }))),
    body);
  if (width) dialog.style.setProperty('--drawer-w', `${width}px`);
  attachLightDismiss(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  if (typeof content === 'function') content(body, dialog);
  else if (content) append(body, [content]);
  dialog.showModal();
  return { dialog, body, close: () => dialog.close() };
}

export function openModal({ title, subtitle, body, footer, wide, onClose, dismissible = true }) {
  const bodyEl = h('div', { class: 'modal-body' });
  const footerEl = h('div', { class: 'modal-footer' });
  const dialog = h('dialog', { class: ['modal', wide ? 'wide' : ''], closedby: dismissible ? 'any' : 'closerequest', 'aria-label': title },
    h('div', { class: 'modal-header' },
      h('div', null, h('h2', { class: 'modal-title' }, title), subtitle ? h('p', { class: 'modal-subtitle' }, subtitle) : null),
      iconButton('x', { title: t('Close'), onClick: () => dialog.close() })),
    bodyEl, footerEl);
  if (dismissible) attachLightDismiss(dialog);
  dialog.addEventListener('close', () => { dialog.remove(); if (onClose) onClose(dialog.returnValue); });
  document.body.append(dialog);
  if (typeof body === 'function') body(bodyEl, dialog); else if (body) append(bodyEl, [body]);
  if (typeof footer === 'function') footer(footerEl, dialog); else if (footer) append(footerEl, [footer]); else footerEl.remove();
  dialog.showModal();
  return { dialog, body: bodyEl, footer: footerEl, close: (v) => dialog.close(v) };
}

/** Promise-based confirmation. requireText forces typing an exact phrase for destructive actions. */
export function confirmDialog({ title, message, details, confirmLabel, tone = 'primary', requireText = null }) {
  return new Promise((resolve) => {
    let resolved = false;
    const finish = (v) => { if (!resolved) { resolved = true; resolve(v); } };
    const confirmBtn = button({ label: confirmLabel || t('Confirm'), variant: tone === 'danger' ? 'danger' : 'primary', disabled: Boolean(requireText) });
    const cancelBtn = button({ label: t('Cancel') });
    const m = openModal({
      title,
      body: (el) => {
        append(el, [message ? h('p', { class: 'muted' }, message) : null, details || null]);
        if (requireText) {
          const input = textInput({ placeholder: requireText, attrs: { 'aria-label': t('Confirmation text'), autocomplete: 'off' } });
          input.addEventListener('input', () => { confirmBtn.disabled = input.value.trim() !== requireText; });
          append(el, [field({ label: t('Type {text} to confirm', { text: requireText }), control: input })]);
          setTimeout(() => input.focus(), 30);
        }
      },
      footer: (el) => append(el, [cancelBtn, confirmBtn]),
      onClose: () => finish(false),
    });
    cancelBtn.addEventListener('click', () => { finish(false); m.close(); });
    confirmBtn.addEventListener('click', () => { finish(true); m.close(); });
  });
}

/**
 * Modal alert displayed when a Read-Only Viewer attempts to change settings or run a control action.
 */
export function alertPermissionRequired({ action, iamPermission, reason } = {}) {
  if (document.querySelector('dialog.perm-alert-modal')) return null;
  const s = session();
  const email = s.email || t('Viewer');
  const m = openModal({
    title: t('Permission required'),
    subtitle: t('Read-only account (Viewer)'),
    body: (el, dialog) => {
      dialog.classList.add('perm-alert-modal');
      append(el, [
        banner({
          tone: 'warning',
          iconName: 'lock',
          title: t('Administrator permissions required'),
          text: t('Your Google account has Read-Only (Viewer) access. You can view all dashboards and analytics without masking, but modifying settings or running controls requires Administrator permissions.'),
        }),
        kv([
          action ? [t('Requested action'), action] : null,
          [t('Current account'), h('span', { class: 'mono' }, email)],
          [t('Current role'), badge(t('Viewer (Read-only)'), 'neutral', { size: 'sm' })],
          [t('Required role'), badge(t('Administrator'), 'accent', { size: 'sm' })],
          iamPermission ? [t('Required Google Cloud IAM'), h('code', { class: 'cell-mono' }, iamPermission)] : null,
          reason ? [t('Details'), serverText(reason)] : null,
        ]),
        h('p', { class: 'muted' }, t('To control or change this setting, ask a workspace administrator to grant Administrator permissions to your account in Settings > Access control (or grant the required Google Cloud IAM permissions).')),
      ]);
    },
    footer: (el, dialog) => append(el, [
      button({ label: t('OK'), variant: 'primary', onClick: () => dialog.close() }),
    ]),
  });
  return m;
}

export function requireAdmin(action, iamPermission) {
  if (isAdmin()) return true;
  alertPermissionRequired({ action, iamPermission });
  return false;
}

let activePopover = null;
let lastClosed = { anchor: null, at: 0 };

export function closePopover() {
  if (activePopover) activePopover();
}

/** Anchored popover menu using the Popover API when available, with a manual fallback. */
export function openPopover(anchor, build, { align = 'end', wide = false } = {}) {
  if (lastClosed.anchor === anchor && Date.now() - lastClosed.at < 250) return null;
  closePopover();
  const pop = h('div', { class: ['popover', wide ? 'wide' : ''], role: 'dialog' });
  const native = typeof pop.showPopover === 'function';
  let closed = false;
  const cleanup = () => {
    if (closed) return;
    closed = true;
    lastClosed = { anchor, at: Date.now() };
    document.removeEventListener('mousedown', outside, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', close);
    window.removeEventListener('scroll', reposition, true);
    anchor.setAttribute('aria-expanded', 'false');
    pop.remove();
    if (activePopover === close) activePopover = null;
  };
  const close = () => {
    if (native && pop.matches(':popover-open')) { try { pop.hidePopover(); } catch (e) { /* already closed */ } }
    cleanup();
  };
  const outside = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) close(); };
  const onKey = (e) => { if (e.key === 'Escape') { close(); anchor.focus(); } };
  const reposition = () => {
    const r = anchor.getBoundingClientRect();
    const pw = pop.offsetWidth;
    const ph = pop.offsetHeight;
    let left = align === 'start' ? r.left : r.right - pw;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    let top = r.bottom + 6;
    if (top + ph > window.innerHeight - 8 && r.top - ph - 6 > 8) top = r.top - ph - 6;
    pop.style.setProperty('left', `${left}px`);
    pop.style.setProperty('top', `${top}px`);
  };
  const host = anchor.closest('dialog') || document.body;
  host.append(pop);
  append(pop, [build(close)]);
  if (native) {
    pop.setAttribute('popover', 'manual');
    pop.showPopover();
  } else {
    pop.classList.add('is-open');
  }
  document.addEventListener('mousedown', outside, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', close);
  window.addEventListener('scroll', reposition, true);
  anchor.setAttribute('aria-expanded', 'true');
  reposition();
  activePopover = close;
  const first = pop.querySelector('button, input, select, [tabindex="0"]');
  if (first) setTimeout(() => first.focus(), 0);
  return close;
}

export function menuItem({ label, desc, iconName, checked, onClick, danger }) {
  return h('button', { type: 'button', class: ['menu-item', danger ? 'danger' : ''], onclick: onClick, role: 'menuitem' },
    iconName ? h('span', { class: 'menu-icon' }, icon(iconName, { size: 15 })) : null,
    h('span', null, label, desc ? h('span', { class: 'menu-desc' }, desc) : null),
    checked ? h('span', { class: 'menu-check' }, icon('check', { size: 14 })) : null);
}

/* ------------------------------------------------------------------ toasts */

let toastRegion = null;

export function toast({ title, text, tone = 'info', timeout = 4800 }) {
  if (!toastRegion) {
    toastRegion = h('div', { class: 'toast-region', 'aria-live': 'polite' });
    document.body.append(toastRegion);
    if (typeof toastRegion.showPopover === 'function') toastRegion.setAttribute('popover', 'manual');
  }
  if (typeof toastRegion.showPopover === 'function') {
    try { if (toastRegion.matches(':popover-open')) toastRegion.hidePopover(); toastRegion.showPopover(); } catch (e) { /* ignore */ }
  }
  const icons = { success: 'check-circle', danger: 'alert-circle', warning: 'alert-triangle', info: 'info' };
  const el = h('div', { class: ['toast', `toast-${tone}`], role: tone === 'danger' ? 'alert' : 'status' },
    h('span', { class: 'toast-icon' }, icon(icons[tone] || 'info', { size: 16 })),
    h('div', { class: 'toast-body' }, h('div', { class: 'toast-title' }, title), text ? h('div', { class: 'toast-text' }, text) : null),
    iconButton('x', { title: t('Dismiss'), size: 14, onClick: () => el.remove() }));
  toastRegion.append(el);
  setTimeout(() => el.remove(), timeout);
}

/* ------------------------------------------------------------------ tooltips */

let tipEl = null;
export function initTooltips() {
  const show = (target) => {
    const text = target.getAttribute('data-tip');
    if (!text) return;
    if (!tipEl) tipEl = h('div', { class: 'tooltip', role: 'tooltip' });
    const host = target.closest('dialog') || document.body;
    if (tipEl.parentNode !== host) host.append(tipEl);
    tipEl.textContent = text;
    tipEl.classList.add('show');
    const r = target.getBoundingClientRect();
    const tw = tipEl.offsetWidth;
    const th = tipEl.offsetHeight;
    let left = r.left + r.width / 2 - tw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
    let top = r.top - th - 8;
    if (top < 8) top = r.bottom + 8;
    tipEl.style.setProperty('left', `${left}px`);
    tipEl.style.setProperty('top', `${top}px`);
  };
  const hide = () => { if (tipEl) tipEl.classList.remove('show'); };
  document.addEventListener('mouseover', (e) => { const tg = e.target.closest && e.target.closest('[data-tip]'); if (tg) show(tg); });
  document.addEventListener('mouseout', (e) => { const tg = e.target.closest && e.target.closest('[data-tip]'); if (tg) hide(); });
  document.addEventListener('focusin', (e) => { const tg = e.target.closest && e.target.closest('[data-tip]'); if (tg) show(tg); });
  document.addEventListener('focusout', hide);
  window.addEventListener('scroll', hide, true);
}

/* ------------------------------------------------------------------ markdown-lite */

function inline(text) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith('**')) out.push(h('strong', null, tok.slice(2, -2)));
    else out.push(h('code', null, tok.slice(1, -1)));
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** Render a small, safe markdown subset (paragraphs, bullets, numbered lists, headings, bold, code). */
export function markdownLite(text) {
  const root = h('div', { class: 'md' });
  const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
  let para = [];
  let list = null;
  const flushPara = () => { if (para.length) { root.append(h('p', null, inline(para.join(' ')))); para = []; } };
  const flushList = () => { if (list) { root.append(list); list = null; } };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { flushPara(); flushList(); continue; }
    const bullet = line.match(/^[-*\u2022]\s+(.*)$/);
    const numbered = line.match(/^\d+[.)]\s+(.*)$/);
    const heading = line.match(/^#{1,4}\s+(.*)$/);
    if (heading) { flushPara(); flushList(); root.append(h('h4', null, inline(heading[1]))); continue; }
    if (bullet || numbered) {
      flushPara();
      const tag = bullet ? 'ul' : 'ol';
      if (!list || list.tagName.toLowerCase() !== tag) { flushList(); list = h(tag); }
      list.append(h('li', null, inline((bullet || numbered)[1])));
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();
  return root;
}

/* ------------------------------------------------------------------ status mapping */

const STATUS_TONES = {
  active: ['Active', 'success'],
  idle: ['Idle', 'warning'],
  deprecation_candidate: ['Deprecation candidate', 'danger'],
  ok: ['Healthy', 'success'],
  warning: ['Degraded', 'warning'],
  error: ['Error', 'danger'],
  unconfigured: ['Not configured', 'neutral'],
  pass: ['Pass', 'success'],
  warn: ['Warning', 'warning'],
  fail: ['Fail', 'danger'],
  ENFORCEABLE_ALLOWED: ['Enforceable', 'success'],
  CAPPED_BY_TOOL: ['Capped', 'warning'],
  BLOCKED_BY_ADMIN: ['Blocked by admin', 'danger'],
  NOT_ENFORCEABLE: ['Not enforceable', 'info'],
  NO_ACCESS: ['No access', 'neutral'],
  UNKNOWN: ['Unknown', 'neutral'],
  block: ['Block', 'danger'],
  restore: ['Restore', 'success'],
  manual: ['Manual action', 'info'],
  review: ['Awaiting review', 'warning'],
  disallow: ['Block (rule)', 'danger'],
  none: ['No change', 'neutral'],
  blocked: ['Blocked', 'danger'],
  allowed: ['Allowed', 'success'],
  success: ['Success', 'success'],
  failed: ['Failed', 'danger'],
  partial: ['Partial', 'warning'],
  aborted: ['Aborted', 'warning'],
  dry_run: ['Dry run', 'info'],
  applied: ['Applied', 'success'],
  skipped: ['Skipped', 'neutral'],
};

export function statusBadge(status, { size } = {}) {
  const def = STATUS_TONES[status];
  if (!def) return badge(String(status || DASH), 'neutral', { size });
  return badge(t(def[0]), def[1], { dot: true, size });
}

export function statusTone(status) {
  const def = STATUS_TONES[status];
  return def ? def[1] : 'neutral';
}
