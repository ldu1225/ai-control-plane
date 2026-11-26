/**
 * Chargeback: computed AI usage cost allocated to organization units, people and projects, with a month close that
 * freezes the figures of a finished month. The organization structure (principal,unit_path,cost_center) is imported
 * from a CSV: the server checks every row first (dry run) and the administrator applies the checked file.
 * The Monthly budgets view (#/chargeback?view=budgets) shows spending against the budgets of organization units and
 * projects for the running month; administrators edit the budgets there (POST /api/v1/budgets). Project budgets set
 * to Hard cap also get a Google Cloud spend cap; the Google spend caps card shows their state, previews and applies
 * them and lifts a paused cap (/api/v1/budgets/spend-caps).
 */
import { h, mount, frag, downloadText, toCsv } from '../core/dom.js';
import { t, getLocale } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api, ApiError, isAbort } from '../core/api.js';
import { isAdmin, today, store } from '../core/state.js';
import {
  pageHeader, kpi, card, dataTable, badge, button, banner, chip, segmented, selectInput, stat, kv, listCell, userCell,
  principalCell, emptyState, load, warningsNotice, confirmDialog, toast, setBusy, requireAdmin,
  tabs, progress, textInput, errorState, skeleton, openModal,
} from '../core/components.js';
import { errorText, noticeText, serverText } from '../core/messages.js';
import { pageFlow, openUserDrawer, modelCostCell, costShareCell, filteredCostSummary } from './_shared.js';

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
const UNASSIGNED = 'u_unassigned';
/** Usage without a project is reported under this project id. */
const UNKNOWN_PROJECT = 'unknown';
const MAX_CSV_MB = 5;
const MAX_CSV_BYTES = MAX_CSV_MB * 1024 * 1024;
const TEMPLATE_COLUMNS = [
  { key: 'principal', label: 'principal' },
  { key: 'unit_path', label: 'unit_path' },
  { key: 'cost_center', label: 'cost_center' },
];
const TEMPLATE_ROWS = [
  { principal: 'alice@example.com', unit_path: 'Example/Finance/Accounting', cost_center: 'CC-1100' },
  { principal: 'bob@example.com', unit_path: 'Example/Engineering/Platform', cost_center: 'CC-2100' },
  { principal: 'project:example-analytics-prod', unit_path: 'Example/Engineering/Platform', cost_center: 'CC-2100' },
];
const DIFF_KEYS = ['unitsAdded', 'unitsRemoved', 'membersAdded', 'membersRemoved', 'membersMoved', 'projectsChanged'];

/** Breakdown view chosen by the user; kept while switching months. */
let lastView = null;

export default async function render(ctx) {
  const view = ctx.query && ctx.query.get('view') === 'budgets' ? 'budgets' : 'charges';
  const actionSlot = h('div', { class: 'row wrap' });
  const header = pageHeader({
    title: t('Chargeback'),
    desc: t('AI usage cost allocated to organization units, people and projects. A month close freezes the figures of a finished month.'),
    actions: actionSlot,
  });
  const requested = ctx.query ? ctx.query.get('month') : null;
  // The server answers 400 for a future month; such a link falls back to the current month.
  const thisMonth = String(today() || '').slice(0, 7);
  const params = MONTH_RE.test(requested || '') && !(MONTH_RE.test(thisMonth) && requested > thisMonth) ? { month: requested } : null;
  // Monthly charges and monthly budgets are two views of the page; each loads only its own data.
  const top = frag(header, viewTabs(ctx, view, params ? params.month : null));
  if (view === 'budgets') return budgetsPage(ctx, top);
  return pageFlow(ctx, {
    header: top,
    kpiCount: 6,
    blocks: 2,
    fetcher: () => api.get('/api/v1/chargeback/summary', params, { signal: ctx.signal }),
    render: (body, d) => renderPage(ctx, body, d || {}, actionSlot),
  });
}

function renderPage(ctx, body, d, actionSlot) {
  const admin = isAdmin();
  const ccy = d.currency || 'USD';
  const totals = d.totals || {};
  const orgEmpty = Boolean(d.orgModel && d.orgModel.empty);

  mount(actionSlot, monthSelect(ctx, d), statusChip(d), admin ? closeButton(ctx, d) : null);

  body.append(basisBanner(d));
  if (hasUnknownAmounts(d)) {
    const affected = isNum(totals.unknownUsers) && totals.unknownUsers > 0 ? t('People affected: {n}', { n: fmt.int(totals.unknownUsers) }) : null;
    body.append(banner({
      tone: 'warning',
      title: t('Some amounts are unknown'),
      text: [t('A model used in this month has no unit price. Amounts that need it are shown as a dash and left out of the totals. Add the price or a model alias in Settings > Data collection and cost.'), affected].filter(Boolean).join(' '),
      actions: admin ? button({ label: t('Pricing settings'), size: 'sm', onClick: () => ctx.navigate('#/settings/data-collection') }) : null,
    }));
  }
  let emptyBanner = null;
  const org = orgCard(ctx, d, (tree) => { if (tree.empty === false && emptyBanner) emptyBanner.remove(); });
  if (orgEmpty) {
    emptyBanner = banner({
      tone: 'info',
      title: t('No organization structure yet'),
      text: admin
        ? t('Import a CSV that maps people and projects to organization units to see costs by unit. Until then, costs are shown by project and person.')
        : t('Costs are shown by project and person until an administrator imports the organization structure.'),
      actions: admin ? button({ label: t('Import organization CSV'), icon: 'upload', size: 'sm', variant: 'primary', onClick: () => org.focusImport() }) : null,
    });
    body.append(emptyBanner);
  }

  const breakdown = breakdownCard(ctx, d, orgEmpty);
  const unassignedUsers = isNum(totals.unassignedUsers) ? totals.unassignedUsers : 0;
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Total charged back'), value: moneyKpi(totals.total, ccy), tip: t('Charged usage plus seat cost for the month.') }),
    kpi({ label: t('Usage cost'), value: moneyKpi(totals.cost, ccy), tip: t('List cost: logged tokens x the unit price of each model.') }),
    kpi({
      label: t('Included credit applied'),
      value: moneyKpi(totals.credit, ccy),
      meta: d.allocationMode === 'list' ? h('span', null, t('Not applied in list mode')) : null,
      tip: t('Included credit deducted from the usage cost before it is charged back.'),
    }),
    kpi({
      label: t('Seat cost'),
      value: d.seatPricing ? moneyKpi(totals.seat, ccy) : t('Off'),
      tip: d.seatPricing ? t('Monthly price of each assigned license seat, by edition.') : t('Seat pricing is off, so license seats are not charged back.'),
    }),
    kpi({
      label: t('People'),
      value: fmt.int(totals.users),
      meta: h('span', null, `${t('Requests {v}', { v: fmt.int(totals.requests) })} \u00b7 ${t('Tokens {v}', { v: fmt.compact(totals.tokens) })}`),
    }),
    kpi({
      label: t('Unassigned'),
      value: moneyKpi(totals.unassignedTotal, ccy),
      meta: h('span', null, t('People without a unit: {n}', { n: fmt.int(totals.unassignedUsers) })),
      tip: t('Charges of people and projects that no organization unit covers.'),
      onClick: unassignedUsers > 0 ? () => breakdown.showUnassigned() : null,
    })));
  body.append(breakdown.el, org.el, historyCard(ctx, d));
  return null;
}

/* ------------------------------------------------------------------ monthly budgets (organization units and projects) */

const UNIT_ACTIONS = ['suspend', 'review', 'notify'];
const PROJECT_ACTIONS = ['notify', 'hard_cap'];
const HARD_CAP_THRESHOLDS = [50, 80, 100];
const PROJECT_ID_RE = /^[a-z][a-z0-9-]{4,61}[a-z0-9]$/;

function viewTabs(ctx, view, month) {
  return tabs([
    { id: 'charges', label: t('Monthly charges') },
    { id: 'budgets', label: t('Monthly budgets') },
  ], view, (id) => {
    if (id === view) return;
    if (id === 'budgets') ctx.navigate('#/chargeback?view=budgets');
    else ctx.navigate(month ? `#/chargeback?month=${month}` : '#/chargeback');
  });
}

function actionLabel(action) {
  switch (action) {
    case 'suspend': return t('Suspend people');
    case 'review': return t('Send to review');
    case 'notify': return t('Notify only');
    case 'hard_cap': return t('Hard cap (Google spend cap)');
    default: return action ? String(action) : DASH;
  }
}

function actionBadge(action) {
  const tone = { suspend: 'danger', review: 'warning', notify: 'info', hard_cap: 'accent' }[action] || 'neutral';
  return badge(actionLabel(action), tone, { size: 'sm' });
}

function actionOptions(list, current) {
  const values = current && !list.includes(current) ? [...list, current] : list;
  return values.map((v) => ({ value: v, label: actionLabel(v) }));
}

function budgetRatio(r) {
  if (isNum(r.ratio)) return r.ratio;
  if (isNum(r.spent) && isNum(r.budget) && r.budget > 0) return r.spent / r.budget;
  return isNum(r.budget) && r.budget === 0 && isNum(r.spent) && r.spent > 0 ? 1 : null;
}

function budgetBar(r) {
  const ratio = budgetRatio(r);
  if (!isNum(ratio)) return h('span', { class: 'text-muted' }, DASH);
  const tone = r.usedUp || ratio >= 1 ? 'danger' : ratio >= 0.8 ? 'warning' : 'success';
  return h('div', { class: 'rt-budget-bar' }, progress(Math.min(1, ratio), tone), h('span', { class: 'tabular nowrap' }, fmt.pct(ratio, 0)));
}

function budgetState(r) {
  const ratio = budgetRatio(r);
  if (r.usedUp) return badge(t('Used up'), 'danger', { dot: true, size: 'sm' });
  if (isNum(ratio) && ratio >= 0.8) return badge(t('Near the limit'), 'warning', { dot: true, size: 'sm' });
  return badge(t('Within budget'), 'success', { dot: true, size: 'sm' });
}

function crossedCell(list) {
  const items = (Array.isArray(list) ? list : []).filter((x) => isNum(x));
  if (!items.length) return h('span', { class: 'text-muted' }, DASH);
  return h('div', { class: 'chips' }, items.map((x) => chip(`${fmt.int(x)}%`)));
}

