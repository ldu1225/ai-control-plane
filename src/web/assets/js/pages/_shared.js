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
  confirmDialog, toast, button, kv, emptyState, banner, stat, tagMatrix, tagLegend, segmented, chip, progress, requireAdmin,
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
  notebooklm: 'Gemini Notebook',
  antigravity: 'Antigravity',
  agent_platform: 'Gemini Platform',
};

export function surfaceChips(list) {
  return h('div', { class: 'chips' }, (list || []).map((sf) => h('span', { class: 'chip' }, SURFACE_LABELS[sf] || sf)));
}

const SURFACE_ITEMS = [
  { key: 'gemini_enterprise', short: 'GE', label: 'Gemini Enterprise' },
  { key: 'notebooklm', short: 'NB', label: 'Gemini Notebook' },
  { key: 'antigravity', short: 'AG', label: 'Antigravity' },
  { key: 'agent_platform', short: 'AP', label: 'Gemini Platform' },
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
  WORKFORCE_IDENTITY: 'The user signs in through Workforce Identity Federation (an external identity provider). Usage and cost are counted, but per-user caps cannot act on workforce principals. Restrict access through the identity provider or the workforce pool bindings.',
};

export function reasonLabel(reason) {
  const map = {
    PRIVILEGED_ROLE: 'Privileged role', OTHER_ACCESS_ROLE: 'Other access role', GROUP_GRANT_PRESENT: 'Group or domain grant',
    INHERITED_BINDING: 'Inherited binding', CONFLICTING_BINDINGS: 'Conflicting bindings', POLICY_UNREADABLE: 'Policy unreadable', ANCESTOR_UNCHECKED: 'Ancestors unchecked',
    WORKFORCE_IDENTITY: 'Workforce identity',
  };
  return reason ? t(map[reason] || reason) : null;
}

export function iamBadge(iam, { surfaceContext } = {}) {
  if (!iam || !iam.state) return badge(DASH, 'neutral', { size: 'sm' });
  if (iam.aiBlocked && iam.aiBlocked.active) {
    const b = statusBadge('ALL_AI_BLOCKED', { size: 'sm' });
    b.title = t('All project AI IAM roles and dashboard sign-in are blocked.');
    return b;
  }
  if (surfaceContext === 'users' && iam.state === 'BLOCKED_BY_ADMIN') {
    const b = badge(t('Antigravity blocked'), 'warning', { dot: true, size: 'sm' });
    b.title = t('Only Antigravity (businessaicode.*) is blocked; Gemini Enterprise web access remains enabled.');
    return b;
  }
  const b = statusBadge(iam.state, { size: 'sm' });
  if (iam.reason) b.title = reasonLabel(iam.reason);
  return b;
}

/* ------------------------------------------------------------------ per-user model + cost helpers */

function _formatModelMoney(amount, ccy) {
  if (!isNum(amount)) return DASH;
  return fmt.currency(amount, ccy || 'USD', { precise: true });
}

/**
 * Compact inline breakdown of a user's models with each model's estimated cost (or call count when
 * token cost is not available). Models beyond maxVisible sit inside a native <details> disclosure.
 */
export function modelCostCell(byModel, ccy = 'USD', { maxVisible = 3 } = {}) {
  const list = Array.isArray(byModel) ? byModel.filter((m) => m && m.model) : [];
  if (!list.length) return h('span', { class: 'text-muted' }, DASH);
  const renderRow = (m) => {
    const rowCcy = m.currency || ccy || 'USD';
    const hasCost = isNum(m.cost);
    const amountText = hasCost
      ? `${_formatModelMoney(m.cost, rowCcy)}${m.costPartial ? ' *' : ''}`
      : (isNum(m.requests) ? t('{n} calls', { n: fmt.int(m.requests) }) : DASH);
    const tipParts = [m.model];
    if (isNum(m.requests)) tipParts.push(t('{n} requests', { n: fmt.int(m.requests) }));
    if (isNum(m.tokens)) tipParts.push(t('{n} tokens', { n: fmt.compact(m.tokens) }));
    if (hasCost) tipParts.push(_formatModelMoney(m.cost, rowCcy));
    return h('div', { class: 'model-cost-row', title: tipParts.join(' \u00b7 ') },
      h('span', { class: 'model-cost-name' }, m.model),
      h('span', { class: ['model-cost-amt', hasCost ? '' : 'muted'] }, amountText));
  };
  const head = list.slice(0, maxVisible).map(renderRow);
  const rest = list.slice(maxVisible);
  if (!rest.length) return h('div', { class: 'model-cost-list' }, ...head);
  return h('div', { class: 'model-cost-list' },
    ...head,
    h('details', { class: 'model-cost-more' },
      h('summary', null, t('+{n} more models', { n: fmt.int(rest.length) })),
      h('div', { class: 'model-cost-more-list' }, ...rest.map(renderRow))));
}

/** Progress bar + percentage cell for one model's share of a user's cost. */
export function costShareCell(share) {
  if (!isNum(share)) return h('span', { class: 'text-muted' }, DASH);
  return h('div', { class: 'share-cell' }, progress(Math.min(1, Math.max(0, share)), 'accent'), h('span', { class: 'tabular' }, fmt.pct(share, 0)));
}

