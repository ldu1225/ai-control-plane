/**
 * Agent Registry: unified agent catalog with lifecycle (idle / deprecation) detection.
 */
import { h, mount } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { pageHeader, kpi, card, dataTable, badge, statusBadge, chip, openDrawer, kv, banner, userCell } from '../core/components.js';
import { barChart, fromTimeline, TONES } from '../core/charts.js';
import { pageFlow, fetchRange, rangeText, chartCard, mountCharts } from './_shared.js';

const PLATFORM_LABELS = {
  agent_engine: 'Agent Engine',
  gemini_enterprise: 'Gemini Enterprise',
  gemini_enterprise_observed: 'Observed in activity',
};

export default async function render(ctx) {
  const header = pageHeader({
    title: t('Agent Registry'),
    desc: t('Every agent registered in Agent Engine and Gemini Enterprise, enriched with activity to surface idle agents and deprecation candidates.'),
  });
  return pageFlow(ctx, {
    header,
    kpiCount: 5,
    fetcher: () => fetchRange(ctx, '/api/v1/agent-platform/registry'),
    render: (body, d) => renderPage(body, d),
  });
}

function renderPage(body, d) {
  const k = d.kpis || {};
  const th = d.thresholds || { idleDays: 14, deprecationDays: 50 };
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Total agents'), value: fmt.int(k.total) }),
    kpi({ label: t('Active'), value: fmt.int(k.active), meta: h('span', null, t('Activity within {n} days', { n: th.idleDays })) }),
    kpi({ label: t('Idle'), value: fmt.int(k.idle), meta: h('span', null, t('No activity for {n}+ days', { n: th.idleDays })) }),
    kpi({ label: t('Deprecation candidates'), value: fmt.int(k.deprecationCandidates), meta: h('span', null, t('No activity for {n}+ days', { n: th.deprecationDays })) }),
    kpi({ label: t('Created in period'), value: fmt.int(k.createdInRange) })));

  if (isNum(k.deprecationCandidates) && k.deprecationCandidates > 0) {
    body.append(banner({
      tone: 'warning',
      title: t('{n} agents are deprecation candidates', { n: k.deprecationCandidates }),
      text: t('These agents have had no activity for {n} days or more. Review ownership and decommission unused agents to reduce cost and attack surface.', { n: th.deprecationDays }),
    }));
  }

  const agents = d.agents || [];
  const filters = { status: 'all', platform: 'all', project: 'all' };
  const tableHost = h('div');
  const statusChips = h('div', { class: 'chips' });
  const platformChips = h('div', { class: 'chips' });
  const renderChips = () => {
    const counts = (key, v) => agents.filter((a) => v === 'all' || a[key] === v).length;
    mount(statusChips, [['all', 'All statuses'], ['active', 'Active'], ['idle', 'Idle'], ['deprecation_candidate', 'Deprecation candidate']].map(([v, l]) => chip(`${t(l)} ${counts('status', v)}`, { pressed: filters.status === v, onClick: () => { filters.status = v; renderChips(); renderTable(); } })));
    const platforms = ['all', ...new Set(agents.map((a) => a.platform))];
    const projects = [...new Set(agents.map((a) => a.project).filter(Boolean))];
    const rightChips = platforms.map((v) => chip(v === 'all' ? t('All platforms') : t(PLATFORM_LABELS[v] || v), { pressed: filters.platform === v, onClick: () => { filters.platform = v; renderChips(); renderTable(); } }));
    if (projects.length > 1) {
      rightChips.push(...['all', ...projects].map((p) => chip(p === 'all' ? t('All projects') : `${p} ${counts('project', p)}`, { pressed: filters.project === p, onClick: () => { filters.project = p; renderChips(); renderTable(); } })));
    }
    mount(platformChips, rightChips);
  };
  const renderTable = () => {
    const rows = agents.filter((a) => (filters.status === 'all' || a.status === filters.status) && (filters.platform === 'all' || a.platform === filters.platform) && (filters.project === 'all' || a.project === filters.project));
    mount(tableHost, dataTable({
      columns: [
        { key: 'name', label: t('Agent'), render: (a) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main truncate', title: a.name }, a.name || a.id), h('span', { class: 'cell-sub truncate', title: a.description || '' }, a.description || a.id)) },
        { key: 'platform', label: t('Platform'), render: (a) => h('div', { class: 'cell-stack' }, h('span', null, t(PLATFORM_LABELS[a.platform] || a.platform)), h('span', { class: 'cell-sub' }, a.type || '')) },
        { key: 'project', label: t('Project'), render: (a) => h('span', { class: 'cell-mono' }, a.project || DASH) },
        { key: 'region', label: t('Region'), render: (a) => h('span', { class: 'cell-mono' }, a.region || DASH) },
        { key: 'owner', label: t('Owner'), render: (a) => h('span', { class: 'truncate', title: a.owner || '' }, a.owner || DASH) },
        { key: 'calls', label: t('Calls'), align: 'num', format: fmt.int },
        { key: 'lastActivity', label: t('Last activity'), render: (a) => (a.lastActivity ? fmt.relative(a.lastActivity) : DASH), value: (a) => a.lastActivity },
        { key: 'daysIdle', label: t('Idle days'), align: 'num', format: fmt.int },
        { key: 'status', label: t('Status'), render: (a) => statusBadge(a.status, { size: 'sm' }) },
      ],
      rows: d.agents === null ? null : rows, pageSize: 12, exportName: 'agent-registry', initialSort: { key: 'daysIdle', dir: 'desc' },
      emptyText: t('No agents match the selected filters.'),
      onRowClick: (a) => openAgent(a, th),
    }));
  };
  renderChips();
  renderTable();
  body.append(card({
    title: t('Agent catalog'), subtitle: d.agents === null ? t('Data source unavailable') : t('{n} agents', { n: agents.length }), flush: true,
    body: h('div', null, d.agents === null ? null : h('div', { class: 'table-toolbar' }, h('div', { class: 'left' }, statusChips), h('div', { class: 'right' }, platformChips)), tableHost),
    source: 'Agent Platform reasoningEngines, Discovery Engine agents, activity logs',
  }));

  const row = h('div', { class: 'grid' });
  const trend = chartCard({
    title: t('Agent creation'), subtitle: rangeText(), source: 'cloudaudit_googleapis_com_activity',
    build: (host) => barChart(host, { ...fromTimeline(d.creationTrend, { names: { created: t('Agents created') }, colors: { created: TONES.violet } }), integer: true, height: 230 }),
  });
  trend.classList.add('span-7');
  const creators = card({
    title: t('Top creators'), subtitle: t('Create operations recorded in audit logs'), flush: true,
    body: dataTable({
      columns: [
        { key: 'email', label: t('Creator'), render: (r) => userCell(r.email) },
        { key: 'created', label: t('Created'), align: 'num', format: fmt.int },
        { key: 'lastCreated', label: t('Last created'), render: (r) => (r.lastCreated ? fmt.relative(r.lastCreated) : DASH), value: (r) => r.lastCreated },
      ],
      rows: d.creators, searchable: false, pageSize: 6, initialSort: { key: 'created', dir: 'desc' },
      emptyText: t('No agent creation events in this period.'),
    }),
  });
  creators.classList.add('span-5');
  body.append(row);
  mountCharts(row, [trend, creators]);
}