function budgetUnitText(r, unitById) {
  if (r.unitId === UNASSIGNED) return t('Unassigned');
  if (r.path) return String(r.path);
  const u = unitById.get(r.unitId);
  return u ? (u.path || u.name || u.id) : (r.unitId ? t('Unknown unit ({id})', { id: r.unitId }) : DASH);
}

function budgetStatusCard(doc, status, unitById, month) {
  const meta = [
    doc.updatedAt ? t('Saved {time} by {user}', { time: fmt.dateTime(doc.updatedAt), user: doc.updatedBy || DASH }) : null,
    isNum(doc.version) ? t('Version {v}', { v: fmt.int(doc.version) }) : null,
  ].filter(Boolean).join(' \u00b7 ');
  const footer = meta ? h('span', { class: 'muted' }, meta) : null;
  if (!status) {
    return card({
      title: t('Spending against budgets'),
      subtitle: monthName(month),
      body: emptyState({ icon: 'gauge', compact: true, title: t('No budget status yet'), body: t('Spending appears here once a budget is saved and the usage of this month has been read.') }),
      footer,
    });
  }
  const units = Array.isArray(status.units) ? status.units.filter(Boolean) : [];
  const projects = Array.isArray(status.projects) ? status.projects.filter(Boolean) : [];
  const common = [
    { key: 'action', label: t('Action'), render: (r) => actionBadge(r.action), value: (r) => r.action || '' },
    { key: 'spent', label: t('Paid this month'), align: 'num', render: (r) => moneyCell(r.spent, 'USD', true), value: (r) => r.spent },
    { key: 'budget', label: t('Monthly budget'), align: 'num', render: (r) => moneyCell(r.budget, 'USD'), value: (r) => r.budget },
    { key: 'ratio', label: t('Used'), width: '200px', render: budgetBar, value: (r) => budgetRatio(r), csv: (r) => budgetRatio(r) },
    { key: 'crossed', label: t('Alerts reached'), sortable: false, render: (r) => crossedCell(r.crossed), csv: (r) => (Array.isArray(r.crossed) ? r.crossed.join('|') : '') },
    { key: 'state', label: t('State'), render: budgetState, value: (r) => (r.usedUp ? 2 : (budgetRatio(r) >= 0.8 ? 1 : 0)) },
  ];
  return card({
    title: t('Spending against budgets'),
    subtitle: t('{month}: paid usage in USD beyond the free credit', { month: monthName(month) }),
    flush: true,
    body: h('div', null,
      dataTable({
        title: t('Organization units'),
        columns: [{ key: 'unit', label: t('Unit'), render: (r) => h('span', { class: 'truncate', title: budgetUnitText(r, unitById) }, budgetUnitText(r, unitById)), value: (r) => budgetUnitText(r, unitById) }, ...common],
        rows: units, pageSize: 10, searchable: units.length > 10, initialSort: { key: 'ratio', dir: 'desc' },
        exportName: `budget-units-${month}`, emptyText: t('No unit budget.'),
      }),
      dataTable({
        title: t('Projects'),
        columns: [{
          key: 'project', label: t('Project'),
          render: (r) => h('div', { class: 'cell-stack' }, projectCell(r.project),
            isNum(r.cost) ? h('span', { class: 'cell-sub' }, t('List cost {v}', { v: fmt.currency(r.cost, 'USD') })) : null),
          value: (r) => r.project || '',
        }, ...common],
        rows: projects, pageSize: 10, searchable: projects.length > 10, initialSort: { key: 'ratio', dir: 'desc' },
        exportName: `budget-projects-${month}`, emptyText: t('No project budget.'),
      })),
    footer,
  });
}

function amountOf(input) {
  const raw = input.value.trim();
  if (raw === '') return null;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 && n <= 1e9 ? n : null;
}

/** "50, 80, 100" -> [50, 80, 100]; null when a value is not a whole percentage from 1 to 100. */
function parseThresholds(raw) {
  const parts = String(raw || '').split(/[\s,;%]+/).map((x) => x.trim()).filter(Boolean);
  if (!parts.length || parts.length > 10) return null;
  const nums = parts.map(Number);
  if (nums.some((n) => !Number.isInteger(n) || n < 1 || n > 100)) return null;
  return [...new Set(nums)].sort((a, b) => a - b);
}

function thresholdsText(list, fallback) {
  if (Array.isArray(list) && list.length) return list.join(', ');
  return (Array.isArray(fallback) && fallback.length ? fallback : HARD_CAP_THRESHOLDS).join(', ');
}

