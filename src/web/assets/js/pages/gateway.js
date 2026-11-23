/**
 * Agent Gateway: the gateways of the workspace and how they are governed, the traffic they allowed and denied,
 * and the calls to AI service APIs recorded in Cloud Audit Logs.
 *
 * Views (each has its own address): #/gateway, #/gateway/traffic, #/gateway/api.
 */
import { h, mount, append } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api, isAbort } from '../core/api.js';
import { store } from '../core/state.js';
import { errorText, serverText } from '../core/messages.js';
import {
  pageHeader, kpi, card, dataTable, badge, userCell, button, setBusy, tabs, kv, stat, field, textInput, selectInput,
  switchInput, banner, emptyState, openDrawer, openModal, confirmDialog, requireAdmin, toast, codeBlock, listCell,
} from '../core/components.js';
import { barChart, donutChart, hbarList, mapList, fromTimeline, TONES } from '../core/charts.js';
import { pageFlow, fetchRange, rangeText, deltaOf, chartCard, mountCharts, openUserDrawer } from './_shared.js';

const BASE = '/api/v1/agent-platform/gateways';
const DOCS = 'https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways';
const POLL_MS = 3000;
const POLL_LIMIT = 200; // about ten minutes

/* ------------------------------------------------------------------ labels */

function pathLabel(path) {
  if (path === 'AGENT_TO_ANYWHERE') return t('Agent-to-Anywhere (egress)');
  if (path === 'CLIENT_TO_AGENT') return t('Client-to-Agent (ingress)');
  return t('Unknown');
}

function deploymentLabel(mode) {
  if (mode === 'google_managed') return t('Google-managed');
  if (mode === 'self_managed') return t('Self-managed');
  return t('Unknown');
}

function workloadTypeLabel(type) {
  return type === 'gemini_enterprise' ? t('Gemini Enterprise app') : t('Agent Runtime agent');
}

function kindLabel(kind) {
  switch (kind) {
    case 'iap': return t('Identity-Aware Proxy');
    case 'model_armor': return 'Model Armor';
    case 'semantic_governance': return t('Semantic governance');
    case 'custom': return t('Custom authorization engine');
    case 'rule': return t('Allow or deny rule');
    default: return t('Unknown');
  }
}

function accessBadge(g) {
  if (g.accessPath !== 'AGENT_TO_ANYWHERE') return h('span', { class: 'text-muted', title: t('Access authorization applies to outbound traffic.') }, DASH);
  const state = g.access ? g.access.state : 'unknown';
  if (state === 'enforced') return badge(t('Enforced'), 'success', { size: 'sm', dot: true });
  if (state === 'audit') return badge(t('Audit-only'), 'warning', { size: 'sm', dot: true });
  if (state === 'none') return badge(t('Not configured'), 'danger', { size: 'sm', dot: true });
  return badge(t('Unknown'), 'neutral', { size: 'sm' });
}

function contentBadge(g) {
  const state = g.contentSecurity ? g.contentSecurity.state : 'unknown';
  if (state === 'configured') return badge('Model Armor', 'success', { size: 'sm', dot: true });
  if (state === 'none') return badge(t('None'), 'neutral', { size: 'sm' });
  return badge(t('Unknown'), 'neutral', { size: 'sm' });
}

const SEVERITY_TONE = { high: 'danger', medium: 'warning', low: 'info', info: 'neutral' };

function severityLabel(severity) {
  if (severity === 'high') return t('High');
  if (severity === 'medium') return t('Medium');
  if (severity === 'low') return t('Low');
  return t('Note');
}

/** [title, explanation] of a governance finding. */
function findingText(code) {
  switch (code) {
    case 'egress_default_deny': return [t('No authorization policy: all outbound traffic is blocked'), t('An Agent-to-Anywhere gateway without an authorization policy denies every request. Set up access authorization so that agents can reach the destinations they are allowed to use.')];
    case 'no_access_authorization': return [t('No access authorization'), t('Requests are not checked against IAM access policies, so an agent bound to this gateway can reach any destination that the content policies let through.')];
    case 'access_audit_only': return [t('Access authorization is audit-only'), t('Identity-Aware Proxy evaluates and logs every request but does not block the ones it would deny. Review them in the Traffic view, then enforce the policies.')];
    case 'legacy_policy_model': return [t('Legacy allow policies (V1)'), t('Unified Access Policies (V2) are recommended for fine-grained access control.')];
    case 'fail_open': return [t('An authorization extension fails open'), t('When the extension fails or times out, requests continue without that check.')];
    case 'no_content_security': return [t('No content security'), t('Prompts and tool responses that pass through this gateway are not screened by Model Armor.')];
    case 'ingress_unscreened': return [t('Inbound requests are not screened'), t('A Client-to-Agent gateway without a content policy passes every request through. Attach a Model Armor policy to screen prompts.')];
    case 'semantic_audit_only': return [t('Semantic governance is in dry run'), t('Verdicts are logged, but tool calls that violate a policy are not blocked.')];
    case 'no_registry': return [t('No Agent Registry associated'), t('Destinations cannot be matched to registered agents, MCP servers or endpoints, so only policies for unregistered endpoints can allow traffic.')];
    case 'registry_mismatch': return [t('Agent Registry does not match the gateway'), t('A gateway uses the regional Agent Registry of its own project and region.')];
    case 'logging_disabled': return [t('Request logging is turned off'), t('A telemetry policy disables the gateway request log, so allow and deny decisions are not recorded.')];
    case 'no_tracing': return [t('Tracing is not configured'), t('No telemetry policy targets this gateway, so its requests are not exported to Cloud Trace.')];
    case 'no_workloads': return [t('No workload uses this gateway'), t('No Agent Runtime agent or Gemini Enterprise app routes traffic through it.')];
    default: return [String(code), ''];
  }
}

function findingRow(finding, gateway) {
  const [title, text] = findingText(finding.code);
  const cls = finding.severity === 'high' ? 'no' : (finding.severity === 'medium' ? 'warn' : '');
  const iconName = finding.severity === 'high' ? 'alert-circle' : (finding.severity === 'medium' ? 'alert-triangle' : 'info');
  return h('div', { class: 'check-item', role: 'listitem' },
    h('span', { class: ['ci-icon', cls] }, icon(iconName, { size: 16 })),
    h('div', { class: 'ci-text' },
      h('div', null, gateway ? h('span', { class: 'cell-mono' }, `${gateway.name}: `) : null, title),
      text ? h('div', { class: 'ci-sub' }, text) : null),
    badge(severityLabel(finding.severity), SEVERITY_TONE[finding.severity] || 'neutral', { size: 'sm' }));
}

function external(href, label) {
  return h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, label, ' ', icon('external', { size: 12 }));
}

function shortName(resource) {
  return resource ? String(resource).split('/').pop() : null;
}

function mono(text, title) {
  return text ? h('span', { class: 'cell-mono truncate', title: title || text }, text) : h('span', { class: 'text-muted' }, DASH);
}

function gatewayHash(view, key) {
  const path = view === 'gateways' ? '#/gateway' : `#/gateway/${view}`;
  return key ? `${path}?gateway=${encodeURIComponent(key)}` : path;
}

function logsUrl(g) {
  const query = `resource.type="networkservices.googleapis.com/Gateway"\nresource.labels.location="${g.location}"\nresource.labels.gateway_name="${g.name}"`;
  return `https://console.cloud.google.com/logs/query;query=${encodeURIComponent(query)}?project=${encodeURIComponent(g.project)}`;
}

/* ------------------------------------------------------------------ entry */

const VIEWS = ['gateways', 'traffic', 'api'];

