/**
 * Agent Deployments: runtime inventory, identities and per-identity model usage.
 */
import { h } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { pageHeader, kpi, card, dataTable, badge, banner, openDrawer, kv, principalCell } from '../core/components.js';
import { hbarList } from '../core/charts.js';
import { pageFlow, fetchRange, deltaOf, rangeText } from './_shared.js';

export default async function render(ctx) {
  const header = pageHeader({
    title: t('Deployments'),
    desc: t('Agent Engine runtimes, the identities they run as, invocation volume and the models each identity calls.'),
  });
  return pageFlow(ctx, {
    header,
    kpiCount: 5,
    fetcher: () => fetchRange(ctx, '/api/v1/agent-platform/deployments'),
    render: (body, d) => renderPage(body, d),
  });
}

function renderPage(body, d) {
  const k = d.kpis || {};
  const deployments = d.deployments || [];
  const ccy = (deployments.find((x) => x.currency) || {}).currency || 'USD';
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Deployments'), value: fmt.int(k.deployments) }),
    kpi({ label: t('Regions'), value: fmt.int(k.regions) }),
    kpi({ label: t('Runtime identities'), value: fmt.int(k.identities), tip: t('Distinct service accounts or agent identities used by deployments.') }),
    kpi({ label: t('Invocations'), value: fmt.int(k.invocations && k.invocations.value), delta: deltaOf(k.invocations), goodWhen: null, tip: t('ReasoningEngineExecutionService calls recorded in data access audit logs.') }),
    kpi({ label: t('Runtime cost'), value: fmt.currency(k.runtimeCost, ccy), tip: t('Billing export cost attributed to Agent Engine resources (resource-level export required).') })));

  body.append(card({
    title: t('Agent Engine deployments'), subtitle: rangeText(), flush: true, source: 'Agent Platform reasoningEngines + audit logs + billing export',
    body: dataTable({
      columns: [
        { key: 'name', label: t('Deployment'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main truncate', title: r.name }, r.name || r.id), h('span', { class: 'cell-sub cell-mono' }, r.id)) },
        { key: 'project', label: t('Project'), render: (r) => h('span', { class: 'cell-mono' }, r.project || DASH) },
        { key: 'region', label: t('Region'), render: (r) => h('span', { class: 'cell-mono' }, r.region || DASH) },
        { key: 'framework', label: t('Runtime'), render: (r) => h('div', { class: 'cell-stack' }, h('span', null, r.framework || DASH), h('span', { class: 'cell-sub' }, r.pythonVersion ? `Python ${r.pythonVersion}` : '')) },
        { key: 'identity', label: t('Identity'), render: (r) => principalCell(r.identity) },
        { key: 'invocations', label: t('Invocations'), align: 'num', format: fmt.int },
        { key: 'callers', label: t('Callers'), align: 'num', format: fmt.int },
        { key: 'errors', label: t('Errors'), align: 'num', render: (r) => (r.errors ? h('span', { class: 'text-danger fw-600' }, fmt.int(r.errors)) : fmt.int(r.errors || 0)), value: (r) => r.errors },
        { key: 'runtimeCost', label: t('Runtime cost'), align: 'num', render: (r) => fmt.currency(r.runtimeCost, r.currency || ccy), value: (r) => r.runtimeCost },
        { key: 'updateTime', label: t('Updated'), render: (r) => (r.updateTime ? fmt.relative(r.updateTime) : DASH), value: (r) => r.updateTime },
      ],
      rows: d.deployments === null ? null : deployments, pageSize: 10, exportName: 'agent-deployments', initialSort: { key: 'invocations', dir: 'desc' },
      emptyText: t('No Agent Engine deployments were found in the configured regions.'),
      onRowClick: (r) => openDeployment(r, ccy),
    }),
  }));

  const usage = d.identityUsage || [];
  const byModel = {};
  usage.forEach((u) => { byModel[u.model] = (byModel[u.model] || 0) + (u.calls || 0); });
  const row = h('div', { class: 'grid' });
  const usageCard = card({
    title: t('Model usage by identity'), subtitle: rangeText(), flush: true, source: 'cloudaudit data_access (aiplatform.googleapis.com)',
    body: h('div', null,
      d.attributionNote ? h('div', { class: 'card-body flush-top' }, banner({ tone: 'neutral', iconName: 'info', title: t('Attribution method'), text: t(d.attributionNote) })) : null,
      dataTable({
        columns: [
          { key: 'principal', label: t('Identity'), render: (r) => principalCell(r.principal, r.type === 'service_account' ? t('Service account') : t('User')) },
          { key: 'model', label: t('Model'), render: (r) => h('span', { class: 'cell-mono' }, r.model || DASH) },
          { key: 'calls', label: t('Calls'), align: 'num', format: fmt.int },
          { key: 'allocatedTokens', label: t('Allocated tokens'), align: 'num', render: (r) => h('span', null, fmt.compact(r.allocatedTokens), ' ', badge(t('est.'), 'neutral', { size: 'sm' })), value: (r) => r.allocatedTokens, csv: (r) => r.allocatedTokens },
          { key: 'allocatedCost', label: t('Allocated cost'), align: 'num', render: (r) => fmt.currency(r.allocatedCost, ccy, { precise: true }), value: (r) => r.allocatedCost },
        ],
        rows: d.identityUsage === null ? null : usage, pageSize: 10, exportName: 'identity-model-usage', initialSort: { key: 'calls', dir: 'desc' },
        emptyText: t('No model calls from identities were recorded in this period.'),
      })),
  });
  usageCard.classList.add('span-8');
  const modelCard = card({
    title: t('Calls by model'), subtitle: t('All identities'),
    body: hbarList(d.identityUsage === null ? null : Object.entries(byModel).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([label, value]) => ({ label, value }))),
  });
  modelCard.classList.add('span-4');
  row.append(usageCard, modelCard);
  body.append(row);
}

function openDeployment(r, ccy) {
  openDrawer({
    title: r.name || r.id,
    subtitle: t('Agent Engine deployment'),
    width: 640,
    content: (body) => body.append(
      card({
        title: t('Runtime'),
        body: kv([
          [t('Project'), r.project ? h('span', { class: 'mono' }, r.project) : null],
          [t('Resource'), h('span', { class: 'mono' }, r.id)],
          [t('Region'), r.region],
          [t('Framework'), r.framework],
          [t('Python'), r.pythonVersion],
          [t('Identity'), r.identity ? h('span', { class: 'mono' }, r.identity) : null],
          [t('Min instances'), isNum(r.minInstances) ? fmt.int(r.minInstances) : null],
          [t('Max instances'), isNum(r.maxInstances) ? fmt.int(r.maxInstances) : null],
          [t('Created'), r.createTime ? fmt.dateTime(r.createTime) : null],
          [t('Updated'), r.updateTime ? fmt.dateTime(r.updateTime) : null],
        ]),
      }),
      card({
        title: t('Activity in period'),
        body: kv([
          [t('Invocations'), fmt.int(r.invocations)],
          [t('Distinct callers'), fmt.int(r.callers)],
          [t('Errors'), fmt.int(r.errors)],
          [t('Runtime cost'), fmt.currency(r.runtimeCost, r.currency || ccy)],
        ]),
      })),
  });
}