/** Live filtered subtotal strip rendered above User x Model tables. */
export function filteredCostSummary(rows, ccy = 'USD', { showUserCount = true } = {}) {
  const list = Array.isArray(rows) ? rows : [];
  const users = new Set(list.map((r) => r.email || r.person).filter(Boolean)).size;
  const models = new Set(list.map((r) => r.model).filter(Boolean)).size;
  const reqs = list.reduce((acc, r) => acc + (isNum(r.requests) ? r.requests : 0), 0);
  let tokSum = 0;
  let hasTok = false;
  let costSum = 0;
  let hasCost = false;
  let resolvedCcy = ccy;
  list.forEach((r) => {
    if (isNum(r.tokens)) { tokSum += r.tokens; hasTok = true; }
    if (isNum(r.cost)) {
      costSum += r.cost;
      hasCost = true;
      if (r.currency) resolvedCcy = r.currency;
    }
  });
  const item = (label, value, emphasis = false) => h('div', { class: 'model-summary-item' },
    h('span', { class: 'model-summary-label' }, label),
    h('span', { class: ['model-summary-value', emphasis ? 'emphasis' : ''] }, value));
  return h('div', { class: 'model-summary-bar', role: 'status', 'aria-live': 'polite' },
    showUserCount ? item(t('Users'), fmt.int(users)) : null,
    item(t('Models'), fmt.int(models)),
    item(t('Requests'), fmt.int(reqs)),
    hasTok ? item(t('Total tokens'), fmt.compact(tokSum)) : null,
    item(t('Estimated cost'), hasCost ? _formatModelMoney(costSum, resolvedCcy) : DASH, hasCost));
}

/* ------------------------------------------------------------------ user 360 drawer */

const DETAIL_PARTS = ['core', 'activity'];
// Antigravity totals come from the core part: the same usage source as the Antigravity page and the model table.
const AGY_METRICS = new Set(['antigravityRequests', 'antigravityTokens']);
const SURFACE_ORDER = ['gemini_enterprise', 'notebooklm', 'antigravity', 'agent_platform'];

function laterOf(a, b) {
  if (!a) return b || null;
  if (!b) return a;
  return new Date(b).getTime() > new Date(a).getTime() ? b : a;
}

/** Joins the two parts of the user details; either may still be loading or have failed. */
function mergeUserDetail(core, activity) {
  const c = core || {};
  const a = activity || {};
  const cu = c.user || {};
  const au = a.user || {};
  const cm = c.metrics || {};
  const am = a.metrics || {};
  const metrics = {};
  new Set([...Object.keys(cm), ...Object.keys(am)]).forEach((k) => {
    const [first, second] = AGY_METRICS.has(k) ? [cm[k], am[k]] : [am[k], cm[k]];
    metrics[k] = isNum(first) ? first : (isNum(second) ? second : null);
  });
  const surfaces = new Set([...(cu.surfaces || []), ...(au.surfaces || [])]);
  return {
    user: {
      uid: cu.uid || au.uid || null,
      email: cu.email || au.email || null,
      lastActive: laterOf(cu.lastActive, au.lastActive),
      surfaces: [...SURFACE_ORDER.filter((sf) => surfaces.has(sf)), ...[...surfaces].filter((sf) => !SURFACE_ORDER.includes(sf))],
    },
    metrics,
    activity: a.activity || null,
    antigravity: c.antigravity || null,
    models: c.models || null,
    modelTotals: c.modelTotals || null,
    agents: a.agents || null,
    files: a.files || null,
    armorEvents: a.armorEvents || null,
    iam: c.iam || null,
  };
}

