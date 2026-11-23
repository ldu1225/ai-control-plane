/**
 * Antigravity Controls: per-user caps on tokens, requests and estimated cost within a settlement
 * period, enforced through project IAM (agy-admin 6-state model, marker roles, safety gates G1-G5).
 * Over-cap users are suspended until the period resets, automatically (scheduled enforcement runs)
 * or after administrator review; exception rules allow, block or individually limit specific users.
 */
import { h, mount, append } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api, isAbort } from '../core/api.js';
import { isAdmin, rangeParams, store } from '../core/state.js';
import {
  pageHeader, kpi, card, dataTable, badge, statusBadge, button, banner, segmented, selectInput, tabs, progress, userCell, confirmDialog, openModal,
  toast, setBusy, field, textInput, emptyState, warningsNotice, errorState, skeleton, load, kv, requireAdmin,
} from '../core/components.js';
import { abortReasonText, errorText, serverText } from '../core/messages.js';
import { proportionBar } from '../core/charts.js';
import {
  IAM_STATE_COLORS, IAM_STATE_LABELS, REMEDIATION, reasonLabel, openUserDrawer, pageSkeleton, rangeText,
  modelCostCell, costShareCell, filteredCostSummary, capRuleText, capPeriodText, removeExceptionRule, ruleTag,
} from './_shared.js';

const GATE_TEXT = {
  G1_POLICY_READABLE: ['Policy readable', 'The project IAM policy can be read. Failure aborts the run.'],
  G2_ANY_DETERMINATE: ['Determinate classification', 'At least one user could be classified. Failure aborts the run.'],
  G3_TELEMETRY_HEALTHY: ['Telemetry healthy', 'Usage telemetry returned records. When degraded, automatic restores are withheld.'],
  G4_CHANGE_BUDGET: ['Change budget', 'At most this many people are suspended in one run (circuit breaker); the rest follow in the next runs. Restores are never limited.'],
  G5_NON_USER_GRANT_ABSENT: ['No group or domain grants', 'Access is not granted through groups or domains. Affected users require manual action.'],
  G6_DENY_CAPACITY: ['Deny policy capacity', 'An attachment point lists at most 2,500 people. A warning starts at 2,000, and new suspensions that would go over the limit wait.'],
  G7_SURGE_GUARD: ['Surge guard', 'When one run would newly suspend at least the number and the share of people set in Settings, nobody is suspended and the run asks for a review.'],
  // G1 as the IAM deny policy method checks it (the gate id stays G1_POLICY_READABLE).
  G1_DENY_POLICY_READABLE: ['Deny policies readable', 'The deny policy can be read at every attachment point. Failure aborts the run.'],
};

const PERIODS = [['daily', 'Daily'], ['weekly', 'Weekly'], ['monthly', 'Monthly'], ['custom', 'Custom cycle'], ['all', 'Last 90 days']];
const REASON_LABELS = {
  tokens: 'Tokens', requests: 'Requests', cost: 'Estimated cost',
  budget: 'Paid budget spent', credit: 'Weekly free credit used up', paid: 'Paid usage this week',
  unit_budget: 'Organization unit budget used up',
};
const TRIGGER_LABELS = { auto: 'Automatic', usage: 'Real-time usage', review: 'Review approval', bulk: 'Bulk apply', admin: 'Administrator', scheduler: 'Scheduler', unblock: 'Block lifted' };
const RULE_HINTS = {
  allow: 'The user is never suspended for exceeding a cap. A suspended user is restored now.',
  limit: 'The user is evaluated against these caps instead of the default policy. Empty fields keep the default cap.',
  block: 'Antigravity access is removed now (administrator block marker) and stays removed until the rule is deleted.',
};
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const SUSPENDED = 'CAPPED_BY_TOOL';
let activeTab = 'usage';

