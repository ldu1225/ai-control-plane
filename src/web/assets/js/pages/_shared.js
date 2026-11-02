/**
 * Shared helpers for pages.
 */
import { h, mount, append } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';
import { fmt, change, isNum, DASH } from '../core/format.js';
import { api, isAbort } from '../core/api.js';
import { store, rangeParams, isAdmin, session } from '../core/state.js';
import {
  card, kpi, kpiSkeleton, warningsNotice, errorState, skeleton, openDrawer, avatar, badge, statusBadge, dataTable,
  confirmDialog, toast, button, kv, emptyState, banner, stat, tagMatrix, tagLegend, segmented, requireAdmin,
} from '../core/components.js';
import { errorText, serverText } from '../core/messages.js';
import { lineChart, barChart, donutChart, fromTimeline, colorAt, TONES } from '../core/charts.js';

export function rangeText() {
  const g = store.granularity === 'week' ? t('Weekly') : t('Daily');
  return `${fmt.dateRange(store.range.start, store.range.end)} \u00b7 ${g}`;
}

export function deltaOf(metric) {
  if (!metric || typeof metric !== 'object') return null;
  return change(metric.value, metric.previous);
}

export function val(metric) {
  if (metric === null || metric === undefined) return null;
  if (typeof metric === 'object') return metric.value;
  return metric;
}

/** Fetch a range-based endpoint with standard params and abort signal. */
export function fetchRange(ctx, path, extra = {}) {
  return api.get(path, rangeParams(extra), { signal: ctx.signal });
}

/** Render a page-level skeleton (KPIs + blocks) while the main payload loads. */
export function pageSkeleton(kpiCount = 4, blocks = 2) {
  return h('div', { class: 'stack-lg' },
    h('div', { class: 'kpi-grid' }, Array.from({ length: kpiCount }, kpiSkeleton)),
    h('div', { class: 'grid' }, Array.from({ length: blocks }, () => {
      const c = card({ title: h('span', { class: ['skeleton', 'skeleton-line'] }), body: skeleton('block') });
      c.classList.add('span-6');
      return c;
    })));
}

/**
 * Standard page flow: header -> skeleton -> fetch -> render(content) or error state.
 * Returns whatever `render` returns (cleanup function).
 */
