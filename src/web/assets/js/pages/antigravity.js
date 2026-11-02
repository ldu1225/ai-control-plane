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
  pageHeader, kpi, card, dataTable, badge, statusBadge, button, banner, segmented, selectInput, progress, userCell, confirmDialog, openModal,
  toast, setBusy, field, textInput, emptyState, warningsNotice, errorState, skeleton, load, kv, listCell, requireAdmin,
} from '../core/components.js';
import { abortReasonText, errorText, serverText } from '../core/messages.js';
import { proportionBar } from '../core/charts.js';
import { IAM_STATE_COLORS, IAM_STATE_LABELS, REMEDIATION, reasonLabel, openUserDrawer, pageSkeleton, rangeText } from './_shared.js';

const GATE_TEXT = {
  G1_POLICY_READABLE: ['Policy readable', 'The project IAM policy can be read. Failure aborts the run.'],
  G2_ANY_DETERMINATE: ['Determinate classification', 'At least one user could be classified. Failure aborts the run.'],
  G3_TELEMETRY_HEALTHY: ['Telemetry healthy', 'Usage telemetry returned records. When degraded, automatic restores are withheld.'],
  G4_CHANGE_BUDGET: ['Change budget', 'Planned changes stay within the circuit-breaker budget. Exceeding it aborts the run.'],
  G5_NON_USER_GRANT_ABSENT: ['No group or domain grants', 'Access is not granted through groups or domains. Affected users require manual action.'],
};

const PERIODS = [['daily', 'Daily'], ['weekly', 'Weekly'], ['monthly', 'Monthly'], ['all', 'Last 90 days']];
const REASON_LABELS = { tokens: 'Tokens', requests: 'Requests', cost: 'Estimated cost' };
const TRIGGER_LABELS = { auto: 'Automatic', review: 'Review approval', bulk: 'Bulk apply', admin: 'Administrator', scheduler: 'Scheduler' };
const RULE_HINTS = {
  allow: 'The user is never suspended for exceeding a cap. A suspended user is restored now.',
  limit: 'The user is evaluated against these caps instead of the default policy. Empty fields keep the default cap.',
  block: 'Antigravity access is removed now (administrator block marker) and stays removed until the rule is deleted.',
};
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const SUSPENDED = 'CAPPED_BY_TOOL';

export default async function render(ctx) {
  const admin = isAdmin();
  let status = null;
  const dryBtn = button({
    label: t('Dry run'),
    icon: 'play',
    onClick: () => {
      if (!requireAdmin(t('Dry run'), 'resourcemanager.projects.getIamPolicy')) return;
      if (status) runEnforce(ctx, status, true, dryBtn);
    },
  });
  const runBtn = button({
    label: t('Run enforcement now'),
    icon: 'zap',
    variant: 'primary',
    onClick: () => {
      if (!requireAdmin(t('Run enforcement now'), 'resourcemanager.projects.setIamPolicy')) return;
      if (status) runEnforce(ctx, status, false, runBtn);
    },
  });
  const header = pageHeader({
    title: t('Antigravity Controls'),
    desc: t('Per-user caps on tokens, requests and estimated cost within a settlement period. Users over their cap are suspended through project IAM until the period resets, automatically or after administrator review, with exception rules for specific users.'),
    actions: [dryBtn, runBtn],
  });
  const body = h('div', { class: 'stack-lg' });
  mount(ctx.root, header, body);
  mount(body, pageSkeleton(6, 2));

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
  renderStatus(ctx, body, status);
  return null;
}

/* ------------------------------------------------------------------ text helpers */

function ccyOf(policy) {
  return (policy && policy.currency) || 'USD';
}

function periodLabel(policy) {
  const label = t((PERIODS.find((p) => p[0] === policy.period) || [null, policy.period || DASH])[1]);
  if (policy.period === 'all') return label;
  return `${label} \u00b7 ${policy.window === 'rolling' ? t('Rolling') : t('Calendar')}`;
}

