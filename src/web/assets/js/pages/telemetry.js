/**
 * Telemetry Extensions: every telemetry source found in the project (Cloud Logging logs and BigQuery
 * tables) is shown as a card that explains what the data is and what its dashboard shows, and becomes a
 * standard dashboard with one click. Descriptions, breakdown fields and the ready connector body come
 * from the server catalog (services/telemetry_catalog.py), so nothing has to be typed. The custom
 * connector form and the Log Router sink generator remain available for sources that are not listed.
 */
import { h, mount, append } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api, isAbort } from '../core/api.js';
import { store, isAdmin, isModuleEnabled, rangeParams } from '../core/state.js';
import {
  pageHeader, kpi, card, dataTable, badge, button, banner, switchInput, openModal, openDrawer, toast, setBusy, field, textInput,
  selectInput, errorState, warningsNotice, confirmDialog, codeBlock, chip, userCell, searchBox, skeleton, requireAdmin,
} from '../core/components.js';
import { errorText } from '../core/messages.js';
import { barChart, hbarList, mapList, sparkline, fromTimeline, TONES, colorAt } from '../core/charts.js';
import { rangeText, chartCard, mountCharts, pageSkeleton, deltaOf } from './_shared.js';
import { cardTitle, cardWhat, shortValue, statusText } from '../core/telemetry_view.js';
import { loadLayouts, isPinned, togglePin, canEditLayouts } from '../core/widgets.js';

const MAPPING_FIELDS = [
  ['time', 'Timestamp', true],
  ['user', 'User', false],
  ['operation', 'Operation', false],
  ['model', 'Model', false],
  ['tokens', 'Tokens', false],
  ['status', 'Status', false],
];

/** A log dashboard counts at most this many entries per view (MAX_LOG_ENTRIES on the server). */
const MAX_LOG_READ = 5000;

const CATEGORIES = [
  { id: 'ai', label: 'AI and agents', icon: 'spark' },
  { id: 'security', label: 'Security and audit', icon: 'shield' },
  { id: 'operations', label: 'Operations', icon: 'server' },
  { id: 'other', label: 'Other', icon: 'database' },
];

/** Built-in pages that read the built-in sources: route id -> [nav label, module id]. */
const PAGES = {
  overview: ['Overview', 'overview'],
  'gemini-enterprise': ['Gemini Enterprise', 'gemini_enterprise'],
  notebooklm: ['NotebookLM', 'notebooklm'],
  gateway: ['Agent Gateway', 'agent_gateway'],
  registry: ['Agent Registry', 'agent_registry'],
  deployments: ['Deployments', 'agent_deployments'],
  'model-armor': ['Model Armor', 'model_armor'],
  models: ['Models & Tokens', 'model_usage'],
  users: ['Users & Access', 'users'],
  antigravity: ['Antigravity Controls', 'antigravity'],
};

const LOG_FILTER_RE = /^\s*logName\s*=\s*"projects\/[^/"]+\/logs\/([^"]+)"\s*$/;

export default async function render(ctx) {
  if (ctx.params[0]) return renderConnector(ctx, ctx.params[0]);
  return renderList(ctx);
}

/* ------------------------------------------------------------------ text helpers */

/** Card title; sources that share a title (for example two Cloud Run system log streams) add their log or table. */
function sourceTitle(s) {
  const base = cardTitle(s.card) || s.tech;
  return s.variant ? `${base} (${s.variant})` : base;
}

function categoryOf(s) {
  return CATEGORIES.find((x) => x.id === (s.card && s.card.category)) || CATEGORIES[CATEGORIES.length - 1];
}

function logNameOf(conn) {
  const m = /^\s*logName\s*=\s*"(projects\/[^"]+)"\s*$/.exec((conn.logging && conn.logging.filter) || '');
  return m ? m[1] : null;
}

function logIdOf(conn) {
  const m = LOG_FILTER_RE.exec((conn.logging && conn.logging.filter) || '');
  if (!m) return null;
  try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
}

const AGENT_FILTER_RE = /^\s*\(?resource\.labels\.job="([A-Za-z0-9_-]+)"\s+OR\s+resource\.labels\.service_name="\1"\)?(?:\s+AND\s+.*)?$/;

/** What a connector reads, for people: the log id, the table reference or the custom filter. */
function sourceRef(conn) {
  if (conn.source === 'bigquery' && conn.bigquery) return `${conn.bigquery.project}.${conn.bigquery.dataset}.${conn.bigquery.table}`;
  const flt = (conn.logging && conn.logging.filter) || '';
  const am = AGENT_FILTER_RE.exec(flt);
  if (am) return `${am[1]} (CODEMENDER_COST_TELEMETRY)`;
  return logIdOf(conn) || flt || '';
}

function rangeDays() {
  const start = Date.parse(store.range.start);
  const end = Date.parse(store.range.end);
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(1, Math.round((end - start) / 86400000) + 1) : 14;
}

/* ------------------------------------------------------------------ list view */