export async function pageFlow(ctx, { header, fetcher, render, kpiCount = 4, blocks = 2 }) {
  const body = h('div', { class: 'stack-lg' });
  mount(ctx.root, header, body);
  mount(body, pageSkeleton(kpiCount, blocks));
  try {
    const res = await fetcher();
    if (ctx.signal.aborted) return null;
    mount(body);
    const notice = warningsNotice(res.meta && res.meta.warnings);
    if (notice) body.append(notice);
    return render(body, res.data, res.meta);
  } catch (err) {
    if (isAbort(err) || ctx.signal.aborted) return null;
    mount(body, card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    return null;
  }
}

export function chartCard({ title, subtitle, source, actions, height = 240, build, cls }) {
  const host = h('div');
  const c = card({ title, subtitle, source, actions, body: host, cls });
  c.chartHost = host;
  c.build = () => {
    build(host);
    if (!host.firstChild) {
      const el = h('div', { class: ['chart-empty', 'unavailable'] }, t('Data source unavailable'));
      el.style.setProperty('height', `${height}px`);
      mount(host, el);
    }
  };
  return c;
}

/** Mount chart cards into a grid, then build charts once attached (accurate widths). */
export function mountCharts(container, cards) {
  append(container, cards);
  requestAnimationFrame(() => cards.forEach((c) => { if (c.build) c.build(); }));
}

export function currencyFmt(currency) {
  return (v) => fmt.currency(v, currency);
}

export function currencyCompact(currency) {
  return (v) => fmt.currency(v, currency, { compact: true });
}

export function emailOrUnattributed(email) {
  return email || t('Unattributed');
}

const SURFACE_LABELS = {
  gemini_enterprise: 'Gemini Enterprise',
  notebooklm: 'NotebookLM',
  antigravity: 'Antigravity',
  agent_platform: 'Agent Platform',
};

export function surfaceChips(list) {
  return h('div', { class: 'chips' }, (list || []).map((sf) => h('span', { class: 'chip' }, SURFACE_LABELS[sf] || sf)));
}

const SURFACE_ITEMS = [
  { key: 'gemini_enterprise', short: 'GE', label: 'Gemini Enterprise' },
  { key: 'notebooklm', short: 'NB', label: 'NotebookLM' },
  { key: 'antigravity', short: 'AG', label: 'Antigravity' },
  { key: 'agent_platform', short: 'AP', label: 'Agent Platform' },
];

/** Legend text for the compact surface matrix (used as a column header tooltip). */
export function surfaceLegend() {
  return tagLegend(SURFACE_ITEMS);
}

/** Fixed-order on/off service tags for dense tables. Unknown surfaces are appended. */
export function surfaceMatrix(list) {
  return tagMatrix(SURFACE_ITEMS, list);
}

export const IAM_STATE_COLORS = {
  ENFORCEABLE_ALLOWED: TONES.success,
  CAPPED_BY_TOOL: TONES.warning,
  BLOCKED_BY_ADMIN: TONES.danger,
  NOT_ENFORCEABLE: TONES.info,
  NO_ACCESS: TONES.neutral,
  UNKNOWN: TONES.slate,
};

export const IAM_STATE_LABELS = {
  ENFORCEABLE_ALLOWED: 'Enforceable',
  CAPPED_BY_TOOL: 'Capped by policy',
  BLOCKED_BY_ADMIN: 'Blocked by admin',
  NOT_ENFORCEABLE: 'Not enforceable',
  NO_ACCESS: 'No access',
  UNKNOWN: 'Unknown',
};

export const REMEDIATION = {
  PRIVILEGED_ROLE: 'The user holds a privileged role (for example Owner, Editor or Discovery Engine Admin) that implicitly grants access. Reduce the privileged role before applying per-user enforcement.',
  OTHER_ACCESS_ROLE: 'Another role grants Antigravity access. Remove it, or add it to the managed access roles in Settings > Governance policy.',
  GROUP_GRANT_PRESENT: 'Access is granted through a Google Group or domain binding. Remove the user from the group, or replace the group binding with individual user bindings.',
  INHERITED_BINDING: 'Access is inherited from a folder or organization IAM policy. Adjust the ancestor policy to enforce limits for this user.',
  CONFLICTING_BINDINGS: 'The user has both the access role and a marker role, or conditional bindings. Resolve the conflicting bindings manually.',
  POLICY_UNREADABLE: 'The project IAM policy could not be read. Grant the control plane identity resourcemanager.projects.getIamPolicy.',
  ANCESTOR_UNCHECKED: 'Folder or organization policies could not be verified. Grant the control plane identity read access to ancestor IAM policies.',
};

export function reasonLabel(reason) {
  const map = {
    PRIVILEGED_ROLE: 'Privileged role', OTHER_ACCESS_ROLE: 'Other access role', GROUP_GRANT_PRESENT: 'Group or domain grant',
    INHERITED_BINDING: 'Inherited binding', CONFLICTING_BINDINGS: 'Conflicting bindings', POLICY_UNREADABLE: 'Policy unreadable', ANCESTOR_UNCHECKED: 'Ancestors unchecked',
  };
  return reason ? t(map[reason] || reason) : null;
}

export function iamBadge(iam) {
  if (!iam || !iam.state) return badge(DASH, 'neutral', { size: 'sm' });
  const b = statusBadge(iam.state, { size: 'sm' });
  if (iam.reason) b.title = reasonLabel(iam.reason);
  return b;
}

/* ------------------------------------------------------------------ user 360 drawer */

export function openUserDrawer(uid, { onChanged } = {}) {
  const drawer = openDrawer({
    title: t('User details'),
    subtitle: t('Loading'),
    width: 820,
    content: (body) => mount(body, skeleton('lines')),
  });
  const params = rangeParams({ uid });
  api.get('/api/v1/users/detail', params).then((res) => {
    const d = res.data;
    const u = d.user || {};
    const titleEl = drawer.dialog.querySelector('.drawer-title');
    const subEl = drawer.dialog.querySelector('.drawer-subtitle');
    titleEl.textContent = u.email || t('Unattributed');
    subEl.textContent = u.lastActive ? t('Last active {time}', { time: fmt.relative(u.lastActive) }) : t('No recent activity');
    const lead = drawer.dialog.querySelector('.drawer-heading');
    lead.prepend(avatar(u.email || '?', 'lg'));
    renderUserDetail(drawer, d, res.meta, onChanged);
  }).catch((err) => {
    if (isAbort(err)) return;
    mount(drawer.body, errorState(err));
  });
  return drawer;
}

function renderUserDetail(drawer, d, meta, onChanged) {
  const m = d.metrics || {};
  const body = drawer.body;
  mount(body);
  const notice = warningsNotice(meta && meta.warnings);
  if (notice) body.append(notice);
  body.append(h('div', { class: 'row wrap' }, surfaceChips(d.user && d.user.surfaces), d.iam ? iamBadge(d.iam) : null));
  body.append(h('div', { class: 'stat-row' },
    stat(t('Gemini Enterprise prompts'), fmt.int(m.gePrompts)),
    stat(t('Agent calls'), fmt.int(m.agentCalls)),
    stat(t('NotebookLM prompts'), fmt.int(m.notebooklmPrompts)),
    stat(t('Antigravity tokens'), fmt.compact(m.antigravityTokens), t('{n} requests', { n: fmt.int(m.antigravityRequests) })),
    stat(t('Model Armor blocks'), fmt.int(m.armorBlocks)),
    stat(t('Files uploaded'), fmt.int(m.fileUploads))));

  const actHost = h('div');
  const WORKSPACE_KEYS = ['gePrompts', 'agentCalls', 'notebooklmPrompts'];
  const ACT_COLORS = { gePrompts: colorAt(0), agentCalls: colorAt(1), notebooklmPrompts: colorAt(2), antigravityRequests: colorAt(3) };
  const agSeries = ((d.activity && d.activity.series) || []).find((sr) => sr.key === 'antigravityRequests');
  const hasAg = Boolean(agSeries && (agSeries.values || []).some((v) => isNum(v) && v > 0));
  const actOpts = (view) => ({
    ...fromTimeline(d.activity, { include: view === 'antigravity' ? ['antigravityRequests'] : WORKSPACE_KEYS, colors: ACT_COLORS }),
    height: 200, integer: true,
  });
  let actChart = null;
  const actToggle = hasAg ? segmented(
    [{ value: 'workspace', label: t('Workspace') }, { value: 'antigravity', label: 'Antigravity' }],
    'workspace', (v) => { if (actChart) actChart.update(actOpts(v)); }, { label: t('Activity') }) : null;
  body.append(card({ title: t('Activity'), subtitle: rangeText(), actions: actToggle, body: actHost }));
  requestAnimationFrame(() => { actChart = lineChart(actHost, actOpts('workspace')); });

  const ag = d.antigravity || {};
  if ((ag.byModel && ag.byModel.length) || (ag.byClient && ag.byClient.length)) {
    const modelHost = h('div');
    body.append(h('div', { class: 'grid' },
      (() => { const c = card({ title: t('Antigravity tokens by model'), body: modelHost }); c.classList.add('span-6'); return c; })(),
      (() => {
        const c = card({ title: t('Antigravity clients'), body: dataTable({
          columns: [
            { key: 'client', label: t('Client'), render: (r) => h('span', { class: 'cell-mono' }, r.client) },
            { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
            { key: 'tokens', label: t('Tokens'), align: 'num', format: fmt.compact },
          ],
          rows: ag.byClient || [], searchable: false, pageSize: 6,
        }), flush: true });
        c.classList.add('span-6');
        return c;
      })()));
    requestAnimationFrame(() => donutChart(modelHost, {
      items: (ag.byModel || []).map((x, i) => ({ label: x.model, value: x.tokens, color: colorAt(i) })),
      format: fmt.compact, centerValue: fmt.compact((ag.byModel || []).reduce((a, b) => a + (b.tokens || 0), 0)), centerLabel: t('tokens'), size: 140,
    }));
  }

  if (d.agents && d.agents.length) {
    body.append(card({ title: t('Agents used'), flush: true, body: dataTable({
      columns: [
        { key: 'agent', label: t('Agent'), render: (r) => h('span', { class: 'cell-main' }, r.agent) },
        { key: 'calls', label: t('Calls'), align: 'num', format: fmt.int },
        { key: 'lastUsed', label: t('Last used'), render: (r) => fmt.dateTime(r.lastUsed), value: (r) => r.lastUsed },
      ],
      rows: d.agents, searchable: false, pageSize: 6, initialSort: { key: 'calls', dir: 'desc' },
    }) }));
  }

  if (d.files && d.files.length) {
    body.append(card({ title: t('Uploaded files'), flush: true, body: dataTable({
      columns: [
        { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time), value: (r) => r.time },
        { key: 'fileName', label: t('File'), render: (r) => (r.restricted ? h('span', { class: 'muted' }, t('Restricted')) : (r.fileName ? h('span', { class: 'truncate cell-main', title: r.fileName }, r.fileName) : (r.fileId ? h('span', { class: 'cell-mono text-muted', title: r.fileId }, `file_${r.fileId}`) : h('span', { class: 'text-muted' }, t('Name not recorded'))))) },
        { key: 'mimeType', label: t('Type'), render: (r) => h('span', { class: 'cell-mono truncate', title: r.mimeType || '' }, r.mimeType || DASH) },
      ],
      rows: d.files, searchable: false, pageSize: 6,
    }) }));
  }

  if (d.armorEvents && d.armorEvents.length) {
    body.append(card({ title: t('Model Armor events'), flush: true, body: dataTable({
      columns: [
        { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time), value: (r) => r.time },
        { key: 'verdict', label: t('Verdict'), render: (r) => statusBadge(r.verdict, { size: 'sm' }) },
        { key: 'filters', label: t('Filters'), render: (r) => h('div', { class: 'chips' }, (r.filters || []).map((f) => h('span', { class: 'chip' }, filterName(f)))) },
        { key: 'text', label: t('Content'), render: (r) => (r.restricted ? h('span', { class: 'muted' }, t('Restricted')) : h('span', { class: 'truncate', title: r.text || '' }, r.text || DASH)) },
      ],
      rows: d.armorEvents, searchable: false, pageSize: 6,
    }) }));
  }

  if (d.iam) body.append(iamCard(drawer, d, onChanged));
}