function resetText(policy) {
  if (policy.period === 'all' || policy.window === 'rolling') return t('Rolling window: usage leaves the window continuously');
  const tz = policy.resetTimezone || 'UTC';
  if (policy.period === 'daily') return t('Resets every day at 00:00 ({tz})', { tz });
  if (policy.period === 'weekly') return t('Resets every Monday at 00:00 ({tz})', { tz });
  return t('Resets on the 1st of every month at 00:00 ({tz})', { tz });
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

function costBasis(pricing) {
  if (!pricing || !pricing.configured) return pricing && pricing.source === 'manual' ? t('Token prices not entered') : t('No token prices found');
  if (pricing.source === 'manual') return t('Estimated with entered prices');
  const c = pricing.counts || {};
  if (c.billing && c.catalog) return t('Estimated with billed prices and the Google price list');
  return c.billing ? t('Estimated with billed prices') : t('Estimated with the Google price list');
}

function reasonBadges(reasons) {
  if (!reasons || !reasons.length) return h('span', { class: 'text-muted' }, DASH);
  return h('div', { class: 'chips' }, reasons.map((r) => badge(t(REASON_LABELS[r] || r), r === 'cost' ? 'warning' : 'danger', { size: 'sm' })));
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

function ruleBadge(rule) {
  if (!rule) return h('span', { class: 'text-muted' }, DASH);
  if (rule.type === 'block') return badge(t('Always block'), 'danger', { size: 'sm' });
  if (rule.type === 'limit') return badge(t('Individual cap'), 'accent', { size: 'sm' });
  return badge(rule.scope === 'period' ? t('Allowed this period') : t('Always allow'), 'success', { size: 'sm' });
}

function ruleDetail(rule, ccy) {
  if (rule.type === 'limit') {
    const parts = [];
    if (isNum(rule.tokenCap)) parts.push(t('Tokens {v}', { v: fmt.compact(rule.tokenCap) }));
    if (isNum(rule.requestCap)) parts.push(t('Requests {v}', { v: fmt.int(rule.requestCap) }));
    if (isNum(rule.costCap)) parts.push(t('Cost {v}', { v: fmt.currency(rule.costCap, ccy) }));
    const caps = parts.join(' \u00b7 ');
    return rule.until ? `${caps} \u00b7 ${t('Until {time}', { time: fmt.dateTime(rule.until) })}` : caps;
  }
  if (rule.type === 'block') return t('Antigravity access removed until the rule is deleted');
  return rule.until ? t('Until {time}', { time: fmt.dateTime(rule.until) }) : t('No expiry');
}

/* ------------------------------------------------------------------ page */

function gateTone(s) {
  return s === 'pass' ? 'pass' : s === 'warn' ? 'warn' : 'fail';
}

function gateList(gates) {
  return h('div', { class: 'gate-list' }, (gates || []).map((g) => {
    const txt = GATE_TEXT[g.id] || [g.id, ''];
    const tone = gateTone(g.status);
    return h('div', { class: ['gate', tone] },
      h('span', { class: 'gate-icon' }, icon(tone === 'pass' ? 'check' : tone === 'warn' ? 'alert-triangle' : 'x', { size: 15 })),
      h('div', { class: 'cell-stack' },
        h('div', { class: 'row' }, h('span', { class: 'gate-id' }, g.id), h('span', { class: 'fw-600' }, t(txt[0]))),
        h('div', { class: 'gate-detail' }, g.detail ? serverText(g.detail) : t(txt[1]))),
      statusBadge(g.status, { size: 'sm' }));
  }));
}

function renderStatus(ctx, body, s) {
  const policy = s.policy || {};
  const pop = s.population || { byState: {} };
  const plan = s.plan || {};
  const totals = s.totals || {};
  const enforcement = s.enforcement || {};
  const mode = policy.enforcementMode || 'manual';
  const ccy = ccyOf(policy);
  const admin = isAdmin();

  if (plan.aborted) {
    body.append(banner({ tone: 'danger', title: t('Enforcement would abort'), text: abortReasonText(plan.abortReason) || t('A safety gate failed. No IAM changes will be applied until the gate passes.') }));
  } else if (plan.withheldRestores) {
    body.append(banner({ tone: 'warning', title: t('Automatic restores withheld'), text: t('Usage telemetry is degraded (gate G3), so users capped earlier will not be restored automatically in this cycle. Suspensions whose settlement period has ended are still restored.') }));
  }
  const usageUnavailable = s.usageSource === 'unavailable';
  const prereq = s.prerequisites || {};
  if (usageUnavailable) {
    body.append(banner({ tone: 'warning', title: t('Antigravity usage could not be read'), text: t('The inference log query failed. Grant the service identity roles/logging.viewer (or read access to the BigQuery sink table) and check the data source notices above.') }));
  } else if (prereq.telemetryDetected === false) {
    body.append(banner({ tone: 'info', title: t('No Antigravity token usage logs in this project'), text: t('Token usage appears here when Antigravity for organizations writes its inference log (businessaicode.googleapis.com/inference_response) to this project. Agents used from a personal setup, such as Jetski, do not write that log. What agents did in this project is shown under Agent activity in your cloud.') }));
  }
  if (enforcement.durable === false) {
    body.append(banner({ tone: 'warning', title: t('Exception rules are not stored durably'), text: t('Settings are kept in memory on this deployment, so exception rules and suspension records are lost when the service restarts. Configure a settings bucket (SETTINGS_BUCKET).') }));
  }
  // Restores happen only inside an enforcement run, so suspended users without a schedule wait for an administrator.
  if (isNum(totals.suspended) && totals.suspended > 0 && (enforcement.scheduler || {}).configured === false) {
    body.append(banner({ tone: 'warning', title: t('No enforcement schedule'), text: t('{n} users are suspended, but no enforcement schedule is configured. Their access returns only when an enforcement run happens after the settlement period resets. Select Run enforcement now after each reset, or set ENFORCEMENT_SCHEDULE and redeploy.', { n: fmt.int(totals.suspended) }) }));
  }

  body.append(enforcementCard(ctx, s));

  const plannedBlocks = (plan.block || []).length;
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Users in period'), value: usageUnavailable ? DASH : fmt.int(pop.total), meta: h('span', null, windowText(policy)) }),
    kpi({ label: t('Over cap'), value: fmt.int(totals.overCap), meta: h('span', null, capsText(policy)) }),
    kpi({
      label: mode === 'manual' ? t('Awaiting review') : t('Suspended at next run'),
      value: fmt.int(mode === 'manual' ? totals.review : plannedBlocks),
      meta: h('span', null, mode === 'manual' ? t('Administrator decision required') : t('Applied by the next enforcement run')),
    }),
    kpi({ label: t('Suspended now'), value: fmt.int(totals.suspended), meta: h('span', null, policy.nextReset ? t('Restored at {time}', { time: fmt.dateTime(policy.nextReset) }) : t('Restored when usage falls below the cap')) }),
    kpi({
      label: t('Estimated cost'), value: isNum(totals.estimatedCost) ? fmt.currency(totals.estimatedCost, ccy) : DASH,
      meta: h('span', null, costBasis(policy.pricing)),
      tip: t('Per-user cost is estimated from each person\'s tokens per model and the price per token: what your billing account actually paid, or the public Google price list. Billing exports do not break cost down by user.'),
    }),
    kpi({ label: t('Enforceable coverage'), value: fmt.pct(pop.enforceableCoverage), meta: isNum(pop.enforceableCoverage) ? progress(pop.enforceableCoverage, pop.enforceableCoverage >= 0.8 ? 'success' : 'warning') : null })));

  body.append(queueCard(ctx, s));
  body.append(suspendedCard(ctx, s));
  body.append(exceptionsCard(ctx, s));

  const row1 = h('div', { class: 'grid' });
  const pricing = policy.pricing || {};
  const policyCard = card({
    title: t('Cap policy'), subtitle: t('Default caps per user and settlement period'),
    actions: button({
      label: t('Edit policy'), icon: 'edit', size: 'sm',
      onClick: () => {
        if (!requireAdmin(t('Edit policy'))) return;
        editPolicy(ctx, policy);
      },
    }),
    body: kv([
      [t('Token cap'), policy.tokenCap ? fmt.int(policy.tokenCap) : t('Disabled')],
      [t('Request cap'), policy.requestCap ? fmt.int(policy.requestCap) : t('Disabled')],
      [t('Cost cap per person'), policy.costCap ? h('span', null, fmt.currency(policy.costCap, ccy), pricing.configured ? null : h('span', { class: 'text-warning' }, ` \u00b7 ${t('no token prices')}`)) : t('Disabled')],
      [t('Token prices'), pricingSummary(pricing, ccy)],
      [t('Settlement period'), periodLabel(policy)],
      [t('Period reset'), resetText(policy)],
      [t('Change budget'), t('{n} changes per cycle', { n: fmt.int(policy.changeBudget) })],
      [t('Usage source'), { bigquery: t('BigQuery log sink'), logging: t('Cloud Logging API'), unavailable: t('Unavailable') }[s.usageSource] || s.usageSource],
    ]),
  });
  const priceTable = usedPriceTable(pricing, ccy);
  if (priceTable) policyCard.bodyEl.append(h('div', { class: 'policy-prices' }, priceTable));
  policyCard.classList.add('span-4');
  const gatesCard = card({ title: t('Safety gates'), subtitle: t('Evaluated before any IAM change'), body: gateList(s.gates) });
  gatesCard.classList.add('span-8');
  row1.append(policyCard, gatesCard);
  body.append(row1);

  const segments = Object.keys(IAM_STATE_LABELS).map((st) => ({ label: t(IAM_STATE_LABELS[st]), value: (pop.byState || {})[st] || 0, color: IAM_STATE_COLORS[st] }));
  body.append(card({
    title: t('Access state population'), subtitle: t('Users with usage in the window or a direct binding on the access or marker roles'),
    body: pop.byState ? h('div', null, proportionBar(segments), h('div', { class: 'state-legend' }, segments.map((sg) => {
      const sw = h('span', { class: 'swatch round' });
      sw.style.setProperty('background', sg.color);
      return h('div', { class: 'sl-row' }, sw, h('span', null, sg.label), h('strong', null, fmt.int(sg.value)));
    }))) : emptyState({ icon: 'alert-triangle', tone: 'warning', compact: true, title: t('Population unknown'), body: t('Usage and IAM data could not be read, so the user population is unknown.') }),
  }));

  body.append(card({
    title: t('Usage against cap'), subtitle: windowText(policy), flush: true, source: 'businessaicode.googleapis.com/inference_response',
    body: dataTable({
      columns: [
        { key: 'email', label: t('User'), render: (u) => userCell(u.email) },
        { key: 'requests', label: t('Requests'), align: 'num', format: fmt.int },
        { key: 'tokens', label: t('Tokens'), align: 'num', render: (u) => (isNum(u.tokens) ? h('div', { class: 'cell-stack' }, h('span', { class: 'fw-600' }, fmt.compact(u.tokens)), h('span', { class: 'cell-sub' }, t('{p} in \u00b7 {c} out', { p: fmt.compact(u.promptTokens), c: fmt.compact(u.candidateTokens) }))) : h('span', { class: 'text-muted' }, DASH)), value: (u) => u.tokens },
        { key: 'estimatedCost', label: t('Estimated cost'), align: 'num', render: (u) => h('span', { title: u.costPartial ? t('Some models have no unit price') : null }, money(u.estimatedCost, ccy), u.costPartial ? ' *' : ''), value: (u) => u.estimatedCost },
        { key: 'models', label: t('Models'), sortable: false, render: (u) => listCell(u.models, { mono: true }), csv: (u) => (u.models || []).join('|') },
        { key: 'capUsage', label: t('Cap usage'), align: 'num', value: (u) => u.capUsage, render: (u) => capUsageCell(u.capUsage) },
        {
          key: 'costCap', label: t('Cost cap'), align: 'num', value: (u) => (u.caps ? u.caps.costCap : null),
          render: (u) => {
            const own = u.exception && u.exception.type === 'limit' && isNum(u.exception.costCap);
            const capValue = u.caps ? u.caps.costCap : null;
            const label = h('div', { class: 'cell-stack' },
              h('span', { class: own ? 'fw-600' : '' }, isNum(capValue) && capValue > 0 ? fmt.currency(capValue, ccy) : t('None')),
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
        { key: 'exception', label: t('Rule'), render: (u) => ruleBadge(u.exception), value: (u) => (u.exception ? u.exception.type : ''), csv: (u) => (u.exception ? u.exception.type : '') },
        { key: 'state', label: t('Access state'), render: (u) => { const b = statusBadge(u.state, { size: 'sm' }); if (u.reason) b.title = reasonLabel(u.reason); else if (u.restoreAt) b.title = t('Restored at {time}', { time: fmt.dateTime(u.restoreAt) }); return b; } },
        { key: 'plannedAction', label: t('Planned action'), render: (u) => statusBadge(u.plannedAction || 'none', { size: 'sm' }) },
      ],
      rows: s.users, pageSize: 15, exportName: 'antigravity-usage', initialSort: { key: 'capUsage', dir: 'desc' },
      emptyText: t('No Antigravity usage was recorded in this window.'),
      unavailableText: t('Usage and IAM data could not be read, so the user population is unknown.'),
      onRowClick: (u) => u.uid && openUserDrawer(u.uid, { onChanged: () => ctx.rerender() }),
    }),
  }));

  const manual = s.manualActions || [];
  let manualBody;
  if (s.manualActions === null || s.manualActions === undefined) {
    manualBody = emptyState({ icon: 'alert-triangle', tone: 'warning', title: t('Cannot be determined'), body: t('Manual actions are computed only when both the IAM policy and the usage telemetry can be read.'), compact: true });
  } else if (manual.length) {
    manualBody = dataTable({
      columns: [
        { key: 'email', label: t('User'), render: (r) => userCell(r.email) },
        { key: 'state', label: t('State'), render: (r) => statusBadge(r.state, { size: 'sm' }) },
        { key: 'reason', label: t('Reason'), render: (r) => badge(reasonLabel(r.reason) || DASH, 'info', { size: 'sm' }) },
        { key: 'remediation', label: t('Remediation'), sortable: false, wrap: true, render: (r) => h('span', { class: 'cell-sub' }, t(REMEDIATION[r.reason] || r.remediation || '')) },
      ],
      rows: manual, pageSize: 8, exportName: 'antigravity-manual-actions', searchable: manual.length > 8,
    });
  } else {
    manualBody = emptyState({ icon: 'check-circle', title: t('No manual actions'), body: t('Every user with usage can be enforced automatically.'), compact: true });
  }
  body.append(card({
    title: t('Manual action required'), subtitle: t('Users whose access cannot be enforced through individual project bindings'), flush: true,
    body: manualBody,
  }));

  body.append(agentActivityCard(ctx));

  const row3 = h('div', { class: 'grid' });
  const auditHost = h('div');
  const auditCard = card({ title: t('Administrative audit log'), subtitle: t('Actions by this control plane and agy-admin'), flush: true, body: auditHost, source: 'logs/agy-admin%2Fadmin_actions' });
  auditCard.classList.add('span-8');
  const prereqCard = card({ title: t('Enforcement prerequisites'), subtitle: t('Marker roles, permissions and telemetry'), body: prerequisites(ctx, s.prerequisites || {}) });
  prereqCard.classList.add('span-4');
  row3.append(auditCard, prereqCard);
  body.append(row3);
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

function enforcementCard(ctx, s) {
  const policy = s.policy || {};
  const enforcement = s.enforcement || {};
  const mode = policy.enforcementMode || 'manual';
  const last = enforcement.lastRun;
  const sched = enforcement.scheduler || {};
  const control = segmented([
    { value: 'manual', label: t('Manual review'), title: t('Over-cap users wait for an administrator decision') },
    { value: 'auto', label: t('Automatic suspension'), title: t('Over-cap users are suspended by every enforcement run') },
  ], mode, (v) => {
    if (!requireAdmin(t('Enforcement mode'))) { ctx.rerender(); return; }
    setMode(ctx, v);
  }, { label: t('Enforcement mode') });
  let schedSub;
  if (sched.lastRun) schedSub = t('Last scheduled run {when}', { when: fmt.relative(sched.lastRun.at) });
  else if (sched.configured) schedSub = t('Waiting for the first scheduled run');
  else schedSub = t('Runs only when an administrator selects Run enforcement now');
  return card({
    title: t('Enforcement'),
    subtitle: mode === 'auto'
      ? t('Automatic: users over their cap are suspended by each enforcement run and restored when the settlement period resets.')
      : t('Manual: users over their cap wait in the review queue. An administrator suspends them until the period resets or allows them for the rest of the period.'),
    actions: control,
    body: h('div', { class: 'enf-grid' },
      enfItem(t('Settlement period'), periodLabel(policy), resetText(policy)),
      enfItem(t('Next reset'), policy.nextReset ? fmt.dateTime(policy.nextReset) : t('No fixed reset'), policy.nextReset ? fmt.relative(policy.nextReset) : t('Suspended users are restored when their usage falls below the cap')),
      enfItem(t('Last run'), last ? fmt.dateTime(last.at) : t('Not run yet'), last ? runSummary(last) : t('Select Run enforcement now or configure the scheduler')),
      enfItem(t('Scheduler'), sched.configured ? t('Configured') : t('Not configured'), schedSub)),
  });
}

async function setMode(ctx, next) {
  const ok = await confirmDialog({
    title: next === 'auto' ? t('Turn on automatic suspension') : t('Switch to manual review'),
    message: next === 'auto'
      ? t('Every enforcement run will suspend users over their cap until the settlement period resets. Exception rules and safety gates still apply.')
      : t('Enforcement runs will no longer suspend anyone. Users over their cap wait in the review queue for an administrator decision. Suspensions still end when the period resets.'),
    confirmLabel: next === 'auto' ? t('Turn on') : t('Switch'),
    tone: next === 'auto' ? 'danger' : 'primary',
  });
  if (!ok) { ctx.rerender(); return; }
  try {
    await api.post('/api/v1/settings', { section: 'governance', values: { enforcement_mode: next } });
    toast({ title: next === 'auto' ? t('Automatic suspension is on') : t('Manual review is on'), tone: 'success' });
  } catch (err) {
    toast({ title: t('Save failed'), text: errorText(err), tone: 'danger' });
  }
  ctx.rerender();
}

async function runEnforce(ctx, s, dry, btn) {
  const mode = (s.policy || {}).enforcementMode || 'manual';
  if (!dry) {
    const ok = await confirmDialog({
      title: t('Run enforcement now'),
      message: mode === 'auto'
        ? t('IAM bindings are changed in one atomic update: users over their cap are suspended until the settlement period resets, suspensions whose period ended are restored and exception rules are applied. Users that are not enforceable are never modified.')
        : t('Manual review mode: nobody is suspended by this run. Suspensions whose period ended are restored and exception rules are applied; users over their cap stay in the review queue.'),
      confirmLabel: t('Run now'),
      tone: mode === 'auto' ? 'danger' : 'primary',
      requireText: mode === 'auto' ? 'APPLY' : null,
    });
    if (!ok) return;
  }
  setBusy(btn, true, dry ? t('Simulating') : t('Applying'));
  try {
    const res = await api.post('/api/v1/governance/antigravity/enforce', dry ? { dryRun: true } : {});
    setBusy(btn, false);
    showResult(ctx, res.data);
  } catch (err) {
    setBusy(btn, false);
    toast({ title: dry ? t('Dry run failed') : t('Enforcement failed'), text: errorText(err), tone: 'danger', timeout: 9000 });
  }
}

function showResult(ctx, r) {
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
      gateList(r.gates),
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

/* ------------------------------------------------------------------ review queue */

function queueCard(ctx, s) {
  const policy = s.policy || {};
  const manual = (policy.enforcementMode || 'manual') === 'manual';
  const ccy = ccyOf(policy);
  const rows = manual ? s.reviewQueue : (Array.isArray(s.users) ? s.users.filter((u) => u.plannedAction === 'block') : null);
  let content;
  if (rows === null || rows === undefined) {
    content = emptyState({ icon: 'alert-triangle', tone: 'warning', compact: true, title: t('Cannot be determined'), body: t('The review queue is computed only when both the IAM policy and the usage telemetry can be read.') });
  } else if (!rows.length) {
    content = emptyState({ icon: 'check-circle', compact: true, title: manual ? t('No users awaiting review') : t('No suspensions planned'), body: t('No enforceable user is over the cap in this period.') });
  } else {
    const columns = [
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
          button({ label: t('Suspend'), icon: 'lock', size: 'sm', variant: 'danger', onClick: () => { if (requireAdmin(t('Suspend'), 'resourcemanager.projects.setIamPolicy')) decide(ctx, [r], 'suspend'); } }),
          button({ label: t('Allow this period'), size: 'sm', onClick: () => { if (requireAdmin(t('Allow this period'), 'resourcemanager.projects.setIamPolicy')) decide(ctx, [r], 'allow'); } })),
      });
    }
    content = dataTable({
      columns, rows, pageSize: 8, searchable: rows.length > 8, initialSort: { key: 'capUsage', dir: 'desc' },
      exportName: manual ? 'antigravity-review-queue' : 'antigravity-planned-suspensions',
      onRowClick: (r) => r.uid && openUserDrawer(r.uid, { onChanged: () => ctx.rerender() }),
    });
  }
  const bulk = manual && rows && rows.length > 1 ? h('div', { class: 'row' },
    button({ label: t('Suspend all ({n})', { n: rows.length }), icon: 'lock', size: 'sm', variant: 'danger', onClick: () => { if (requireAdmin(t('Suspend'), 'resourcemanager.projects.setIamPolicy')) decide(ctx, rows, 'suspend'); } }),
    button({ label: t('Allow all this period'), size: 'sm', onClick: () => { if (requireAdmin(t('Allow this period'), 'resourcemanager.projects.setIamPolicy')) decide(ctx, rows, 'allow'); } })) : null;
  return card({
    title: manual ? t('Review queue') : t('Suspended at the next run'),
    subtitle: manual
      ? t('Users over their cap in this period. A suspension ends automatically when the settlement period resets.')
      : t('Automatic mode: these users are over their cap and will be suspended by the next enforcement run.'),
    actions: bulk, flush: true, body: content,
  });
}

async function decide(ctx, rows, decision) {
  const emails = rows.map((r) => r.email);
  const many = rows.length > 1;
  const suspend = decision === 'suspend';
  let title;
  if (suspend) title = many ? t('Suspend {n} users', { n: rows.length }) : t('Suspend {email}', { email: emails[0] });
  else title = many ? t('Allow {n} users for this period', { n: rows.length }) : t('Allow {email} for this period', { email: emails[0] });
  const ok = await confirmDialog({
    title,
    message: suspend
      ? t('Antigravity access is removed now (cap marker role) and restored automatically when the settlement period resets.')
      : t('The user keeps Antigravity access until the settlement period ends, even above the cap. A suspended user is restored now.'),
    details: many ? h('p', { class: 'cell-sub' }, emails.join(', ')) : null,
    confirmLabel: suspend ? t('Suspend') : t('Allow'),
    tone: suspend ? 'danger' : 'primary',
  });
  if (!ok) return;
  try {
    const res = await api.post('/api/v1/governance/antigravity/review', { decision, uids: rows.map((r) => r.uid) });
    const results = res.data.results || [];
    const done = results.filter((r) => r.status === 'applied').length;
    const other = results.filter((r) => r.status !== 'applied');
    toast({
      title: suspend ? t('{n} users suspended until the period resets', { n: done }) : t('{n} users allowed for this period', { n: done }),
      text: other.map((r) => `${r.email || r.uid}: ${serverText(r.message)}`).join(' ') || null,
      tone: other.length ? 'warning' : 'success', timeout: other.length ? 9000 : 4800,
    });
    ctx.rerender();
  } catch (err) {
    toast({ title: t('Action refused'), text: errorText(err), tone: 'danger', timeout: 9000 });
  }
}

/* ------------------------------------------------------------------ suspended users */

function suspendedCard(ctx, s) {
  const policy = s.policy || {};
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
          if (u.restoreAt) return h('div', { class: 'cell-stack' }, h('span', { class: 'fw-600' }, fmt.dateTime(u.restoreAt)), h('span', { class: 'cell-sub' }, fmt.relative(u.restoreAt)));
          return h('span', { class: 'cell-sub' }, t('When usage falls below the cap'));
        },
        value: (u) => u.restoreAt,
      },
      { key: 'reasons', label: t('Cap reached'), sortable: false, render: (u) => reasonBadges((u.suspension && u.suspension.reasons && u.suspension.reasons.length) ? u.suspension.reasons : u.overCapReasons) },
      { key: 'trigger', label: t('Suspended by'), render: (u) => (u.suspension ? h('div', { class: 'cell-stack' }, h('span', null, t(TRIGGER_LABELS[u.suspension.trigger] || u.suspension.trigger || DASH)), u.suspension.by ? h('span', { class: 'cell-sub truncate' }, u.suspension.by) : null) : h('span', { class: 'text-muted' }, DASH)), value: (u) => (u.suspension ? u.suspension.trigger : '') },
      { key: 'usage', label: t('Usage now'), align: 'num', render: (u) => h('div', { class: 'cell-stack' }, h('span', { class: 'fw-600' }, isNum(u.tokens) ? fmt.compact(u.tokens) : DASH), isNum(u.estimatedCost) ? h('span', { class: 'cell-sub' }, money(u.estimatedCost, ccy)) : null), value: (u) => u.tokens },
      { key: 'actions', label: '', actions: true, render: (u) => button({ label: t('Restore now'), icon: 'unlock', size: 'sm', onClick: () => { if (requireAdmin(t('Restore now'), 'resourcemanager.projects.setIamPolicy')) decide(ctx, [u], 'allow'); } }) },
    ];
    content = dataTable({ columns, rows, pageSize: 8, searchable: rows.length > 8, exportName: 'antigravity-suspended', initialSort: { key: 'restoreAt', dir: 'asc' }, onRowClick: (u) => u.uid && openUserDrawer(u.uid, { onChanged: () => ctx.rerender() }) });
  }
  return card({ title: t('Suspended users'), subtitle: resetText(policy), flush: true, body: content });
}

