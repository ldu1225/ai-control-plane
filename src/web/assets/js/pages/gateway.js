/**
 * Agent Gateway: request volume, authentication outcomes and routing across AI services.
 */
import { h } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { pageHeader, kpi, card, dataTable, badge, userCell } from '../core/components.js';
import { barChart, donutChart, hbarList, mapList, fromTimeline, TONES } from '../core/charts.js';
import { pageFlow, fetchRange, rangeText, deltaOf, chartCard, mountCharts, openUserDrawer } from './_shared.js';

const OUTCOMES = {
  success: ['Success', TONES.success],
  client_error: ['Client error', TONES.warning],
  denied: ['Denied', TONES.danger],
  throttled: ['Throttled', TONES.violet],
  server_error: ['Server error', TONES.slate],
};

const OUTCOME_TONE = { success: 'success', client_error: 'warning', denied: 'danger', throttled: 'info', server_error: 'neutral' };

export default async function render(ctx) {
  const header = pageHeader({
    title: t('Agent Gateway'),
    desc: t('Inbound traffic to AI services: who is calling, which methods are routed, and how requests are authorized, throttled or denied.'),
  });
  return pageFlow(ctx, {
    header,
    kpiCount: 5,
    fetcher: () => fetchRange(ctx, '/api/v1/agent-platform/gateway'),
    render: (body, d) => renderPage(body, d),
  });
}

function shortMethod(m) {
  if (!m) return DASH;
  const parts = String(m).split('.');
  return parts.length > 2 ? parts.slice(-2).join('.') : m;
}