const CAP_REASON_LABELS = { tokens: 'Tokens', requests: 'Requests', cost: 'Estimated cost' };

function capRuleText(rule) {
  if (!rule) return null;
  if (rule.type === 'block') return t('Always block');
  if (rule.type === 'limit') return t('Individual cap');
  return rule.scope === 'period' ? t('Allowed this period') : t('Always allow');
}

function iamCard(drawer, d, onChanged) {
  const iam = d.iam;
  const actions = [];
  if (iam.actions) {
    if (iam.actions.allow) actions.push(button({ label: iam.state === 'CAPPED_BY_TOOL' ? t('Restore now') : t('Allow access'), icon: 'unlock', size: 'sm', onClick: () => userAction(drawer, d, 'allow', onChanged) }));
    if (iam.actions.disallow) actions.push(button({ label: t('Disallow'), icon: 'lock', size: 'sm', variant: 'danger', onClick: () => userAction(drawer, d, 'disallow', onChanged) }));
  }
  const remediation = iam.reason && REMEDIATION[iam.reason] ? t(REMEDIATION[iam.reason]) : (iam.remediation ? serverText(iam.remediation) : null);
  const ccy = iam.currency || 'USD';
  const reasons = (iam.overCapReasons || []).map((r) => t(CAP_REASON_LABELS[r] || r)).join(', ');
  return card({
    title: t('Antigravity access (IAM)'),
    subtitle: t('Evaluated against the project IAM policy using the agy-admin status model.'),
    actions: actions.length ? h('div', { class: 'row' }, actions) : null,
    body: h('div', { class: 'stack' },
      kv([
        [t('State'), iamBadge(iam)],
        iam.reason ? [t('Reason'), reasonLabel(iam.reason)] : null,
        isNum(iam.capUsage) ? [t('Cap usage'), `${fmt.pct(iam.capUsage, 0)}${reasons ? ` \u00b7 ${t('cap reached: {r}', { r: reasons })}` : ''}`] : null,
        isNum(iam.estimatedCost) ? [t('Estimated cost'), fmt.currency(iam.estimatedCost, ccy, { precise: true })] : null,
        iam.exception ? [t('Exception rule'), `${capRuleText(iam.exception)}${iam.exception.until ? ` \u00b7 ${t('Until {time}', { time: fmt.dateTime(iam.exception.until) })}` : ''}`] : null,
        iam.restoreAt ? [t('Access returns'), `${fmt.dateTime(iam.restoreAt)} (${fmt.relative(iam.restoreAt)})`] : null,
      ]),
      remediation ? banner({ tone: 'info', title: t('Manual action required'), text: remediation }) : null,
      (iam.bindings && iam.bindings.length) ? dataTable({
        columns: [
          { key: 'role', label: t('Role'), render: (r) => h('span', { class: 'cell-mono' }, r.role) },
          { key: 'member', label: t('Member'), render: (r) => h('span', { class: 'cell-mono' }, r.member) },
          { key: 'conditional', label: t('Conditional'), render: (r) => (r.conditional ? badge(t('Yes'), 'warning', { size: 'sm' }) : h('span', { class: 'muted' }, t('No'))) },
        ],
        rows: iam.bindings, searchable: false, pageSize: 8,
      }) : h('p', { class: 'muted' }, t('No direct project bindings for this user.'))),
  });
}