/* ------------------------------------------------------------------ exception rules */

function exceptionsCard(ctx, s) {
  const policy = s.policy || {};
  const ccy = ccyOf(policy);
  const rows = s.exceptions || [];
  const add = button({ label: t('Add rule'), icon: 'plus', size: 'sm', onClick: () => { if (requireAdmin(t('Add rule'), 'resourcemanager.projects.setIamPolicy')) editException(ctx, s, null); } });
  let content;
  if (!rows.length) {
    content = emptyState({ icon: 'shield', compact: true, title: t('No exception rules'), body: t('Add a rule to always allow a user, always block a user, or give a user an individual cap. Rules apply in both enforcement modes.') });
  } else {
    const columns = [
      { key: 'email', label: t('User'), render: (r) => userCell(r.email) },
      { key: 'type', label: t('Rule'), render: (r) => ruleBadge(r) },
      { key: 'detail', label: t('Detail'), sortable: false, wrap: true, render: (r) => h('span', { class: 'cell-sub' }, ruleDetail(r, ccy)), csv: (r) => ruleDetail(r, ccy) },
      { key: 'note', label: t('Note'), sortable: false, wrap: true, render: (r) => h('span', { class: 'cell-sub' }, r.note || DASH) },
      { key: 'createdAt', label: t('Added'), render: (r) => h('div', { class: 'cell-stack' }, h('span', null, r.createdAt ? fmt.dateTime(r.createdAt) : DASH), r.createdBy ? h('span', { class: 'cell-sub truncate' }, r.createdBy) : null), value: (r) => r.createdAt },
      { key: 'state', label: t('Access state'), render: (r) => statusBadge(r.state, { size: 'sm' }) },
      {
        key: 'actions', label: '', actions: true,
        render: (r) => h('div', { class: 'row nowrap' },
          button({ icon: 'edit', size: 'sm', title: t('Edit rule'), onClick: () => { if (requireAdmin(t('Edit rule'), 'resourcemanager.projects.setIamPolicy')) editException(ctx, s, r); } }),
          button({ icon: 'trash', size: 'sm', title: t('Remove rule'), onClick: () => { if (requireAdmin(t('Remove rule'), 'resourcemanager.projects.setIamPolicy')) removeException(ctx, r); } })),
      },
    ];
    content = dataTable({ columns, rows, pageSize: 8, searchable: rows.length > 8, exportName: 'antigravity-exception-rules', initialSort: { key: 'createdAt', dir: 'desc' } });
  }
  return card({ title: t('Exception rules'), subtitle: t('Per-user rules that take precedence over the default cap policy'), actions: add, flush: true, body: content });
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
  const ownCost = existing && isNum(existing.costCap) ? existing.costCap : null;
  const costPick = amountPicker({ value: ownCost || 0, currency: ccy, allowDefault: true, isDefault: ownCost === null, defaultLabel: defaultCostLabel(policy, ccy) });
  const noteIn = textInput({ value: existing ? existing.note || '' : '', placeholder: t('Reason (optional)'), attrs: { maxlength: 200 } });
  const capsRow = h('div', { class: 'stack' },
    field({ label: t('Cost cap ({ccy})', { ccy }), help: t('Use default keeps the cost cap of the cap policy.'), control: costPick }),
    h('div', { class: 'field-row' },
      field({ label: t('Token cap'), control: tokenIn }),
      field({ label: t('Request cap'), control: reqIn })));
  const scopeField = field({
    label: t('Duration'),
    control: selectInput([{ value: 'permanent', label: t('No expiry') }, { value: 'period', label: t('This settlement period only') }], state.scope, (v) => { state.scope = v; }),
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
      const caps = { tokenCap: numberOrNull(tokenIn, true), requestCap: numberOrNull(reqIn, true), costCap: costPick.valid() ? costPick.read() : NaN };
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
      toast({
        title: t('Exception rule saved'),
        text: ch ? `${email}: ${t(IAM_STATE_LABELS[ch.previousState] || ch.previousState)} \u2192 ${t(IAM_STATE_LABELS[ch.newState] || ch.newState)}` : null,
        tone: 'success',
      });
      m.close();
      ctx.rerender();
    } catch (e) {
      setBusy(save, false);
      err.textContent = errorText(e);
    }
  });
}