export default async function render(ctx) {
  const view = VIEWS.includes(ctx.params[0]) ? ctx.params[0] : 'gateways';
  const state = { inventory: null };
  const header = h('div', { class: 'stack' },
    pageHeader({
      title: t('Agent Gateway'),
      desc: t('The policy enforcement point for agent traffic: which gateways exist, how they are governed, what they allowed and denied, and which agents bypass them.'),
      actions: [
        external(`${DOCS}/agent-gateway-overview`, t('Documentation')),
        button({ label: t('Create gateway'), icon: 'plus', variant: 'primary', onClick: () => openCreate(ctx, state.inventory) }),
      ],
    }),
    tabs([
      { id: 'gateways', label: t('Gateways') },
      { id: 'traffic', label: t('Traffic') },
      { id: 'api', label: t('AI API calls') },
    ], view, (id) => ctx.navigate(gatewayHash(id))));
  if (view === 'traffic') {
    return pageFlow(ctx, {
      header, kpiCount: 5,
      fetcher: () => {
        const raw = ctx.query.get('gateway') || '';
        const gateway = raw && (!store.selectedProject || raw.startsWith(`${store.selectedProject}/`)) ? raw : undefined;
        return fetchRange(ctx, `${BASE}/traffic`, { gateway });
      },
      render: (body, d) => renderTraffic(ctx, body, d),
    });
  }
  if (view === 'api') {
    return pageFlow(ctx, {
      header, kpiCount: 5,
      fetcher: () => fetchRange(ctx, '/api/v1/agent-platform/gateway'),
      render: (body, d) => renderApiCalls(body, d),
    });
  }
  return pageFlow(ctx, {
    header, kpiCount: 4,
    fetcher: () => fetchRange(ctx, BASE),
    render: (body, d) => { state.inventory = d; renderGateways(ctx, body, d); },
  });
}

/* ------------------------------------------------------------------ gateways and posture */