/** Unit and project budget rows; administrators save them with POST /api/v1/budgets, viewers read them. */
function budgetEditor(ctx, doc, units, unitById, admin) {
  const nodes = Array.isArray(doc.nodes) ? doc.nodes.filter((n) => n && typeof n === 'object') : [];
  // Nodes of other kinds are sent back unchanged, so saving here never drops them.
  const others = nodes.filter((n) => n.scope !== 'unit' && n.scope !== 'project');
  // The server names the actions and default thresholds it accepts; the constants cover an older server.
  const offered = (scope, fallback) => (doc.actions && Array.isArray(doc.actions[scope]) && doc.actions[scope].length ? doc.actions[scope] : fallback);
  const unitActions = offered('unit', UNIT_ACTIONS);
  const projectActions = offered('project', PROJECT_ACTIONS);
  const defaultThresholds = Array.isArray(doc.defaultThresholds) && doc.defaultThresholds.length ? doc.defaultThresholds : HARD_CAP_THRESHOLDS;
  const unitRows = [];
  const projectRows = [];
  const err = h('div', { class: 'field-error', role: 'alert' });
  const head = (first) => h('div', null, h('div', { class: ['price-row', 'price-head'], 'aria-hidden': 'true' },
    h('span', null, first), h('span', null, t('USD per month')), h('span', null, t('Action')), h('span', null, t('Alert thresholds (%)')), h('span')));
  const unitHead = head(t('Unit'));
  const projectHead = head(t('Project'));
  const unitList = h('div', { class: 'price-rows' });
  const projectList = h('div', { class: 'price-rows' });
  const unitEmpty = h('div', { class: 'price-empty' }, units.length
    ? t('No unit budget yet.')
    : t('Import the organization structure first (Monthly charges > Organization structure) to set unit budgets.'));
  const projectEmpty = h('div', { class: 'price-empty' }, t('No project budget yet.'));
  const listId = `budget-projects-${Math.random().toString(36).slice(2, 8)}`;
  const wsProjects = (store.boot && store.boot.workspace && Array.isArray(store.boot.workspace.projects)) ? store.boot.workspace.projects : [];
  const known = [...new Set([...wsProjects, ...nodes.filter((n) => n.scope === 'project').map((n) => n.project)].filter(Boolean))];
  const datalist = h('datalist', { id: listId }, known.map((p) => h('option', { value: p })));
  const sync = () => {
    unitHead.hidden = unitRows.length === 0;
    unitEmpty.hidden = unitRows.length > 0;
    projectHead.hidden = projectRows.length === 0;
    projectEmpty.hidden = projectRows.length > 0;
  };
  const amountInput = (node) => textInput({ value: isNum(node.monthlyPaidUsd) ? String(node.monthlyPaidUsd) : '', type: 'number', attrs: { min: 0, step: 'any', inputmode: 'decimal', placeholder: 'USD', 'aria-label': t('USD per month'), disabled: !admin || null } });
  const thresholdInput = (node) => textInput({ value: thresholdsText(node.thresholds, defaultThresholds), mono: true, attrs: { placeholder: '50, 80, 100', maxlength: 60, 'aria-label': t('Alert thresholds (%)'), disabled: !admin || null } });
  const removeButton = (rows, r) => {
    if (!admin) return h('span');
    return button({
      icon: 'trash', variant: 'ghost', size: 'sm', title: t('Remove'),
      onClick: () => {
        rows.splice(rows.indexOf(r), 1);
        r.el.remove();
        sync();
      },
    });
  };
  const unitOptions = (selected) => {
    // The Unassigned unit collects usage that no organization rule attributes; it can have a budget too.
    const opts = [{ value: '', label: t('Choose a unit') }]
      .concat(units.map((u) => ({ value: u.id, label: u.path || u.name || u.id })))
      .concat([{ value: UNASSIGNED, label: t('Unassigned') }]);
    if (selected && selected !== UNASSIGNED && !unitById.has(selected)) opts.push({ value: selected, label: t('Unknown unit ({id})', { id: selected }) });
    return opts;
  };
  const addUnitRow = (node = {}) => {
    const r = { unitId: node.unitId ? String(node.unitId) : '', action: node.action || 'notify' };
    r.sel = selectInput(unitOptions(r.unitId), r.unitId, (v) => { r.unitId = v; r.el.classList.remove('invalid'); }, { disabled: !admin || null, 'aria-label': t('Unit') });
    r.amount = amountInput(node);
    r.actionSel = selectInput(actionOptions(unitActions, r.action), r.action, (v) => { r.action = v; }, { disabled: !admin || null, 'aria-label': t('Action') });
    r.thresholds = thresholdInput(node);
    r.el = h('div', { class: 'price-row' }, r.sel, r.amount, r.actionSel, r.thresholds, removeButton(unitRows, r));
    [r.amount, r.thresholds].forEach((x) => x.addEventListener('input', () => r.el.classList.remove('invalid')));
    unitRows.push(r);
    unitList.append(r.el);
    sync();
    return r;
  };
  const addProjectRow = (node = {}) => {
    const r = { action: node.action || 'notify' };
    r.project = textInput({ value: node.project || '', mono: true, attrs: { list: listId, placeholder: t('Project ID'), maxlength: 100, spellcheck: 'false', autocomplete: 'off', 'aria-label': t('Project'), disabled: !admin || null } });
    r.amount = amountInput(node);
    r.thresholds = thresholdInput(node);
    // Google sends spend cap alerts at fixed thresholds, so a hard cap has no thresholds of its own.
    const syncCap = () => {
      const cap = r.action === 'hard_cap';
      r.thresholds.disabled = !admin || cap;
      if (cap) r.thresholds.value = HARD_CAP_THRESHOLDS.join(', ');
    };
    r.actionSel = selectInput(actionOptions(projectActions, r.action), r.action, (v) => { r.action = v; syncCap(); }, { disabled: !admin || null, 'aria-label': t('Action') });
    syncCap();
    r.el = h('div', { class: 'price-row' }, r.project, r.amount, r.actionSel, r.thresholds, removeButton(projectRows, r));
    [r.project, r.amount, r.thresholds].forEach((x) => x.addEventListener('input', () => r.el.classList.remove('invalid')));
    projectRows.push(r);
    projectList.append(r.el);
    sync();
    return r;
  };
  nodes.filter((n) => n.scope === 'unit').forEach((n) => addUnitRow(n));
  nodes.filter((n) => n.scope === 'project').forEach((n) => addProjectRow(n));
  sync();

  const addUnit = admin ? button({ label: t('Add unit budget'), icon: 'plus', size: 'sm', disabled: !units.length, onClick: () => addUnitRow().sel.querySelector('select').focus() }) : null;
  const addProject = admin ? button({ label: t('Add project budget'), icon: 'plus', size: 'sm', onClick: () => addProjectRow().project.focus() }) : null;

  const collect = () => {
    const out = [];
    const fail = (r, msg, target) => {
      r.el.classList.add('invalid');
      err.textContent = msg;
      if (target) target.focus();
      return null;
    };
    const seenUnits = new Set();
    for (let i = 0; i < unitRows.length; i += 1) {
      const r = unitRows[i];
      const n = i + 1;
      const sel = r.sel.querySelector('select');
      if (!r.unitId) return fail(r, t('Unit budget {n}: choose a unit.', { n }), sel);
      if (seenUnits.has(r.unitId)) return fail(r, t('Unit budget {n}: this unit already has a budget.', { n }), sel);
      seenUnits.add(r.unitId);
      const amount = amountOf(r.amount);
      if (amount === null) return fail(r, t('Unit budget {n}: enter an amount in USD of 0 or more.', { n }), r.amount);
      const thresholds = parseThresholds(r.thresholds.value);
      if (!thresholds) return fail(r, t('Unit budget {n}: enter alert thresholds from 1 to 100, for example 50, 80, 100.', { n }), r.thresholds);
      out.push({ scope: 'unit', unitId: r.unitId, monthlyPaidUsd: amount, action: r.action, thresholds });
    }
    const seenProjects = new Set();
    for (let i = 0; i < projectRows.length; i += 1) {
      const r = projectRows[i];
      const n = i + 1;
      const project = r.project.value.trim().toLowerCase();
      if (!PROJECT_ID_RE.test(project)) return fail(r, t('Project budget {n}: enter a valid project ID.', { n }), r.project);
      if (seenProjects.has(project)) return fail(r, t('Project budget {n}: this project already has a budget.', { n }), r.project);
      seenProjects.add(project);
      const amount = amountOf(r.amount);
      if (amount === null) return fail(r, t('Project budget {n}: enter an amount in USD of 0 or more.', { n }), r.amount);
      const thresholds = r.action === 'hard_cap' ? [...HARD_CAP_THRESHOLDS] : parseThresholds(r.thresholds.value);
      if (!thresholds) return fail(r, t('Project budget {n}: enter alert thresholds from 1 to 100, for example 50, 80, 100.', { n }), r.thresholds);
      out.push({ scope: 'project', project, monthlyPaidUsd: amount, action: r.action, thresholds });
    }
    return out.concat(others);
  };

  const save = admin ? button({ label: t('Save budgets'), icon: 'check', variant: 'primary' }) : null;
  if (save) {
    save.addEventListener('click', async () => {
      if (!requireAdmin(t('Save budgets'))) return;
      err.textContent = '';
      [...unitRows, ...projectRows].forEach((r) => r.el.classList.remove('invalid'));
      const out = collect();
      if (!out) return;
      const strict = out.some((x) => x.action === 'suspend' || x.action === 'hard_cap');
      const ok = await confirmDialog({
        title: t('Save budgets'),
        message: strict
          ? t('Budgets set to Suspend people or Hard cap stop paid usage when they are used up, from the next enforcement run. Continue?')
          : t('The budgets apply from the next enforcement run. Continue?'),
        confirmLabel: t('Save'),
        tone: strict ? 'danger' : 'primary',
      });
      if (!ok) return;
      setBusy(save, true, t('Saving'));
      try {
        await api.post('/api/v1/budgets', { nodes: out });
        toast({ title: t('Budgets saved'), text: t('{n} budgets apply from the next enforcement run.', { n: fmt.int(out.length) }), tone: 'success' });
        ctx.rerender();
      } catch (e) {
        setBusy(save, false);
        err.textContent = errorText(e);
      }
    });
  }

  const c = card({
    title: t('Budgets by organization unit and project'),
    subtitle: t('Amounts are USD of paid usage per calendar month: what people use beyond their free credit.'),
    body: h('div', { class: 'stack' },
      h('div', { class: 'stack-sm' },
        h('div', { class: 'row between wrap' }, h('strong', null, t('Organization unit budgets')), addUnit),
        h('ul', { class: 'muted stack-sm' },
          h('li', null, t('A unit budget covers the paid usage of everyone in the unit and its sub-units.')),
          h('li', null, t('Suspend people: when the budget is used up, the people of the unit are suspended while nothing is free for them, until the 1st of the next month. With weekly free credit, the credit that returns each week can still be used.')),
          h('li', null, t('Send to review: the people of the unit wait in the review queue for an administrator decision.')),
          h('li', null, t('Notify only: alerts at the thresholds; nobody is suspended.'))),
        h('div', { class: 'price-editor' }, unitHead, unitList, unitEmpty)),
      h('div', { class: 'divider' }),
      h('div', { class: 'stack-sm' },
        h('div', { class: 'row between wrap' }, h('strong', null, t('Project budgets')), addProject),
        h('ul', { class: 'muted stack-sm' },
          h('li', null, t('A project budget covers the paid usage recorded in one project.')),
          h('li', null, t('Notify only: alerts at the thresholds.')),
          h('li', null, t('Hard cap: a Google Cloud spend cap stops the Gemini Platform usage of the project when the budget is spent. It covers one project and the Gemini Platform service, counts list prices before credits and is not instant, so usage can go a little over. Google sends its alerts at 50, 80 and 100%.')),
          h('li', null, t('Hard caps apply only when Google spend caps are turned on in Settings > Governance policy > Enforcement.'))),
        doc.spendCaps === false && projectRows.some((r) => r.action === 'hard_cap') ? banner({
          tone: 'warning',
          title: t('Google spend caps are off'),
          text: t('Until they are turned on in Settings > Governance policy > Enforcement, a hard cap only sends alerts at the thresholds.'),
        }) : null,
        h('div', { class: 'price-editor' }, projectHead, projectList, projectEmpty, datalist)),
      h('p', { class: 'muted' }, t('Alert thresholds are percentages of the budget, for example 50, 80, 100.')),
      others.length ? h('p', { class: 'muted' }, t('{n} other budgets are kept unchanged.', { n: fmt.int(others.length) })) : null,
      err),
  });
  if (save) c.append(h('div', { class: 'form-actions' }, h('span', { class: 'spacer' }), save));
  return c;
}

/* Google spend caps of the project budgets set to Hard cap (services/spend_caps.py). */

const SPEND_CAPS_PATH = '/api/v1/budgets/spend-caps';

/** Google's state of a spend cap (spendCap.outputState). */
function capStateBadge(state, reconciling) {
  let main;
  if (state === 'CONFIGURED') main = badge(t('Configured'), 'success', { size: 'sm' });
  else if (state === 'ENFORCED') main = badge(t('Usage paused'), 'danger', { dot: true, size: 'sm', title: t('Google has paused the Gemini Platform usage of this project until the next month or until the cap is lifted.') });
  else if (state === 'AWAITING_NEXT_PERIOD') main = badge(t('Lifted until next month'), 'info', { size: 'sm' });
  else main = state && state !== 'STATE_UNSPECIFIED' ? badge(String(state), 'neutral', { size: 'sm' }) : h('span', { class: 'text-muted' }, DASH);
  return reconciling ? h('div', { class: 'chips' }, main, badge(t('Updating'), 'neutral', { size: 'sm' })) : main;
}

/** What the next apply does with the cap of a project: the reason it is refused first, else the change. */
function capPlanBadge(r) {
  if (r.blocked === 'enforced') return badge(t('Enforced by Google'), 'warning', { size: 'sm', title: t('Google is enforcing this spend cap, so only a lift can change it. Lift it or wait for the next month.') });
  if (r.blocked === 'currency_mismatch') return badge(t('Billing account not in USD'), 'warning', { size: 'sm', title: t('Budgets are in USD, so spend caps are set only on a billing account billed in USD. Set this cap in the Cloud console.') });
  if (r.blocked === 'name_too_long') return badge(t('Project ID too long'), 'warning', { size: 'sm', title: t('The project id is too long for the 60-character budget name, so no spend cap is managed for it.') });
  if (r.blocked === 'outside_workspace') return badge(t('Not in this workspace'), 'warning', { size: 'sm', title: t('The project is not part of this workspace, so the control plane does not manage a spend cap for it.') });
  if (r.blocked) return badge(String(r.blocked), 'warning', { size: 'sm' });
  switch (r.op) {
    case 'create': return badge(t('To be created'), 'info', { size: 'sm' });
    case 'update': return badge(t('To be changed'), 'info', { size: 'sm' });
    case 'none': return badge(t('Up to date'), 'success', { size: 'sm' });
    case 'orphan': return badge(t('No longer a hard cap'), 'neutral', { size: 'sm', title: t('The project is no longer a hard cap. Its spend cap was kept; delete it in the Cloud console if it is not needed any more.') });
    default: return h('span', { class: 'text-muted' }, DASH);
  }
}

/** Outcome for one project of an apply or a dry run. */
function capResultBadge(status) {
  switch (status) {
    case 'created': return badge(t('Cap created'), 'success', { size: 'sm' });
    case 'updated': return badge(t('Cap updated'), 'success', { size: 'sm' });
    case 'unchanged': return badge(t('Up to date'), 'neutral', { size: 'sm' });
    case 'orphan': return badge(t('Kept'), 'neutral', { size: 'sm' });
    case 'dry_run': return badge(t('Would change'), 'info', { size: 'sm' });
    case 'enforced': return badge(t('Enforced by Google'), 'warning', { size: 'sm' });
    case 'currency_mismatch': return badge(t('Billing account not in USD'), 'warning', { size: 'sm' });
    case 'name_too_long': return badge(t('Project ID too long'), 'warning', { size: 'sm' });
    case 'outside_workspace': return badge(t('Not in this workspace'), 'warning', { size: 'sm' });
    case 'not_owned': return badge(t('Not managed by the control plane'), 'warning', { size: 'sm' });
    case 'skipped': return badge(t('Skipped'), 'neutral', { size: 'sm' });
    case 'failed': return badge(t('Failed'), 'danger', { size: 'sm' });
    default: return status ? badge(String(status), 'neutral', { size: 'sm' }) : h('span', { class: 'text-muted' }, DASH);
  }
}