async function renderList(ctx) {
  const admin = isAdmin();
  const header = pageHeader({
    title: t('Telemetry Extensions'),
    desc: t('Logs and tables that this project already collects but the built-in pages do not show. Pick one and a ready-made dashboard is created with one click; nothing needs to be typed.'),
  });
  const body = h('div', { class: 'stack-lg' });
  mount(ctx.root, header, body);
  mount(body, pageSkeleton(3, 2));

  // Discovery reads Cloud Logging, Monitoring and BigQuery and can take a while when it is not cached, so the
  // dashboards are shown as soon as they load and the source list follows.
  const discoveryReq = api.get('/api/v1/telemetry/discover', null, { signal: ctx.signal }).catch((e) => ({ error: e }));
  let cRes;
  try {
    cRes = await api.get('/api/v1/telemetry/connectors', null, { signal: ctx.signal });
  } catch (err) {
    if (isAbort(err) || ctx.signal.aborted) return null;
    mount(body, card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    return null;
  }
  if (ctx.signal.aborted) return null;
  const connectors = cRes.data.connectors || [];
  const pending = card({ title: t('Data you can add'), body: skeleton('block') });
  mount(body, warningsNotice(cRes.meta.warnings || []), howItWorks(), dashboardsSection(ctx, connectors), pending);

  const dRes = await discoveryReq;
  if (ctx.signal.aborted || isAbort(dRes.error)) return null;
  const discovery = dRes.error ? null : dRes.data;
  ctx.discovery = discovery;
  const sources = buildSources(discovery);
  const env = {
    admin,
    days: (discovery && discovery.activityDays) || 7,
    logs: (discovery && Array.isArray(discovery.logs)) ? discovery.logs : [],
    listed: Boolean(discovery && (Array.isArray(discovery.tables) || Array.isArray(discovery.logs))),
  };
  pending.replaceWith(...[
    warningsNotice((dRes.meta && dRes.meta.warnings) || []),
    dRes.error ? banner({ tone: 'warning', title: t('The sources of this project could not be listed'), text: errorText(dRes.error) }) : null,
    sourcesSection(ctx, sources, env),
    builtinSection(sources),
    notListedCard(ctx, env),
  ].filter(Boolean));
  return null;
}

/** One entry per source. A log routed to BigQuery is shown once, as its table (complete history, no read cap). */
function buildSources(discovery) {
  const tables = (discovery && Array.isArray(discovery.tables)) ? discovery.tables : [];
  const logs = (discovery && Array.isArray(discovery.logs)) ? discovery.logs : [];
  const tableIds = new Set(tables.map((tb) => tb.id));
  const logsById = new Map(logs.map((lg) => [lg.id, lg]));
  const out = tables.map((tb) => {
    const log = logsById.get(tb.id) || {};
    return {
      kind: 'bigquery', id: tb.id, project: tb.project || null, card: tb.catalog || null, quick: tb.quickAdd || null,
      connectorId: tb.connectorId || log.connectorId || null, core: Boolean(tb.core),
      tech: `${tb.project}.${tb.dataset}.${tb.table}`, rows: tb.rows, lastModified: tb.lastModified,
      entries7d: tb.entries7d, daily: tb.daily, logName: log.logName || null,
    };
  });
  logs.forEach((lg) => {
    if (lg.routedToBigQuery && tableIds.has(lg.id)) return;
    out.push({
      kind: 'logging', id: lg.id, project: lg.project || null, card: lg.catalog || null, quick: lg.quickAdd || null, connectorId: lg.connectorId || null,
      core: Boolean(lg.core), tech: lg.shortName, rows: null, lastModified: null, entries7d: lg.entries7d, daily: lg.daily,
      logName: lg.logName, sinkFilter: lg.sinkFilter || null,
    });
  });
  const titles = new Map();
  out.forEach((s) => { const k = cardTitle(s.card); titles.set(k, (titles.get(k) || 0) + 1); });
  out.forEach((s) => {
    if (titles.get(cardTitle(s.card)) > 1) s.variant = s.kind === 'logging' ? s.tech.replace(/^[^/]+\//, '') : s.tech.split('.').slice(1).join('.');
  });
  return out;
}

function isStale(s, days) {
  if (s.kind === 'logging') return s.entries7d === 0;
  if (s.rows === 0) return true;
  return Boolean(s.lastModified) && Date.now() - Date.parse(s.lastModified) > days * 86400000;
}

function activityScore(s) {
  if (isNum(s.entries7d)) return s.entries7d;
  return isNum(s.rows) ? s.rows : -1;
}

function byActivity(a, b) {
  return activityScore(b) - activityScore(a) || sourceTitle(a).localeCompare(sourceTitle(b));
}

function howItWorks() {
  const steps = [
    ['search', t('1. Found automatically'), t('Every log and table of this project is listed with a plain description of what it contains.')],
    ['plus', t('2. Added with one click'), t('Select Add to dashboard. The fields to chart are chosen for you.')],
    ['bar-chart', t('3. Ready-made dashboard'), t('Daily volume, the main breakdowns, users and the latest entries for the selected period.')],
  ];
  return h('ol', { class: 'tx-steps', 'aria-label': t('How it works') }, steps.map(([ic, title, text]) => h('li', { class: 'tx-step' },
    h('span', { class: 'tx-step-icon', 'aria-hidden': 'true' }, icon(ic, { size: 16 })),
    h('div', null, h('div', { class: 'tx-step-title' }, title), h('div', { class: 'tx-step-text' }, text)))));
}

function dashboardsSection(ctx, connectors) {
  const grid = h('div', { class: 'connector-grid' });
  connectors.forEach((c) => grid.append(connectorCard(ctx, c)));
  return h('section', { class: 'stack' },
    h('h2', { class: 'section-title' }, t('My dashboards'), h('span', { class: 'muted' }, String(connectors.length))),
    connectors.length ? grid : h('div', { class: 'src-empty' }, t('No dashboards yet. Choose a source below and select Add to dashboard.')));
}

function sourcesSection(ctx, sources, env) {
  const addable = sources.filter((s) => !s.core);
  const st = { cat: 'all', q: '' };
  const counts = {};
  addable.forEach((s) => { const id = categoryOf(s).id; counts[id] = (counts[id] || 0) + 1; });
  const chipsHost = h('div', { class: 'chips', role: 'group', 'aria-label': t('Filter by category') });
  const listHost = h('div', { class: 'stack-lg' });
  const matches = (s) => !st.q || [sourceTitle(s), cardWhat(s.card), s.tech].some((x) => String(x || '').toLowerCase().includes(st.q));
  const draw = () => {
    mount(chipsHost, [{ id: 'all', label: 'All' }, ...CATEGORIES].filter((c) => c.id === 'all' || counts[c.id]).map((c) => chip(
      `${t(c.label)} ${c.id === 'all' ? addable.length : counts[c.id]}`,
      { pressed: st.cat === c.id, onClick: () => { st.cat = c.id; draw(); } })));
    const visible = addable.filter((s) => (st.cat === 'all' || categoryOf(s).id === st.cat) && matches(s));
    const active = visible.filter((s) => !isStale(s, env.days));
    const stale = visible.filter((s) => isStale(s, env.days)).sort(byActivity);
    const nodes = [];
    CATEGORIES.forEach((c) => {
      const items = active.filter((s) => categoryOf(s).id === c.id).sort(byActivity);
      if (!items.length) return;
      nodes.push(h('div', { class: 'stack' },
        h('h3', { class: 'src-group-title' }, icon(c.icon, { size: 14 }), t(c.label), h('span', { class: 'muted' }, String(items.length))),
        h('div', { class: 'src-grid' }, items.map((s) => sourceCard(ctx, s, env)))));
    });
    if (!active.length) {
      const text = !env.listed ? t('The sources could not be listed. See the notices above.')
        : addable.length ? t('No source matches the filter.') : t('Every source of this project is already shown on the built-in pages.');
      nodes.push(h('div', { class: 'src-empty' }, text));
    }
    if (stale.length) {
      nodes.push(h('details', { class: 'src-more' },
        h('summary', null, t('{n} sources without data in the last {d} days', { n: stale.length, d: env.days })),
        h('div', { class: 'src-grid' }, stale.map((s) => sourceCard(ctx, s, env)))));
    }
    mount(listHost, nodes);
  };
  const search = searchBox({ placeholder: t('Search sources'), onInput: (v) => { st.q = v.trim().toLowerCase(); draw(); } });
  draw();
  return h('section', { class: 'stack' },
    h('div', { class: 'src-toolbar' },
      h('div', { class: 'stack-sm' },
        h('h2', { class: 'section-title' }, t('Data you can add'), h('span', { class: 'muted' }, String(addable.length))),
        h('p', { class: 'src-what' }, t('Collected in this project but not shown on the built-in pages. Sorted by recent volume.'))),
      addable.length ? search : null),
    addable.length ? chipsHost : null,
    listHost);
}

function dashboardShows(s) {
  const m = (s.quick && s.quick.mapping) || {};
  const c = s.card || {};
  const out = [t('Events per day')];
  if (m.operation) out.push(t('By {label}', { label: t(c.operationLabel || 'Operation') }));
  if (m.status) out.push(t('By {label}', { label: t(c.statusLabel || 'Status') }));
  if (m.user) out.push(t('Top users'));
  if (m.model) out.push(t('Top models'));
  if (m.tokens) out.push(t(c.tokensLabel || 'Tokens'));
  out.push(t('Latest entries'));
  return out;
}

function activityLine(s, days) {
  const spark = Array.isArray(s.daily) && s.daily.some((v) => v > 0) ? sparkline(s.daily, TONES.violet) : null;
  const parts = [];
  if (s.kind === 'logging') {
    parts.push(isNum(s.entries7d) ? t('{n} entries in the last {d} days', { n: fmt.int(s.entries7d), d: days }) : t('Recent volume unavailable'));
  } else {
    if (isNum(s.rows)) parts.push(t('{n} rows in BigQuery', { n: fmt.int(s.rows) }));
    if (s.lastModified) parts.push(t('updated {time}', { time: fmt.relative(s.lastModified) }));
    if (!parts.length) parts.push(t('Table statistics unavailable'));
  }
  return h('div', { class: 'src-activity' },
    spark ? h('span', { class: 'src-spark', title: t('Entries written per day, last {d} days', { d: days }) }, spark) : null,
    h('span', null, parts.join(' \u00b7 ')));
}

/** Logs read through the Logging API are capped per view; busy ones are better exported to BigQuery. */
function volumeNote(ctx, s, env) {
  if (s.kind !== 'logging' || !isNum(s.entries7d) || (s.entries7d / env.days) * rangeDays() <= MAX_LOG_READ) return null;
  return h('div', { class: 'src-note' }, icon('info', { size: 13 }),
    h('span', null, t('High volume: its dashboard counts only the latest {n} entries of a period. Export it to BigQuery for complete counts.', { n: fmt.int(MAX_LOG_READ) })),
    s.logName ? button({ label: t('Export to BigQuery'), size: 'xs', variant: 'ghost', icon: 'code', onClick: () => { if (requireAdmin(t('Export to BigQuery'))) openSinkGenerator(ctx, s, env.logs); } }) : null);
}

function sourceAction(ctx, s, env) {
  const btns = [];
  if (s.connectorId) {
    btns.push(button({ label: t('Open dashboard'), icon: 'arrow-right', size: 'sm', onClick: () => ctx.navigate(`#/extensions/${encodeURIComponent(s.connectorId)}`) }));
  } else if (!s.quick) {
    btns.push(h('span', { class: 'src-hint' }, t('This source cannot be added automatically. Use Custom setup below.')));
  } else {
    const btn = button({ label: t('Add to dashboard'), icon: 'plus', variant: 'primary', size: 'sm' });
    btn.addEventListener('click', () => {
      if (!requireAdmin(t('Add to dashboard'))) return;
      quickAdd(ctx, s, btn);
    });
    btns.push(btn);
  }
  if (s.kind === 'logging' && s.logName) {
    btns.push(button({
      label: t('Export to BigQuery'), icon: 'database', size: 'sm', variant: 'ghost',
      onClick: () => { if (requireAdmin(t('Export to BigQuery'))) openSinkGenerator(ctx, s, env && env.logs); },
    }));
  }
  return h('div', { class: 'row', style: { gap: '8px', flexWrap: 'wrap' } }, btns);
}

function sourceCard(ctx, s, env) {
  const c = s.card || {};
  const cat = categoryOf(s);
  return h('article', { class: ['src-card', `src-${cat.id}`] },
    h('div', { class: 'src-head' },
      h('span', { class: 'src-icon', 'aria-hidden': 'true' }, icon(cat.icon, { size: 16 })),
      h('div', { class: 'src-heading' },
        h('h4', { class: 'src-title', title: sourceTitle(s) }, sourceTitle(s)),
        h('div', { class: 'src-badges' },
          s.project ? badge(s.project, 'info', { size: 'sm', dot: true }) : null,
          badge(s.kind === 'bigquery' ? 'BigQuery' : 'Cloud Logging', 'neutral', { size: 'sm' }),
          c.sensitive ? badge(t('Contains prompt or response text'), 'warning', { size: 'sm' }) : null,
          s.connectorId ? badge(t('Added'), 'success', { size: 'sm', dot: true }) : null))),
    h('p', { class: 'src-what' }, cardWhat(c)),
    h('div', { class: 'src-block' },
      h('div', { class: 'src-label' }, t('Its dashboard shows')),
      h('div', { class: 'chips' }, dashboardShows(s).map((x) => chip(x)))),
    activityLine(s, env.days),
    volumeNote(ctx, s, env),
    h('div', { class: 'src-footer-wrap' },
      h('div', { class: 'src-block' },
        h('div', { class: 'src-label' }, s.kind === 'bigquery' ? t('BigQuery table') : t('Cloud Logging log name')),
        h('code', { class: 'src-code', title: s.tech }, s.tech)),
      h('div', { class: 'src-actions' }, sourceAction(ctx, s, env))));
}

async function quickAdd(ctx, s, btn) {
  if (s.card && s.card.sensitive) {
    const ok = await confirmDialog({
      title: t('Add a source that contains prompt or response text?'),
      message: t('The latest entries of its dashboard show that text to administrators. Viewers see it only when "Mask personal data for viewers" is turned off in Settings.'),
      confirmLabel: t('Add to dashboard'),
    });
    if (!ok) return;
  }
  const name = (sourceTitle(s) || s.quick.name || '').slice(0, 80);
  setBusy(btn, true, t('Adding'));
  try {
    const res = await api.post('/api/v1/telemetry/connectors', { ...s.quick, name });
    const created = res.data && res.data.connector;
    toast({ title: t('Dashboard added'), text: name, tone: 'success' });
    if (created && created.id) ctx.navigate(`#/extensions/${encodeURIComponent(created.id)}`);
    else ctx.rerender();
  } catch (err) {
    setBusy(btn, false);
    toast({ title: t('The dashboard could not be added'), text: errorText(err), tone: 'danger' });
  }
}

function pageLinks(c) {
  const ids = ((c && c.builtIn) || []).filter((id) => PAGES[id] && isModuleEnabled(PAGES[id][1]));
  if (!ids.length) return [h('span', { class: 'muted' }, DASH)];
  return ids.map((id) => h('a', { class: 'chip chip-link', href: `#/${id}` }, t(PAGES[id][0])));
}

function builtinSection(sources) {
  const core = sources.filter((s) => s.core).sort((a, b) => sourceTitle(a).localeCompare(sourceTitle(b)));
  if (!core.length) return null;
  return h('details', { class: 'card src-builtin' },
    h('summary', null,
      h('span', { class: 'src-builtin-title' }, t('Already shown on the built-in pages ({n})', { n: core.length })),
      h('span', { class: 'muted' }, t('No need to add these. Select a page to open it.'))),
    h('div', { class: 'src-builtin-list' }, core.map((s) => h('div', { class: 'src-builtin-row' },
      h('div', { class: 'src-builtin-main' },
        h('div', { class: 'src-builtin-name' }, sourceTitle(s)),
        h('p', { class: 'src-what' }, cardWhat(s.card)),
        h('code', { class: 'src-code' }, s.tech)),
      h('div', { class: 'src-builtin-pages' }, pageLinks(s.card))))));
}

function notListedCard(ctx, env) {
  return card({
    title: t('Looking for something that is not listed?'),
    body: h('div', { class: 'stack-sm' },
      h('p', { class: 'src-what' }, t('New logs appear here automatically within about 10 minutes after a product starts writing to Cloud Logging.')),
      h('p', { class: 'src-what' }, t('Log dashboards read Cloud Logging directly, so they cover its retention period (30 days by default) and count at most {n} entries per view. To keep longer history or count everything, export the log to BigQuery; the exported table then appears in this list.', { n: fmt.int(MAX_LOG_READ) })),
      h('p', { class: 'src-what' }, t('To chart only part of a log, use Custom setup. To list the tables of another BigQuery dataset, add it under Settings > Workspace and data sources > Additional telemetry datasets.')),
      h('div', { class: 'row' },
        button({ label: t('Custom setup'), icon: 'edit', onClick: () => { if (requireAdmin(t('Custom setup'))) openConnectorForm(ctx, null, null); } }),
        button({ label: t('Export logs to BigQuery'), icon: 'code', onClick: () => { if (requireAdmin(t('Export logs to BigQuery'))) openSinkGenerator(ctx, null, env.logs); } }))),
  });
}

function connectorCard(ctx, c) {
  const wsProj = ((store.boot && store.boot.workspace) || {}).projectId || '';
  const projId = c.project || wsProj;
  const stats = h('div', { class: 'connector-stats' },
    h('div', null, h('div', { class: 'cs-label' }, t('Events')), h('div', { class: 'cs-value' }, '\u2026')),
    h('div', null, h('div', { class: 'cs-label' }, t('Users')), h('div', { class: 'cs-value' }, '\u2026')),
    h('div', null, h('div', { class: 'cs-label' }, t('Tokens')), h('div', { class: 'cs-value' }, '\u2026')));
  const sparkHost = h('div', { class: 'connector-spark' });
  sparkHost.style.setProperty('height', '30px');
  const metaRow = h('div', { class: 'connector-meta' },
    projId ? badge(projId, 'info', { size: 'sm', dot: true, title: t('Project: {id}', { id: projId }) }) : null,
    badge(c.source === 'bigquery' ? 'BigQuery' : 'Cloud Logging', 'neutral', { size: 'sm' }),
    c.source === 'logging' && c.bigquerySink ? badge(t('BQ sink linked'), 'success', { size: 'sm', dot: true, title: t('BigQuery linked: {dest}', { dest: c.bigquerySink }) }) : null);
  const fullRef = `${c.source === 'bigquery' ? 'BigQuery' : 'Cloud Logging'} \u00b7 ${sourceRef(c)}`;
  const bottom = h('div', { class: 'connector-bottom' },
    h('code', { class: 'src-code', title: fullRef }, fullRef),
    stats,
    sparkHost);
  const el = card({
    cls: 'connector-card',
    title: c.name,
    subtitle: c.catalog ? cardWhat(c.catalog) : t('Custom connector set up by an administrator.'),
    actions: c.enabled ? badge(t('Enabled'), 'success', { size: 'sm', dot: true }) : badge(t('Disabled'), 'neutral', { size: 'sm' }),
    body: [metaRow, bottom],
    footer: h('span', null, c.lastEvent ? t('Last event {time}', { time: fmt.relative(c.lastEvent) }) : t('No events yet')),
    source: c.health === 'error' ? t('Source error') : null,
  });
  el.tabIndex = 0;
  el.setAttribute('role', 'link');
  el.addEventListener('click', () => ctx.navigate(`#/extensions/${encodeURIComponent(c.id)}`));
  el.addEventListener('keydown', (e) => { if (e.key === 'Enter') ctx.navigate(`#/extensions/${encodeURIComponent(c.id)}`); });
  if (c.enabled) {
    api.get('/api/v1/telemetry/connectors/data', rangeParams({ id: c.id }), { signal: ctx.signal }).then((res) => {
      const k = res.data.kpis || {};
      const plus = res.data.sample && res.data.sample.partial ? '+' : '';
      const vals = stats.querySelectorAll('.cs-value');
      vals[0].textContent = isNum(k.events && k.events.value) ? `${fmt.compact(k.events.value)}${plus}` : DASH;
      vals[1].textContent = isNum(k.users) ? `${fmt.int(k.users)}${plus}` : DASH;
      vals[2].textContent = isNum(k.tokens) ? `${fmt.compact(k.tokens)}${plus}` : DASH;
      const ev = ((res.data.trend && res.data.trend.series) || []).find((s) => s.key === 'events');
      if (ev) mount(sparkHost, sparkline(ev.values, TONES.violet));
    }).catch(() => {
      stats.querySelectorAll('.cs-value').forEach((v) => { v.textContent = DASH; });
    });
  } else {
    stats.querySelectorAll('.cs-value').forEach((v) => { v.textContent = DASH; });
  }
  return el;
}

/* ------------------------------------------------------------------ connector dashboard */

/** "Add to Overview" / "Remove from Overview" for a dashboard: pins a summary widget to the person's Overview. */
function pinControl(c) {
  const host = h('span', { class: 'row' });
  const render = (pinned, busy) => mount(host, button({
    label: pinned ? t('Remove from Overview') : t('Add to Overview'), icon: pinned ? 'check' : 'grid', disabled: busy,
    onClick: async () => {
      render(pinned, true);
      try {
        const now = await togglePin('overview', c.id);
        toast({ title: now ? t('Added to the Overview') : t('Removed from the Overview'), text: now ? t('Drag it on the Overview to change its place.') : null, tone: 'success' });
        render(now, false);
      } catch (err) {
        toast({ title: t('The widget layout could not be saved'), text: errorText(err), tone: 'danger' });
        render(pinned, false);
      }
    },
  }));
  render(false, true);
  loadLayouts().then(() => { if (canEditLayouts()) render(isPinned('overview', c.id), false); else mount(host); }).catch(() => mount(host));
  return host;
}

function backButton(ctx) {
  return button({ label: t('Telemetry Extensions'), icon: 'chevron-left', variant: 'ghost', onClick: () => ctx.navigate('#/extensions') });
}

function breakdownCard(title, list, key, subtitle, labelOf = shortValue) {
  const items = mapList(list, (o, i) => ({
    label: labelOf(o[key]), title: o[key] === null || o[key] === undefined ? '' : String(o[key]), value: o.events, color: colorAt(i),
  }));
  return card({ title, subtitle, cls: 'tx-wrap', body: hbarList(items) });
}

async function renderConnector(ctx, id) {
  const body = h('div', { class: 'stack-lg' });
  const headerSlot = h('div');
  mount(ctx.root, headerSlot, body);
  mount(headerSlot, pageHeader({ title: t('Dashboard'), desc: t('Loading') }));
  mount(body, pageSkeleton(4, 2));
  let res;
  try {
    res = await api.get('/api/v1/telemetry/connectors/data', rangeParams({ id }), { signal: ctx.signal });
  } catch (err) {
    if (isAbort(err) || ctx.signal.aborted) return null;
    mount(headerSlot, pageHeader({ title: t('Dashboard'), actions: backButton(ctx) }));
    mount(body, card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    return null;
  }
  if (ctx.signal.aborted) return null;
  const d = res.data;
  const c = d.connector || {};
  const cat = c.catalog || null;
  const m = c.mapping || {};
  const opLabel = t((cat && cat.operationLabel) || 'Operation');
  const statusLabel = t((cat && cat.statusLabel) || 'Status');
  const tokensLabel = t((cat && cat.tokensLabel) || 'Tokens');
  const statusKey = (cat && cat.statusLabel) || 'Status';
  // Agent Engine IDs and similar values are shown with their display names when the server could resolve them.
  const opNames = (d.valueNames && d.valueNames.operation) || {};
  const opLabelOf = (v) => (v && opNames[v] ? `${opNames[v]} \u00b7 ${shortValue(v)}` : shortValue(v));
  const statusOf = (v) => statusText(v, statusKey);
  const wsProj = ((store.boot && store.boot.workspace) || {}).projectId || '';
  const connProj = c.project || wsProj;
  const toggle = switchInput({
    checked: c.enabled, label: t('Enabled'),
    onChange: async (v, input) => {
      if (!requireAdmin(t('Enabled'))) { input.checked = !v; return; }
      try {
        await api.post('/api/v1/telemetry/connectors', { ...c, enabled: v });
        toast({ title: v ? t('Dashboard turned on') : t('Dashboard turned off'), tone: 'success' });
      } catch (err) {
        input.checked = !v;
        toast({ title: t('Update failed'), text: errorText(err), tone: 'danger' });
      }
    },
  });
  mount(headerSlot, pageHeader({
    title: c.name || id,
    desc: cat ? cardWhat(cat) : t('Custom connector set up by an administrator.'),
    project: connProj,
    projectScope: c.projectScope || 'single',
    bigquerySink: c.bigquerySink || null,
    actions: [
      backButton(ctx),
      h('label', { class: 'row' }, h('span', { class: 'muted' }, t('Enabled')), toggle),
      c.id ? pinControl(c) : null,
      button({ label: t('Edit'), icon: 'edit', onClick: () => { if (requireAdmin(t('Edit'))) openConnectorForm(ctx, c, null); } }),
      button({ label: t('Delete'), icon: 'trash', variant: 'danger', onClick: () => { if (requireAdmin(t('Delete'))) deleteConnector(ctx, c); } }),
    ],
  }));
  mount(body);
  body.append(h('div', { class: 'tx-source' },
    connProj ? badge(t('Project: {id}', { id: connProj }), 'info', { size: 'sm', dot: true }) : null,
    badge(c.source === 'bigquery' ? 'BigQuery' : 'Cloud Logging', 'neutral', { size: 'sm' }),
    c.bigquerySink ? badge(t('BigQuery linked: {dest}', { dest: c.bigquerySink }), 'success', { size: 'sm', dot: true }) : null,
    h('code', { class: 'src-code' }, sourceRef(c))));
  const sample = d.sample || null;
  const partial = Boolean(sample && sample.partial);
  const plus = partial ? '+' : '';
  const sampleNote = partial ? t('Latest {n} entries only', { n: fmt.int(sample.limit) }) : null;
  if (partial) {
    const logName = logNameOf(c);
    const connSource = {
      kind: 'logging', id: c.id, project: connProj, card: cat, quick: c,
      tech: sourceRef(c), logName, sinkFilter: c.logging && c.logging.filter,
    };
    body.append(banner({
      tone: 'warning',
      title: t('Only the latest {n} entries of this period were counted', { n: fmt.int(sample.limit) }),
      text: t('This log has more entries in the selected period than a dashboard reads from Cloud Logging. Counts start at {time}; earlier days have no bars. Export the log to BigQuery for complete counts.', { time: fmt.dateTime(sample.since, true) }),
      actions: logName ? button({ label: t('Export to BigQuery'), icon: 'database', size: 'sm', onClick: () => { if (requireAdmin(t('Export to BigQuery'))) openSinkGenerator(ctx, connSource, []); } }) : null,
    }));
  }
  if (cat && cat.sensitive) {
    body.append(banner({ tone: 'warning', title: t('Contains prompt or response text'), text: t('The latest entries of its dashboard show that text to administrators. Viewers see it only when "Mask personal data for viewers" is turned off in Settings.') }));
  }
  // The read cap is explained above (or does not affect this period), so its generic notice is left out.
  const notice = warningsNotice(((res.meta && res.meta.warnings) || []).filter((w) => !(sample && w.code === 'truncated')));
  if (notice) body.append(notice);

  const k = d.kpis || {};
  body.append(h('div', { class: 'kpi-grid' }, [
    kpi({
      label: t('Events'), value: isNum(k.events && k.events.value) ? `${fmt.compact(k.events.value)}${plus}` : DASH, delta: deltaOf(k.events), goodWhen: null,
      meta: sample && sample.truncated && !partial ? h('span', null, t('No comparison: the previous period has too many entries')) : null,
    }),
    m.user ? kpi({ label: t('Active users'), value: isNum(k.users) ? `${fmt.int(k.users)}${plus}` : DASH }) : null,
    m.tokens ? kpi({ label: tokensLabel, value: isNum(k.tokens) ? `${fmt.compact(k.tokens)}${plus}` : DASH }) : null,
    kpi({ label: t('Last event'), value: k.lastEvent ? fmt.relative(k.lastEvent) : DASH }),
  ].filter(Boolean)));

  const row = h('div', { class: 'grid' });
  const trend = chartCard({
    title: t('Events per day'), subtitle: partial ? `${rangeText()} \u00b7 ${t('counted from {time}', { time: fmt.dateTime(sample.since, true) })}` : rangeText(),
    build: (host) => barChart(host, { ...fromTimeline(d.trend, { include: ['events'], names: { events: t('Events') }, colors: { events: TONES.violet } }), integer: true, height: 240 }),
  });
  const charts = [trend];
  if (m.operation) {
    trend.classList.add('span-8');
    const ops = breakdownCard(t('By {label}', { label: opLabel }), d.topOperations, 'operation', sampleNote, opLabelOf);
    ops.classList.add('span-4');
    charts.push(ops);
  } else {
    trend.classList.add('span-12');
  }
  body.append(row);
  mountCharts(row, charts);

  const extra = [];
  if (m.status) extra.push(breakdownCard(t('By {label}', { label: statusLabel }), d.topStatuses, 'status', sampleNote, statusOf));
  if (m.user) {
    extra.push(card({
      title: t('Top users'), flush: true,
      body: dataTable({ columns: [{ key: 'user', label: t('User'), render: (r) => userCell(r.user) }, { key: 'events', label: t('Events'), align: 'num', format: fmt.int }], rows: d.topUsers || [], searchable: false, pageSize: 8, initialSort: { key: 'events', dir: 'desc' } }),
    }));
  }
  if (m.model) {
    extra.push(card({
      title: t('Top models'), flush: true,
      body: dataTable({ columns: [{ key: 'model', label: t('Model'), render: (r) => h('span', { class: 'cell-mono' }, r.model || DASH) }, { key: 'events', label: t('Events'), align: 'num', format: fmt.int }, { key: 'tokens', label: tokensLabel, align: 'num', format: fmt.compact }], rows: d.topModels || [], searchable: false, pageSize: 8, initialSort: { key: 'events', dir: 'desc' } }),
    }));
  }
  if (extra.length) {
    const row2 = h('div', { class: 'grid' });
    const spanCls = extra.length === 1 ? 'span-12' : extra.length === 2 ? 'span-6' : 'span-4';
    extra.forEach((x) => { x.classList.add(spanCls); row2.append(x); });
    body.append(row2);
  }

  const recent = Array.isArray(d.recent) ? d.recent : null;
  const hasMessage = (recent || []).some((r) => r.message);
  const hasPayload = (recent || []).some((r) => r.payload);
  const cols = [
    { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time, true), value: (r) => r.time },
    { key: 'project', label: t('Project'), render: (r) => badge(r.project || connProj || DASH, 'neutral', { size: 'sm' }), value: (r) => r.project || connProj || '' },
  ];
  if (m.user) cols.push({ key: 'user', label: t('User'), render: (r) => h('span', { class: 'cell-mono truncate', style: { maxWidth: '180px' }, title: r.user || '' }, r.user || DASH) });
  if (m.operation) {
    cols.push({
      key: 'operation', label: opLabel,
      render: (r) => (r.operation && opNames[r.operation]
        ? h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main truncate', style: { maxWidth: '240px' }, title: opNames[r.operation] }, opNames[r.operation]), h('span', { class: 'cell-sub cell-mono truncate', style: { maxWidth: '240px' }, title: r.operation }, r.operation))
        : h('span', { class: 'cell-mono truncate', style: { maxWidth: '240px' }, title: r.operation || '' }, r.operation || DASH)),
    });
  }
  if (m.model) cols.push({ key: 'model', label: t('Model'), render: (r) => h('span', { class: 'cell-mono' }, r.model || DASH) });
  if (m.tokens) cols.push({ key: 'tokens', label: tokensLabel, align: 'num', format: fmt.int });
  if (m.status) cols.push({ key: 'status', label: statusLabel, render: (r) => (r.status !== null && r.status !== undefined ? badge(statusOf(r.status), 'neutral', { size: 'sm' }) : DASH) });
  if (hasMessage) cols.push({ key: 'message', label: t('Message'), sortable: false, render: (r) => h('span', { class: 'cell-wrap tx-message' }, r.message || DASH) });
  body.append(card({
    title: t('Latest entries'),
    subtitle: hasPayload ? t('The latest {n} entries of the period. Select a row to see the full entry.', { n: (recent || []).length })
      : t('The latest {n} entries of the period.', { n: (recent || []).length }),
    flush: true,
    body: dataTable({
      columns: cols, rows: recent, pageSize: 10, exportName: `connector-${c.id || id}`,
      onRowClick: hasPayload ? (r) => { if (r.payload) openDrawer({ title: t('Full entry'), subtitle: fmt.dateTime(r.time, true), width: 720, content: codeBlock(prettyJson(r.payload), { wrap: true }) }); } : null,
    }),
  }));
  return null;
}

function prettyJson(text) {
  if (!text) return '';
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch (e) { return String(text); }
}

async function deleteConnector(ctx, c) {
  const ok = await confirmDialog({ title: t('Delete dashboard'), message: t('The dashboard {name} will be removed. The underlying data is not affected.', { name: c.name }), confirmLabel: t('Delete'), tone: 'danger' });
  if (!ok) return;
  try {
    await api.post('/api/v1/telemetry/connectors/delete', { id: c.id });
    toast({ title: t('Dashboard deleted'), tone: 'success' });
    ctx.navigate('#/extensions');
  } catch (err) {
    toast({ title: t('Delete failed'), text: errorText(err), tone: 'danger' });
  }
}

/* ------------------------------------------------------------------ custom setup (manual connector form) */

async function openConnectorForm(ctx, existing, table, logFilter) {
  let discovery = ctx.discovery || { tables: [], logs: [] };
  const getTables = () => (discovery.tables || []).filter((tb) => !tb.core || (existing && existing.bigquery && existing.bigquery.table === tb.logical));
  let tables = getTables();
  const st = {
    id: existing ? existing.id : undefined,
    name: existing ? existing.name : (table ? prettyName(table.logical) : ''),
    enabled: existing ? existing.enabled : true,
    source: existing ? existing.source : (logFilter ? 'logging' : 'bigquery'),
    tableKey: existing && existing.bigquery ? `${existing.bigquery.project}.${existing.bigquery.dataset}.${existing.bigquery.table}` : (table ? `${table.project}.${table.dataset}.${table.logical}` : ''),
    filter: existing && existing.logging ? existing.logging.filter : (logFilter || ''),
    mapping: existing ? { ...existing.mapping } : (table ? { ...(table.suggestedMapping || {}) } : {}),
  };
  const err = h('div', { class: 'field-error' });
  const sourceHost = h('div', { class: 'stack' });
  const mappingHost = h('div', { class: 'field-row' });
  const nameIn = textInput({ value: st.name, placeholder: t('For example CodeMender'), onInput: (v) => { st.name = v; } });

  const findTable = () => tables.find((tb) => `${tb.project}.${tb.dataset}.${tb.logical}` === st.tableKey);
  const renderMapping = () => {
    const tb = findTable();
    mount(mappingHost, MAPPING_FIELDS.map(([key, label, required]) => {
      let control;
      if (st.source === 'bigquery') {
        const cols = (tb && tb.columns) || [];
        control = selectInput([{ value: '', label: required ? t('Select a column') : t('Not mapped') }, ...cols.map((cname) => ({ value: cname, label: cname }))], st.mapping[key] || '', (v) => { st.mapping[key] = v || null; }, { 'aria-label': t(label) });
      } else {
        control = textInput({ value: st.mapping[key] || '', mono: true, placeholder: key === 'time' ? 'timestamp' : key === 'user' ? 'labels.user_id' : key === 'tokens' ? 'jsonPayload.metadata.totalTokenCount' : '', onInput: (v) => { st.mapping[key] = v.trim() || null; } });
      }
      return field({ label: `${t(label)}${required ? ' *' : ''}`, control });
    }));
  };
  const renderSource = () => {
    if (st.source === 'bigquery') {
      mount(sourceHost, field({
        label: t('BigQuery table'),
        help: t('Tables discovered in the audit and telemetry datasets.'),
        control: selectInput([{ value: '', label: t('Select a table') }, ...tables.map((tb) => ({ value: `${tb.project}.${tb.dataset}.${tb.logical}`, label: `${tb.dataset}.${tb.logical}${tb.sharded ? ' (sharded)' : ''}` }))], st.tableKey, (v) => {
          st.tableKey = v;
          const tb = findTable();
          if (tb) { st.mapping = { ...(tb.suggestedMapping || {}) }; if (!st.name) { st.name = prettyName(tb.logical); nameIn.value = st.name; } }
          renderMapping();
        }),
      }));
    } else {
      const ta = h('textarea', { class: 'textarea mono', rows: 3, placeholder: 'logName:"codemender"', maxlength: 2000 });
      ta.value = st.filter;
      ta.addEventListener('input', () => { st.filter = ta.value; });
      mount(sourceHost, field({ label: t('Cloud Logging filter'), help: t('Any Logging query language filter. Entries are read through the Logging API for the selected period.'), control: ta }));
    }
    renderMapping();
  };
  const save = button({ label: existing ? t('Save connector') : t('Create connector'), variant: 'primary' });
  const m = openModal({
    title: existing ? t('Edit connector') : t('Custom setup'),
    subtitle: t('Map the source fields once; the control plane builds a standard dashboard for it.'),
    wide: true,
    body: (el) => append(el, [
      h('div', { class: 'field-row' },
        field({ label: t('Display name'), control: nameIn }),
        field({ label: t('Source type'), control: selectInput([{ value: 'bigquery', label: t('BigQuery table') }, { value: 'logging', label: t('Cloud Logging filter') }], st.source, (v) => { st.source = v; st.mapping = {}; renderSource(); }) })),
      sourceHost,
      h('div', { class: 'field-label' }, t('Field mapping')),
      mappingHost,
      h('label', { class: 'row' }, switchInput({ checked: st.enabled, label: t('Enable connector'), onChange: (v) => { st.enabled = v; } }), h('span', null, t('Enable connector and show it on dashboards'))),
      err]),
    footer: (el) => append(el, [button({ label: t('Cancel'), onClick: () => m.close() }), save]),
  });
  renderSource();
  if (!ctx.discovery) {
    api.get('/api/v1/telemetry/discover').then((res) => {
      discovery = res.data || { tables: [], logs: [] };
      ctx.discovery = discovery;
      tables = getTables();
      if (st.source === 'bigquery') renderSource();
    }).catch(() => {});
  }
  save.addEventListener('click', async () => {
    err.textContent = '';
    if (!st.name.trim()) { err.textContent = t('Enter a display name.'); return; }
    if (!st.mapping.time) { err.textContent = t('Map the timestamp field.'); return; }
    const payload = { id: st.id, name: st.name.trim(), enabled: st.enabled, source: st.source, mapping: st.mapping };
    if (st.source === 'bigquery') {
      const tb = findTable();
      if (!tb) { err.textContent = t('Select a BigQuery table.'); return; }
      payload.bigquery = { project: tb.project, dataset: tb.dataset, table: tb.logical };
    } else {
      if (!st.filter.trim()) { err.textContent = t('Enter a Cloud Logging filter.'); return; }
      payload.logging = { filter: st.filter.trim() };
    }
    setBusy(save, true, t('Saving'));
    try {
      const res = await api.post('/api/v1/telemetry/connectors', payload);
      toast({ title: existing ? t('Connector saved') : t('Connector created'), tone: 'success' });
      m.close();
      const created = res.data && (res.data.connector || res.data);
      if (!existing && created && created.id) ctx.navigate(`#/extensions/${encodeURIComponent(created.id)}`);
      else ctx.rerender();
    } catch (e) {
      setBusy(save, false);
      err.textContent = errorText(e);
    }
  });
}

function prettyName(logical) {
  return String(logical || '').replace(/_googleapis_com/g, '').split('_').filter(Boolean).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

/* ------------------------------------------------------------------ export logs to BigQuery (one-click sink + dashboard) */

function mappedFieldsSummary(s) {
  const m = (s && s.quick && s.quick.mapping) || {};
  const c = (s && s.card) || {};
  const items = [
    [t('Timestamp'), m.time || 'timestamp'],
    m.operation ? [t(c.operationLabel || 'Operation'), m.operation] : null,
    m.status ? [t(c.statusLabel || 'Status'), m.status] : null,
    m.user ? [t('User'), m.user] : null,
    m.model ? [t('Model'), m.model] : null,
    m.tokens ? [t(c.tokensLabel || 'Tokens'), m.tokens] : null,
  ].filter(Boolean);
  return h('div', { class: 'chips' }, items.map(([k, v]) => chip(`${k}: ${v}`)));
}

function openSinkGenerator(ctx, source = null, logs = []) {
  const ws = (store.boot && store.boot.workspace) || {};
  const destProject = ws.projectId || 'PROJECT_ID';
  const candidateSources = (logs || []).filter((l) => !l.routedToBigQuery).map((lg) => ({
    kind: 'logging', id: lg.id, project: lg.project || destProject, card: lg.catalog || null,
    quick: lg.quickAdd || null, tech: lg.shortName, logName: lg.logName, sinkFilter: lg.sinkFilter || null,
  }));
  let activeSource = source || candidateSources[0] || null;
  const defaultSinkName = (s) => {
    const slug = String((s && s.id) || 'telemetry').replace(/[^a-zA-Z0-9_]+/g, '_').replace(/^_+|_+$/g, '').toLowerCase();
    return `cp_sink_${slug || 'telemetry'}`.slice(0, 80);
  };
  const st = {
    dataset: ws.auditDataset || 'ge_analytics',
    sink: defaultSinkName(activeSource),
    custom: '',
  };
  const errEl = h('div', { class: 'field-error' });
  const summaryHost = h('div', { class: 'stack' });
  const advOut = h('div', { class: 'stack' });
  const sinkIn = textInput({ value: st.sink, mono: true, onInput: (v) => { st.sink = v.replace(/[^A-Za-z0-9_\-.]/g, ''); build(); } });
  const datasetIn = textInput({ value: st.dataset, mono: true, onInput: (v) => { st.dataset = v.replace(/[^A-Za-z0-9_]/g, ''); build(); } });

  const buildFilter = () => {
    let base = '';
    if (activeSource && activeSource.sinkFilter) {
      base = activeSource.sinkFilter;
    } else if (activeSource && activeSource.logName) {
      base = `logName="${activeSource.logName}"`;
    } else {
      base = `logName="projects/${destProject}/logs/LOG_ID"`;
    }
    if (st.custom.trim()) {
      return `(${base}) AND (${st.custom.trim()})`;
    }
    return base;
  };

  const build = () => {
    const srcProj = (activeSource && activeSource.project) || destProject;
    const filter = buildFilter();
    const titleText = activeSource ? sourceTitle(activeSource) : t('Telemetry logs');
    const metrics = activeSource ? dashboardShows(activeSource) : [t('Events per day'), t('Latest entries')];
    mount(summaryHost,
      h('div', { class: 'card', style: { padding: '14px 16px', background: 'var(--bg-subtle, rgba(99,102,241,0.05))' } },
        h('div', { class: 'stack-sm' },
          h('div', { class: 'row', style: { justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '8px' } },
            h('strong', { style: { fontSize: '14px' } }, titleText),
            h('div', { class: 'row', style: { gap: '6px' } },
              badge(srcProj, 'neutral', { size: 'sm', mono: true }),
              badge('Cloud Logging \u2192 BigQuery', 'info', { size: 'sm' }))),
          activeSource && activeSource.card ? h('p', { class: 'src-what', style: { margin: '2px 0 6px' } }, cardWhat(activeSource.card)) : null,
          h('div', { class: 'src-label' }, t('Metrics & charts for this source')),
          h('div', { class: 'chips' }, metrics.map((x) => chip(x))),
          activeSource ? h('div', { class: 'stack-xs', style: { marginTop: '6px' } },
            h('div', { class: 'src-label' }, t('Auto-mapped telemetry fields')),
            mappedFieldsSummary(activeSource)) : null,
          h('div', { class: 'stack-xs', style: { marginTop: '6px' } },
            h('div', { class: 'src-label' }, t('What will be configured automatically when you click the button')),
            h('ul', { style: { margin: '4px 0 0 18px', padding: 0, fontSize: '12.5px', lineHeight: '1.6' } },
              h('li', null, t('Create or update Cloud Logging sink {sink} in project {project}', { sink: st.sink || 'cp_sink', project: srcProj })),
              h('li', null, t('Connect partitioned tables in BigQuery dataset {dataset} and grant the sink service account write access automatically', { dataset: `${destProject}.${st.dataset}` })),
              h('li', null, t('Create and open the {title} dashboard immediately', { title: titleText })))))),
    );
    const cmd = [
      `gcloud logging sinks create ${st.sink} \\`,
      `  bigquery.googleapis.com/projects/${destProject}/datasets/${st.dataset} \\`,
      `  --log-filter='${filter.replace(/'/g, "'\\''")}' \\`,
      '  --use-partitioned-tables \\',
      `  --project=${srcProj}`,
    ].join('\n');
    mount(advOut,
      field({ label: t('Log Router filter'), control: codeBlock(filter, { wrap: true }) }),
      field({ label: t('gcloud commands (reference only — executed automatically by the button below)'), control: codeBlock(cmd) }));
  };

  const runBtn = button({ label: t('Auto-configure BigQuery & open dashboard'), icon: 'check', variant: 'primary' });
  const modalTitle = source ? `${sourceTitle(source)} \u00b7 ${t('Export to BigQuery')}` : t('Export logs to BigQuery');
  const modalSub = source && source.card
    ? cardWhat(source.card)
    : t('Select a telemetry source below and click the button to automatically create the Cloud Logging sink, grant BigQuery permissions, and open its dashboard.');

  const m = openModal({
    title: modalTitle,
    subtitle: modalSub,
    wide: true,
    body: (el) => {
      const sourcePicker = (!source && candidateSources.length) ? field({
        label: t('Select telemetry source'),
        control: selectInput(
          candidateSources.map((s) => ({
            value: s.id,
            label: `${sourceTitle(s)} (${s.project || destProject}) \u2014 ${dashboardShows(s).slice(0, 3).join(' \u00b7 ')}`,
          })),
          activeSource ? activeSource.id : '',
          (val) => {
            const found = candidateSources.find((s) => s.id === val);
            if (found) {
              activeSource = found;
              st.sink = defaultSinkName(found);
              sinkIn.value = st.sink;
              build();
            }
          },
        ),
      }) : null;
      const custom = h('textarea', { class: 'textarea mono', rows: 2, placeholder: 'severity>="DEFAULT"' });
      custom.addEventListener('input', () => { st.custom = custom.value; build(); });
      append(el, [
        sourcePicker,
        summaryHost,
        h('div', { class: 'field-row' },
          field({ label: t('Sink name'), control: sinkIn }),
          field({ label: t('Destination dataset'), control: datasetIn })),
        h('details', { class: 'src-more' },
          h('summary', null, t('Advanced filter & CLI reference (optional)')),
          h('div', { class: 'stack', style: { marginTop: '10px' } },
            field({ label: t('Additional filter (optional)'), help: t('For example a product log name or resource type.'), control: custom }),
            advOut)),
        errEl,
      ].filter(Boolean));
      build();
    },
    footer: (el) => append(el, [button({ label: t('Cancel'), onClick: () => m.close() }), runBtn]),
  });

  runBtn.addEventListener('click', async () => {
    errEl.textContent = '';
    const srcProj = (activeSource && activeSource.project) || destProject;
    const quick = activeSource && activeSource.quick
      ? { ...activeSource.quick, name: (sourceTitle(activeSource) || activeSource.quick.name || '').slice(0, 80) }
      : null;
    setBusy(runBtn, true, t('Configuring automatically'));
    try {
      const res = await api.post('/api/v1/telemetry/sinks', {
        sourceId: activeSource ? activeSource.id : 'telemetry',
        sinkName: st.sink,
        dataset: st.dataset,
        project: srcProj,
        filter: buildFilter(),
        logNames: activeSource && activeSource.logName && !activeSource.sinkFilter ? [activeSource.logName] : [],
        quickAdd: quick,
        createConnector: true,
      });
      const data = res.data || {};
      const conn = data.connector;
      m.close();
      toast({
        title: t('BigQuery sink and dashboard configured'),
        text: `${st.sink} \u2192 ${destProject}.${st.dataset}`,
        tone: 'success',
      });
      if (conn && conn.id && ctx && typeof ctx.navigate === 'function') {
        ctx.navigate(`#/extensions/${encodeURIComponent(conn.id)}`);
      } else if (ctx && typeof ctx.rerender === 'function') {
        ctx.rerender();
      }
    } catch (e) {
      setBusy(runBtn, false);
      errEl.textContent = errorText(e);
    }
  });
}