function renderGateways(ctx, body, d) {
  const gateways = d.gateways;
  if (gateways === null || gateways === undefined) {
    const disabled = d.status === 'api_disabled';
    body.append(card({
      body: emptyState({
        icon: 'share', tone: 'warning',
        title: disabled ? t('Agent Gateway is not set up in this project') : t('The gateways could not be read'),
        body: disabled
          ? t('Enable the Network Services, Network Security, Identity-Aware Proxy and Agent Registry APIs in the project, then create the first gateway.')
          : t('Grant the runtime service account the roles Network Services Viewer and Network Security Viewer. See the data source notices above.'),
        action: external(`${DOCS}/set-up-agent-gateway`, t('Set up an Agent Gateway')),
      }),
    }));
    return;
  }
  const k = d.kpis || {};
  const findings = k.findings || {};
  const serious = (findings.high || 0) + (findings.medium || 0);
  body.append(h('div', { class: 'kpi-grid' },
    kpi({
      label: t('Gateways'), value: fmt.int(k.gateways), goodWhen: null,
      meta: h('span', null, t('{e} egress \u00b7 {i} ingress', { e: fmt.int(k.egress), i: fmt.int(k.ingress) })),
      tip: t('Agent Gateway resources in the projects and regions of this workspace.'),
    }),
    kpi({
      label: t('Enforcing access policies'), value: `${fmt.int(k.enforced)} / ${fmt.int(k.egress)}`, goodWhen: null,
      meta: h('span', null, t('{a} audit-only \u00b7 {n} without access authorization', { a: fmt.int(k.audit), n: fmt.int(k.unprotected) })),
      tip: t('Agent-to-Anywhere gateways whose Identity-Aware Proxy policies block denied requests.'),
    }),
    kpi({
      label: t('Governed workloads'), value: isNum(k.workloads) ? `${fmt.int(k.governed)} / ${fmt.int(k.workloads)}` : DASH, goodWhen: null,
      meta: isNum(k.workloads) ? h('span', null, t('{n} bypass every gateway', { n: fmt.int(k.workloads - k.governed) })) : null,
      tip: t('Agent Runtime agents and Gemini Enterprise apps whose outbound traffic goes through a gateway.'),
    }),
    kpi({
      label: t('Findings to resolve'), value: fmt.int(serious), goodWhen: null,
      meta: h('span', null, t('{h} high \u00b7 {m} medium \u00b7 {l} low', { h: fmt.int(findings.high), m: fmt.int(findings.medium), l: fmt.int(findings.low) })),
      tip: t('Governance findings of high and medium severity across all gateways.'),
    })));

  if (!gateways.length) {
    body.append(card({
      body: emptyState({
        icon: 'share',
        title: t('No Agent Gateway yet'),
        body: t('A gateway sits in front of agents (Client-to-Agent) or between agents and the tools, MCP servers and APIs they call (Agent-to-Anywhere), and applies access, content and semantic policies to every request.'),
        action: h('div', { class: 'row' },
          button({ label: t('Create gateway'), icon: 'plus', variant: 'primary', onClick: () => openCreate(ctx, d) }),
          external(`${DOCS}/set-up-agent-gateway`, t('Set up an Agent Gateway'))),
      }),
    }));
  } else {
    const top = [];
    gateways.forEach((g) => (g.findings || []).forEach((f) => { if (f.severity === 'high' || f.severity === 'medium') top.push([f, g]); }));
    top.sort((a, b) => (a[0].severity === b[0].severity ? 0 : (a[0].severity === 'high' ? -1 : 1)));
    if (top.length) {
      body.append(card({
        title: t('Findings to resolve'),
        subtitle: t('Open a gateway to see every finding and what to do about it.'),
        body: h('div', { class: 'checklist', role: 'list' }, top.slice(0, 8).map(([f, g]) => findingRow(f, g)),
          top.length > 8 ? h('p', { class: 'muted' }, t('{n} more in the gateway details', { n: fmt.int(top.length - 8) })) : null),
      }));
    }
    body.append(card({
      title: t('Gateways'), subtitle: t('Select a gateway to see its policies, workloads and observability settings.'), flush: true,
      source: 'networkservices.googleapis.com agentGateways \u00b7 networksecurity.googleapis.com authzPolicies',
      body: dataTable({
        columns: [
          { key: 'name', label: t('Gateway'), render: (g) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main' }, g.name), h('span', { class: 'cell-sub' }, `${g.project} \u00b7 ${g.location}`)), value: (g) => `${g.name} ${g.project} ${g.location}`, csv: (g) => `${g.project}/${g.location}/${g.name}` },
          { key: 'accessPath', label: t('Governed access path'), render: (g) => pathLabel(g.accessPath), value: (g) => g.accessPath },
          { key: 'access', label: t('Access authorization'), render: accessBadge, value: (g) => (g.access ? g.access.state : ''), csv: (g) => (g.access ? g.access.state : '') },
          { key: 'content', label: t('Content security'), render: contentBadge, value: (g) => (g.contentSecurity ? g.contentSecurity.state : ''), csv: (g) => (g.contentSecurity ? g.contentSecurity.state : '') },
          { key: 'workloads', label: t('Workloads'), align: 'num', render: (g) => (g.workloads ? fmt.int(g.workloads.length) : DASH), value: (g) => (g.workloads ? g.workloads.length : null), csv: (g) => (g.workloads ? g.workloads.length : '') },
          { key: 'severity', label: t('Findings'), render: (g) => (g.findings && g.findings.length ? badge(`${severityLabel(g.severity)} \u00b7 ${fmt.int(g.findings.length)}`, SEVERITY_TONE[g.severity] || 'neutral', { size: 'sm' }) : badge(t('None'), 'success', { size: 'sm' })), value: (g) => (g.findings ? g.findings.length : 0), csv: (g) => (g.findings ? g.findings.map((f) => f.code).join('|') : '') },
          { key: 'updateTime', label: t('Updated'), render: (g) => (g.updateTime ? fmt.relative(g.updateTime) : DASH), value: (g) => g.updateTime },
        ],
        rows: gateways, pageSize: 10, exportName: 'agent-gateways', onRowClick: (g) => openGateway(ctx, d, g),
      }),
    }));
  }

  body.append(workloadsCard(d));
  if (gateways.length) body.append(exportsCard(d));
}

function workloadsCard(d) {
  const names = {};
  (d.gateways || []).forEach((g) => { names[g.key] = g.name; });
  const bound = (b) => {
    if (!b) return h('span', { class: 'text-muted' }, DASH);
    return mono(names[b.gateway] || shortName(b.ref), b.ref);
  };
  return card({
    title: t('Workloads and their gateways'),
    subtitle: t('Agent Runtime agents and Gemini Enterprise apps. A workload without an egress gateway reaches tools and APIs without gateway policies.'),
    flush: true, source: 'aiplatform.googleapis.com reasoningEngines \u00b7 discoveryengine.googleapis.com engines',
    body: dataTable({
      columns: [
        { key: 'displayName', label: t('Workload'), render: (w) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main' }, w.displayName), h('span', { class: 'cell-sub' }, `${w.project} \u00b7 ${w.location}`)), value: (w) => `${w.displayName} ${w.project}`, csv: (w) => `${w.project}/${w.location}/${w.displayName}` },
        { key: 'type', label: t('Type'), render: (w) => workloadTypeLabel(w.type), value: (w) => w.type },
        { key: 'egress', label: t('Egress gateway'), render: (w) => bound(w.egress), value: (w) => (w.egress ? w.egress.ref : ''), csv: (w) => (w.egress ? w.egress.ref : '') },
        { key: 'ingress', label: t('Ingress gateway'), render: (w) => bound(w.ingress), value: (w) => (w.ingress ? w.ingress.ref : ''), csv: (w) => (w.ingress ? w.ingress.ref : '') },
        {
          key: 'state', label: t('Status'), value: (w) => (w.egress ? 1 : 0),
          csv: (w) => (w.egress ? 'governed' : (w.bindable === false ? 'redeploy_required' : 'ungoverned')),
          render: (w) => {
            if (w.egress) return badge(t('Governed'), 'success', { size: 'sm', dot: true });
            if (w.bindable === false) return badge(t('Redeploy to bind'), 'neutral', { size: 'sm', title: t('Agents created before 29 April 2026 cannot be bound to a gateway. Deploy the agent again to route it through one.') });
            return badge(t('Not governed'), 'warning', { size: 'sm', dot: true });
          },
        },
      ],
      rows: d.workloads, pageSize: 8, exportName: 'gateway-workloads', initialSort: { key: 'state', dir: 'asc' },
      emptyText: t('No Agent Runtime agent or Gemini Enterprise app was found.'),
    }),
  });
}

function exportsCard(d) {
  const coverLabel = (c) => {
    if (c === 'all') return t('All logs');
    if (c === 'gateway_requests') return t('Gateway requests');
    return t('Data Access audit logs');
  };
  return card({
    title: t('Audit log export'),
    subtitle: t('Log Router sinks whose filter names the gateway request log or the Data Access audit log, for retention and SIEM. Without one, decisions stay only as long as the log bucket keeps them.'),
    flush: true, source: 'logging.googleapis.com sinks',
    body: dataTable({
      columns: [
        { key: 'name', label: t('Sink'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main cell-mono' }, r.name), h('span', { class: 'cell-sub' }, r.project)) },
        { key: 'service', label: t('Destination'), render: (r) => h('div', { class: 'cell-stack' }, h('span', null, r.service || DASH), h('span', { class: 'cell-sub truncate', title: r.destination }, r.destination)) },
        { key: 'covers', label: t('Logs named in the filter'), render: (r) => h('div', { class: 'chips' }, (r.covers || []).map((c) => h('span', { class: 'chip' }, coverLabel(c)))), value: (r) => (r.covers || []).join(' ') },
        { key: 'disabled', label: t('Status'), render: (r) => (r.disabled ? badge(t('Disabled'), 'warning', { size: 'sm' }) : badge(t('Active'), 'success', { size: 'sm', dot: true })), value: (r) => (r.disabled ? 1 : 0) },
      ],
      rows: d.exports, searchable: false, pageSize: 6,
      emptyText: t('No sink exports the gateway logs.'),
    }),
  });
}

/* ------------------------------------------------------------------ gateway details */

function tracingSnippet(g) {
  return [
    `cat > tracing-policy.yaml <<EOF`,
    `name: "projects/${g.project}/locations/${g.location}/telemetryPolicies/${g.name}-tracing"`,
    'telemetryTarget:',
    '  resources:',
    `  - "//networkservices.googleapis.com/projects/${g.project}/locations/${g.location}/agentGateways/${g.name}"`,
    'tracingConfiguration:',
    '  samplingRate: 0.01',
    '  parentBasedSampling:',
    '    enabled: true',
    '    samplingRate: 1.0',
    'EOF',
    `gcloud beta network-services telemetry-policies import ${g.name}-tracing \\`,
    `  --source=tracing-policy.yaml --location=${g.location} --project=${g.project}`,
  ].join('\n');
}

function section(title, ...children) {
  return h('section', { class: 'stack' }, h('h3', { class: 'section-title' }, title), children);
}

function openGateway(ctx, d, g) {
  const egress = g.accessPath === 'AGENT_TO_ANYWHERE';
  const actions = [
    button({ label: t('Edit'), icon: 'edit', size: 'sm', onClick: () => openEdit(ctx, g, drawer) }),
    button({ label: t('Delete'), icon: 'trash', size: 'sm', variant: 'danger', onClick: () => deleteGateway(ctx, g, drawer) }),
  ];
  const drawer = openDrawer({
    title: g.name, subtitle: `${pathLabel(g.accessPath)} \u00b7 ${g.project} \u00b7 ${g.location}`, width: 860,
    actions: h('div', { class: 'row' }, actions),
    content: (body) => {
      if (g.findings && g.findings.length) {
        body.append(section(t('Findings'), h('div', { class: 'checklist', role: 'list' }, g.findings.map((f) => findingRow(f)))));
      } else {
        body.append(banner({ tone: 'success', title: t('No governance findings'), text: t('Access authorization is enforced and no gap was found in the configuration that this page checks.') }));
      }

      const registry = g.registries && g.registries.length
        ? h('div', { class: 'cell-stack' }, g.registries.map((r) => mono(r)))
        : h('span', { class: 'text-muted' }, egress ? t('None') : t('Not used for inbound traffic'));
      body.append(section(t('Overview'), kv([
        [t('Resource name'), mono(g.resource)],
        [t('Deployment mode'), deploymentLabel(g.deployment)],
        [t('Governed access path'), pathLabel(g.accessPath)],
        [t('Agent Registry'), registry],
        g.description ? [t('Description'), g.description] : null,
        g.labels && Object.keys(g.labels).length ? [t('Labels'), h('div', { class: 'chips' }, Object.entries(g.labels).map(([key, value]) => h('span', { class: 'chip' }, `${key}=${value}`)))] : null,
        g.endpoint && g.endpoint.mtls ? [t('mTLS endpoint'), mono(g.endpoint.mtls)] : null,
        g.endpoint && g.endpoint.serviceAccount ? [t('Service Extensions service account'), mono(g.endpoint.serviceAccount)] : null,
        [t('Created'), g.createTime ? fmt.dateTime(g.createTime) : DASH],
        [t('Updated'), g.updateTime ? fmt.dateTime(g.updateTime) : DASH],
      ]),
      h('p', { class: 'muted' }, t('Name, region, governed access path and network attachment cannot be changed after creation. Each gateway governs up to {n} resources of its Agent Registry.', { n: fmt.int((d.limits && d.limits.registryResources) || 5000) }))));

      if (egress) body.append(accessSection(ctx, d, g, drawer));

      const cs = g.contentSecurity || {};
      const sem = g.semantic || {};
      body.append(section(t('AI security'), kv([
        [t('Model Armor'), cs.state === 'configured' ? badge(t('Configured'), 'success', { size: 'sm', dot: true }) : (cs.state === 'none' ? badge(t('Not configured'), 'neutral', { size: 'sm' }) : badge(t('Unknown'), 'neutral', { size: 'sm' }))],
        cs.requestTemplate ? [t('Template for requests'), mono(shortName(cs.requestTemplate), cs.requestTemplate)] : null,
        cs.responseTemplate ? [t('Template for responses'), mono(shortName(cs.responseTemplate), cs.responseTemplate)] : null,
        [t('Semantic governance'), sem.state === 'enforced' ? badge(t('Enforced'), 'success', { size: 'sm', dot: true }) : (sem.state === 'audit' ? badge(t('Dry run'), 'warning', { size: 'sm', dot: true }) : (sem.state === 'none' ? badge(t('Not configured'), 'neutral', { size: 'sm' }) : badge(t('Unknown'), 'neutral', { size: 'sm' })))],
      ]),
      h('p', { class: 'muted' }, t('Model Armor templates and semantic governance policies are attached in the Google Cloud console or with gcloud.'), ' ', external(`${DOCS}/delegate-authorization`, t('Delegate authorization')))));

      const limit = egress ? ((d.limits && d.limits.egressPolicies) || 4) : 1;
      body.append(section(t('Authorization policies'),
        h('p', { class: 'muted' }, egress
          ? t('Request policies run first (identity and access), then content policies. This gateway can have up to {n} authorization policies.', { n: fmt.int(limit) })
          : t('A Client-to-Agent gateway supports one content policy.')),
        dataTable({
          columns: [
            { key: 'name', label: t('Policy'), render: (p) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main cell-mono' }, p.name), p.shared ? h('span', { class: 'cell-sub' }, t('Also targets other gateways')) : null) },
            { key: 'profile', label: t('Evaluates'), render: (p) => (p.profile === 'CONTENT_AUTHZ' ? t('Content') : t('Request')), value: (p) => p.profile },
            { key: 'kind', label: t('Delegates to'), render: (p) => h('div', { class: 'cell-stack' }, h('span', null, p.kind === 'rule' ? (p.action === 'DENY' ? t('Deny rule') : t('Allow rule')) : kindLabel(p.kind)), p.extension && p.extension.service ? h('span', { class: 'cell-sub truncate', title: p.extension.service }, p.extension.service) : null), value: (p) => p.kind },
            { key: 'mode', label: t('Mode'), render: (p) => policyMode(p), value: (p) => (p.extension ? p.extension.mode : '') },
            { key: 'mcp', label: t('MCP methods and tools'), render: (p) => listCell(p.mcp, { mono: true }), value: (p) => (p.mcp || []).join(' ') },
          ],
          rows: g.policies, searchable: false, pageSize: 6,
          emptyText: egress ? t('No authorization policy targets this gateway, so it blocks all outbound traffic.') : t('No authorization policy targets this gateway, so inbound requests pass through.'),
        })));

      const net = g.network || {};
      if (net.attachment || net.connectivityTemplate || (net.dnsDomains && net.dnsDomains.length) || (net.proxies && net.proxies.length)) {
        body.append(section(t('Private connectivity'), kv([
          net.attachment ? [t('Network attachment'), mono(shortName(net.attachment), net.attachment)] : null,
          net.connectivityTemplate ? [t('Connectivity template'), mono(shortName(net.connectivityTemplate), net.connectivityTemplate)] : null,
          net.dnsDomains && net.dnsDomains.length ? [t('DNS peering domains'), listCell(net.dnsDomains, { mono: true })] : null,
          net.dnsNetwork ? [t('DNS peering network'), mono(shortName(net.dnsNetwork), net.dnsNetwork)] : null,
          net.proxies && net.proxies.length ? [t('Attached proxies'), listCell(net.proxies.map(shortName), { mono: true })] : null,
        ])));
      }

      body.append(section(t('Workloads'), dataTable({
        columns: [
          { key: 'displayName', label: t('Workload'), render: (w) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main' }, w.displayName), h('span', { class: 'cell-sub' }, `${w.project} \u00b7 ${w.location}`)) },
          { key: 'type', label: t('Type'), render: (w) => workloadTypeLabel(w.type), value: (w) => w.type },
          { key: 'path', label: t('Direction'), render: (w) => (w.path === 'ingress' ? t('Inbound') : t('Outbound')), value: (w) => w.path },
        ],
        rows: g.workloads, searchable: false, pageSize: 6,
        emptyText: t('No workload routes traffic through this gateway.'),
      })));

      const tel = g.telemetry || {};
      const tracing = tel.state === 'configured'
        ? h('span', null, badge(t('Configured'), 'success', { size: 'sm', dot: true }), ' ', t('{rate} of requests sampled', { rate: fmt.pct(tel.samplingRate, 2) }), tel.parentBased ? ` \u00b7 ${t('follows the sampling decision of the caller')}` : '')
        : (tel.state === 'none' ? badge(t('Not configured'), 'neutral', { size: 'sm' }) : badge(t('Unknown'), 'neutral', { size: 'sm' }));
      body.append(section(t('Observability'), kv([
        [t('Request log'), tel.logging === 'disabled' ? badge(t('Turned off'), 'danger', { size: 'sm', dot: true }) : (tel.logging === 'unknown' ? badge(t('Unknown'), 'neutral', { size: 'sm' }) : badge(t('On'), 'success', { size: 'sm', dot: true }))],
        [t('Tracing (Cloud Trace)'), tracing],
        tel.policy ? [t('Telemetry policy'), mono(tel.policy)] : null,
      ]),
      h('div', { class: 'row wrap' },
        button({ label: t('View traffic'), icon: 'activity', size: 'sm', onClick: () => { drawer.close(); ctx.navigate(gatewayHash('traffic', g.key)); } }),
        external(logsUrl(g), t('Open in Logs Explorer')),
        external(g.consoleUrl, t('Open in Google Cloud console'))),
      tel.state === 'none' ? h('details', { class: 'src-more' },
        h('summary', null, t('Turn on tracing for this gateway')),
        h('div', { class: 'stack' },
          h('p', { class: 'muted' }, t('Tracing is in Preview. Grant the Cloud Trace Agent role to the Compute Engine, Network Security and Agent Gateway service agents, then import a telemetry policy:')),
          codeBlock(tracingSnippet(g)))) : null));
    },
  });
  return drawer;
}

function policyMode(p) {
  const ext = p.extension;
  if (!ext) return h('span', { class: 'text-muted' }, DASH);
  const parts = [];
  if (ext.mode === 'audit') parts.push(badge(p.kind === 'semantic_governance' ? t('Dry run') : t('Audit-only'), 'warning', { size: 'sm' }));
  else if (ext.mode === 'enforced') parts.push(badge(t('Enforced'), 'success', { size: 'sm' }));
  if (ext.failOpen) parts.push(badge(t('Fails open'), 'warning', { size: 'sm', title: t('When the extension fails or times out, requests continue without that check.') }));
  return parts.length ? h('div', { class: 'chips' }, parts) : h('span', { class: 'text-muted' }, DASH);
}

function accessSection(ctx, d, g, drawer) {
  const access = g.access || { state: 'unknown' };
  const rows = [
    [t('Status'), accessBadge(g)],
    access.policyModel ? [t('Policy model'), access.policyModel === 'V2' ? t('Unified Access Policy (V2)') : t('Legacy allow policy (V1)')] : null,
    access.policy ? [t('Authorization policy'), mono(access.policy)] : null,
    access.extension ? [t('Authorization extension'), mono(access.extension)] : null,
  ];
  const buttons = [];
  if (access.state === 'audit') buttons.push(button({ label: t('Enforce policies'), icon: 'shield-check', size: 'sm', variant: 'primary', onClick: () => changeAccess(ctx, g, 'enforce', drawer) }));
  if (access.state === 'enforced') buttons.push(button({ label: t('Switch to audit-only'), icon: 'eye', size: 'sm', onClick: () => changeAccess(ctx, g, 'audit', drawer) }));
  if (access.state === 'none') buttons.push(button({ label: t('Set up access authorization'), icon: 'shield', size: 'sm', variant: 'primary', onClick: () => setUpAccess(ctx, g, drawer) }));
  let note = t('Identity-Aware Proxy checks every outbound request of an agent against the IAM access policies of the destination. Without a policy that allows it, a request is denied.');
  if (access.state === 'audit') note = t('Audit-only: requests are evaluated and logged, but none is blocked. The Traffic view lists the requests that enforcement would block.');
  if (access.state === 'none') note = (g.policies && g.policies.length)
    ? t('No Identity-Aware Proxy policy is attached, so the identity of the calling agent is not checked.')
    : t('No authorization policy is attached, so this gateway blocks all outbound traffic.');
  return section(t('Access authorization'), kv(rows), h('p', { class: 'muted' }, note), buttons.length ? h('div', { class: 'row wrap' }, buttons) : null);
}

/* ------------------------------------------------------------------ management (administrators) */

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    if (signal) signal.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); }, { once: true });
  });
}

