/**
 * Gemini Enterprise: adoption, licensing, agent usage and content governance.
 */
import { h, append } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api } from '../core/api.js';
import { pageHeader, kpi, card, dataTable, badge, userCell, emptyState, load, progress, stat, banner } from '../core/components.js';
import { lineChart, barChart, fromTimeline, hbarList, TONES, colorAt } from '../core/charts.js';
import { pageFlow, fetchRange, rangeText, deltaOf, chartCard, mountCharts, openUserDrawer } from './_shared.js';

const CATEGORY_LABELS = {
  document: 'Document', spreadsheet: 'Spreadsheet', presentation: 'Presentation', image: 'Image', text: 'Text', other: 'Other',
};

export default async function render(ctx) {
  const header = pageHeader({
    title: t('Gemini Enterprise'),
    desc: t('Adoption, license utilization, agent usage and uploaded content across Gemini Enterprise.'),
  });
  return pageFlow(ctx, {
    header,
    kpiCount: 5,
    fetcher: () => fetchRange(ctx, '/api/v1/gemini-enterprise/summary'),
    render: (body, d) => renderPage(ctx, body, d),
  });
}

function renderPage(ctx, body, d) {
  const k = d.kpis || {};
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Active users'), value: fmt.int(k.activeUsers && k.activeUsers.value), delta: deltaOf(k.activeUsers), tip: t('Distinct people with prompts or agent activity in the period.') }),
    kpi({ label: t('Prompts'), value: fmt.int(k.prompts && k.prompts.value), delta: deltaOf(k.prompts), spark: (d.activity && d.activity.series && (d.activity.series.find((s) => s.key === 'prompts') || {}).values) || [], sparkColor: TONES.info, tip: t('Messages people sent to the assistant. Automatic session summaries and scheduled runs are not counted.') }),
    kpi({ label: t('Prompts per user'), value: fmt.num(k.promptsPerUser, 1), tip: t('Average prompts submitted per active user.') }),
    kpi({ label: t('Agent calls'), value: fmt.int(k.agentCalls && k.agentCalls.value), delta: deltaOf(k.agentCalls), tip: t('Assistant requests addressed to a specific agent, including scheduled runs.') }),
    kpi({ label: t('Files uploaded'), value: fmt.int(k.fileUploads && k.fileUploads.value), delta: deltaOf(k.fileUploads), goodWhen: null, tip: t('Completed file uploads into assistant sessions.') })));

  const row1 = h('div', { class: 'grid' });
  const promptsCard = chartCard({
    title: t('Prompts'), subtitle: rangeText(), source: 'gen_ai_user_message, gemini_enterprise_user_activity',
    build: (host) => barChart(host, { ...fromTimeline(d.activity, { include: ['prompts'], names: { prompts: t('Prompts') }, colors: { prompts: TONES.info } }), integer: true, height: 230 }),
  });
  promptsCard.classList.add('span-6');
  const usersCard = chartCard({
    title: t('Daily active users'), subtitle: rangeText(), source: 'gen_ai_user_message, gemini_enterprise_user_activity',
    build: (host) => lineChart(host, { ...fromTimeline(d.activity, { include: ['activeUsers'], names: { activeUsers: t('Active users') }, colors: { activeUsers: TONES.teal } }), integer: true, height: 230 }),
  });
  usersCard.classList.add('span-6');
  body.append(row1);
  mountCharts(row1, [promptsCard, usersCard]);

  const agentSeries = (d.agentUsage && d.agentUsage.series) || [];
  const row2 = h('div', { class: 'grid' });
  const agentCard = chartCard({
    title: t('Agent usage'), subtitle: t('Calls per agent \u00b7 {range}', { range: rangeText() }), source: 'discoveryengine_googleapis_com_gemini_enterprise_user_activity',
    build: (host) => barChart(host, { ...fromTimeline(d.agentUsage), stacked: true, integer: true, height: 260 }),
  });
  agentCard.classList.add('span-8');
  const shareCard = card({
    title: t('Agent share'), subtitle: t('Total calls in period'),
    body: hbarList(!d.agentUsage ? null : agentSeries.map((s, i) => ({ label: s.name, value: isNum(s.total) ? s.total : (s.values || []).reduce((a, b) => a + (b || 0), 0), color: colorAt(i) })).sort((a, b) => b.value - a.value)),
  });
  shareCard.classList.add('span-4');
  body.append(row2);
  mountCharts(row2, [agentCard, shareCard]);

  const row3 = h('div', { class: 'grid' });
  const licHost = h('div');
  const licCard = card({ title: t('License utilization'), subtitle: t('Gemini Enterprise license configurations and seat assignment'), body: licHost, source: 'Discovery Engine licenseConfigs, userLicenses' });
  licCard.classList.add('span-12');
  row3.append(licCard);
  body.append(row3);
  load(licHost, () => api.get('/api/v1/gemini-enterprise/licenses', null, { signal: ctx.signal }), (res) => renderLicenses(licHost, res.data), { skeletonKind: 'lines', signal: ctx.signal });

  body.append(card({
    title: t('Top users'), subtitle: rangeText(), flush: true,
    body: dataTable({
      columns: [
        { key: 'email', label: t('User'), render: (r) => userCell(r.email) },
        { key: 'prompts', label: t('Prompts'), align: 'num', format: fmt.int },
        { key: 'agentCalls', label: t('Agent calls'), align: 'num', format: fmt.int },
        { key: 'fileUploads', label: t('Files'), align: 'num', format: fmt.int },
        { key: 'lastActive', label: t('Last active'), render: (r) => (r.lastActive ? fmt.relative(r.lastActive) : DASH), value: (r) => r.lastActive },
      ],
      rows: d.topUsers, pageSize: 10, exportName: 'gemini-enterprise-top-users', initialSort: { key: 'prompts', dir: 'desc' },
      onRowClick: (r) => r.uid && openUserDrawer(r.uid),
    }),
  }));

  const filesHost = h('div');
  body.append(card({ title: t('Uploaded files'), subtitle: t('Files attached to Gemini Enterprise conversations'), flush: true, body: filesHost, source: 'gen_ai_user_message, gemini_enterprise_user_activity' }));
  load(filesHost, () => fetchRange(ctx, '/api/v1/gemini-enterprise/file-uploads'), (res) => {
    const items = res.data.items || [];
    append(filesHost, [dataTable({
      columns: [
        { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time), value: (r) => r.time },
        { key: 'email', label: t('User'), render: (r) => userCell(r.email) },
        { key: 'fileName', label: t('File'), render: (r) => (r.restricted ? h('span', { class: 'muted' }, t('Restricted')) : (r.fileName ? h('span', { class: 'truncate cell-main', title: r.fileName }, r.fileName) : (r.fileId ? h('span', { class: 'cell-mono text-muted', title: r.fileId }, `file_${r.fileId}`) : h('span', { class: 'text-muted' }, t('Name not recorded'))))) },
        { key: 'category', label: t('Category'), render: (r) => badge(t(CATEGORY_LABELS[r.category] || r.category || 'Other'), 'neutral', { size: 'sm' }) },
        { key: 'mimeType', label: t('MIME type'), render: (r) => h('span', { class: 'cell-mono truncate', title: r.mimeType || '' }, r.mimeType || DASH) },
        { key: 'reviewRecommended', label: t('Review'), render: (r) => (r.reviewRecommended ? badge(t('Review recommended'), 'warning', { size: 'sm', dot: true }) : badge(t('Standard'), 'neutral', { size: 'sm' })), value: (r) => (r.reviewRecommended ? 1 : 0) },
      ],
      rows: res.data.items === null ? null : items, pageSize: 10, exportName: 'gemini-enterprise-file-uploads',
      emptyText: t('No files were uploaded in this period.'),
      onRowClick: (r) => r.uid && openUserDrawer(r.uid),
    })]);
  }, { skeletonKind: 'table', signal: ctx.signal });
  return null;
}