async function removeException(ctx, r) {
  const block = r.type === 'block';
  const ok = await confirmDialog({
    title: t('Remove exception rule'),
    message: block
      ? t('The rule is removed and Antigravity access is restored for {email}. The default cap policy applies again.', { email: r.email })
      : t('The rule is removed for {email}. The default cap policy applies again from the next evaluation.', { email: r.email }),
    confirmLabel: t('Remove'),
    tone: block ? 'primary' : 'danger',
  });
  if (!ok) return;
  try {
    const res = await api.post('/api/v1/governance/antigravity/exceptions/delete', { email: r.email });
    const ch = res.data.change;
    toast({ title: t('Exception rule removed'), text: ch ? `${r.email}: ${t(IAM_STATE_LABELS[ch.previousState] || ch.previousState)} \u2192 ${t(IAM_STATE_LABELS[ch.newState] || ch.newState)}` : null, tone: 'success' });
    ctx.rerender();
  } catch (err) {
    toast({ title: t('Action refused'), text: errorText(err), tone: 'danger', timeout: 9000 });
  }
}

/* ------------------------------------------------------------------ prerequisites and policy */

function prerequisites(ctx, p) {
  const items = [];
  (p.markerRoles || []).forEach((r) => items.push([r.exists, t('Marker role {role}', { role: r.role }), r.exists ? t('Present in project') : t('Missing')]));
  Object.entries(p.permissions || {}).forEach(([perm, ok]) => items.push([ok, perm, ok ? t('Granted') : t('Not granted to the control plane identity')]));
  items.push([p.telemetryDetected, t('Antigravity inference telemetry'), p.telemetryDetected ? t('Detected') : t('Not detected')]);
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
    h('div', { class: 'checklist' }, items.map(([ok, label, sub]) => h('div', { class: 'check-item' },
      h('span', { class: ['ci-icon', ok ? 'ok' : 'no'] }, icon(ok ? 'check-circle' : 'x-circle', { size: 15 })),
      h('div', { class: 'ci-text' }, h('div', { class: 'cell-mono' }, label), h('div', { class: 'ci-sub' }, sub))))),
    btn);
}