/** Waits until a long-running operation of Google Cloud is done. Throws its curated error when it failed. */
async function waitOperation(operation, signal) {
  for (let i = 0; i < POLL_LIMIT; i += 1) {
    await sleep(POLL_MS, signal);
    const res = await api.get(`${BASE}/operation`, { api: operation.api, name: operation.name }, { signal });
    if (res.data.done) {
      if (res.data.failed) throw new Error(serverText(res.data.error) || t('The operation failed in Google Cloud.'));
      return;
    }
  }
  throw new Error(t('The operation is still running in Google Cloud. Check the gateway again in a few minutes.'));
}

/** Repeats a management call that does one step at a time until it reports done. */
async function runSteps(path, body, signal, onStep) {
  let payload = body;
  for (let i = 0; i < 12; i += 1) {
    const res = await api.post(`${BASE}${path}`, payload, { signal });
    const r = res.data;
    if (onStep) onStep(r);
    if (r.status !== 'pending') return r;
    await waitOperation(r.operation, signal);
    if (r.deleting) return r;
    if (r.cleanup) payload = { ...body, cleanup: r.cleanup };
  }
  throw new Error(t('The operation is still running in Google Cloud. Check the gateway again in a few minutes.'));
}

function parseLabels(text) {
  const labels = {};
  String(text || '').split(/[,\n]/).map((part) => part.trim()).filter(Boolean).forEach((part) => {
    const idx = part.indexOf('=');
    labels[(idx < 0 ? part : part.slice(0, idx)).trim()] = idx < 0 ? '' : part.slice(idx + 1).trim();
  });
  return labels;
}