function openAgent(a, th) {
  openDrawer({
    title: a.name || a.id,
    subtitle: t(PLATFORM_LABELS[a.platform] || a.platform),
    width: 680,
    content: (body) => {
      body.append(
        h('div', { class: 'row wrap' }, statusBadge(a.status), a.project ? badge(a.project, 'neutral') : null, a.type ? badge(a.type, 'neutral') : null, a.region ? badge(a.region, 'neutral') : null),
        a.description ? h('p', { class: 'muted' }, a.description) : null,
        card({
          title: t('Lifecycle'),
          body: kv([
            [t('Last activity'), a.lastActivity ? `${fmt.dateTime(a.lastActivity)} (${fmt.relative(a.lastActivity)})` : null],
            [t('Idle days'), fmt.int(a.daysIdle)],
            [t('Calls in period'), fmt.int(a.calls)],
            [t('Idle threshold'), t('{n} days', { n: th.idleDays })],
            [t('Deprecation threshold'), t('{n} days', { n: th.deprecationDays })],
          ]),
        }),
        card({
          title: t('Registration'),
          body: kv([
            [t('Project'), a.project ? h('span', { class: 'mono' }, a.project) : null],
            [t('Agent ID'), h('span', { class: 'mono' }, a.id)],
            [t('Resource name'), a.resourceName ? h('span', { class: 'mono' }, a.resourceName) : null],
            [t('Owner'), a.owner],
            [t('Identity'), a.identity ? h('span', { class: 'mono' }, a.identity) : null],
            [t('Created'), a.createTime ? fmt.dateTime(a.createTime) : null],
            [t('Updated'), a.updateTime ? fmt.dateTime(a.updateTime) : null],
          ]),
        }),
        a.status === 'deprecation_candidate' ? banner({ tone: 'warning', title: t('Recommended action'), text: t('Confirm the owner still needs this agent. If not, delete the deployment and revoke its service identity.') }) : null);
    },
  });
}