export default async function render(ctx) {
  const admin = isAdmin();
  let status = null;
  // Shared with the enforcement status card: its latest data, its refresh and a callback for the gates card.
  const hooks = { enforcement: null, refresh: null, onEnforcement: null };
  const policyBtn = button({
    label: t('Edit policy'),
    icon: 'edit',
    onClick: () => {
      if (!requireAdmin(t('Edit policy'))) return;
      if (status) editPolicy(ctx, status.policy || {});
    },
  });
  const dryBtn = button({
    label: t('Dry run'),
    icon: 'play',
    onClick: () => {
      if (!requireAdmin(t('Dry run'), 'resourcemanager.projects.getIamPolicy')) return;
      if (status) runEnforce(ctx, status, true, dryBtn, hooks);
    },
  });
  const runBtn = button({
    label: t('Run enforcement now'),
    icon: 'zap',
    variant: 'primary',
    onClick: () => {
      if (!requireAdmin(t('Run enforcement now'), 'resourcemanager.projects.setIamPolicy')) return;
      if (status) runEnforce(ctx, status, false, runBtn, hooks);
    },
  });
  const header = pageHeader({
    title: t('Antigravity Controls'),
    desc: t('Per-user model usage, estimated costs, spending caps and access control.'),
    actions: [policyBtn, dryBtn, runBtn],
  });
  const body = h('div', { class: 'stack-lg' });
  mount(ctx.root, header, body);
  mount(body, pageSkeleton(4, 2));

  try {
    const res = await api.get('/api/v1/governance/antigravity/status', null, { signal: ctx.signal });
    status = res.data;
    if (ctx.signal.aborted) return null;
    mount(body);
    const notice = warningsNotice(res.meta && res.meta.warnings);
    if (notice) body.append(notice);
  } catch (err) {
    if (isAbort(err) || ctx.signal.aborted) return null;
    mount(body, card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    return null;
  }

  if (admin && status.plan && status.plan.aborted) {
    runBtn.disabled = true;
    runBtn.title = t('A safety gate failed. Resolve it before running enforcement.');
  }
  renderStatus(ctx, body, status, hooks);
  return null;
}

/* ------------------------------------------------------------------ text helpers */

function ccyOf(policy) {
  return (policy && policy.currency) || 'USD';
}

function periodLabel(policy) {
  if (policy.period === 'all') return t('Last 90 days');
  const label = policy.period === 'custom'
    ? t('Every {days} days', { days: fmt.int(isNum(policy.cycleDays) && policy.cycleDays >= 1 ? policy.cycleDays : 30) })
    : t((PERIODS.find((p) => p[0] === policy.period) || [null, policy.period || DASH])[1]);
  return `${label} \u00b7 ${policy.window === 'rolling' ? t('Rolling') : t('Calendar')}`;
}

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

function resetText(policy) {
  if (policy.period === 'all' || policy.window === 'rolling') return t('Rolling window: usage leaves the window continuously');
  const tz = policy.resetTimezone || 'UTC';
  const time = policy.resetTime || '00:00';
  if (policy.period === 'daily') return t('Resets every day at {time} ({tz})', { time, tz });
  if (policy.period === 'weekly') return t('Resets every {day} at {time} ({tz})', { day: t(WEEKDAYS[policy.resetWeekday || 0] || 'Monday'), time, tz });
  if (policy.period === 'custom') {
    const days = fmt.int(isNum(policy.cycleDays) && policy.cycleDays >= 1 ? policy.cycleDays : 30);
    const start = policy.cycleStart && policy.cycleStart !== '1970-01-01' ? policy.cycleStart : '';
    return start
      ? t('Resets every {days} days from {start} at {time} ({tz})', { days, start, time, tz })
      : t('Resets every {days} days at {time} ({tz})', { days, time, tz });
  }
  const day = isNum(policy.resetDay) && policy.resetDay >= 1 && policy.resetDay <= 31 ? policy.resetDay : 1;
  if (day === 1) return t('Resets on the 1st of every month at {time} ({tz})', { time, tz });
  if (day >= 29) return t('Resets on day {day} of every month (last day in shorter months) at {time} ({tz})', { day: fmt.int(day), time, tz });
  return t('Resets on day {day} of every month at {time} ({tz})', { day: fmt.int(day), time, tz });
}

function enforcementRunNote(run) {
  if (!run) return null;
  if (run.started && run.via === 'job') return t('Enforcement job started to apply the change. The status updates in about a minute.');
  if (run.started && run.via === 'run' && (run.suspended || run.restored)) {
    return t('Applied immediately: {s} suspended \u00b7 {r} restored.', { s: fmt.int(run.suspended), r: fmt.int(run.restored) });
  }
  if (run.error) return t('Saved, but immediate enforcement could not start; the next scheduled run applies it.');
  return null;
}

function scheduleJobRefresh(ctx, run) {
  if (!run || !run.started || run.via !== 'job') return;
  [30000, 75000].forEach((ms) => {
    setTimeout(() => {
      if (window.location.hash.startsWith('#/antigravity') && !document.querySelector('dialog[open]')) ctx.rerender();
    }, ms);
  });
}

function realtimeSub(rt) {
  if (!rt || !rt.configured) return t('Schedule only. Redeploy with REALTIME_ENFORCEMENT=true to trigger enforcement as soon as usage logs arrive.');
  const parts = [];
  if (rt.lastEventAt) parts.push(t('Last usage event {when}', { when: fmt.relative(rt.lastEventAt) }));
  if (rt.lastTriggerAt) {
    parts.push(t('Last triggered {when} ({n} total)', { when: fmt.relative(rt.lastTriggerAt), n: fmt.int(rt.triggers || 0) }));
  }
  if (!parts.length) return t('Listening for Antigravity usage logs. Scheduled runs remain the safety net for period resets.');
  return parts.join(' \u00b7 ');
}

/** Weekly per-person cap that keeps everyone inside the Antigravity credit pool: the monthly credit per licensed
 * user (Gemini Enterprise Standard USD 10, Plus USD 15) divided by 4, the pool Google enforces every 7 days. */
function weeklyCreditUsd(policy) {
  const monthly = isNum(policy.creditPerUserMonth) && policy.creditPerUserMonth > 0 ? policy.creditPerUserMonth : 10;
  return Math.round((monthly / 4) * 100) / 100;
}

function capsText(policy) {
  const ccy = ccyOf(policy);
  return [
    t('Tokens {v}', { v: policy.tokenCap ? fmt.compact(policy.tokenCap) : t('off') }),
    t('Requests {v}', { v: policy.requestCap ? fmt.int(policy.requestCap) : t('off') }),
    t('Cost {v}', { v: policy.costCap ? fmt.currency(policy.costCap, ccy) : t('off') }),
  ].join(' \u00b7 ');
}

function windowText(policy) {
  if (!policy.windowStart) return DASH;
  return `${fmt.dateTime(policy.windowStart)} \u2013 ${fmt.dateTime(policy.windowEnd)}`;
}

function money(v, ccy) {
  if (!isNum(v)) return DASH;
  return fmt.currency(v, ccy, { precise: true });
}

/** Credit caps: weekly free credit (Gemini models) + monthly paid budget per person, in USD. */
function isCredit(policy) {
  return Boolean(policy) && policy.capModel === 'credit_budget';
}

function usd(v) {
  return money(v, 'USD');
}

/** A paid budget: null = no limit, 0 = credits only. */
function budgetText(b) {
  if (!isNum(b)) return t('No limit');
  return b === 0 ? t('Credits only') : usd(b);
}

function creditWeekCell(c) {
  if (!c) return h('span', { class: 'text-muted' }, DASH);
  const ratio = c.creditWeek > 0 ? c.freeUsedWeek / c.creditWeek : 0;
  return h('div', { class: 'cell-stack' },
    h('div', { class: 'share-cell' }, progress(Math.min(1, ratio), ratio >= 1 ? 'warning' : 'success'),
      h('span', null, `${usd(c.freeUsedWeek)} / ${usd(c.creditWeek)}`)),
    h('span', { class: 'cell-sub' }, t('{v} left', { v: usd(c.freeLeftWeek) })));
}

function paidMonthCell(c) {
  if (!c) return h('span', { class: 'text-muted' }, DASH);
  const b = c.budget;
  let ratio = 0;
  if (isNum(b) && b > 0) ratio = c.paidMonth / b;
  else if (b === 0 && c.paidMonth > 0) ratio = 1;
  const tone = ratio >= 1 ? 'danger' : ratio >= 0.8 ? 'warning' : 'success';
  return h('div', { class: 'cell-stack' },
    h('div', { class: 'share-cell' }, isNum(b) ? progress(Math.min(1, ratio), tone) : null,
      h('span', { class: c.budgetUsedUp ? 'fw-600 text-danger' : 'fw-600' }, usd(c.paidMonth))),
    h('span', { class: 'cell-sub' }, isNum(b) ? t('of {cap}', { cap: budgetText(b) }) : t('No limit')));
}

function weeklyCapsText(policy) {
  return [
    t('Tokens {v}', { v: policy.tokenCap ? fmt.compact(policy.tokenCap) : t('off') }),
    t('Requests {v}', { v: policy.requestCap ? fmt.int(policy.requestCap) : t('off') }),
  ].join(' \u00b7 ');
}

function costBasis(pricing) {
  if (!pricing || !pricing.configured) return pricing && pricing.source === 'manual' ? t('Token prices not entered') : t('No token prices found');
  if (pricing.source === 'manual') return t('Estimated with entered prices');
  const c = pricing.counts || {};
  if (c.billing && c.catalog) return t('Estimated with billed prices and the Google price list');
  return c.billing ? t('Estimated with billed prices') : t('Estimated with the Google price list');
}

function reasonBadges(reasons) {
  if (!reasons || !reasons.length) return h('span', { class: 'text-muted' }, DASH);
  const tone = (r) => (r === 'cost' || r === 'budget' ? 'warning' : r === 'credit' ? 'info' : 'danger');
  return h('div', { class: 'chips' }, reasons.map((r) => badge(t(REASON_LABELS[r] || r), tone(r), { size: 'sm' })));
}

function capUsageCell(ratio) {
  if (!isNum(ratio)) return h('span', { class: 'text-muted' }, DASH);
  const tone = ratio >= 1 ? 'danger' : ratio >= 0.8 ? 'warning' : 'success';
  return h('div', { class: 'share-cell' }, progress(Math.min(1, ratio), tone), h('span', { class: ratio >= 1 ? 'text-danger fw-600' : '' }, fmt.pct(ratio, 0)));
}

function capValue(value, cap, format) {
  return h('div', { class: 'cell-stack' },
    h('span', { class: 'fw-600' }, isNum(value) ? format(value) : DASH),
    isNum(cap) && cap > 0 ? h('span', { class: 'cell-sub' }, t('of {cap}', { cap: format(cap) })) : null);
}

/** The kind of rule only; where it matters the end is shown next to it (Expires column) or by ruleTag. */
function ruleTypeBadge(rule) {
  if (!rule) return h('span', { class: 'text-muted' }, DASH);
  if (rule.type === 'block') return badge(t('Always block'), 'danger', { size: 'sm' });
  if (rule.type === 'limit') return badge(t('Individual cap'), 'accent', { size: 'sm' });
  return badge(rule.until ? t('Allow') : t('Always allow'), 'success', { size: 'sm' });
}

/** When an "allow" decision made now ends: the end of the current cap period (the next reset, or one rolling window). */
function allowEndOf(policy) {
  return (policy && (policy.periodEnd || policy.nextReset)) || null;
}

function ruleDetail(rule, ccy) {
  if (rule.type === 'limit') {
    const parts = [];
    if (isNum(rule.tokenCap)) parts.push(t('Tokens {v}', { v: fmt.compact(rule.tokenCap) }));
    if (isNum(rule.requestCap)) parts.push(t('Requests {v}', { v: fmt.int(rule.requestCap) }));
    if (isNum(rule.costCap)) parts.push(t('Cost {v}', { v: fmt.currency(rule.costCap, ccy) }));
    if (isNum(rule.paidBudget)) parts.push(t('Paid budget {v}', { v: budgetText(rule.paidBudget) }));
    if (isNum(rule.creditMonth)) parts.push(t('Credit {v} a month', { v: usd(rule.creditMonth) }));
    return parts.join(' \u00b7 ');
  }
  if (rule.type === 'block') return t('Antigravity access removed until the rule is deleted');
  return t('Not suspended for going over the cap');
}

/* ------------------------------------------------------------------ page */

function gateTone(s) {
  return s === 'pass' ? 'pass' : s === 'warn' ? 'warn' : 'fail';
}

/** Safety gates; with the IAM deny policy method, G1 is about the deny policies at every attachment point. */
function gateList(gates, backend) {
  return h('div', { class: 'gate-list' }, (gates || []).map((g) => {
    const key = g.id === 'G1_POLICY_READABLE' && backend === 'iam_deny' ? 'G1_DENY_POLICY_READABLE' : g.id;
    const txt = GATE_TEXT[key] || [g.id, ''];
    const tone = gateTone(g.status);
    return h('div', { class: ['gate', tone] },
      h('span', { class: 'gate-icon' }, icon(tone === 'pass' ? 'check' : tone === 'warn' ? 'alert-triangle' : 'x', { size: 15 })),
      h('div', { class: 'cell-stack' },
        h('div', { class: 'row' }, h('span', { class: 'gate-id' }, g.id), h('span', { class: 'fw-600' }, t(txt[0]))),
        h('div', { class: 'gate-detail' }, g.detail ? serverText(g.detail) : t(txt[1]))),
      statusBadge(g.status, { size: 'sm' }));
  }));
}

function compactControlBar(ctx, s) {
  const policy = s.policy || {};
  const enforcement = s.enforcement || {};
  const rt = enforcement.realtime || {};
  const mode = policy.enforcementMode || 'manual';
  const credit = isCredit(policy);
  const last = enforcement.lastRun;
  const control = segmented([
    { value: 'manual', label: t('Manual review'), title: t('Over-cap users wait for an administrator decision') },
    {
      value: 'auto',
      label: t('Automatic suspension'),
      title: rt.configured
        ? t('Over-cap users are suspended as soon as a usage log arrives (IAM takes a few minutes to apply)')
        : t('Over-cap users are suspended by every enforcement run'),
    },
  ], mode, (v) => {
    if (!requireAdmin(t('Enforcement mode'))) { ctx.rerender(); return; }
    setMode(ctx, v, rt.configured);
  }, { label: t('Enforcement mode') });
  const fact = (label, value, title = null) => h('div', { class: 'agy-control-fact', title },
    h('span', { class: 'agy-control-fact-label' }, label),
    h('span', { class: 'agy-control-fact-value' }, value));
  const capSummary = credit
    ? t('{w}/wk free \u00b7 {b}/mo paid', { w: usd(policy.creditPerWeek), b: budgetText(policy.paidBudgetMonth) })
    : capsText(policy);
  const periodValue = capPeriodText(policy) || DASH;
  const periodTitle = policy.nextReset ? `${resetText(policy)} \u00b7 ${fmt.relative(policy.nextReset)}` : resetText(policy);
  const methodValue = rt.configured && mode === 'auto'
    ? `${backendLabel(backendOf(s))} \u00b7 ${t('Real-time')}`
    : backendLabel(backendOf(s));
  const methodTitle = rt.configured ? `${backendSub(backendOf(s)) || ''} ${realtimeSub(rt)}`.trim() : backendSub(backendOf(s));
  const lastRunText = last ? `${fmt.relative(last.at)} \u00b7 ${t(statusLabel(last.result))}` : t('Not run yet');
  return h('div', { class: 'agy-control-bar' },
    h('div', { class: 'agy-control-facts' },
      fact(t('Cap policy'), capSummary, credit ? resetText(policy) : `${periodLabel(policy)} \u00b7 ${resetText(policy)}`),
      fact(t('Cap period'), periodValue, periodTitle),
      fact(t('Enforcement method'), methodValue, methodTitle),
      fact(t('Last run'), lastRunText, last ? runSummary(last) : null)),
    control);
}

function userStatusCell(u, ctx, s) {
  const tz = s && s.policy ? s.policy.resetTimezone : null;
  const b = statusBadge(u.state, { size: 'sm' });
  if (u.reason) b.title = reasonLabel(u.reason);
  else if (u.restoreAt) b.title = t('Restored at {time}', { time: fmt.dateTimeDay(u.restoreAt, tz) });
  const planned = u.plannedAction && u.plannedAction !== 'none' ? statusBadge(u.plannedAction, { size: 'sm' }) : null;
  const canRevoke = Boolean(ctx && u.exception && u.exception.type === 'allow');
  return h('div', { class: 'status-cell' },
    h('div', { class: 'status-cell-row' }, b, planned),
    u.exception ? ruleTag(u.exception, {
      email: u.email,
      onRevoke: canRevoke ? () => removeException(ctx, s, { ...u.exception, email: u.email }) : null,
    }) : null,
    u.restoreAt ? h('span', { class: 'cell-sub' }, t('Back {time}', { time: fmt.dateTimeDay(u.restoreAt, tz) })) : null);
}

function renderStatus(ctx, body, s, hooks) {
  const policy = s.policy || {};
  const pop = s.population || { byState: {} };
  const plan = s.plan || {};
  const totals = s.totals || {};
  const enforcement = s.enforcement || {};
  const rt = enforcement.realtime || {};
  const mode = policy.enforcementMode || 'manual';
  const ccy = ccyOf(policy);
  const credit = isCredit(policy);
  const usageUnavailable = s.usageSource === 'unavailable';
  const prereq = s.prerequisites || {};
  const metaLog = prereq.metadataLogging || {};
  const missingMetaLocs = metaLog.missingLocations || [];

  if (plan.aborted) {
    body.append(banner({ tone: 'danger', title: t('Enforcement would abort'), text: abortReasonText(plan.abortReason) || t('A safety gate failed. No IAM changes will be applied until the gate passes.') }));
  } else if (plan.withheldRestores) {
    body.append(banner({ tone: 'warning', title: t('Automatic restores withheld'), text: t('Usage telemetry is degraded (gate G3), so users capped earlier will not be restored automatically in this cycle. Suspensions whose settlement period has ended are still restored.') }));
  }
  if ((plan.deferredBlocks || []).length) {
    body.append(banner({ tone: 'warning', title: t('Suspensions spread over several runs'), text: t('{n} more people are over their limits than the change budget allows in one run ({m} per run). The people furthest over are suspended first and the rest in the next runs, every few minutes. Restores are never limited.', { n: fmt.int(plan.deferredBlocks.length), m: fmt.int(plan.changeBudget) }) }));
  }
  if (usageUnavailable) {
    body.append(banner({ tone: 'warning', title: t('Antigravity usage could not be read'), text: t('The inference log query failed. Grant the service identity roles/logging.viewer (or read access to the BigQuery sink table) and check the data source notices above.') }));
  } else if (missingMetaLocs.length) {
    body.append(banner({ tone: 'warning', title: t('Metadata logging is off in an active location'), text: t('AI developer tools is turned on in {missing}, but Metadata logging is off there. Antigravity calls routed to that location (for example global for Gemini 4 and preview models) do not write {log}. Open Gemini Enterprise > Settings > AI developer tools > Compliance, switch the location to each listed region, and turn on Metadata logging.', { missing: formatLocationList(missingMetaLocs), log: prereq.inferenceLog || t('set in Settings') }) }));
  } else if (prereq.telemetryDetected === false) {
    body.append(banner({ tone: 'info', title: t('No Antigravity token usage logs in this project'), text: t('Turn on Metadata logging in Gemini Enterprise > Settings > AI developer tools > Compliance (off by default). From then on every Antigravity call writes {log} with the user and token counts, and usage, cost and caps appear here.', { log: prereq.inferenceLog || t('set in Settings') }) }));
  }
  if (enforcement.durable === false) {
    body.append(banner({ tone: 'warning', title: t('Exception rules are not stored durably'), text: t('Settings are kept in memory on this deployment, so exception rules and suspension records are lost when the service restarts. Configure a settings bucket (SETTINGS_BUCKET).') }));
  }
  if ((enforcement.scheduler || {}).stale) {
    const lastAt = ((enforcement.scheduler || {}).lastRun || {}).at;
    body.append(banner({ tone: 'danger', title: t('Scheduled enforcement stopped'), text: t('The enforcement schedule last ran {when}. Suspended users are not restored at the period reset until it runs again. Check the Cloud Scheduler job {job} or select Run enforcement now.', { when: lastAt ? fmt.relative(lastAt) : DASH, job: 'ai-control-plane-enforce' }) }));
  }
  if (isNum(totals.suspended) && totals.suspended > 0 && (enforcement.scheduler || {}).configured === false) {
    body.append(banner({ tone: 'warning', title: t('No enforcement schedule'), text: t('{n} users are suspended, but no enforcement schedule is configured. Their access returns only when an enforcement run happens after the settlement period resets. Select Run enforcement now after each reset, or set ENFORCEMENT_SCHEDULE and redeploy.', { n: fmt.int(totals.suspended) }) }));
  }

  const usersList = Array.isArray(s.users) ? s.users : [];
  const plannedBlocks = (plan.block || []).length;
  const reviewCount = mode === 'manual' ? (isNum(totals.review) ? totals.review : 0) : plannedBlocks;
  const suspendedCount = isNum(totals.suspended) ? totals.suspended : 0;
  const gateIssues = (s.gates || []).filter((g) => g.status && g.status !== 'pass').length;

  let tabBar = null;
  const goTab = (id) => { if (tabBar) tabBar.setActive(id); };

  const costKpi = credit
    ? kpi({
      label: t('Paid this month'),
      value: isNum(totals.paidMonth) ? usd(totals.paidMonth) : DASH,
      meta: h('span', null, isNum(policy.orgPaidBudgetMonth) ? t('Organization budget {v}', { v: budgetText(policy.orgPaidBudgetMonth) }) : t('Free credit applied {v}', { v: usd(totals.freeMonth) })),
      tip: t('Claude usage and Gemini usage beyond each person\'s weekly free share, priced per token. Google bills the project and applies the credit as a shared pool, so the invoice can be lower than this sum.'),
    })
    : kpi({
      label: t('Estimated cost'),
      value: isNum(totals.estimatedCost) ? fmt.currency(totals.estimatedCost, ccy) : DASH,
      meta: h('span', null, costBasis(policy.pricing)),
      tip: t('Per-user cost is estimated from each person\'s tokens per model and the price per token: what your billing account actually paid, or the public Google price list. Billing exports do not break cost down by user.'),
    });

  let queueLabel = t('Awaiting review');
  let queueMeta = t('Administrator decision required');
  if (mode === 'auto') {
    queueLabel = rt.configured ? t('Pending auto-suspension') : t('Suspended at next run');
    queueMeta = rt.configured
      ? t('Suspends immediately on usage events (IAM takes a few minutes)')
      : t('Applied by the next enforcement run');
  }

  body.append(h('div', { class: 'kpi-grid' },
    kpi({
      label: credit ? t('People this month') : t('Users in period'),
      value: usageUnavailable ? DASH : fmt.int(pop.total),
      meta: h('span', null, windowText(policy)),
      onClick: () => goTab('usage'),
    }),
    costKpi,
    kpi({
      label: queueLabel,
      value: fmt.int(reviewCount),
      meta: h('span', null, queueMeta),
      onClick: () => goTab('review'),
    }),
    kpi({
      label: t('Suspended now'),
      value: fmt.int(suspendedCount),
      meta: h('span', null, policy.nextReset ? t('Restored at {time}', { time: fmt.dateTimeDay(policy.nextReset, policy.resetTimezone) }) : t('Restored when usage falls below the cap')),
      onClick: () => goTab('review'),
    })));

  body.append(compactControlBar(ctx, s));

  const validTabs = ['usage', 'review', 'report', 'operations'];
  if (!validTabs.includes(activeTab)) activeTab = 'usage';

  const panels = {
    usage: h('div', { id: 'agy-panel-usage', class: ['agy-tab-panel', 'stack-lg'], role: 'tabpanel', 'aria-labelledby': 'agy-tab-usage' }),
    review: h('div', { id: 'agy-panel-review', class: ['agy-tab-panel', 'stack-lg'], role: 'tabpanel', 'aria-labelledby': 'agy-tab-review' }),
    report: h('div', { id: 'agy-panel-report', class: ['agy-tab-panel', 'stack-lg'], role: 'tabpanel', 'aria-labelledby': 'agy-tab-report' }),
    operations: h('div', { id: 'agy-panel-operations', class: ['agy-tab-panel', 'stack-lg'], role: 'tabpanel', 'aria-labelledby': 'agy-tab-operations' }),
  };

  /* ---- Tab 1: Users & model costs ---- */
  const budgetCell = (u) => {
    const own = u.exception && u.exception.type === 'limit' && isNum(u.exception.paidBudget);
    const b = u.credit ? u.credit.budget : (u.caps ? u.caps.paidBudget : null);
    return h('div', { class: 'row nowrap cap-cell' },
      h('div', { class: 'cell-stack' }, h('span', { class: own ? 'fw-600' : '' }, budgetText(b)), h('span', { class: 'cell-sub' }, own ? t('Individual') : t('Default'))),
      button({ icon: 'edit', size: 'sm', title: t('Change paid budget'), onClick: () => { if (requireAdmin(t('Change paid budget'))) editPersonCap(ctx, s, u); } }));
  };
  const creditColumns = [
    { key: 'email', label: t('User'), render: (u) => userCell(u.email) },
    {
      key: 'models', label: t('Models & cost'), sortable: false,
      render: (u) => modelCostCell(u.byModel, 'USD'),
      csv: (u) => (u.byModel || []).map((m) => `${m.model}:${isNum(m.cost) ? m.cost : m.tokens}`).join('|'),
    },
    { key: 'estimatedCost', label: t('This week'), align: 'num', value: (u) => u.estimatedCost, render: (u) => h('div', { class: 'cell-stack' }, h('span', { class: 'fw-600', title: u.costPartial ? t('Some models have no unit price') : null }, usd(u.estimatedCost), u.costPartial ? ' *' : ''), h('span', { class: 'cell-sub' }, isNum(u.tokens) ? t('{n} tokens', { n: fmt.compact(u.tokens) }) : DASH)) },
    { key: 'freeWeek', label: t('Free credit this week'), align: 'num', value: (u) => (u.credit ? u.credit.freeUsedWeek : null), render: (u) => creditWeekCell(u.credit), csv: (u) => (u.credit ? u.credit.freeUsedWeek : '') },
    { key: 'paidMonth', label: t('Paid this month'), align: 'num', value: (u) => (u.credit ? u.credit.paidMonth : null), render: (u) => paidMonthCell(u.credit), csv: (u) => (u.credit ? u.credit.paidMonth : '') },
    { key: 'budget', label: t('Paid budget'), align: 'num', value: (u) => (u.credit ? u.credit.budget : null), render: budgetCell, csv: (u) => (u.credit && isNum(u.credit.budget) ? u.credit.budget : '') },
    { key: 'state', label: t('Status'), render: (u) => userStatusCell(u, ctx, s), value: (u) => u.state || '' },
  ];
  const periodColumns = [
    { key: 'email', label: t('User'), render: (u) => userCell(u.email) },
    {
      key: 'models', label: t('Models & cost'), sortable: false,
      render: (u) => modelCostCell(u.byModel, ccy),
      csv: (u) => (u.byModel || []).map((m) => `${m.model}:${isNum(m.cost) ? m.cost : m.tokens}`).join('|'),
    },
    {
      key: 'tokens', label: t('Tokens'), align: 'num',
      render: (u) => (isNum(u.tokens)
        ? h('div', { class: 'cell-stack' },
          h('span', { class: 'fw-600' }, fmt.compact(u.tokens)),
          h('span', { class: 'cell-sub' }, t('{n} requests', { n: fmt.int(u.requests) })))
        : h('span', { class: 'text-muted' }, DASH)),
      value: (u) => u.tokens,
    },
    {
      key: 'estimatedCost', label: t('Estimated cost'), align: 'num',
      render: (u) => h('span', { class: 'fw-600', title: u.costPartial ? t('Some models have no unit price') : null }, money(u.estimatedCost, ccy), u.costPartial ? ' *' : ''),
      value: (u) => u.estimatedCost,
    },
    { key: 'capUsage', label: t('Cap usage'), align: 'num', value: (u) => u.capUsage, render: (u) => capUsageCell(u.capUsage) },
    {
      key: 'costCap', label: t('Cost cap'), align: 'num', value: (u) => (u.caps ? u.caps.costCap : null),
      render: (u) => {
        const own = u.exception && u.exception.type === 'limit' && isNum(u.exception.costCap);
        const capVal = u.caps ? u.caps.costCap : null;
        const label = h('div', { class: 'cell-stack' },
          h('span', { class: own ? 'fw-600' : '' }, isNum(capVal) && capVal > 0 ? fmt.currency(capVal, ccy) : t('None')),
          h('span', { class: 'cell-sub' }, own ? t('Individual') : t('Default')));
        return h('div', { class: 'row nowrap cap-cell' }, label, button({
          icon: 'edit', size: 'sm', title: t('Change cost cap'),
          onClick: () => {
            if (!requireAdmin(t('Change cost cap'))) return;
            editPersonCap(ctx, s, u);
          },
        }));
      },
      csv: (u) => (u.caps ? u.caps.costCap : ''),
    },
    { key: 'state', label: t('Status'), render: (u) => userStatusCell(u, ctx, s), value: (u) => u.state || '' },
  ];
  const peopleState = { view: 'user', user: 'all', model: 'all' };
  const peopleHost = h('div');
  const flatPeopleModels = flattenUserModels(usersList);
  const renderPeopleCard = () => {
    if (peopleState.view === 'models') {
      mount(peopleHost, userModelBreakdownTable(ctx, flatPeopleModels, {
        state: peopleState,
        onFilter: renderPeopleCard,
        ccy: credit ? 'USD' : ccy,
        showCreditTag: credit,
        exportName: 'antigravity-user-models',
      }));
      return;
    }
    mount(peopleHost, dataTable({
      columns: credit ? creditColumns : periodColumns,
      rows: s.users, pageSize: 15, exportName: 'antigravity-usage',
      initialSort: credit ? { key: 'paidMonth', dir: 'desc' } : { key: 'estimatedCost', dir: 'desc' },
      emptyText: t('No Antigravity usage was recorded in this window.'),
      unavailableText: t('Usage and IAM data could not be read, so the user population is unknown.'),
      onRowClick: (u) => u.uid && openUserDrawer(u.uid, { email: u.email, onChanged: () => ctx.rerender() }),
    }));
  };
  const peopleToggle = segmented(
    [{ value: 'user', label: t('By user') }, { value: 'models', label: t('By user & model') }],
    'user', (v) => { peopleState.view = v; renderPeopleCard(); }, { label: t('View by') });
  renderPeopleCard();
  panels.usage.append(card({
    title: t('Users & model costs'),
    subtitle: credit
      ? t('This week {range} \u00b7 paid this month from the 1st', { range: capPeriodText(policy) || windowText(policy) })
      : t('Cap period {range} \u00b7 usage as of {time}', { range: capPeriodText(policy) || DASH, time: fmt.dateTime(policy.windowEnd) }),
    actions: peopleToggle,
    flush: true, source: prereq.inferenceLog || undefined,
    body: peopleHost,
  }));

  /* ---- Tab 2: Review & access ---- */
  panels.review.append(queueCard(ctx, s), suspendedCard(ctx, s), exceptionsCard(ctx, s));
  const manual = s.manualActions || [];
  if (s.manualActions === null || s.manualActions === undefined || manual.length) {
    const manualBody = (s.manualActions === null || s.manualActions === undefined)
      ? emptyState({ icon: 'alert-triangle', tone: 'warning', title: t('Cannot be determined'), body: t('Manual actions are computed only when both the IAM policy and the usage telemetry can be read.'), compact: true })
      : dataTable({
        columns: [
          { key: 'email', label: t('User'), render: (r) => userCell(r.email) },
          { key: 'state', label: t('State'), render: (r) => statusBadge(r.state, { size: 'sm' }) },
          { key: 'reason', label: t('Reason'), render: (r) => badge(reasonLabel(r.reason) || DASH, 'info', { size: 'sm' }) },
          { key: 'remediation', label: t('Remediation'), sortable: false, wrap: true, render: (r) => h('span', { class: 'cell-sub' }, t(REMEDIATION[r.reason] || r.remediation || '')) },
        ],
        rows: manual, pageSize: 8, exportName: 'antigravity-manual-actions', searchable: manual.length > 8,
      });
    panels.review.append(card({
      title: t('Manual action required'), subtitle: t('Users whose access cannot be enforced through individual project bindings'), flush: true,
      body: manualBody,
    }));
  }

  /* ---- Tab 3 (lazy): Monthly report & cloud agent activity ---- */
  let reportMounted = false;
  const mountReportTab = () => {
    if (reportMounted) return;
    reportMounted = true;
    panels.report.append(billingReportCard(ctx), agentActivityCard(ctx));
  };

  /* ---- Tab 4 (lazy): Policy, enforcement job, gates, audit log & prerequisites ---- */
  let opsMounted = false;
  const mountOpsTab = () => {
    if (opsMounted) return;
    opsMounted = true;
    const row1 = h('div', { class: 'grid' });
    const pricing = policy.pricing || {};
    const policyCard = card({
      title: t('Cap policy'), subtitle: credit ? t('Default limits per person, in USD') : t('Default caps per user and settlement period'),
      actions: button({
        label: t('Edit policy'), icon: 'edit', size: 'sm',
        onClick: () => {
          if (!requireAdmin(t('Edit policy'))) return;
          editPolicy(ctx, policy);
        },
      }),
      body: credit ? kv([
        [t('Caps'), t('Weekly free credit + monthly paid budget')],
        [t('Credit per person'), t('{m} a month ({w} a week, Gemini models only)', { m: usd(policy.creditPerUserMonth), w: usd(policy.creditPerWeek) })],
        [t('Paid budget per person'), budgetText(policy.paidBudgetMonth)],
        [t('Organization paid budget'), isNum(policy.orgPaidBudgetMonth) ? t('{v} ({used} used this month)', { v: usd(policy.orgPaidBudgetMonth), used: usd(policy.orgPaidMonth) }) : t('No limit')],
        [t('Weekly reset'), resetText(policy)],
        [t('Budget month'), t('From the 1st at 00:00 ({tz})', { tz: policy.resetTimezone || 'UTC' })],
        policy.tokenCap || policy.requestCap ? [t('Weekly token and request caps'), weeklyCapsText(policy)] : null,
        [t('Token prices'), pricingSummary(pricing, 'USD')],
        [t('Change budget'), t('At most {n} suspensions per run', { n: fmt.int(policy.changeBudget) })],
        [t('Usage source'), { bigquery: t('BigQuery log sink'), logging: t('Cloud Logging API'), unavailable: t('Unavailable') }[s.usageSource] || s.usageSource],
      ]) : kv([
        [t('Caps'), t('Fixed caps per period')],
        [t('Token cap'), policy.tokenCap ? fmt.int(policy.tokenCap) : t('Disabled')],
        [t('Request cap'), policy.requestCap ? fmt.int(policy.requestCap) : t('Disabled')],
        [t('Cost cap per person'), policy.costCap ? h('span', null, fmt.currency(policy.costCap, ccy), pricing.configured ? null : h('span', { class: 'text-warning' }, ` \u00b7 ${t('no token prices')}`)) : t('Disabled')],
        [t('Token prices'), pricingSummary(pricing, ccy)],
        [t('Settlement period'), periodLabel(policy)],
        [t('Period reset'), resetText(policy)],
        [t('Change budget'), t('At most {n} suspensions per run', { n: fmt.int(policy.changeBudget) })],
        [t('Usage source'), { bigquery: t('BigQuery log sink'), logging: t('Cloud Logging API'), unavailable: t('Unavailable') }[s.usageSource] || s.usageSource],
      ]),
    });
    const priceTable = usedPriceTable(pricing, ccy);
    if (priceTable) policyCard.bodyEl.append(h('div', { class: 'policy-prices' }, priceTable));
    policyCard.classList.add('span-5');
    const gatesCard = card({ title: t('Safety gates'), subtitle: t('Evaluated before any IAM change'), body: gateList(s.gates, backendOf(s)) });
    gatesCard.classList.add('span-7');
    if (hooks && !backendOf(s)) {
      hooks.onEnforcement = (e) => { if (e && e.backend) mount(gatesCard.bodyEl, gateList(s.gates, e.backend)); };
      if (hooks.enforcement) hooks.onEnforcement(hooks.enforcement);
    }
    row1.append(policyCard, gatesCard);
    panels.operations.append(row1);
    panels.operations.append(enforcementStatusCard(ctx, s, hooks));

    const segments = Object.keys(IAM_STATE_LABELS).map((st) => ({ label: t(IAM_STATE_LABELS[st]), value: (pop.byState || {})[st] || 0, color: IAM_STATE_COLORS[st] }));
    panels.operations.append(card({
      title: t('Access state population'), subtitle: t('Users with usage in the window or a direct binding on the access or marker roles'),
      body: pop.byState ? h('div', null, proportionBar(segments), h('div', { class: 'state-legend' }, segments.map((sg) => {
        const sw = h('span', { class: 'swatch round' });
        sw.style.setProperty('background', sg.color);
        return h('div', { class: 'sl-row' }, sw, h('span', null, sg.label), h('strong', null, fmt.int(sg.value)));
      }))) : emptyState({ icon: 'alert-triangle', tone: 'warning', compact: true, title: t('Population unknown'), body: t('Usage and IAM data could not be read, so the user population is unknown.') }),
    }));

    const row3 = h('div', { class: 'grid' });
    const auditHost = h('div');
    const auditCard = card({ title: t('Administrative audit log'), subtitle: t('Actions by this control plane and agy-admin'), flush: true, body: auditHost, source: 'logs/agy-admin%2Fadmin_actions' });
    auditCard.classList.add('span-8');
    const prereqCard = card({ title: t('Enforcement prerequisites'), subtitle: t('Marker roles, permissions and telemetry'), body: prerequisites(ctx, s.prerequisites || {}) });
    prereqCard.classList.add('span-4');
    row3.append(auditCard, prereqCard);
    panels.operations.append(row3);

    load(auditHost, () => api.get('/api/v1/governance/audit-log', { limit: 50 }, { signal: ctx.signal }), (res) => {
      const raw = res.data.items;
      const items = raw || [];
      const auditWarnings = warningsNotice(res.meta && res.meta.warnings);
      let content;
      if (raw === null || raw === undefined) {
        content = emptyState({ icon: 'alert-triangle', tone: 'warning', title: t('Audit log unavailable'), body: t('The administrative audit log could not be read. Grant the service identity roles/logging.viewer.'), compact: true });
      } else if (items.length) {
        content = dataTable({
          columns: [
            { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time, true), value: (r) => r.time },
            { key: 'actor', label: t('Actor'), render: (r) => h('span', { class: 'truncate' }, r.actor || DASH) },
            { key: 'action', label: t('Action'), render: (r) => h('span', { class: 'cell-mono' }, r.action) },
            { key: 'target', label: t('Target'), render: (r) => h('span', { class: 'truncate' }, r.target || DASH) },
            { key: 'tool', label: t('Source'), render: (r) => badge(r.tool || DASH, r.tool === 'control-plane' ? 'accent' : 'neutral', { size: 'sm' }) },
            { key: 'result', label: t('Result'), render: (r) => statusBadge(r.result, { size: 'sm' }) },
          ],
          rows: items, pageSize: 8, searchable: false, exportName: 'antigravity-audit-log',
        });
      } else {
        content = emptyState({ icon: 'file-text', title: t('No administrative actions'), body: t('Enforcement runs, review decisions, exception rules and allow or disallow actions are recorded here.'), compact: true });
      }
      append(auditHost, [auditWarnings && raw ? h('div', { class: 'card-body flush-top' }, auditWarnings) : null, content]);
    }, { skeletonKind: 'table', signal: ctx.signal });
  };

  const syncTabs = (id) => {
    activeTab = id;
    Object.entries(panels).forEach(([k, p]) => { p.hidden = k !== id; });
    if (id === 'report') mountReportTab();
    if (id === 'operations') mountOpsTab();
  };

  const opsIssues = gateIssues + (missingMetaLocs.length ? 1 : 0);
  tabBar = tabs([
    { id: 'usage', tabId: 'agy-tab-usage', controls: 'agy-panel-usage', label: t('Users & model costs'), count: usersList.length },
    {
      id: 'review', tabId: 'agy-tab-review', controls: 'agy-panel-review', label: t('Review & access'),
      count: reviewCount + suspendedCount + (s.exceptions || []).length, tone: reviewCount > 0 ? 'warning' : '',
    },
    { id: 'report', tabId: 'agy-tab-report', controls: 'agy-panel-report', label: t('Monthly report') },
    {
      id: 'operations', tabId: 'agy-tab-operations', controls: 'agy-panel-operations', label: t('Policy & operations'),
      count: opsIssues > 0 ? opsIssues : null, tone: opsIssues > 0 ? 'warning' : '',
    },
  ], activeTab, syncTabs);
  tabBar.classList.add('agy-tabs');
  syncTabs(activeTab);

  body.append(tabBar, panels.usage, panels.review, panels.report, panels.operations);
}

