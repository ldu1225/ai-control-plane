/**
 * Users & Access: unified per-user activity across surfaces with IAM enforcement state.
 */
import { h, mount } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { pageHeader, kpi, card, dataTable, chip, banner, userCell, selectInput, progress, emptyState } from '../core/components.js';
import { proportionBar } from '../core/charts.js';
import {
  pageFlow, fetchRange, rangeText, surfaceMatrix, surfaceLegend, iamBadge, openUserDrawer, IAM_STATE_COLORS, IAM_STATE_LABELS,
} from './_shared.js';

const SURFACES = [
  ['all', 'All surfaces'], ['gemini_enterprise', 'Gemini Enterprise'], ['notebooklm', 'NotebookLM'], ['antigravity', 'Antigravity'], ['vertex_ai', 'Vertex AI'],
];

export default async function render(ctx) {
  const header = pageHeader({
    title: t('Users & Access'),
    desc: t('One view of every person using Gemini Enterprise, NotebookLM, Antigravity and Vertex AI, with their access enforcement state.'),
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
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Active users'), value: fmt.int(k.activeUsers), tip: t('People with activity on at least one surface in the period.') }),
    kpi({ label: t('Antigravity users'), value: fmt.int(k.antigravityUsers) }),
    kpi({ label: t('Over cap'), value: fmt.int(k.overCap), meta: h('span', null, t('Against the active Antigravity cap policy')), onClick: () => ctx.navigate('#/antigravity') }),
    kpi({ label: t('Manual action required'), value: fmt.int(k.manualActionRequired), meta: h('span', null, t('Access cannot be enforced per user')), onClick: () => ctx.navigate('#/antigravity') }),
    kpi({ label: t('Enforceable coverage'), value: fmt.pct(k.enforceableCoverage), meta: isNum(k.enforceableCoverage) ? progress(k.enforceableCoverage, k.enforceableCoverage >= 0.8 ? 'success' : 'warning') : null, tip: t('Share of users whose access can be safely controlled through individual project bindings.') })));

  if (d.iamAvailable === false) {
    body.append(banner({ tone: 'warning', title: t('IAM state unavailable'), text: t('The project IAM policy could not be read, so enforcement states are shown as Unknown. Grant the control plane identity resourcemanager.projects.getIamPolicy.') }));
  }

  const counts = {};
  users.forEach((u) => { const s = (u.iam && u.iam.state) || 'UNKNOWN'; counts[s] = (counts[s] || 0) + 1; });
  const segments = Object.keys(IAM_STATE_LABELS).map((s) => ({ label: t(IAM_STATE_LABELS[s]), value: counts[s] || 0, color: IAM_STATE_COLORS[s] }));
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

  const filters = { surface: 'all', state: 'all' };
  const chips = h('div', { class: 'chips' });
  const tableHost = h('div');
  const renderChips = () => mount(chips, SURFACES.map(([v, l]) => chip(t(l), { pressed: filters.surface === v, onClick: () => { filters.surface = v; renderChips(); renderTable(); } })));
  const stateSelect = selectInput(
    [{ value: 'all', label: t('All access states') }, ...Object.keys(IAM_STATE_LABELS).map((s) => ({ value: s, label: t(IAM_STATE_LABELS[s]) }))],
    'all', (v) => { filters.state = v; renderTable(); }, { 'aria-label': t('Access state') });
  const renderTable = () => {
    const rows = users.filter((u) => (filters.surface === 'all' || (u.surfaces || []).includes(filters.surface))
      && (filters.state === 'all' || (u.iam && u.iam.state) === filters.state));
    mount(tableHost, dataTable({
      columns: [
        { key: 'email', label: t('User'), render: (u) => userCell(u.email) },
        { key: 'surfaces', label: t('Surfaces'), title: surfaceLegend(), sortable: false, render: (u) => surfaceMatrix(u.surfaces), csv: (u) => (u.surfaces || []).join('|') },
        { key: 'gePrompts', label: t('GE prompts'), align: 'num', format: fmt.int },
        { key: 'agentCalls', label: t('Agent calls'), align: 'num', format: fmt.int },
        { key: 'notebooklmPrompts', label: t('NotebookLM'), align: 'num', format: fmt.int },
        { key: 'antigravityTokens', label: t('AG tokens'), align: 'num', format: fmt.compact },
        { key: 'vertexCalls', label: t('Vertex calls'), align: 'num', format: fmt.int },
        { key: 'armorBlocks', label: t('Blocks'), align: 'num', render: (u) => (u.armorBlocks ? h('span', { class: 'text-danger fw-600' }, fmt.int(u.armorBlocks)) : fmt.int(u.armorBlocks || 0)), value: (u) => u.armorBlocks },
        { key: 'iam', label: t('Access state'), render: (u) => iamBadge(u.iam), value: (u) => (u.iam && u.iam.state) || '', csv: (u) => (u.iam && u.iam.state) || '' },
        { key: 'lastActive', label: t('Last active'), render: (u) => (u.lastActive ? fmt.relative(u.lastActive) : DASH), value: (u) => u.lastActive },
      ],
      rows: d.users === null ? null : rows, pageSize: 15, exportName: 'users-access', initialSort: { key: 'gePrompts', dir: 'desc' },
      toolbar: stateSelect,
      emptyText: t('No users match the selected filters.'),
      onRowClick: (u) => openUserDrawer(u.uid, { onChanged: () => ctx.rerender() }),
    }));
  };
  renderChips();
  renderTable();
  body.append(card({
    title: t('Users'), subtitle: rangeText(), flush: true,
    body: h('div', null, h('div', { class: 'table-toolbar' }, h('div', { class: 'left' }, chips)), tableHost),
    source: t('Audit logs, Gemini Enterprise logs, Antigravity inference logs, project IAM policy'),
  }));
  return null;
}
