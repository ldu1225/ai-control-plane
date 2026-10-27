/**
 * Overview: cross-module governance posture.
 */
import { h, append } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api } from '../core/api.js';
import { isModuleEnabled } from '../core/state.js';
import { pageHeader, kpi, card, button, dataTable, statusBadge, userCell, emptyState, load } from '../core/components.js';
import { serverText } from '../core/messages.js';
import { lineChart, barChart, donutChart, fromTimeline, TONES, colorAt } from '../core/charts.js';
import { pageFlow, fetchRange, rangeText, deltaOf, chartCard, mountCharts, openUserDrawer } from './_shared.js';
import { widgetBoard } from '../core/widgets.js';

export default async function render(ctx) {
  const header = pageHeader({
    title: t('Overview'),
    desc: t('Governance posture across Gemini Enterprise, Agent Platform, models and cloud spend.'),
    actions: isModuleEnabled('analyst') ? button({ label: t('Ask AI Analyst'), icon: 'spark', onClick: () => ctx.navigate('#/analyst') }) : null,
  });
  return pageFlow(ctx, {
    header,
    kpiCount: 8,
    blocks: 2,
    fetcher: () => fetchRange(ctx, '/api/v1/overview/summary'),
    render: (body, d) => renderOverview(ctx, body, d),
  });
}

function sumSeries(timeline) {
  if (!timeline || !timeline.series) return [];
  const n = (timeline.buckets || []).length;
  const out = new Array(n).fill(0);
  timeline.series.forEach((sr) => (sr.values || []).forEach((v, i) => { out[i] += isNum(v) ? v : 0; }));
  return out;
}

function seriesValues(timeline, key) {
  const sr = timeline && (timeline.series || []).find((x) => x.key === key);
  return sr ? sr.values : [];
}

