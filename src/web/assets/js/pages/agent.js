/**
 * Agent details: one Agent Engine deployment with its telemetry, models and tools, traces, topology,
 * revisions, evaluation and a playground.
 *
 * Address: #/deployments/<project>/<region>/<engine>[/<view>]. Opened from the Deployments table and from the
 * Agent Engine entries of the Agent Registry. Every view is a page render of its own, so it can be linked to.
 */
import { h, s, mount, append } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api, isAbort } from '../core/api.js';
import { rangeParams, isAdmin } from '../core/state.js';
import {
  card, kpi, dataTable, badge, banner, button, kv, errorState, skeleton, warningsNotice, openDrawer, principalCell,
  emptyState, markdownLite, codeBlock, listCell, stat,
} from '../core/components.js';
import { errorText } from '../core/messages.js';
import { lineChart, barChart, hbarList, fromTimeline, TONES, colorAt } from '../core/charts.js';
import { rangeText, chartCard, mountCharts, deltaOf, val, pageSkeleton } from './_shared.js';

const API = '/api/v1/agent-platform/agent';
const TAB_IDS = ['telemetry', 'models', 'traces', 'topology', 'revisions', 'evaluation', 'playground'];
const KIND_COLORS = { agent: TONES.info, tool: TONES.teal, model: TONES.violet, workflow: TONES.slate, span: TONES.neutral, client: TONES.slate };

/** Playground conversations survive page re-renders (tab and date range changes); they are cleared on reload. */
const conversations = new Map();

/** Set when a tab link is followed: the page is drawn again, and keyboard focus returns to the tab strip. */
let keepTabFocus = false;

export function agentHash(project, region, engine, tab) {
  const parts = [project, region, engine].map((p) => encodeURIComponent(p));
  if (tab) parts.push(tab);
  return `#/deployments/${parts.join('/')}`;
}

function tabLabel(id) {
  switch (id) {
    case 'telemetry': return t('Telemetry');
    case 'models': return t('Models and tools');
    case 'traces': return t('Traces');
    case 'topology': return t('Topology');
    case 'revisions': return t('Revisions');
    case 'evaluation': return t('Evaluation');
    default: return t('Playground');
  }
}

function kindLabel(kind) {
  switch (kind) {
    case 'agent': return t('Agent');
    case 'tool': return t('Tool');
    case 'model': return t('Model');
    case 'workflow': return t('Workflow');
    case 'client': return t('Client');
    default: return t('Span');
  }
}

/** Kind of a span or graph node, with the color the call graph and the trace timeline use for that kind. */
function kindBadge(kind) {
  const dot = h('span', { class: ['swatch', 'round'] });
  dot.style.setProperty('background', KIND_COLORS[kind] || TONES.neutral);
  return h('span', { class: ['badge', 'badge-neutral', 'sm'] }, dot, kindLabel(kind));
}

function ms(v) {
  return isNum(v) ? fmt.duration(v) : DASH;
}