const CURRENCIES = ['KRW', 'USD', 'JPY', 'EUR', 'GBP', 'SGD', 'AUD', 'CNY', 'INR', 'TWD'];
const COST_PRESETS = { KRW: [1000000, 2000000, 3000000], JPY: [100000, 200000, 300000] };

function costPresets(ccy) {
  return COST_PRESETS[ccy] || [1000, 2000, 3000];
}

/**
 * Amount input with one-click presets (for example 1,000,000 / 2,000,000 / 3,000,000 KRW).
 * read() returns null for "use default", 0 for no cost cap, else the amount.
 */
function amountPicker({ value, currency, allowDefault = false, isDefault = false, defaultLabel = '' }) {
  const state = { value: isNum(value) ? value : 0, useDefault: isDefault };
  const input = textInput({ value: state.value ? String(state.value) : '', type: 'number', placeholder: t('Amount'), attrs: { min: 0, step: 'any', 'aria-label': t('Amount') } });
  const chips = h('div', { class: 'chips' });
  const preview = h('span', { class: 'amount-preview' });
  const render = () => {
    const opts = [];
    if (allowDefault) opts.push({ key: 'default', label: t('Use default'), pressed: state.useDefault });
    opts.push({ key: 'off', label: t('No cost cap'), pressed: !state.useDefault && !state.value });
    costPresets(currency).forEach((amount) => opts.push({ key: amount, label: fmt.currency(amount, currency, { compact: true }), pressed: !state.useDefault && state.value === amount }));
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
  return pricing.configured
    ? t('Entered prices: {inp} input \u00b7 {out} output per 1M tokens', { inp: money(pricing.inputPerMillion, ccy), out: money(pricing.outputPerMillion, ccy) })
    : t('Entered prices: not set');
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
      { key: 'input', label: t('Input'), align: 'num', render: (r) => (r.price ? money(r.price.input, ccy) : DASH), value: (r) => (r.price ? r.price.input : null) },
      { key: 'output', label: t('Output'), align: 'num', render: (r) => (r.price ? money(r.price.output, ccy) : DASH), value: (r) => (r.price ? r.price.output : null) },
    ],
    rows,
  });
}

