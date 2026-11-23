/**
 * Users & Access: unified per-user activity across surfaces with IAM enforcement state.
 */
import { h, mount } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { isAdmin } from '../core/state.js';
import {
  pageHeader, kpi, card, dataTable, chip, badge, banner, button, userCell, selectInput, segmented,
  progress, emptyState, requireAdmin,
} from '../core/components.js';
import { proportionBar } from '../core/charts.js';
import {
  pageFlow, fetchRange, rangeText, surfaceMatrix, surfaceLegend, iamBadge, openUserDrawer, IAM_STATE_COLORS, IAM_STATE_LABELS,
  modelCostCell, costShareCell, filteredCostSummary,
} from './_shared.js';
import { accessMembers, openAccessDrawer } from './settings.js';

const SURFACES = [
  ['all', 'All surfaces'], ['gemini_enterprise', 'Gemini Enterprise'], ['notebooklm', 'NotebookLM'], ['antigravity', 'Antigravity'], ['agent_platform', 'Agent Platform'],
];

const MODEL_SURFACE_LABELS = {
  antigravity: 'Antigravity',
  agent_platform: 'Agent Platform',
};

export default async function render(ctx) {
  const header = pageHeader({
    title: t('Users & Access'),
    desc: t('One view of every person using Gemini Enterprise, NotebookLM, Antigravity and Agent Platform, with their models, costs and access enforcement state.'),
    actions: [
      button({
        label: t('Add account'),
        icon: 'plus',
        variant: 'primary',
        onClick: () => {
          if (!requireAdmin(t('Add account / Manage access control'))) return;
          openAccessDrawer({ onChanged: () => ctx.rerender() });
        },
      }),
    ],
  });
  return pageFlow(ctx, {
    header,
    kpiCount: 5,
    blocks: 1,
    fetcher: () => fetchRange(ctx, '/api/v1/users'),
    render: (body, d) => renderPage(ctx, body, d),
  });
}

