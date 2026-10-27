/**
 * Model Armor: sanitize operations, block verdicts and filter matches.
 */
import { h, mount } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api } from '../core/api.js';
import { rangeParams } from '../core/state.js';
import {
  pageHeader, kpi, card, dataTable, badge, statusBadge, userCell, segmented, chip, openDrawer, kv, load, banner, listCell,
} from '../core/components.js';
import { barChart, hbarList, mapList, TONES, colorAt } from '../core/charts.js';
import { pageFlow, fetchRange, rangeText, deltaOf, chartCard, mountCharts, openUserDrawer, filterName, FILTER_NAMES } from './_shared.js';

const OP_LABELS = { prompt: 'User prompt', response: 'Model response', search_result: 'Search result (grounding)', other: 'Other' };
const ORIGIN_LABELS = { user: 'Person', automatic: 'Automatic run', unknown: 'Not recorded' };

/** Who or what caused the screened operation: a person, an automatic run, or not recorded in the logs. */
function originCell(r) {
  if (r.origin === 'user' && r.email) return userCell(r.email);
  if (r.origin === 'automatic') {
    const sub = r.email ? (r.inferred ? t('{email} (by run time)', { email: r.email }) : r.email) : t('Session summary or scheduled run');
    return h('div', { class: 'user-cell', title: t('Gemini Enterprise ran this by itself (session summaries, scheduled runs). It is not a prompt typed by a person.') },
      h('span', { class: 'avatar muted', 'aria-hidden': 'true' }, icon('zap', { size: 14 })),
      h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main' }, t('Automatic run')), h('span', { class: ['cell-sub', 'truncate'], title: sub }, sub)));
  }
  return h('div', { class: 'user-cell', title: t('Model Armor does not record users, and this operation could not be matched to Gemini Enterprise activity.') },
    h('span', { class: 'avatar muted', 'aria-hidden': 'true' }, icon('user', { size: 14 })),
    h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main muted' }, t('User not recorded'))));
}

function originText(r) {
  if (r.origin === 'user') return r.email || t(ORIGIN_LABELS.user);
  if (r.origin === 'automatic') return r.email ? `${t('Automatic run')} \u00b7 ${r.email}${r.inferred ? ` (${t('by run time')})` : ''}` : t('Automatic run');
  return t('User not recorded');
}

export default async function render(ctx) {
  const header = pageHeader({
    title: t('Model Armor'),
    desc: t('Prompt and response screening: block verdicts, matched filters, affected applications and the users behind them.'),
  });
  return pageFlow(ctx, {
    header,
    kpiCount: 5,
    fetcher: () => fetchRange(ctx, '/api/v1/model-armor/summary'),
    render: (body, d) => renderPage(ctx, body, d),
  });
}

function renderPage(ctx, body, d) {
  const k = d.kpis || {};
  const origin = k.blockedByOrigin || {};
  const trendSeries = (key) => ((d.trend && d.trend.series) || []).find((s) => s.key === key);
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Sanitize operations'), value: fmt.compact(k.operations && k.operations.value), delta: deltaOf(k.operations), goodWhen: null }),
    kpi({ label: t('Blocked'), value: fmt.int(k.blocked && k.blocked.value), delta: deltaOf(k.blocked), goodWhen: 'down', spark: (trendSeries('blocked') || {}).values, sparkColor: TONES.danger }),
    kpi({ label: t('Blocked: people'), value: fmt.int(origin.user), tip: t('Blocks on prompts and files that people sent.') }),
    kpi({ label: t('Blocked: automatic runs'), value: fmt.int(origin.automatic), tip: t('Blocks in runs Gemini Enterprise starts by itself (session summaries, scheduled runs), mostly web search results it screened.') }),
    kpi({ label: t('Block rate'), value: fmt.pct(k.blockRate, 2) })));
  if (isNum(origin.automatic) && origin.automatic > 0) {
    body.append(banner({
      tone: 'info', iconName: 'zap', title: t('{n} of {total} blocks came from automatic runs', { n: fmt.int(origin.automatic), total: fmt.int(k.blocked && k.blocked.value) }),
      text: t('Gemini Enterprise summarizes sessions and runs scheduled tasks by itself. The web search results these runs read are screened too, so blocks can appear on days nobody used the assistant. They are not prompts typed by people.'),
    }));
  }

  const row1 = h('div', { class: 'grid' });
  const trend = chartCard({
    title: t('Verdicts over time'), subtitle: rangeText(), source: 'modelarmor_googleapis_com_sanitize_operations',
    build: (host) => barChart(host, {
      buckets: (d.trend && d.trend.buckets) || [], granularity: d.trend && d.trend.granularity, stacked: true, integer: true, height: 250,
      series: [
        { key: 'allowed', name: t('Allowed'), values: (trendSeries('allowed') || {}).values || [], color: TONES.neutral },
        { key: 'blocked', name: t('Blocked'), values: (trendSeries('blocked') || {}).values || [], color: TONES.danger },
      ],
    }),
  });
  trend.classList.add('span-8');
  const filters = card({
    title: t('Matched filters'), subtitle: t('Filter matches on screened content'),
    body: hbarList(mapList(d.filters, (f, i) => ({ label: t(f.name || FILTER_NAMES[f.key] || f.key), value: f.matches, color: [TONES.danger, TONES.warning, TONES.violet, TONES.info, TONES.slate][i % 5] }))),
  });
  filters.classList.add('span-4');
  body.append(row1);
  mountCharts(row1, [trend, filters]);

  const row2 = h('div', { class: 'grid' });
  const apps = card({
    title: t('By application'), subtitle: t('Blocked versus screened'),
    body: hbarList(mapList(d.byApp, (a, i) => ({ label: a.app || t('Unknown'), value: a.blocked, sub: t('of {n}', { n: fmt.compact(a.operations) }), color: colorAt(i) }))),
  });
  apps.classList.add('span-5');
  const users = card({
    title: t('Users with blocked content'), subtitle: t('Prompts sent by people only; automatic runs are excluded'), flush: true,
    body: dataTable({
      columns: [
        { key: 'email', label: t('User'), render: (r) => userCell(r.email) },
        { key: 'blocked', label: t('Blocked'), align: 'num', format: fmt.int },
        { key: 'lastBlocked', label: t('Last blocked'), render: (r) => (r.lastBlocked ? fmt.relative(r.lastBlocked) : DASH), value: (r) => r.lastBlocked },
      ],
      rows: d.byUser, searchable: false, pageSize: 6, initialSort: { key: 'blocked', dir: 'desc' },
      emptyText: t('No blocked content was attributed to users in this period.'),
      onRowClick: (r) => r.uid && openUserDrawer(r.uid),
    }),
  });
  users.classList.add('span-7');
  row2.append(apps, users);
  body.append(row2);

  const state = { verdict: 'blocked', filter: '', origin: 'all' };
  let lastItems = null;
  const eventsHost = h('div');
  const filterChips = h('div', { class: 'chips' });
  const renderChips = () => mount(filterChips,
    chip(t('All filters'), { pressed: !state.filter, onClick: () => { state.filter = ''; renderChips(); fetchEvents(); } }),
    Object.keys(FILTER_NAMES).map((key) => chip(filterName(key), { pressed: state.filter === key, onClick: () => { state.filter = key; renderChips(); fetchEvents(); } })));
  const renderEvents = () => {
    const rows = lastItems === null ? null : lastItems.filter((r) => state.origin === 'all' || r.origin === state.origin);
    mount(eventsHost, dataTable({
      columns: [
        { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time, true), value: (r) => r.time },
        { key: 'email', label: t('Caused by'), render: (r) => originCell(r), value: (r) => originText(r), csv: (r) => originText(r) },
        { key: 'app', label: t('Application'), render: (r) => h('span', { class: 'cell-mono' }, r.app || DASH) },
        { key: 'operation', label: t('Operation'), render: (r) => t(OP_LABELS[r.operation] || r.operation || 'Other') },
        { key: 'verdict', label: t('Verdict'), render: (r) => statusBadge(r.verdict, { size: 'sm' }) },
        { key: 'filters', label: t('Filters'), sortable: false, render: (r) => listCell((r.filters || []).map((f) => filterName(f))), csv: (r) => (r.filters || []).join('|') },
        { key: 'text', label: t('Content'), render: (r) => (r.restricted ? h('span', { class: 'muted' }, t('Restricted')) : h('span', { class: 'truncate content-preview', title: r.text || '' }, r.text || DASH)) },
      ],
      rows, pageSize: 10, exportName: 'model-armor-events',
      emptyText: t('No events match the selected filters.'),
      onRowClick: (r) => openEvent(r),
    }));
  };
  const fetchEvents = () => load(eventsHost, () => api.get('/api/v1/model-armor/events', rangeParams({ verdict: state.verdict, filter: state.filter || undefined, limit: 200 }), { signal: ctx.signal }), (res) => {
    lastItems = res.data.items;
    renderEvents();
  }, { skeletonKind: 'table', signal: ctx.signal });
  renderChips();
  const originToggle = segmented([{ value: 'all', label: t('All') }, { value: 'user', label: t('People') }, { value: 'automatic', label: t('Automatic runs') }], state.origin, (v) => { state.origin = v; renderEvents(); }, { label: t('Caused by') });
  const verdictToggle = segmented([{ value: 'blocked', label: t('Blocked') }, { value: 'all', label: t('All verdicts') }], state.verdict, (v) => { state.verdict = v; fetchEvents(); });
  body.append(card({
    title: t('Screening events'), subtitle: t('Latest sanitize operations with verdict details'), flush: true, actions: h('div', { class: 'row' }, originToggle, verdictToggle),
    body: h('div', null, h('div', { class: 'table-toolbar' }, h('div', { class: 'left' }, filterChips)), eventsHost),
    source: 'modelarmor_googleapis_com_sanitize_operations LEFT JOIN gemini_enterprise_user_activity',
  }));
  fetchEvents();
}