function labelsText(labels) {
  return Object.entries(labels || {}).map(([key, value]) => `${key}=${value}`).join(', ');
}

/** Keeps aria-invalid in step with the native validity of a field once the person has used it. */
function trackValidity(input) {
  const sync = () => { if (input.validity.valid) input.removeAttribute('aria-invalid'); else input.setAttribute('aria-invalid', 'true'); };
  input.addEventListener('blur', sync);
  input.addEventListener('input', () => { if (input.hasAttribute('aria-invalid')) sync(); });
  return input;
}

function openCreate(ctx, inventory) {
  if (!requireAdmin(t('Create an Agent Gateway'), 'networkservices.agentGateways.create')) return;
  const manage = (inventory && inventory.manage) || {};
  const ws = (store.boot && store.boot.workspace) || {};
  const fallbackProjects = (Array.isArray(ws.projects) && ws.projects.length ? ws.projects : [ws.projectId]).filter(Boolean);
  const projects = manage.projects && manage.projects.length ? manage.projects : fallbackProjects;
  const regions = manage.regions || [];
  const defaultProject = (store.selectedProject && projects.includes(store.selectedProject)) ? store.selectedProject : (projects[0] || '');
  const st = { project: defaultProject, location: regions[0] || 'us-central1', path: 'AGENT_TO_ANYWHERE', registry: true, access: 'audit' };
  const controller = new AbortController();
  const errEl = h('div', { class: 'field-error', role: 'alert', id: 'gateway-create-error' });
  const progress = h('div', { class: 'checklist', role: 'status' });
  const nameInput = trackValidity(textInput({
    mono: true, placeholder: 'egress-gateway',
    attrs: { name: 'name', required: true, maxlength: 63, pattern: '[a-z]([a-z0-9-]{0,61}[a-z0-9])?', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'none', 'aria-describedby': 'gateway-create-error' },
  }));
  const regionControl = regions.length
    ? selectInput(regions.map((r) => ({ value: r, label: r })), st.location, (v) => { st.location = v; }, { name: 'location' })
    : trackValidity(textInput({ mono: true, value: st.location, placeholder: 'us-central1', onInput: (v) => { st.location = v.trim(); }, attrs: { name: 'location', required: true, pattern: '[a-z]+(-[a-z]+)+[0-9]+', autocomplete: 'off', spellcheck: 'false' } }));
  const projectControl = projects.length
    ? selectInput(projects.map((p) => ({ value: p, label: p })), st.project, (v) => { st.project = v; }, { name: 'project' })
    : trackValidity(textInput({ mono: true, value: st.project, onInput: (v) => { st.project = v.trim(); }, attrs: { name: 'project', required: true, autocomplete: 'off', spellcheck: 'false' } }));
  const description = h('textarea', { class: 'textarea', rows: 2, maxlength: 1024, name: 'description' });
  const labels = textInput({ mono: true, placeholder: 'env=prod, team=platform', attrs: { name: 'labels', autocomplete: 'off', spellcheck: 'false' } });
  const registryRow = h('label', { class: 'row' },
    switchInput({ checked: true, label: t('Associate the regional Agent Registry'), onChange: (v) => { st.registry = v; } }),
    h('span', null, t('Associate the regional Agent Registry of the project and region (recommended)')));
  const accessField = field({
    label: t('Access authorization'),
    help: t('Identity-Aware Proxy with Unified Access Policies. Start in audit-only mode: every request passes and is logged, so you can see what enforcement would block before turning it on.'),
    control: selectInput([
      { value: 'audit', label: t('Audit-only (recommended to start)') },
      { value: 'enforce', label: t('Enforce policies') },
      { value: 'none', label: t('Set up later (the gateway blocks all traffic until then)') },
    ], st.access, (v) => { st.access = v; }, { name: 'access' }),
  });
  const egressOnly = h('div', { class: 'stack' }, registryRow, accessField);
  const pathField = field({
    label: t('Governed access path'),
    help: t('Agent-to-Anywhere governs what agents call (tools, MCP servers, APIs, other agents). Client-to-Agent governs who calls the agents. It cannot be changed later.'),
    control: selectInput([
      { value: 'AGENT_TO_ANYWHERE', label: t('Agent-to-Anywhere (egress)') },
      { value: 'CLIENT_TO_AGENT', label: t('Client-to-Agent (ingress)') },
    ], st.path, (v) => { st.path = v; egressOnly.hidden = v !== 'AGENT_TO_ANYWHERE'; }, { name: 'accessPath' }),
  });
  const submit = button({ label: t('Create gateway'), icon: 'check', variant: 'primary', type: 'submit' });
  const cancel = button({ label: t('Cancel'), onClick: () => modal.close() });
  const form = h('form', { class: 'stack' },
    h('div', { class: 'field-row' },
      field({ label: t('Project'), control: projectControl }),
      field({ label: t('Region'), help: t('The region of the agents, or the region that the Gemini Enterprise app location requires (global and us: us-central1, eu: europe-west1).'), control: regionControl })),
    field({ label: t('Name'), help: t('Lowercase letters, digits and hyphens, starting with a letter. It cannot be changed later.'), control: nameInput }),
    pathField, egressOnly,
    field({ label: t('Description (optional)'), control: description }),
    field({ label: t('Labels (optional)'), help: t('key=value pairs separated by commas.'), control: labels }),
    progress, errEl,
    h('div', { class: 'row', style: { justifyContent: 'flex-end' } }, cancel, submit));
  const modal = openModal({
    title: t('Create an Agent Gateway'),
    subtitle: t('A Google-managed gateway: Google runs the proxy, you define the policies.'),
    wide: true, body: form, onClose: () => controller.abort(),
  });

  const step = (text, done) => h('div', { class: 'check-item' },
    h('span', { class: ['ci-icon', done ? 'ok' : ''] }, done ? icon('check-circle', { size: 16 }) : h('span', { class: 'spinner' })),
    h('div', { class: 'ci-text' }, text));

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    errEl.textContent = '';
    if (!form.reportValidity()) return;
    const body = {
      project: st.project, location: st.location, name: nameInput.value.trim(), accessPath: st.path,
      description: description.value.trim(), labels: parseLabels(labels.value), registry: st.registry,
    };
    const withAccess = st.path === 'AGENT_TO_ANYWHERE' && st.access !== 'none';
    let created = false;
    setBusy(submit, true, t('Creating'));
    cancel.disabled = true;
    try {
      mount(progress, step(t('Creating the gateway. This can take a few minutes.'), false));
      const res = await api.post(BASE, body, { signal: controller.signal });
      created = true;
      if (res.data.status === 'pending') await waitOperation(res.data.operation, controller.signal);
      mount(progress, step(t('Gateway created'), true));
      if (withAccess) {
        const waiting = step(t('Setting up access authorization'), false);
        progress.append(waiting);
        await runSteps('/access', { project: body.project, location: body.location, name: body.name, mode: st.access }, controller.signal);
        waiting.replaceWith(step(t('Access authorization set up'), true));
      }
      modal.close();
      toast({
        title: t('Gateway created'), tone: 'success', timeout: 9000,
        text: withAccess || st.path !== 'AGENT_TO_ANYWHERE'
          ? `${body.name} (${body.project} \u00b7 ${body.location})`
          : t('{name} blocks all outbound traffic until access authorization is set up.', { name: body.name }),
      });
      ctx.rerender();
    } catch (err) {
      if (isAbort(err)) { if (created) ctx.rerender(); return; }
      setBusy(submit, false);
      cancel.disabled = false;
      if (created) {
        // The gateway exists: only the follow-up step failed. Creating again would be refused.
        mount(progress, step(t('Gateway created'), true));
        errEl.textContent = t('Access authorization was not set up: {error} Open the gateway to finish.', { error: errorText(err) });
        submit.replaceWith(button({ label: t('Close'), variant: 'primary', onClick: () => { modal.close(); ctx.rerender(); } }));
        cancel.remove();
      } else {
        mount(progress);
        errEl.textContent = errorText(err);
      }
    }
  });
  setTimeout(() => nameInput.focus(), 30);
}