function mergeWarnings(...metas) {
  const seen = new Set();
  const out = [];
  metas.forEach((meta) => ((meta && meta.warnings) || []).forEach((w) => {
    const key = `${w.source}|${w.code}|${w.message}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push(w);
  }));
  return out;
}

/**
 * User details. `email` is the address of the row that was opened: it is shown at once and sent with the uid, so the
 * server does not have to resolve the uid against every user's activity first (it verifies that both match).
 * The details load as two parts at the same time and each part is shown as soon as it arrives: core (Antigravity
 * usage, models, costs and access) and activity (Gemini Enterprise, Gemini Notebook, Gemini Platform and Model Armor over
 * time, agents and files: the slowest query). Closing the drawer cancels what is still loading.
 */
export function openUserDrawer(uid, { onChanged, email } = {}) {
  const known = typeof email === 'string' && email.includes('@') ? email.trim().toLowerCase() : null;
  const drawer = openDrawer({
    title: known || t('User details'),
    subtitle: t('Loading'),
    width: 820,
    content: (body) => mount(body, skeleton('lines')),
  });
  const lead = drawer.dialog.querySelector('.drawer-heading');
  const titleEl = drawer.dialog.querySelector('.drawer-title');
  const subEl = drawer.dialog.querySelector('.drawer-subtitle');
  let hasAvatar = false;
  const showAvatar = (addr) => {
    if (hasAvatar || !addr || !lead) return;
    lead.prepend(avatar(addr, 'lg'));
    hasAvatar = true;
  };
  showAvatar(known);
  const controller = new AbortController();
  drawer.dialog.addEventListener('close', () => controller.abort());
  const base = known ? { uid, email: known } : { uid };
  const data = {};
  const metas = {};
  const errors = {};
  let view = null;

  const update = () => {
    if (!drawer.dialog.isConnected) return;
    if (DETAIL_PARTS.every((p) => errors[p])) {
      view = null;
      mount(drawer.body, errorState(errors.core, {
        onRetry: () => { mount(drawer.body, skeleton('lines')); DETAIL_PARTS.forEach(fetchPart); },
      }));
      return;
    }
    if (!DETAIL_PARTS.some((p) => data[p])) return; // the first skeleton stays until a part arrives
    const pending = {};
    DETAIL_PARTS.forEach((p) => { pending[p] = !data[p] && !errors[p]; });
    const d = mergeUserDetail(data.core, data.activity);
    const u = d.user;
    if (titleEl) titleEl.textContent = u.email || known || t('Unattributed');
    if (subEl) {
      if (u.lastActive) subEl.textContent = t('Last active {time}', { time: fmt.relative(u.lastActive) });
      else subEl.textContent = (pending.core || pending.activity) ? t('Loading') : t('No recent activity');
    }
    showAvatar(u.email || '?');
    if (!view) view = userDetailView(drawer, onChanged, fetchPart);
    view.render(d, { pending, errors, warnings: mergeWarnings(metas.core, metas.activity) });
  };

  function fetchPart(part) {
    delete errors[part];
    delete data[part];
    api.get('/api/v1/users/detail', rangeParams({ ...base, part }), { signal: controller.signal }).then((res) => {
      data[part] = res.data || {};
      metas[part] = res.meta;
      update();
    }).catch((err) => {
      if (isAbort(err)) return;
      errors[part] = err;
      update();
    });
    if (view) update(); // a retried part shows its placeholders again
  }

  DETAIL_PARTS.forEach(fetchPart);
  return drawer;
}

/** The drawer body: fixed sections filled as the parts arrive, so what is already shown does not move or redraw. */
function userDetailView(drawer, onChanged, retry) {
  const section = () => h('div', { class: 'drawer-section' });
  // Core sections first, activity sections last: the slower activity part never pushes down what is already shown.
  const hosts = {
    notice: section(), head: section(), stats: section(), models: section(), access: section(), agy: section(),
    activity: section(), lists: section(),
  };
  mount(drawer.body, Object.values(hosts));
  const shown = {};
  const stateOf = (part, pending, errors) => {
    if (pending[part]) return 'pending';
    return errors[part] ? 'error' : 'ready';
  };
  const failed = (part, err) => card({ body: errorState(err, { compact: true, onRetry: () => retry(part) }) });
  return {
    render(d, { pending, errors, warnings }) {
      mount(hosts.notice, warningsNotice(warnings));
      mount(hosts.head, h('div', { class: 'row wrap drawer-head-row' },
        surfaceChips(d.user && d.user.surfaces),
        d.iam ? iamBadge(d.iam, { surfaceContext: 'users' }) : null,
        h('span', { class: 'cell-sub drawer-range' }, rangeText())));
      mount(hosts.stats, detailStats(d, pending));
      const core = stateOf('core', pending, errors);
      if (shown.core !== core) {
        shown.core = core;
        if (core === 'pending') {
          mount(hosts.models, card({ title: t('Models used'), subtitle: rangeText(), body: skeleton('table') }));
          mount(hosts.agy);
          mount(hosts.access, card({ title: t('Antigravity access (IAM)'), body: skeleton('lines') }));
        } else if (core === 'error') {
          mount(hosts.models, failed('core', errors.core));
          mount(hosts.agy);
          mount(hosts.access);
        } else {
          mount(hosts.models, modelsSection(d));
          mount(hosts.agy, agySection(d));
          mount(hosts.access, d.iam ? [iamCard(drawer, d, onChanged), workspaceAccessCard(drawer, d, onChanged)] : null);
        }
      }
      const act = stateOf('activity', pending, errors);
      if (shown.activity !== act) {
        shown.activity = act;
        if (act === 'pending') {
          mount(hosts.activity, card({ title: t('Activity'), subtitle: rangeText(), body: skeleton('block', 200) }));
          mount(hosts.lists);
        } else if (act === 'error') {
          mount(hosts.activity, failed('activity', errors.activity));
          mount(hosts.lists);
        } else {
          mount(hosts.activity, activitySection(d));
          mount(hosts.lists, listSections(d));
        }
      }
    },
  };
}

function modelRowsOf(d) {
  if (Array.isArray(d.models) && d.models.length) return d.models;
  const ag = d.antigravity || {};
  return (ag.byModel || []).map((row) => ({
    ...row,
    surface: row.surface || 'antigravity',
    outputTokens: isNum(row.outputTokens) ? row.outputTokens : (isNum(row.tokens) && isNum(row.promptTokens) ? Math.max(row.tokens - row.promptTokens, row.candidateTokens || 0, 0) : row.candidateTokens),
  }));
}

function costOf(d, modelRows) {
  const mt = d.modelTotals || {};
  const fallback = modelRows.reduce((acc, r) => acc + (isNum(r.cost) ? r.cost : 0), 0);
  const hasAnyCost = isNum(mt.cost) || modelRows.some((r) => isNum(r.cost));
  const total = isNum(mt.cost) ? mt.cost : (hasAnyCost ? fallback : null);
  const ccy = mt.currency || (modelRows.find((r) => r.currency) || {}).currency || (d.iam && d.iam.currency) || 'USD';
  return { total, ccy };
}

/** Headline numbers; a value whose part is still loading shows a placeholder bar. */
function detailStats(d, pending) {
  const m = d.metrics || {};
  const rows = modelRowsOf(d);
  const { total, ccy } = costOf(d, rows);
  const show = (loading, text) => (loading ? h('span', { class: ['skeleton', 'skeleton-line', 'stat-skeleton'] }) : text);
  return h('div', { class: 'stat-row' },
    stat(t('Estimated cost'), show(pending.core, isNum(total) ? _formatModelMoney(total, ccy) : DASH), !pending.core && rows.length ? t('{n} models', { n: fmt.int(rows.length) }) : null),
    stat(t('Antigravity tokens'), show(pending.core, fmt.compact(m.antigravityTokens)), pending.core ? null : t('{n} requests', { n: fmt.int(m.antigravityRequests) })),
    stat(t('Gemini Enterprise prompts'), show(pending.activity, fmt.int(m.gePrompts))),
    stat(t('Agent calls'), show(pending.activity, fmt.int(m.agentCalls))),
    stat(t('Gemini Notebook prompts'), show(pending.activity, fmt.int(m.notebooklmPrompts))),
    stat(t('Model Armor blocks'), show(pending.activity, fmt.int(m.armorBlocks))));
}

function modelsSection(d) {
  const modelRows = modelRowsOf(d);
  if (!modelRows.length) return null;
  const { ccy: costCcy } = costOf(d, modelRows);
  const surfaceLabels = { antigravity: 'Antigravity', agent_platform: 'Gemini Platform' };
  const surfaces = Array.from(new Set(modelRows.map((row) => row.surface).filter(Boolean)));
  let activeSurface = 'all';
  const host = h('div');
  const chipBar = h('div', { class: 'chips' });
  const drawModels = () => {
    const filtered = activeSurface === 'all' ? modelRows : modelRows.filter((row) => row.surface === activeSurface);
    if (surfaces.length > 1) {
      mount(chipBar, [['all', t('All surfaces')], ...surfaces.map((sf) => [sf, t(surfaceLabels[sf] || sf)])].map(([val, lbl]) => chip(lbl, {
        pressed: activeSurface === val,
        onClick: () => { activeSurface = val; drawModels(); },
      })));
    }
    mount(host,
      filteredCostSummary(filtered, costCcy, { showUserCount: false }),
      dataTable({
        columns: [
          {
            key: 'model',
            label: t('Model'),
            render: (r) => h('div', { class: 'row wrap' },
              h('span', { class: 'cell-mono' }, r.model || DASH),
              r.credit === false ? badge(t('Paid model'), 'warning', { size: 'sm' }) : null),
            value: (r) => r.model || '',
            csv: (r) => r.model || '',
          },
          {
            key: 'surface',
            label: t('Surface'),
            render: (r) => badge(t(surfaceLabels[r.surface] || r.surface || ''), r.surface === 'antigravity' ? 'accent' : 'info', { size: 'sm' }),
            value: (r) => r.surface || '',
          },
          { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
          { key: 'promptTokens', label: t('Input tokens'), align: 'num', format: fmt.compact, csv: (r) => (isNum(r.promptTokens) ? r.promptTokens : '') },
          { key: 'cachedTokens', label: t('From cache'), align: 'num', format: fmt.compact, csv: (r) => (isNum(r.cachedTokens) ? r.cachedTokens : '') },
          { key: 'outputTokens', label: t('Output tokens'), align: 'num', format: fmt.compact, csv: (r) => (isNum(r.outputTokens) ? r.outputTokens : '') },
          { key: 'tokens', label: t('Total tokens'), align: 'num', render: (r) => (isNum(r.tokens) ? h('span', { class: 'fw-600' }, fmt.compact(r.tokens)) : h('span', { class: 'text-muted' }, DASH)), value: (r) => r.tokens, csv: (r) => (isNum(r.tokens) ? r.tokens : '') },
          {
            key: 'cost',
            label: t('Estimated cost'),
            align: 'num',
            render: (r) => (isNum(r.cost)
              ? h('span', { class: 'fw-600', title: r.costPartial ? t('Some models have no unit price') : null }, _formatModelMoney(r.cost, r.currency || costCcy), r.costPartial ? ' *' : '')
              : h('span', { class: 'text-muted' }, DASH)),
            value: (r) => r.cost,
            csv: (r) => (isNum(r.cost) ? r.cost : ''),
          },
          {
            key: 'costShare',
            label: t('Cost share'),
            align: 'num',
            render: (r) => costShareCell(r.costShare),
            value: (r) => r.costShare,
            csv: (r) => (isNum(r.costShare) ? r.costShare : ''),
          },
        ],
        rows: filtered,
        searchable: modelRows.length > 6,
        pageSize: 10,
        initialSort: { key: 'cost', dir: 'desc' },
        toolbar: surfaces.length > 1 ? chipBar : null,
        exportName: `user-models-${(d.user && d.user.uid) || 'user'}`,
      }));
  };
  drawModels();
  return card({
    title: t('Models used'),
    subtitle: `${t('Per-model requests, tokens and estimated cost for this user')} \u00b7 ${rangeText()}`,
    flush: true,
    body: host,
  });
}

function activitySection(d) {
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
  const c = card({ title: t('Activity'), subtitle: rangeText(), actions: actToggle, body: actHost });
  requestAnimationFrame(() => { actChart = lineChart(actHost, actOpts('workspace')); });
  return c;
}

function agySection(d) {
  const ag = d.antigravity || {};
  if (!((ag.byModel && ag.byModel.length) || (ag.byClient && ag.byClient.length))) return null;
  const modelHost = h('div');
  const grid = h('div', { class: 'grid' },
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
    })());
  requestAnimationFrame(() => donutChart(modelHost, {
    items: (ag.byModel || []).map((x, i) => ({ label: x.model, value: x.tokens, color: colorAt(i) })),
    format: fmt.compact, centerValue: fmt.compact((ag.byModel || []).reduce((a, b) => a + (b.tokens || 0), 0)), centerLabel: t('tokens'), size: 140,
  }));
  return grid;
}

function listSections(d) {
  const out = [];
  if (d.agents && d.agents.length) {
    out.push(card({ title: t('Agents used'), flush: true, body: dataTable({
      columns: [
        { key: 'agent', label: t('Agent'), render: (r) => h('span', { class: 'cell-main' }, r.agent) },
        { key: 'calls', label: t('Calls'), align: 'num', format: fmt.int },
        { key: 'lastUsed', label: t('Last used'), render: (r) => fmt.dateTime(r.lastUsed), value: (r) => r.lastUsed },
      ],
      rows: d.agents, searchable: false, pageSize: 6, initialSort: { key: 'calls', dir: 'desc' },
    }) }));
  }
  if (d.files && d.files.length) {
    out.push(card({ title: t('Uploaded files'), flush: true, body: dataTable({
      columns: [
        { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time), value: (r) => r.time },
        { key: 'fileName', label: t('File'), render: (r) => (r.restricted ? h('span', { class: 'muted' }, t('Restricted')) : (r.fileName ? h('span', { class: 'truncate cell-main', title: r.fileName }, r.fileName) : (r.fileId ? h('span', { class: 'cell-mono text-muted', title: r.fileId }, `file_${r.fileId}`) : h('span', { class: 'text-muted' }, t('Name not recorded'))))) },
        { key: 'mimeType', label: t('Type'), render: (r) => h('span', { class: 'cell-mono truncate', title: r.mimeType || '' }, r.mimeType || DASH) },
      ],
      rows: d.files, searchable: false, pageSize: 6,
    }) }));
  }
  if (d.armorEvents && d.armorEvents.length) {
    out.push(card({ title: t('Model Armor events'), flush: true, body: dataTable({
      columns: [
        { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time), value: (r) => r.time },
        { key: 'verdict', label: t('Verdict'), render: (r) => statusBadge(r.verdict, { size: 'sm' }) },
        { key: 'filters', label: t('Filters'), render: (r) => h('div', { class: 'chips' }, (r.filters || []).map((f) => h('span', { class: 'chip' }, filterName(f)))) },
        { key: 'text', label: t('Content'), render: (r) => (r.restricted ? h('span', { class: 'muted' }, t('Restricted')) : h('span', { class: 'truncate', title: r.text || '' }, r.text || DASH)) },
      ],
      rows: d.armorEvents, searchable: false, pageSize: 6,
    }) }));
  }
  return out;
}

const CAP_REASON_LABELS = {
  tokens: 'Tokens', requests: 'Requests', cost: 'Estimated cost',
  budget: 'Paid budget spent', credit: 'Weekly free credit used up', paid: 'Paid usage this week',
};

function usdText(v) {
  return fmt.currency(v, 'USD', { precise: true });
}

/** Credit caps: a paid budget, null = no limit, 0 = credits only. */
function budgetLabel(b) {
  if (!isNum(b)) return t('No limit');
  return b === 0 ? t('Credits only') : usdText(b);
}

/** A rule in words, with its explicit end when it has one ("Allowed until Mon, Nov 23, 00:00"). */
export function capRuleText(rule) {
  if (!rule) return null;
  if (rule.type === 'block') return t('Always block');
  if (rule.type === 'limit') return rule.until ? t('Individual cap until {time}', { time: fmt.dateTimeDay(rule.until) }) : t('Individual cap');
  return rule.until ? t('Allowed until {time}', { time: fmt.dateTimeDay(rule.until) }) : t('Always allow');
}

const RULE_TAG_STYLE = { allow: ['success', 'shield-check'], block: ['danger', 'lock'], limit: ['accent', 'gauge'] };

/**
 * One exception rule as a single pill: icon and the rule with its exact end, and when `onRevoke` is given a "Revoke"
 * action inside the same pill (after a divider), so the rule and the way to end it read as one control.
 */
export function ruleTag(rule, { onRevoke, email } = {}) {
  if (!rule) return null;
  const [tone, iconName] = RULE_TAG_STYLE[rule.type] || RULE_TAG_STYLE.allow;
  return h('span', { class: ['rule-tag', `rule-tag-${tone}`] },
    h('span', { class: 'rule-tag-label', title: rule.until ? fmt.relative(rule.until) : null },
      icon(iconName, { size: 13 }),
      h('span', { class: 'rule-tag-text' }, capRuleText(rule))),
    onRevoke ? h('button', {
      type: 'button',
      class: 'rule-tag-action',
      'aria-label': email ? t('Revoke the rule of {email}', { email }) : t('Revoke rule'),
      onclick: onRevoke,
    }, t('Revoke')) : null);
}

/**
 * The current cap period with explicit dates: start to the next reset, or the start of a rolling window.
 * `p` holds start / nextReset (drawer capPeriod) or windowStart / nextReset (Antigravity policy).
 */
export function capPeriodText(p) {
  const start = p && (p.start || p.windowStart);
  if (!start) return null;
  const tz = p && (p.resetTimezone || p.timezone);
  if (p.nextReset) return `${fmt.dateTimeDay(start, tz)} \u2013 ${fmt.dateTimeDay(p.nextReset, tz)}`;
  return t('Rolling window since {time}', { time: fmt.dateTimeDay(start, tz) });
}

/** What removing a rule does, in the confirmation and in the toast. */
function ruleRemovalPlan({ email, rule, user, period }) {
  if (rule.type === 'block') {
    return {
      title: t('Remove block rule'),
      message: t('The rule is removed and Antigravity access is restored for {email}. The default cap policy applies again.', { email }),
      confirmLabel: t('Remove'), tone: 'primary', doneTitle: t('Exception rule removed'), after: null,
    };
  }
  if (rule.type === 'limit') {
    return {
      title: t('Remove individual cap'),
      message: t('The individual cap of {email} is removed and the default cap applies again.', { email }),
      confirmLabel: t('Remove'), tone: 'danger', doneTitle: t('Exception rule removed'), after: null,
    };
  }
  const p = period || {};
  const tz = p.resetTimezone || p.timezone;
  const auto = (p.mode || p.enforcementMode) === 'auto';
  const usage = user && isNum(user.capUsage) ? fmt.pct(user.capUsage, 0) : null;
  let after;
  if (user && user.overCap === true) {
    if (!auto) {
      after = t('{email} is over the cap now ({usage}). After you revoke, {email} moves to the review queue, where you choose Suspend or Allow.', { email, usage: usage || DASH });
    } else if (p.nextReset) {
      after = t('{email} is over the cap now ({usage}). Enforcement runs right away to suspend {email} until {time}.', { email, usage: usage || DASH, time: fmt.dateTimeDay(p.nextReset, tz) });
    } else {
      after = t('{email} is over the cap now ({usage}). Enforcement runs right away to suspend {email} until usage falls below the cap.', { email, usage: usage || DASH });
    }
  } else {
    const later = auto
      ? t('If {email} reaches the cap, Antigravity is suspended automatically.', { email })
      : t('If {email} reaches the cap, {email} moves to the review queue.', { email });
    after = usage ? `${t('Nothing changes now: usage is {usage} of the cap.', { usage })} ${later}` : `${t('Nothing changes now.')} ${later}`;
  }
  return {
    title: rule.until ? t('Revoke the allow rule until {time}', { time: fmt.dateTimeDay(rule.until, tz) }) : t('Revoke the always-allow rule'),
    message: `${t('The allow rule of {email} is removed and the default cap applies again.', { email })} ${after}`,
    confirmLabel: t('Revoke'), tone: 'danger', doneTitle: t('Allow rule revoked'), after,
  };
}

/**
 * Confirms and removes one person's exception rule. An allow rule only stops protecting the person (in automatic mode
 * enforcement runs right away; in manual mode they wait for review), a block rule restores access now, and an
 * individual cap falls back to the default cap. `user` carries overCap / capUsage, `period` the cap period (nextReset
 * and the enforcement mode). Returns true when the rule was removed.
 */
export async function removeExceptionRule({ email, rule, user, period }) {
  if (!email || !rule) return false;
  if (!requireAdmin(t('Remove rule'), 'resourcemanager.projects.setIamPolicy')) return false;
  const plan = ruleRemovalPlan({ email, rule, user, period });
  const ok = await confirmDialog({ title: plan.title, message: plan.message, confirmLabel: plan.confirmLabel, tone: plan.tone });
  if (!ok) return false;
  try {
    const res = await api.post('/api/v1/governance/antigravity/exceptions/delete', { email });
    const ch = res.data && res.data.change;
    const run = res.data && res.data.run;
    let text = plan.after;
    if (ch) {
      text = `${email}: ${t(IAM_STATE_LABELS[ch.previousState] || ch.previousState)} \u2192 ${t(IAM_STATE_LABELS[ch.newState] || ch.newState)}`;
    } else if (run && run.started && run.via === 'job') {
      text = t('Enforcement job started to apply the change. The status updates in about a minute.');
    } else if (run && run.started && run.via === 'run' && (run.suspended || run.restored)) {
      text = t('Applied immediately: {s} suspended \u00b7 {r} restored.', { s: fmt.int(run.suspended), r: fmt.int(run.restored) });
    }
    toast({
      title: plan.doneTitle,
      text,
      tone: 'success',
      timeout: text ? 9000 : 4800,
    });
    return true;
  } catch (err) {
    toast({ title: t('Action refused'), text: errorText(err), tone: 'danger', timeout: 9000 });
    return false;
  }
}

function workspaceAccessCard(drawer, d, onChanged) {
  const iam = d.iam;
  const aiBlock = iam.aiBlocked;
  const actions = [];
  if (iam.actions) {
    if (iam.actions.unblockAll) {
      actions.push(button({
        label: t('Restore AI access'),
        icon: 'unlock',
        size: 'sm',
        variant: 'primary',
        onClick: () => allAiAccessAction(drawer, d, 'unblock_all', onChanged),
      }));
    }
    if (iam.actions.blockAll) {
      actions.push(button({
        label: t('Block all AI access'),
        icon: 'lock',
        size: 'sm',
        variant: 'danger',
        onClick: () => allAiAccessAction(drawer, d, 'block_all', onChanged),
      }));
    }
  }
  const hasRoles = Boolean(iam.bindings && iam.bindings.length);
  let statusNode;
  if (aiBlock && aiBlock.active) {
    statusNode = statusBadge('ALL_AI_BLOCKED', { size: 'sm' });
  } else if (aiBlock && (aiBlock.remainingRoles || []).length) {
    statusNode = badge(t('Dashboard blocked (IAM roles remain)'), 'warning', { dot: true, size: 'sm' });
  } else if (hasRoles) {
    statusNode = badge(t('{n} direct IAM roles', { n: fmt.int(iam.bindings.length) }), 'info', { size: 'sm' });
  } else {
    statusNode = badge(t('No direct IAM roles'), 'neutral', { size: 'sm' });
  }
  const removedList = aiBlock && (aiBlock.removedRoles || []).length ? aiBlock.removedRoles.join(', ') : null;
  const restoreList = aiBlock && (aiBlock.restoreRoles || []).length ? aiBlock.restoreRoles.join(', ') : null;
  return card({
    title: t('AI service & workspace access'),
    subtitle: t('Controls Gemini Enterprise, Gemini Notebook, Antigravity and Gemini Platform IAM roles on this project, plus sign-in to this dashboard.'),
    actions: actions.length ? h('div', { class: 'row' }, actions) : null,
    body: h('div', { class: 'stack' },
      kv([
        [t('Overall status'), statusNode],
        aiBlock && aiBlock.at ? [t('Blocked at'), `${fmt.dateTime(aiBlock.at)}${aiBlock.by ? ` \u00b7 ${aiBlock.by}` : ''}`] : null,
        removedList ? [t('Removed IAM roles'), h('span', { class: 'cell-mono' }, removedList)] : null,
        aiBlock && restoreList ? [t('Roles on restore'), h('span', { class: 'cell-mono' }, restoreList)] : null,
      ]),
      aiBlock && (aiBlock.remainingRoles || []).length ? banner({
        tone: 'warning',
        title: t('Direct AI IAM roles still present'),
        text: t('The following AI roles still exist on this user in Google Cloud IAM: {roles}. Select Block all AI access to remove them.', { roles: aiBlock.remainingRoles.join(', ') }),
      }) : null,
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

function iamCard(drawer, d, onChanged) {
  const iam = d.iam;
  const actions = [];
  if (iam.actions) {
    if (iam.actions.allow) actions.push(button({ label: iam.state === 'CAPPED_BY_TOOL' ? t('Restore now') : t('Allow Antigravity'), icon: 'unlock', size: 'sm', onClick: () => userAction(drawer, d, 'allow', onChanged) }));
    if (iam.actions.disallow) actions.push(button({ label: t('Disallow Antigravity'), icon: 'lock', size: 'sm', variant: 'danger', onClick: () => userAction(drawer, d, 'disallow', onChanged) }));
  }
  const remediation = iam.reason && REMEDIATION[iam.reason] ? t(REMEDIATION[iam.reason]) : (iam.remediation ? serverText(iam.remediation) : null);
  const ccy = iam.currency || 'USD';
  const reasons = (iam.overCapReasons || []).map((r) => t(CAP_REASON_LABELS[r] || r)).join(', ');
  const period = capPeriodText(iam.capPeriod);
  const rule = iam.exception;
  const ruleCell = rule ? h('div', { class: 'status-cell-row' },
    ruleTag(rule, {
      email: d.user.email,
      onRevoke: async () => {
        const removed = await removeExceptionRule({
          email: d.user.email, rule, user: { overCap: iam.overCap, capUsage: iam.capUsage }, period: iam.capPeriod,
        });
        if (!removed) return;
        drawer.close();
        if (onChanged) onChanged();
      },
    }),
    rule.until ? h('span', { class: 'cell-sub' }, fmt.relative(rule.until)) : null) : null;
  return card({
    title: t('Antigravity access (IAM)'),
    subtitle: t('Stops or restores Antigravity coding IDE permissions (businessaicode.*) while keeping Gemini Enterprise web chat enabled.'),
    actions: actions.length ? h('div', { class: 'row' }, actions) : null,
    body: h('div', { class: 'stack' },
      kv([
        [t('State'), iamBadge(iam)],
        iam.reason ? [t('Reason'), reasonLabel(iam.reason)] : null,
        ruleCell ? [t('Exception rule'), ruleCell] : null,
        period ? [t('Cap period'), period] : null,
        iam.credit ? [t('Free credit this week'), `${usdText(iam.credit.freeUsedWeek)} / ${usdText(iam.credit.creditWeek)}`] : null,
        iam.credit ? [t('Paid this month'), `${usdText(iam.credit.paidMonth)} / ${budgetLabel(iam.credit.budget)}${reasons ? ` \u00b7 ${reasons}` : ''}`] : null,
        !iam.credit && isNum(iam.capUsage) ? [t('Cap usage'), `${fmt.pct(iam.capUsage, 0)}${reasons ? ` \u00b7 ${t('cap reached: {r}', { r: reasons })}` : ''}`] : null,
        !iam.credit && isNum(iam.estimatedCost) ? [t('Cost in this cap period'), fmt.currency(iam.estimatedCost, ccy, { precise: true })] : null,
        iam.restoreAt ? [t('Access returns'), `${fmt.dateTimeDay(iam.restoreAt)} (${fmt.relative(iam.restoreAt)})`] : null,
      ]),
      remediation ? banner({ tone: 'info', title: t('Manual action required'), text: remediation }) : null),
  });
}

async function allAiAccessAction(drawer, d, action, onChanged) {
  if (!requireAdmin(action === 'block_all' ? t('Block all AI access') : t('Restore AI access'), 'resourcemanager.projects.setIamPolicy')) return;
  const email = d.user.email;
  const blocking = action === 'block_all';
  const restoreRoles = ((d.iam && d.iam.aiBlocked && d.iam.aiBlocked.restoreRoles) || []).join(', ');
  const message = blocking
    ? t('Every direct AI IAM role (Gemini Enterprise, Gemini Notebook, Antigravity, Gemini Platform and marker roles) on this project will be removed for {email}, active dashboard sessions will end immediately, and future dashboard sign-ins will be blocked until restored.', { email })
    : (restoreRoles
      ? t('Dashboard sign-in and project AI IAM roles ({roles}) will be restored for {email}.', { email, roles: restoreRoles })
      : t('Dashboard sign-in and the default Gemini Enterprise access role will be restored for {email}.', { email }));
  const ok = await confirmDialog({
    title: blocking ? t('Block all AI access') : t('Restore AI access'),
    message,
    confirmLabel: blocking ? t('Block all AI access') : t('Restore AI access'),
    tone: blocking ? 'danger' : 'primary',
    requireText: blocking ? 'BLOCK' : null,
  });
  if (!ok) return;
  try {
    const res = await api.post('/api/v1/users/access-action', { uid: d.user.uid, action });
    const r = res.data || {};
    const warn = (r.warnings || []).map((w) => serverText(w)).join(' ');
    if (blocking) {
      const count = (r.removedRoles || []).length;
      toast({
        title: t('All AI access blocked'),
        text: warn
          ? `${r.email}: ${warn}`
          : t('{email}: {n} project AI IAM role(s) removed and dashboard access revoked.', { email: r.email, n: fmt.int(count) }),
        tone: warn ? 'warning' : 'success',
        timeout: warn ? 10000 : 5000,
      });
    } else {
      toast({
        title: t('AI access restored'),
        text: t('{email}: restored {roles} and unblocked dashboard sign-in.', { email: r.email, roles: (r.restoredRoles || []).join(', ') || DASH }),
        tone: 'success',
      });
    }
    drawer.close();
    if (onChanged) onChanged();
  } catch (err) {
    toast({ title: t('Action refused'), text: errorText(err), tone: 'danger', timeout: 9000 });
  }
}

async function userAction(drawer, d, action, onChanged) {
  if (!requireAdmin(action === 'allow' ? t('Allow / Restore Antigravity access') : t('Disallow Antigravity access'), 'resourcemanager.projects.setIamPolicy')) return;
  const email = d.user.email;
  const capped = d.iam && d.iam.state === 'CAPPED_BY_TOOL';
  const allowEnd = d.iam && d.iam.capPeriod ? d.iam.capPeriod.end : null;
  let message;
  if (action === 'disallow') message = t('The access role will be replaced with the administrative block marker for {email}, and an always-block exception rule is recorded. Enforcement runs will not re-enable this user until the rule is removed.', { email });
  else if (capped && allowEnd) message = t('{email} gets Antigravity access back now and keeps it until {time}, even above the cap. An allow rule until then is recorded; you can revoke it earlier in the user details or under Review & access.', { email, time: fmt.dateTimeDay(allowEnd) });
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