function openEvent(r) {
  openDrawer({
    title: r.verdict === 'blocked' ? t('Blocked content') : t('Screened content'),
    subtitle: fmt.dateTime(r.time, true),
    width: 680,
    content: (body) => body.append(
      h('div', { class: 'row wrap' }, statusBadge(r.verdict), (r.filters || []).map((f) => badge(filterName(f), 'danger', { size: 'sm' }))),
      card({
        title: t('Event'),
        body: kv([
          [t('Caused by'), originText(r)],
          [t('Application'), r.app],
          [t('Operation'), t(OP_LABELS[r.operation] || r.operation || 'Other')],
          r.origin === 'automatic' ? [t('Note'), t('Gemini Enterprise ran this by itself (session summaries, scheduled runs). It is not a prompt typed by a person.')] : null,
          [t('Template'), r.template ? h('span', { class: 'mono' }, r.template) : null],
          [t('Reason'), r.reason],
          [t('Event ID'), h('span', { class: 'mono' }, r.id)],
        ]),
      }),
      card({
        title: t('Content'),
        body: r.restricted
          ? banner({ tone: 'neutral', iconName: 'lock', title: t('Restricted'), text: t('Screened content is visible to administrators only.') })
          : h('div', { class: 'text-block' }, r.text || t('No content captured for this operation.')),
      })),
  });
}