function renderLicenses(host, d) {
  const purchased = d.purchased;
  const assigned = d.assigned;
  const util = isNum(purchased) && purchased > 0 && isNum(assigned) ? assigned / purchased : null;
  const tone = util === null ? '' : util > 0.95 ? 'danger' : util > 0.8 ? 'warning' : 'success';
  // Seats are distributed per location; show where each row lives when more than one location is read.
  const byLocation = Array.isArray(d.locations) && d.locations.length > 1
    ? [{ key: 'location', label: t('Location'), render: (r) => h('span', { class: 'cell-mono' }, r.location || DASH) }]
    : [];
  append(host, [h('div', { class: 'stack' },
    h('div', { class: 'stat-row' },
      stat(t('Purchased seats'), fmt.int(purchased)),
      stat(t('Assigned'), fmt.int(assigned), util !== null ? t('{p} utilization', { p: fmt.pct(util) }) : null),
      stat(t('Unassigned'), fmt.int(d.unassigned)),
      stat(t('Idle assigned seats'), fmt.int(d.idleAssigned), t('No sign-in for {n}+ days', { n: d.idleThresholdDays || 30 }))),
    util !== null ? progress(util, tone) : null,
    d.userLicensesAvailable === false ? banner({ tone: 'neutral', title: t('Seat-level data unavailable'), text: t('The userLicenses API did not return data. Seat totals are based on license configurations only.') }) : null,
    (d.configs && d.configs.length) ? dataTable({
      columns: [
        { key: 'displayName', label: t('Configuration'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main' }, r.displayName || r.name.split('/').pop()), h('span', { class: 'cell-sub cell-mono' }, r.name.split('/').pop())) },
        ...byLocation,
        { key: 'subscriptionTier', label: t('Tier'), render: (r) => r.subscriptionTier || DASH },
        { key: 'state', label: t('State'), render: (r) => badge(r.state || DASH, r.state === 'ACTIVE' ? 'success' : 'neutral', { size: 'sm', dot: true }) },
        { key: 'licenseCount', label: t('Seats'), align: 'num', format: fmt.int },
        { key: 'startDate', label: t('Term'), render: (r) => (r.startDate ? `${fmt.date(r.startDate, 'long')} \u2013 ${r.endDate ? fmt.date(r.endDate, 'long') : t('Open')}` : DASH) },
      ],
      rows: d.configs, searchable: false, pageSize: 5,
    }) : emptyState({ icon: 'key', title: t('No license configurations'), body: t('No Gemini Enterprise license configuration was returned for this project.'), compact: true }),
    (d.users && d.users.length) ? dataTable({
      title: t('Seat assignments'),
      columns: [
        { key: 'email', label: t('User'), render: (r) => userCell(r.email) },
        ...byLocation,
        { key: 'state', label: t('Assignment'), render: (r) => badge(r.state || DASH, r.state === 'ASSIGNED' ? 'success' : 'neutral', { size: 'sm' }) },
        { key: 'lastLogin', label: t('Last sign-in'), render: (r) => (r.lastLogin ? fmt.dateTime(r.lastLogin) : t('Never')), value: (r) => r.lastLogin || '' },
        { key: 'idleDays', label: t('Idle days'), align: 'num', render: (r) => (isNum(r.idleDays) ? h('span', { class: r.idleDays >= (d.idleThresholdDays || 30) ? 'text-warning fw-600' : '' }, fmt.int(r.idleDays)) : DASH), value: (r) => r.idleDays },
      ],
      rows: d.users, pageSize: 8, exportName: 'gemini-enterprise-seats', initialSort: { key: 'idleDays', dir: 'desc' },
    }) : null)]);
}