export default async function render(ctx) {
  const [project, region, engine] = ctx.params;
  const tab = TAB_IDS.includes(ctx.params[3]) ? ctx.params[3] : 'telemetry';
  const st = { ctx, tab, agentParams: { project, region, engine }, agent: null };
  const heading = h('div', { class: 'agent-heading' });
  const tabs = tabStrip(st);
  const panel = h('div', { class: 'stack-lg' });
  mount(ctx.root, h('div', { class: 'agent-head' }, heading, tabs), panel);
  renderHeading(heading, st, null);
  if (keepTabFocus) {
    keepTabFocus = false;
    const active = tabs.querySelector('[aria-current="page"]');
    if (active) active.focus({ preventScroll: true });
  }
  mount(panel, pageSkeleton(4, 2));

  const view = VIEWS[tab];
  // The tab's data is requested alongside the agent summary; both answers are needed before anything is drawn.
  const pending = view.fetch ? view.fetch(st).then((res) => ({ res }), (err) => ({ err })) : Promise.resolve({});
  try {
    const res = await api.get(API, st.agentParams, { signal: ctx.signal });
    st.agent = res.data.agent;
  } catch (err) {
    if (isAbort(err) || ctx.signal.aborted) return null;
    mount(panel, card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    return null;
  }
  if (ctx.signal.aborted) return null;
  renderHeading(heading, st, st.agent);
  const { res, err } = await pending;
  if (ctx.signal.aborted) return null;
  if (err) {
    if (!isAbort(err)) mount(panel, card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    return null;
  }
  mount(panel);
  const notice = res ? warningsNotice(res.meta && res.meta.warnings) : null;
  if (notice) panel.append(notice);
  return view.render(panel, res ? res.data : null, st) || null;
}

/* ------------------------------------------------------------------ header */

function fact(label, value) {
  return h('div', { class: 'agent-fact' }, h('span', { class: 'agent-fact-label' }, label), h('span', { class: 'agent-fact-value' }, value));
}

/**
 * Views of the agent as links: every view has its own address, so this is navigation (aria-current marks the
 * open view), not a tab widget. Built once per page render so that it keeps focus while the heading is redrawn.
 */
function tabStrip(st) {
  const { project, region, engine } = st.agentParams;
  return h('nav', { class: ['tabs', 'agent-tabs'], 'aria-label': t('Agent views') }, TAB_IDS.map((id) => h('a', {
    class: 'tab', href: agentHash(project, region, engine, id), 'aria-current': id === st.tab ? 'page' : null,
    // A plain click opens the view in this page; a modified click opens it elsewhere and leaves this page as is.
    onclick: (e) => { keepTabFocus = id !== st.tab && e.button === 0 && !(e.metaKey || e.ctrlKey || e.shiftKey || e.altKey); },
  }, tabLabel(id))));
}

function renderHeading(heading, st, agent) {
  const title = h('h1', { class: 'page-title' }, agent ? agent.name : st.agentParams.engine);
  const back = h('a', { class: 'back-link', href: '#/deployments' }, icon('chevron-left', { size: 14 }), t('All deployments'));
  if (!agent) {
    mount(heading, back, h('div', { class: 'page-heading' }, title));
    return;
  }
  const instances = isNum(agent.minInstances) || isNum(agent.maxInstances)
    ? `${isNum(agent.minInstances) ? fmt.int(agent.minInstances) : DASH} \u2013 ${isNum(agent.maxInstances) ? fmt.int(agent.maxInstances) : DASH}` : null;
  mount(heading, back,
    h('div', { class: 'page-heading' },
      h('div', { class: 'row wrap' }, title,
        badge(agent.project, 'neutral', { size: 'sm' }), badge(agent.region, 'neutral', { size: 'sm' }),
        agent.framework ? badge(agent.framework, 'info', { size: 'sm' }) : null),
      agent.description ? h('p', { class: 'page-desc' }, agent.description) : null,
      h('div', { class: 'agent-facts' },
        fact(t('Resource'), h('span', { class: 'mono' }, `reasoningEngines/${agent.id}`)),
        agent.identity ? fact(t('Identity'), principalCell(agent.identity)) : null,
        agent.pythonVersion ? fact(t('Runtime'), `Python ${agent.pythonVersion}`) : null,
        instances ? fact(t('Instances'), instances) : null,
        agent.updateTime ? fact(t('Updated'), fmt.relative(agent.updateTime)) : null)));
}

/* ------------------------------------------------------------------ shared pieces */

function fetchView(st, path, ranged) {
  const params = ranged ? rangeParams(st.agentParams) : st.agentParams;
  return api.get(`${API}/${path}`, params, { signal: st.ctx.signal });
}

function spanCard(n, c) {
  c.classList.add(`span-${n}`);
  return c;
}

function sectionTitle(title, desc) {
  return h('div', { class: 'section-head' }, h('h2', { class: 'section-title' }, title), desc ? h('p', { class: 'section-desc' }, desc) : null);
}

function notReported() {
  return banner({
    tone: 'info',
    title: t('No agent-reported metrics in this period'),
    text: t('Invocation, model, tool and token metrics appear when the agent is built with Agent Development Kit 2.6 or later and deployed with GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true.'),
  });
}

/* ------------------------------------------------------------------ telemetry */

function renderTelemetry(panel, d, st) {
  const rt = d.runtime || {};
  const k = rt.kpis || {};
  const flags = d.telemetry || {};
  if (flags.enabled === false) {
    panel.append(banner({
      tone: 'warning',
      title: t('Agent telemetry is turned off for this deployment'),
      text: t('GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY is set to false, so the agent sends no traces and no agent-reported metrics. Runtime metrics below are still collected.'),
    }));
  }
  const sessions = d.sessions;
  panel.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Requests'), value: fmt.int(val(k.requests)), delta: deltaOf(k.requests), goodWhen: null, tip: t('Requests that reached the agent runtime, from the Cloud Monitoring metric request_count.') }),
    kpi({ label: t('Error rate'), value: fmt.pct(val(k.errorRate), 2), delta: deltaOf(k.errorRate), goodWhen: 'down', meta: isNum(k.errors) ? h('span', null, t('{n} errors', { n: fmt.int(k.errors) })) : null, tip: t('Share of requests answered with a 4xx or 5xx response code.') }),
    kpi({ label: t('P95 request latency'), value: ms(val(k.latencyP95Ms)), delta: deltaOf(k.latencyP95Ms), goodWhen: 'down', meta: h('span', null, `p50 ${ms(k.latencyP50Ms)} \u00b7 p99 ${ms(k.latencyP99Ms)}`) }),
    kpi({ label: t('Stored sessions'), value: fmt.int(sessions && sessions.count), meta: sessions && isNum(sessions.users) ? h('span', null, t('{n} users', { n: fmt.int(sessions.users) })) : null, tip: t('Sessions kept in Agent Platform Sessions that were active in this period. Agents that keep sessions elsewhere show zero.') })));

  const charts = h('div', { class: 'grid' });
  const requests = chartCard({
    title: t('Requests by outcome'), subtitle: rangeText(), source: 'aiplatform.googleapis.com/reasoning_engine/request_count',
    build: (host) => barChart(host, { ...fromTimeline(rt.requestTrend, { names: { ok: t('Success'), client: t('Client errors (4xx)'), server: t('Server errors (5xx)') }, colors: { ok: TONES.success, client: TONES.warning, server: TONES.danger } }), stacked: true, integer: true, height: 230 }),
  });
  const latency = chartCard({
    title: t('Request latency'), subtitle: t('Percentiles per period'), source: 'aiplatform.googleapis.com/reasoning_engine/request_latencies',
    build: (host) => lineChart(host, { ...fromTimeline(rt.latencyTrend, { colors: { p50: TONES.info, p95: TONES.warning, p99: TONES.danger } }), yFormat: ms, valueFormat: ms, area: false, gaps: true, height: 230 }),
  });
  panel.append(charts);
  mountCharts(charts, [spanCard(6, requests), spanCard(6, latency)]);

  const ag = d.agent;
  panel.append(sectionTitle(t('Reported by the agent'), t('OpenTelemetry metrics the agent sends itself: invocations per agent, latency and token usage.')));
  if (ag && ag.reported === false) {
    panel.append(notReported());
  } else if (ag && ag.reported) {
    const ak = ag.kpis || {};
    const turnBased = isNum(ak.turns);
    panel.append(h('div', { class: 'kpi-grid' },
      kpi({ label: t('Agent invocations'), value: fmt.int(val(ak.invocations)), delta: deltaOf(ak.invocations), goodWhen: null, meta: isNum(ak.errors) && ak.errors > 0 ? h('span', { class: 'text-danger' }, t('{n} errors', { n: fmt.int(ak.errors) })) : null, tip: t('Every invocation of the root agent and of its sub-agents.') }),
      turnBased
        ? kpi({ label: t('P95 turn latency'), value: ms(ak.turnLatencyP95Ms), meta: h('span', null, `${t('{n} turns', { n: fmt.int(ak.turns) })} \u00b7 p50 ${ms(ak.turnLatencyP50Ms)}`), tip: t('Time to answer one user message, across all agents that took part.') })
        : kpi({ label: t('P95 agent latency'), value: ms(ak.agentLatencyP95Ms), tip: t('Duration of a single agent invocation.') }),
      kpi({ label: t('Input tokens'), value: fmt.compact(ak.inputTokens) }),
      kpi({ label: t('Output tokens'), value: fmt.compact(ak.outputTokens) })));
    const row = h('div', { class: 'grid' });
    const tokens = chartCard({
      title: t('Token usage'), subtitle: rangeText(), source: 'gen_ai.client.token.usage',
      build: (host) => barChart(host, { ...fromTimeline(ag.tokenTrend, { names: { input: t('Input tokens'), output: t('Output tokens') }, colors: { input: TONES.info, output: TONES.violet } }), stacked: true, integer: true, height: 220 }),
    });
    const perAgent = card({
      title: t('Invocations by agent'), subtitle: t('Root agent and sub-agents'), flush: true, source: 'gen_ai.invoke_agent.duration',
      body: dataTable({
        columns: [
          { key: 'agent', label: t('Agent'), render: (r) => h('span', { class: 'cell-main' }, r.agent) },
          { key: 'invocations', label: t('Invocations'), align: 'num', format: fmt.int },
          { key: 'errors', label: t('Errors'), align: 'num', render: (r) => (r.errors ? h('span', { class: 'text-danger fw-600' }, fmt.int(r.errors)) : fmt.int(r.errors || 0)), value: (r) => r.errors },
          { key: 'p50Ms', label: 'p50', align: 'num', format: ms },
          { key: 'p95Ms', label: 'p95', align: 'num', format: ms },
        ],
        rows: ag.byAgent, searchable: false, pageSize: 8, initialSort: { key: 'invocations', dir: 'desc' },
        emptyText: t('No agent invocations in this period.'),
      }),
    });
    panel.append(row);
    mountCharts(row, [spanCard(6, tokens), spanCard(6, perAgent)]);
  }

  panel.append(sectionTitle(t('Runtime resources'), t('Container CPU and memory allocated to the deployment, and requests by response code.')));
  const usage = h('div', { class: 'grid' });
  const resources = chartCard({
    title: t('CPU and memory allocation'), subtitle: rangeText(), source: 'aiplatform.googleapis.com/reasoning_engine/{cpu,memory}/allocation_time',
    build: (host) => lineChart(host, { ...fromTimeline(rt.resourceTrend, { names: { cpu: t('CPU (vCPU-seconds)'), memory: t('Memory (GiB-seconds)') }, colors: { cpu: TONES.teal, memory: TONES.violet } }), area: false, forceLegend: true, height: 220 }),
  });
  const codes = card({
    title: t('Response codes'), subtitle: rangeText(), flush: true,
    body: dataTable({
      columns: [
        { key: 'code', label: t('Response code'), render: (r) => h('span', { class: 'cell-mono' }, r.code || DASH) },
        { key: 'class', label: t('Outcome'), render: (r) => (r.class === 'ok' ? badge(t('Success'), 'success', { size: 'sm' }) : badge(r.class === 'client' ? t('Client error') : t('Server error'), r.class === 'client' ? 'warning' : 'danger', { size: 'sm' })) },
        { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
      ],
      rows: rt.byCode === undefined ? null : rt.byCode, searchable: false, pageSize: 6, initialSort: { key: 'requests', dir: 'desc' },
      emptyText: t('No requests in this period.'),
    }),
  });
  panel.append(usage);
  mountCharts(usage, [spanCard(7, resources), spanCard(5, codes)]);
  return null;
}

/* ------------------------------------------------------------------ models and tools */

function renderModels(panel, d) {
  if (d.reported === false) {
    panel.append(notReported());
    return null;
  }
  const k = d.kpis || {};
  panel.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Model calls'), value: fmt.int(val(k.modelCalls)), delta: deltaOf(k.modelCalls), goodWhen: null }),
    kpi({ label: t('Model error rate'), value: fmt.pct(k.modelErrorRate, 2), meta: isNum(k.modelErrors) ? h('span', null, t('{n} errors', { n: fmt.int(k.modelErrors) })) : null, tip: t('Model calls that ended with an error, for example a quota or permission error.') }),
    kpi({ label: t('P95 model latency'), value: ms(k.modelLatencyP95Ms) }),
    kpi({ label: t('Tokens'), value: fmt.compact(isNum(k.inputTokens) && isNum(k.outputTokens) ? k.inputTokens + k.outputTokens : null), meta: h('span', null, `${t('Input')} ${fmt.compact(k.inputTokens)} \u00b7 ${t('Output')} ${fmt.compact(k.outputTokens)}`) }),
    kpi({ label: t('Model calls per invocation'), value: fmt.num(k.inferenceCallsPerInvocation, 2), tip: t('Average number of model calls one agent invocation makes.') })));

  const trend = chartCard({
    title: t('Calls by model'), subtitle: rangeText(), source: 'gen_ai.client.operation.duration',
    build: (host) => barChart(host, { ...fromTimeline(d.modelTrend, { names: { other: t('Other models') } }), stacked: true, integer: true, height: 230 }),
  });
  const row = h('div', { class: 'grid' });
  panel.append(row);
  mountCharts(row, [spanCard(12, trend)]);

  panel.append(card({
    title: t('Models'), subtitle: rangeText(), flush: true, source: 'gen_ai.client.operation.duration, gen_ai.client.token.usage',
    body: dataTable({
      columns: [
        { key: 'model', label: t('Model'), render: (r) => h('span', { class: 'cell-main cell-mono' }, r.model) },
        { key: 'calls', label: t('Calls'), align: 'num', format: fmt.int },
        { key: 'errors', label: t('Errors'), align: 'num', render: (r) => (r.errors ? h('span', { class: 'text-danger fw-600', title: (r.errorTypes || []).map((e) => `${e.type}: ${e.count}`).join(', ') }, fmt.int(r.errors)) : (isNum(r.errors) ? fmt.int(r.errors) : DASH)), value: (r) => r.errors },
        { key: 'errorRate', label: t('Error rate'), align: 'num', format: (v) => fmt.pct(v, 2) },
        { key: 'errorTypes', label: t('Error types'), sortable: false, render: (r) => listCell((r.errorTypes || []).map((e) => `${e.type} (${fmt.int(e.count)})`), { mono: true }), csv: (r) => (r.errorTypes || []).map((e) => `${e.type}:${e.count}`).join(' ') },
        { key: 'p50Ms', label: 'p50', align: 'num', format: ms },
        { key: 'p95Ms', label: 'p95', align: 'num', format: ms },
        { key: 'inputTokens', label: t('Input tokens'), align: 'num', format: fmt.compact },
        { key: 'outputTokens', label: t('Output tokens'), align: 'num', format: fmt.compact },
      ],
      rows: d.models, pageSize: 10, exportName: 'agent-models', initialSort: { key: 'calls', dir: 'desc' },
      emptyText: t('No model calls in this period.'),
    }),
  }));

  panel.append(sectionTitle(t('Tools'), t('Tools the agents called, with latency and failures.')));
  panel.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Tool calls'), value: fmt.int(k.toolCalls) }),
    kpi({ label: t('Failed tool calls'), value: fmt.int(k.toolErrors) }),
    kpi({ label: t('Tool calls per invocation'), value: fmt.num(k.toolCallsPerInvocation, 2) })));
  panel.append(card({
    title: t('Tools'), subtitle: rangeText(), flush: true, source: 'gen_ai.execute_tool.duration',
    body: dataTable({
      columns: [
        { key: 'tool', label: t('Tool'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main cell-mono' }, r.tool), h('span', { class: 'cell-sub' }, r.type || '')) },
        { key: 'agents', label: t('Called by'), sortable: false, render: (r) => listCell(r.agents), csv: (r) => (r.agents || []).join(' ') },
        { key: 'calls', label: t('Calls'), align: 'num', format: fmt.int },
        { key: 'errors', label: t('Errors'), align: 'num', render: (r) => (r.errors ? h('span', { class: 'text-danger fw-600' }, fmt.int(r.errors)) : fmt.int(r.errors || 0)), value: (r) => r.errors },
        { key: 'errorRate', label: t('Error rate'), align: 'num', format: (v) => fmt.pct(v, 2) },
        { key: 'p50Ms', label: 'p50', align: 'num', format: ms },
        { key: 'p95Ms', label: 'p95', align: 'num', format: ms },
      ],
      rows: d.tools, pageSize: 10, exportName: 'agent-tools', initialSort: { key: 'calls', dir: 'desc' },
      emptyText: t('No tool calls in this period.'),
    }),
  }));
  return null;
}

/* ------------------------------------------------------------------ traces */

function contentNote() {
  return banner({
    tone: 'neutral', iconName: 'eye-off',
    title: t('Prompts and responses are not shown'),
    text: t('This view shows timing, structure and usage metadata only. Message content stays in Google Cloud, where it is protected by its own access controls.'),
  });
}

function renderTraces(panel, d, st) {
  panel.append(contentNote());
  const rows = d.traces;
  // Agents that do not record a user ID on their spans get no (empty) user column.
  const showUser = (rows || []).some((r) => r.userId || r.restricted);
  panel.append(card({
    title: t('Recent traces'), flush: true, source: 'Cloud Trace',
    subtitle: rows === null ? t('Data source unavailable') : (d.truncated ? t('Latest {n} traces of the period', { n: fmt.int(rows.length) }) : t('{n} traces', { n: fmt.int(rows.length) })),
    body: dataTable({
      columns: [
        { key: 'start', label: t('Started'), render: (r) => fmt.dateTime(r.start, true), value: (r) => r.start },
        { key: 'name', label: t('Root span'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main truncate', title: r.name }, r.subject || r.agent || r.name), h('span', { class: 'cell-sub cell-mono' }, r.traceId.slice(0, 16))) },
        { key: 'durationMs', label: t('Elapsed time'), align: 'num', format: ms },
        { key: 'sessionId', label: t('Session'), render: (r) => (r.sessionId ? h('span', { class: 'cell-mono truncate', title: r.sessionId }, r.sessionId) : DASH) },
        showUser ? { key: 'userId', label: t('User'), render: (r) => (r.restricted ? h('span', { class: 'muted' }, t('Restricted')) : (r.userId ? h('span', { class: 'truncate', title: r.userId }, r.userId) : DASH)) } : null,
        { key: 'error', label: t('Status'), title: t('Outcome of the root span. Open a trace to see errors of the spans inside it.'), render: (r) => (r.error ? badge(r.error, 'danger', { size: 'sm', dot: true }) : badge(t('Success'), 'success', { size: 'sm', dot: true })), value: (r) => (r.error ? 1 : 0) },
      ].filter(Boolean),
      rows, pageSize: 15, exportName: 'agent-traces', initialSort: { key: 'start', dir: 'desc' },
      emptyText: t('No traces in this period. Traces appear when the agent is deployed with telemetry turned on.'),
      onRowClick: (r) => openTrace(st, r),
    }),
  }));
  return null;
}

function openTrace(st, row) {
  const drawer = openDrawer({
    title: row.subject || row.agent || row.name, subtitle: `${t('Trace')} ${row.traceId}`, width: 920,
    content: (body) => mount(body, skeleton('lines')),
  });
  api.get(`${API}/trace`, { ...st.agentParams, trace: row.traceId }).then((res) => {
    const tr = res.data.trace;
    mount(drawer.body);
    const notice = warningsNotice(res.meta && res.meta.warnings);
    if (notice) drawer.body.append(notice);
    if (!tr) return;
    append(drawer.body, [
      h('div', { class: 'stat-row' },
        stat(t('Elapsed time'), ms(tr.durationMs)),
        stat(t('Spans'), fmt.int(tr.spanCount)),
        stat(t('Model calls'), fmt.int(tr.modelCalls)),
        stat(t('Tool calls'), fmt.int(tr.toolCalls)),
        stat(t('Tokens'), fmt.compact(tr.inputTokens + tr.outputTokens), `${t('Input')} ${fmt.compact(tr.inputTokens)} \u00b7 ${t('Output')} ${fmt.compact(tr.outputTokens)}`),
        stat(t('Errors'), fmt.int(tr.errors))),
      card({
        title: t('Trace details'),
        body: kv([
          [t('Started'), fmt.dateTime(tr.start, true)],
          [t('Agents'), (tr.agents || []).join(', ')],
          [t('Session'), tr.sessionId ? h('span', { class: 'mono' }, tr.sessionId) : null],
          tr.restricted || tr.userId ? [t('User'), tr.restricted ? h('span', { class: 'muted' }, t('Restricted')) : tr.userId] : null,
        ]),
      }),
      card({ title: t('Timeline'), subtitle: tr.truncated ? t('First {n} spans', { n: fmt.int(tr.spans.length) }) : null, flush: true, body: waterfall(tr) }),
    ]);
  }).catch((err) => {
    if (!isAbort(err)) mount(drawer.body, errorState(err));
  });
}

function attributeText(span) {
  const a = span.attributes || {};
  const parts = [];
  if (a.model && span.kind !== 'model') parts.push(a.model);
  if (isNum(a.inputTokens) || isNum(a.outputTokens)) parts.push(t('{a} in / {b} out tokens', { a: fmt.int(a.inputTokens || 0), b: fmt.int(a.outputTokens || 0) }));
  if (a.toolType) parts.push(a.toolType);
  if (a.finishReason) parts.push(a.finishReason);
  return parts.join(' \u00b7 ');
}

/** Span tree as a table: name indented by depth, duration, and a bar placed on the trace's time axis. */
function waterfall(tr) {
  const total = isNum(tr.durationMs) && tr.durationMs > 0 ? tr.durationMs : 1;
  const rows = tr.spans.map((sp) => {
    const label = h('div', { class: 'wf-label' }, kindBadge(sp.kind),
      h('div', { class: 'cell-stack' },
        h('span', { class: 'cell-main truncate', title: sp.name }, sp.subject || sp.name),
        attributeText(sp) ? h('span', { class: 'cell-sub truncate' }, attributeText(sp)) : null));
    label.style.setProperty('padding-left', `${Math.min(sp.depth, 12) * 16}px`);
    const bar = h('span', { class: ['wf-bar', sp.error ? 'error' : ''] });
    const left = isNum(sp.offsetMs) ? Math.max(0, Math.min(100, (sp.offsetMs / total) * 100)) : 0;
    const width = isNum(sp.durationMs) ? Math.max(0.6, Math.min(100 - left, (sp.durationMs / total) * 100)) : 0.6;
    bar.style.setProperty('left', `${left}%`);
    bar.style.setProperty('width', `${width}%`);
    if (!sp.error) bar.style.setProperty('background', KIND_COLORS[sp.kind] || TONES.neutral);
    // The bar is a picture of two numbers; the same numbers are its text alternative.
    const timing = t('Starts {a} into the trace and takes {b}', { a: ms(sp.offsetMs), b: ms(sp.durationMs) });
    return h('tr', null,
      h('td', null, label),
      h('td', { class: 'num' }, ms(sp.durationMs)),
      h('td', null, sp.error ? badge(sp.error, 'danger', { size: 'sm' }) : null),
      h('td', { class: 'wf-track-cell' }, h('div', { class: 'wf-track', role: 'img', 'aria-label': timing, title: timing }, bar)));
  });
  return h('div', { class: 'table-wrap auto-h' }, h('table', { class: ['table', 'waterfall'] },
    h('thead', null, h('tr', null,
      h('th', { scope: 'col' }, t('Span')), h('th', { scope: 'col', class: 'num' }, t('Elapsed time')),
      h('th', { scope: 'col' }, t('Error')), h('th', { scope: 'col' }, t('Timeline')))),
    h('tbody', null, rows)));
}

/* ------------------------------------------------------------------ topology */

const NODE_W = 184;
const NODE_H = 50;
const COL_GAP = 96;
const ROW_GAP = 18;

function layout(nodes, edges) {
  const out = new Map();
  const next = new Map();
  edges.forEach((e) => { if (!next.has(e.source)) next.set(e.source, []); next.get(e.source).push(e.target); });
  const depth = new Map();
  const roots = nodes.some((n) => n.id === 'client') ? ['client'] : nodes.slice(0, 1).map((n) => n.id);
  const queue = roots.map((id) => [id, 0]);
  while (queue.length) {
    const [id, level] = queue.shift();
    if (depth.has(id)) continue;
    depth.set(id, level);
    (next.get(id) || []).forEach((target) => { if (!depth.has(target)) queue.push([target, level + 1]); });
  }
  const columns = [];
  nodes.forEach((n) => {
    const level = depth.has(n.id) ? depth.get(n.id) : 0;
    if (!columns[level]) columns[level] = [];
    columns[level].push(n);
  });
  const tallest = Math.max(...Array.from(columns, (c) => (c ? c.length : 0)), 1);
  const height = tallest * NODE_H + (tallest - 1) * ROW_GAP + 24;
  columns.forEach((col, level) => {
    if (!col) return;
    const colHeight = col.length * NODE_H + (col.length - 1) * ROW_GAP;
    col.forEach((n, i) => out.set(n.id, { x: 12 + level * (NODE_W + COL_GAP), y: (height - colHeight) / 2 + i * (NODE_H + ROW_GAP) }));
  });
  return { pos: out, width: 24 + columns.length * NODE_W + (columns.length - 1) * COL_GAP, height };
}

function clip(text, max) {
  const str = String(text || '');
  return str.length > max ? `${str.slice(0, max - 1)}\u2026` : str;
}

function topologyGraph(nodes, edges) {
  const { pos, width, height } = layout(nodes, edges);
  const svg = s('svg', { width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', class: 'topology-svg' },
    s('title', null, t('Call graph of the agent: {n} nodes and {m} connections. The table below lists every connection.', { n: nodes.length, m: edges.length })));
  const maxCalls = Math.max(...edges.map((e) => e.calls), 1);
  edges.forEach((e) => {
    const a = pos.get(e.source);
    const b = pos.get(e.target);
    if (!a || !b) return;
    const x1 = a.x + NODE_W;
    const y1 = a.y + NODE_H / 2;
    const x2 = b.x;
    const y2 = b.y + NODE_H / 2;
    const bend = Math.max(36, Math.abs(x2 - x1) / 2);
    const path = s('path', { d: `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`, class: 'topology-edge', fill: 'none' });
    path.style.setProperty('stroke-width', `${1 + (e.calls / maxCalls) * 2.5}`);
    svg.append(path, s('text', { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 5, 'text-anchor': 'middle', class: 'topology-edge-label' }, fmt.int(e.calls)));
  });
  nodes.forEach((n) => {
    const p = pos.get(n.id);
    if (!p) return;
    const g = s('g', { class: 'topology-node' });
    const rect = s('rect', { x: p.x, y: p.y, width: NODE_W, height: NODE_H, rx: 9 });
    rect.style.setProperty('stroke', KIND_COLORS[n.kind] || TONES.neutral);
    const name = n.kind === 'client' ? t('Client') : n.name;
    const sub = n.kind === 'client' ? '' : `${kindLabel(n.kind)} \u00b7 ${t('{n} calls', { n: fmt.int(n.calls) })}${isNum(n.avgMs) ? ` \u00b7 ${ms(n.avgMs)}` : ''}`;
    const failed = n.errors ? ` \u00b7 ${t('{n} errors', { n: fmt.int(n.errors) })}` : '';
    append(g, [rect,
      s('title', null, `${kindLabel(n.kind)}: ${name}${failed}`),
      s('text', { x: p.x + 12, y: p.y + (sub ? 21 : 30), class: 'topology-name' }, clip(name, n.errors ? 21 : 24)),
      sub ? s('text', { x: p.x + 12, y: p.y + 38, class: 'topology-sub' }, clip(sub, 34)) : null,
      n.errors ? s('circle', { cx: p.x + NODE_W - 13, cy: p.y + 14, r: 4.5, class: 'topology-error' }) : null]);
    svg.append(g);
  });
  return h('div', { class: 'topology-scroll' }, svg);
}

function renderTopology(panel, d) {
  const nodes = d.nodes;
  const edges = d.edges || [];
  if (nodes === null) {
    panel.append(card({ title: t('Call graph'), body: emptyState({ icon: 'share', tone: 'warning', title: t('Data source unavailable'), body: t('See the data source notices above.') }) }));
    return null;
  }
  if (!nodes.length) {
    panel.append(card({ title: t('Call graph'), body: emptyState({ icon: 'share', title: t('No traces to build the graph from'), body: t('The call graph is drawn from the agent\'s traces. Traces appear when the agent is deployed with telemetry turned on and receives requests in the selected period.') }) }));
    return null;
  }
  const names = new Map(nodes.map((n) => [n.id, n]));
  const nameOf = (id) => { const n = names.get(id); return n ? (n.kind === 'client' ? t('Client') : n.name) : id; };
  panel.append(card({
    title: t('Call graph'),
    subtitle: d.truncated ? t('From the {n} most recent traces of the period', { n: fmt.int(d.sampledTraces) }) : t('From {n} traces', { n: fmt.int(d.sampledTraces) }),
    body: h('div', { class: 'stack' },
      topologyGraph(nodes, edges),
      h('div', { class: 'row wrap topology-legend' }, ['agent', 'tool', 'model'].map((kind) => {
        const dot = h('span', { class: 'swatch round' });
        dot.style.setProperty('background', KIND_COLORS[kind]);
        return h('span', { class: 'row' }, dot, kindLabel(kind));
      }))),
    source: 'Cloud Trace',
  }));
  const row = h('div', { class: 'grid' });
  const connections = card({
    title: t('Connections'), subtitle: t('Who calls what, and how often'), flush: true,
    body: dataTable({
      columns: [
        { key: 'source', label: t('Caller'), render: (e) => h('span', { class: 'cell-main' }, nameOf(e.source)), value: (e) => nameOf(e.source), csv: (e) => nameOf(e.source) },
        { key: 'target', label: t('Called component'), render: (e) => h('div', { class: 'row' }, kindBadge((names.get(e.target) || {}).kind), h('span', { class: 'cell-mono' }, nameOf(e.target))), value: (e) => nameOf(e.target), csv: (e) => nameOf(e.target) },
        { key: 'calls', label: t('Calls'), align: 'num', format: fmt.int },
      ],
      rows: edges, searchable: false, pageSize: 10, exportName: 'agent-topology', initialSort: { key: 'calls', dir: 'desc' },
    }),
  });
  const components = card({
    title: t('Components'), subtitle: t('Calls, failures and average elapsed time'), flush: true,
    body: dataTable({
      columns: [
        { key: 'name', label: t('Component'), render: (n) => h('div', { class: 'row' }, kindBadge(n.kind), h('span', { class: 'cell-mono' }, n.name)) },
        { key: 'calls', label: t('Calls'), align: 'num', format: fmt.int },
        { key: 'errors', label: t('Errors'), align: 'num', render: (n) => (n.errors ? h('span', { class: 'text-danger fw-600' }, fmt.int(n.errors)) : fmt.int(n.errors || 0)), value: (n) => n.errors },
        { key: 'avgMs', label: t('Average elapsed time'), align: 'num', format: ms },
      ],
      rows: nodes.filter((n) => n.kind !== 'client'), searchable: false, pageSize: 10, exportName: 'agent-components', initialSort: { key: 'calls', dir: 'desc' },
    }),
  });
  panel.append(row);
  append(row, [spanCard(6, connections), spanCard(6, components)]);
  return null;
}

/* ------------------------------------------------------------------ revisions */

function stateBadge(state) {
  if (state === 'ACTIVE') return badge(t('Active'), 'success', { size: 'sm', dot: true });
  if (state === 'ARCHIVED') return badge(t('Archived'), 'neutral', { size: 'sm', dot: true });
  if (state === 'SUSPENDED') return badge(t('Suspended'), 'warning', { size: 'sm', dot: true });
  if (state === 'FAILED') return badge(t('Failed'), 'danger', { size: 'sm', dot: true });
  if (state === 'WARNING') return badge(t('Warning'), 'warning', { size: 'sm', dot: true });
  return badge(state || DASH, 'neutral', { size: 'sm' });
}

function telemetryBadge(flags) {
  if (!flags || flags.enabled === null || flags.enabled === undefined) return h('span', { class: 'text-muted' }, t('Not set'));
  return flags.enabled ? badge(t('On'), 'success', { size: 'sm' }) : badge(t('Off'), 'neutral', { size: 'sm' });
}

function renderRevisions(panel, d) {
  const k = d.kpis || {};
  const rows = d.revisions;
  const latest = (rows || []).find((r) => r.latest);
  panel.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Traffic split'), value: d.splitMode === 'manual' ? t('Manual') : (d.splitMode === 'latest' ? t('Latest revision') : DASH), tip: t('Latest revision: all traffic goes to the newest active revision. Manual: traffic is divided between revisions by percentage.') }),
    kpi({ label: t('Active revisions'), value: fmt.int(k.active), meta: isNum(k.total) ? h('span', null, t('of {n} revisions', { n: fmt.int(k.total) })) : null }),
    kpi({ label: t('Latest revision'), value: latest ? h('span', { class: 'mono' }, latest.id) : DASH, meta: latest && latest.createTime ? h('span', null, fmt.relative(latest.createTime)) : null })));
  panel.append(card({
    title: t('Runtime revisions'), flush: true, source: 'Agent Platform API v1beta1 (runtimeRevisions)',
    subtitle: t('A revision is created whenever versioned fields of the deployment change.'),
    body: dataTable({
      columns: [
        { key: 'id', label: t('Revision'), render: (r) => h('div', { class: 'row' }, h('span', { class: 'cell-main cell-mono' }, r.id), r.latest ? badge(t('Latest'), 'info', { size: 'sm' }) : null) },
        { key: 'state', label: t('State'), render: (r) => stateBadge(r.state) },
        { key: 'traffic', label: t('Traffic'), align: 'num', render: (r) => (isNum(r.traffic) ? fmt.pct(r.traffic / 100, 0) : DASH), value: (r) => r.traffic },
        { key: 'createTime', label: t('Created'), render: (r) => (r.createTime ? fmt.dateTime(r.createTime) : DASH), value: (r) => r.createTime },
        { key: 'framework', label: t('Runtime'), render: (r) => h('div', { class: 'cell-stack' }, h('span', null, r.framework || DASH), h('span', { class: 'cell-sub' }, r.pythonVersion ? `Python ${r.pythonVersion}` : '')) },
        { key: 'maxInstances', label: t('Instances'), render: (r) => (isNum(r.minInstances) || isNum(r.maxInstances) ? `${isNum(r.minInstances) ? r.minInstances : DASH} \u2013 ${isNum(r.maxInstances) ? r.maxInstances : DASH}` : DASH), value: (r) => r.maxInstances },
        { key: 'telemetry', label: t('Telemetry'), sortable: false, render: (r) => telemetryBadge(r.telemetry), csv: (r) => (r.telemetry && r.telemetry.enabled === true ? 'on' : (r.telemetry && r.telemetry.enabled === false ? 'off' : '')) },
      ],
      rows, pageSize: 12, exportName: 'agent-revisions', initialSort: { key: 'createTime', dir: 'desc' },
      emptyText: t('This deployment has no runtime revisions.'),
    }),
  }));
  return null;
}

/* ------------------------------------------------------------------ evaluation */

function renderEvaluation(panel, d) {
  const k = d.kpis || {};
  panel.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Online monitors'), value: fmt.int(k.monitors), meta: isNum(k.activeMonitors) ? h('span', null, t('{n} active', { n: fmt.int(k.activeMonitors) })) : null, tip: t('Monitors that score a sample of this agent\'s live traces on a schedule.') }),
    kpi({ label: t('Evaluations'), value: fmt.int(k.evaluations), tip: t('Samples scored by the online monitors in this period.') }),
    kpi({ label: t('Failed evaluations'), value: fmt.int(k.failedEvaluations) }),
    kpi({ label: t('Evaluation runs'), value: fmt.int(d.runs ? d.runs.length : null), tip: t('Evaluation runs that ran this agent against an evaluation set.') })));

  const row = h('div', { class: 'grid' });
  // Most metrics score between 0 and 1; a metric on a wider scale (for example 1 to 5) widens the axis.
  const scoreMax = Math.max(1, ...(d.scores || []).map((r) => (isNum(r.average) ? r.average : 0)));
  const scores = card({
    title: t('Average score by metric'), subtitle: rangeText(), source: 'aiplatform.googleapis.com/online_evaluator/scores',
    body: hbarList(d.scores === null ? null : (d.scores || []).map((r, i) => ({ label: r.metric, value: r.average, sub: t('{n} samples', { n: fmt.int(r.samples) }), color: colorAt(i) })), { format: (v) => fmt.num(v, 2), max: scoreMax, emptyText: d.scores === null ? null : t('No online evaluation scores in this period.') }),
  });
  const trend = chartCard({
    title: t('Score trend'), subtitle: t('Average per period'), source: 'aiplatform.googleapis.com/online_evaluator/scores',
    build: (host) => lineChart(host, { ...fromTimeline(d.scoreTrend), yFormat: (v) => fmt.num(v, 2), valueFormat: (v) => fmt.num(v, 3), area: false, gaps: true, forceLegend: true, height: 220 }),
  });
  panel.append(row);
  mountCharts(row, [spanCard(5, scores), spanCard(7, trend)]);

  panel.append(card({
    title: t('Online monitors'), flush: true, source: 'Agent Platform API (onlineEvaluators)',
    body: dataTable({
      columns: [
        { key: 'name', label: t('Monitor'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main' }, r.name), h('span', { class: 'cell-sub cell-mono' }, r.id)) },
        { key: 'state', label: t('State'), render: (r) => h('div', { class: 'state-cell' }, stateBadge(r.state), r.message ? h('span', { class: 'cell-sub truncate', title: r.message }, r.message) : null) },
        { key: 'metrics', label: t('Metrics'), sortable: false, render: (r) => listCell(r.metrics, { mono: true }), csv: (r) => (r.metrics || []).join(' ') },
        { key: 'scope', label: t('Scope'), render: (r) => (r.scope === 'session' ? t('Sessions') : (r.scope === 'trace' ? t('Traces') : DASH)) },
        { key: 'samplingPercent', label: t('Sampling'), align: 'num', render: (r) => (isNum(r.samplingPercent) ? fmt.pct(r.samplingPercent / 100, 0) : DASH), value: (r) => r.samplingPercent },
        { key: 'updateTime', label: t('Updated'), render: (r) => (r.updateTime || r.createTime ? fmt.relative(r.updateTime || r.createTime) : DASH), value: (r) => r.updateTime || r.createTime },
      ],
      rows: d.monitors, searchable: false, pageSize: 8, initialSort: { key: 'name', dir: 'asc' },
      emptyText: t('No online monitor is attached to this agent.'),
    }),
  }));

  panel.append(card({
    title: t('Evaluation runs'), flush: true, source: 'Agent Platform API (evaluationRuns)',
    subtitle: t('Runs that called this agent. Runs over recorded data do not name an agent and are not listed.'),
    body: dataTable({
      columns: [
        { key: 'name', label: t('Run'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main' }, r.name), h('span', { class: 'cell-sub cell-mono' }, r.id)) },
        { key: 'state', label: t('State'), render: (r) => runBadge(r.state) },
        { key: 'totalItems', label: t('Items'), align: 'num', format: fmt.int },
        { key: 'failedItems', label: t('Failed items'), align: 'num', format: fmt.int },
        { key: 'metrics', label: t('Summary metrics'), sortable: false, render: (r) => listCell((r.metrics || []).map((m) => `${m.name} ${fmt.num(m.value, 2)}`), { mono: true }), csv: (r) => (r.metrics || []).map((m) => `${m.name}=${m.value}`).join(' ') },
        { key: 'createTime', label: t('Created'), render: (r) => (r.createTime ? fmt.dateTime(r.createTime) : DASH), value: (r) => r.createTime },
      ],
      rows: d.runs, searchable: false, pageSize: 8, initialSort: { key: 'createTime', dir: 'desc' },
      emptyText: t('No evaluation run has called this agent.'),
    }),
  }));
  return null;
}

function runBadge(state) {
  if (state === 'SUCCEEDED') return badge(t('Succeeded'), 'success', { size: 'sm', dot: true });
  if (state === 'FAILED') return badge(t('Failed'), 'danger', { size: 'sm', dot: true });
  if (state === 'CANCELLED') return badge(t('Cancelled'), 'neutral', { size: 'sm', dot: true });
  if (!state) return badge(DASH, 'neutral', { size: 'sm' });
  return badge(t('Running'), 'info', { size: 'sm', dot: true });
}

/* ------------------------------------------------------------------ playground */

function agentRow(...content) {
  return h('div', { class: ['msg', 'msg-assistant'] }, h('div', { class: 'assistant-mark' }, icon('spark', { size: 15 })), h('div', { class: 'assistant-body' }, content));
}

function eventNode(ev) {
  const parts = (ev.parts || []).map((part) => {
    if (part.type === 'text') return part.thought ? h('div', { class: 'pg-thought' }, part.text) : markdownLite(part.text);
    const call = part.type === 'call';
    return h('details', { class: 'pg-tool' },
      h('summary', null, icon(call ? 'terminal' : 'check-circle', { size: 13 }), h('span', null, call ? t('Tool call') : t('Tool result')), h('span', { class: 'mono' }, part.name || '')),
      codeBlock(part.text || '', { wrap: true }));
  });
  return agentRow(parts,
    ev.error ? banner({ tone: 'danger', title: t('The agent reported an error'), text: ev.error }) : null,
    ev.author ? h('div', { class: 'msg-meta' }, h('span', null, ev.author)) : null);
}

/** Nodes of one conversation message: the user's bubble, the "working" indicator, or the events of an answer. */
function messageNodes(m) {
  if (m.role === 'user') return [h('div', { class: ['msg', 'msg-user'] }, h('div', { class: 'bubble' }, m.text))];
  // The indicator is decoration: the log region announces the answer itself when it arrives.
  if (m.pending) return [agentRow(h('div', { class: 'typing', 'aria-hidden': 'true' }, h('span'), h('span'), h('span')))];
  if (m.error) return [agentRow(banner({ tone: 'danger', title: t('The agent could not be called'), text: errorText(m.error) }))];
  const nodes = (m.events || []).map(eventNode);
  if (!nodes.length) nodes.push(agentRow(h('span', { class: 'muted' }, t('The agent returned no content.'))));
  const meta = [
    isNum(m.latencyMs) ? h('span', null, fmt.duration(m.latencyMs)) : null,
    m.truncated ? h('span', null, t('Answer cut at the size or time limit')) : null,
    m.auditLogged === false ? h('span', { class: 'text-warning' }, t('Audit log not written')) : null,
  ].filter(Boolean);
  if (meta.length) nodes.push(h('div', { class: ['msg-meta', 'pg-meta'] }, meta));
  return nodes;
}

function renderPlayground(panel, _d, st) {
  const agent = st.agent;
  const support = agent.playground || {};
  if (!isAdmin()) {
    panel.append(banner({ tone: 'neutral', iconName: 'lock', title: t('The playground is available to administrators'), text: t('Sending a message runs the deployed agent, including its model and tool calls, so the playground is limited to administrators.') }));
    return null;
  }
  if (!support.supported) {
    panel.append(banner({ tone: 'info', title: t('The playground cannot call this agent'), text: t('The playground talks to agents that expose a streaming query method (agents built with the Agent Development Kit). This deployment does not expose one.') }));
    return null;
  }
  const key = `${agent.project}/${agent.region}/${agent.id}`;
  if (!conversations.has(key)) conversations.set(key, { sessionId: null, messages: [], busy: false, view: null });
  const convo = conversations.get(key);

  panel.append(banner({
    tone: 'warning', iconName: 'info',
    title: t('Messages run the deployed agent'),
    text: t('Each message calls the live deployment with your account as the user ID. The agent can call its models and tools, which costs tokens and can change data in the systems it is connected to. Every call is written to the governance audit log without the message text.'),
  }));

  // role="log": new messages are announced politely, in the order they are added.
  const thread = h('div', { class: ['chat-thread', 'pg-thread'], role: 'log', 'aria-label': t('Conversation with the agent') });
  const inputId = 'pg-message';
  const hintId = 'pg-hint';
  const input = h('textarea', { id: inputId, rows: 1, maxlength: 4000, 'aria-describedby': hintId });
  const sendBtn = button({ icon: 'send', variant: 'primary', title: t('Send'), size: 'sm' });
  const sessionInfo = h('span', { class: ['mono', 'truncate'] });
  const newBtn = button({ label: t('New session'), icon: 'plus', size: 'sm' });
  const composer = h('div', { class: 'composer' },
    h('label', { class: 'field-label', for: inputId }, t('Message to the agent')),
    h('div', { class: 'composer-box' }, input, sendBtn),
    h('div', { class: 'composer-meta' }, h('span', { id: hintId }, t('Enter to send, Shift+Enter for a new line')), h('span', { class: 'row' }, sessionInfo, newBtn)));
  panel.append(h('div', { class: ['analyst-main', 'pg-main'] }, thread, composer));

  // Where the browser sizes the field to its content (field-sizing), CSS does it; elsewhere the height is set here.
  const autoSize = CSS.supports('field-sizing', 'content') ? () => {} : () => {
    input.style.setProperty('height', 'auto');
    input.style.setProperty('height', `${Math.min(160, input.scrollHeight)}px`);
  };
  const toEnd = () => { thread.scrollTop = thread.scrollHeight; };

  function sync() {
    sessionInfo.textContent = convo.sessionId ? `${t('Session')} ${convo.sessionId}` : '';
    sessionInfo.title = convo.sessionId || '';
    sendBtn.disabled = convo.busy;
    newBtn.disabled = convo.busy;
  }

  function redraw() {
    sync();
    if (convo.messages.length) mount(thread, convo.messages.map(messageNodes));
    else mount(thread, emptyState({ icon: 'play', title: t('Try the agent'), body: t('Send a message to see how the deployed agent answers, which tools it calls and what they return.') }));
    toEnd();
  }

  // The view on screen. A call that is still running when the page is drawn again answers into the new view.
  convo.view = { redraw };

  async function send() {
    const text = input.value.trim();
    if (!text || convo.busy) return;
    convo.busy = true;
    input.value = '';
    autoSize();
    const question = { role: 'user', text };
    const answer = { role: 'agent', pending: true };
    if (!convo.messages.length) mount(thread);
    convo.messages.push(question, answer);
    // Appended, not redrawn: the log region then announces only the new message.
    const waiting = messageNodes(answer)[0];
    append(thread, [messageNodes(question), waiting]);
    toEnd();
    sync();
    try {
      const res = await api.post(`${API}/playground`, { ...st.agentParams, message: text, sessionId: convo.sessionId || undefined });
      Object.assign(answer, { pending: false, events: res.data.events, latencyMs: res.data.latencyMs, truncated: res.data.truncated, auditLogged: res.data.auditLogged });
      if (res.data.sessionId) convo.sessionId = res.data.sessionId;
    } catch (err) {
      Object.assign(answer, { pending: false, error: err });
    }
    convo.busy = false;
    if (!waiting.isConnected) {
      if (convo.view) convo.view.redraw();
      return;
    }
    waiting.replaceWith(...messageNodes(answer));
    toEnd();
    sync();
    // Disabling the send button drops focus to the page; it goes back to the message field.
    const focused = document.activeElement;
    if (!focused || focused === document.body || focused === sendBtn) input.focus();
  }

  input.addEventListener('input', autoSize);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  sendBtn.addEventListener('click', send);
  newBtn.addEventListener('click', () => {
    if (convo.busy) return;
    convo.sessionId = null;
    convo.messages = [];
    redraw();
    input.focus();
  });
  redraw();
  return null;
}

/* ------------------------------------------------------------------ views */

const VIEWS = {
  telemetry: { fetch: (st) => fetchView(st, 'telemetry', true), render: renderTelemetry },
  models: { fetch: (st) => fetchView(st, 'models', true), render: renderModels },
  traces: { fetch: (st) => fetchView(st, 'traces', true), render: renderTraces },
  topology: { fetch: (st) => fetchView(st, 'topology', true), render: renderTopology },
  revisions: { fetch: (st) => fetchView(st, 'revisions', false), render: renderRevisions },
  evaluation: { fetch: (st) => fetchView(st, 'evaluation', true), render: renderEvaluation },
  playground: { fetch: null, render: renderPlayground },
};
