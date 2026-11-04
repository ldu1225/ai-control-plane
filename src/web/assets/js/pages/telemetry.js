/**
 * Telemetry Extensions: dynamically discovered Cloud Logging streams and BigQuery tables,
 * Gemini source profiling & grouping, bundled group dashboards, administrator source overrides,
 * and cost-controlled Log Router -> BigQuery routing (sampling, severity filter, exclusions,
 * partition retention, daily GiB budgets with automatic pause/resume, and monthly cost estimates).
 */
import { h, mount, append, debounce } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t, getLocale } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api, isAbort } from '../core/api.js';
import { store, isAdmin, isModuleEnabled, rangeParams } from '../core/state.js';
import {
  pageHeader, kpi, card, dataTable, badge, button, banner, switchInput, openModal, openDrawer, toast, setBusy, field, textInput,
  selectInput, errorState, warningsNotice, confirmDialog, codeBlock, chip, userCell, searchBox, skeleton, requireAdmin, stat, progress, emptyState,
} from '../core/components.js';
import { errorText, serverText } from '../core/messages.js';
import { barChart, hbarList, mapList, sparkline, fromTimeline, TONES, colorAt } from '../core/charts.js';
import { rangeText, chartCard, mountCharts, pageSkeleton, deltaOf } from './_shared.js';
import { cardTitle, cardWhat, cardGroup, cardFieldLabel, shortValue, statusText } from '../core/telemetry_view.js';
import { loadLayouts, isPinned, togglePin, canEditLayouts } from '../core/widgets.js';

const MAPPING_FIELDS = [
  ['time', 'Timestamp', true],
  ['user', 'User', false],
  ['operation', 'Operation', false],
  ['model', 'Model', false],
  ['tokens', 'Tokens', false],
  ['status', 'Status', false],
];

const SEVERITIES = ['DEFAULT', 'DEBUG', 'INFO', 'NOTICE', 'WARNING', 'ERROR', 'CRITICAL', 'ALERT', 'EMERGENCY'];

const CATEGORIES = [
  { id: 'ai', label: 'AI and agents', icon: 'spark' },
  { id: 'security', label: 'Security and audit', icon: 'shield' },
  { id: 'operations', label: 'Operations', icon: 'server' },
  { id: 'other', label: 'Other', icon: 'database' },
];

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
const WORKLOAD_FILTER_RE = /resource\.labels\.[A-Za-z0-9_]+="([A-Za-z0-9_-]+)"/;

export default async function render(ctx) {
  if (ctx.params[0]) return renderConnector(ctx, ctx.params[0]);
  return renderList(ctx);
}

/* ------------------------------------------------------------------ text helpers */

function sourceTitle(s) {
  const base = cardTitle(s.card) || s.tech;
  return s.variant ? `${base} (${s.variant})` : base;
}