function renderPage(body, d) {
  const k = d.kpis || {};
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Requests'), value: fmt.compact(k.requests && k.requests.value), delta: deltaOf(k.requests), goodWhen: null, tip: t('Audit-logged calls to the configured AI services.') }),
    kpi({ label: t('Calling principals'), value: fmt.int(k.principals && k.principals.value), delta: deltaOf(k.principals), goodWhen: null, meta: h('span', null, t('{u} users \u00b7 {s} service accounts', { u: fmt.int(k.humanPrincipals), s: fmt.int(k.servicePrincipals) })) }),
    kpi({ label: t('Error rate'), value: fmt.pct(k.errorRate, 2), tip: t('Share of requests that did not complete successfully.') }),
    kpi({ label: t('Denied'), value: fmt.int(k.denied), tip: t('PERMISSION_DENIED and UNAUTHENTICATED responses.') }),
    kpi({ label: t('Throttled'), value: fmt.int(k.throttled), tip: t('RESOURCE_EXHAUSTED (quota or rate limit) responses.') })));

  const row1 = h('div', { class: 'grid' });
  const names = {};
  const colors = {};
  Object.entries(OUTCOMES).forEach(([key, [label, color]]) => { names[key] = t(label); colors[key] = color; });
  const trend = chartCard({
    title: t('Request outcomes'), subtitle: rangeText(), source: 'cloudaudit data_access + activity',
    build: (host) => barChart(host, { ...fromTimeline(d.trend, { names, colors }), stacked: true, height: 260, valueFormat: fmt.int }),
  });
  trend.classList.add('span-8');
  const principals = chartCard({
    title: t('Principal types'), subtitle: t('Distinct callers in period'),
    build: (host) => donutChart(host, {
      items: [
        { label: t('Users'), value: k.humanPrincipals, color: TONES.info },
        { label: t('Service accounts'), value: k.servicePrincipals, color: TONES.violet },
      ],
      centerValue: fmt.int(k.principals && k.principals.value), centerLabel: t('principals'), stacked: true, size: 160,
    }),
  });
  principals.classList.add('span-4');
  body.append(row1);
  mountCharts(row1, [trend, principals]);

  const row2 = h('div', { class: 'grid' });
  const svc = card({
    title: t('Requests by service'), subtitle: rangeText(),
    body: hbarList(mapList(d.byService, (s) => ({ label: s.service, value: s.requests, sub: s.errors ? t('{n} errors', { n: fmt.int(s.errors) }) : null })), { format: fmt.compact }),
  });
  svc.classList.add('span-5');
  const methods = card({
    title: t('Top methods'), subtitle: t('Most frequently routed API methods'), flush: true,
    body: dataTable({
      columns: [
        { key: 'method', label: t('Method'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main cell-mono', title: r.method }, shortMethod(r.method)), h('span', { class: 'cell-sub' }, r.service)) },
        { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
        { key: 'errors', label: t('Errors'), align: 'num', render: (r) => (r.errors ? h('span', { class: 'text-danger fw-600' }, fmt.int(r.errors)) : fmt.int(0)), value: (r) => r.errors },
      ],
      rows: d.byMethod, searchable: false, pageSize: 8, initialSort: { key: 'requests', dir: 'desc' },
    }),
  });
  methods.classList.add('span-7');
  row2.append(svc, methods);
  body.append(row2);

  const row3 = h('div', { class: 'grid' });
  const callers = card({
    title: t('Top callers'), subtitle: rangeText(), flush: true,
    body: dataTable({
      columns: [
        { key: 'principal', label: t('Principal'), render: (r) => userCell(r.principal, r.type === 'service_account' ? t('Service account') : t('User')) },
        { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
        { key: 'errors', label: t('Errors'), align: 'num', format: fmt.int },
        { key: 'lastSeen', label: t('Last seen'), render: (r) => (r.lastSeen ? fmt.relative(r.lastSeen) : DASH), value: (r) => r.lastSeen },
      ],
      rows: d.topPrincipals, pageSize: 8, exportName: 'gateway-callers', initialSort: { key: 'requests', dir: 'desc' },
      onRowClick: (r) => (r.type === 'user' && r.uid ? openUserDrawer(r.uid) : null),
    }),
  });
  callers.classList.add('span-7');
  const modelsCard = card({
    title: t('Model invocations'), subtitle: t('Reported by Cloud Monitoring'), flush: true, source: 'publisher/online_serving/model_invocation_count',
    body: dataTable({
      columns: [
        { key: 'model', label: t('Model'), render: (r) => h('span', { class: 'cell-mono cell-main' }, r.model) },
        { key: 'invocations', label: t('Invocations'), align: 'num', format: fmt.compact },
        { key: 'errorRate', label: t('Error rate'), align: 'num', render: (r) => (isNum(r.errorRate) ? h('span', { class: r.errorRate > 0.02 ? 'text-danger fw-600' : '' }, fmt.pct(r.errorRate, 2)) : DASH), value: (r) => r.errorRate },
      ],
      rows: d.modelInvocations, searchable: false, pageSize: 8, initialSort: { key: 'invocations', dir: 'desc' },
      emptyText: t('No publisher model invocations were reported in this period.'),
    }),
  });
  modelsCard.classList.add('span-5');
  row3.append(callers, modelsCard);
  body.append(row3);

  body.append(card({
    title: t('Recent failed requests'), subtitle: t('Denied, throttled and failed calls (latest 50)'), flush: true,
    body: dataTable({
      columns: [
        { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time, true), value: (r) => r.time },
        { key: 'principal', label: t('Principal'), render: (r) => h('span', { class: 'truncate', title: r.principal || '' }, r.principal || DASH) },
        { key: 'method', label: t('Method'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-mono', title: r.method }, shortMethod(r.method)), h('span', { class: 'cell-sub' }, r.service)) },
        { key: 'outcome', label: t('Outcome'), render: (r) => badge(t((OUTCOMES[r.outcome] || [r.outcome])[0]), OUTCOME_TONE[r.outcome] || 'neutral', { size: 'sm', dot: true }) },
        { key: 'code', label: t('Code'), align: 'num' },
        { key: 'message', label: t('Message'), render: (r) => h('span', { class: 'truncate', title: r.message || '' }, r.message || DASH) },
        { key: 'callerIp', label: t('Caller IP'), render: (r) => h('span', { class: 'cell-mono' }, r.callerIp || DASH) },
      ],
      rows: d.recentFailures, pageSize: 10, exportName: 'gateway-failures',
      emptyText: t('No failed requests in this period.'),
    }),
  }));
}