async function userAction(drawer, d, action, onChanged) {
  if (!requireAdmin(action === 'allow' ? t('Allow / Restore Antigravity access') : t('Disallow Antigravity access'), 'resourcemanager.projects.setIamPolicy')) return;
  const email = d.user.email;
  const capped = d.iam && d.iam.state === 'CAPPED_BY_TOOL';
  let message;
  if (action === 'disallow') message = t('The access role will be replaced with the administrative block marker for {email}, and an always-block exception rule is recorded. Enforcement runs will not re-enable this user until the rule is removed.', { email });
  else if (capped) message = t('{email} gets Antigravity access back now and keeps it for the rest of the settlement period, even above the cap. An allowed-this-period exception rule is recorded.', { email });
  else message = t('Marker roles will be removed and the access role will be granted to {email}.', { email });
  const ok = await confirmDialog({
    title: action === 'allow' ? (capped ? t('Restore Antigravity access') : t('Allow Antigravity access')) : t('Disallow Antigravity access'),
    message,
    confirmLabel: action === 'allow' ? (capped ? t('Restore') : t('Allow')) : t('Disallow'),
    tone: action === 'allow' ? 'primary' : 'danger',
    requireText: action === 'disallow' ? 'DISALLOW' : null,
  });
  if (!ok) return;
  try {
    const res = await api.post('/api/v1/governance/antigravity/user-action', { uid: d.user.uid, action });
    const r = res.data;
    toast({ title: t('IAM policy updated'), text: `${r.email}: ${t(IAM_STATE_LABELS[r.previousState] || r.previousState)} \u2192 ${t(IAM_STATE_LABELS[r.newState] || r.newState)}`, tone: 'success' });
    drawer.close();
    if (onChanged) onChanged();
  } catch (err) {
    toast({ title: t('Action refused'), text: errorText(err), tone: 'danger', timeout: 9000 });
  }
}

export const FILTER_NAMES = {
  pi_jailbreak: 'Prompt injection and jailbreak',
  sdp: 'Sensitive data protection',
  rai: 'Responsible AI',
  malicious_uri: 'Malicious URLs',
  csam: 'CSAM',
};

export function filterName(key) {
  return t(FILTER_NAMES[key] || key);
}

export { isNum, DASH };