function openEdit(ctx, g, drawer) {
  if (!requireAdmin(t('Edit an Agent Gateway'), 'networkservices.agentGateways.update')) return;
  const egress = g.accessPath === 'AGENT_TO_ANYWHERE';
  const hadRegistry = Boolean(g.registries && g.registries.length);
  const st = { registry: hadRegistry };
  const errEl = h('div', { class: 'field-error', role: 'alert' });
  const description = h('textarea', { class: 'textarea', rows: 3, maxlength: 1024, name: 'description' });
  description.value = g.description || '';
  const labels = textInput({ mono: true, value: labelsText(g.labels), placeholder: 'env=prod, team=platform', attrs: { name: 'labels', autocomplete: 'off', spellcheck: 'false' } });
  const save = button({ label: t('Save changes'), icon: 'check', variant: 'primary', type: 'submit' });
  const form = h('form', { class: 'stack' },
    field({ label: t('Description (optional)'), control: description }),
    field({ label: t('Labels (optional)'), help: t('key=value pairs separated by commas.'), control: labels }),
    egress ? h('label', { class: 'row' },
      switchInput({ checked: hadRegistry, label: t('Associate the regional Agent Registry'), onChange: (v) => { st.registry = v; } }),
      h('span', null, t('Associate the regional Agent Registry of the project and region (recommended)'))) : null,
    h('p', { class: 'muted' }, t('Name, region, governed access path and network attachment cannot be changed. To change them, create a new gateway and move the workloads to it.')),
    errEl,
    h('div', { class: 'row', style: { justifyContent: 'flex-end' } }, button({ label: t('Cancel'), onClick: () => modal.close() }), save));
  const modal = openModal({ title: t('Edit {name}', { name: g.name }), subtitle: `${g.project} \u00b7 ${g.location}`, body: form });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    errEl.textContent = '';
    const body = { project: g.project, location: g.location, name: g.name, etag: g.etag || undefined };
    const text = description.value.trim();
    if (text !== (g.description || '')) body.description = text;
    if (labelsText(parseLabels(labels.value)) !== labelsText(g.labels)) body.labels = parseLabels(labels.value);
    if (egress && st.registry !== hadRegistry) body.registry = st.registry;
    if (!('description' in body) && !('labels' in body) && !('registry' in body)) { modal.close(); return; }
    setBusy(save, true, t('Saving'));
    try {
      const res = await api.post(`${BASE}/update`, body);
      if (res.data.status === 'pending') await waitOperation(res.data.operation);
      modal.close();
      if (drawer) drawer.close();
      toast({ title: t('Gateway updated'), text: g.name, tone: 'success' });
      ctx.rerender();
    } catch (err) {
      setBusy(save, false);
      errEl.textContent = errorText(err);
    }
  });
}

async function applyAccess(ctx, g, mode, drawer, title) {
  toast({ title: t('Applying the change in Google Cloud'), text: g.name, tone: 'info' });
  try {
    await runSteps('/access', { project: g.project, location: g.location, name: g.name, mode });
    toast({ title, text: t('{name}: a policy change can take a few minutes to reach every request.', { name: g.name }), tone: 'success', timeout: 9000 });
  } catch (err) {
    toast({ title: t('Action refused'), text: errorText(err), tone: 'danger', timeout: 12000 });
  }
  if (drawer) drawer.close();
  ctx.rerender();
}

async function changeAccess(ctx, g, mode, drawer) {
  if (!requireAdmin(mode === 'enforce' ? t('Enforce access policies') : t('Switch access authorization to audit-only'), 'networkservices.authzExtensions.update')) return;
  const bound = (g.workloads || []).filter((w) => w.path === 'egress').length;
  const ok = await confirmDialog(mode === 'enforce' ? {
    title: t('Enforce access policies on {name}', { name: g.name }),
    message: t('Identity-Aware Proxy will block every outbound request that no IAM access policy allows. {n} workloads use this gateway. An agent fails (error 498) when a platform endpoint it needs, such as the Agent Platform or Cloud Logging APIs, is not allowed yet. Check the requests that would be blocked in the Traffic view first.', { n: fmt.int(bound) }),
    confirmLabel: t('Enforce policies'), tone: 'danger', requireText: g.name,
  } : {
    title: t('Switch {name} to audit-only', { name: g.name }),
    message: t('Requests that the access policies deny will be logged but no longer blocked. {n} workloads use this gateway. Use this to diagnose blocked traffic, and enforce again afterwards.', { n: fmt.int(bound) }),
    confirmLabel: t('Switch to audit-only'), tone: 'danger', requireText: g.name,
  });
  if (!ok) return;
  await applyAccess(ctx, g, mode, drawer, mode === 'enforce' ? t('Access policies enforced') : t('Access authorization is now audit-only'));
}

function setUpAccess(ctx, g, drawer) {
  if (!requireAdmin(t('Set up access authorization'), 'networksecurity.authzPolicies.create')) return;
  const st = { mode: 'audit' };
  const apply = button({ label: t('Set up access authorization'), icon: 'shield', variant: 'primary' });
  const modal = openModal({
    title: t('Set up access authorization for {name}', { name: g.name }),
    subtitle: t('Creates an Identity-Aware Proxy authorization extension (Unified Access Policies) and the authorization policy that attaches it to the gateway.'),
    body: h('div', { class: 'stack' },
      field({
        label: t('Mode'),
        help: t('In audit-only mode every request passes and is logged. Enforcing blocks every request that no IAM access policy allows.'),
        control: selectInput([
          { value: 'audit', label: t('Audit-only (recommended to start)') },
          { value: 'enforce', label: t('Enforce policies') },
        ], st.mode, (v) => { st.mode = v; }),
      }),
      banner({ tone: 'info', text: t('Access policies themselves are IAM Unified Access Policies: they grant an agent identity the permission to reach a destination registered in Agent Registry.') })),
    footer: (el) => append(el, [button({ label: t('Cancel'), onClick: () => modal.close() }), apply]),
  });
  apply.addEventListener('click', async () => {
    modal.close();
    await applyAccess(ctx, g, st.mode, drawer, t('Access authorization set up'));
  });
}