/** Rows of GET .../spend-caps: the plan (wanted and existing caps) with Google's state of each cap. */
function spendCapRows(sc) {
  const caps = sc.caps && typeof sc.caps === 'object' && !Array.isArray(sc.caps) ? sc.caps : {};
  const desired = sc.desired && typeof sc.desired === 'object' && !Array.isArray(sc.desired) ? sc.desired : {};
  const rows = [];
  const seen = new Set();
  const add = (project, p) => {
    if (!project || seen.has(project)) return;
    seen.add(project);
    const c = caps[project] && typeof caps[project] === 'object' ? caps[project] : {};
    rows.push({
      project,
      wanted: p ? p.amount : (isNum(desired[project]) ? desired[project] : null),
      current: p && isNum(p.current) ? p.current : (isNum(c.amount) ? c.amount : null),
      currency: (p && p.currency) || c.currency || sc.currency || null,
      state: c.outputState || (p && p.state) || null,
      reconciling: c.reconciling === true,
      op: p ? p.op : null,
      blocked: p ? p.blocked : null,
    });
  };
  (Array.isArray(sc.plan) ? sc.plan : []).forEach((p) => { if (p && typeof p === 'object') add(p.project, p); });
  // Without a plan (spend caps off or not readable) the wanted and the known caps still show.
  Object.keys(caps).forEach((project) => add(project, null));
  Object.keys(desired).forEach((project) => add(project, null));
  return rows;
}

function spendCapResults(r, onClose) {
  const results = Array.isArray(r.results) ? r.results.filter((x) => x && typeof x === 'object') : [];
  const m = openModal({
    title: r.dryRun ? t('Spend cap preview') : t('Spend caps applied'),
    subtitle: r.dryRun ? t('Nothing was changed. This is what applying the spend caps would do.') : null,
    wide: true,
    body: (el) => el.append(...[
      results.length ? dataTable({
        columns: [
          { key: 'project', label: t('Project'), render: (x) => projectCell(x.project), value: (x) => x.project || '' },
          { key: 'amount', label: t('Monthly budget'), align: 'num', render: (x) => moneyText(x.amount, 'USD'), value: (x) => x.amount },
          { key: 'status', label: t('Result'), render: (x) => capResultBadge(x.status), value: (x) => x.status || '' },
          { key: 'message', label: t('Detail'), sortable: false, wrap: true, render: (x) => (x.message ? h('span', { class: 'cell-sub' }, serverText(x.message)) : h('span', { class: 'text-muted' }, DASH)) },
        ],
        rows: results, searchable: false, pageSize: 10,
      }) : h('p', { class: 'muted' }, t('No project budget is set to Hard cap.')),
      r.dryRun ? null : h('div', { class: 'row wrap muted' },
        r.auditLogged ? badge(t('Recorded in audit log'), 'success', { size: 'sm', dot: true }) : badge(t('Audit log not written'), 'warning', { size: 'sm', dot: true })),
    ].filter(Boolean)),
    footer: (el) => el.append(button({ label: t('Close'), variant: 'primary', onClick: () => m.close() })),
    onClose: onClose || null,
  });
}

function renderSpendCaps(ctx, host, sc, admin, liftCap) {
  const rows = spendCapRows(sc);
  let notice = null;
  if (sc.enabled === false) {
    notice = banner({
      tone: 'info',
      title: t('Google spend caps are off'),
      text: t('Until they are turned on in Settings > Governance policy > Enforcement, a hard cap only sends alerts at the thresholds.'),
      actions: admin ? button({ label: t('Open settings'), size: 'sm', onClick: () => ctx.navigate('#/settings/governance') }) : null,
    });
  } else if (sc.accountConfigured === false) {
    notice = banner({
      tone: 'warning',
      title: t('The billing account ID is missing'),
      text: t('Spend caps are set on the billing account. Enter its ID in Settings > Workspace and data sources > Google Cloud workspace.'),
      actions: admin ? button({ label: t('Open settings'), size: 'sm', onClick: () => ctx.navigate('#/settings/workspace') }) : null,
    });
  } else if (sc.error) {
    notice = banner({ tone: 'warning', title: t('Spend caps could not be read'), text: serverText(sc.error) });
  }
  const paused = rows.filter((r) => r.state === 'ENFORCED').length;
  const columns = [
    { key: 'project', label: t('Project'), render: (r) => projectCell(r.project), value: (r) => r.project },
    { key: 'wanted', label: t('Monthly budget'), align: 'num', render: (r) => moneyText(r.wanted, 'USD'), value: (r) => r.wanted },
    { key: 'current', label: t('Spend cap'), align: 'num', render: (r) => moneyText(r.current, r.currency || 'USD'), value: (r) => r.current },
    { key: 'state', label: t('Google state'), render: (r) => capStateBadge(r.state, r.reconciling), value: (r) => r.state || '' },
    { key: 'plan', label: t('Next apply'), sortable: false, render: capPlanBadge, csv: (r) => r.blocked || r.op || '' },
  ];
  if (admin) {
    columns.push({
      key: 'actions', label: '', actions: true,
      render: (r) => {
        if (r.state !== 'ENFORCED') return h('span');
        const b = button({ label: t('Lift cap'), icon: 'unlock', size: 'sm' });
        b.addEventListener('click', () => liftCap(r.project, b));
        return b;
      },
    });
  }
  host.append(h('div', { class: 'stack-sm' },
    notice,
    paused ? banner({
      tone: 'danger',
      title: t('{n} projects are paused by their spend cap', { n: fmt.int(paused) }),
      text: t('Google has paused the Gemini Platform usage of these projects until the next month. An administrator can lift a cap earlier.'),
    }) : null,
    rows.length
      ? dataTable({ columns, rows, pageSize: 10, searchable: rows.length > 10, exportName: 'spend-caps' })
      : h('p', { class: 'muted' }, t('No project budget is set to Hard cap.')),
    h('p', { class: 'muted' }, t('Google counts the list cost of all Gemini Platform usage in the project before credits, so a spend cap can stop usage before the dashboard budget is used up. Usage billed under other services is not covered.'))));
}

/** Google spend caps of the project budgets set to Hard cap: their state, the next apply, and lifting a paused cap. */
function spendCapsCard(ctx, admin) {
  const host = h('div', null, skeleton('lines'));
  const refreshBtn = button({ icon: 'refresh', size: 'sm', title: t('Refresh') });
  const previewBtn = admin ? button({ label: t('Preview changes'), icon: 'eye' }) : null;
  const applyBtn = admin ? button({ label: t('Apply spend caps'), icon: 'check', variant: 'primary' }) : null;
  // A plain wrapper, because .form-actions sets its own display and would ignore hidden.
  const actions = h('div', null, h('div', { class: 'form-actions' }, h('span', { class: 'spacer' }), previewBtn, applyBtn));
  actions.hidden = true;
  const c = card({
    title: t('Google spend caps'),
    subtitle: t('Spend caps on the billing account for the project budgets set to Hard cap'),
    actions: refreshBtn,
    body: host,
  });
  if (admin) c.append(actions);
  let liftCap = null;
  const refresh = async () => {
    actions.hidden = true;
    mount(host, skeleton('lines'));
    try {
      const res = await api.get(SPEND_CAPS_PATH, null, { signal: ctx.signal });
      if (ctx.signal.aborted) return;
      const sc = res.data || {};
      mount(host);
      renderSpendCaps(ctx, host, sc, admin, liftCap);
      actions.hidden = !(admin && sc.enabled === true && sc.accountConfigured !== false && !sc.error);
    } catch (err) {
      if (isAbort(err) || ctx.signal.aborted) return;
      // A server without the spend caps routes, or a role that may not read them, leaves the card out.
      if (err instanceof ApiError && (err.status === 404 || err.status === 403)) {
        c.remove();
        return;
      }
      mount(host, errorState(err, { compact: true, onRetry: () => refresh() }));
    }
  };
  liftCap = async (project, btn) => {
    if (!requireAdmin(t('Lift cap'))) return;
    const ok = await confirmDialog({
      title: t('Lift the spend cap of {project}?', { project }),
      message: t('Google resumes the Gemini Platform usage of the project within about an hour. The cap stays lifted until the start of the next month, or until its amount is raised.'),
      confirmLabel: t('Lift cap'),
      tone: 'danger',
    });
    if (!ok) return;
    setBusy(btn, true, t('Lifting'));
    try {
      const res = await api.post(`${SPEND_CAPS_PATH}/lift`, { project });
      const r = res.data || {};
      const lifted = r.status === 'lifted';
      toast({
        title: lifted ? t('Spend cap lifted') : t('The spend cap was not lifted'),
        text: serverText(r.message),
        tone: lifted ? 'success' : (r.status === 'failed' ? 'danger' : 'info'),
        timeout: 9000,
      });
      refresh();
    } catch (err) {
      setBusy(btn, false);
      toast({ title: t('The spend cap was not lifted'), text: errorText(err), tone: 'danger', timeout: 9000 });
    }
  };
  const run = async (dryRun, btn) => {
    if (!requireAdmin(t('Apply spend caps'))) return;
    if (!dryRun) {
      const ok = await confirmDialog({
        title: t('Apply spend caps'),
        message: t('Creates or changes the Google Cloud spend caps of the project budgets set to Hard cap on the billing account. When a project reaches its cap, Google pauses its Gemini Platform usage until the next month. Spend caps that are no longer needed are kept; delete them in the Cloud console.'),
        confirmLabel: t('Apply'),
        tone: 'danger',
      });
      if (!ok) return;
    }
    setBusy(btn, true, dryRun ? t('Simulating') : t('Applying'));
    try {
      const res = await api.post(`${SPEND_CAPS_PATH}/apply`, { dryRun });
      setBusy(btn, false);
      spendCapResults(res.data || {}, dryRun ? null : () => refresh());
    } catch (err) {
      setBusy(btn, false);
      toast({ title: dryRun ? t('Dry run failed') : t('Spend caps could not be applied'), text: errorText(err), tone: 'danger', timeout: 9000 });
    }
  };
  if (previewBtn) previewBtn.addEventListener('click', () => run(true, previewBtn));
  if (applyBtn) applyBtn.addEventListener('click', () => run(false, applyBtn));
  refreshBtn.addEventListener('click', () => refresh());
  refresh();
  return c;
}