function flattenUserModels(users) {
  const out = [];
  (users || []).forEach((u) => {
    const models = Array.isArray(u.byModel) ? u.byModel : [];
    const totalCost = models.reduce((acc, m) => acc + (isNum(m.cost) ? m.cost : 0), 0);
    models.forEach((m) => {
      const share = isNum(m.costShare) ? m.costShare : (isNum(m.cost) && totalCost > 0 ? m.cost / totalCost : null);
      out.push({
        uid: u.uid,
        email: u.email,
        model: m.model,
        requests: m.requests,
        promptTokens: m.promptTokens,
        cachedTokens: m.cachedTokens,
        outputTokens: m.outputTokens,
        tokens: m.tokens,
        cost: m.cost,
        costPartial: m.costPartial,
        costShare: share,
        credit: m.credit,
      });
    });
  });
  return out;
}

function userModelBreakdownTable(ctx, allRows, { state, onFilter, ccy, showCreditTag, exportName }) {
  const emails = Array.from(new Set(allRows.map((r) => r.email).filter(Boolean))).sort();
  const models = Array.from(new Set(allRows.map((r) => r.model).filter(Boolean))).sort();
  if (state.user !== 'all' && !emails.includes(state.user)) state.user = 'all';
  if (state.model !== 'all' && !models.includes(state.model)) state.model = 'all';
  const rows = allRows.filter((r) => (state.user === 'all' || r.email === state.user)
    && (state.model === 'all' || r.model === state.model));
  const toolbar = h('div', { class: 'row wrap' },
    selectInput(
      [{ value: 'all', label: t('All users') }, ...emails.map((em) => ({ value: em, label: em }))],
      state.user, (v) => { state.user = v; onFilter(); }, { 'aria-label': t('Filter by user') }),
    selectInput(
      [{ value: 'all', label: t('All models') }, ...models.map((m) => ({ value: m, label: m }))],
      state.model, (v) => { state.model = v; onFilter(); }, { 'aria-label': t('Filter by model') }));
  return h('div', null,
    filteredCostSummary(rows, ccy),
    dataTable({
      columns: [
        { key: 'email', label: t('User'), render: (r) => userCell(r.email) },
        {
          key: 'model',
          label: t('Model'),
          render: (r) => h('div', { class: 'row wrap' },
            h('span', { class: 'cell-mono' }, r.model || DASH),
            showCreditTag && r.credit === false ? badge(t('Paid model'), 'warning', { size: 'sm' }) : null),
          value: (r) => r.model || '',
          csv: (r) => r.model || '',
        },
        { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
        { key: 'promptTokens', label: t('Input tokens'), align: 'num', format: fmt.compact, csv: (r) => r.promptTokens },
        { key: 'cachedTokens', label: t('From cache'), align: 'num', format: fmt.compact, csv: (r) => r.cachedTokens },
        { key: 'outputTokens', label: t('Output tokens'), align: 'num', format: fmt.compact, csv: (r) => r.outputTokens },
        { key: 'tokens', label: t('Total tokens'), align: 'num', render: (r) => h('span', { class: 'fw-600' }, fmt.compact(r.tokens)), value: (r) => r.tokens, csv: (r) => r.tokens },
        {
          key: 'cost',
          label: t('Estimated cost'),
          align: 'num',
          render: (r) => h('span', { class: 'fw-600', title: r.costPartial ? t('Some models have no unit price') : null }, money(r.cost, ccy), r.costPartial ? ' *' : ''),
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
      rows,
      pageSize: 15,
      exportName,
      initialSort: { key: 'cost', dir: 'desc' },
      toolbar,
      emptyText: t('No per-user model usage was recorded in this window.'),
      onRowClick: (r) => r.uid && openUserDrawer(r.uid, { email: r.email, onChanged: () => ctx.rerender() }),
    }));
}

/* ------------------------------------------------------------------ monthly usage report (chargeback) */

function monthOptions(count = 6) {
  const out = [];
  const d = new Date();
  for (let i = 0; i < count; i += 1) {
    const y = d.getFullYear();
    const m = d.getMonth() + 1;
    out.push(`${y}-${String(m).padStart(2, '0')}`);
    d.setDate(1);
    d.setMonth(d.getMonth() - 1);
  }
  return out;
}

function billingReportCard(ctx) {
  const months = monthOptions();
  const state = { month: months[0], credit: true, view: 'user', user: 'all', model: 'all' };
  let lastRes = null;
  const host = h('div');
  const monthSel = selectInput(months.map((m) => ({ value: m, label: m })), state.month, (v) => { state.month = v; refresh(); }, { 'aria-label': t('Month') });
  const creditSeg = segmented([{ value: 'yes', label: t('Deduct credit') }, { value: 'no', label: t('Full amount') }], 'yes', (v) => { state.credit = v === 'yes'; renderBody(); });
  const viewSeg = segmented([{ value: 'user', label: t('By user') }, { value: 'models', label: t('By user & model') }], 'user', (v) => { state.view = v; renderBody(); }, { label: t('View by') });
  const out = card({
    title: t('Monthly usage report'),
    subtitle: t('Per-person Antigravity tokens and cost for chargeback'),
    actions: h('div', { class: 'row wrap' }, monthSel, viewSeg, creditSeg),
    flush: true,
    body: host,
  });
  function renderBody() {
    if (!lastRes) return;
    const r = lastRes.data || {};
    const ccy = r.currency || 'USD';
    const rows = r.users || [];
    const notice = warningsNotice(lastRes.meta && lastRes.meta.warnings);
    const amount = (u) => (state.credit ? u.chargeable : u.cost);
    const creditCaps = r.capModel === 'credit_budget';
    let content;
    if (!rows.length) {
      content = emptyState({ icon: 'file-text', compact: true, title: t('No Antigravity usage in {month}', { month: r.month || state.month }), body: t('Usage appears when Antigravity for organizations writes its inference log with Metadata logging turned on.') });
    } else if (state.view === 'models') {
      content = userModelBreakdownTable(ctx, flattenUserModels(rows), {
        state,
        onFilter: renderBody,
        ccy,
        showCreditTag: creditCaps,
        exportName: `antigravity-usage-${r.month || state.month}-by-model`,
      });
    } else {
      content = dataTable({
        columns: creditCaps ? [
          { key: 'email', label: t('User'), render: (u) => userCell(u.email) },
          {
            key: 'models', label: t('Models & cost'), sortable: false,
            render: (u) => modelCostCell(u.byModel, 'USD'),
            csv: (u) => (u.byModel || []).map((m) => `${m.model}:${isNum(m.cost) ? m.cost : m.tokens}`).join('|'),
          },
          { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
          { key: 'tokens', label: t('Tokens'), align: 'num', format: fmt.compact, csv: (u) => u.tokens },
          { key: 'gemini', label: t('Gemini'), align: 'num', render: (u) => usd(u.gemini), value: (u) => u.gemini },
          { key: 'other', label: t('Claude and other models'), align: 'num', render: (u) => usd(u.other), value: (u) => u.other },
          { key: 'credit', label: t('Free credit'), align: 'num', render: (u) => (state.credit ? usd(u.credit) : h('span', { class: 'text-muted' }, DASH)), value: (u) => (state.credit ? u.credit : null) },
          { key: 'chargeable', label: state.credit ? t('Amount to charge') : t('Cost'), align: 'num', render: (u) => h('span', { class: 'fw-600', title: u.costPartial ? t('Some models have no unit price') : null }, usd(amount(u)), u.costPartial ? ' *' : ''), value: (u) => amount(u) },
          { key: 'budget', label: t('Paid budget'), align: 'num', render: (u) => budgetText(u.budget), value: (u) => u.budget, csv: (u) => (isNum(u.budget) ? u.budget : '') },
        ] : [
          { key: 'email', label: t('User'), render: (u) => userCell(u.email) },
          {
            key: 'models', label: t('Models & cost'), sortable: false,
            render: (u) => modelCostCell(u.byModel, ccy),
            csv: (u) => (u.byModel || []).map((m) => `${m.model}:${isNum(m.cost) ? m.cost : m.tokens}`).join('|'),
          },
          { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
          { key: 'promptTokens', label: t('Input tokens'), align: 'num', format: fmt.compact, csv: (u) => u.promptTokens },
          { key: 'cachedTokens', label: t('From cache'), align: 'num', format: fmt.compact, csv: (u) => u.cachedTokens },
          { key: 'outputTokens', label: t('Output tokens'), align: 'num', format: fmt.compact, csv: (u) => u.outputTokens },
          { key: 'cost', label: t('Cost'), align: 'num', render: (u) => h('span', { title: u.costPartial ? t('Some models have no unit price') : null }, money(u.cost, ccy), u.costPartial ? ' *' : ''), value: (u) => u.cost },
          { key: 'credit', label: t('Credit'), align: 'num', render: (u) => (state.credit ? money(u.credit, ccy) : h('span', { class: 'text-muted' }, DASH)), value: (u) => (state.credit ? u.credit : null) },
          { key: 'chargeable', label: state.credit ? t('Amount to charge') : t('Cost'), align: 'num', render: (u) => h('span', { class: 'fw-600' }, money(amount(u), ccy)), value: (u) => amount(u) },
        ],
        rows, pageSize: 10, searchable: rows.length > 10, exportName: `antigravity-usage-${r.month || state.month}`,
        initialSort: { key: 'chargeable', dir: 'desc' },
        onRowClick: (u) => u.uid && openUserDrawer(u.uid, { email: u.email, onChanged: () => ctx.rerender() }),
      });
    }
    const totals = r.totals || {};
    const foot = h('div', { class: 'card-body stack-sm' },
      h('div', { class: 'row wrap' },
        h('span', { class: 'fw-600' }, t('Total {v}', { v: money(state.credit ? totals.chargeable : totals.cost, ccy) })),
        h('span', { class: 'text-muted' }, t('{n} people \u00b7 {month} ({tz})', { n: fmt.int(rows.length), month: r.month || state.month, tz: r.timezone || '' })),
        r.complete === false ? badge(t('Month in progress'), 'warning', { size: 'sm' }) : null),
      h('p', { class: 'muted' }, t('Tokens are what Google logged for each call (businessaicode.googleapis.com/inference_response). Cost = tokens x unit price of each model; prompt tokens served from cache use the cache price. Google bills the project, not people, so this is how the project\'s usage is divided between people.')),
      creditCaps
        ? h('p', { class: 'muted' }, t('Free credit: {w} per person per week ({m} a month / 4), for Gemini models only; unused credit does not carry over. Claude is always paid. Amount to charge = Claude + Gemini beyond the weekly free share. Google bills the project and applies the credit as a shared pool, so the invoice can be lower than the sum.', { w: usd(r.creditPerWeek), m: usd(r.creditPerUserMonthUsd) }))
        : (state.credit ? h('p', { class: 'muted' }, t('Credit: {v} per person per month (Gemini Enterprise Antigravity credit, Settings > Governance).', { v: money(r.creditPerUser, ccy) })) : null));
    append(host, [notice ? h('div', { class: 'card-body flush-top' }, notice) : null, content, foot]);
  }
  function refresh() {
    load(host, () => api.get('/api/v1/governance/antigravity/billing-report', { month: state.month }, { signal: ctx.signal }), (res) => {
      lastRes = res;
      renderBody();
    }, { skeletonKind: 'table', signal: ctx.signal });
  }
  refresh();
  return out;
}

/* ------------------------------------------------------------------ enforcement mode and runs */

function enfItem(label, value, sub) {
  return h('div', { class: 'enf-item' },
    h('span', { class: 'enf-label' }, label),
    h('span', { class: 'enf-value' }, value),
    sub ? h('span', { class: 'enf-sub' }, sub) : null);
}

function runSummary(run) {
  const who = TRIGGER_LABELS[run.trigger] ? t(TRIGGER_LABELS[run.trigger]) : (run.trigger || DASH);
  const counts = t('{b} suspended \u00b7 {r} restored \u00b7 {d} blocked by rules \u00b7 {q} awaiting review', { b: fmt.int(run.blocked), r: fmt.int(run.restored), d: fmt.int(run.disallowed), q: fmt.int(run.review) });
  return `${who} \u00b7 ${t(statusLabel(run.result))} \u00b7 ${counts}`;
}

function statusLabel(result) {
  return { success: 'Success', partial: 'Partial', failed: 'Failed', aborted: 'Aborted', dry_run: 'Dry run' }[result] || result || DASH;
}

async function setMode(ctx, next, realtime = false) {
  const autoMsg = realtime
    ? t('Users already over their cap are evaluated right away, and new over-cap usage triggers suspension automatically as soon as usage logs arrive (IAM takes a few minutes to apply). Access returns automatically when the period resets. Exception rules and safety gates still apply.')
    : t('Users already over their cap are evaluated right away, and every enforcement run suspends over-cap users until the settlement period resets. Exception rules and safety gates still apply.');
  const ok = await confirmDialog({
    title: next === 'auto' ? t('Turn on automatic suspension') : t('Switch to manual review'),
    message: next === 'auto'
      ? autoMsg
      : t('Enforcement runs will no longer suspend anyone. Users over their cap wait in the review queue for an administrator decision. Suspensions still end when the period resets.'),
    confirmLabel: next === 'auto' ? t('Turn on') : t('Switch'),
    tone: next === 'auto' ? 'danger' : 'primary',
  });
  if (!ok) { ctx.rerender(); return; }
  try {
    const res = await api.post('/api/v1/settings', { section: 'governance', values: { enforcement_mode: next } });
    const run = res && res.data && res.data.enforcement;
    const note = enforcementRunNote(run);
    toast({
      title: next === 'auto' ? t('Automatic suspension is on') : t('Manual review is on'),
      text: note,
      tone: run && run.error ? 'warning' : 'success',
      timeout: note ? 9000 : 4800,
    });
    scheduleJobRefresh(ctx, run);
  } catch (err) {
    toast({ title: t('Save failed'), text: errorText(err), tone: 'danger' });
  }
  ctx.rerender();
}

async function runEnforce(ctx, s, dry, btn, hooks) {
  const mode = (s.policy || {}).enforcementMode || 'manual';
  const known = (hooks && hooks.enforcement) || {};
  const backend = backendOf(s) || known.backend || 'iam_allow_swap';
  const changes = mode === 'auto' && backend !== 'notify_only';
  if (!dry) {
    let message;
    if (mode !== 'auto') message = t('Manual review mode: nobody is suspended by this run. Suspensions whose period ended are restored and exception rules are applied; users over their cap stay in the review queue.');
    else if (backend === 'iam_deny') message = t('People over their limits are added to the deny policy at every attachment point until the settlement period resets, people whose suspension ended are removed from it and exception rules are applied. Changes take a few minutes to take effect.');
    else if (backend === 'notify_only') message = t('Notify only: no access is changed. The run records and notifies what it would do.');
    else message = t('IAM bindings are changed in one atomic update: users over their cap are suspended until the settlement period resets, suspensions whose period ended are restored and exception rules are applied. Users that are not enforceable are never modified.');
    const ok = await confirmDialog({
      title: t('Run enforcement now'),
      message: known.job && known.job.configured ? `${message} ${t('The enforcement job applies the changes with its own service account.')}` : message,
      confirmLabel: t('Run now'),
      tone: changes ? 'danger' : 'primary',
      requireText: changes ? 'APPLY' : null,
    });
    if (!ok) return;
  }
  setBusy(btn, true, dry ? t('Simulating') : t('Applying'));
  try {
    const res = await api.post('/api/v1/governance/antigravity/enforce', dry ? { dryRun: true } : {});
    setBusy(btn, false);
    const r = res.data || {};
    // With an enforcement job the request only starts an execution; the job applies the changes.
    if (r.triggered) jobStarted(ctx, r, hooks);
    else showResult(ctx, r, backend);
  } catch (err) {
    setBusy(btn, false);
    toast({ title: dry ? t('Dry run failed') : t('Enforcement failed'), text: errorText(err), tone: 'danger', timeout: 9000 });
  }
}

function showResult(ctx, r, backend) {
  const plan = r.plan || {};
  const changes = r.changes || [];
  const applied = changes.filter((c) => c.status === 'applied').length;
  const statBox = (label, value, sub) => h('div', { class: 'stat' }, h('span', { class: 'stat-label' }, label), h('span', { class: 'stat-value' }, fmt.int(value)), sub ? h('span', { class: 'stat-sub' }, sub) : null);
  const m = openModal({
    title: r.mode === 'apply' ? t('Enforcement result') : t('Dry run result'),
    subtitle: r.executed ? t('{n} IAM changes applied', { n: applied }) : t('No IAM changes were made'),
    wide: true,
    body: (el) => append(el, [
      plan.aborted ? banner({ tone: 'danger', title: t('Run aborted by safety gate'), text: abortReasonText(plan.abortReason) }) : null,
      h('div', { class: 'stat-row' },
        statBox(t('Suspend'), (plan.block || []).length),
        statBox(t('Restore'), (plan.restore || []).length, plan.withheldRestores ? t('Withheld (G3)') : null),
        statBox(t('Block (rule)'), (plan.disallow || []).length),
        statBox(t('Awaiting review'), (plan.review || []).length),
        statBox(t('Manual action'), (plan.manual || []).length)),
      gateList(r.gates, backend),
      changes.length ? dataTable({
        columns: [
          { key: 'email', label: t('User'), render: (c) => userCell(c.email) },
          { key: 'action', label: t('Action'), render: (c) => statusBadge(c.action, { size: 'sm' }) },
          { key: 'status', label: t('Status'), render: (c) => statusBadge(c.status, { size: 'sm' }) },
          { key: 'message', label: t('Detail'), wrap: true, render: (c) => h('span', { class: 'cell-sub' }, serverText(c.message)) },
        ],
        rows: changes, searchable: false, pageSize: 8,
      }) : h('p', { class: 'muted' }, (plan.review || []).length ? t('No IAM change is needed. Users over their cap are waiting in the review queue.') : t('No user requires a change for the current policy.')),
      h('div', { class: 'row wrap muted' },
        r.auditLogged ? badge(t('Recorded in audit log'), 'success', { size: 'sm', dot: true }) : badge(t('Audit log not written'), 'warning', { size: 'sm', dot: true }),
        r.webhookSent ? badge(t('Webhook notified'), 'info', { size: 'sm' }) : null),
    ]),
    footer: (el) => append(el, [button({ label: t('Close'), variant: 'primary', onClick: () => m.close() })]),
    onClose: () => { if (r.mode === 'apply') ctx.rerender(); },
  });
}

/* ------------------------------------------------------------------ enforcement method, deny policy and job */

function backendOf(s) {
  const e = (s && s.enforcement) || {};
  const p = (s && s.policy) || {};
  return e.backend || p.enforcementBackend || null;
}

function backendLabel(backend) {
  switch (backend) {
    case 'iam_deny': return t('IAM deny policy');
    case 'notify_only': return t('Notify only');
    case 'iam_allow_swap': return t('Allow policy swap');
    default: return backend ? String(backend) : DASH;
  }
}

function backendSub(backend) {
  switch (backend) {
    case 'iam_deny': return t('Suspended people are listed in a deny policy, which also stops access through groups and inherited roles.');
    case 'notify_only': return t('Runs plan and notify only; nobody loses access.');
    case 'iam_allow_swap': return t('The direct role binding is swapped for a marker role in the primary project.');
    default: return null;
  }
}

function attachmentLabel(a) {
  if (a === 'scope') return t('The folders or organization of the workspace scope');
  if (a === 'projects') return t('Every workspace project');
  return a ? String(a) : DASH;
}

/** Last path segment of a resource name or of an object's name (executions, operations, jobs). */
function shortName(v) {
  const s = String((v && typeof v === 'object' ? v.name : v) || '');
  return s.slice(s.lastIndexOf('/') + 1) || s;
}

/** Attachment point without the service prefix: cloudresourcemanager.googleapis.com/projects/p -> projects/p. */
function attachmentText(v) {
  let s = String(v || '');
  if (/%2F/i.test(s)) {
    try { s = decodeURIComponent(s); } catch (e) { /* shown as received */ }
  }
  return s.replace(/^(?:\/\/)?cloudresourcemanager\.googleapis\.com\//, '');
}

function capacityRatio(a) {
  if (isNum(a.ratio)) return a.ratio;
  return isNum(a.count) && isNum(a.limit) && a.limit > 0 ? a.count / a.limit : null;
}

function capacityCell(a) {
  const ratio = capacityRatio(a);
  if (!isNum(a.count) && !isNum(ratio)) return h('span', { class: 'text-muted' }, DASH);
  const tone = ratio >= 1 ? 'danger' : ratio >= 0.8 ? 'warning' : 'success';
  const text = isNum(a.limit) ? t('{n} of {limit}', { n: fmt.int(a.count), limit: fmt.int(a.limit) }) : fmt.int(a.count);
  return h('div', { class: 'rt-budget-bar' }, progress(Math.min(1, isNum(ratio) ? ratio : 0), tone), h('span', { class: 'tabular nowrap' }, text));
}

function attachmentState(a) {
  const ratio = capacityRatio(a);
  const out = [];
  if (a.exists === false) out.push(badge(t('No deny policy'), 'neutral', { size: 'sm' }));
  if (a.foreign) {
    out.push(badge(t('Changed outside the dashboard'), 'warning', {
      size: 'sm', title: t('This deny policy has content that the dashboard did not write, so the dashboard does not change it. Review it in Google Cloud IAM.'),
    }));
  }
  if (isNum(ratio) && ratio >= 1) out.push(badge(t('At the limit'), 'danger', { size: 'sm' }));
  else if (isNum(ratio) && ratio >= 0.8) out.push(badge(t('Near the limit'), 'warning', { size: 'sm' }));
  if (!out.length && !a.error) out.push(badge(t('Within the limit'), 'success', { size: 'sm' }));
  return h('div', { class: 'chips' }, out);
}

/**
 * Sentence for an error code of the enforcement status (GcpError kinds of gcp.py, invalid_response, internal and
 * internal_error); a code without a sentence goes through serverText.
 */
function gcpErrorText(code) {
  switch (code) {
    case 'denied':
    case 'access_denied':
    case 'permission_denied':
      return t('Access was denied for the service identity. Check the IAM roles of the runtime service account.');
    case 'not_found': return t('The resource was not found.');
    case 'credentials':
    case 'credentials_unavailable':
      return t('Google Cloud credentials are unavailable.');
    case 'timeout': return t('The request timed out.');
    case 'unavailable':
    case 'upstream_unavailable':
    case 'network':
      return t('The service is temporarily unavailable.');
    case 'invalid':
    case 'http':
      return t('The request was rejected by the service.');
    case 'conflict':
    case 'precondition':
      return t('The request conflicts with the current state. Reload and try again.');
    case 'invalid_response':
    case 'internal':
    case 'internal_error':
      return t('The data could not be processed.');
    default: return serverText(code);
  }
}

function denyErrorText(code) {
  switch (code) {
    case 'denied':
    case 'access_denied':
    case 'permission_denied':
      return t('Access denied. Grant the dashboard service account Deny Reviewer (roles/iam.denyReviewer) on the organization.');
    case 'not_found': return t('The attachment point was not found.');
    default: return gcpErrorText(code);
  }
}

function executionBadge(x) {
  const raw = String((x && x.state) || '').toUpperCase().replace(/^EXECUTION_/, '');
  let key = raw;
  if ((!key || key === 'UNKNOWN') && x) {
    if (x.runningCount > 0) key = 'RUNNING';
    else if (x.failedCount > 0) key = 'FAILED';
    else if (x.cancelledCount > 0) key = 'CANCELLED';
    else if (x.succeededCount > 0) key = 'SUCCEEDED';
  }
  if (['RUNNING', 'PENDING', 'STARTED', 'IN_PROGRESS', 'QUEUED'].includes(key)) return badge(t('Running'), 'info', { dot: true, size: 'sm' });
  if (['SUCCEEDED', 'SUCCESS', 'COMPLETED', 'DONE'].includes(key)) return badge(t('Succeeded'), 'success', { dot: true, size: 'sm' });
  if (['FAILED', 'ERROR'].includes(key)) return badge(t('Failed'), 'danger', { dot: true, size: 'sm' });
  if (['CANCELLED', 'CANCELED', 'CANCELLING'].includes(key)) return badge(t('Cancelled'), 'warning', { dot: true, size: 'sm' });
  return badge(!raw || raw === 'UNKNOWN' ? t('Unknown') : raw, 'neutral', { size: 'sm' });
}

function executionSub(x) {
  const parts = [shortName(x.name)].filter(Boolean);
  const started = x.startTime || x.createTime;
  if (started) parts.push(t('Started {when}', { when: fmt.relative(started) }));
  if (x.completionTime) parts.push(t('Finished {time}', { time: fmt.dateTime(x.completionTime) }));
  if (isNum(x.succeededCount) && x.succeededCount > 0) parts.push(t('{n} succeeded', { n: fmt.int(x.succeededCount) }));
  if (isNum(x.failedCount) && x.failedCount > 0) parts.push(t('{n} failed', { n: fmt.int(x.failedCount) }));
  if (isNum(x.cancelledCount) && x.cancelledCount > 0) parts.push(t('{n} cancelled', { n: fmt.int(x.cancelledCount) }));
  if (isNum(x.runningCount) && x.runningCount > 0) parts.push(t('{n} running', { n: fmt.int(x.runningCount) }));
  return parts.join(' \u00b7 ');
}

/** Google's state of a spend cap (spendCap.outputState, else the requested inputState). */
function spendCapState(r) {
  if (r.exists === false) return badge(t('Not created yet'), 'neutral', { size: 'sm' });
  switch (r.outputState || r.inputState) {
    case 'CONFIGURED': return badge(t('Configured'), 'success', { size: 'sm' });
    case 'ENFORCED': return badge(t('Usage paused'), 'danger', { dot: true, size: 'sm' });
    case 'AWAITING_NEXT_PERIOD': return badge(t('Lifted until next month'), 'info', { size: 'sm' });
    default: {
      const s = r.outputState || r.inputState;
      return s && s !== 'STATE_UNSPECIFIED' ? badge(String(s), 'neutral', { size: 'sm' }) : h('span', { class: 'text-muted' }, DASH);
    }
  }
}

/**
 * Spend cap rows of the enforcement status: {items: [...]}, or the spend caps status of services/spend_caps.py
 * ({desired: {project: amount}, caps: {project: cap}, currency}).
 */
function spendCapItems(sc) {
  if (Array.isArray(sc.items)) return sc.items.filter((x) => x && typeof x === 'object');
  const caps = sc.caps && typeof sc.caps === 'object' && !Array.isArray(sc.caps) ? sc.caps : {};
  const desired = sc.desired && typeof sc.desired === 'object' && !Array.isArray(sc.desired) ? sc.desired : {};
  return [...new Set([...Object.keys(desired), ...Object.keys(caps)])].sort().map((project) => {
    const c = caps[project] && typeof caps[project] === 'object' ? caps[project] : null;
    if (!c) return { project, amount: isNum(desired[project]) ? desired[project] : null, currency: 'USD', exists: false };
    return { project, amount: isNum(c.amount) ? c.amount : null, currency: c.currency || sc.currency || 'USD', inputState: c.inputState || null, outputState: c.outputState || null };
  });
}

function denySection(e) {
  const deny = e.deny;
  if (!deny) {
    return e.backend === 'iam_deny' ? h('p', { class: 'muted' }, t('The deny policy state is not available yet.')) : null;
  }
  const rows = Array.isArray(deny.attachments) ? deny.attachments.filter(Boolean) : [];
  return h('div', { class: 'stack-sm' },
    deny.readable === false ? banner({
      tone: 'warning', title: t('Deny policies could not be read'),
      text: t('Grant the dashboard service account Deny Reviewer (roles/iam.denyReviewer) on the organization to show the deny policy state here.'),
    }) : null,
    rows.length ? dataTable({
      title: t('Attachment points'),
      searchable: rows.length > 8, pageSize: 8,
      columns: [
        { key: 'attachment', label: t('Attachment point'), render: (a) => h('span', { class: 'cell-mono truncate', title: attachmentText(a.attachment) }, attachmentText(a.attachment)), value: (a) => attachmentText(a.attachment) },
        { key: 'count', label: t('People listed'), width: '240px', value: (a) => capacityRatio(a), render: capacityCell, csv: (a) => (isNum(a.count) ? a.count : '') },
        { key: 'state', label: t('State'), sortable: false, render: attachmentState, csv: (a) => (a.foreign ? 'foreign' : '') },
        { key: 'error', label: t('Detail'), sortable: false, wrap: true, render: (a) => (a.error ? h('span', { class: 'cell-sub text-danger' }, denyErrorText(a.error)) : h('span', { class: 'text-muted' }, DASH)), csv: (a) => a.error || '' },
      ],
      rows,
    }) : h('p', { class: 'muted' }, t('No attachment point yet. Save a workspace scope and run enforcement once.')));
}

function spendCapSection(sc) {
  if (!sc || typeof sc !== 'object') return null;
  if (sc.enabled === false) {
    return h('p', { class: 'muted' }, t('Google spend caps are off. Turn them on in Settings > Governance policy > Enforcement to apply project hard caps.'));
  }
  const items = spendCapItems(sc);
  let notice = null;
  if (sc.accountConfigured === false) {
    notice = banner({ tone: 'warning', title: t('The billing account ID is missing'), text: t('Spend caps are set on the billing account. Enter its ID in Settings > Workspace and data sources > Google Cloud workspace.') });
  } else if (sc.ok === false || sc.error) {
    notice = banner({ tone: 'warning', title: t('Spend caps could not be read'), text: sc.error ? serverText(sc.error) : null });
  }
  // Viewers get the on/off state only: the caps name the billing account and the projects (server viewer view).
  if (sc.adminOnly) return h('div', { class: 'stack-sm' }, notice, h('p', { class: 'muted' }, t('Only administrators see the project spend caps.')));
  return h('div', { class: 'stack-sm' },
    notice,
    items.length ? dataTable({
      title: t('Project spend caps'), searchable: false, pageSize: 8,
      columns: [
        { key: 'project', label: t('Project'), render: (r) => h('span', { class: 'cell-mono' }, r.project || DASH) },
        { key: 'amount', label: t('Monthly cap'), align: 'num', render: (r) => (isNum(r.amount) ? fmt.currency(r.amount, r.currency || 'USD') : DASH), value: (r) => r.amount },
        { key: 'outputState', label: t('Google state'), render: spendCapState, value: (r) => r.outputState || r.inputState || '' },
      ],
      rows: items,
    }) : h('p', { class: 'muted' }, t('No project has a hard cap. Set one in Chargeback > Monthly budgets.')),
    items.length ? h('p', { class: 'muted' }, t('Change the amounts or lift a paused cap in Chargeback > Monthly budgets.')) : null);
}

function renderEnforcementStatus(host, e, warnings, fallbackRealtime = null) {
  const backend = e.backend || null;
  const job = e.job || {};
  const rt = e.realtime || fallbackRealtime || {};
  const latest = job.latest || null;
  const perms = Array.isArray(e.permissions) ? e.permissions.filter(Boolean) : [];
  let where = DASH;
  let whereSub = null;
  if (backend === 'iam_deny') {
    where = attachmentLabel(e.attachment);
    whereSub = e.policyId ? t('Deny policy {id}', { id: e.policyId }) : null;
  } else if (backend === 'iam_allow_swap') {
    where = t('Primary project');
  }
  let lastValue = job.configured ? t('Not run yet') : DASH;
  if (latest) lastValue = executionBadge(latest);
  const rtBadge = rt.configured
    ? badge(t('Active'), 'success', { dot: true, size: 'sm' })
    : badge(t('Schedule only'), 'neutral', { size: 'sm' });
  append(host, [
    warningsNotice(warnings),
    h('div', { class: 'stack' },
      h('div', { class: 'enf-grid' },
        enfItem(t('Enforcement method'), backendLabel(backend), backendSub(backend)),
        enfItem(t('Applied at'), where, whereSub),
        enfItem(t('Applied by'), job.configured ? t('Enforcement job') : t('Dashboard service'), job.configured ? (shortName(job.job) || null) : t('No enforcement job is set, so the dashboard applies suspensions itself.')),
        enfItem(t('Latest job run'), lastValue, latest ? executionSub(latest) : null),
        enfItem(t('Real-time detection'), rtBadge, realtimeSub(rt))),
      backend === 'iam_deny' && (perms.length || e.geApp || e.agentPlatform) ? h('div', { class: 'row wrap' },
        h('span', { class: 'muted' }, t('Denied permissions')),
        h('div', { class: 'chips' }, perms.map((p) => h('span', { class: 'chip cell-mono' }, p))),
        e.geApp ? badge(t('Gemini Enterprise app also denied'), 'warning', { size: 'sm' }) : null,
        e.agentPlatform ? badge(t('Agent Platform model calls also denied'), 'warning', { size: 'sm' }) : null) : null,
      job.error ? banner({ tone: 'warning', title: t('The enforcement job could not be read'), text: gcpErrorText(job.error) }) : null,
      denySection(e),
      spendCapSection(e.spendCaps)),
  ]);
}

/** Method, attachment points with their capacity, the enforcement job and project spend caps (GET .../enforcement). */
function enforcementStatusCard(ctx, s, hooks) {
  const host = h('div', null, skeleton('lines'));
  const refreshBtn = button({ icon: 'refresh', size: 'sm', title: t('Refresh') });
  const out = card({
    title: t('Enforcement method and job'),
    subtitle: t('How suspensions are applied, where, and the latest run of the enforcement job'),
    actions: refreshBtn,
    body: host,
  });
  const refresh = () => load(host, () => api.get('/api/v1/governance/antigravity/enforcement', null, { signal: ctx.signal }), (res) => {
    const e = res.data || {};
    if (hooks) {
      hooks.enforcement = e;
      if (hooks.onEnforcement) hooks.onEnforcement(e);
    }
    renderEnforcementStatus(host, e, (res.meta && res.meta.warnings) || [], (s && s.enforcement && s.enforcement.realtime) || null);
  }, { skeletonKind: 'lines', signal: ctx.signal });
  refreshBtn.addEventListener('click', () => refresh());
  if (hooks) hooks.refresh = refresh;
  refresh();
  return out;
}

/** Run now with an enforcement job: the request starts an execution and the job applies the changes later. */
function jobStarted(ctx, r, hooks) {
  const exec = shortName(r.execution) || shortName(r.operation);
  toast({
    title: t('Enforcement job started'),
    text: exec
      ? t('Execution {name} applies the changes with the service account of the job. The status here updates in about a minute.', { name: exec })
      : t('The enforcement job applies the changes with its own service account. The status here updates in about a minute.'),
    tone: 'success',
    timeout: 9000,
  });
  const later = (ms, fn) => setTimeout(() => { if (!ctx.signal.aborted) fn(); }, ms);
  if (hooks && hooks.refresh) later(10000, () => hooks.refresh());
  later(60000, () => { if (!document.querySelector('dialog[open]')) ctx.rerender(); });
}

/* ------------------------------------------------------------------ review queue */

function queueCard(ctx, s) {
  const policy = s.policy || {};
  const tz = policy.resetTimezone;
  const rt = (s.enforcement && s.enforcement.realtime) || {};
  const manual = (policy.enforcementMode || 'manual') === 'manual';
  const ccy = ccyOf(policy);
  const allowEnd = allowEndOf(policy);
  const allowLabel = allowEnd ? t('Allow until {time}', { time: fmt.dateTimeDay(allowEnd, tz) }) : t('Allow');
  const rows = manual ? s.reviewQueue : (Array.isArray(s.users) ? s.users.filter((u) => u.plannedAction === 'block') : null);
  let content;
  if (rows === null || rows === undefined) {
    content = emptyState({ icon: 'alert-triangle', tone: 'warning', compact: true, title: t('Cannot be determined'), body: t('The review queue is computed only when both the IAM policy and the usage telemetry can be read.') });
  } else if (!rows.length) {
    content = emptyState({ icon: 'check-circle', compact: true, title: manual ? t('No users awaiting review') : t('No suspensions planned'), body: t('No enforceable user is over the cap in this period.') });
  } else {
    const columns = isCredit(policy) ? [
      { key: 'email', label: t('User'), render: (r) => userCell(r.email, r.exception ? t('Individual cap') : null) },
      { key: 'estimatedCost', label: t('This week'), align: 'num', render: (r) => usd(r.estimatedCost), value: (r) => r.estimatedCost },
      { key: 'freeWeek', label: t('Free credit this week'), align: 'num', render: (r) => creditWeekCell(r.credit), value: (r) => (r.credit ? r.credit.freeUsedWeek : null) },
      { key: 'paidMonth', label: t('Paid this month'), align: 'num', render: (r) => paidMonthCell(r.credit), value: (r) => (r.credit ? r.credit.paidMonth : null) },
      { key: 'overCapReasons', label: t('Why'), sortable: false, render: (r) => reasonBadges(r.overCapReasons), csv: (r) => (r.overCapReasons || []).join('|') },
    ] : [
      { key: 'email', label: t('User'), render: (r) => userCell(r.email, r.exception ? t('Individual cap') : null) },
      { key: 'tokens', label: t('Tokens'), align: 'num', render: (r) => capValue(r.tokens, r.caps && r.caps.tokenCap, fmt.compact), value: (r) => r.tokens },
      { key: 'requests', label: t('Requests'), align: 'num', render: (r) => capValue(r.requests, r.caps && r.caps.requestCap, fmt.int), value: (r) => r.requests },
      { key: 'estimatedCost', label: t('Estimated cost'), align: 'num', render: (r) => capValue(r.estimatedCost, r.caps && r.caps.costCap, (v) => money(v, ccy)), value: (r) => r.estimatedCost },
      { key: 'capUsage', label: t('Cap usage'), align: 'num', render: (r) => capUsageCell(r.capUsage), value: (r) => r.capUsage },
      { key: 'overCapReasons', label: t('Cap reached'), sortable: false, render: (r) => reasonBadges(r.overCapReasons), csv: (r) => (r.overCapReasons || []).join('|') },
    ];
    if (manual) {
      columns.push({
        key: 'actions', label: '', actions: true,
        render: (r) => h('div', { class: 'row nowrap' },
          button({ label: t('Suspend'), icon: 'lock', size: 'sm', variant: 'danger', onClick: () => { if (requireAdmin(t('Suspend'), 'resourcemanager.projects.setIamPolicy')) decide(ctx, [r], 'suspend', backendOf(s), policy); } }),
          button({ label: allowLabel, size: 'sm', onClick: () => { if (requireAdmin(allowLabel, 'resourcemanager.projects.setIamPolicy')) decide(ctx, [r], 'allow', backendOf(s), policy); } })),
      });
    }
    content = dataTable({
      columns, rows, pageSize: 8, searchable: rows.length > 8, initialSort: { key: 'capUsage', dir: 'desc' },
      exportName: manual ? 'antigravity-review-queue' : 'antigravity-planned-suspensions',
      onRowClick: (r) => r.uid && openUserDrawer(r.uid, { email: r.email, onChanged: () => ctx.rerender() }),
    });
  }
  const bulk = manual && rows && rows.length > 1 ? h('div', { class: 'row' },
    button({ label: t('Suspend all ({n})', { n: rows.length }), icon: 'lock', size: 'sm', variant: 'danger', onClick: () => { if (requireAdmin(t('Suspend'), 'resourcemanager.projects.setIamPolicy')) decide(ctx, rows, 'suspend', backendOf(s), policy); } }),
    button({ label: allowEnd ? t('Allow all until {time}', { time: fmt.dateTimeDay(allowEnd, tz) }) : t('Allow all'), size: 'sm', onClick: () => { if (requireAdmin(allowLabel, 'resourcemanager.projects.setIamPolicy')) decide(ctx, rows, 'allow', backendOf(s), policy); } })) : null;
  let title = t('Review queue');
  if (!manual) title = rt.configured ? t('Pending auto-suspension') : t('Suspended at the next run');
  let subtitle;
  if (isCredit(policy)) {
    subtitle = t('Paid budget spent and nothing free right now. A suspension ends automatically at the weekly reset or on the 1st of the month.');
  } else if (manual) {
    subtitle = policy.nextReset
      ? t('Users over their cap in the cap period {range}. A suspension ends automatically at {time}.', { range: capPeriodText(policy), time: fmt.dateTimeDay(policy.nextReset, tz) })
      : t('Users over their cap in this period. A suspension ends automatically when the settlement period resets.');
  } else if (rt.configured) {
    subtitle = t('Automatic mode (real-time): enforcement runs as soon as a usage log exceeds the cap; IAM changes take a few minutes to take effect.');
  } else {
    subtitle = t('Automatic mode: these users are over their cap and will be suspended by the next enforcement run.');
  }
  return card({ title, subtitle, actions: bulk, flush: true, body: content });
}

async function decide(ctx, rows, decision, backend, policy = {}) {
  const tz = policy.resetTimezone;
  const emails = rows.map((r) => r.email);
  const many = rows.length > 1;
  const suspend = decision === 'suspend';
  const allowEnd = allowEndOf(policy);
  const until = allowEnd ? fmt.dateTimeDay(allowEnd, tz) : null;
  const reset = policy.nextReset ? fmt.dateTimeDay(policy.nextReset, tz) : null;
  let title;
  if (suspend) title = many ? t('Suspend {n} users', { n: rows.length }) : t('Suspend {email}', { email: emails[0] });
  else if (until) title = many ? t('Allow {n} users until {time}', { n: rows.length, time: until }) : t('Allow {email} until {time}', { email: emails[0], time: until });
  else title = many ? t('Allow {n} users for this period', { n: rows.length }) : t('Allow {email} for this period', { email: emails[0] });
  let message = until
    ? t('Antigravity access stays on until {time} ({relative}), even above the cap, and a suspended user is restored now. The allow rule then ends by itself and the default cap applies again. To end it earlier, select Revoke next to the user.', { time: until, relative: fmt.relative(allowEnd) })
    : t('The user keeps Antigravity access until the settlement period ends, even above the cap. A suspended user is restored now.');
  if (suspend) {
    if (reset) {
      message = backend === 'iam_deny'
        ? t('Antigravity access is removed now (deny policy) and restored automatically at {time}.', { time: reset })
        : t('Antigravity access is removed now (cap marker role) and restored automatically at {time}.', { time: reset });
    } else {
      message = backend === 'iam_deny'
        ? t('Antigravity access is removed now (deny policy) and restored automatically when the settlement period resets.')
        : t('Antigravity access is removed now (cap marker role) and restored automatically when the settlement period resets.');
    }
  }
  const ok = await confirmDialog({
    title,
    message,
    details: many ? h('p', { class: 'cell-sub' }, emails.join(', ')) : null,
    confirmLabel: suspend ? t('Suspend') : t('Allow'),
    tone: suspend ? 'danger' : 'primary',
  });
  if (!ok) return;
  try {
    const res = await api.post('/api/v1/governance/antigravity/review', { decision, uids: rows.map((r) => r.uid) });
    const results = res.data.results || [];
    const done = results.filter((r) => r.status === 'applied').length;
    // queued: recorded; the enforcement job (or the next run) applies it.
    const queued = results.filter((r) => r.status === 'queued');
    const other = results.filter((r) => r.status !== 'applied' && r.status !== 'queued');
    let heading;
    if (suspend) heading = reset ? t('{n} users suspended until {time}', { n: done, time: reset }) : t('{n} users suspended until the period resets', { n: done });
    else heading = until ? t('{n} users allowed until {time}', { n: done, time: until }) : t('{n} users allowed for this period', { n: done });
    if (queued.length && !done) heading = t('{n} decisions recorded; the enforcement job applies them', { n: queued.length });
    const notes = queued.length ? [serverText(queued[0].message)] : [];
    toast({
      title: heading,
      text: notes.concat(other.map((r) => `${r.email || r.uid}: ${serverText(r.message)}`)).join(' ') || null,
      tone: other.length ? 'warning' : 'success', timeout: other.length || queued.length ? 9000 : 4800,
    });
    if (queued.length) {
      [30000, 90000, 150000].forEach((ms) => {
        setTimeout(() => {
          if (window.location.hash.startsWith('#/antigravity') && !document.querySelector('dialog[open]')) ctx.rerender();
        }, ms);
      });
    }
    ctx.rerender();
  } catch (err) {
    toast({ title: t('Action refused'), text: errorText(err), tone: 'danger', timeout: 9000 });
  }
}

/* ------------------------------------------------------------------ suspended users */

function suspendedCard(ctx, s) {
  const policy = s.policy || {};
  const tz = policy.resetTimezone;
  const ccy = ccyOf(policy);
  const rows = Array.isArray(s.users) ? s.users.filter((u) => u.state === SUSPENDED) : null;
  let content;
  if (rows === null) {
    content = emptyState({ icon: 'alert-triangle', tone: 'warning', compact: true, title: t('Cannot be determined'), body: t('Usage and IAM data could not be read, so the user population is unknown.') });
  } else if (!rows.length) {
    content = emptyState({ icon: 'user-check', compact: true, title: t('No suspended users'), body: t('Users suspended for exceeding a cap appear here with the time their access returns.') });
  } else {
    const columns = [
      { key: 'email', label: t('User'), render: (u) => userCell(u.email) },
      { key: 'since', label: t('Suspended since'), render: (u) => (u.suspension && u.suspension.since ? fmt.dateTime(u.suspension.since) : h('span', { class: 'text-muted', title: t('Suspended before suspension records were kept') }, DASH)), value: (u) => (u.suspension ? u.suspension.since : null) },
      {
        key: 'restoreAt', label: t('Access returns'),
        render: (u) => {
          if (u.plannedAction === 'restore') return badge(t('At the next run'), 'success', { size: 'sm' });
          if (u.restoreAt) return h('div', { class: 'cell-stack' }, h('span', { class: 'fw-600' }, fmt.dateTimeDay(u.restoreAt, tz)), h('span', { class: 'cell-sub' }, fmt.relative(u.restoreAt)));
          return h('span', { class: 'cell-sub' }, t('When usage falls below the cap'));
        },
        value: (u) => u.restoreAt,
      },
      { key: 'reasons', label: t('Cap reached'), sortable: false, render: (u) => reasonBadges((u.suspension && u.suspension.reasons && u.suspension.reasons.length) ? u.suspension.reasons : u.overCapReasons) },
      { key: 'trigger', label: t('Suspended by'), render: (u) => (u.suspension ? h('div', { class: 'cell-stack' }, h('span', null, t(TRIGGER_LABELS[u.suspension.trigger] || u.suspension.trigger || DASH)), u.suspension.by ? h('span', { class: 'cell-sub truncate' }, u.suspension.by) : null) : h('span', { class: 'text-muted' }, DASH)), value: (u) => (u.suspension ? u.suspension.trigger : '') },
      { key: 'usage', label: t('Usage now'), align: 'num', render: (u) => h('div', { class: 'cell-stack' }, h('span', { class: 'fw-600' }, isNum(u.tokens) ? fmt.compact(u.tokens) : DASH), isNum(u.estimatedCost) ? h('span', { class: 'cell-sub' }, money(u.estimatedCost, ccy)) : null), value: (u) => u.tokens },
      { key: 'actions', label: '', actions: true, render: (u) => button({ label: t('Restore now'), icon: 'unlock', size: 'sm', onClick: () => { if (requireAdmin(t('Restore now'), 'resourcemanager.projects.setIamPolicy')) decide(ctx, [u], 'allow', backendOf(s), policy); } }) },
    ];
    content = dataTable({ columns, rows, pageSize: 8, searchable: rows.length > 8, exportName: 'antigravity-suspended', initialSort: { key: 'restoreAt', dir: 'asc' }, onRowClick: (u) => u.uid && openUserDrawer(u.uid, { email: u.email, onChanged: () => ctx.rerender() }) });
  }
  return card({ title: t('Suspended users'), subtitle: resetText(policy), flush: true, body: content });
}

/* ------------------------------------------------------------------ exception rules */

function exceptionsCard(ctx, s) {
  const policy = s.policy || {};
  const tz = policy.resetTimezone;
  const ccy = ccyOf(policy);
  const rows = s.exceptions || [];
  const add = button({ label: t('Add rule'), icon: 'plus', size: 'sm', onClick: () => { if (requireAdmin(t('Add rule'), 'resourcemanager.projects.setIamPolicy')) editException(ctx, s, null); } });
  let content;
  if (!rows.length) {
    content = emptyState({ icon: 'shield', compact: true, title: t('No exception rules'), body: t('Add a rule to always allow a user, always block a user, or give a user an individual cap. Rules apply in both enforcement modes.') });
  } else {
    const added = (r) => [r.createdAt ? t('Added {time}', { time: fmt.dateTime(r.createdAt) }) : null, r.createdBy].filter(Boolean).join(' \u00b7 ');
    const columns = [
      { key: 'email', label: t('User'), render: (r) => userCell(r.email, null, { compact: true }) },
      {
        key: 'type', label: t('Rule'),
        render: (r) => (r.type === 'limit'
          ? h('div', { class: 'cell-stack' }, ruleTypeBadge(r), h('span', { class: 'cell-sub' }, ruleDetail(r, ccy)))
          : ruleTypeBadge(r)),
        value: (r) => r.type || '',
        csv: (r) => (r.type === 'limit' ? `${capRuleText(r)} (${ruleDetail(r, ccy)})` : capRuleText(r)),
      },
      {
        key: 'until', label: t('Expires'),
        render: (r) => (r.until
          ? h('div', { class: 'cell-stack' }, h('span', { class: 'fw-600' }, fmt.dateTimeDay(r.until, tz)), h('span', { class: 'cell-sub' }, fmt.relative(r.until)))
          : h('span', { class: 'cell-sub' }, r.type === 'block' ? t('Until removed') : t('No expiry'))),
        // Soonest end first; rules without an end last.
        value: (r) => r.until || '9999',
        csv: (r) => r.until || '',
      },
      {
        key: 'note', label: t('Note'), sortable: false, wrap: true,
        render: (r) => h('div', { class: 'cell-stack' },
          h('span', null, r.note ? serverText(r.note) : DASH),
          added(r) ? h('span', { class: 'cell-sub' }, added(r)) : null),
        csv: (r) => [r.note || '', added(r)].filter(Boolean).join(' \u00b7 '),
      },
      { key: 'state', label: t('Access state'), render: (r) => statusBadge(r.state, { size: 'sm' }) },
      {
        key: 'actions', label: '', actions: true,
        render: (r) => h('div', { class: 'row nowrap' },
          button({ icon: 'edit', size: 'sm', title: t('Edit rule'), onClick: () => { if (requireAdmin(t('Edit rule'), 'resourcemanager.projects.setIamPolicy')) editException(ctx, s, r); } }),
          button({ label: t('Revoke'), size: 'sm', onClick: () => removeException(ctx, s, r) })),
      },
    ];
    content = dataTable({
      columns, rows, pageSize: 8, searchable: rows.length > 8, exportName: 'antigravity-exception-rules', initialSort: { key: 'until', dir: 'asc' },
      onRowClick: (r) => r.uid && openUserDrawer(r.uid, { email: r.email, onChanged: () => ctx.rerender() }),
    });
  }
  return card({
    title: t('Exception rules'),
    subtitle: t('Per-user rules that take precedence over the default cap policy. A rule with an expiry ends by itself at that time; Revoke ends it now.'),
    actions: add, flush: true, body: content,
  });
}

function numberOrNull(input, integer) {
  const raw = input.value.trim();
  if (raw === '') return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || n < 0 || (integer && !Number.isInteger(n))) return NaN;
  return n;
}

async function editException(ctx, s, existing) {
  const policy = s.policy || {};
  const ccy = ccyOf(policy);
  const state = { type: existing ? existing.type : 'allow', scope: existing && existing.scope === 'period' ? 'period' : 'permanent' };
  const listId = `agy-users-${Date.now()}`;
  const emailIn = textInput({ value: existing ? existing.email : '', placeholder: 'name@example.com', attrs: { list: listId, autocomplete: 'off', disabled: existing ? true : null, 'aria-label': t('User email') } });
  const datalist = h('datalist', { id: listId }, (s.users || []).map((u) => h('option', { value: u.email })));
  const tokenIn = textInput({ value: existing && isNum(existing.tokenCap) ? String(existing.tokenCap) : '', type: 'number', placeholder: t('Default'), attrs: { min: 0, step: 1000 } });
  const reqIn = textInput({ value: existing && isNum(existing.requestCap) ? String(existing.requestCap) : '', type: 'number', placeholder: t('Default'), attrs: { min: 0, step: 10 } });
  const credit = isCredit(policy);
  const ownCost = existing && isNum(existing.costCap) ? existing.costCap : null;
  const costPick = amountPicker({ value: ownCost || 0, currency: ccy, pricing: policy.pricing, allowDefault: true, isDefault: ownCost === null, defaultLabel: defaultCostLabel(policy, ccy) });
  const budgetIn = textInput({ value: existing && isNum(existing.paidBudget) ? String(existing.paidBudget) : '', type: 'number', placeholder: t('Default ({v})', { v: budgetText(policy.paidBudgetMonth) }), attrs: { min: 0, step: 10 } });
  const creditIn = textInput({ value: existing && isNum(existing.creditMonth) ? String(existing.creditMonth) : '', type: 'number', placeholder: t('Default ({v})', { v: usd(policy.creditPerUserMonth) }), attrs: { min: 0, step: 1 } });
  const noteIn = textInput({ value: existing ? existing.note || '' : '', placeholder: t('Reason (optional)'), attrs: { maxlength: 200 } });
  const capsRow = h('div', { class: 'stack' },
    credit
      ? h('div', { class: 'field-row' },
        field({ label: t('Paid budget per month (USD)'), help: t('Empty keeps the default. 0 = credits only.'), control: budgetIn }),
        field({ label: t('Credit per month (USD)'), help: t('Empty keeps the default, for example 15 for a Plus license.'), control: creditIn }))
      : field({ label: t('Cost cap ({ccy})', { ccy }), help: t('Use default keeps the cost cap of the cap policy.'), control: costPick }),
    h('div', { class: 'field-row' },
      field({ label: credit ? t('Token cap per week') : t('Token cap'), control: tokenIn }),
      field({ label: credit ? t('Request cap per week') : t('Request cap'), control: reqIn })));
  const periodEnd = allowEndOf(policy);
  const scopeField = field({
    label: t('Duration'),
    control: selectInput([
      { value: 'permanent', label: t('No expiry') },
      { value: 'period', label: periodEnd ? t('Until the cap period ends ({time})', { time: fmt.dateTimeDay(periodEnd, policy.resetTimezone) }) : t('This settlement period only') },
    ], state.scope, (v) => { state.scope = v; }),
  });
  const hint = h('p', { class: 'muted' });
  const err = h('div', { class: 'field-error' });
  const sync = () => {
    capsRow.hidden = state.type !== 'limit';
    scopeField.hidden = state.type === 'block';
    hint.textContent = t(RULE_HINTS[state.type]);
  };
  const typeSel = selectInput([
    { value: 'allow', label: t('Always allow (never suspended)') },
    { value: 'limit', label: t('Individual cap') },
    { value: 'block', label: t('Always block') },
  ], state.type, (v) => { state.type = v; sync(); });
  const save = button({ label: t('Save rule'), variant: 'primary' });
  const m = openModal({
    title: existing ? t('Edit exception rule') : t('Add exception rule'),
    subtitle: t('Rules take precedence over the default cap policy in both enforcement modes.'),
    body: (el) => append(el, [
      field({ label: t('User email'), control: h('div', null, emailIn, datalist) }),
      h('div', { class: 'field-row' }, field({ label: t('Rule'), control: typeSel }), scopeField),
      capsRow,
      field({ label: t('Note'), control: noteIn }),
      hint, err]),
    footer: (el) => append(el, [button({ label: t('Cancel'), onClick: () => m.close() }), save]),
  });
  sync();
  save.addEventListener('click', async () => {
    err.textContent = '';
    const email = emailIn.value.trim().toLowerCase();
    if (!EMAIL_RE.test(email)) { err.textContent = t('Enter a valid email address.'); return; }
    const payload = { email, type: state.type, scope: state.type === 'block' ? 'permanent' : state.scope, note: noteIn.value.trim() };
    if (state.type === 'limit') {
      const caps = credit
        ? { tokenCap: numberOrNull(tokenIn, true), requestCap: numberOrNull(reqIn, true), paidBudget: numberOrNull(budgetIn, false), creditMonth: numberOrNull(creditIn, false) }
        : { tokenCap: numberOrNull(tokenIn, true), requestCap: numberOrNull(reqIn, true), costCap: costPick.valid() ? costPick.read() : NaN };
      if (Object.values(caps).some((v) => Number.isNaN(v))) { err.textContent = t('Caps must be numbers of zero or more (token and request caps are whole numbers).'); return; }
      if (Object.values(caps).every((v) => v === null)) { err.textContent = t('Set at least one individual cap.'); return; }
      Object.assign(payload, caps);
    }
    if (state.type === 'block') {
      const ok = await confirmDialog({ title: t('Always block {email}', { email }), message: t(RULE_HINTS.block), confirmLabel: t('Block'), tone: 'danger' });
      if (!ok) return;
    }
    setBusy(save, true, t('Saving'));
    try {
      const res = await api.post('/api/v1/governance/antigravity/exceptions', payload);
      const ch = res.data.change;
      const run = res.data.run;
      const runNote = enforcementRunNote(run);
      const text = ch
        ? `${email}: ${t(IAM_STATE_LABELS[ch.previousState] || ch.previousState)} \u2192 ${t(IAM_STATE_LABELS[ch.newState] || ch.newState)}`
        : runNote;
      toast({
        title: t('Exception rule saved'),
        text,
        tone: run && run.error ? 'warning' : 'success',
        timeout: text ? 9000 : 4800,
      });
      scheduleJobRefresh(ctx, run);
      m.close();
      ctx.rerender();
    } catch (e) {
      setBusy(save, false);
      err.textContent = errorText(e);
    }
  });
}

/** Removes one exception rule after a confirmation that says what happens next (see removeExceptionRule). */
async function removeException(ctx, s, r) {
  const user = (s.users || []).find((u) => u.email === r.email) || null;
  const removed = await removeExceptionRule({ email: r.email, rule: r, user, period: s.policy || {} });
  if (removed) ctx.rerender();
}

/* ------------------------------------------------------------------ prerequisites and policy */

function formatLocationList(entries) {
  const byProject = new Map();
  (entries || []).forEach((e) => {
    if (!e || !e.project || !e.location) return;
    if (!byProject.has(e.project)) byProject.set(e.project, []);
    byProject.get(e.project).push(e.location);
  });
  return Array.from(byProject.entries()).map(([proj, locs]) => `${proj} (${locs.join(', ')})`).join(', ');
}

function prerequisites(ctx, p) {
  const items = [];
  (p.markerRoles || []).forEach((r) => items.push([r.exists, t('Marker role {role}', { role: r.role }), r.exists ? t('Present in project') : t('Missing')]));
  Object.entries(p.permissions || {}).forEach(([perm, ok]) => items.push([ok, perm, ok ? t('Granted') : t('Not granted to the control plane identity')]));
  const ml = p.metadataLogging || {};
  if (ml.checked) {
    const missing = ml.missingLocations || [];
    const enabled = ml.enabledLocations || [];
    if (missing.length) {
      const parts = [t('Off in {missing}', { missing: formatLocationList(missing) })];
      if (enabled.length) parts.push(t('On in {enabled}', { enabled: formatLocationList(enabled) }));
      items.push([false, t('Metadata logging (by location)'), parts.join(' \u00b7 '),
        t('Metadata logging is configured per location (global, us, eu) in Gemini Enterprise > Settings > AI developer tools > Compliance.')]);
    } else if (enabled.length) {
      items.push([true, t('Metadata logging (by location)'), t('On in {enabled}', { enabled: formatLocationList(enabled) })]);
    } else {
      items.push([false, t('Metadata logging (by location)'), t('AI developer tools not enabled in any scanned project')]);
    }
  }
  items.push([p.telemetryDetected, t('Antigravity inference telemetry'), p.telemetryDetected ? t('Detected') : t('Not detected'),
    p.telemetryDetected ? null : t('Counted only when Antigravity for organizations writes businessaicode.googleapis.com/inference_response logs in a scanned project. Antigravity used with a personal account writes no such log.')]);
  const missingRoles = (p.markerRoles || []).some((r) => !r.exists);
  const btn = missingRoles ? button({ label: t('Create marker roles'), icon: 'key', size: 'sm' }) : null;
  if (btn) {
    btn.addEventListener('click', async () => {
      if (!requireAdmin(t('Create marker roles'), 'iam.roles.create')) return;
      const ok = await confirmDialog({
        title: t('Create marker roles'),
        message: t('Custom roles will be created in the project with the permissions of the access role minus businessaicode.* permissions.'),
        confirmLabel: t('Create roles'),
      });
      if (!ok) return;
      setBusy(btn, true, t('Creating'));
      try {
        const res = await api.post('/api/v1/governance/antigravity/setup', {});
        const failed = (res.data.results || []).filter((r) => r.status === 'failed');
        toast({ title: failed.length ? t('Some roles could not be created') : t('Marker roles ready'), text: (res.data.results || []).map((r) => `${r.role}: ${serverText(r.message) || r.status}`).join(' '), tone: failed.length ? 'warning' : 'success', timeout: 8000 });
        ctx.rerender();
      } catch (err) {
        setBusy(btn, false);
        toast({ title: t('Setup failed'), text: errorText(err), tone: 'danger' });
      }
    });
  }
  return h('div', { class: 'stack' },
    h('div', { class: 'checklist' }, items.map(([ok, label, sub, hint]) => h('div', { class: 'check-item' },
      h('span', { class: ['ci-icon', ok ? 'ok' : 'no'] }, icon(ok ? 'check-circle' : 'x-circle', { size: 15 })),
      h('div', { class: 'ci-text' }, h('div', { class: 'cell-mono' }, label), h('div', { class: 'ci-sub' }, sub),
        hint ? h('div', { class: 'ci-sub' }, hint) : null)))),
    btn);
}

/** Values the browser knows for an Intl key ('currency', 'timeZone'); [] in browsers without Intl.supportedValuesOf. */
function intlValues(key) {
  try {
    return typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf(key) : [];
  } catch (e) {
    return [];
  }
}

/** USD -> ccy from the daily official exchange-rate sync (or the rate of the price table in use), else null. */
function usdRate(ccy, pricing) {
  if (!ccy) return null;
  if (ccy === 'USD') return 1;
  if (pricing && pricing.fx && pricing.fx.to === ccy && isNum(pricing.fx.rate)) return pricing.fx.rate;
  const rates = (store.boot && store.boot.pricingAlerts && store.boot.pricingAlerts.fx && store.boot.pricingAlerts.fx.rates) || {};
  return rates[ccy] && isNum(rates[ccy].rate) ? rates[ccy].rate : null;
}

/** Every ISO 4217 code: the saved one and the currencies with a synced exchange rate first, then all others. */
function currencyCodes(saved) {
  const rates = (store.boot && store.boot.pricingAlerts && store.boot.pricingAlerts.fx && store.boot.pricingAlerts.fx.rates) || {};
  return [...new Set([saved, ...Object.keys(rates), 'USD', ...intlValues('currency')].filter(Boolean))];
}

/**
 * One-click amounts: 1x, 2x and 3x the power of ten closest to USD 1,000 in the cap currency, using the synced
 * exchange rate (KRW at about 1,400 per USD gives 1,000,000 / 2,000,000 / 3,000,000). No rate, no presets.
 */
const PRESET_BASE_USD = 1000;

function costPresets(ccy, pricing) {
  const rate = usdRate(ccy, pricing);
  if (!isNum(rate) || rate <= 0) return [];
  const unit = 10 ** Math.round(Math.log10(PRESET_BASE_USD * rate));
  return [1, 2, 3].map((k) => k * unit);
}

/**
 * Amount input with one-click presets (for example 1,000,000 / 2,000,000 / 3,000,000 KRW).
 * read() returns null for "use default", 0 for no cost cap, else the amount.
 */
function amountPicker({ value, currency, pricing = null, allowDefault = false, isDefault = false, defaultLabel = '' }) {
  const state = { value: isNum(value) ? value : 0, useDefault: isDefault };
  const input = textInput({ value: state.value ? String(state.value) : '', type: 'number', placeholder: t('Amount'), attrs: { min: 0, step: 'any', 'aria-label': t('Amount') } });
  const chips = h('div', { class: 'chips' });
  const preview = h('span', { class: 'amount-preview' });
  const render = () => {
    const opts = [];
    if (allowDefault) opts.push({ key: 'default', label: t('Use default'), pressed: state.useDefault });
    opts.push({ key: 'off', label: t('No cost cap'), pressed: !state.useDefault && !state.value });
    costPresets(currency, pricing).forEach((amount) => opts.push({ key: amount, label: fmt.currency(amount, currency, { compact: true }), pressed: !state.useDefault && state.value === amount }));
    mount(chips, opts.map((o) => h('button', {
      type: 'button', class: 'chip', 'aria-pressed': o.pressed ? 'true' : 'false',
      onclick: () => {
        if (o.key === 'default') { state.useDefault = true; input.value = ''; }
        else if (o.key === 'off') { state.useDefault = false; state.value = 0; input.value = ''; }
        else { state.useDefault = false; state.value = o.key; input.value = String(o.key); }
        render();
      },
    }, o.label)));
    if (state.useDefault) preview.textContent = defaultLabel || t('Default');
    else if (Number.isFinite(state.value) && state.value > 0) preview.textContent = fmt.currency(state.value, currency);
    else if (Number.isFinite(state.value) && state.value === 0) preview.textContent = t('No cost cap');
    else preview.textContent = '';
  };
  input.addEventListener('input', () => {
    state.useDefault = false;
    state.value = input.value.trim() === '' ? 0 : Number(input.value);
    render();
  });
  render();
  const el = h('div', { class: 'amount-picker' }, chips, h('div', { class: 'amount-row' }, input, preview));
  el.read = () => (state.useDefault ? null : state.value);
  el.valid = () => state.useDefault || (Number.isFinite(state.value) && state.value >= 0);
  return el;
}

function pricingSummary(pricing, ccy) {
  if (!pricing) return DASH;
  if (pricing.source === 'auto') {
    const c = pricing.counts || {};
    const parts = [t('Automatic')];
    if (pricing.configured) parts.push(t('billed prices for {b} models, Google price list for {c}', { b: fmt.int(c.billing || 0), c: fmt.int(c.catalog || 0) }));
    else parts.push(t('no prices found'));
    if (pricing.fx && pricing.fx.rate) parts.push(t('1 USD = {rate} (Google rate, {date})', { rate: fmt.currency(pricing.fx.rate, ccy), date: pricing.fx.asOf ? fmt.date(String(pricing.fx.asOf).slice(0, 10), 'long') : DASH }));
    return parts.join(' \u00b7 ');
  }
  if (!pricing.configured) return t('Entered prices: not set');
  // Per-model prices win; the single default price applies only to models without one (0 = not set).
  const models = (pricing.models || []).length;
  const fallback = (pricing.inputPerMillion || 0) > 0 || (pricing.outputPerMillion || 0) > 0;
  if (!models) return t('Entered prices: {inp} input \u00b7 {out} output per 1M tokens', { inp: money(pricing.inputPerMillion, ccy), out: money(pricing.outputPerMillion, ccy) });
  const parts = [t('Entered prices for {n} models', { n: fmt.int(models) })];
  if (fallback) parts.push(t('other models: {inp} input \u00b7 {out} output per 1M tokens', { inp: money(pricing.inputPerMillion, ccy), out: money(pricing.outputPerMillion, ccy) }));
  return parts.join(' \u00b7 ');
}

function usedPriceTable(pricing, ccy) {
  const rows = (pricing && pricing.used) || [];
  if (!rows.length) return null;
  const sourceBadge = (p) => {
    if (!p) return badge(t('No price'), 'warning', { size: 'sm' });
    const label = { billing: 'Billed price', catalog: 'Google price list', manual: 'Entered price' }[p.source] || p.source;
    return badge(t(label), p.source === 'billing' ? 'success' : 'neutral', { size: 'sm' });
  };
  return dataTable({
    title: t('Prices of models used in this period (per 1M tokens)'), searchable: false, pageSize: 6,
    columns: [
      {
        key: 'model', label: t('Model'),
        render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-mono' }, r.model), h('div', null, sourceBadge(r.price))),
        csv: (r) => r.model,
      },
      {
        // Cached prompt tokens are billed at their own price (second line); without one they count at the input price.
        key: 'input', label: t('Input'), align: 'num',
        render: (r) => (r.price
          ? h('div', { class: 'cell-stack' }, h('span', null, money(r.price.input, ccy)),
            isNum(r.price.cached)
              ? h('span', { class: 'cell-sub' }, t('cached {v}', { v: money(r.price.cached, ccy) }))
              : h('span', { class: 'cell-sub', title: t('No cached-token price: cached tokens count at the input price') }, t('cached = input')))
          : DASH),
        value: (r) => (r.price ? r.price.input : null),
        csv: (r) => (r.price ? r.price.input : ''),
      },
      { key: 'output', label: t('Output'), align: 'num', render: (r) => (r.price ? money(r.price.output, ccy) : DASH), value: (r) => (r.price ? r.price.output : null) },
    ],
    rows,
  });
}

function capModelSelect(value, onChange) {
  return selectInput([
    { value: 'period', label: t('Fixed caps per period (default)') },
    { value: 'credit_budget', label: t('Weekly free credit + monthly paid budget') },
  ], value, onChange, { 'aria-label': t('Caps') });
}

function tzChoices(workspaceTz, current) {
  return [...new Set([workspaceTz, current, 'UTC', ...intlValues('timeZone')].filter(Boolean))].map((z) => ({ value: z, label: z === workspaceTz ? `${z} (${t('workspace')})` : z }));
}

function priceInfoBlock(pricing, ccy) {
  return h('div', { class: 'stack-sm' },
    h('div', { class: pricing.configured ? 'fw-600' : 'fw-600 text-warning' }, pricingSummary(pricing, ccy)),
    h('p', { class: 'muted' }, pricing.source === 'manual'
      ? t('Entered token prices from Settings > Governance policy are used.')
      : t('Prices are found automatically: what your billing account actually paid per token in the last 30 days, and the public Google Cloud price list for models without billed usage. Amounts are converted with the Google exchange rate.')));
}

function todayIsoInZone(tz) {
  try {
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone: tz || 'UTC', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
    const get = (type) => (parts.find((p) => p.type === type) || {}).value || '';
    const iso = `${get('year')}-${get('month')}-${get('day')}`;
    return /^\d{4}-\d{2}-\d{2}$/.test(iso) ? iso : new Date().toISOString().slice(0, 10);
  } catch (e) {
    return new Date().toISOString().slice(0, 10);
  }
}

function monthDayOptions() {
  return Array.from({ length: 31 }, (_, i) => {
    const d = i + 1;
    if (d === 1) return { value: '1', label: t('Day 1 (1st to month end)') };
    if (d >= 29) return { value: String(d), label: t('Day {d} (last day in shorter months)', { d: fmt.int(d) }) };
    return { value: String(d), label: t('Day {d}', { d: fmt.int(d) }) };
  });
}

/** Credit caps: monthly credit (weekly free share for Gemini), a monthly paid budget per person and optionally one for
 * everyone, all in USD; the weekly reset follows the project's 7-day Antigravity credit cycle. */
async function editCreditPolicy(ctx, policy) {
  const workspaceTz = (store.boot && store.boot.workspace && store.boot.workspace.timezone) || 'UTC';
  const state = {
    model: 'credit_budget', tz: policy.resetTimezone || workspaceTz,
    weekday: isNum(policy.resetWeekday) ? policy.resetWeekday : 0, at: policy.resetTime || '00:00',
    budgetMode: !isNum(policy.paidBudgetMonth) ? 'none' : (policy.paidBudgetMonth === 0 ? 'credits' : 'limit'),
  };
  const numIn = (v, step, placeholder) => textInput({ value: isNum(v) ? String(v) : '', type: 'number', placeholder: placeholder || '', attrs: { min: 0, step } });
  const creditIn = numIn(isNum(policy.creditPerUserMonth) ? policy.creditPerUserMonth : 10, 1);
  const weeklyNote = h('span', { class: 'cell-sub' });
  const syncWeekly = () => {
    const v = Number(creditIn.value);
    weeklyNote.textContent = creditIn.value.trim() !== '' && Number.isFinite(v) && v >= 0 ? t('{w} free per person per week (Gemini models only)', { w: usd(v / 4) }) : '';
  };
  creditIn.addEventListener('input', syncWeekly);
  const preset = (label, v) => button({ label, size: 'sm', onClick: () => { creditIn.value = String(v); syncWeekly(); } });
  const budgetIn = numIn(state.budgetMode === 'limit' ? policy.paidBudgetMonth : 100, 10);
  budgetIn.hidden = state.budgetMode !== 'limit';
  const budgetSeg = segmented([
    { value: 'none', label: t('No limit') }, { value: 'credits', label: t('Credits only') }, { value: 'limit', label: t('Monthly limit') },
  ], state.budgetMode, (v) => { state.budgetMode = v; budgetIn.hidden = v !== 'limit'; });
  const orgIn = numIn(policy.orgPaidBudgetMonth, 10, t('No limit'));
  const weekdaySel = selectInput(WEEKDAYS.map((d, i) => ({ value: String(i), label: t(d) })), String(state.weekday), (v) => { state.weekday = Number(v); });
  const timeIn = textInput({ value: state.at, attrs: { placeholder: '00:00', maxlength: 5, inputmode: 'numeric' } });
  const tokenIn = textInput({ value: isNum(policy.tokenCap) ? String(policy.tokenCap) : '0', type: 'number', attrs: { min: 0, step: 1000 } });
  const reqIn = textInput({ value: isNum(policy.requestCap) ? String(policy.requestCap) : '0', type: 'number', attrs: { min: 0, step: 10 } });
  const how = h('ul', { class: 'muted stack-sm' },
    h('li', null, t('Each person can use the weekly free credit for Gemini models. Claude is always paid.')),
    h('li', null, t('Paid usage counts against the monthly paid budget. While budget is left, nobody is stopped.')),
    h('li', null, t('When the budget is spent, the person is stopped once nothing is free: the week\'s free credit is used up, or they pay again.')),
    h('li', null, t('They are restored automatically at the weekly reset (new free credit) and on the 1st of the month (new budget). Nobody is stopped permanently.')));
  const err = h('div', { class: 'field-error' });
  const save = button({ label: t('Save policy'), variant: 'primary' });
  syncWeekly();
  const m = openModal({
    title: t('Edit cap policy'),
    subtitle: t('Amounts are in USD, like the Antigravity credit. Give specific people a different budget from the Paid budget column or with exception rules.'),
    wide: true,
    body: (el) => append(el, [
      field({
        label: t('Caps'),
        control: capModelSelect(state.model, (v) => {
          state.model = v;
          if (v === 'period') {
            m.close();
            // Credit caps report their weekly cycle as `period`; fixed caps start from the saved period (monthly by
            // default), never from the weekly credit reset.
            editPolicy(ctx, {
              ...policy, capModel: 'period', period: policy.periodSetting || 'monthly', window: policy.windowSetting || 'calendar',
            });
          }
        }),
      }),
      h('div', { class: 'field-row' },
        field({ label: t('Credit per person per month (USD)'), help: t('Gemini Enterprise Standard 10, Plus 15. Google applies a quarter of it every 7 days.'), control: h('div', { class: 'stack-sm' }, creditIn, h('div', { class: 'row' }, preset(t('Standard {v}', { v: '$10' }), 10), preset(t('Plus {v}', { v: '$15' }), 15)), weeklyNote) }),
        field({ label: t('Paid budget per person per month (USD)'), help: t('Claude and Gemini beyond the free credit. Credits only = no paid usage.'), control: h('div', { class: 'stack-sm' }, budgetSeg, budgetIn) })),
      h('div', { class: 'field-row' },
        field({ label: t('Paid budget for everyone per month (USD)'), help: t('Optional. When everyone together has spent it, everyone is credits only until the 1st. Empty = no limit.'), control: orgIn }),
        field({ label: t('Reset time zone'), control: selectInput(tzChoices(workspaceTz, state.tz), state.tz, (v) => { state.tz = v; }) })),
      h('div', { class: 'field-row' },
        field({ label: t('Weekly reset day'), help: t('The project\'s 7-day Antigravity credit cycle starts at its first request; use the same day and time.'), control: weekdaySel }),
        field({ label: t('Reset time'), help: t('HH:MM in the reset time zone'), control: timeIn })),
      h('div', { class: 'field-row' },
        field({ label: t('Token cap per user per week'), help: t('Optional. 0 disables it.'), control: tokenIn }),
        field({ label: t('Request cap per user per week'), help: t('Optional. 0 disables it.'), control: reqIn })),
      field({ label: t('How it works'), control: how }),
      field({ label: t('Token prices'), control: priceInfoBlock(policy.pricing || {}, 'USD') }),
      err]),
    footer: (el) => append(el, [button({ label: t('Cancel'), onClick: () => m.close() }), save]),
  });
  save.addEventListener('click', async () => {
    err.textContent = '';
    const amount = (input) => (input.value.trim() === '' ? NaN : Number(input.value));
    const creditVal = amount(creditIn);
    if (!Number.isFinite(creditVal) || creditVal < 0) { err.textContent = t('Enter the credit as a number of zero or more.'); return; }
    let budget = -1;
    if (state.budgetMode === 'credits') budget = 0;
    if (state.budgetMode === 'limit') {
      budget = amount(budgetIn);
      if (!Number.isFinite(budget) || budget < 0) { err.textContent = t('Enter the paid budget as a number of zero or more.'); return; }
    }
    let org = -1;
    if (orgIn.value.trim() !== '') {
      org = amount(orgIn);
      if (!Number.isFinite(org) || org < 0) { err.textContent = t('Enter the paid budget as a number of zero or more.'); return; }
    }
    const tc = Number(tokenIn.value);
    const rc = Number(reqIn.value);
    if (!Number.isInteger(tc) || tc < 0 || !Number.isInteger(rc) || rc < 0) { err.textContent = t('Caps must be whole numbers of zero or more.'); return; }
    const at = String(timeIn.value || '').trim();
    if (!/^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(at)) { err.textContent = t('Enter the reset time as HH:MM, for example 09:00.'); return; }
    const values = {
      cap_model: state.model, credit_per_user_month: creditVal, paid_budget_month: budget, org_paid_budget_month: org,
      reset_timezone: state.tz, reset_weekday: state.weekday, reset_time: at, token_cap: tc, request_cap: rc,
    };
    setBusy(save, true, t('Saving'));
    try {
      const res = await api.post('/api/v1/settings', { section: 'governance', values });
      const run = res && res.data && res.data.enforcement;
      const note = enforcementRunNote(run);
      toast({
        title: t('Cap policy saved'),
        text: note,
        tone: run && run.error ? 'warning' : 'success',
        timeout: note ? 9000 : 4800,
      });
      scheduleJobRefresh(ctx, run);
      m.close();
      ctx.rerender();
    } catch (e) {
      setBusy(save, false);
      err.textContent = errorText(e);
    }
  });
}

/** Default fixed caps per configurable period (daily, weekly, monthly from any day 1-31, or custom N-day cycle). */
async function editPolicy(ctx, policy) {
  if (isCredit(policy)) { editCreditPolicy(ctx, policy); return; }
  const pricing = policy.pricing || {};
  const workspaceTz = (store.boot && store.boot.workspace && store.boot.workspace.timezone) || 'UTC';
  const saved = typeof policy.currencySetting === 'string' ? policy.currencySetting : ccyOf(policy);
  const initialTz = policy.resetTimezone || workspaceTz;
  const savedCycleStart = policy.cycleStart && policy.cycleStart !== '1970-01-01' ? policy.cycleStart : '';
  const state = {
    period: policy.period || 'monthly',
    window: policy.window || 'calendar',
    tz: initialTz,
    currency: saved,
    weekday: isNum(policy.resetWeekday) ? policy.resetWeekday : 0,
    day: isNum(policy.resetDay) && policy.resetDay >= 1 && policy.resetDay <= 31 ? policy.resetDay : 1,
    cycleDays: isNum(policy.cycleDays) && policy.cycleDays >= 1 && policy.cycleDays <= 92 ? policy.cycleDays : 30,
    cycleStart: savedCycleStart || todayIsoInZone(initialTz),
    at: policy.resetTime || '00:00',
    model: 'period',
  };
  const uid = `agy-pol-${Date.now()}`;
  const shownCcy = () => state.currency || (saved ? 'USD' : ccyOf(policy));
  const num = (v, step, min = 0) => textInput({ value: isNum(v) ? String(v) : '0', type: 'number', attrs: { min, step } });
  const tokenIn = num(policy.tokenCap, 1000);
  const reqIn = num(policy.requestCap, 10);
  const costHost = h('div');
  let cost = null;
  const renderCost = (value) => {
    const prev = value !== undefined ? value : (cost ? cost.read() : (policy.costCap || 0));
    cost = amountPicker({ value: prev || 0, currency: shownCcy(), pricing });
    mount(costHost, cost);
  };
  const billingLabel = saved ? t('Billing account currency') : t('Billing account currency ({ccy})', { ccy: ccyOf(policy) });
  const currencyOptions = [{ value: '', label: billingLabel }, ...currencyCodes(saved).map((c) => ({ value: c, label: c }))];
  const currencySel = selectInput(currencyOptions, state.currency, (v) => { state.currency = v; renderCost(); }, { 'aria-label': t('Currency') });

  /* ---- Period chooser (native radio cards: 4 exclusive options) ---- */
  const periodChoices = [
    { value: 'daily', label: t('Daily'), desc: t('Resets every day') },
    { value: 'weekly', label: t('Weekly'), desc: t('Choose reset weekday') },
    { value: 'monthly', label: t('Monthly'), desc: t('Choose start day (e.g., 1st–month end)') },
    { value: 'custom', label: t('Custom cycle'), desc: t('Every N days from a start date') },
  ];
  const periodRadios = new Map();
  let syncPeriodUI = () => {};
  let schedulePreview = () => {};

  const periodFieldset = h('fieldset', { class: 'period-fieldset' },
    h('legend', { class: 'field-label' }, t('Reset period')),
    h('div', { class: 'period-grid' }, periodChoices.map((c) => {
      const id = `${uid}-p-${c.value}`;
      const radio = h('input', {
        id, type: 'radio', name: `${uid}-period`, value: c.value,
        checked: state.period === c.value ? true : null,
      });
      radio.addEventListener('change', () => {
        if (!radio.checked) return;
        state.period = c.value;
        if (state.period === 'custom' && !state.cycleStart) state.cycleStart = todayIsoInZone(state.tz);
        syncPeriodUI();
        schedulePreview();
      });
      periodRadios.set(c.value, radio);
      return h('label', { class: ['wb-pick', 'period-card'], for: id },
        radio,
        h('span', { class: 'wb-pick-text' },
          h('span', { class: 'wb-pick-name' }, c.label),
          h('span', { class: 'wb-pick-what' }, c.desc)));
    })));

  /* ---- Contextual period controls ---- */
  const weekdaySel = selectInput(
    WEEKDAYS.map((d, i) => ({ value: String(i), label: t(d) })),
    String(state.weekday),
    (v) => { state.weekday = Number(v); schedulePreview(); },
    { 'aria-label': t('Reset weekday') });
  const monthDaySel = selectInput(
    monthDayOptions(),
    String(state.day),
    (v) => { state.day = Number(v); schedulePreview(); },
    { 'aria-label': t('Monthly start day') });
  const cycleDaysIn = textInput({
    value: String(state.cycleDays),
    type: 'number',
    attrs: { min: 1, max: 92, step: 1, inputmode: 'numeric', 'aria-label': t('Cycle length (days)') },
    onInput: (v) => {
      const n = Number(v);
      if (Number.isInteger(n) && n >= 1 && n <= 92) {
        state.cycleDays = n;
        syncCycleChips();
        schedulePreview();
      }
    },
  });
  const cycleChipsHost = h('div', { class: 'chips' });
  function syncCycleChips() {
    mount(cycleChipsHost, [7, 14, 30].map((d) => h('button', {
      type: 'button',
      class: 'chip',
      'aria-pressed': state.cycleDays === d ? 'true' : 'false',
      onclick: () => {
        state.cycleDays = d;
        cycleDaysIn.value = String(d);
        syncCycleChips();
        schedulePreview();
      },
    }, t('{n} days', { n: fmt.int(d) }))));
  }
  syncCycleChips();

  const cycleStartIn = textInput({
    value: state.cycleStart,
    type: 'date',
    attrs: { 'aria-label': t('Start date') },
    onInput: (v) => {
      state.cycleStart = String(v || '').trim();
      schedulePreview();
    },
  });
  const timeIn = textInput({
    value: state.at,
    attrs: { placeholder: '00:00', maxlength: 5, inputmode: 'numeric', 'aria-label': t('Reset time') },
    onInput: (v) => {
      state.at = String(v || '').trim();
      schedulePreview();
    },
  });
  const tzSel = selectInput(tzChoices(workspaceTz, state.tz), state.tz, (v) => {
    state.tz = v;
    schedulePreview();
  }, { 'aria-label': t('Reset time zone') });

  const weekdayField = field({ label: t('Reset weekday'), help: t('The day of the week each new cap period begins.'), control: weekdaySel });
  const monthDayField = field({ label: t('Monthly start day'), help: t('If a month has fewer days (e.g., Feb), the period starts on its last day.'), control: monthDaySel });
  const cycleDaysField = field({ label: t('Cycle length (days)'), help: t('From 1 to 92 days.'), control: h('div', { class: 'stack-sm' }, cycleDaysIn, cycleChipsHost) });
  const cycleStartField = field({ label: t('Start date'), help: t('Cycles repeat every N days anchored to this date.'), control: cycleStartIn });
  const timeField = field({ label: t('Reset time'), help: t('HH:MM in the reset time zone'), control: timeIn });
  const tzField = field({ label: t('Reset time zone'), control: tzSel });

  const scheduleControls = h('div', { class: 'period-controls' },
    weekdayField, monthDayField, cycleDaysField, cycleStartField, timeField, tzField);

  /* ---- Advanced window disclosure (Rolling / Legacy 90-day) ---- */
  const windowChoices = [
    { value: 'calendar', label: t('Calendar (fixed reset)'), desc: t('Usage resets cleanly on schedule; over-cap users stay blocked until reset and resume automatically.') },
    { value: 'rolling', label: t('Rolling'), desc: t('Continuous trailing window; usage drops off continuously with no fixed reset time.') },
  ];
  const windowRadios = new Map();
  const rolling90Radio = h('input', {
    id: `${uid}-w-all`, type: 'radio', name: `${uid}-window`, value: 'all',
    checked: state.period === 'all' ? true : null,
  });
  rolling90Radio.addEventListener('change', () => {
    if (!rolling90Radio.checked) return;
    state.period = 'all';
    state.window = 'rolling';
    periodRadios.forEach((r) => { r.checked = false; });
    syncPeriodUI();
    schedulePreview();
  });
  const advancedDetails = h('details', { class: 'period-advanced', open: (state.window === 'rolling' || state.period === 'all') ? true : null },
    h('summary', { class: 'period-advanced-summary' }, t('Advanced window options')),
    h('div', { class: 'period-advanced-body' },
      h('fieldset', { class: 'period-fieldset' },
        h('legend', { class: 'field-label' }, t('Window calculation')),
        h('div', { class: 'period-window-list' },
          ...windowChoices.map((w) => {
            const id = `${uid}-w-${w.value}`;
            const radio = h('input', {
              id, type: 'radio', name: `${uid}-window`, value: w.value,
              checked: state.period !== 'all' && state.window === w.value ? true : null,
            });
            radio.addEventListener('change', () => {
              if (!radio.checked) return;
              if (state.period === 'all') {
                state.period = 'monthly';
                const monthlyRadio = periodRadios.get('monthly');
                if (monthlyRadio) monthlyRadio.checked = true;
              }
              state.window = w.value;
              syncPeriodUI();
              schedulePreview();
            });
            windowRadios.set(w.value, radio);
            return h('label', { class: ['wb-pick', 'period-card'], for: id },
              radio,
              h('span', { class: 'wb-pick-text' },
                h('span', { class: 'wb-pick-name' }, w.label),
                h('span', { class: 'wb-pick-what' }, w.desc)));
          }),
          h('label', { class: ['wb-pick', 'period-card'], for: `${uid}-w-all` },
            rolling90Radio,
            h('span', { class: 'wb-pick-text' },
              h('span', { class: 'wb-pick-name' }, t('Last 90 days (rolling)')),
              h('span', { class: 'wb-pick-what' }, t('Evaluate usage across the trailing 90 days with no fixed reset.'))))))));

  /* ---- Live Period Preview (aria-live="polite", debounced 250ms, no interstitial "Loading...") ---- */
  const previewBox = h('div', { class: 'period-preview', role: 'status', 'aria-live': 'polite' });
  let previewTimer = null;
  let previewAbort = null;

  function previewDraftPolicy() {
    return {
      period: state.period,
      window: state.window,
      resetTimezone: state.tz,
      resetWeekday: state.weekday,
      resetDay: state.day,
      cycleDays: state.cycleDays,
      cycleStart: state.cycleStart,
      resetTime: state.at,
    };
  }

  function renderPreview(data) {
    const draft = previewDraftPolicy();
    const rows = [];
    rows.push(h('div', { class: 'period-preview-head' }, resetText(draft)));
    const list = (data && Array.isArray(data.periods)) ? data.periods : [];
    const zone = (data && data.timezone) || state.tz;
    if (data && data.rolling) {
      if (list[0] && list[0].start) {
        rows.push(h('div', { class: 'period-preview-row' },
          h('span', { class: 'period-preview-label' }, t('Current window')),
          h('span', { class: 'period-preview-val' }, `${fmt.dateTimeDay(list[0].start, zone)} \u2013 ${t('Now')}`)));
      }
      rows.push(h('div', { class: 'period-preview-note' }, t('Rolling window: access resumes once usage in the trailing window drops below the cap.')));
    } else {
      if (list[0] && list[0].start && list[0].end) {
        rows.push(h('div', { class: 'period-preview-row' },
          h('span', { class: 'period-preview-label' }, t('Current period')),
          h('span', { class: 'period-preview-val' }, `${fmt.dateTimeDay(list[0].start, zone)} \u2013 ${fmt.dateTimeDay(list[0].end, zone)}`)));
      }
      if (list[1] && list[1].start && list[1].end) {
        rows.push(h('div', { class: 'period-preview-row' },
          h('span', { class: 'period-preview-label' }, t('Next period')),
          h('span', { class: 'period-preview-val' }, `${fmt.dateTimeDay(list[1].start, zone)} \u2013 ${fmt.dateTimeDay(list[1].end, zone)}`)));
      }
      rows.push(h('div', { class: 'period-preview-note' }, t('When a user reaches the cap, access is suspended until the next reset and restored automatically then.')));
    }
    mount(previewBox, rows);
  }

  async function fetchPreview() {
    if (previewAbort) previewAbort.abort();
    const at = String(state.at || '').trim();
    if (!/^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(at)) return;
    if (state.period === 'custom' && state.window === 'calendar' && state.cycleStart && !/^\d{4}-\d{2}-\d{2}$/.test(state.cycleStart)) return;
    previewAbort = new AbortController();
    const params = {
      period: state.period,
      window: state.window,
      tz: state.tz,
      at,
      weekday: String(state.weekday),
      day: String(state.day),
      cycleDays: String(state.cycleDays),
      cycleStart: state.cycleStart || '',
    };
    try {
      const res = await api.get('/api/v1/governance/antigravity/period-preview', params, { signal: previewAbort.signal });
      if (res && res.data) renderPreview(res.data);
    } catch (e) {
      if (!isAbort(e)) renderPreview(null);
    }
  }

  schedulePreview = () => {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(fetchPreview, 250);
  };

  syncPeriodUI = () => {
    const cal = state.window === 'calendar' && state.period !== 'all';
    weekdayField.hidden = !(cal && state.period === 'weekly');
    monthDayField.hidden = !(cal && state.period === 'monthly');
    cycleDaysField.hidden = state.period !== 'custom';
    cycleStartField.hidden = !(cal && state.period === 'custom');
    timeField.hidden = !cal;
    tzField.hidden = !cal;
    scheduleControls.hidden = !cal && state.period !== 'custom';
  };
  syncPeriodUI();

  // Credit-based cap preset: weekly share of the Antigravity credit pool.
  const creditUsd = weeklyCreditUsd(policy);
  const creditBtn = button({
    label: t('Set from Antigravity credits ({v} per week)', { v: fmt.currency(creditUsd, 'USD', { precise: true }) }), icon: 'dollar', size: 'sm',
    onClick: () => {
      let ccy = shownCcy();
      let amount = creditUsd;
      if (ccy !== 'USD') {
        const fx = pricing.fx;
        if (fx && fx.from === 'USD' && fx.to === ccy && isNum(fx.rate)) {
          amount = Math.round(creditUsd * fx.rate * 100) / 100;
        } else {
          state.currency = 'USD';
          currencySel.querySelector('select').value = 'USD';
          ccy = 'USD';
        }
      }
      state.period = 'weekly';
      state.window = 'calendar';
      const weeklyRadio = periodRadios.get('weekly');
      if (weeklyRadio) weeklyRadio.checked = true;
      const calRadio = windowRadios.get('calendar');
      if (calRadio) calRadio.checked = true;
      rolling90Radio.checked = false;
      syncPeriodUI();
      schedulePreview();
      renderCost(amount);
    },
  });
  const priceInfo = priceInfoBlock(pricing, ccyOf(policy));
  const err = h('div', { class: 'field-error' });
  const save = button({ label: t('Save policy'), variant: 'primary' });
  renderCost();
  fetchPreview();
  const m = openModal({
    title: t('Edit cap policy'),
    subtitle: t('Caps apply to each person within the configured cap period. Give specific people a different amount from the Cost cap column of the usage table or with exception rules.'),
    wide: true,
    body: (el) => append(el, [
      field({
        label: t('Caps'),
        help: t('Choose the credit caps to give everyone the weekly free credit and a monthly paid budget in USD.'),
        control: capModelSelect(state.model, (v) => {
          state.model = v;
          if (v === 'credit_budget') {
            m.close();
            editCreditPolicy(ctx, { ...policy, capModel: 'credit_budget' });
          }
        }),
      }),
      h('div', { class: 'field-row' },
        field({ label: t('Cost cap per person'), help: t('Users above this estimated cost are suspended (automatic mode) or queued for review.'), control: h('div', { class: 'stack-sm' }, costHost, creditBtn) }),
        field({ label: t('Currency'), help: t('Cost caps and estimates are shown in this currency'), control: currencySel })),
      h('div', { class: 'field-row' },
        field({ label: t('Token cap per user'), help: t('Total tokens within the period. 0 disables it.'), control: tokenIn }),
        field({ label: t('Request cap per user'), help: t('Inference requests within the period. 0 disables it.'), control: reqIn })),
      h('div', { class: 'stack-sm' }, periodFieldset, scheduleControls, previewBox, advancedDetails),
      field({ label: t('Token prices'), control: priceInfo }),
      err]),
    footer: (el) => append(el, [button({ label: t('Cancel'), onClick: () => m.close() }), save]),
    onClose: () => {
      clearTimeout(previewTimer);
      if (previewAbort) previewAbort.abort();
    },
  });
  save.addEventListener('click', async () => {
    err.textContent = '';
    const tc = Number(tokenIn.value);
    const rc = Number(reqIn.value);
    if (!Number.isInteger(tc) || tc < 0 || !Number.isInteger(rc) || rc < 0) { err.textContent = t('Caps must be whole numbers of zero or more.'); return; }
    if (!cost.valid()) { err.textContent = t('The cost cap must be a number of zero or more.'); return; }
    const at = String(timeIn.value || '').trim();
    if (!/^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(at)) { err.textContent = t('Enter the reset time as HH:MM, for example 09:00.'); return; }
    const cDays = Number(cycleDaysIn.value);
    if (state.period === 'custom' && (!Number.isInteger(cDays) || cDays < 1 || cDays > 92)) {
      err.textContent = t('Enter a cycle length from 1 to 92 days.');
      return;
    }
    const cStart = String(cycleStartIn.value || '').trim();
    if (state.period === 'custom' && state.window === 'calendar' && cStart && !/^\d{4}-\d{2}-\d{2}$/.test(cStart)) {
      err.textContent = t('Enter a valid start date (YYYY-MM-DD).');
      return;
    }
    const values = {
      token_cap: tc, request_cap: rc, cost_cap: cost.read() || 0, currency: state.currency,
      period: state.period, window: state.window, reset_timezone: state.tz, reset_weekday: state.weekday,
      reset_day: state.day, cycle_days: Number.isInteger(cDays) && cDays >= 1 && cDays <= 92 ? cDays : state.cycleDays,
      cycle_start: state.period === 'custom' ? cStart : (savedCycleStart || ''),
      reset_time: at, cap_model: state.model,
    };
    setBusy(save, true, t('Saving'));
    try {
      const res = await api.post('/api/v1/settings', { section: 'governance', values });
      const run = res && res.data && res.data.enforcement;
      const note = enforcementRunNote(run);
      toast({
        title: t('Cap policy saved'),
        text: note,
        tone: run && run.error ? 'warning' : 'success',
        timeout: note ? 9000 : 4800,
      });
      scheduleJobRefresh(ctx, run);
      m.close();
      ctx.rerender();
    } catch (e) {
      setBusy(save, false);
      err.textContent = errorText(e);
    }
  });
}

/* ------------------------------------------------------------------ per-person cost cap */

function defaultCostLabel(policy, ccy) {
  return t('Default ({v})', { v: policy.costCap ? fmt.currency(policy.costCap, ccy) : t('no cost cap') });
}

async function editPersonBudget(ctx, s, user) {
  const policy = s.policy || {};
  const rule = user.exception;
  const isLimit = rule && rule.type === 'limit';
  const own = isLimit && isNum(rule.paidBudget) ? rule.paidBudget : null;
  const state = { mode: own === null ? 'default' : (own === 0 ? 'credits' : 'limit') };
  const fallback = isNum(policy.paidBudgetMonth) && policy.paidBudgetMonth > 0 ? policy.paidBudgetMonth : 100;
  const amountIn = textInput({ value: String(own || fallback), type: 'number', attrs: { min: 0, step: 10 } });
  amountIn.hidden = state.mode !== 'limit';
  const seg = segmented([
    { value: 'default', label: t('Default ({v})', { v: budgetText(policy.paidBudgetMonth) }) },
    { value: 'credits', label: t('Credits only') },
    { value: 'limit', label: t('Individual limit') },
  ], state.mode, (v) => { state.mode = v; amountIn.hidden = v !== 'limit'; });
  const c = user.credit;
  const usage = c ? t('Paid this month {p} \u00b7 free credit this week {f} of {w}', { p: usd(c.paidMonth), f: usd(c.freeUsedWeek), w: usd(c.creditWeek) }) : null;
  const err = h('div', { class: 'field-error' });
  const save = button({ label: t('Save'), variant: 'primary' });
  const m = openModal({
    title: t('Paid budget for {email}', { email: user.email }),
    subtitle: t('Default for everyone: {v}', { v: budgetText(policy.paidBudgetMonth) }),
    body: (el) => append(el, [
      rule && !isLimit ? banner({ tone: 'info', text: t('This person has an exception rule ({rule}). Saving an individual budget replaces it.', { rule: capRuleText(rule) }) }) : null,
      field({ label: t('Paid budget per month (USD)'), help: usage, control: h('div', { class: 'stack-sm' }, seg, amountIn) }),
      err]),
    footer: (el) => append(el, [button({ label: t('Cancel'), onClick: () => m.close() }), save]),
  });
  save.addEventListener('click', async () => {
    err.textContent = '';
    let amount = null;
    if (state.mode === 'credits') amount = 0;
    if (state.mode === 'limit') {
      amount = amountIn.value.trim() === '' ? NaN : Number(amountIn.value);
      if (!Number.isFinite(amount) || amount < 0) { err.textContent = t('Enter the paid budget as a number of zero or more.'); return; }
    }
    setBusy(save, true, t('Saving'));
    try {
      const keepOther = isLimit && (isNum(rule.tokenCap) || isNum(rule.requestCap) || isNum(rule.creditMonth) || isNum(rule.costCap));
      let res = null;
      if (amount === null && !keepOther) {
        if (isLimit) res = await api.post('/api/v1/governance/antigravity/exceptions/delete', { email: user.email });
      } else {
        res = await api.post('/api/v1/governance/antigravity/exceptions', {
          email: user.email, type: 'limit', scope: isLimit && rule.scope === 'period' ? 'period' : 'permanent',
          tokenCap: isLimit ? rule.tokenCap : null, requestCap: isLimit ? rule.requestCap : null,
          costCap: isLimit ? rule.costCap : null, creditMonth: isLimit ? rule.creditMonth : null, paidBudget: amount,
          note: isLimit && rule.note ? rule.note : t('Individual paid budget'),
        });
      }
      const run = res && res.data && res.data.run;
      const baseText = amount === null ? t('{email} uses the default budget.', { email: user.email }) : t('{email}: {v}', { email: user.email, v: budgetText(amount) });
      const runNote = enforcementRunNote(run);
      toast({
        title: t('Paid budget saved'),
        text: runNote ? `${baseText} ${runNote}` : baseText,
        tone: run && run.error ? 'warning' : 'success',
        timeout: runNote ? 9000 : 4800,
      });
      scheduleJobRefresh(ctx, run);
      m.close();
      ctx.rerender();
    } catch (e) {
      setBusy(save, false);
      err.textContent = errorText(e);
    }
  });
}

async function editPersonCap(ctx, s, user) {
  if (isCredit(s.policy)) { editPersonBudget(ctx, s, user); return; }
  const policy = s.policy || {};
  const ccy = ccyOf(policy);
  const rule = user.exception;
  const isLimit = rule && rule.type === 'limit';
  const ownCost = isLimit && isNum(rule.costCap) ? rule.costCap : null;
  const picker = amountPicker({ value: ownCost || 0, currency: ccy, pricing: policy.pricing, allowDefault: true, isDefault: ownCost === null, defaultLabel: defaultCostLabel(policy, ccy) });
  const err = h('div', { class: 'field-error' });
  const save = button({ label: t('Save'), variant: 'primary' });
  const range = capPeriodText(policy);
  let usage = t('No estimated cost this period');
  if (isNum(user.estimatedCost)) {
    usage = range
      ? t('Estimated cost in the cap period {range}: {v}', { range, v: money(user.estimatedCost, ccy) })
      : t('Estimated cost this period: {v}', { v: money(user.estimatedCost, ccy) });
  }
  const m = openModal({
    title: t('Cost cap for {email}', { email: user.email }),
    subtitle: t('Default for everyone: {v}', { v: policy.costCap ? fmt.currency(policy.costCap, ccy) : t('no cost cap') }),
    body: (el) => append(el, [
      rule && !isLimit ? banner({ tone: 'info', text: t('This person has an exception rule ({rule}). Saving an individual cost cap replaces it.', { rule: capRuleText(rule) }) }) : null,
      field({ label: t('Cost cap for this person'), help: usage, control: picker }),
      err]),
    footer: (el) => append(el, [button({ label: t('Cancel'), onClick: () => m.close() }), save]),
  });
  save.addEventListener('click', async () => {
    err.textContent = '';
    if (!picker.valid()) { err.textContent = t('The cost cap must be a number of zero or more.'); return; }
    const amount = picker.read();
    setBusy(save, true, t('Saving'));
    try {
      const keepOther = isLimit && (isNum(rule.tokenCap) || isNum(rule.requestCap));
      let res = null;
      if (amount === null && !keepOther) {
        if (isLimit) res = await api.post('/api/v1/governance/antigravity/exceptions/delete', { email: user.email });
      } else {
        res = await api.post('/api/v1/governance/antigravity/exceptions', {
          email: user.email, type: 'limit', scope: isLimit && rule.scope === 'period' ? 'period' : 'permanent',
          tokenCap: isLimit ? rule.tokenCap : null, requestCap: isLimit ? rule.requestCap : null, costCap: amount,
          note: isLimit && rule.note ? rule.note : t('Individual cost cap'),
        });
      }
      const run = res && res.data && res.data.run;
      const baseText = amount === null ? t('{email} uses the default cap.', { email: user.email }) : t('{email}: {v}', { email: user.email, v: amount ? fmt.currency(amount, ccy) : t('no cost cap') });
      const runNote = enforcementRunNote(run);
      toast({
        title: t('Cost cap saved'),
        text: runNote ? `${baseText} ${runNote}` : baseText,
        tone: run && run.error ? 'warning' : 'success',
        timeout: runNote ? 9000 : 4800,
      });
      scheduleJobRefresh(ctx, run);
      m.close();
      ctx.rerender();
    } catch (e) {
      setBusy(save, false);
      err.textContent = errorText(e);
    }
  });
}


/* ------------------------------------------------------------------ coding agents in the cloud */

const KIND_LABELS = { read: 'Read', change: 'Change' };

/** Google Cloud API calls that coding agents made with users' credentials. */
function agentActivityCard(ctx) {
  const host = h('div', null, skeleton('table'));
  const c = card({
    title: t('Agent activity in your cloud'),
    subtitle: t('Google Cloud API calls that coding agents made with user credentials \u00b7 {range}', { range: rangeText() }),
    body: host, flush: true,
    source: 'cloudaudit data_access + activity (user agent "agent-name/...")',
  });
  load(host, () => api.get('/api/v1/governance/antigravity/agent-activity', rangeParams(), { signal: ctx.signal }), (res) => {
    const d = res.data || {};
    const k = d.kpis || {};
    const calls = k.calls || {};
    if (calls.value === 0) {
      mount(host, emptyState({ icon: 'terminal', title: t('No agent activity in this period'), body: t('Calls appear here when a coding agent runs Google Cloud commands (gcloud, bq, client libraries) in this project.'), compact: true }));
      return;
    }
    const kindBadge = (kind) => badge(t(KIND_LABELS[kind] || kind || 'Read'), kind === 'change' ? 'warning' : 'neutral', { size: 'sm' });
    mount(host,
      h('div', { class: 'kpi-grid agent-kpis' },
        kpi({ label: t('API calls'), value: fmt.int(calls.value), meta: (d.byAgent || []).length ? h('span', null, (d.byAgent || []).map((a) => `${a.agent} ${fmt.int(a.calls)}`).join(' \u00b7 ')) : null }),
        kpi({ label: t('Users'), value: fmt.int(k.users) }),
        kpi({ label: t('Changes'), value: fmt.int(k.changes), tip: t('Calls recorded in admin activity logs (resources created, updated or deleted).') }),
        kpi({ label: t('Errors'), value: fmt.int(k.errors) })),
      h('div', { class: 'grid agent-tables' },
        h('div', { class: 'span-6' }, dataTable({
          title: t('By user'), searchable: false, pageSize: 6,
          columns: [
            { key: 'email', label: t('User'), render: (r) => userCell(r.email, (r.agents || []).join(', ')) },
            { key: 'calls', label: t('API calls'), align: 'num', format: fmt.int },
            { key: 'changes', label: t('Changes'), align: 'num', format: fmt.int },
            { key: 'lastSeen', label: t('Last call'), render: (r) => (r.lastSeen ? fmt.relative(r.lastSeen) : DASH), value: (r) => r.lastSeen },
          ],
          rows: d.byUser, initialSort: { key: 'calls', dir: 'desc' },
          onRowClick: (r) => r.uid && openUserDrawer(r.uid, { email: r.email }),
        })),
        h('div', { class: 'span-6' }, dataTable({
          title: t('Most called APIs'), searchable: false, pageSize: 6,
          columns: [
            { key: 'method', label: t('API'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: ['cell-main', 'cell-mono', 'truncate'], title: r.method }, r.method), h('span', { class: 'cell-sub' }, r.service)) },
            { key: 'kind', label: t('Type'), render: (r) => kindBadge(r.kind) },
            { key: 'calls', label: t('Calls'), align: 'num', format: fmt.int },
            { key: 'errors', label: t('Errors'), align: 'num', format: fmt.int },
          ],
          rows: d.byApi, initialSort: { key: 'calls', dir: 'desc' },
        }))),
      dataTable({
        title: t('Recent agent calls'), pageSize: 8, exportName: 'agent-activity',
        columns: [
          { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time, true), value: (r) => r.time },
          { key: 'email', label: t('User'), render: (r) => userCell(r.email, r.agent, { compact: true }) },
          { key: 'method', label: t('API'), render: (r) => h('span', { class: ['cell-mono', 'truncate'], title: `${r.service || ''} ${r.method || ''}` }, r.method || DASH) },
          { key: 'kind', label: t('Type'), render: (r) => kindBadge(r.kind) },
          { key: 'code', label: t('Result'), render: (r) => (r.code ? badge(t('Error {code}', { code: r.code }), 'danger', { size: 'sm' }) : badge(t('OK'), 'success', { size: 'sm' })), value: (r) => r.code || 0 },
        ],
        rows: d.recent, initialSort: { key: 'time', dir: 'desc' },
      }));
  }, { skeletonKind: 'table', signal: ctx.signal });
  return c;
}