async function deleteGateway(ctx, g, drawer) {
  if (!requireAdmin(t('Delete an Agent Gateway'), 'networkservices.agentGateways.delete')) return;
  const workloads = g.workloads || [];
  if (workloads.length) {
    openModal({
      title: t('{name} is still in use', { name: g.name }),
      body: h('div', { class: 'stack' },
        banner({ tone: 'warning', title: t('Disassociate the workloads first'), text: t('Google Cloud refuses to delete a gateway that agents or Gemini Enterprise apps still route traffic through.') }),
        h('ul', null, workloads.map((w) => h('li', null, `${w.displayName} (${workloadTypeLabel(w.type)}, ${w.project} \u00b7 ${w.location})`))),
        external(`${DOCS}/manage-agent-gateways`, t('How to disassociate workloads'))),
    });
    return;
  }
  const policies = g.policies || [];
  const shared = policies.filter((p) => p.shared);
  const ok = await confirmDialog({
    title: t('Delete {name}', { name: g.name }),
    message: policies.length
      ? t('The gateway and the {n} authorization policies that target it are deleted, together with the authorization extensions that no other policy uses. If an agent outside this workspace still uses the gateway, its traffic is blocked as soon as the policies are gone. This cannot be undone.', { n: fmt.int(policies.length) })
      : t('The gateway is deleted. This cannot be undone.'),
    details: shared.length ? banner({ tone: 'warning', text: t('A policy of this gateway also targets other gateways, so the deletion will be refused. Remove this gateway from that policy first.') }) : null,
    confirmLabel: t('Delete gateway'), tone: 'danger', requireText: g.name,
  });
  if (!ok) return;
  toast({ title: t('Deleting the gateway'), text: g.name, tone: 'info' });
  try {
    await runSteps('/delete', { project: g.project, location: g.location, name: g.name, removePolicies: policies.length > 0 });
    toast({ title: t('Gateway deleted'), text: g.name, tone: 'success' });
  } catch (err) {
    toast({ title: t('Action refused'), text: errorText(err), tone: 'danger', timeout: 12000 });
  }
  if (drawer) drawer.close();
  ctx.rerender();
}

/* ------------------------------------------------------------------ traffic */

const REQUEST_OUTCOMES = {
  allowed: ['Allowed', TONES.success],
  denied: ['Denied', TONES.danger],
  client_error: ['Client error', TONES.warning],
  server_error: ['Server error', TONES.slate],
};

function requestOutcomeName(key) {
  if (key === 'allowed') return t('Allowed');
  if (key === 'denied') return t('Denied');
  if (key === 'client_error') return t('Client error');
  return t('Server error');
}

function decisionName(key) {
  if (key === 'granted') return t('Granted');
  if (key === 'denied') return t('Denied');
  return t('Would be blocked (audit-only)');
}