/** Default caps. The administrator sets amounts only; token prices are found automatically. */
async function editPolicy(ctx, policy) {
  const pricing = policy.pricing || {};
  const workspaceTz = (store.boot && store.boot.workspace && store.boot.workspace.timezone) || 'UTC';
  const saved = typeof policy.currencySetting === 'string' ? policy.currencySetting : ccyOf(policy);
  const state = { period: policy.period || 'daily', window: policy.window || 'calendar', tz: policy.resetTimezone || 'UTC', currency: saved };
  const shownCcy = () => state.currency || (saved ? 'USD' : ccyOf(policy));
  const num = (v, step, min = 0) => textInput({ value: isNum(v) ? String(v) : '0', type: 'number', attrs: { min, step } });
  const tokenIn = num(policy.tokenCap, 1000);
  const reqIn = num(policy.requestCap, 10);
  const costHost = h('div');
  let cost = null;
  const renderCost = () => {
    const prev = cost ? cost.read() : (policy.costCap || 0);
    cost = amountPicker({ value: prev || 0, currency: shownCcy() });
    mount(costHost, cost);
  };
  const billingLabel = saved ? t('Billing account currency') : t('Billing account currency ({ccy})', { ccy: ccyOf(policy) });
  const currencyOptions = [{ value: '', label: billingLabel }, ...[...new Set([saved, ...CURRENCIES])].filter(Boolean).map((c) => ({ value: c, label: c }))];
  const currencySel = selectInput(currencyOptions, state.currency, (v) => { state.currency = v; renderCost(); }, { 'aria-label': t('Currency') });
  const priceInfo = h('div', { class: 'stack-sm' },
    h('div', { class: pricing.configured ? 'fw-600' : 'fw-600 text-warning' }, pricingSummary(pricing, ccyOf(policy))),
    h('p', { class: 'muted' }, pricing.source === 'manual'
      ? t('Entered token prices from Settings > Governance policy are used.')
      : t('Prices are found automatically: what your billing account actually paid per token in the last 30 days, and the public Google Cloud price list for models without billed usage. Amounts are converted with the Google exchange rate.')));
  const tzOptions = [...new Set(['UTC', workspaceTz, state.tz, 'Asia/Seoul', 'Asia/Tokyo', 'America/Los_Angeles', 'Europe/London'])].map((z) => ({ value: z, label: z === workspaceTz ? `${z} (${t('workspace')})` : z }));
  const err = h('div', { class: 'field-error' });
  const save = button({ label: t('Save policy'), variant: 'primary' });
  renderCost();
  const m = openModal({
    title: t('Edit cap policy'),
    subtitle: t('Caps apply to each person within the settlement period. Give specific people a different amount from the Cost cap column of the usage table or with exception rules.'),
    wide: true,
    body: (el) => append(el, [
      h('div', { class: 'field-row' },
        field({ label: t('Cost cap per person'), help: t('Users above this estimated cost are suspended (automatic mode) or queued for review.'), control: costHost }),
        field({ label: t('Currency'), help: t('Cost caps and estimates are shown in this currency'), control: currencySel })),
      h('div', { class: 'field-row' },
        field({ label: t('Token cap per user'), help: t('Total tokens within the period. 0 disables it.'), control: tokenIn }),
        field({ label: t('Request cap per user'), help: t('Inference requests within the period. 0 disables it.'), control: reqIn })),
      h('div', { class: 'field-row' },
        field({ label: t('Settlement period'), control: selectInput(PERIODS.map(([v, l]) => ({ value: v, label: t(l) })), state.period, (v) => { state.period = v; }) }),
        field({ label: t('Window'), control: selectInput([{ value: 'calendar', label: t('Calendar (fixed reset)') }, { value: 'rolling', label: t('Rolling') }], state.window, (v) => { state.window = v; }) }),
        field({ label: t('Reset time zone'), help: t('Calendar periods reset at local midnight'), control: selectInput(tzOptions, state.tz, (v) => { state.tz = v; }) })),
      field({ label: t('Token prices'), control: priceInfo }),
      err]),
    footer: (el) => append(el, [button({ label: t('Cancel'), onClick: () => m.close() }), save]),
  });
  save.addEventListener('click', async () => {
    err.textContent = '';
    const tc = Number(tokenIn.value);
    const rc = Number(reqIn.value);
    if (!Number.isInteger(tc) || tc < 0 || !Number.isInteger(rc) || rc < 0) { err.textContent = t('Caps must be whole numbers of zero or more.'); return; }
    if (!cost.valid()) { err.textContent = t('The cost cap must be a number of zero or more.'); return; }
    const values = {
      token_cap: tc, request_cap: rc, cost_cap: cost.read() || 0, currency: state.currency,
      period: state.period, window: state.window, reset_timezone: state.tz,
    };
    setBusy(save, true, t('Saving'));
    try {
      await api.post('/api/v1/settings', { section: 'governance', values });
      toast({ title: t('Cap policy saved'), tone: 'success' });
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

async function editPersonCap(ctx, s, user) {
  const policy = s.policy || {};
  const ccy = ccyOf(policy);
  const rule = user.exception;
  const isLimit = rule && rule.type === 'limit';
  const ownCost = isLimit && isNum(rule.costCap) ? rule.costCap : null;
  const picker = amountPicker({ value: ownCost || 0, currency: ccy, allowDefault: true, isDefault: ownCost === null, defaultLabel: defaultCostLabel(policy, ccy) });
  const err = h('div', { class: 'field-error' });
  const save = button({ label: t('Save'), variant: 'primary' });
  const usage = isNum(user.estimatedCost) ? t('Estimated cost this period: {v}', { v: money(user.estimatedCost, ccy) }) : t('No estimated cost this period');
  const m = openModal({
    title: t('Cost cap for {email}', { email: user.email }),
    subtitle: t('Default for everyone: {v}', { v: policy.costCap ? fmt.currency(policy.costCap, ccy) : t('no cost cap') }),
    body: (el) => append(el, [
      rule && !isLimit ? banner({ tone: 'info', text: t('This person has an exception rule ({rule}). Saving an individual cost cap replaces it.', { rule: ruleBadge(rule).textContent }) }) : null,
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
      if (amount === null && !keepOther) {
        if (isLimit) await api.post('/api/v1/governance/antigravity/exceptions/delete', { email: user.email });
      } else {
        await api.post('/api/v1/governance/antigravity/exceptions', {
          email: user.email, type: 'limit', scope: isLimit && rule.scope === 'period' ? 'period' : 'permanent',
          tokenCap: isLimit ? rule.tokenCap : null, requestCap: isLimit ? rule.requestCap : null, costCap: amount,
          note: isLimit && rule.note ? rule.note : t('Individual cost cap'),
        });
      }
      toast({ title: t('Cost cap saved'), text: amount === null ? t('{email} uses the default cap.', { email: user.email }) : t('{email}: {v}', { email: user.email, v: amount ? fmt.currency(amount, ccy) : t('no cost cap') }), tone: 'success' });
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

/** Google Cloud API calls that coding agents (Antigravity, Jetski) made with users' credentials. */
function agentActivityCard(ctx) {
  const host = h('div', null, skeleton('table'));
  const c = card({
    title: t('Agent activity in your cloud'),
    subtitle: t('Google Cloud API calls that coding agents such as Antigravity and Jetski made with user credentials \u00b7 {range}', { range: rangeText() }),
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
          onRowClick: (r) => r.uid && openUserDrawer(r.uid),
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