function renderBudgets(ctx, body, doc, tree, treeError) {
  const admin = isAdmin();
  const units = tree && Array.isArray(tree.units) ? tree.units.filter((u) => u && u.id) : [];
  const unitById = new Map(units.map((u) => [u.id, u]));
  const status = doc.status && typeof doc.status === 'object' ? doc.status : null;
  const month = MONTH_RE.test(doc.month || '') ? doc.month : String(today() || '').slice(0, 7);
  body.append(banner({
    tone: 'info',
    title: t('How budgets work'),
    text: t('A budget limits the paid usage of a month in USD, that is usage beyond the free credit. The enforcement run compares the budgets with the usage of this month every few minutes, sends alerts at the thresholds and applies the action of a budget when it is used up. Nobody is suspended permanently: suspensions end on the 1st of the next month.'),
  }));
  if (!admin) {
    body.append(banner({ tone: 'neutral', iconName: 'lock', title: t('Read-only'), text: t('Only administrators can change budgets.') }));
  }
  if (treeError) {
    body.append(banner({ tone: 'warning', title: t('The organization structure could not be read'), text: errorText(treeError) }));
  }
  body.append(budgetStatusCard(doc, status, unitById, month));
  body.append(budgetEditor(ctx, doc, units, unitById, admin));
  // Google spend caps matter once they are turned on or a project budget is a hard cap.
  const hardCaps = (Array.isArray(doc.nodes) ? doc.nodes : []).some((n) => n && n.scope === 'project' && n.action === 'hard_cap');
  if (doc.spendCaps === true || hardCaps) body.append(spendCapsCard(ctx, admin));
}