function cardRouteReason(c) {
  if (!c) return '';
  if (getLocale() === 'ko' && c.routeReasonKo) return c.routeReasonKo;
  return c.routeReason || '';
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

function sourceRef(conn) {
  if (conn.source === 'bigquery' && conn.bigquery) return `${conn.bigquery.project}.${conn.bigquery.dataset}.${conn.bigquery.table}`;
  const flt = (conn.logging && conn.logging.filter) || '';
  const lid = logIdOf(conn);
  if (lid) return lid;
  const wm = WORKLOAD_FILTER_RE.exec(flt);
  if (wm) return wm[1];
  return flt || '';
}

function rangeDays() {
  const start = Date.parse(store.range.start);
  const end = Date.parse(store.range.end);
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(1, Math.round((end - start) / 86400000) + 1) : 14;
}

function fmtGib(gib) {
  if (!isNum(gib)) return DASH;
  if (gib === 0) return '0 GiB';
  if (gib < 0.01) return `${fmt.num(gib * 1024, 1)} MiB`;
  return `${fmt.num(gib, gib < 1 ? 2 : 1)} GiB`;
}

function fmtEstimate(est) {
  if (!est) return null;
  const parts = [];
  if (isNum(est.gibPerDay)) parts.push(t('{vol}/day', { vol: fmtGib(est.gibPerDay) }));
  if (est.priced && isNum(est.monthlyCost)) {
    parts.push(t('~{cost}/mo', { cost: fmt.currency(est.monthlyCost, est.currency || 'USD', { precise: true }) }));
  }
  return parts.length ? parts.join(' \u00b7 ') : null;
}

/* ------------------------------------------------------------------ list view */

async function renderList(ctx) {
  const admin = isAdmin();
  const actionsSlot = h('div', { class: 'row wrap' });
  const header = pageHeader({
    title: t('Telemetry Extensions'),
    desc: t('Dynamically discovers Cloud Logging streams and BigQuery tables, groups related telemetry with Gemini into ready-made dashboards, and routes logs to BigQuery with automatic volume and cost controls.'),
    actions: actionsSlot,
  });
  const body = h('div', { class: 'stack-lg' });
  mount(ctx.root, header, body);

  const env = {
    admin,
    days: 7,
    maxLogEntries: 1000,
    logs: [],
    groups: [],
    listed: false,
    aiGrouping: null,
    routing: null,
  };

  const newRouteBtn = button({
    label: t('New BigQuery route'),
    icon: 'database',
    size: 'sm',
    variant: 'primary',
    onClick: () => {
      if (!requireAdmin(t('Configure BigQuery log routing'))) return;
      openRouteModal(ctx, { env });
    },
  });
  const refreshAiBtn = admin ? button({
    label: t('Re-analyze with Gemini'),
    icon: 'spark',
    size: 'sm',
    onClick: async () => {
      const ok = await confirmDialog({
        title: t('Re-analyze telemetry sources'),
        message: t('Clears cached Gemini source descriptions and groups so discovery profiles active logs and tables fresh.'),
        confirmLabel: t('Re-analyze'),
      });
      if (!ok) return;
      setBusy(refreshAiBtn, true, t('Analyzing'));
      try {
        await api.post('/api/v1/telemetry/profiles/refresh', {});
        toast({ title: t('Gemini profiles refreshed'), tone: 'success' });
        ctx.rerender();
      } catch (err) {
        setBusy(refreshAiBtn, false);
        toast({ title: t('Refresh failed'), text: errorText(err), tone: 'danger' });
      }
    },
  }) : null;
  const policyBtn = button({
    label: t('Cost & collection settings'),
    icon: 'sliders',
    size: 'sm',
    variant: 'ghost',
    onClick: () => ctx.navigate('#/settings/data-collection'),
  });
  mount(actionsSlot, newRouteBtn, refreshAiBtn, policyBtn);

  const connectorsWarnSlot = h('div');
  const connectorsSlot = card({ title: t('My dashboards'), body: skeleton('block', 96) });
  const routingPlaceholder = card({ title: t('BigQuery Log Router & cost controls'), body: skeleton('lines') });
  const pending = card({ title: t('Data you can add'), body: skeleton('block') });
  mount(body, connectorsWarnSlot, howItWorks(), connectorsSlot, routingPlaceholder, pending);

  let latestRoutingNode = routingPlaceholder;
  let discoveryDone = false;

  const connectorsTask = api.get('/api/v1/telemetry/connectors', null, { signal: ctx.signal })
    .then((cRes) => {
      if (ctx.signal.aborted) return;
      const warnNode = warningsNotice(cRes.meta.warnings || []);
      if (warnNode) mount(connectorsWarnSlot, warnNode);
      connectorsSlot.replaceWith(dashboardsSection(ctx, cRes.data.connectors || []));
    })
    .catch((err) => {
      if (isAbort(err) || ctx.signal.aborted) return;
      connectorsSlot.replaceWith(card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    });

  const routingTask = api.get('/api/v1/telemetry/routing', null, { signal: ctx.signal })
    .then((rRes) => {
      if (ctx.signal.aborted) return;
      ctx.routing = rRes.data;
      env.routing = rRes.data;
      const nextNode = routingSection(ctx, rRes.data, env);
      latestRoutingNode.replaceWith(nextNode);
      latestRoutingNode = nextNode;
    })
    .catch((err) => {
      if (isAbort(err) || ctx.signal.aborted) return;
      const errNode = banner({ tone: 'warning', title: t('BigQuery routing status could not be loaded'), text: errorText(err) });
      latestRoutingNode.replaceWith(errNode);
      latestRoutingNode = errNode;
    });

  const discoveryTask = api.get('/api/v1/telemetry/discover', ctx.refresh ? { refresh: 1 } : null, { signal: ctx.signal })
    .then((dRes) => {
      if (ctx.signal.aborted) return;
      discoveryDone = true;
      const discovery = dRes.data;
      ctx.discovery = discovery;
      env.days = (discovery && discovery.activityDays) || 7;
      env.maxLogEntries = (discovery && discovery.maxLogEntries) || 1000;
      env.logs = (discovery && Array.isArray(discovery.logs)) ? discovery.logs : [];
      env.groups = (discovery && Array.isArray(discovery.groups)) ? discovery.groups : [];
      env.listed = Boolean(discovery && (Array.isArray(discovery.tables) || Array.isArray(discovery.logs)));
      env.aiGrouping = (discovery && discovery.aiGrouping) || null;

      if (env.routing && latestRoutingNode.isConnected) {
        const refreshedRouting = routingSection(ctx, env.routing, env);
        latestRoutingNode.replaceWith(refreshedRouting);
        latestRoutingNode = refreshedRouting;
      }

      const sources = buildSources(discovery);
      pending.replaceWith(...[
        warningsNotice((dRes.meta && dRes.meta.warnings) || []),
        sourcesSection(ctx, sources, env),
        builtinSection(sources),
        hiddenSourcesSection(ctx, sources, env),
        notListedCard(ctx, env),
      ].filter(Boolean));
    })
    .catch((err) => {
      if (isAbort(err) || ctx.signal.aborted) return;
      discoveryDone = true;
      const sources = buildSources(null);
      pending.replaceWith(...[
        banner({ tone: 'warning', title: t('The sources of this project could not be listed'), text: errorText(err) }),
        sourcesSection(ctx, sources, env),
        notListedCard(ctx, env),
      ].filter(Boolean));
    });

  await Promise.all([connectorsTask, routingTask, discoveryTask]);
  void discoveryDone;
  return null;
}

function buildSources(discovery) {
  const tables = (discovery && Array.isArray(discovery.tables)) ? discovery.tables : [];
  const logs = (discovery && Array.isArray(discovery.logs)) ? discovery.logs : [];
  const tableIds = new Set(tables.map((tb) => tb.id));
  const logsById = new Map(logs.map((lg) => [lg.id, lg]));
  const out = tables.map((tb) => {
    const log = logsById.get(tb.id) || {};
    return {
      key: tb.key || `bq:${tb.project}.${tb.dataset}.${tb.logical || tb.table}`,
      kind: 'bigquery', id: tb.id, project: tb.project || null, card: tb.catalog || null, quick: tb.quickAdd || null,
      connectorId: tb.connectorId || log.connectorId || null, core: Boolean(tb.core),
      routedDataset: Boolean(tb.routedDataset), routedBy: tb.routedBy || log.routedBy || null,
      tech: `${tb.project}.${tb.dataset}.${tb.table}`, rows: tb.rows, sizeBytes: tb.sizeBytes, lastModified: tb.lastModified,
      entries7d: tb.entries7d, daily: tb.daily, logName: log.logName || null,
      volume: tb.volume || null, estimate: tb.estimate || null,
    };
  });
  logs.forEach((lg) => {
    if (lg.routedToBigQuery && tableIds.has(lg.id)) return;
    out.push({
      key: lg.key || `log:${lg.project || ''}:${lg.shortName || lg.id}`,
      kind: 'logging', id: lg.id, project: lg.project || null, card: lg.catalog || null, quick: lg.quickAdd || null,
      connectorId: lg.connectorId || null, core: Boolean(lg.core),
      routedToBigQuery: Boolean(lg.routedToBigQuery), routedBy: lg.routedBy || null,
      tech: lg.shortName, rows: null, sizeBytes: null, lastModified: null, entries7d: lg.entries7d, daily: lg.daily,
      logName: lg.logName, sinkFilter: lg.sinkFilter || null,
      volume: lg.volume || null, estimate: lg.estimate || null,
    });
  });
  const titles = new Map();
  out.forEach((s) => {
    if (s.card && s.card.hidden) return;
    const k = cardTitle(s.card);
    titles.set(k, (titles.get(k) || 0) + 1);
  });
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
    ['search', t('1. Discovered & grouped dynamically'), t('Active Cloud Logging streams and BigQuery tables are inspected from live schemas and grouped by Gemini — nothing is hardcoded.')],
    ['layers', t('2. Bundled into dashboards'), t('Add a single source or bundle a whole log group into one dashboard with automatic field mapping.')],
    ['database', t('3. Cost-controlled BigQuery routing'), t('Export logs via Log Router with sampling, severity thresholds, retention days and daily GiB caps that auto-pause sinks.')],
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
    connectors.length ? grid : h('div', { class: 'src-empty' }, t('No dashboards yet. Choose a source or group below to create a dashboard with one click.')));
}

/* ------------------------------------------------------------------ BigQuery routing & cost controls section */

function routeStatusBadge(r) {
  if (r.status === 'paused_budget') return badge(t('Paused (budget)'), 'danger', { size: 'sm', dot: true });
  if (r.status === 'paused') return badge(t('Paused'), 'warning', { size: 'sm', dot: true });
  if (r.live && r.live.exists === false) return badge(t('Sink missing'), 'warning', { size: 'sm' });
  return badge(t('Active'), 'success', { size: 'sm', dot: true });
}

function policySummaryChips(pol) {
  if (!pol) return null;
  const chips = [];
  const sample = isNum(pol.samplePercent) ? pol.samplePercent : 100;
  chips.push(chip(sample < 100 ? t('{p}% sample', { p: fmt.num(sample, 2) }) : t('100% (all entries)')));
  if (pol.minSeverity) chips.push(chip(t('>= {sev}', { sev: pol.minSeverity })));
  if (Array.isArray(pol.exclusions) && pol.exclusions.length) {
    chips.push(chip(t('{n} exclusions', { n: pol.exclusions.length })));
  }
  const ret = isNum(pol.retentionDays) ? pol.retentionDays : 0;
  chips.push(chip(ret > 0 ? t('{d}d retention', { d: fmt.int(ret) }) : t('Unlimited retention')));
  return h('div', { class: 'chips' }, chips);
}

function routingSection(ctx, routing, env) {
  if (!routing) return null;
  const pol = routing.policy || {};
  const totals = routing.totals || {};
  const ds = routing.dataset || {};
  const routes = Array.isArray(routing.routes) ? routing.routes : [];
  const events = Array.isArray(routing.events) ? routing.events : [];
  const prices = routing.prices || null;
  const lastEnf = routing.lastEnforcement || null;

  const dailyBudget = isNum(totals.dailyBudgetGb) ? totals.dailyBudgetGb : (isNum(pol.dailyBudgetGb) ? pol.dailyBudgetGb : 0);
  const gbToday = isNum(totals.gbToday) ? totals.gbToday : 0;
  const pausedCount = routes.filter((r) => r.status !== 'active').length;

  const enforceBtn = env.admin ? button({
    label: t('Check budgets now'),
    icon: 'shield-check',
    size: 'sm',
    onClick: async () => {
      setBusy(enforceBtn, true, t('Checking'));
      try {
        const res = await api.post('/api/v1/telemetry/routing/enforce', { follow: true });
        const sum = (res.data && res.data.summary) || {};
        if (ctx.onBootChange && res.data && res.data.routing) {
          const rlist = res.data.routing.routes || [];
          ctx.onBootChange({
            telemetryRouting: {
              routes: rlist.length,
              paused: rlist.filter((x) => x.status !== 'active').length,
              lastEnforcement: res.data.routing.lastEnforcement,
            },
          });
        }
        toast({
          title: t('Routing budgets verified'),
          text: t('{gb} routed today \u00b7 {paused} paused \u00b7 {resumed} resumed', {
            gb: fmtGib(sum.gbToday || 0),
            paused: fmt.int(sum.paused || 0),
            resumed: fmt.int(sum.resumed || 0),
          }),
          tone: (sum.paused || 0) > 0 ? 'warning' : 'success',
        });
        ctx.rerender();
      } catch (err) {
        setBusy(enforceBtn, false);
        toast({ title: t('Budget check failed'), text: errorText(err), tone: 'danger' });
      }
    },
  }) : null;

  const createBtn = button({
    label: t('Add route'),
    icon: 'plus',
    size: 'sm',
    variant: 'primary',
    onClick: () => {
      if (!requireAdmin(t('Add BigQuery route'))) return;
      openRouteModal(ctx, { env });
    },
  });

  const editDefaultsBtn = button({
    label: t('Default policy'),
    icon: 'sliders',
    size: 'sm',
    variant: 'ghost',
    onClick: () => ctx.navigate('#/settings/data-collection'),
  });

  const statsRow = h('div', { class: 'stat-row' },
    stat(
      t('Managed routes'),
      `${fmt.int(routes.length)} / ${fmt.int(totals.maxSinks || pol.maxSinks || 20)}`,
      pausedCount > 0 ? t('{n} paused', { n: pausedCount }) : t('Destination: {ds}', { ds: `${ds.project || ''}.${ds.dataset || pol.dataset || 'telemetry_routed'}` }),
    ),
    stat(
      t('Routed volume today'),
      dailyBudget > 0 ? `${fmtGib(gbToday)} / ${fmtGib(dailyBudget)}` : fmtGib(gbToday),
      dailyBudget > 0
        ? (pol.overBudgetAction === 'pause'
          ? t('Auto-pauses sink over budget{resume}', { resume: pol.autoResume ? t(' \u00b7 resumes next day') : '' })
          : t('Alerts when daily budget is exceeded'))
        : t('No daily total cap set'),
    ),
    stat(
      t('Default filter & retention'),
      `${fmt.num(pol.samplePercent ?? 100, 2)}% \u00b7 ${pol.retentionDays ? `${fmt.int(pol.retentionDays)}d` : t('Unlimited')}`,
      pol.minSeverity ? t('Minimum severity: {sev}', { sev: pol.minSeverity }) : t('All severities included'),
    ),
    stat(
      t('BigQuery list rates'),
      prices && isNum(prices.ingestPerGiB)
        ? `${fmt.currency(prices.ingestPerGiB, prices.currency || 'USD', { precise: true })} / GiB`
        : t('Sync prices to estimate'),
      prices && isNum(prices.storagePerGiBMonth)
        ? t('Storage: {rate} / GiB-mo', { rate: fmt.currency(prices.storagePerGiBMonth, prices.currency || 'USD', { precise: true }) })
        : (lastEnf && lastEnf.at ? t('Last guard check {when}', { when: fmt.relative(lastEnf.at) }) : t('Checked automatically every 15m')),
    ));

  const budgetBar = dailyBudget > 0
    ? h('div', { class: 'rt-budget-bar' },
      progress(Math.min(1, gbToday / dailyBudget)),
      h('span', { class: 'muted tabular' }, `${fmt.pct(gbToday / dailyBudget)} ${t('of daily total budget')}`))
    : null;

  const runAction = async (route, action, confirmOpts) => {
    if (!requireAdmin(t('Manage BigQuery route'))) return;
    if (confirmOpts) {
      const ok = await confirmDialog(confirmOpts);
      if (!ok) return;
    }
    try {
      const res = await api.post('/api/v1/telemetry/routing/routes/action', { id: route.id, action });
      if (ctx.onBootChange && res.data && res.data.routing) {
        const rlist = res.data.routing.routes || [];
        ctx.onBootChange({
          telemetryRouting: {
            routes: rlist.length,
            paused: rlist.filter((x) => x.status !== 'active').length,
            lastEnforcement: res.data.routing.lastEnforcement,
          },
        });
      }
      toast({ title: t('Route updated'), text: route.title || route.sink, tone: 'success' });
      ctx.rerender();
    } catch (err) {
      toast({ title: t('Action failed'), text: errorText(err), tone: 'danger' });
    }
  };

  const routeTable = routes.length ? dataTable({
    columns: [
      {
        key: 'title', label: t('Route & sink'),
        render: (r) => h('div', { class: 'cell-stack' },
          h('div', { class: 'row wrap' },
            h('span', { class: 'cell-main' }, r.title || r.sink),
            r.project ? badge(r.project, 'info', { size: 'sm', dot: true }) : null,
            r.group ? badge(getLocale() === 'ko' && r.groupKo ? r.groupKo : r.group, 'neutral', { size: 'sm' }) : null,
            r.followGroup ? badge(t('Follows group'), 'accent', { size: 'sm' }) : null),
          h('span', { class: 'cell-sub cell-mono truncate', title: `${r.sink} \u00b7 ${(r.logs || []).length} logs` },
            `${r.sink} \u00b7 ${(r.logs && r.logs.length) ? t('{n} logs', { n: r.logs.length }) : t('Custom filter')}`)),
      },
      {
        key: 'status', label: t('Status'),
        render: (r) => h('div', { class: 'cell-stack' },
          routeStatusBadge(r),
          r.pausedReason ? h('span', { class: 'cell-sub' }, serverText(r.pausedReason)) : null),
      },
      {
        key: 'policy', label: t('Cost controls'), sortable: false,
        render: (r) => policySummaryChips(r.effectivePolicy || r.policy),
      },
      {
        key: 'gbToday', label: t('Today / Cap'), align: 'num',
        value: (r) => r.gbToday || 0,
        render: (r) => {
          const cap = r.effectivePolicy && isNum(r.effectivePolicy.dailyGb) ? r.effectivePolicy.dailyGb : 0;
          return h('div', { class: 'cell-stack' },
            h('span', { class: ['tabular', r.overBudget ? 'text-danger fw-600' : ''] },
              cap > 0 ? `${fmtGib(r.gbToday || 0)} / ${fmtGib(cap)}` : fmtGib(r.gbToday || 0)),
            cap > 0 ? progress(Math.min(1, (r.gbToday || 0) / cap)) : null);
        },
      },
      {
        key: 'estimate', label: t('Est. monthly cost'), align: 'num',
        value: (r) => (r.estimate && isNum(r.estimate.monthlyCost) ? r.estimate.monthlyCost : -1),
        render: (r) => {
          const est = r.estimate || {};
          if (est.priced && isNum(est.monthlyCost)) {
            return h('div', { class: 'cell-stack' },
              h('span', { class: 'tabular fw-600' }, fmt.currency(est.monthlyCost, est.currency || 'USD', { precise: true })),
              isNum(est.gibPerDay) ? h('span', { class: 'cell-sub tabular' }, t('{vol}/day', { vol: fmtGib(est.gibPerDay) })) : null);
          }
          return isNum(est.gibPerDay) ? h('span', { class: 'muted tabular' }, t('{vol}/day', { vol: fmtGib(est.gibPerDay) })) : DASH;
        },
      },
      {
        key: 'actions', label: '', sortable: false,
        render: (r) => h('div', { class: 'row wrap' },
          r.status === 'active'
            ? button({ label: t('Pause'), size: 'xs', variant: 'ghost', onClick: () => runAction(r, 'pause') })
            : button({ label: t('Resume'), size: 'xs', variant: 'primary', onClick: () => runAction(r, 'resume') }),
          button({ label: t('Edit'), size: 'xs', variant: 'ghost', onClick: () => { if (requireAdmin(t('Edit BigQuery route'))) openRouteModal(ctx, { route: r, env }); } }),
          button({
            label: t('Delete'), size: 'xs', variant: 'ghost',
            onClick: () => runAction(r, 'delete', {
              title: t('Delete BigQuery route'),
              message: t('Deletes Log Router sink {sink} in project {project}. Existing tables in BigQuery are kept.', { sink: r.sink, project: r.project }),
              confirmLabel: t('Delete route'),
              tone: 'danger',
            }),
          })),
      },
    ],
    rows: routes,
    searchable: routes.length > 6,
    pageSize: 8,
    initialSort: { key: 'gbToday', dir: 'desc' },
  }) : emptyState({
    icon: 'database',
    title: t('No managed BigQuery log routes yet'),
    body: t('Route individual logs or entire Gemini log groups to BigQuery with sampling, severity filtering, partition expiration and daily GiB caps so storage and ingestion costs stay bounded.'),
    compact: true,
  });

  const eventsDetails = events.length ? h('details', { class: 'src-more' },
    h('summary', null, t('Recent routing & budget events ({n})', { n: events.length })),
    dataTable({
      columns: [
        { key: 'at', label: t('Time'), render: (ev) => fmt.dateTime(ev.at, true) },
        { key: 'kind', label: t('Event'), render: (ev) => badge(t(ev.kind || 'event'), ev.kind === 'over_budget' || ev.kind === 'paused' || ev.kind === 'error' ? 'warning' : 'neutral', { size: 'sm' }) },
        { key: 'title', label: t('Route'), render: (ev) => h('span', { class: 'cell-mono' }, ev.title || ev.sink || ev.route || DASH) },
        { key: 'detail', label: t('Detail'), render: (ev) => serverText(ev.detail || '') || (isNum(ev.bytes) ? fmt.bytes(ev.bytes) : DASH) },
      ],
      rows: events.slice(0, 20),
      searchable: false,
      pageSize: 5,
    })) : null;

  return card({
    title: t('BigQuery Log Router & cost controls'),
    subtitle: t('Automatic Cloud Logging -> BigQuery sinks with sampling, severity filters, partition retention and daily GiB caps.'),
    actions: h('div', { class: 'row wrap' }, enforceBtn, editDefaultsBtn, createBtn),
    body: h('div', { class: 'stack' }, statsRow, budgetBar, routeTable, eventsDetails),
  });
}

/* ------------------------------------------------------------------ sources & group bundling */

function groupIcon(items) {
  const counts = {};
  items.forEach((s) => { const id = categoryOf(s).id; counts[id] = (counts[id] || 0) + 1; });
  const top = CATEGORIES.slice().sort((a, b) => (counts[b.id] || 0) - (counts[a.id] || 0))[0];
  return (top && top.icon) || 'database';
}

async function bundleGroupDashboard(ctx, gInfo, btn) {
  if (!requireAdmin(t('Bundle group dashboard'))) return;
  setBusy(btn, true, t('Bundling'));
  try {
    const res = await api.post('/api/v1/telemetry/groups/dashboard', {
      name: gInfo.name,
      nameKo: gInfo.nameKo || '',
      logs: gInfo.logs,
    });
    const created = res.data && res.data.connector;
    toast({ title: t('Group dashboard ready'), text: getLocale() === 'ko' && gInfo.nameKo ? gInfo.nameKo : gInfo.name, tone: 'success' });
    if (created && created.id) ctx.navigate(`#/extensions/${encodeURIComponent(created.id)}`);
    else ctx.rerender();
  } catch (err) {
    setBusy(btn, false);
    toast({ title: t('Could not bundle group dashboard'), text: errorText(err), tone: 'danger' });
  }
}

function groupHeader(ctx, gLabel, items, env) {
  const rawGroupName = (items.find((s) => s.card && s.card.group) || { card: {} }).card.group || gLabel;
  const rawGroupKo = (items.find((s) => s.card && s.card.groupKo) || { card: {} }).card.groupKo || '';
  const matched = (env.groups || []).find((g) => g.name === rawGroupName || (g.nameKo && g.nameKo === gLabel));
  const groupLogs = matched && Array.isArray(matched.logs) && matched.logs.length
    ? matched.logs
    : [...new Set(items.map((s) => s.logName).filter(Boolean))];

  const actions = [];
  if (groupLogs.length >= 2) {
    const bundleBtn = button({
      label: t('Bundle {n} logs into dashboard', { n: groupLogs.length }),
      icon: 'layers',
      size: 'xs',
      onClick: () => bundleGroupDashboard(ctx, { name: rawGroupName, nameKo: rawGroupKo, logs: groupLogs }, bundleBtn),
    });
    actions.push(bundleBtn);
  }
  if (groupLogs.length >= 1) {
    const existingRoute = ((env.routing && env.routing.routes) || []).find((r) => r.group && r.group === rawGroupName);
    actions.push(button({
      label: existingRoute ? t('Edit group route') : t('Route group to BigQuery'),
      icon: 'database',
      size: 'xs',
      variant: existingRoute ? 'ghost' : 'secondary',
      onClick: () => {
        if (!requireAdmin(t('Route group to BigQuery'))) return;
        if (existingRoute) {
          openRouteModal(ctx, { route: existingRoute, env });
        } else {
          openRouteModal(ctx, {
            group: { name: rawGroupName, nameKo: rawGroupKo, logs: groupLogs },
            env,
          });
        }
      },
    }));
  }

  return h('div', { class: 'src-group-bar' },
    h('h3', { class: 'src-group-title' }, icon(groupIcon(items), { size: 14 }), gLabel, h('span', { class: 'muted' }, String(items.length))),
    actions.length ? h('div', { class: 'row wrap' }, actions) : null);
}

function sourcesSection(ctx, sources, env) {
  const addable = sources.filter((s) => !s.core && !(s.card && s.card.hidden));
  const st = { cat: 'all', q: '' };
  const counts = {};
  addable.forEach((s) => { const id = categoryOf(s).id; counts[id] = (counts[id] || 0) + 1; });
  const chipsHost = h('div', { class: 'chips', role: 'group', 'aria-label': t('Filter by category') });
  const listHost = h('div', { class: 'stack-lg' });
  const matches = (s) => !st.q || [sourceTitle(s), cardWhat(s.card), cardGroup(s.card), s.tech].some((x) => String(x || '').toLowerCase().includes(st.q));
  const draw = () => {
    mount(chipsHost, [{ id: 'all', label: 'All' }, ...CATEGORIES].filter((c) => c.id === 'all' || counts[c.id]).map((c) => chip(
      `${t(c.label)} ${c.id === 'all' ? addable.length : counts[c.id]}`,
      { pressed: st.cat === c.id, onClick: () => { st.cat = c.id; draw(); } })));
    const visible = addable.filter((s) => (st.cat === 'all' || categoryOf(s).id === st.cat) && matches(s));
    const active = visible.filter((s) => !isStale(s, env.days));
    const stale = visible.filter((s) => isStale(s, env.days)).sort(byActivity);
    const nodes = [];
    const hasAiGroups = active.some((s) => Boolean(cardGroup(s.card)));
    if (hasAiGroups) {
      const buckets = new Map();
      active.forEach((s) => {
        const gLabel = cardGroup(s.card) || t(categoryOf(s).label);
        if (!buckets.has(gLabel)) buckets.set(gLabel, []);
        buckets.get(gLabel).push(s);
      });
      const ordered = [...buckets.entries()].sort((a, b) => {
        const scoreA = a[1].reduce((acc, s) => acc + Math.max(0, activityScore(s)), 0);
        const scoreB = b[1].reduce((acc, s) => acc + Math.max(0, activityScore(s)), 0);
        return scoreB - scoreA || a[0].localeCompare(b[0]);
      });
      ordered.forEach(([gLabel, items]) => {
        items.sort(byActivity);
        nodes.push(h('div', { class: 'stack' },
          groupHeader(ctx, gLabel, items, env),
          h('div', { class: 'src-grid' }, items.map((s) => sourceCard(ctx, s, env)))));
      });
    } else {
      CATEGORIES.forEach((c) => {
        const items = active.filter((s) => categoryOf(s).id === c.id).sort(byActivity);
        if (!items.length) return;
        nodes.push(h('div', { class: 'stack' },
          h('h3', { class: 'src-group-title' }, icon(c.icon, { size: 14 }), t(c.label), h('span', { class: 'muted' }, String(items.length))),
          h('div', { class: 'src-grid' }, items.map((s) => sourceCard(ctx, s, env)))));
      });
    }
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
  const aiInfo = env.aiGrouping || null;
  const aiModel = aiInfo && aiInfo.enabled && aiInfo.model ? aiInfo.model : null;
  return h('section', { class: 'stack' },
    h('div', { class: 'src-toolbar' },
      h('div', { class: 'stack-sm' },
        h('div', { class: 'row wrap' },
          h('h2', { class: 'section-title tx-no-margin' }, t('Data you can add'), h('span', { class: 'muted' }, String(addable.length))),
          aiModel ? badge(t('Gemini grouped ({model})', { model: aiModel }), 'info', { size: 'sm', dot: true }) : null,
          aiInfo && isNum(aiInfo.profiled) && aiInfo.profiled > 0 ? badge(t('{n} sources profiled', { n: aiInfo.profiled }), 'neutral', { size: 'sm' }) : null),
        h('p', { class: 'src-what' }, t('Discovered from live Cloud Logging and BigQuery schemas. Bundle a group into one dashboard or route high-value logs to BigQuery.'))),
      addable.length ? search : null),
    addable.length ? chipsHost : null,
    listHost);
}

function dashboardShows(s) {
  const m = (s.quick && s.quick.mapping) || {};
  const c = s.card || {};
  const out = [t('Events per day')];
  if (m.operation) out.push(t('By {label}', { label: cardFieldLabel(c, 'operation', 'Operation') }));
  if (m.status) out.push(t('By {label}', { label: cardFieldLabel(c, 'status', 'Status') }));
  if (m.user) out.push(t('Top users'));
  if (m.model) out.push(t('Top models'));
  if (m.tokens) out.push(cardFieldLabel(c, 'tokens', 'Tokens'));
  out.push(t('Latest entries'));
  return out;
}

function activityLine(s, days) {
  const spark = Array.isArray(s.daily) && s.daily.some((v) => v > 0) ? sparkline(s.daily, TONES.violet) : null;
  const parts = [];
  if (s.kind === 'logging') {
    parts.push(isNum(s.entries7d) ? t('{n} entries in the last {d} days', { n: fmt.int(s.entries7d), d: days }) : t('Recent volume unavailable'));
    const estText = fmtEstimate(s.estimate);
    if (estText) parts.push(estText);
  } else {
    if (isNum(s.rows)) parts.push(t('{n} rows in BigQuery', { n: fmt.int(s.rows) }));
    if (isNum(s.sizeBytes) && s.sizeBytes > 0) parts.push(fmt.bytes(s.sizeBytes));
    if (s.lastModified) parts.push(t('updated {time}', { time: fmt.relative(s.lastModified) }));
    if (!parts.length) parts.push(t('Table statistics unavailable'));
  }
  return h('div', { class: 'src-activity' },
    spark ? h('span', { class: 'src-spark', title: t('Entries written per day, last {d} days', { d: days }) }, spark) : null,
    h('span', null, parts.join(' \u00b7 ')));
}

function volumeNote(ctx, s, env) {
  const maxEntries = env.maxLogEntries || 1000;
  const c = s.card || {};
  const overReadCap = s.kind === 'logging' && isNum(s.entries7d) && (s.entries7d / env.days) * rangeDays() > maxEntries;
  if (!overReadCap && !(s.kind === 'logging' && c.routeRecommended && !s.routedBy)) return null;
  const reason = cardRouteReason(c);
  const text = overReadCap
    ? t('High volume: live Logging dashboards read at most {n} entries per view. Route to BigQuery with cost controls for complete history.', { n: fmt.int(maxEntries) })
    : (reason || t('Gemini recommends routing this log to BigQuery for retention and SQL analytics.'));
  return h('div', { class: 'src-note' }, icon('info', { size: 13 }),
    h('span', null, text),
    (s.logName || s.sinkFilter) && !s.routedBy
      ? button({
        label: t('Route to BigQuery'),
        size: 'xs',
        variant: 'ghost',
        icon: 'database',
        onClick: () => { if (requireAdmin(t('Route to BigQuery'))) openRouteModal(ctx, { source: s, env }); },
      })
      : null);
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
  if (s.kind === 'logging' && (s.logName || s.sinkFilter)) {
    const existingRoute = s.routedBy && env && env.routing && Array.isArray(env.routing.routes)
      ? env.routing.routes.find((r) => r.id === s.routedBy)
      : null;
    btns.push(button({
      label: existingRoute ? t('Edit route') : t('Route to BigQuery'),
      icon: 'database',
      size: 'sm',
      variant: 'ghost',
      onClick: () => {
        if (!requireAdmin(t('Route to BigQuery'))) return;
        openRouteModal(ctx, existingRoute ? { route: existingRoute, env } : { source: s, env });
      },
    }));
  }
  if (env && env.admin && s.key) {
    btns.push(button({
      label: t('Customize'),
      icon: 'edit',
      size: 'sm',
      variant: 'ghost',
      onClick: () => openOverrideModal(ctx, s),
    }));
  }
  return h('div', { class: 'row wrap' }, btns);
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
          c.aiProfiled ? badge(t('Gemini profiled'), 'info', { size: 'sm', title: c.aiModel || '' }) : null,
          c.routeRecommended && !s.routedBy && s.kind === 'logging' ? badge(t('BigQuery recommended'), 'accent', { size: 'sm' }) : null,
          s.routedBy || s.routedDataset ? badge(t('Routed to BigQuery'), 'success', { size: 'sm', dot: true }) : null,
          c.overridden ? badge(t('Customized'), 'neutral', { size: 'sm' }) : null,
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

function hiddenSourcesSection(ctx, sources, env) {
  if (!env.admin) return null;
  const hidden = sources.filter((s) => !s.core && s.card && s.card.hidden);
  if (!hidden.length) return null;
  return h('details', { class: 'src-more' },
    h('summary', null, t('Hidden sources ({n})', { n: hidden.length })),
    h('div', { class: 'src-grid' }, hidden.map((s) => sourceCard(ctx, s, env))));
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
  const maxEntries = env.maxLogEntries || 1000;
  return card({
    title: t('Looking for something that is not listed?'),
    body: h('div', { class: 'stack-sm' },
      h('p', { class: 'src-what' }, t('New logs appear automatically after a service writes to Cloud Logging. Adjust discovery windows, field hints and Gemini profiling under Settings > Data collection and cost.')),
      h('p', { class: 'src-what' }, t('Live Cloud Logging dashboards cover Cloud Logging retention and read up to {n} entries per view (configurable in Settings). For full history and SQL aggregation without runaway spend, create a cost-controlled BigQuery route.', { n: fmt.int(maxEntries) })),
      h('div', { class: 'row wrap' },
        button({ label: t('Custom connector'), icon: 'edit', onClick: () => { if (requireAdmin(t('Custom connector'))) openConnectorForm(ctx, null, null); } }),
        button({ label: t('New BigQuery route'), icon: 'database', onClick: () => { if (requireAdmin(t('New BigQuery route'))) openRouteModal(ctx, { env }); } }),
        button({ label: t('Data collection & cost settings'), icon: 'sliders', variant: 'ghost', onClick: () => ctx.navigate('#/settings/data-collection') }))),
  });
}

/* ------------------------------------------------------------------ administrator source override modal */

function openOverrideModal(ctx, s) {
  const c = s.card || {};
  const raw = c.override || {};
  const st = {
    title: raw.title !== undefined ? raw.title : '',
    titleKo: raw.titleKo !== undefined ? raw.titleKo : (c.titleKo || ''),
    what: raw.what !== undefined ? raw.what : '',
    whatKo: raw.whatKo !== undefined ? raw.whatKo : (c.whatKo || ''),
    category: raw.category || c.category || 'other',
    group: raw.group !== undefined ? raw.group : (c.group || ''),
    groupKo: raw.groupKo !== undefined ? raw.groupKo : (c.groupKo || ''),
    hidden: Boolean(raw.hidden !== undefined ? raw.hidden : c.hidden),
  };
  const errEl = h('div', { class: 'field-error', role: 'alert' });
  const saveBtn = button({ label: t('Save customization'), variant: 'primary', icon: 'check' });
  const resetBtn = c.overridden ? button({ label: t('Reset to automatic'), variant: 'ghost', icon: 'refresh' }) : null;

  const whatEn = h('textarea', { class: 'textarea', rows: 2, placeholder: cardWhat(c), maxlength: 400 });
  whatEn.value = st.what;
  whatEn.addEventListener('input', () => { st.what = whatEn.value; });

  const whatKo = h('textarea', { class: 'textarea', rows: 2, placeholder: c.whatKo || '', maxlength: 400 });
  whatKo.value = st.whatKo;
  whatKo.addEventListener('input', () => { st.whatKo = whatKo.value; });

  const m = openModal({
    title: t('Customize telemetry source'),
    subtitle: s.tech,
    wide: true,
    body: (el) => append(el, [
      h('div', { class: 'field-row' },
        field({ label: t('Title (English)'), help: t('Leave empty to keep the automatic title.'), control: textInput({ value: st.title, placeholder: sourceTitle(s), onInput: (v) => { st.title = v; } }) }),
        field({ label: t('Title (Korean)'), control: textInput({ value: st.titleKo, placeholder: c.titleKo || '', onInput: (v) => { st.titleKo = v; } }) })),
      h('div', { class: 'field-row' },
        field({ label: t('Description (English)'), control: whatEn }),
        field({ label: t('Description (Korean)'), control: whatKo })),
      h('div', { class: 'field-row' },
        field({
          label: t('Category'),
          control: selectInput(CATEGORIES.map((cat) => ({ value: cat.id, label: t(cat.label) })), st.category, (v) => { st.category = v; }),
        }),
        field({ label: t('Group (English)'), help: t('Sources with the same group can be bundled and routed together.'), control: textInput({ value: st.group, onInput: (v) => { st.group = v; } }) }),
        field({ label: t('Group (Korean)'), control: textInput({ value: st.groupKo, onInput: (v) => { st.groupKo = v; } }) })),
      h('label', { class: 'row' },
        switchInput({ checked: st.hidden, label: t('Hide this source from the main list'), onChange: (v) => { st.hidden = v; } }),
        h('span', null, t('Hide this source from the main list'))),
      errEl,
    ]),
    footer: (el) => append(el, [
      resetBtn,
      h('span', { class: 'spacer' }),
      button({ label: t('Cancel'), onClick: () => m.close() }),
      saveBtn,
    ].filter(Boolean)),
  });

  if (resetBtn) {
    resetBtn.addEventListener('click', async () => {
      setBusy(resetBtn, true, t('Resetting'));
      try {
        await api.post('/api/v1/telemetry/sources/override', { key: s.key, reset: true });
        toast({ title: t('Customization reset'), tone: 'success' });
        m.close();
        ctx.rerender();
      } catch (err) {
        setBusy(resetBtn, false);
        errEl.textContent = errorText(err);
      }
    });
  }

  saveBtn.addEventListener('click', async () => {
    errEl.textContent = '';
    setBusy(saveBtn, true, t('Saving'));
    try {
      await api.post('/api/v1/telemetry/sources/override', {
        key: s.key,
        title: st.title.trim(),
        titleKo: st.titleKo.trim(),
        what: st.what.trim(),
        whatKo: st.whatKo.trim(),
        category: st.category,
        group: st.group.trim(),
        groupKo: st.groupKo.trim(),
        hidden: st.hidden,
      });
      toast({ title: t('Source customization saved'), tone: 'success' });
      m.close();
      ctx.rerender();
    } catch (err) {
      setBusy(saveBtn, false);
      errEl.textContent = errorText(err);
    }
  });
}

/* ------------------------------------------------------------------ Cost-controlled BigQuery Route Modal */

function projectOfLog(logName) {
  const m = /^projects\/([^/]+)\/logs\//.exec(logName || '');
  return m ? m[1] : '';
}

function openRouteModal(ctx, { source = null, group = null, route = null, env = null } = {}) {
  const ws = (store.boot && store.boot.workspace) || {};
  const defaultProj = ws.projectId || '';
  const routing = (env && env.routing) || ctx.routing || {};
  const defPol = routing.policy || {};
  const allLogs = (env && env.logs) || ((ctx.discovery && ctx.discovery.logs) || []);

  // Determine initial project and logs
  let initProject = (route && route.project) || (source && source.project) || defaultProj;
  let initLogs = [];
  if (route && Array.isArray(route.logs)) {
    initLogs = [...route.logs];
  } else if (group && Array.isArray(group.logs)) {
    const byProj = new Map();
    group.logs.forEach((ln) => {
      const p = projectOfLog(ln) || defaultProj;
      if (!byProj.has(p)) byProj.set(p, []);
      byProj.get(p).push(ln);
    });
    initProject = byProj.has(defaultProj) ? defaultProj : ([...byProj.keys()][0] || defaultProj);
    initLogs = byProj.get(initProject) || [];
  } else if (source && source.logName) {
    initLogs = [source.logName];
  } else {
    const first = allLogs.find((l) => l.logName && (!initProject || l.project === initProject)) || allLogs.find((l) => l.logName);
    if (first) {
      initProject = first.project || initProject;
      initLogs = [first.logName];
    }
  }

  const routePol = (route && route.policy) || {};
  const st = {
    id: route ? route.id : undefined,
    project: initProject,
    title: route ? route.title : (group ? (getLocale() === 'ko' && group.nameKo ? group.nameKo : group.name) : (source ? sourceTitle(source) : '')),
    logs: initLogs,
    filter: route ? (route.filter || '') : (source && source.sinkFilter ? source.sinkFilter : ''),
    group: route ? (route.group || '') : (group ? group.name : ((source && source.card && source.card.group) || '')),
    groupKo: route ? (route.groupKo || '') : (group ? (group.nameKo || '') : ((source && source.card && source.card.groupKo) || '')),
    followGroup: route ? Boolean(route.followGroup) : Boolean(group),
    samplePercent: isNum(routePol.samplePercent) ? routePol.samplePercent : (isNum(defPol.samplePercent) ? defPol.samplePercent : 100),
    minSeverity: routePol.minSeverity !== undefined ? routePol.minSeverity : (defPol.minSeverity || ''),
    exclusionsText: Array.isArray(routePol.exclusions) ? routePol.exclusions.join('\n') : (Array.isArray(defPol.exclusions) ? defPol.exclusions.join('\n') : ''),
    dailyGb: isNum(routePol.dailyGb) ? routePol.dailyGb : (isNum(defPol.sinkDailyGb) ? defPol.sinkDailyGb : 1),
    retentionDays: isNum(routePol.retentionDays) ? routePol.retentionDays : (isNum(defPol.retentionDays) ? defPol.retentionDays : 90),
    createConnector: !route && Boolean((source && source.quick) || group),
  };

  const projects = [...new Set([initProject, defaultProj, ...(ws.projects || []), ...allLogs.map((l) => l.project)].filter(Boolean))];
  const errEl = h('div', { class: 'field-error', role: 'alert' });
  const estimateHost = h('div', { class: 'rt-estimate-box' });
  const logsHost = h('div', { class: 'stack-xs' });
  const filterPreviewHost = h('div');

  const parseExclusions = () => st.exclusionsText.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
  const policyPayload = () => ({
    samplePercent: Number(st.samplePercent),
    minSeverity: st.minSeverity,
    exclusions: parseExclusions(),
    dailyGb: Number(st.dailyGb),
    retentionDays: Number(st.retentionDays),
  });

  let estSeq = 0;
  const runEstimate = debounce(async () => {
    const seq = ++estSeq;
    if (!st.logs.length && !st.filter.trim()) {
      mount(estimateHost, h('div', { class: 'muted' }, t('Select at least one log or enter a filter to estimate volume and BigQuery cost.')));
      return;
    }
    try {
      const res = await api.post('/api/v1/telemetry/routing/estimate', {
        id: st.id,
        logs: st.logs,
        filter: st.filter.trim(),
        policy: policyPayload(),
      });
      if (seq !== estSeq) return;
      const d = res.data || {};
      const est = d.estimate || {};
      const checks = d.checks || {};
      const warnings = [];
      if (checks.overRouteBudget) warnings.push(t('Estimated daily volume exceeds this route\u2019s daily GiB cap — lower the sample rate, raise minimum severity, or increase the cap.'));
      if (checks.overTotalBudget) warnings.push(t('Combined daily volume across routes exceeds the workspace daily budget.'));
      mount(estimateHost,
        h('div', { class: 'stat-row' },
          stat(t('Est. routed volume'), isNum(est.gibPerDay) ? `${fmtGib(est.gibPerDay)} / ${t('day')}` : t('Filter-only (measured after routing)'),
            t('{p}% sample \u00b7 {ret}', { p: fmt.num(st.samplePercent, 2), ret: st.retentionDays > 0 ? t('{d}d retention', { d: fmt.int(st.retentionDays) }) : t('unlimited retention') })),
          stat(t('Est. BigQuery cost'), est.priced && isNum(est.monthlyCost) ? `${fmt.currency(est.monthlyCost, est.currency || 'USD', { precise: true })} / ${t('month')}` : t('Not priced yet'),
            est.priced ? t('30d streaming ingest + {d}d active storage', { d: fmt.int(st.retentionDays || 30) }) : t('Click Check prices now in Cost & FinOps')),
          stat(t('Daily route cap'), st.dailyGb > 0 ? `${fmtGib(st.dailyGb)} / ${t('day')}` : t('Unlimited'),
            defPol.overBudgetAction === 'pause' ? t('Auto-pauses sink when reached') : t('Alerts when reached'))),
        warnings.map((w) => banner({ tone: 'warning', title: t('Budget warning'), text: w })));
      if (d.filter) {
        mount(filterPreviewHost, field({ label: t('Generated Log Router filter'), control: codeBlock(d.filter, { wrap: true }) }));
      }
    } catch (err) {
      if (seq !== estSeq) return;
      mount(estimateHost, h('div', { class: 'muted' }, errorText(err)));
    }
  }, 240);

  const renderLogChoices = () => {
    const projLogs = allLogs.filter((l) => l.logName && (!st.project || l.project === st.project));
    if (!projLogs.length && !st.logs.length) {
      mount(logsHost, h('div', { class: 'muted' }, t('No Cloud Logging streams listed for this project yet; enter a Cloud Logging filter below.')));
      return;
    }
    const known = new Map(projLogs.map((l) => [l.logName, l]));
    st.logs.forEach((ln) => { if (!known.has(ln)) known.set(ln, { logName: ln, shortName: ln.split('/logs/')[1] || ln, catalog: null, entries7d: null }); });
    const items = [...known.values()];
    mount(logsHost,
      h('div', { class: 'wb-pick-list' }, items.map((lg) => {
        const checked = st.logs.includes(lg.logName);
        const cb = h('input', { type: 'checkbox', checked });
        cb.addEventListener('change', () => {
          if (cb.checked) {
            if (!st.logs.includes(lg.logName)) st.logs.push(lg.logName);
            if (!st.title) { st.title = cardTitle(lg.catalog) || lg.shortName || ''; titleInput.value = st.title; }
          } else {
            st.logs = st.logs.filter((x) => x !== lg.logName);
          }
          runEstimate();
        });
        const label = cardTitle(lg.catalog) || lg.shortName || lg.logName;
        return h('label', { class: 'wb-pick' },
          cb,
          h('div', { class: 'wb-pick-text' },
            h('div', { class: 'wb-pick-name' },
              h('span', null, label),
              isNum(lg.entries7d) ? badge(t('{n} in 7d', { n: fmt.compact(lg.entries7d) }), 'neutral', { size: 'sm' }) : null),
            h('div', { class: 'wb-pick-what mono' }, lg.shortName || lg.logName)));
      })));
  };

  const titleInput = textInput({ value: st.title, placeholder: t('For example Agent runtime logs'), onInput: (v) => { st.title = v; } });
  const exclInput = h('textarea', { class: 'textarea mono', rows: 2, placeholder: 'httpRequest.requestUrl:"/healthz"' });
  exclInput.value = st.exclusionsText;
  exclInput.addEventListener('input', () => { st.exclusionsText = exclInput.value; runEstimate(); });

  const extraFilter = h('textarea', { class: 'textarea mono', rows: 2, placeholder: 'jsonPayload.env="prod"' });
  extraFilter.value = st.filter;
  extraFilter.addEventListener('input', () => { st.filter = extraFilter.value; runEstimate(); });

  const saveBtn = button({ label: route ? t('Save route') : t('Create BigQuery route'), icon: 'check', variant: 'primary' });

  const modal = openModal({
    title: route ? t('Edit BigQuery log route') : t('Route logs to BigQuery with cost controls'),
    subtitle: t('Destination dataset: {ds} (partitioned tables, managed by AI Control Plane).', {
      ds: `${(routing.dataset && routing.dataset.project) || defaultProj}.${(routing.dataset && routing.dataset.dataset) || defPol.dataset || 'telemetry_routed'}`,
    }),
    wide: true,
    body: (el) => {
      renderLogChoices();
      append(el, [
        h('div', { class: 'field-row' },
          field({ label: t('Route title'), control: titleInput }),
          field({
            label: t('Source project'),
            control: selectInput(
              projects.map((p) => ({ value: p, label: p })),
              st.project,
              (v) => { st.project = v; st.logs = st.logs.filter((ln) => projectOfLog(ln) === v); renderLogChoices(); runEstimate(); },
              { disabled: Boolean(route) || null },
            ),
          })),
        field({ label: t('Logs to include (same project, up to 60)'), control: logsHost }),
        st.group ? h('label', { class: 'row' },
          switchInput({ checked: st.followGroup, label: t('Automatically include future logs in group "{group}" within budget', { group: getLocale() === 'ko' && st.groupKo ? st.groupKo : st.group }), onChange: (v) => { st.followGroup = v; } }),
          h('span', null, t('Automatically include future logs in group "{group}" within budget', { group: getLocale() === 'ko' && st.groupKo ? st.groupKo : st.group }))) : null,
        h('div', { class: 'form-subhead' }, t('Cost & volume controls')),
        h('div', { class: 'field-row' },
          field({
            label: t('Sample rate (%)'),
            help: t('Deterministic sample(insertId, rate). Lowering this cuts BigQuery ingestion & storage proportionally.'),
            control: textInput({ value: String(st.samplePercent), type: 'number', attrs: { min: 0.01, max: 100, step: 0.01 }, onInput: (v) => { st.samplePercent = v; runEstimate(); } }),
          }),
          field({
            label: t('Minimum severity'),
            help: t('Drop noisy debug or info entries before BigQuery.'),
            control: selectInput(
              [{ value: '', label: t('Any severity') }, ...SEVERITIES.map((s) => ({ value: s, label: t('{severity} and above', { severity: s }) }))],
              st.minSeverity,
              (v) => { st.minSeverity = v; runEstimate(); },
            ),
          }),
          field({
            label: t('Daily budget cap (GiB / day)'),
            help: t('0 = unlimited. Sink auto-pauses for the rest of the day when exceeded.'),
            control: textInput({ value: String(st.dailyGb), type: 'number', attrs: { min: 0, step: 'any' }, onInput: (v) => { st.dailyGb = v; runEstimate(); } }),
          }),
          field({
            label: t('Table retention (days)'),
            help: t('Partition expiration on the destination dataset. 0 = keep indefinitely.'),
            control: textInput({ value: String(st.retentionDays), type: 'number', attrs: { min: 0, max: 3650, step: 1 }, onInput: (v) => { st.retentionDays = v; runEstimate(); } }),
          })),
        field({ label: t('Exclusion filters (one per line)'), help: t('Matching entries are excluded via NOT (...) so health checks and noisy probes are never billed.'), control: exclInput }),
        estimateHost,
        !route ? h('label', { class: 'row' },
          switchInput({ checked: st.createConnector, label: t('Also create a dashboard for this route'), onChange: (v) => { st.createConnector = v; } }),
          h('span', null, t('Also create a dashboard for this route'))) : null,
        h('details', { class: 'src-more' },
          h('summary', null, t('Additional Logging filter & preview')),
          h('div', { class: 'stack' },
            field({ label: t('Additional Cloud Logging filter (optional)'), control: extraFilter }),
            filterPreviewHost)),
        errEl,
      ].filter(Boolean));
      runEstimate();
    },
    footer: (el) => append(el, [button({ label: t('Cancel'), onClick: () => modal.close() }), saveBtn]),
  });

  saveBtn.addEventListener('click', async () => {
    errEl.textContent = '';
    if (!st.logs.length && !st.filter.trim()) {
      errEl.textContent = t('Select at least one log or enter a Cloud Logging filter.');
      return;
    }
    const sample = Number(st.samplePercent);
    if (!Number.isFinite(sample) || sample <= 0 || sample > 100) {
      errEl.textContent = t('Sample rate must be between 0.01 and 100.');
      return;
    }
    const dailyGb = Number(st.dailyGb);
    if (!Number.isFinite(dailyGb) || dailyGb < 0) {
      errEl.textContent = t('Daily budget cap must be 0 or greater.');
      return;
    }
    const retentionDays = Number(st.retentionDays);
    if (!Number.isInteger(retentionDays) || retentionDays < 0 || retentionDays > 3650) {
      errEl.textContent = t('Retention days must be a whole number from 0 to 3650.');
      return;
    }
    const title = (st.title || '').trim() || (st.logs[0] ? (st.logs[0].split('/logs/')[1] || 'Telemetry route') : 'Telemetry route');
    const quick = st.createConnector && source && source.quick
      ? { ...source.quick, name: title.slice(0, 80) }
      : null;

    setBusy(saveBtn, true, t('Saving'));
    try {
      const res = await api.post('/api/v1/telemetry/routing/routes', {
        id: st.id,
        project: st.project || defaultProj,
        title,
        logs: st.logs,
        filter: st.filter.trim(),
        group: st.group || undefined,
        groupKo: st.groupKo || undefined,
        followGroup: st.group ? st.followGroup : false,
        policy: policyPayload(),
        createConnector: Boolean(st.createConnector && quick),
        quickAdd: quick,
      });
      const data = res.data || {};
      if (ctx.onBootChange && data.routing) {
        const rlist = data.routing.routes || [];
        ctx.onBootChange({
          telemetryRouting: {
            routes: rlist.length,
            paused: rlist.filter((x) => x.status !== 'active').length,
            lastEnforcement: data.routing.lastEnforcement,
          },
        });
      }
      if (!route && st.createConnector && !quick && group && st.logs.length >= 2) {
        await api.post('/api/v1/telemetry/groups/dashboard', {
          name: group.name,
          nameKo: group.nameKo || '',
          logs: st.logs,
        }).catch(() => {});
      }
      modal.close();
      toast({
        title: route ? t('BigQuery route updated') : t('BigQuery route created'),
        text: data.route ? `${data.route.sink} (${data.route.project})` : title,
        tone: 'success',
      });
      if (data.connector && data.connector.id) {
        ctx.navigate(`#/extensions/${encodeURIComponent(data.connector.id)}`);
      } else {
        ctx.rerender();
      }
    } catch (err) {
      setBusy(saveBtn, false);
      errEl.textContent = errorText(err);
    }
  });
}

/* ------------------------------------------------------------------ connector card & dashboard */

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
    cardGroup(c.catalog) ? badge(cardGroup(c.catalog), 'info', { size: 'sm' }) : null,
    c.source === 'logging' && c.bigquerySink ? badge(t('BQ sink linked'), 'success', { size: 'sm', dot: true, title: t('BigQuery linked: {dest}', { dest: c.bigquerySink }) }) : null);
  const fullRef = `${c.source === 'bigquery' ? 'BigQuery' : 'Cloud Logging'} \u00b7 ${sourceRef(c)}`;
  const bottom = h('div', { class: 'connector-bottom' },
    h('code', { class: 'src-code', title: fullRef }, fullRef),
    stats,
    sparkHost);
  const el = card({
    cls: 'connector-card',
    title: (c.catalog && c.catalog.aiProfiled && cardTitle(c.catalog)) ? cardTitle(c.catalog) : c.name,
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
  const opLabel = cardFieldLabel(cat, 'operation', 'Operation');
  const statusLabel = cardFieldLabel(cat, 'status', 'Status');
  const tokensLabel = cardFieldLabel(cat, 'tokens', 'Tokens');
  const statusKey = (cat && cat.statusLabel) || 'Status';
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
    title: (cat && cat.aiProfiled && cardTitle(cat)) ? cardTitle(cat) : (c.name || id),
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
    cardGroup(cat) ? badge(cardGroup(cat), 'info', { size: 'sm' }) : null,
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
      text: t('This log has more entries in the selected period than a dashboard reads from Cloud Logging. Counts start at {time}; earlier days have no bars. Route the log to BigQuery for complete counts.', { time: fmt.dateTime(sample.since, true) }),
      actions: (logName || (c.logging && c.logging.filter)) ? button({
        label: t('Route to BigQuery'),
        icon: 'database',
        size: 'sm',
        onClick: () => { if (requireAdmin(t('Route to BigQuery'))) openRouteModal(ctx, { source: connSource }); },
      }) : null,
    }));
  }
  if (cat && cat.sensitive) {
    body.append(banner({ tone: 'warning', title: t('Contains prompt or response text'), text: t('The latest entries of its dashboard show that text to administrators. Viewers see it only when "Mask personal data for viewers" is turned off in Settings.') }));
  }
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
  if (m.user) cols.push({ key: 'user', label: t('User'), render: (r) => h('span', { class: 'cell-mono truncate tx-col-user', title: r.user || '' }, r.user || DASH) });
  if (m.operation) {
    cols.push({
      key: 'operation', label: opLabel,
      render: (r) => (r.operation && opNames[r.operation]
        ? h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main truncate tx-col-op', title: opNames[r.operation] }, opNames[r.operation]), h('span', { class: 'cell-sub cell-mono truncate tx-col-op', title: r.operation }, r.operation))
        : h('span', { class: 'cell-mono truncate tx-col-op', title: r.operation || '' }, r.operation || DASH)),
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
  const nameIn = textInput({ value: st.name, placeholder: t('For example Agent runtime'), onInput: (v) => { st.name = v; } });

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
        help: t('Tables discovered in the audit, routed and telemetry datasets.'),
        control: selectInput([{ value: '', label: t('Select a table') }, ...tables.map((tb) => ({ value: `${tb.project}.${tb.dataset}.${tb.logical}`, label: `${tb.dataset}.${tb.logical}${tb.sharded ? ' (sharded)' : ''}` }))], st.tableKey, (v) => {
          st.tableKey = v;
          const tb = findTable();
          if (tb) { st.mapping = { ...(tb.suggestedMapping || {}) }; if (!st.name) { st.name = prettyName(tb.logical); nameIn.value = st.name; } }
          renderMapping();
        }),
      }));
    } else {
      const ta = h('textarea', { class: 'textarea mono', rows: 3, placeholder: 'logName:"agent"', maxlength: 2000 });
      ta.value = st.filter;
      ta.addEventListener('input', () => { st.filter = ta.value; });
      mount(sourceHost, field({ label: t('Cloud Logging filter'), help: t('Any Logging query language filter. Entries are read through the Logging API for the selected period.'), control: ta }));
    }
    renderMapping();
  };
  const save = button({ label: existing ? t('Save connector') : t('Create connector'), variant: 'primary' });
  const m = openModal({
    title: existing ? t('Edit connector') : t('Custom connector'),
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
    if (existing && existing.meta) payload.meta = existing.meta;
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