function renderTraffic(ctx, body, d) {
  const k = d.kpis || {};
  const scope = d.scope || {};
  const gateways = scope.gateways || [];
  if (gateways.length) {
    const picker = selectInput(
      [{ value: '', label: t('All gateways') }, ...gateways.map((g) => ({ value: g.key, label: `${g.name} (${g.project} \u00b7 ${g.location})` }))],
      scope.gateway || '', (v) => ctx.navigate(gatewayHash('traffic', v || null)), { 'aria-label': t('Gateway') });
    body.append(h('div', { class: 'row wrap' }, h('span', { class: 'fw-600' }, t('Gateway')), picker,
      scope.gateway ? h('span', { class: 'muted' }, t('Access decisions are shown for the whole project of the gateway.')) : null));
  } else {
    body.append(banner({ tone: 'info', title: t('No gateway to report on'), text: t('Traffic appears here after a gateway exists and agents route requests through it.'), actions: button({ label: t('Gateways'), size: 'sm', onClick: () => ctx.navigate(gatewayHash('gateways')) }) }));
  }
  const sampled = d.sampled || {};
  if ((sampled.requests && sampled.requests.truncated) || (sampled.decisions && sampled.decisions.truncated)) {
    body.append(banner({ tone: 'warning', title: t('Only the most recent log entries were read'), text: t('The breakdowns below cover the latest {r} requests and {a} access decisions of the period. Choose a shorter period for complete figures.', { r: fmt.int(sampled.requests ? sampled.requests.read : 0), a: fmt.int(sampled.decisions ? sampled.decisions.read : 0) }) }));
  }
  const deniedRate = isNum(k.requests) && k.requests > 0 && isNum(k.denied) ? k.denied / k.requests : null;
  body.append(h('div', { class: 'kpi-grid' },
    kpi({
      label: t('Requests'), value: fmt.compact(k.requests), goodWhen: null,
      meta: k.requestSource ? h('span', null, k.requestSource === 'metrics' ? t('from Cloud Monitoring') : t('from the request log')) : null,
      tip: t('Requests that passed through the gateways, without the CONNECT requests that open a tunnel.'),
    }),
    kpi({
      label: t('Denied by the gateway'), value: fmt.int(k.denied), goodWhen: null,
      meta: isNum(deniedRate) ? h('span', null, t('{p} of requests', { p: fmt.pct(deniedRate, 1) })) : null,
      tip: t('Requests answered with 403 or denied by an authorization policy.'),
    }),
    kpi({
      label: t('Access checks'), value: fmt.compact(k.authorizations), goodWhen: null,
      meta: isNum(k.authorizationFailures) ? h('span', null, t('{n} failed ({p})', { n: fmt.int(k.authorizationFailures), p: fmt.pct(k.failureRate, 1) })) : null,
      tip: t('Identity-Aware Proxy decisions for outbound agent requests (Cloud Audit Logs, Data Access).'),
    }),
    kpi({
      label: t('Would be blocked'), value: fmt.int(k.wouldBlock), goodWhen: null,
      tip: t('Requests that passed in audit-only mode and that enforcement would deny. Resolve them before enforcing.'),
    }),
    kpi({
      label: t('Latency (p95)'), value: fmt.duration(k.latencyP95), goodWhen: null,
      meta: isNum(k.latencyP50) ? h('span', null, t('median {v}', { v: fmt.duration(k.latencyP50) })) : null,
      tip: t('Time a request spends in the gateway and at its destination.'),
    })));

  const row1 = h('div', { class: 'grid' });
  const names = {};
  const colors = {};
  Object.entries(REQUEST_OUTCOMES).forEach(([key, [, color]]) => { names[key] = requestOutcomeName(key); colors[key] = color; });
  const trend = chartCard({
    title: t('Requests by outcome'), subtitle: rangeText(),
    source: k.requestSource === 'metrics' ? 'networkservices.googleapis.com/https/request_count' : 'networkservices.googleapis.com/gateway_requests',
    build: (host) => barChart(host, { ...fromTimeline(d.requestTrend, { names, colors }), stacked: true, height: 260, valueFormat: fmt.int }),
  });
  trend.classList.add('span-8');
  const perGateway = card({
    title: t('Requests by gateway'), subtitle: rangeText(),
    body: hbarList(mapList(d.byGateway, (g) => ({
      label: g.name || DASH, value: g.requests,
      sub: g.denied ? t('{n} denied', { n: fmt.int(g.denied) }) : null,
    })), { format: fmt.compact, onClick: (item, i) => { const g = d.byGateway[i]; if (g && g.key) ctx.navigate(gatewayHash('traffic', g.key)); } }),
  });
  perGateway.classList.add('span-4');
  body.append(row1);
  mountCharts(row1, [trend, perGateway]);

  const row2 = h('div', { class: 'grid' });
  const decisionColors = { granted: TONES.success, denied: TONES.danger, would_block: TONES.warning };
  const decisionNames = { granted: decisionName('granted'), denied: decisionName('denied'), would_block: decisionName('would_block') };
  const decisions = chartCard({
    title: t('Access decisions'), subtitle: t('Identity-Aware Proxy, per day'), source: 'cloudaudit.googleapis.com/data_access (iap.googleapis.com)',
    build: (host) => barChart(host, { ...fromTimeline(d.decisionTrend, { names: decisionNames, colors: decisionColors }), stacked: true, height: 240, valueFormat: fmt.int }),
  });
  decisions.classList.add('span-8');
  const controls = card({
    title: t('Other controls'), subtitle: rangeText(),
    body: h('div', { class: 'stack' },
      h('div', { class: 'stat-row' },
        stat(t('To unregistered destinations'), fmt.int(k.unregistered), t('outbound requests without an Agent Registry match')),
        stat(t('Model Armor matches'), fmt.int(k.armorMatches), t('prompts or responses that matched a filter')),
        stat(t('Semantic governance denials'), fmt.int(k.semanticDenials), t('tool calls with the verdict DENY'))),
      d.extensions && d.extensions.length ? dataTable({
        columns: [
          { key: 'name', label: t('Authorization extension'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-mono' }, r.name), h('span', { class: 'cell-sub' }, kindLabel(r.kind))) },
          { key: 'invocations', label: t('Invocations'), align: 'num', format: fmt.compact },
          { key: 'failedOpen', label: t('Failed open'), align: 'num', render: (r) => (r.failedOpen ? h('span', { class: 'text-danger fw-600' }, fmt.int(r.failedOpen)) : fmt.int(0)), value: (r) => r.failedOpen },
        ],
        rows: d.extensions, searchable: false, pageSize: 5,
      }) : null),
  });
  controls.classList.add('span-4');
  body.append(row2);
  mountCharts(row2, [decisions]);
  row2.append(controls);

  body.append(card({
    title: t('Denied and would-be-blocked access'),
    subtitle: t('Agent identities and the destinations they were refused, or would be refused once policies are enforced. Register the destination and grant the agent access, or keep it blocked.'),
    flush: true,
    body: dataTable({
      columns: [
        { key: 'principal', label: t('Agent identity'), render: (r) => h('span', { class: 'cell-mono truncate', title: r.principal || '' }, r.principal || DASH) },
        { key: 'resource', label: t('Destination'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-mono truncate', title: r.resource || '' }, r.resource || DASH), r.unregistered ? h('span', { class: 'cell-sub' }, t('Not registered in Agent Registry')) : null) },
        { key: 'denied', label: t('Denied'), align: 'num', format: fmt.int },
        { key: 'wouldBlock', label: t('Would be blocked'), align: 'num', format: fmt.int },
        { key: 'lastSeen', label: t('Last seen'), render: (r) => (r.lastSeen ? fmt.relative(r.lastSeen) : DASH), value: (r) => r.lastSeen },
      ],
      rows: d.decisions, pageSize: 8, exportName: 'gateway-access-decisions',
      emptyText: t('No access was denied in this period.'),
    }),
  }));

  const row3 = h('div', { class: 'grid' });
  const destinations = card({
    title: t('Top destinations'), subtitle: t('Hosts that agents and clients reached through the gateways'), flush: true,
    body: dataTable({
      columns: [
        { key: 'host', label: t('Host'), render: (r) => h('span', { class: 'cell-mono truncate', title: r.host }, r.host) },
        { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
        { key: 'denied', label: t('Denied'), align: 'num', render: (r) => (r.denied ? h('span', { class: 'text-danger fw-600' }, fmt.int(r.denied)) : fmt.int(0)), value: (r) => r.denied },
        { key: 'registered', label: t('Agent Registry'), render: (r) => (r.registered === null || r.registered === undefined ? h('span', { class: 'text-muted' }, DASH) : (r.registered ? badge(t('Registered'), 'success', { size: 'sm' }) : badge(t('Unregistered'), 'warning', { size: 'sm' }))), value: (r) => (r.registered ? 1 : 0) },
      ],
      rows: d.destinations, pageSize: 8, exportName: 'gateway-destinations', initialSort: { key: 'requests', dir: 'desc' },
      emptyText: t('No request in this period.'),
    }),
  });
  destinations.classList.add('span-7');
  const mcp = card({
    title: t('MCP methods and tools'), subtitle: t('Model Context Protocol calls that the gateways inspected'), flush: true,
    body: dataTable({
      columns: [
        { key: 'method', label: t('Method'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-mono' }, r.method), r.tool ? h('span', { class: 'cell-sub' }, r.tool) : null), value: (r) => `${r.method} ${r.tool || ''}` },
        { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
        { key: 'denied', label: t('Denied'), align: 'num', format: fmt.int },
      ],
      rows: d.mcp, searchable: false, pageSize: 8, initialSort: { key: 'requests', dir: 'desc' },
      emptyText: t('No MCP call in this period.'),
    }),
  });
  mcp.classList.add('span-5');
  row3.append(destinations, mcp);
  body.append(row3);

  body.append(card({
    title: t('Denied requests'), subtitle: t('Latest 50 requests that a gateway refused'), flush: true,
    source: 'networkservices.googleapis.com/gateway_requests',
    body: dataTable({
      columns: [
        { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time, true), value: (r) => r.time },
        { key: 'gateway', label: t('Gateway'), render: (r) => h('div', { class: 'cell-stack' }, h('span', null, r.gateway || DASH), h('span', { class: 'cell-sub' }, r.location || '')) },
        { key: 'host', label: t('Destination'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-mono truncate', title: `${r.host || ''}${r.path || ''}` }, r.host || DASH), h('span', { class: 'cell-sub truncate', title: r.path || '' }, `${r.method || ''} ${r.path || ''}`.trim())), value: (r) => `${r.host || ''}${r.path || ''}` },
        { key: 'status', label: t('Status'), align: 'num' },
        { key: 'policy', label: t('Policy'), render: (r) => (r.policy ? h('span', { class: 'cell-mono' }, r.policy) : h('span', { class: 'text-muted' }, DASH)) },
        { key: 'mcp', label: t('MCP'), render: (r) => (r.mcp ? h('span', { class: 'cell-mono' }, r.mcp) : h('span', { class: 'text-muted' }, DASH)) },
        { key: 'traceId', label: t('Trace'), csv: false, render: (r) => (r.traceId && r.project ? external(`https://console.cloud.google.com/traces/list?project=${encodeURIComponent(r.project)}&tid=${encodeURIComponent(r.traceId)}`, t('Open')) : h('span', { class: 'text-muted' }, DASH)) },
      ],
      rows: d.deniedRequests, pageSize: 10, exportName: 'gateway-denied-requests',
      emptyText: t('No request was denied in this period.'),
    }),
  }));
}

/* ------------------------------------------------------------------ AI API calls (Cloud Audit Logs) */

const OUTCOMES = {
  success: ['Success', TONES.success],
  client_error: ['Client error', TONES.warning],
  denied: ['Denied', TONES.danger],
  throttled: ['Throttled', TONES.violet],
  server_error: ['Server error', TONES.slate],
};

const OUTCOME_TONE = { success: 'success', client_error: 'warning', denied: 'danger', throttled: 'info', server_error: 'neutral' };

function shortMethod(m) {
  if (!m) return DASH;
  const parts = String(m).split('.');
  return parts.length > 2 ? parts.slice(-2).join('.') : m;
}

function renderApiCalls(body, d) {
  const k = d.kpis || {};
  body.append(banner({ tone: 'info', text: t('Calls to AI service APIs recorded in Cloud Audit Logs: who called which method and how the API answered, whether or not the call passed through an Agent Gateway.') }));
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
      onRowClick: (r) => (r.type === 'user' && r.uid ? openUserDrawer(r.uid, { email: r.principal }) : null),
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
