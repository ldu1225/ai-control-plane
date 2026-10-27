/**
 * NotebookLM: notebooks, active users and prompts from Cloud Audit Logs.
 */
import { h } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { fmt, DASH } from '../core/format.js';
import { pageHeader, kpi, card, dataTable, userCell } from '../core/components.js';
import { lineChart, barChart, fromTimeline, TONES } from '../core/charts.js';
import { pageFlow, fetchRange, rangeText, deltaOf, chartCard, mountCharts, openUserDrawer } from './_shared.js';

export default async function render(ctx) {
  const header = pageHeader({
    title: t('NotebookLM'),
    desc: t('Notebook activity and prompt volume for NotebookLM Enterprise, derived from data access audit logs.'),
  });
  return pageFlow(ctx, {
    header,
    kpiCount: 3,
    fetcher: () => fetchRange(ctx, '/api/v1/notebooklm/summary'),
    render: (body, d) => {
      const k = d.kpis || {};
      const series = (key) => ((d.activity && d.activity.series) || []).find((s) => s.key === key);
      body.append(h('div', { class: 'kpi-grid' },
        kpi({ label: t('Active notebooks'), value: fmt.int(k.notebooks && k.notebooks.value), delta: deltaOf(k.notebooks), tip: t('Distinct notebooks with activity in the period.') }),
        kpi({ label: t('Active users'), value: fmt.int(k.activeUsers && k.activeUsers.value), delta: deltaOf(k.activeUsers), spark: (series('activeUsers') || {}).values, sparkColor: TONES.teal }),
        kpi({ label: t('Prompts'), value: fmt.int(k.prompts && k.prompts.value), delta: deltaOf(k.prompts), spark: (series('prompts') || {}).values, sparkColor: TONES.violet, tip: t('Questions submitted to notebooks, de-duplicated across streaming calls.') })));
      const row = h('div', { class: 'grid' });
      const c1 = chartCard({
        title: t('Prompts'), subtitle: rangeText(), source: 'cloudaudit_googleapis_com_data_access',
        build: (host) => barChart(host, { ...fromTimeline(d.activity, { include: ['prompts'], names: { prompts: t('Prompts') }, colors: { prompts: TONES.violet } }), integer: true, height: 240 }),
      });
      c1.classList.add('span-7');
      const c2 = chartCard({
        title: t('Active users'), subtitle: rangeText(), source: 'cloudaudit_googleapis_com_data_access',
        build: (host) => lineChart(host, { ...fromTimeline(d.activity, { include: ['activeUsers'], names: { activeUsers: t('Active users') }, colors: { activeUsers: TONES.teal } }), integer: true, height: 240 }),
      });
      c2.classList.add('span-5');
      body.append(row);
      mountCharts(row, [c1, c2]);
      body.append(card({
        title: t('Top users'), subtitle: rangeText(), flush: true,
        body: dataTable({
          columns: [
            { key: 'email', label: t('User'), render: (r) => userCell(r.email) },
            { key: 'prompts', label: t('Prompts'), align: 'num', format: fmt.int },
            { key: 'notebooks', label: t('Notebooks'), align: 'num', format: fmt.int },
            { key: 'lastActive', label: t('Last active'), render: (r) => (r.lastActive ? fmt.relative(r.lastActive) : DASH), value: (r) => r.lastActive },
          ],
          rows: d.topUsers, pageSize: 10, exportName: 'notebooklm-users', initialSort: { key: 'prompts', dir: 'desc' },
          onRowClick: (r) => r.uid && openUserDrawer(r.uid),
        }),
      }));
      return null;
    },
  });
}