async function budgetsPage(ctx, top) {
  const body = h('div', { class: 'stack-lg' });
  mount(ctx.root, top, body);
  mount(body, skeleton('block', 260));
  let res;
  let tree = null;
  let treeError = null;
  try {
    const [budgets, treeRes] = await Promise.all([
      api.get('/api/v1/budgets', null, { signal: ctx.signal }),
      api.get('/api/v1/org/tree', null, { signal: ctx.signal }).catch((err) => {
        if (isAbort(err)) throw err;
        treeError = err;
        return null;
      }),
    ]);
    res = budgets;
    tree = treeRes ? (treeRes.data || {}) : null;
  } catch (err) {
    if (isAbort(err) || ctx.signal.aborted) return null;
    mount(body, card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    return null;
  }
  if (ctx.signal.aborted) return null;
  mount(body);
  const notice = warningsNotice(res.meta && res.meta.warnings);
  if (notice) body.append(notice);
  renderBudgets(ctx, body, res.data || {}, tree, treeError);
  return null;
}

/* ------------------------------------------------------------------ month, status and close */

function monthName(m) {
  if (!MONTH_RE.test(m || '')) return m || DASH;
  const locale = getLocale() === 'ko' ? 'ko-KR' : 'en-US';
  return new Intl.DateTimeFormat(locale, { year: 'numeric', month: 'long', timeZone: 'UTC' }).format(new Date(`${m}-01T00:00:00Z`));
}

function monthSelect(ctx, d) {
  const listed = Array.isArray(d.months) ? d.months.filter((m) => MONTH_RE.test(m || '')) : [];
  const months = listed.slice();
  if (MONTH_RE.test(d.month || '') && !months.includes(d.month)) {
    months.push(d.month);
    months.sort().reverse();
  }
  if (!months.length) return null;
  // The list starts with the running month (current + 12 previous, newest first).
  const running = d.current ? d.month : (listed[0] || null);
  const closedMonths = new Set((Array.isArray(d.closes) ? d.closes : []).map((c) => c && c.month));
  const label = (m) => {
    if (m === running) return `${monthName(m)} \u00b7 ${t('In progress')}`;
    if (closedMonths.has(m)) return `${monthName(m)} \u00b7 ${t('Closed')}`;
    return monthName(m);
  };
  return selectInput(months.map((m) => ({ value: m, label: label(m) })), d.month, (v) => {
    if (MONTH_RE.test(v) && v !== d.month) ctx.navigate(`#/chargeback?month=${v}`);
  }, { 'aria-label': t('Month') });
}

function statusChip(d) {
  if (d.status === 'closed') {
    const c = d.close || {};
    const tip = [
      c.closedBy ? t('Closed by {user}', { user: c.closedBy }) : null,
      c.closeId ? t('Close ID {id}', { id: c.closeId }) : null,
      isNum(c.orgVersion) ? t('Organization version {v}', { v: fmt.int(c.orgVersion) }) : null,
    ].filter(Boolean).join(' \u00b7 ');
    return badge(c.closedAt ? t('Closed {time}', { time: fmt.dateTime(c.closedAt) }) : t('Closed'), 'success', { dot: true, title: tip || null });
  }
  if (d.current) {
    return badge(t('Not closed (month in progress)'), 'warning', { dot: true, title: t('The running month cannot be closed. Close it after the month ends.') });
  }
  return badge(t('Not closed'), 'info', { dot: true, title: t('The figures still follow changes to usage, prices and the organization structure until the month is closed.') });
}

function closeButton(ctx, d) {
  const reclose = d.status === 'closed';
  const btn = button({
    label: reclose ? t('Re-close month') : t('Close month'),
    icon: reclose ? 'refresh' : 'lock',
    variant: reclose ? 'secondary' : 'primary',
    disabled: Boolean(d.current) || !MONTH_RE.test(d.month || ''),
    title: d.current ? t('The running month cannot be closed. Close it after the month ends.') : null,
    onClick: () => closeMonth(ctx, d, btn),
  });
  return btn;
}

async function closeMonth(ctx, d, btn) {
  const reclose = d.status === 'closed';
  if (!requireAdmin(reclose ? t('Re-close month') : t('Close month'))) return;
  if (d.current || !MONTH_RE.test(d.month || '')) return;
  const ccy = d.currency || 'USD';
  const totals = d.totals || {};
  const om = d.orgModel || {};
  const name = monthName(d.month);
  const details = kv([
    [t('Total charged back'), moneyText(totals.total, ccy)],
    [t('People'), fmt.int(totals.users)],
    [t('Organization version'), isNum(om.version) ? fmt.int(om.version) : DASH],
  ]);
  const prev = d.close || {};
  // A first close freezes the live figures shown on the page; a re-close recomputes them, so the frozen totals of
  // the previous close are not repeated in its dialog.
  const ok = await confirmDialog(reclose ? {
    title: t('Re-close {month}', { month: name }),
    message: t('{month} was closed on {time} by {user}. A re-close freezes the current figures as a new close; the previous close stays in the history.', {
      month: name, time: fmt.dateTime(prev.closedAt), user: prev.closedBy || DASH,
    }),
    confirmLabel: t('Re-close month'),
    tone: 'danger',
    requireText: 'RECLOSE',
  } : {
    title: t('Close {month}', { month: name }),
    message: t('The figures of {month} are frozen as the month-close snapshot. Later changes to usage, prices or the organization structure no longer change them.', { month: name }),
    details,
    confirmLabel: t('Close month'),
  });
  if (!ok) return;
  setBusy(btn, true, t('Closing'));
  try {
    const res = await api.post('/api/v1/chargeback/close', { month: d.month, reclose });
    const r = res.data || {};
    const notes = ((res.meta && res.meta.warnings) || []).map((w) => noticeText(w)).filter(Boolean);
    const copy = r.bigquery && r.bigquery.table ? t('A copy was written to the BigQuery table {table}.', { table: r.bigquery.table }) : null;
    toast({
      title: t('Month closed'),
      text: [t('{month} is closed. Rows frozen: {n} (close ID {id}).', { month: name, n: fmt.int(r.rows), id: r.closeId || DASH }), copy, ...notes].filter(Boolean).join(' '),
      tone: notes.length ? 'warning' : 'success',
      timeout: notes.length ? 10000 : 5000,
    });
    ctx.rerender();
  } catch (err) {
    setBusy(btn, false);
    toast({ title: t('Month close failed'), text: errorText(err), tone: 'danger', timeout: 9000 });
    // Someone else closed the month meanwhile: show the current state.
    if (err && err.code === 'conflict') ctx.rerender();
  }
}

function basisBanner(d) {
  const closed = d.status === 'closed';
  const c = d.close || {};
  const parts = [
    t('Amounts are computed from logged tokens x the unit price of each model; they are not invoice lines.'),
    d.allocationMode === 'list'
      ? t('List mode: the list cost is charged back without included credit.')
      : t('Included credit is applied per person (the monthly credit / 4 each week); the rest is charged back.'),
    d.seatPricing ? t('Seat cost adds the monthly price of each assigned license seat.') : null,
  ];
  if (closed) {
    parts.push(t('These are the frozen figures of the month close on {time} by {user}.', { time: fmt.dateTime(c.closedAt), user: c.closedBy || DASH }));
  } else if (d.current) {
    parts.push(t('The month is still running, so the figures change until it ends.'));
  } else if (d.complete === false) {
    parts.push(t('Usage data for this month may still be incomplete.'));
  }
  return banner({
    tone: closed ? 'success' : 'info',
    iconName: closed ? 'lock' : 'info',
    title: closed ? t('Month-close snapshot') : t('How the amounts are calculated'),
    text: parts.filter(Boolean).join(' '),
  });
}

/** People whose usage needs a model without a unit price: their usage money is null and left out of every sum. */
function hasUnknownAmounts(d) {
  const tt = d.totals || {};
  if (isNum(tt.unknownUsers) && tt.unknownUsers > 0) return true;
  if (tt.cost === null || tt.chargeable === null || tt.total === null) return true;
  return (Array.isArray(d.projects) ? d.projects : []).some((p) => p && (p.cost === null || p.total === null));
}

/* ------------------------------------------------------------------ amounts and labels */

function moneyText(v, ccy) {
  return isNum(v) ? fmt.currency(v, ccy) : DASH;
}

function moneyKpi(v, ccy) {
  if (!isNum(v)) return DASH;
  const exact = fmt.currency(v, ccy);
  const short = fmt.currency(v, ccy, { compact: true });
  return short === exact ? exact : h('span', { title: exact }, short);
}

function moneyCell(v, ccy, strong) {
  if (!isNum(v)) return h('span', { class: 'text-muted', title: v === null ? t('Unknown: a model used has no unit price') : null }, DASH);
  const text = fmt.currency(v, ccy);
  return strong ? h('span', { class: 'fw-600' }, text) : text;
}

function moneyCol(key, label, pick, ccy, { strong = false, title = null, cellTitle = null } = {}) {
  return {
    key,
    label,
    align: 'num',
    title,
    render: (r) => {
      const cell = moneyCell(pick(r), ccy, strong);
      const tip = cellTitle ? cellTitle(r) : null;
      return tip ? h('span', { title: tip }, cell) : cell;
    },
    value: (r) => pick(r),
    csv: (r) => pick(r),
  };
}

/** Usage cost, credit, charged usage, seat and total read from `src(row)`. */
function amountColumns(d, ccy, src, { totalTitle = null, creditTitle = null } = {}) {
  const pick = (k) => (r) => {
    const o = src(r);
    return o ? o[k] : undefined;
  };
  return [
    moneyCol('cost', t('Usage cost'), pick('cost'), ccy, { title: t('List cost at unit prices') }),
    moneyCol('credit', t('Credit'), pick('credit'), ccy, {
      title: d.allocationMode === 'list' ? t('List mode: no included credit is applied') : t('Included credit applied'),
      cellTitle: creditTitle,
    }),
    moneyCol('chargeable', t('Charged usage'), pick('chargeable'), ccy, { title: t('Usage cost minus included credit') }),
    moneyCol('seat', t('Seat'), pick('seat'), ccy, { title: d.seatPricing ? t('License seat cost for the month') : t('Seat pricing is off') }),
    moneyCol('total', t('Total'), pick('total'), ccy, { strong: true, title: totalTitle || t('Charged usage plus seat cost') }),
  ];
}

function monoOrDash(v) {
  return v ? h('span', { class: 'cell-mono' }, String(v)) : DASH;
}

function countOf(v) {
  return Array.isArray(v) ? v.length : v;
}

function unitName(u) {
  if (u.id === UNASSIGNED) return t('Unassigned');
  return u.name || u.id || DASH;
}

function unitPathText(u) {
  if (u.id === UNASSIGNED) return t('Unassigned');
  return u.path || u.name || u.id || '';
}

function isUnassignedRef(unitId, unitPath) {
  return unitId === UNASSIGNED || (!unitId && !unitPath);
}

function unitRefText(unitId, unitPath) {
  return isUnassignedRef(unitId, unitPath) ? t('Unassigned') : (unitPath || unitId);
}

function unitRefCell(unitId, unitPath) {
  const text = unitRefText(unitId, unitPath);
  if (isUnassignedRef(unitId, unitPath)) return h('span', { class: 'text-muted' }, text);
  return h('span', { class: 'truncate', title: text }, text);
}

/** Tree prefix built from text (non-breaking spaces and a corner), since style attributes are not allowed. */
function treeIndent(depth, base) {
  const level = isNum(depth) ? depth - base : 0;
  if (level <= 0) return '';
  return `${'\u00a0\u00a0\u00a0'.repeat(level - 1)}\u2514\u00a0`;
}

function projectText(p) {
  return p === UNKNOWN_PROJECT ? t('Unknown project') : (p || '');
}

function projectCell(p) {
  if (!p) return DASH;
  if (p === UNKNOWN_PROJECT) return h('span', { class: 'text-muted' }, t('Unknown project'));
  return h('span', { class: 'cell-mono' }, String(p));
}

function usageSourceText(source) {
  if (!source) return t('Usage logs and the unit price of each model');
  switch (source) {
    case 'bigquery': return t('Inference logs in BigQuery and the unit price of each model');
    case 'logging': return t('Inference logs in Cloud Logging and the unit price of each model');
    case 'unavailable': return t('Usage logs unavailable');
    default: return serverText(source);
  }
}

function viaLabel(via) {
  switch (via) {
    case 'member': return t('Member mapping');
    case 'project': return t('Project mapping');
    case 'label': return t('Project label');
    case 'unassigned': return t('Unassigned');
    case 'mixed': return t('Mixed');
    default: return via ? String(via) : null;
  }
}

function viaBadge(via) {
  const label = viaLabel(via);
  if (!label) return h('span', { class: 'text-muted' }, DASH);
  let tone = 'neutral';
  if (via === 'unassigned') tone = 'warning';
  else if (via === 'mixed') tone = 'info';
  return badge(label, tone, { size: 'sm' });
}

function tierLabel(tier) {
  switch (tier) {
    case 'SUBSCRIPTION_TIER_ENTERPRISE': return t('Standard edition');
    case 'SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT': return t('Plus edition');
    case 'SUBSCRIPTION_TIER_ENTERPRISE_EMERGING': return t('Standard edition (emerging markets)');
    case 'SUBSCRIPTION_TIER_CONSUMPTION_ONLY': return t('Pay-as-you-go');
    default: return tier ? String(tier) : null;
  }
}

function creditSourceLabel(source) {
  switch (source) {
    case 'license': return t('Credit set by the license edition');
    case 'fixed': return t('Fixed credit per person');
    case 'rule': return t('Credit set by an individual rule');
    default: return null;
  }
}

function problemLabel(code) {
  switch (code) {
    case 'bad_principal': return t('Invalid principal');
    case 'bad_project': return t('Invalid project ID');
    case 'bad_path': return t('Invalid unit path');
    case 'segment_too_long': return t('Unit name is too long');
    case 'path_too_deep': return t('Unit path is deeper than 10 levels');
    case 'bad_cost_center': return t('Invalid cost center');
    case 'cost_center_conflict': return t('Conflicting cost center');
    case 'principal_conflict': return t('Principal mapped to more than one unit');
    case 'project_conflict': return t('Project mapped to more than one unit');
    case 'bad_row': return t('Malformed row');
    case 'duplicate': return t('Duplicate row');
    default: return code ? String(code) : DASH;
  }
}

/* ------------------------------------------------------------------ breakdown: organization / people / projects */

function unitColumns(d, ccy, base) {
  return [
    {
      key: 'unit',
      label: t('Unit'),
      render: (u) => h('span', { class: u.id === UNASSIGNED ? 'text-muted' : 'cell-main', title: unitPathText(u) }, `${treeIndent(u.depth, base)}${unitName(u)}`),
      value: (u) => unitPathText(u),
      csv: (u) => unitPathText(u),
    },
    { key: 'costCenter', label: t('Cost center'), render: (u) => monoOrDash(u.costCenter) },
    { key: 'users', label: t('People'), align: 'num', format: fmt.int },
    ...amountColumns(d, ccy, (u) => u.rollup, { totalTitle: t('Charged usage plus seat cost, including all sub-units') }),
    moneyCol('ownTotal', t('Own total'), (u) => (u.own ? u.own.total : undefined), ccy, { title: t('Charges of the unit itself, without sub-units') }),
  ];
}

function peopleColumns(d, ccy) {
  return [
    {
      key: 'person',
      label: t('Person'),
      render: (r) => (r.kind === 'workforce' ? principalCell(r.person, t('Workforce identity')) : userCell(r.person)),
    },
    { key: 'unit', label: t('Unit'), render: (r) => unitRefCell(r.unitId, r.unitPath), value: (r) => unitRefText(r.unitId, r.unitPath), csv: (r) => unitRefText(r.unitId, r.unitPath) },
    { key: 'costCenter', label: t('Cost center'), render: (r) => monoOrDash(r.costCenter) },
    { key: 'via', label: t('Assigned by'), render: (r) => viaBadge(r.via), value: (r) => viaLabel(r.via), csv: (r) => viaLabel(r.via) },
    {
      key: 'projects',
      label: t('Projects'),
      sortable: false,
      render: (r) => listCell((Array.isArray(r.projects) ? r.projects : []).map(projectText), { mono: true }),
      value: (r) => (Array.isArray(r.projects) ? r.projects.join(' ') : ''),
      csv: (r) => (Array.isArray(r.projects) ? r.projects.join('|') : ''),
    },
    {
      key: 'models',
      label: t('Models & cost'),
      sortable: false,
      render: (r) => modelCostCell(r.byModel, ccy),
      value: (r) => (Array.isArray(r.byModel) ? r.byModel.map((m) => m.model).join(' ') : ''),
      csv: (r) => (Array.isArray(r.byModel) ? r.byModel.map((m) => `${m.model}:${isNum(m.cost) ? m.cost : m.tokens}`).join('|') : ''),
    },
    { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
    { key: 'tokens', label: t('Tokens'), align: 'num', format: fmt.compact },
    ...amountColumns(d, ccy, (r) => r, { creditTitle: (r) => creditSourceLabel(r.creditSource) }),
    { key: 'tier', label: t('Edition'), render: (r) => tierLabel(r.tier) || DASH, value: (r) => tierLabel(r.tier), csv: (r) => tierLabel(r.tier) },
  ];
}

function projectColumns(d, ccy) {
  return [
    { key: 'project', label: t('Project'), render: (r) => projectCell(r.project) },
    { key: 'unit', label: t('Unit'), render: (r) => unitRefCell(r.unitId, r.unitPath), value: (r) => unitRefText(r.unitId, r.unitPath), csv: (r) => unitRefText(r.unitId, r.unitPath) },
    { key: 'users', label: t('People'), align: 'num', format: fmt.int },
    { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
    { key: 'tokens', label: t('Tokens'), align: 'num', format: fmt.compact },
    ...amountColumns(d, ccy, (r) => r),
  ];
}

function isUnassignedPerson(r) {
  return r.via === 'unassigned' || isUnassignedRef(r.unitId, r.unitPath);
}

function breakdownCard(ctx, d, orgEmpty) {
  const ccy = d.currency || 'USD';
  const month = MONTH_RE.test(d.month || '') ? d.month : 'month';
  const state = { view: lastView || (orgEmpty ? 'projects' : 'org'), unassignedOnly: false, peopleMode: 'person', modelUser: 'all', modelName: 'all' };
  const hosts = { org: h('div'), people: h('div'), projects: h('div') };
  const segSlot = h('div');
  let people = null;
  let peopleLoading = false;

  // Units arrive in depth-first order: units with charges and their ancestors, then the virtual Unassigned unit.
  const renderOrg = () => {
    const all = Array.isArray(d.units) ? d.units : [];
    const minDepth = all.reduce((m, u) => (u && u.id !== UNASSIGNED && isNum(u.depth) ? Math.min(m, u.depth) : m), Infinity);
    const base = Number.isFinite(minDepth) ? minDepth : 0;
    mount(hosts.org, dataTable({
      columns: unitColumns(d, ccy, base),
      rows: all,
      pageSize: 25,
      exportName: `chargeback-units-${month}`,
      emptyText: orgEmpty
        ? t('No organization units yet. Import the organization structure to see costs by unit.')
        : t('No unit has charges in this month.'),
    }));
  };

  const renderProjects = () => {
    mount(hosts.projects, dataTable({
      columns: projectColumns(d, ccy),
      rows: Array.isArray(d.projects) ? d.projects : [],
      pageSize: 25,
      exportName: `chargeback-projects-${month}`,
      emptyText: t('No project usage in this month.'),
    }));
  };

  const renderPeople = () => {
    const all = Array.isArray(people.data.rows) ? people.data.rows : [];
    const notice = warningsNotice(people.meta.warnings);
    const baseRows = state.unassignedOnly ? all.filter(isUnassignedPerson) : all;
    const toggle = chip(t('Unassigned only'), { pressed: state.unassignedOnly, onClick: () => { state.unassignedOnly = !state.unassignedOnly; renderPeople(); } });
    const modeSeg = segmented(
      [{ value: 'person', label: t('By user') }, { value: 'models', label: t('By user & model') }],
      state.peopleMode, (v) => { state.peopleMode = v; renderPeople(); }, { label: t('View by') });
    if (state.peopleMode === 'models') {
      const flat = [];
      baseRows.forEach((r) => {
        const modelsList = Array.isArray(r.byModel) ? r.byModel : [];
        const totalCost = modelsList.reduce((acc, m) => acc + (isNum(m.cost) ? m.cost : 0), 0);
        modelsList.forEach((m) => {
          const share = isNum(m.costShare) ? m.costShare : (isNum(m.cost) && totalCost > 0 ? m.cost / totalCost : null);
          flat.push({
            uid: r.uid,
            person: r.person,
            kind: r.kind,
            unitId: r.unitId,
            unitPath: r.unitPath,
            costCenter: r.costCenter,
            model: m.model,
            requests: m.requests,
            promptTokens: m.promptTokens,
            cachedTokens: m.cachedTokens,
            outputTokens: m.outputTokens,
            tokens: m.tokens,
            cost: m.cost,
            costPartial: m.costPartial,
            costShare: share,
          });
        });
      });
      const emails = Array.from(new Set(flat.map((r) => r.person).filter(Boolean))).sort();
      const models = Array.from(new Set(flat.map((r) => r.model).filter(Boolean))).sort();
      if (state.modelUser !== 'all' && !emails.includes(state.modelUser)) state.modelUser = 'all';
      if (state.modelName !== 'all' && !models.includes(state.modelName)) state.modelName = 'all';
      const filtered = flat.filter((r) => (state.modelUser === 'all' || r.person === state.modelUser)
        && (state.modelName === 'all' || r.model === state.modelName));
      const toolbar = h('div', { class: 'row wrap' },
        modeSeg,
        selectInput(
          [{ value: 'all', label: t('All users') }, ...emails.map((em) => ({ value: em, label: em }))],
          state.modelUser, (v) => { state.modelUser = v; renderPeople(); }, { 'aria-label': t('Filter by user') }),
        selectInput(
          [{ value: 'all', label: t('All models') }, ...models.map((m) => ({ value: m, label: m }))],
          state.modelName, (v) => { state.modelName = v; renderPeople(); }, { 'aria-label': t('Filter by model') }),
        toggle);
      mount(hosts.people,
        notice ? h('div', { class: 'card-body flush-top' }, notice) : null,
        filteredCostSummary(filtered, ccy),
        dataTable({
          columns: [
            { key: 'person', label: t('Person'), render: (r) => (r.kind === 'workforce' ? principalCell(r.person, t('Workforce identity')) : userCell(r.person)) },
            { key: 'unit', label: t('Unit'), render: (r) => unitRefCell(r.unitId, r.unitPath), value: (r) => unitRefText(r.unitId, r.unitPath), csv: (r) => unitRefText(r.unitId, r.unitPath) },
            { key: 'costCenter', label: t('Cost center'), render: (r) => monoOrDash(r.costCenter) },
            { key: 'model', label: t('Model'), render: (r) => h('span', { class: 'cell-mono' }, r.model || DASH), value: (r) => r.model || '' },
            { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
            { key: 'promptTokens', label: t('Input tokens'), align: 'num', format: fmt.compact, csv: (r) => r.promptTokens },
            { key: 'cachedTokens', label: t('From cache'), align: 'num', format: fmt.compact, csv: (r) => r.cachedTokens },
            { key: 'outputTokens', label: t('Output tokens'), align: 'num', format: fmt.compact, csv: (r) => r.outputTokens },
            { key: 'tokens', label: t('Total tokens'), align: 'num', render: (r) => h('span', { class: 'fw-600' }, fmt.compact(r.tokens)), value: (r) => r.tokens, csv: (r) => r.tokens },
            moneyCol('cost', t('Usage cost'), (r) => r.cost, ccy, { strong: true, title: t('List cost at unit prices') }),
            { key: 'costShare', label: t('Cost share'), align: 'num', render: (r) => costShareCell(r.costShare), value: (r) => r.costShare, csv: (r) => (isNum(r.costShare) ? r.costShare : '') },
          ],
          rows: filtered,
          pageSize: 25,
          exportName: `chargeback-people-by-model-${month}`,
          initialSort: { key: 'cost', dir: 'desc' },
          toolbar,
          emptyText: t('No per-user model usage was recorded in this window.'),
          onRowClick: (r) => r.uid && openUserDrawer(r.uid, { email: r.kind === 'workforce' ? null : r.person }),
        }));
      return;
    }
    mount(hosts.people,
      notice ? h('div', { class: 'card-body flush-top' }, notice) : null,
      dataTable({
        columns: peopleColumns(d, ccy),
        rows: baseRows,
        pageSize: 25,
        exportName: `chargeback-people-${month}`,
        initialSort: { key: 'total', dir: 'desc' },
        toolbar: h('div', { class: 'row wrap' }, modeSeg, toggle),
        emptyText: state.unassignedOnly ? t('Everyone with usage belongs to an organization unit.') : t('No usage by people in this month.'),
        onRowClick: (r) => r.uid && openUserDrawer(r.uid, { email: r.kind === 'workforce' ? null : r.person }),
      }));
  };

  // The per-person report is loaded the first time the People view opens.
  const loadPeople = () => {
    if (people || peopleLoading) return;
    peopleLoading = true;
    load(hosts.people, () => api.get('/api/v1/chargeback/report', { month: d.month, level: 'user' }, { signal: ctx.signal }), (res) => {
      people = { data: res.data || {}, meta: res.meta || {} };
      renderPeople();
    }, { skeletonKind: 'table', signal: ctx.signal }).finally(() => { peopleLoading = false; });
  };

  const show = () => {
    Object.keys(hosts).forEach((k) => { hosts[k].hidden = k !== state.view; });
    if (state.view === 'people') loadPeople();
  };

  const renderSeg = () => mount(segSlot, segmented([
    { value: 'org', label: t('Organization') },
    { value: 'people', label: t('People') },
    { value: 'projects', label: t('Projects') },
  ], state.view, (v) => { state.view = v; lastView = v; show(); }, { label: t('View by') }));

  renderSeg();
  renderOrg();
  renderProjects();
  show();
  const el = card({
    title: t('Charges by organization unit, person and project'),
    subtitle: monthName(d.month),
    actions: segSlot,
    flush: true,
    body: h('div', null, hosts.org, hosts.people, hosts.projects),
    source: usageSourceText(d.usageSource),
  });
  return {
    el,
    showUnassigned: () => {
      state.view = 'people';
      lastView = 'people';
      state.unassignedOnly = true;
      renderSeg();
      show();
      if (people) renderPeople();
      el.scrollIntoView({ block: 'start' });
    },
  };
}

/* ------------------------------------------------------------------ organization structure and CSV import */

/** Text of the chosen file. Excel on Korean Windows saves "CSV (comma delimited)" in code page 949, not UTF-8. */
async function readCsvFile(file) {
  let text = await file.text();
  if (text.includes('\ufffd')) {
    try {
      text = new TextDecoder('euc-kr', { fatal: true }).decode(await file.arrayBuffer());
    } catch (e) {
      /* Not code page 949 either: send the text as read and let the check report the rows it cannot use. */
    }
  }
  return text.replace(/^\ufeff/, '');
}

function diffStat(label, v) {
  const el = stat(label, fmt.int(countOf(v)));
  if (Array.isArray(v) && v.length) el.title = `${v.slice(0, 20).join(', ')}${v.length > 20 ? ' \u2026' : ''}`;
  return el;
}

function importErrors(errors, total) {
  const shown = errors.length;
  const fix = t('Fix the rows below in the file, then choose it again to run the check again.');
  return h('div', { class: 'stack-sm' },
    banner({
      tone: 'danger',
      title: t('The file has errors'),
      text: shown && shown < total ? `${fix} ${t('Showing the first {n} of {total} errors.', { n: fmt.int(shown), total: fmt.int(total) })}` : fix,
    }),
    shown ? dataTable({
      columns: [
        { key: 'row', label: t('Row'), align: 'num', format: fmt.int },
        {
          key: 'code',
          label: t('Problem'),
          render: (e) => h('span', { title: e.message ? serverText(e.message) : null }, problemLabel(e.code)),
          value: (e) => problemLabel(e.code),
          csv: (e) => problemLabel(e.code),
        },
        {
          key: 'value',
          label: t('Value'),
          render: (e) => (e.value === null || e.value === undefined || e.value === ''
            ? DASH : h('span', { class: 'cell-mono truncate', title: String(e.value) }, String(e.value))),
        },
      ],
      rows: errors,
      pageSize: 10,
      searchable: shown > 10,
      exportName: 'org-import-errors',
    }) : null);
}

function orgCard(ctx, d, onTree) {
  const admin = isAdmin();
  const om = d.orgModel || {};
  const infoHost = h('div');

  const renderInfo = (tree) => {
    const src = (tree && tree.source) || {};
    const counts = (tree && tree.counts) || {};
    const pick = (k) => (tree && tree[k] !== undefined && tree[k] !== null ? tree[k] : om[k]);
    const count = (k) => countOf(counts[k] !== undefined ? counts[k] : om[k]);
    const version = pick('version');
    const updatedAt = pick('updatedAt');
    const labelKey = pick('labelKey');
    const updated = updatedAt ? [fmt.dateTime(updatedAt), tree && tree.updatedBy].filter(Boolean).join(' \u00b7 ') : t('Never');
    const imported = src.importedAt || isNum(src.rows)
      ? [isNum(src.rows) ? t('{n} rows', { n: fmt.int(src.rows) }) : null, src.importedAt ? fmt.dateTime(src.importedAt) : null, src.importedBy || null].filter(Boolean).join(' \u00b7 ')
      : null;
    mount(infoHost, kv([
      [t('Version'), isNum(version) ? fmt.int(version) : DASH],
      [t('Last updated'), updated],
      [t('Units'), fmt.int(count('units'))],
      [t('Members'), fmt.int(count('members'))],
      [t('Projects'), fmt.int(count('projects'))],
      [t('Project label key'), labelKey ? h('span', { class: 'cell-mono' }, String(labelKey)) : t('Not used')],
      imported ? [t('Last import'), imported] : null,
      src.sha256 ? [t('File SHA-256'), h('span', { class: 'cell-mono', title: String(src.sha256) }, `${String(src.sha256).slice(0, 16)}\u2026`)] : null,
    ]));
  };
  renderInfo(null);
  // The tree is the current structure (a closed month's summary carries the structure of its close) and adds who
  // updated it and which file was imported.
  api.get('/api/v1/org/tree', null, { signal: ctx.signal })
    .then((res) => {
      if (ctx.signal.aborted || !res.data) return;
      renderInfo(res.data);
      if (onTree) onTree(res.data);
    })
    .catch(() => { /* keep the values from the summary */ });

  const help = h('div', { class: 'stack-sm' },
    h('p', { class: 'muted' }, t('CSV columns: principal,unit_path,cost_center, with this header row. One row per person or project; a row with an empty principal only defines a unit.')),
    h('p', { class: 'muted' }, t('principal: an e-mail address, user:EMAIL, a workforce identity principal URI or project:PROJECT_ID. unit_path: unit names separated by /, for example Company/Division/Team (up to 10 levels). cost_center is optional.')),
    h('p', { class: 'muted' }, t('Save the file as CSV UTF-8, up to 5 MB. Every file is checked first, and applying it replaces the whole structure. Closed months keep the figures they were closed with.')));
  const templateBtn = button({
    label: t('Download template'),
    icon: 'download',
    size: 'sm',
    onClick: () => downloadText('organization-structure-template.csv', toCsv(TEMPLATE_COLUMNS, TEMPLATE_ROWS)),
  });

  const imp = { csv: null, result: null, name: '' };
  const resultHost = h('div', { class: 'stack', hidden: true });
  const fileName = h('span', { class: 'muted truncate' }, t('No file selected'));
  const fileInput = h('input', { type: 'file', accept: '.csv,text/csv', class: 'sr-only', tabindex: '-1', 'aria-hidden': 'true' });
  const chooseBtn = button({
    label: t('Import organization CSV'),
    icon: 'upload',
    variant: 'primary',
    onClick: () => { if (requireAdmin(t('Import organization CSV'))) fileInput.click(); },
  });

  const reset = () => {
    imp.csv = null;
    imp.result = null;
    imp.name = '';
    fileName.textContent = t('No file selected');
    mount(resultHost);
    resultHost.hidden = true;
  };

  const problem = (text) => {
    resultHost.hidden = false;
    mount(resultHost, banner({ tone: 'danger', title: t('The file could not be checked'), text }));
  };

  const applyImport = async (applyBtn, cancelBtn) => {
    if (!requireAdmin(t('Import organization CSV'))) return;
    const r = imp.result;
    if (!r || !imp.csv) return;
    setBusy(applyBtn, true, t('Applying'));
    cancelBtn.disabled = true;
    chooseBtn.disabled = true;
    try {
      const res = await api.post('/api/v1/org/import', { csv: imp.csv, dryRun: false, expectSha256: r.sha256 });
      const a = res.data || {};
      const n = (k) => fmt.int(countOf(a[k] !== undefined ? a[k] : r[k]));
      toast({
        title: t('Organization structure imported'),
        text: t('Version {v}. Units: {units}, members: {members}, projects: {projects}.', {
          v: isNum(a.version) ? fmt.int(a.version) : DASH, units: n('units'), members: n('members'), projects: n('projects'),
        }),
        tone: 'success',
      });
      ctx.rerender();
    } catch (err) {
      setBusy(applyBtn, false);
      cancelBtn.disabled = false;
      chooseBtn.disabled = false;
      toast({ title: t('Import failed'), text: errorText(err), tone: 'danger', timeout: 9000 });
    }
  };

  const renderResult = () => {
    const r = imp.result || {};
    const diff = r.diff || {};
    const errors = Array.isArray(r.errors) ? r.errors : [];
    const errorCount = isNum(r.errorCount) ? r.errorCount : errors.length;
    const warningCount = isNum(r.warningCount) ? r.warningCount : (Array.isArray(r.warnings) ? r.warnings.length : 0);
    const valid = r.valid === true && errorCount === 0;
    const changed = DIFF_KEYS.some((k) => {
      const v = countOf(diff[k]);
      return isNum(v) && v > 0;
    });
    const applyBtn = button({
      label: t('Apply'),
      icon: 'check',
      variant: 'primary',
      disabled: !valid,
      title: valid ? null : t('Fix the errors in the file and check it again before applying.'),
    });
    const cancelBtn = button({ label: t('Cancel'), onClick: reset });
    applyBtn.addEventListener('click', () => applyImport(applyBtn, cancelBtn));
    resultHost.hidden = false;
    mount(resultHost,
      h('div', { class: 'row wrap' },
        h('span', { class: 'fw-600' }, t('Check result')),
        h('span', { class: 'muted truncate', title: imp.name || null }, imp.name),
        valid
          ? badge(t('Ready to apply'), 'success', { dot: true, size: 'sm' })
          : badge(t('{n} errors', { n: fmt.int(errorCount) }), 'danger', { dot: true, size: 'sm' })),
      h('div', { class: 'stat-row' },
        stat(t('Rows'), fmt.int(countOf(r.rows))),
        stat(t('Units'), fmt.int(countOf(r.units))),
        stat(t('Members'), fmt.int(countOf(r.members))),
        stat(t('Projects'), fmt.int(countOf(r.projects)))),
      h('div', { class: 'stat-row' },
        diffStat(t('Units added'), diff.unitsAdded),
        diffStat(t('Units removed'), diff.unitsRemoved),
        diffStat(t('Members added'), diff.membersAdded),
        diffStat(t('Members removed'), diff.membersRemoved),
        diffStat(t('Members moved'), diff.membersMoved),
        diffStat(t('Projects changed'), diff.projectsChanged)),
      valid && !changed ? h('p', { class: 'muted' }, t('The file matches the current organization structure.')) : null,
      errorCount ? importErrors(errors, errorCount) : null,
      warningCount ? banner({
        tone: 'warning',
        title: t('Duplicate rows: {n}', { n: fmt.int(warningCount) }),
        text: t('Repeated rows are ignored and do not block the import.'),
      }) : null,
      h('div', { class: 'row' }, applyBtn, cancelBtn));
  };

  const check = async (file) => {
    reset();
    imp.name = file.name || '';
    fileName.textContent = imp.name || t('No file selected');
    if (file.size > MAX_CSV_BYTES) {
      problem(t('The CSV file is larger than {a} MB.', { a: fmt.int(MAX_CSV_MB) }));
      return;
    }
    if (!file.size) {
      problem(t('The CSV file is empty.'));
      return;
    }
    setBusy(chooseBtn, true, t('Checking'));
    try {
      const csv = await readCsvFile(file);
      const res = await api.post('/api/v1/org/import', { csv, dryRun: true });
      imp.csv = csv;
      imp.result = res.data || {};
      renderResult();
    } catch (err) {
      problem(err instanceof ApiError ? errorText(err) : t('The file could not be read. Choose it again.'));
    } finally {
      setBusy(chooseBtn, false);
    }
  };

  fileInput.addEventListener('change', () => {
    const file = fileInput.files && fileInput.files[0];
    if (file) check(file).finally(() => { fileInput.value = ''; });
  });

  const el = card({
    title: t('Organization structure'),
    subtitle: t('People and projects mapped to organization units and cost centers.'),
    actions: templateBtn,
    body: h('div', { class: 'stack' },
      infoHost,
      help,
      admin
        ? h('div', { class: 'row wrap' }, fileInput, chooseBtn, fileName)
        : h('p', { class: 'muted' }, t('Only administrators can import the organization structure.')),
      admin ? resultHost : null),
  });
  return {
    el,
    focusImport: () => {
      el.scrollIntoView({ block: 'start' });
      if (admin) chooseBtn.focus({ preventScroll: true });
    },
  };
}

/* ------------------------------------------------------------------ month-close history */

function historyCard(ctx, d) {
  const closes = (Array.isArray(d.closes) ? d.closes : []).filter(Boolean);
  // Newest first: a later entry for the same month was replaced by a re-close.
  const seen = new Set();
  const superseded = new Set();
  closes.forEach((c, i) => {
    if (seen.has(c.month)) superseded.add(i);
    seen.add(c.month);
  });
  const rows = closes.map((c, i) => ({ ...c, superseded: superseded.has(i) }));
  return card({
    title: t('Month-close history'),
    subtitle: t('A close freezes the figures of a month. A re-close adds a new close that replaces the previous one.'),
    flush: true,
    body: rows.length ? dataTable({
      columns: [
        { key: 'month', label: t('Month'), render: (c) => monthName(c.month) },
        { key: 'closedAt', label: t('Closed at'), render: (c) => fmt.dateTime(c.closedAt) },
        { key: 'closedBy', label: t('Closed by'), render: (c) => (c.closedBy ? h('span', { class: 'truncate', title: String(c.closedBy) }, String(c.closedBy)) : DASH) },
        {
          key: 'closeId',
          label: t('Close ID'),
          render: (c) => h('span', { class: 'row' }, monoOrDash(c.closeId), c.superseded ? badge(t('Replaced'), 'neutral', { size: 'sm' }) : null),
        },
      ],
      rows,
      pageSize: 12,
      searchable: rows.length > 12,
      exportName: 'chargeback-closes',
      onRowClick: (c) => { if (MONTH_RE.test(c.month || '') && c.month !== d.month) ctx.navigate(`#/chargeback?month=${c.month}`); },
    }) : emptyState({
      icon: 'lock',
      compact: true,
      title: t('No month closed yet'),
      body: isAdmin()
        ? t('Close a finished month to freeze its figures for invoicing.')
        : t('An administrator closes each finished month to freeze its figures.'),
    }),
  });
}