function renderOverview(ctx, body, d) {
  const k = d.kpis || {};
  const spend = k.aiSpend || {};
  const ccy = spend.currency || 'USD';
  const lic = k.licenses || {};
  const agents = k.agents || {};
  const nav = (hash) => () => ctx.navigate(hash);

  const kpis = h('div', { class: 'kpi-grid' },
    kpi({
      label: t('Active users'), value: fmt.int(k.activeUsers && k.activeUsers.value), delta: deltaOf(k.activeUsers),
      disabled: k.activeUsers && k.activeUsers.disabled,
      meta: isNum(lic.purchased) ? h('span', null, t('{a} of {p} seats assigned', { a: fmt.int(lic.assigned), p: fmt.int(lic.purchased) })) : null,
      tip: t('Distinct people who submitted prompts or used agents in Gemini Enterprise during the period.'),
      onClick: nav('#/gemini-enterprise'),
    }),
    kpi({
      label: t('Prompts'), value: fmt.int(k.prompts && k.prompts.value), delta: deltaOf(k.prompts), disabled: k.prompts && k.prompts.disabled,
      tip: t('User prompts submitted in Gemini Enterprise, excluding internal system messages.'), onClick: nav('#/gemini-enterprise'),
    }),
    kpi({
      label: t('Model tokens'), value: fmt.compact(k.modelTokens && k.modelTokens.value), delta: deltaOf(k.modelTokens),
      disabled: k.modelTokens && k.modelTokens.disabled,
      meta: k.modelTokens && k.modelTokens.source ? h('span', null, k.modelTokens.source === 'monitoring' ? t('Cloud Monitoring') : t('Billing export')) : null,
      spark: sumSeries(d.tokenTrend), sparkColor: TONES.violet,
      tip: t('Input and output tokens consumed by all enabled models in the catalog.'), onClick: nav('#/models'),
    }),
    kpi({
      label: spend.hasCredits ? t('AI and cloud spend (net)') : t('AI and cloud spend'),
      value: fmt.currency(spend.hasCredits ? spend.net : spend.gross, ccy, { compact: true }),
      delta: isNum(spend.previousNet) && isNum(spend.net) && spend.previousNet !== 0 ? (spend.net - spend.previousNet) / Math.abs(spend.previousNet) : null,
      goodWhen: 'down', disabled: spend.disabled,
      meta: spend.hasCredits ? h('span', null, t('{c} credits applied', { c: fmt.currency(spend.credits, ccy) })) : null,
      spark: seriesValues(d.spendTrend, spend.hasCredits ? 'net' : 'gross'), sparkColor: TONES.teal,
      tip: t('Billing export cost for the period. Net cost subtracts credits only when credit rows exist.'), onClick: nav('#/finops'),
    }),
    kpi({
      label: t('Model Armor blocks'), value: fmt.int(k.armorBlocks && k.armorBlocks.value), delta: deltaOf(k.armorBlocks), goodWhen: 'down',
      disabled: k.armorBlocks && k.armorBlocks.disabled,
      meta: k.armorBlocks && isNum(k.armorBlocks.operations) ? h('span', null, t('of {n} sanitize operations', { n: fmt.int(k.armorBlocks.operations) })) : null,
      spark: seriesValues(d.armorTrend, 'blocked'), sparkColor: TONES.danger,
      tip: t('Prompts and responses blocked by Model Armor policies.'), onClick: nav('#/model-armor'),
    }),
    kpi({
      label: t('Registered agents'), value: fmt.int(agents.total), disabled: agents.disabled,
      meta: isNum(agents.deprecationCandidates) ? h('span', null, t('{n} deprecation candidates', { n: fmt.int(agents.deprecationCandidates) })) : null,
      tip: t('Agents registered in Agent Engine and Gemini Enterprise, plus agents observed in activity logs.'), onClick: nav('#/registry'),
    }),
    isModuleEnabled('notebooklm') ? kpi({
      label: t('NotebookLM prompts'), value: fmt.int(k.notebooklmPrompts && k.notebooklmPrompts.value), delta: deltaOf(k.notebooklmPrompts),
      disabled: k.notebooklmPrompts && k.notebooklmPrompts.disabled, onClick: nav('#/notebooklm'),
    }) : null,
    isModuleEnabled('antigravity') ? kpi({
      label: t('Antigravity tokens'), value: fmt.compact(k.antigravityTokens && k.antigravityTokens.value), delta: deltaOf(k.antigravityTokens),
      disabled: k.antigravityTokens && k.antigravityTokens.disabled,
      meta: k.antigravityTokens && isNum(k.antigravityTokens.users) ? h('span', null, t('{n} users', { n: fmt.int(k.antigravityTokens.users) })) : null,
      onClick: nav('#/antigravity'),
    }) : null);
  body.append(kpis);
  // Personal widgets: Telemetry Extensions dashboards each person pins here and arranges by drag and drop.
  const board = widgetBoard({ page: 'overview', signal: ctx.signal, navigate: ctx.navigate, prominent: true });
  if (board) body.append(board);

  const row1 = h('div', { class: 'grid' });
  const spendCard = chartCard({
    title: t('Spend trend'), subtitle: rangeText(), source: t('Cloud Billing export'),
    build: (host) => lineChart(host, {
      ...fromTimeline(d.spendTrend, { names: { gross: t('Gross cost'), net: t('Net cost') }, colors: { gross: TONES.info, net: TONES.teal } }),
      yFormat: (v) => fmt.currency(v, ccy, { compact: true }), valueFormat: (v) => fmt.currency(v, ccy), height: 250,
    }),
  });
  spendCard.classList.add('span-7');
  const tokenCard = chartCard({
    title: t('Token usage by model'), subtitle: rangeText(), source: k.modelTokens && k.modelTokens.source === 'monitoring' ? t('Cloud Monitoring') : t('Cloud Billing export'),
    actions: button({ label: t('Models'), size: 'xs', variant: 'ghost', iconRight: 'arrow-right', onClick: nav('#/models') }),
    build: (host) => barChart(host, { ...fromTimeline(d.tokenTrend), stacked: true, height: 250, valueFormat: fmt.compact }),
  });
  tokenCard.classList.add('span-5');
  body.append(row1);
  mountCharts(row1, [spendCard, tokenCard]);

  const row2 = h('div', { class: 'grid' });
  const armorCard = chartCard({
    title: t('Security posture'), subtitle: t('Model Armor sanitize operations'), source: 'modelarmor_googleapis_com_sanitize_operations',
    build: (host) => {
      const tl = d.armorTrend;
      if (!tl) return;
      const ops = seriesValues(tl, 'operations');
      const blocked = seriesValues(tl, 'blocked');
      barChart(host, {
        buckets: tl.buckets, granularity: tl.granularity, stacked: true, integer: true, height: 220,
        series: [
          { key: 'allowed', name: t('Allowed'), values: ops.map((v, i) => Math.max(0, (v || 0) - (blocked[i] || 0))), color: TONES.neutral },
          { key: 'blocked', name: t('Blocked'), values: blocked, color: TONES.danger },
        ],
      });
    },
  });
  armorCard.classList.add('span-4');
  const agentCard = chartCard({
    title: t('Agent lifecycle'), subtitle: t('Based on last activity'),
    build: (host) => donutChart(host, {
      items: [
        { label: t('Active'), value: agents.active, color: TONES.success },
        { label: t('Idle'), value: agents.idle, color: TONES.warning },
        { label: t('Deprecation candidate'), value: agents.deprecationCandidates, color: TONES.danger },
      ],
      centerValue: fmt.int(agents.total), centerLabel: t('agents'), size: 150,
      emptyText: isNum(agents.total) ? undefined : t('Data source unavailable'),
    }),
  });
  agentCard.classList.add('span-4');
  const unavailableState = () => emptyState({ icon: 'alert-triangle', tone: 'warning', title: t('Data source unavailable'), body: t('See the data source notices above.'), compact: true });
  const usersCard = card({
    title: t('Most active users'), subtitle: rangeText(), flush: true,
    actions: button({ label: t('Users'), size: 'xs', variant: 'ghost', iconRight: 'arrow-right', onClick: nav('#/users') }),
    body: d.topUsers == null ? unavailableState() : (d.topUsers.length ? dataTable({
      columns: [
        { key: 'email', label: t('User'), render: (r) => userCell(r.email, isNum(r.antigravityTokens) && r.antigravityTokens > 0 ? `${t('AG tokens')} ${fmt.compact(r.antigravityTokens)}` : null, { compact: true }) },
        { key: 'prompts', label: t('Prompts'), align: 'num', format: fmt.int },
      ],
      rows: d.topUsers, searchable: false, pageSize: 6, initialSort: { key: 'prompts', dir: 'desc' },
      onRowClick: (r) => r.uid && openUserDrawer(r.uid),
    }) : emptyState({ icon: 'users', title: t('No user activity'), body: t('No user activity was recorded in this period.'), compact: true })),
  });
  usersCard.classList.add('span-4');
  body.append(row2);
  mountCharts(row2, [armorCard, agentCard, usersCard]);

  const row3 = h('div', { class: 'grid' });
  const actionsCard = card({
    title: t('Recent governance actions'), subtitle: t('Administrative changes recorded in Cloud Logging'), flush: true,
    source: 'logs/agy-admin%2Fadmin_actions',
    body: d.recentActions == null ? unavailableState() : (d.recentActions.length ? dataTable({
      columns: [
        { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time), value: (r) => r.time },
        { key: 'actor', label: t('Actor'), render: (r) => h('span', { class: 'truncate', title: r.actor || '' }, r.actor || DASH) },
        { key: 'action', label: t('Action'), render: (r) => h('span', { class: 'cell-mono' }, r.action) },
        { key: 'target', label: t('Target'), wrap: true },
        { key: 'result', label: t('Result'), render: (r) => statusBadge(r.result, { size: 'sm' }) },
      ],
      rows: d.recentActions, searchable: false, pageSize: 6,
    }) : emptyState({ icon: 'sliders', title: t('No governance actions yet'), body: t('Cap enforcement, allow and disallow actions will appear here.'), compact: true })),
  });
  actionsCard.classList.add('span-8');
  const healthHost = h('div');
  const healthCard = card({ title: t('Data source health'), subtitle: t('Connectivity of the configured Google Cloud sources'), body: healthHost });
  healthCard.classList.add('span-4');
  append(row3, [actionsCard, healthCard]);
  body.append(row3);
  load(healthHost, () => api.get('/api/v1/health', null, { signal: ctx.signal }), (res) => {
    const checks = res.data.checks || [];
    append(healthHost, [h('div', { class: 'checklist' }, checks.map((c) => h('div', { class: 'check-item' },
      statusBadge(c.status, { size: 'sm' }),
      h('div', { class: 'ci-text' }, h('div', null, t(c.label || c.id)), c.detail ? h('div', { class: 'ci-sub' }, serverText(c.detail)) : null))))]);
  }, { skeletonKind: 'lines', signal: ctx.signal });
  return null;
}