function renderPage(ctx, body, d) {
  const k = d.kpis || {};
  const users = d.users || [];
  const ccy = d.currency || 'USD';
  const userModels = Array.isArray(d.userModels) ? d.userModels : [];
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Active users'), value: fmt.int(k.activeUsers), tip: t('People with activity on at least one surface in the period.') }),
    kpi({ label: t('Antigravity users'), value: fmt.int(k.antigravityUsers) }),
    kpi({
      label: t('Estimated cost'),
      value: isNum(k.estimatedCost) ? fmt.currency(k.estimatedCost, ccy, { precise: true }) : DASH,
      meta: h('span', null, t('{n} model records', { n: fmt.int(userModels.length) })),
      onClick: () => ctx.navigate('#/antigravity'),
    }),
    kpi({ label: t('Over cap'), value: fmt.int(k.overCap), meta: h('span', null, t('Against the active Antigravity cap policy')), onClick: () => ctx.navigate('#/antigravity') }),
    kpi({ label: t('Enforceable coverage'), value: fmt.pct(k.enforceableCoverage), meta: isNum(k.enforceableCoverage) ? progress(k.enforceableCoverage, k.enforceableCoverage >= 0.8 ? 'success' : 'warning') : null, tip: t('Share of users whose access can be safely controlled through individual project bindings.') })));

  if (isAdmin()) {
    body.append(accessMembers(ctx, true));
  }

  if (d.iamAvailable === false) {
    body.append(banner({ tone: 'warning', title: t('IAM state unavailable'), text: t('The project IAM policy could not be read, so enforcement states are shown as Unknown. Grant the control plane identity resourcemanager.projects.getIamPolicy.') }));
  }

  const effectiveState = (u) => ((u.iam && u.iam.aiBlocked && u.iam.aiBlocked.active) ? 'ALL_AI_BLOCKED' : ((u.iam && u.iam.state) || 'UNKNOWN'));
  const counts = {};
  users.forEach((u) => { const s = effectiveState(u); counts[s] = (counts[s] || 0) + 1; });
  const stateDefs = [
    ...Object.keys(IAM_STATE_LABELS).map((s) => ({
      key: s,
      label: s === 'BLOCKED_BY_ADMIN' ? t('Antigravity blocked') : t(IAM_STATE_LABELS[s]),
      color: IAM_STATE_COLORS[s],
    })),
    { key: 'ALL_AI_BLOCKED', label: t('All AI blocked'), color: IAM_STATE_COLORS.BLOCKED_BY_ADMIN },
  ];
  const segments = stateDefs.map((d0) => ({ label: d0.label, value: counts[d0.key] || 0, color: d0.color }));
  body.append(card({
    title: t('Access enforcement states'), subtitle: t('Classification of each user against the project IAM policy'),
    body: Array.isArray(d.users) ? h('div', null,
      proportionBar(segments),
      h('div', { class: 'state-legend' }, segments.map((sg) => {
        const sw = h('span', { class: 'swatch round' });
        sw.style.setProperty('background', sg.color);
        return h('div', { class: 'sl-row' }, sw, h('span', null, sg.label), h('strong', null, fmt.int(sg.value)));
      }))) : emptyState({ icon: 'alert-triangle', tone: 'warning', compact: true, title: t('User list unavailable'), body: t('None of the user activity sources could be read. See the data source notices above.') }),
  }));

  const filters = { view: 'users', surface: 'all', state: 'all', modelUser: 'all', modelName: 'all', modelSurface: 'all' };
  const chipsBar = h('div', { class: 'table-toolbar' });
  const chips = h('div', { class: 'chips' });
  chipsBar.append(h('div', { class: 'left' }, chips));
  const tableHost = h('div');
  const renderChips = () => mount(chips, SURFACES.map(([v, l]) => chip(t(l), { pressed: filters.surface === v, onClick: () => { filters.surface = v; renderChips(); renderView(); } })));
  const stateSelect = selectInput(
    [{ value: 'all', label: t('All access states') }, ...stateDefs.map((d0) => ({ value: d0.key, label: d0.label }))],
    'all', (v) => { filters.state = v; renderView(); }, { 'aria-label': t('Access state') });

  const distinctEmails = Array.from(new Set(userModels.map((r) => r.email).filter(Boolean))).sort();
  const distinctModels = Array.from(new Set(userModels.map((r) => r.model).filter(Boolean))).sort();
  const distinctSurfaces = Array.from(new Set(userModels.map((r) => r.surface).filter(Boolean))).sort();

  const renderView = () => {
    chipsBar.hidden = filters.view !== 'users';
    if (filters.view === 'models') {
      const mRows = userModels.filter((r) => (filters.modelUser === 'all' || r.email === filters.modelUser)
        && (filters.modelName === 'all' || r.model === filters.modelName)
        && (filters.modelSurface === 'all' || r.surface === filters.modelSurface));
      const toolbarItems = [
        selectInput(
          [{ value: 'all', label: t('All users') }, ...distinctEmails.map((em) => ({ value: em, label: em }))],
          filters.modelUser, (v) => { filters.modelUser = v; renderView(); }, { 'aria-label': t('Filter by user') }),
        selectInput(
          [{ value: 'all', label: t('All models') }, ...distinctModels.map((m) => ({ value: m, label: m }))],
          filters.modelName, (v) => { filters.modelName = v; renderView(); }, { 'aria-label': t('Filter by model') }),
      ];
      if (distinctSurfaces.length > 1) {
        toolbarItems.push(selectInput(
          [{ value: 'all', label: t('All surfaces') }, ...distinctSurfaces.map((s0) => ({ value: s0, label: t(MODEL_SURFACE_LABELS[s0] || s0) }))],
          filters.modelSurface, (v) => { filters.modelSurface = v; renderView(); }, { 'aria-label': t('Filter by surface') }));
      }
      mount(tableHost,
        filteredCostSummary(mRows, ccy),
        dataTable({
          columns: [
            { key: 'email', label: t('User'), render: (r) => userCell(r.email) },
            { key: 'surface', label: t('Surface'), render: (r) => badge(t(MODEL_SURFACE_LABELS[r.surface] || r.surface || ''), r.surface === 'antigravity' ? 'accent' : 'info', { size: 'sm' }), value: (r) => r.surface || '' },
            { key: 'model', label: t('Model'), render: (r) => h('span', { class: 'cell-mono' }, r.model || DASH), value: (r) => r.model || '' },
            { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
            { key: 'promptTokens', label: t('Input tokens'), align: 'num', format: fmt.compact, csv: (r) => (isNum(r.promptTokens) ? r.promptTokens : '') },
            { key: 'cachedTokens', label: t('From cache'), align: 'num', format: fmt.compact, csv: (r) => (isNum(r.cachedTokens) ? r.cachedTokens : '') },
            { key: 'outputTokens', label: t('Output tokens'), align: 'num', format: fmt.compact, csv: (r) => (isNum(r.outputTokens) ? r.outputTokens : '') },
            { key: 'tokens', label: t('Total tokens'), align: 'num', render: (r) => (isNum(r.tokens) ? h('span', { class: 'fw-600' }, fmt.compact(r.tokens)) : h('span', { class: 'text-muted' }, DASH)), value: (r) => r.tokens, csv: (r) => (isNum(r.tokens) ? r.tokens : '') },
            { key: 'cost', label: t('Estimated cost'), align: 'num', render: (r) => (isNum(r.cost) ? h('span', { class: 'fw-600', title: r.costPartial ? t('Some models have no unit price') : null }, fmt.currency(r.cost, r.currency || ccy, { precise: true }), r.costPartial ? ' *' : '') : h('span', { class: 'text-muted' }, DASH)), value: (r) => r.cost, csv: (r) => (isNum(r.cost) ? r.cost : '') },
            { key: 'costShare', label: t('Cost share'), align: 'num', render: (r) => costShareCell(r.costShare), value: (r) => r.costShare, csv: (r) => (isNum(r.costShare) ? r.costShare : '') },
          ],
          rows: mRows, pageSize: 15, exportName: 'users-by-model', initialSort: { key: 'cost', dir: 'desc' },
          toolbar: h('div', { class: 'row wrap' }, ...toolbarItems),
          emptyText: t('No per-user model usage was recorded in this window.'),
          onRowClick: (r) => r.uid && openUserDrawer(r.uid, { email: r.email, onChanged: () => ctx.rerender() }),
        }));
      return;
    }
    const rows = users.filter((u) => (filters.surface === 'all' || (u.surfaces || []).includes(filters.surface))
      && (filters.state === 'all' || effectiveState(u) === filters.state));
    mount(tableHost, dataTable({
      columns: [
        { key: 'email', label: t('User'), render: (u) => userCell(u.email) },
        { key: 'surfaces', label: t('Surfaces'), title: surfaceLegend(), sortable: false, render: (u) => surfaceMatrix(u.surfaces), csv: (u) => (u.surfaces || []).join('|') },
        {
          key: 'models', label: t('Models & cost'), sortable: false,
          render: (u) => modelCostCell(u.byModel, u.currency || ccy),
          value: (u) => (u.models || []).join(' '),
          csv: (u) => (u.byModel || []).map((m) => `${m.model}:${isNum(m.cost) ? m.cost : m.requests}`).join('|'),
        },
        {
          key: 'estimatedCost', label: t('Estimated cost'), align: 'num',
          render: (u) => (isNum(u.estimatedCost)
            ? h('span', { class: 'fw-600', title: u.costPartial ? t('Some models have no unit price') : null }, fmt.currency(u.estimatedCost, u.currency || ccy, { precise: true }), u.costPartial ? ' *' : '')
            : h('span', { class: 'text-muted' }, DASH)),
          value: (u) => u.estimatedCost,
          csv: (u) => (isNum(u.estimatedCost) ? u.estimatedCost : ''),
        },
        { key: 'gePrompts', label: t('GE prompts'), align: 'num', format: fmt.int },
        { key: 'agentCalls', label: t('Agent calls'), align: 'num', format: fmt.int },
        { key: 'antigravityTokens', label: t('AG tokens'), align: 'num', format: fmt.compact },
        { key: 'agentPlatformCalls', label: t('Agent Platform calls'), align: 'num', format: fmt.int },
        { key: 'iam', label: t('IAM / AI status'), render: (u) => iamBadge(u.iam, { surfaceContext: 'users' }), value: (u) => effectiveState(u), csv: (u) => effectiveState(u) },
        { key: 'lastActive', label: t('Last active'), render: (u) => (u.lastActive ? fmt.relative(u.lastActive) : DASH), value: (u) => u.lastActive },
      ],
      rows: d.users === null ? null : rows, pageSize: 15, exportName: 'users-access', initialSort: { key: 'estimatedCost', dir: 'desc' },
      toolbar: stateSelect,
      emptyText: t('No users match the selected filters.'),
      onRowClick: (u) => openUserDrawer(u.uid, { email: u.email, onChanged: () => ctx.rerender() }),
    }));
  };
  const viewToggle = segmented(
    [{ value: 'users', label: t('By user') }, { value: 'models', label: t('By user & model') }],
    'users', (v) => { filters.view = v; renderView(); }, { label: t('View by') });
  renderChips();
  renderView();
  body.append(card({
    title: t('Users'), subtitle: rangeText(), actions: viewToggle, flush: true,
    body: h('div', null, chipsBar, tableHost),
    source: t('Audit logs, Gemini Enterprise logs, Antigravity inference logs, project IAM policy'),
  }));
  return null;
}
