/**
 * Settings: white-label branding, workspace data sources, module toggles, model catalog,
 * AI engine, governance policy, access control and deployment information.
 */
import { h, mount, append } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH, initials } from '../core/format.js';
import { api, isAbort } from '../core/api.js';
import { store, isAdmin, session } from '../core/state.js';
import {
  pageHeader, card, button, banner, badge, statusBadge, switchInput, selectInput, field, textInput, toast, setBusy, kv, chip,
  errorState, skeleton, confirmDialog, emptyState, dataTable, openDrawer, alertPermissionRequired, requireAdmin,
} from '../core/components.js';
import { errorText, serverText } from '../core/messages.js';

const TABS = [
  ['general', 'General', 'grid'],
  ['access', 'Access control', 'lock'],
  ['workspace', 'Workspace and data sources', 'database'],
  ['modules', 'Modules', 'power'],
  ['models', 'Model catalog', 'layers'],
  ['ai', 'AI engine', 'spark'],
  ['governance', 'Governance policy', 'sliders'],
  ['about', 'About', 'info'],
];

const MODULES = [
  ['overview', 'Overview', 'Cross-module KPIs and posture summary.'],
  ['gemini_enterprise', 'Gemini Enterprise', 'Prompts, active users, agent usage, licenses and uploaded files.'],
  ['notebooklm', 'NotebookLM', 'Notebook activity and prompts from data access audit logs.'],
  ['model_usage', 'Models & Tokens', 'Per-model token, invocation and cost tracking with the model catalog.'],
  ['agent_gateway', 'Agent Gateway', 'Traffic, authentication outcomes and routing for AI services.'],
  ['agent_registry', 'Agent Registry', 'Unified agent catalog with idle and deprecation detection.'],
  ['agent_deployments', 'Deployments', 'Agent Engine runtimes, identities and per-identity model usage.'],
  ['model_armor', 'Model Armor', 'Screening verdicts, filter matches and blocked content.'],
  ['users', 'Users & Access', 'Per-user activity across surfaces with IAM enforcement state.'],
  ['antigravity', 'Antigravity Controls', 'Usage caps enforced through IAM with safety gates.'],
  ['finops', 'Cost & FinOps', 'Spend, credits, SKUs and AI-assisted spike analysis.'],
  ['telemetry', 'Telemetry Extensions', 'Auto-discovered telemetry sources and custom connectors.'],
  ['analyst', 'AI Analyst', 'Conversational analytics over governance datasets.'],
];

function getPath(obj, path) {
  return path.reduce((acc, k) => (acc && typeof acc === 'object' ? acc[k] : undefined), obj);
}

function setPath(obj, path, value) {
  let cur = obj;
  path.slice(0, -1).forEach((k) => { if (!cur[k] || typeof cur[k] !== 'object') cur[k] = {}; cur = cur[k]; });
  cur[path[path.length - 1]] = value;
}

function listToText(v) {
  return Array.isArray(v) ? v.join(', ') : (v || '');
}

function textToList(v) {
  return String(v || '').split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
}

export default async function render(ctx) {
  const tab = TABS.some((x) => x[0] === ctx.params[0]) ? ctx.params[0] : 'general';
  const header = pageHeader({ title: t('Settings'), desc: t('Configure the workspace, branding, modules and governance policies. Changes apply immediately to every user.') });
  const nav = h('nav', { class: 'settings-nav', 'aria-label': t('Settings sections') }, TABS.map(([id, label, ic]) => h('button', {
    type: 'button', 'aria-current': id === tab ? 'true' : 'false', onclick: () => ctx.navigate(`#/settings/${id}`),
  }, icon(ic, { size: 15 }), t(label))));
  const panel = h('div', { class: 'settings-panel' }, skeleton('block', 260));
  mount(ctx.root, header, h('div', { class: 'settings-layout' }, nav, panel));

  let data;
  try {
    data = (await api.get('/api/v1/settings', null, { signal: ctx.signal })).data;
  } catch (err) {
    if (isAbort(err) || ctx.signal.aborted) return null;
    mount(panel, card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    return null;
  }
  if (ctx.signal.aborted) return null;
  const editable = Boolean(data.editable) && isAdmin();
  mount(panel);
  if (!editable) {
    panel.append(banner({
      tone: 'neutral',
      iconName: 'lock',
      title: t('Read-only'),
      text: t('Only administrators can change settings. Values are shown for reference.'),
      actions: button({ label: t('Why is this read-only?'), size: 'sm', onClick: () => alertPermissionRequired({ action: t('Change workspace settings') }) }),
    }));
  }
  if (data.persistence && data.persistence.durable === false) {
    panel.append(banner({ tone: 'warning', title: t('Settings are not persisted durably'), text: t('Changes are stored in {backend} and may be lost when the service restarts. Configure a Cloud Storage bucket (storage.gcs_bucket) for durable settings.', { backend: data.persistence.backend }) }));
  }
  const view = { general, workspace, modules, models, ai, governance, access, about }[tab];
  view(ctx, panel, data, editable);
  return null;
}

/* ------------------------------------------------------------------ form helper */

/**
 * Build a settings card for one section.
 * fields: [{ path: ['key'] | ['billing','dataset'], label, help, type, options, mono, min, max, step }]
 */
function sectionForm(ctx, { title, subtitle, section, data, fields, editable, extraActions, sourceObj }) {
  const effective = sourceObj || (section === 'governance' ? getPath(data.effective, ['governance', 'antigravity']) : getPath(data.effective, [section])) || {};
  const overrides = (section === 'governance' ? getPath(data.overrides, ['governance', 'antigravity']) : getPath(data.overrides, [section])) || {};
  const values = {};
  const err = h('div', { class: 'field-error' });
  const controls = fields.map((f) => {
    const cur = getPath(effective, f.path);
    const overridden = getPath(overrides, f.path) !== undefined;
    let control;
    const set = (v) => setPath(values, f.path, v);
    switch (f.type) {
      case 'switch':
        control = h('div', { class: 'row' }, switchInput({ checked: Boolean(cur), disabled: !editable, label: f.label, onChange: (v) => set(v) }));
        break;
      case 'select':
        control = selectInput(f.options, cur === undefined || cur === null ? '' : String(cur), (v) => set(f.numeric ? Number(v) : v), { disabled: !editable || null, 'aria-label': f.label });
        break;
      case 'number':
        control = textInput({ value: isNum(cur) ? String(cur) : '', type: 'number', attrs: { min: f.min, max: f.max, step: f.step || 1, disabled: !editable || null }, onInput: (v) => set(v === '' ? null : Number(v)) });
        break;
      case 'list':
        control = textInput({ value: listToText(cur), mono: f.mono, attrs: { disabled: !editable || null, placeholder: f.placeholder || '' }, onInput: (v) => set(textToList(v)) });
        break;
      case 'textarea': {
        const ta = h('textarea', { class: ['textarea', f.mono ? 'mono' : ''], rows: f.rows || 3, disabled: !editable || null, placeholder: f.placeholder || '' });
        ta.value = Array.isArray(cur) ? cur.join('\n') : (cur || '');
        ta.addEventListener('input', () => set(f.asList ? textToList(ta.value) : ta.value));
        control = ta;
        break;
      }
      case 'color': {
        const txt = textInput({ value: cur || '#2563EB', mono: true, attrs: { maxlength: 7, disabled: !editable || null } });
        const pick = h('input', { type: 'color', class: 'color-swatch-input', value: /^#[0-9A-Fa-f]{6}$/.test(cur || '') ? cur : '#2563EB', disabled: !editable || null, 'aria-label': f.label });
        pick.addEventListener('input', () => { txt.value = pick.value.toUpperCase(); set(txt.value); });
        txt.addEventListener('input', () => { if (/^#[0-9A-Fa-f]{6}$/.test(txt.value)) pick.value = txt.value; set(txt.value); });
        control = h('div', { class: 'input-group' }, pick, txt);
        break;
      }
      default:
        control = textInput({ value: cur === undefined || cur === null ? '' : String(cur), mono: f.mono, attrs: { disabled: !editable || null, placeholder: f.placeholder || '', maxlength: f.maxlength || 512 }, onInput: (v) => set(v) });
    }
    const label = h('span', { class: 'row' }, f.label, overridden ? h('span', { class: 'override-tag', title: t('Changed from the deployment default') }, t('Customized')) : null);
    return field({ label, help: f.help, control });
  });
  const save = button({ label: t('Save changes'), variant: 'primary' });
  const hasOverrides = Object.keys(overrides).length > 0;
  const reset = hasOverrides && editable ? button({ label: t('Reset to defaults'), variant: 'ghost', icon: 'refresh' }) : null;
  save.addEventListener('click', async () => {
    if (!editable) {
      alertPermissionRequired({ action: `${t('Save changes')}: ${title}` });
      return;
    }
    err.textContent = '';
    if (!Object.keys(values).length) { toast({ title: t('No changes to save'), tone: 'info', timeout: 2500 }); return; }
    if (section === 'workspace' || section === 'security') {
      const ok = await confirmDialog({ title: t('Apply workspace change'), message: t('Data source and access settings affect every user and every module. Continue?'), confirmLabel: t('Apply') });
      if (!ok) return;
    }
    setBusy(save, true, t('Saving'));
    try {
      await api.post('/api/v1/settings', { section, values });
      toast({ title: t('Settings saved'), tone: 'success' });
      await afterSave(ctx);
    } catch (e) {
      setBusy(save, false);
      err.textContent = errorText(e);
    }
  });
  if (reset) {
    reset.addEventListener('click', async () => {
      const ok = await confirmDialog({ title: t('Reset section'), message: t('Customized values in this section will revert to the deployment defaults from config.yaml and environment variables.'), confirmLabel: t('Reset'), tone: 'danger' });
      if (!ok) return;
      try {
        await api.post('/api/v1/settings/reset', { section, keys: [] });
        toast({ title: t('Section reset to defaults'), tone: 'success' });
        await afterSave(ctx);
      } catch (e) {
        toast({ title: t('Reset failed'), text: errorText(e), tone: 'danger' });
      }
    });
  }
  const c = card({ title, subtitle, bordered: true, body: h('div', { class: 'form-grid' }, h('div', { class: 'field-row' }, controls), err) });
  c.bodyEl.classList.add('flush');
  c.append(h('div', { class: 'form-actions' }, extraActions || null, h('span', { class: 'spacer' }), reset, save));
  return c;
}

async function afterSave(ctx) {
  try { await ctx.reloadBootstrap(); } catch (e) { /* shell keeps previous state */ }
  ctx.rerender();
}

/* ------------------------------------------------------------------ tabs */

function general(ctx, panel, data, editable) {
  panel.append(sectionForm(ctx, {
    title: t('Product'), subtitle: t('White-label name shown in the navigation, browser title and exports.'), section: 'product', data, editable,
    fields: [
      { path: ['name'], label: t('Product name'), maxlength: 60 },
      { path: ['tagline'], label: t('Tagline'), maxlength: 140 },
    ],
  }));
  panel.append(sectionForm(ctx, {
    title: t('Branding'), subtitle: t('Customer identity and visual defaults.'), section: 'branding', data, editable,
    fields: [
      { path: ['organization_name'], label: t('Organization name'), maxlength: 80 },
      { path: ['logo_url'], label: t('Logo URL'), help: t('HTTPS URL or /assets/ path. Leave empty to use the generated monogram.'), mono: true, placeholder: 'https://' },
      { path: ['accent_color'], label: t('Accent color'), type: 'color' },
      { path: ['default_locale'], label: t('Default language'), type: 'select', options: [{ value: 'en', label: 'English' }, { value: 'ko', label: '\ud55c\uad6d\uc5b4' }] },
      { path: ['default_theme'], label: t('Default theme'), type: 'select', options: [{ value: 'system', label: t('System') }, { value: 'light', label: t('Light') }, { value: 'dark', label: t('Dark') }] },
    ],
  }));
}

function workspace(ctx, panel, data, editable) {
  const testHost = h('div');
  const testBtn = button({ label: t('Test connections'), icon: 'activity' });
  testBtn.addEventListener('click', async () => {
    setBusy(testBtn, true, t('Testing'));
    try {
      const res = await api.get('/api/v1/health', { refresh: 1 });
      mount(testHost, h('div', { class: 'checklist' }, (res.data.checks || []).map((c) => h('div', { class: 'check-item' },
        statusBadge(c.status, { size: 'sm' }), h('div', { class: 'ci-text' }, h('div', null, t(c.label || c.id)), c.detail ? h('div', { class: 'ci-sub' }, serverText(c.detail)) : null),
        isNum(c.latencyMs) ? h('span', { class: 'muted tabular' }, fmt.duration(c.latencyMs)) : null))));
      if (ctx.refreshHealth) ctx.refreshHealth();
    } catch (e) {
      mount(testHost, banner({ tone: 'danger', title: t('Health check failed'), text: errorText(e) }));
    } finally {
      setBusy(testBtn, false);
    }
  });
  const tzOptions = ['UTC', 'Asia/Seoul', 'Asia/Tokyo', 'Asia/Singapore', 'Asia/Shanghai', 'Asia/Kolkata', 'Europe/London', 'Europe/Berlin', 'Europe/Paris', 'America/New_York', 'America/Chicago', 'America/Los_Angeles', 'Australia/Sydney'];
  const curTz = getPath(data.effective, ['workspace', 'timezone']);
  if (curTz && !tzOptions.includes(curTz)) tzOptions.unshift(curTz);
  const activeProjects = (store.boot && store.boot.workspace && store.boot.workspace.projects) || [];
  if (activeProjects.length > 0) {
    panel.append(card({
      title: t('Scanned Google Cloud projects ({n})', { n: activeProjects.length }),
      subtitle: t('Projects actively scanned across Agent Registry, Deployments, Gemini Enterprise, Cloud Monitoring and Telemetry.'),
      body: h('div', { class: 'row wrap' }, activeProjects.map((p) => badge(p, p === getPath(data.effective, ['workspace', 'project_id']) ? 'info' : 'neutral', { mono: true }))),
    }));
  }
  panel.append(sectionForm(ctx, {
    title: t('Google Cloud workspace'), subtitle: t('The project and datasets the control plane reads from.'), section: 'workspace', data, editable,
    extraActions: testBtn,
    fields: [
      { path: ['project_id'], label: t('Project ID'), mono: true },
      { path: ['scan_all_projects'], label: t('Auto-discover organization projects'), type: 'switch', help: t('Automatically scan all accessible active Google Cloud projects in the organization.') },
      { path: ['projects'], label: t('Additional projects to scan'), type: 'list', mono: true, help: t('Comma separated project IDs scanned alongside the workspace project.') },
      { path: ['quota_project_id'], label: t('Quota project'), help: t('Project billed for API quota. Empty uses the workspace project.'), mono: true },
      { path: ['audit_dataset'], label: t('Audit log dataset'), help: t('BigQuery dataset receiving the Log Router sink.'), mono: true },
      { path: ['timezone'], label: t('Reporting timezone'), type: 'select', options: tzOptions.map((z) => ({ value: z, label: z })) },
      { path: ['default_range_days'], label: t('Default range (days)'), type: 'select', numeric: true, options: [7, 14, 30, 90].map((n) => ({ value: String(n), label: String(n) })) },
      { path: ['billing', 'project_id'], label: t('Billing export project'), help: t('Empty uses the workspace project.'), mono: true },
      { path: ['billing', 'dataset'], label: t('Billing export dataset'), mono: true },
      { path: ['billing', 'table'], label: t('Billing export table'), help: t('Detailed (resource-level) export table, gcp_billing_export_resource_v1_...'), mono: true },
      { path: ['billing', 'account_id'], label: t('Billing account ID'), mono: true },
      { path: ['discovery_engine_locations'], label: t('Gemini Enterprise locations'), type: 'list', mono: true, help: t('Comma separated, for example global, us, eu.') },
      { path: ['agent_engine_regions'], label: t('Agent Engine regions'), type: 'list', mono: true, help: t('Comma separated, for example us-central1, asia-northeast3.') },
      { path: ['telemetry_datasets'], label: t('Additional telemetry datasets'), type: 'list', mono: true, help: t('Scanned for new telemetry sources in addition to the audit dataset.') },
    ],
  }));
  panel.append(card({ title: t('Connection test'), subtitle: t('Runs every data source health check against the saved configuration.'), body: testHost }));
  mount(testHost, h('p', { class: 'muted' }, t('Select Test connections to verify access to BigQuery, Discovery Engine, Agent Engine, Cloud Monitoring, Cloud Logging and IAM.')));
}

function modules(ctx, panel, data, editable) {
  const eff = getPath(data.effective, ['modules']) || {};
  const over = getPath(data.overrides, ['modules']) || {};
  const values = {};
  const rows = MODULES.map(([id, label, desc]) => h('div', { class: 'setting-row' },
    h('div', { class: 'setting-info' },
      h('div', { class: 'setting-title' }, t(label), over[id] !== undefined ? h('span', { class: 'override-tag' }, t('Customized')) : null),
      h('div', { class: 'setting-desc' }, t(desc))),
    h('div', { class: 'setting-control' }, switchInput({ checked: eff[id] !== false, disabled: !editable, label: t(label), onChange: (v) => { values[id] = v; } }))));
  const save = button({ label: t('Save modules'), variant: 'primary' });
  save.addEventListener('click', async () => {
    if (!editable) {
      alertPermissionRequired({ action: t('Save modules') });
      return;
    }
    if (!Object.keys(values).length) { toast({ title: t('No changes to save'), tone: 'info', timeout: 2500 }); return; }
    setBusy(save, true, t('Saving'));
    try {
      await api.post('/api/v1/settings', { section: 'modules', values });
      toast({ title: t('Modules updated'), text: t('Disabled modules are hidden and their queries are skipped.'), tone: 'success' });
      await afterSave(ctx);
    } catch (e) {
      setBusy(save, false);
      toast({ title: t('Save failed'), text: errorText(e), tone: 'danger' });
    }
  });
  const c = card({ title: t('Modules'), subtitle: t('Turn product modules on or off for this workspace. Disabled modules are hidden for every user and their data sources are not queried.'), bordered: true, flush: true, body: h('div', null, rows) });
  c.append(h('div', { class: 'form-actions' }, save));
  panel.append(c);
}

function models(ctx, panel, data, editable) {
  panel.append(sectionForm(ctx, {
    title: t('Model catalog policy'), subtitle: t('How newly discovered models are handled.'), section: 'model_catalog', data, editable,
    extraActions: button({ label: t('Open catalog'), icon: 'layers', onClick: () => ctx.navigate('#/models') }),
    fields: [
      { path: ['auto_enable_new_models'], label: t('Automatically enable new models'), type: 'switch', help: t('New Gemini series and partner models start being tracked as soon as they appear.') },
      { path: ['ai_grouping'], label: t('AI-assisted SKU grouping'), type: 'switch', help: t('Use the AI engine to refine how billing SKUs are grouped into models (cached for 6 hours).') },
      { path: ['excluded_sku_patterns'], label: t('Excluded SKU patterns'), type: 'list', help: t('Case-insensitive substrings. Matching SKUs are excluded from every cost and token view.') },
      { path: ['disabled_models'], label: t('Disabled models'), type: 'list', mono: true, help: t('Model keys excluded from tracking. Easier to manage from the catalog.') },
    ],
  }));
}

async function ai(ctx, panel, data, editable) {
  const eff = getPath(data.effective, ['ai_engine']) || {};
  let discovered = null;
  try { discovered = (await api.get('/api/v1/ai/models', null, { signal: ctx.signal })).data; } catch (e) { discovered = null; }
  const ids = [...new Set([...(eff.models || []), ...((discovered && discovered.models) || []).map((m) => m.id)])];
  panel.append(sectionForm(ctx, {
    title: t('AI engine'), subtitle: t('Gemini model used by AI Analyst, FinOps spike analysis and optional SKU grouping. Users can switch among allowed models.'), section: 'ai_engine', data, editable,
    fields: [
      { path: ['default_model'], label: t('Default model'), type: 'select', options: ids.map((id) => ({ value: id, label: id })) },
      { path: ['location'], label: t('Agent Platform location'), mono: true, help: t('global is recommended for the latest Gemini models.') },
      { path: ['models'], label: t('Allowed models'), type: 'list', mono: true, help: t('Comma separated model IDs users may select.') },
      { path: ['discover_models'], label: t('Discover available models'), type: 'switch', help: t('List Gemini models available to the project through the Agent Platform Models API.') },
      { path: ['restrict_to_allowed'], label: t('Restrict to allowed models'), type: 'switch', help: t('Reject requests for models that are not in the allowed list.') },
    ],
  }));
  const testModel = h('span');
  let pick = eff.default_model || ids[0];
  mount(testModel, selectInput(ids.map((id) => ({ value: id, label: id })), pick, (v) => { pick = v; }, { 'aria-label': t('Model to test') }));
  const result = h('div');
  const testBtn = button({ label: t('Run test'), icon: 'play' });
  testBtn.addEventListener('click', async () => {
    if (!editable) {
      alertPermissionRequired({ action: t('Run test') });
      return;
    }
    setBusy(testBtn, true, t('Testing'));
    try {
      const r = (await api.post('/api/v1/ai/test', { model: pick })).data;
      mount(result, banner({ tone: r.ok ? 'success' : 'danger', title: r.ok ? t('{model} responded in {ms}', { model: r.model, ms: fmt.duration(r.latencyMs) }) : t('Model test failed'), text: serverText(r.message) }));
    } catch (e) {
      mount(result, banner({ tone: 'danger', title: t('Model test failed'), text: errorText(e) }));
    } finally {
      setBusy(testBtn, false);
    }
  });
  panel.append(card({
    title: t('Discovered models'),
    subtitle: discovered ? t('Discovery status: {s}', { s: discovered.discoveryStatus }) : t('Model discovery unavailable'),
    body: h('div', { class: 'stack' },
      h('div', { class: 'chips' }, ((discovered && discovered.models) || []).map((m) => chip(`${m.id}${m.source === 'discovered' ? '' : ' *'}`, { title: m.source }))),
      h('p', { class: 'muted' }, t('* configured in the allowed list')),
      h('div', { class: 'row wrap' }, testModel, testBtn),
      result),
  }));
}

function governance(ctx, panel, data, editable) {
  const workspaceTz = getPath(data.effective, ['workspace', 'timezone']) || 'UTC';
  const resetTz = getPath(data.effective, ['governance', 'antigravity', 'reset_timezone']) || 'UTC';
  const tzChoices = [...new Set(['UTC', workspaceTz, resetTz, 'Asia/Seoul', 'Asia/Tokyo', 'America/Los_Angeles', 'Europe/London'])];
  const curCcy = getPath(data.effective, ['governance', 'antigravity', 'currency']) || '';
  const ccyChoices = [...new Set(['', curCcy, 'KRW', 'USD', 'JPY', 'EUR', 'GBP', 'SGD', 'AUD', 'CNY', 'INR', 'TWD'])];
  panel.append(sectionForm(ctx, {
    title: t('Antigravity cap policy'), subtitle: t('Defaults for cap evaluation and enforcement cycles.'), section: 'governance', data, editable,
    fields: [
      { path: ['enforcement_mode'], label: t('Enforcement mode'), type: 'select', options: [{ value: 'manual', label: t('Manual review') }, { value: 'auto', label: t('Automatic suspension') }], help: t('Automatic suspends users over their cap until the period resets; manual waits for an administrator decision.') },
      { path: ['token_cap'], label: t('Token cap per user'), type: 'number', min: 0, step: 1000, help: t('0 disables the token cap.') },
      { path: ['request_cap'], label: t('Request cap per user'), type: 'number', min: 0, step: 10, help: t('0 disables the request cap.') },
      { path: ['cost_cap'], label: t('Cost cap per person'), type: 'number', min: 0, step: 'any', help: t('Amount in the currency below, for example 1000000 for KRW 1,000,000. 0 disables it. Individual amounts are set on the Antigravity Controls page.') },
      { path: ['currency'], label: t('Currency'), type: 'select', options: ccyChoices.map((c) => ({ value: c, label: c || t('Billing account currency') })), help: t('Cost caps and estimates are shown in this currency') },
      { path: ['pricing_source'], label: t('Token prices'), type: 'select', options: [{ value: 'auto', label: t('Automatic (recommended)') }, { value: 'manual', label: t('Enter prices') }], help: t('Automatic uses what your billing account paid per token, then the public Google price list.') },
      { path: ['input_price_per_million'], label: t('Input price per 1M tokens'), type: 'number', min: 0, step: 0.01, help: t('Used only when token prices are entered') },
      { path: ['output_price_per_million'], label: t('Output price per 1M tokens'), type: 'number', min: 0, step: 0.01, help: t('Used only when token prices are entered') },
      { path: ['period'], label: t('Settlement period'), type: 'select', options: [['daily', 'Daily'], ['weekly', 'Weekly'], ['monthly', 'Monthly'], ['all', 'Last 90 days']].map(([v, l]) => ({ value: v, label: t(l) })) },
      { path: ['window'], label: t('Window'), type: 'select', options: [{ value: 'calendar', label: t('Calendar (fixed reset)') }, { value: 'rolling', label: t('Rolling') }] },
      { path: ['reset_timezone'], label: t('Reset time zone'), type: 'select', options: tzChoices.map((z) => ({ value: z, label: z })), help: t('Calendar periods reset at local midnight') },
      { path: ['change_budget_min'], label: t('Change budget minimum'), type: 'number', min: 1, help: t('Circuit breaker: maximum of this value and the ratio below.') },
      { path: ['change_budget_ratio'], label: t('Change budget ratio'), type: 'number', min: 0, max: 1, step: 0.05 },
      { path: ['usage_source'], label: t('Usage source'), type: 'select', options: [{ value: 'auto', label: t('Automatic') }, { value: 'bigquery', label: t('BigQuery log sink') }, { value: 'logging', label: t('Cloud Logging API') }] },
      { path: ['max_log_entries'], label: t('Maximum log entries per evaluation'), type: 'number', min: 1000, step: 1000 },
      { path: ['access_role'], label: t('Access role'), mono: true },
      { path: ['cap_marker_role'], label: t('Cap marker role'), mono: true },
      { path: ['admin_marker_role'], label: t('Admin marker role'), mono: true },
      { path: ['privileged_roles'], label: t('Privileged roles'), type: 'list', mono: true },
      { path: ['extra_access_roles'], label: t('Other access roles'), type: 'list', mono: true, help: t('Custom roles that also grant Antigravity access.') },
      { path: ['webhook_url'], label: t('Notification webhook'), mono: true, help: t('HTTPS Slack or Google Chat webhook for enforcement notifications.'), placeholder: 'https://chat.googleapis.com/...' },
    ],
  }));
  panel.append(sectionForm(ctx, {
    title: t('Agent lifecycle'), subtitle: t('Thresholds used to classify agents in the registry.'), section: 'agent_lifecycle', data, editable,
    fields: [
      { path: ['idle_days'], label: t('Idle after (days)'), type: 'number', min: 1 },
      { path: ['deprecation_days'], label: t('Deprecation candidate after (days)'), type: 'number', min: 1 },
    ],
  }));
  panel.append(sectionForm(ctx, {
    title: t('Cost spike detection'), section: 'finops', data, editable,
    fields: [
      { path: ['spike_threshold_pct'], label: t('Day-over-day threshold (%)'), type: 'number', min: 1 },
      { path: ['spike_min_delta'], label: t('Minimum increase (billing currency)'), type: 'number', min: 0, step: 0.5 },
    ],
  }));
  panel.append(sectionForm(ctx, {
    title: t('Agent Gateway scope'), section: 'gateway', data, editable,
    fields: [{ path: ['services'], label: t('Monitored services'), type: 'textarea', asList: true, mono: true, rows: 5, help: t('One API service name per line.') }],
  }));
  panel.append(sectionForm(ctx, {
    title: t('AI Analyst limits'), section: 'analyst', data, editable,
    fields: [
      { path: ['max_bytes_billed'], label: t('Maximum bytes billed per query'), type: 'number', min: 10000000, step: 1000000000 },
      { path: ['max_rows'], label: t('Maximum rows returned'), type: 'number', min: 10, max: 5000 },
      { path: ['allow_viewers'], label: t('Allow viewers to use AI Analyst'), type: 'switch',
        help: t('Query results can include free text, such as prompts, that the dashboards hide from viewers. Off by default.') },
    ],
  }));
}

function access(ctx, panel, data, editable) {
  const s = session();
  const sec = getPath(data.effective, ['security']) || {};
  const google = s.authMode === 'google';
  panel.append(card({
    title: t('Current session'),
    actions: button({
      label: t('Add account'),
      icon: 'plus',
      size: 'sm',
      variant: 'primary',
      onClick: () => {
        if (!requireAdmin(t('Add account / Manage access control'))) return;
        openAccessDrawer({ onChanged: () => ctx.rerender() });
      },
    }),
    body: kv([
      [t('Signed in as'), s.email || DASH],
      [t('Role'), s.role === 'admin' ? badge(t('Administrator'), 'accent', { size: 'sm' }) : badge(t('Viewer'), 'neutral', { size: 'sm' })],
      [t('Authentication'), { google: t('Google sign-in (approved accounts only)'), iap: t('Identity-Aware Proxy (verified JWT)'), local: t('Local development') }[s.authMode] || s.authMode],
      [t('Configured mode'), h('span', { class: 'mono' }, sec.auth_mode || DASH)],
      google ? [t('Session length'), t('{n} hours', { n: sec.session_hours || 12 })] : [t('IAP audience'), sec.iap_audience ? h('span', { class: 'mono' }, sec.iap_audience) : t('Not configured')],
    ]),
  }));
  if (isAdmin()) {
    panel.append(accessMembers(ctx, editable));
    panel.append(sectionForm(ctx, {
      title: t('Privacy'), subtitle: t('Applies to every viewer account.'), section: 'security', data, editable,
      fields: [{ path: ['mask_pii_for_viewers'], label: t('Mask personal data for viewers'), type: 'switch', help: t('Hide user emails, prompt text and file names from viewers.') }],
    }));
    return;
  }
  panel.append(sectionForm(ctx, {
    title: t('Access control'), subtitle: t('Administrators can change settings and run governance actions. Everyone admitted by Identity-Aware Proxy is a viewer.'), section: 'security', data, editable,
    fields: [
      { path: ['admins'], label: t('Administrators'), type: 'textarea', asList: true, mono: true, rows: 4, help: t('One email or *@domain.com pattern per line. Administrators defined in the deployment configuration always remain.') },
      { path: ['mask_pii_for_viewers'], label: t('Mask personal data for viewers'), type: 'switch', help: t('Hide user emails, prompt text and file names from viewers.') },
    ],
  }));
}

const MEMBER_RE = /^(?:\*|(?:[A-Za-z0-9._%+\-']+|\*)@[A-Za-z0-9.-]+\.[A-Za-z]{2,})$/;
const ACCOUNT_ID_RE = /^[a-z0-9][a-z0-9._@\-]{1,127}$/;

export function parseMemberTokens(raw) {
  return [...new Set(String(raw || '').split(/[,;\s]+/).map((s) => s.trim().toLowerCase()).filter(Boolean))];
}

function openPasswordModal(account, onSaved) {
  const pwInput = textInput({
    value: '',
    type: 'password',
    placeholder: t('At least 4 characters'),
    attrs: { 'aria-label': t('New password'), autocomplete: 'new-password', maxlength: 256 },
  });
  const confirmInput = textInput({
    value: '',
    type: 'password',
    placeholder: t('Re-enter new password'),
    attrs: { 'aria-label': t('Confirm password'), autocomplete: 'new-password', maxlength: 256 },
  });
  const errEl = h('div', { class: 'field-error', role: 'alert' });
  const saveBtn = button({ label: t('Save password'), variant: 'primary', icon: 'check' });
  const cancelBtn = button({ label: t('Cancel'), variant: 'ghost' });

  const body = h('div', { class: 'stack' },
    banner({
      tone: 'info',
      title: account.email === 'admin' ? t('Administrator password (admin)') : t('Password for {email}', { email: account.email }),
      text: account.defaultPassword
        ? t('This account currently uses the initial default password ("admin"). Set a custom password to secure it.')
        : t('Set a new password for signing in directly on the login page with this account ID.'),
    }),
    field({ label: t('New password'), control: pwInput }),
    field({ label: t('Confirm password'), control: confirmInput }),
    errEl,
    h('div', { class: 'row', style: 'justify-content:flex-end;gap:8px;margin-top:8px' }, cancelBtn, saveBtn));

  const d = openDrawer({
    title: account.hasPassword ? t('Change password') : t('Set password'),
    subtitle: account.email,
    content: body,
    width: 460,
  });

  cancelBtn.addEventListener('click', () => d.close());
  saveBtn.addEventListener('click', async () => {
    errEl.textContent = '';
    const pw = pwInput.value;
    const cf = confirmInput.value;
    if (!pw || pw.length < 4) {
      errEl.textContent = t('Password must be at least 4 characters.');
      pwInput.focus();
      return;
    }
    if (pw !== cf) {
      errEl.textContent = t('Passwords do not match.');
      confirmInput.focus();
      return;
    }
    setBusy(saveBtn, true, t('Saving'));
    try {
      const res = await api.post('/api/v1/access/users', {
        email: account.email,
        role: account.role || 'viewer',
        password: pw,
      });
      toast({ title: t('Password updated'), text: account.email, tone: 'success' });
      d.close();
      if (onSaved) onSaved(res.data);
    } catch (e) {
      setBusy(saveBtn, false);
      errEl.textContent = errorText(e);
    }
  });
  pwInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveBtn.click(); });
  confirmInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveBtn.click(); });
  setTimeout(() => pwInput.focus(), 60);
}

/** Approved accounts (ID/Password & Google OAuth), roles and pending access requests (administrators only). */
export function accessMembers(ctx = {}, editable = true) {
  const signal = ctx && ctx.signal;
  const inDrawer = Boolean(ctx && ctx.inDrawer);
  const rerender = (ctx && ctx.rerender) || (() => {});
  const requestsSlot = h('div', { class: 'access-requests' });
  const tableSlot = h('div', { class: 'access-table-slot' }, skeleton('table'));
  const formError = h('div', { class: 'field-error', role: 'alert' });
  const emailInput = textInput({
    value: '',
    placeholder: 'team-admin, viewer1, user@example.com, *@domain.com',
    mono: true,
    attrs: { 'aria-label': t('Account ID, email or domain'), autocomplete: 'off', spellcheck: 'false', maxlength: 2000, disabled: !editable || null },
  });
  const passwordInput = textInput({
    value: '',
    type: 'password',
    placeholder: t('Password (required for ID login, optional for Google OAuth)'),
    attrs: { 'aria-label': t('Password'), autocomplete: 'new-password', maxlength: 256, disabled: !editable || null },
  });
  let newRole = 'viewer';
  const roleSelect = selectInput([{ value: 'viewer', label: t('Viewer') }, { value: 'admin', label: t('Administrator') }], 'viewer', (v) => { newRole = v; }, { 'aria-label': t('Role'), disabled: !editable || null });
  const addBtn = button({ label: t('Add account'), icon: 'plus', variant: 'primary', disabled: !editable });
  const addForm = h('div', { class: 'access-add' },
    h('div', { class: 'access-add-row' },
      h('div', { class: 'access-add-email' }, emailInput),
      h('div', { class: 'access-add-password' }, passwordInput),
      roleSelect,
      addBtn),
    h('span', { class: 'field-help' }, t('Enter an account ID (e.g. team-admin, viewer1) with a password for direct ID/Password login, or enter a Google email / *@domain rule.')),
    formError);
  const c = card({
    title: inDrawer ? null : t('Accounts & passwords'),
    subtitle: inDrawer ? null : t('Manage ID/Password accounts (including the built-in admin account) and approved Google accounts. Administrators can change settings; viewers have read-only access.'),
    bordered: !inDrawer,
    body: h('div', { class: 'access-body' }, requestsSlot, editable ? addForm : null, tableSlot),
  });
  c.emailInput = emailInput;

  const apply = (data) => {
    renderRequests(data);
    renderTable(data);
  };

  const post = async (path, payload, okTitle, btn) => {
    if (btn) setBusy(btn, true);
    try {
      const res = await api.post(path, payload);
      toast({ title: okTitle, tone: 'success', timeout: 2600 });
      apply(res.data);
      return true;
    } catch (e) {
      if (btn) setBusy(btn, false);
      toast({ title: t('The change was not applied'), text: errorText(e), tone: 'danger' });
      return false;
    }
  };

  addBtn.addEventListener('click', async () => {
    formError.textContent = '';
    const members = parseMemberTokens(emailInput.value);
    const pw = passwordInput.value;
    const hasPw = Boolean(pw && pw.trim());
    if (!members.length) {
      formError.textContent = t('Enter an account ID, email address, or *@domain pattern.');
      emailInput.focus();
      return;
    }
    if (hasPw && pw.length < 4) {
      formError.textContent = t('Password must be at least 4 characters.');
      passwordInput.focus();
      return;
    }
    const invalid = members.find((m) => {
      if (m === '*' && (newRole === 'admin' || hasPw)) return true;
      if (m.startsWith('*@') && hasPw) return true;
      if (MEMBER_RE.test(m)) return false;
      if (hasPw && ACCOUNT_ID_RE.test(m)) return false;
      return true;
    });
    if (invalid) {
      const needsPw = ACCOUNT_ID_RE.test(invalid) && !MEMBER_RE.test(invalid) && !hasPw;
      formError.textContent = needsPw
        ? t('Enter a password (at least 4 characters) when creating a non-email account ID like "{id}".', { id: invalid })
        : t('Enter a valid account ID (with password), email address, or *@domain pattern.');
      if (needsPw) passwordInput.focus();
      else emailInput.focus();
      return;
    }
    const domainEntry = members.find((m) => m.startsWith('*@'));
    if (domainEntry) {
      const ok = await confirmDialog({ title: t('Approve an entire domain'), message: t('Every Google account in {domain} will be able to sign in as {role}. Continue?', { domain: domainEntry.slice(2), role: newRole === 'admin' ? t('Administrator') : t('Viewer') }), confirmLabel: t('Approve domain') });
      if (!ok) return;
    }
    const payload = members.length === 1 ? { email: members[0], role: newRole } : { emails: members, role: newRole };
    if (hasPw) payload.password = pw;
    const done = await post('/api/v1/access/users', payload, hasPw ? t('Account saved with password') : t('Account approved'), addBtn);
    if (done) { setBusy(addBtn, false); emailInput.value = ''; passwordInput.value = ''; }
  });
  emailInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') addBtn.click(); });
  passwordInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') addBtn.click(); });

  function renderRequests(data) {
    const list = (data && data.requests) || [];
    if (!list.length) { mount(requestsSlot); return; }
    mount(requestsSlot,
      h('div', { class: 'access-requests-head' }, icon('user-check', { size: 15 }), h('strong', null, t('Pending access requests')), badge(String(list.length), 'warning', { size: 'sm' })),
      h('ul', { class: 'access-request-list' }, list.map((r) => {
        const viewerBtn = button({ label: t('Approve as viewer'), size: 'sm', variant: 'primary', disabled: !editable });
        const adminBtn = button({ label: t('Approve as administrator'), size: 'sm', disabled: !editable });
        const dismissBtn = button({ label: t('Dismiss'), size: 'sm', variant: 'ghost', disabled: !editable });
        viewerBtn.addEventListener('click', () => post('/api/v1/access/users', { email: r.email, role: 'viewer' }, t('Account approved'), viewerBtn));
        adminBtn.addEventListener('click', async () => {
          const ok = await confirmDialog({ title: t('Grant administrator role'), message: t('{email} will be able to change settings and run governance actions. Continue?', { email: r.email }), confirmLabel: t('Approve') });
          if (ok) post('/api/v1/access/users', { email: r.email, role: 'admin' }, t('Account approved'), adminBtn);
        });
        dismissBtn.addEventListener('click', () => post('/api/v1/access/requests/dismiss', { email: r.email }, t('Request dismissed'), dismissBtn));
        return h('li', { class: 'access-request' },
          h('div', { class: 'user-cell' }, h('span', { class: 'avatar', 'aria-hidden': 'true' }, initials(r.email)),
            h('div', { class: 'cell-stack' },
              h('span', { class: ['cell-main', 'truncate'], title: r.email }, r.name ? `${r.name}` : r.email),
              h('span', { class: ['cell-sub', 'truncate'] }, r.name ? `${r.email} \u00b7 ${fmt.dateTime(r.requestedAt)}` : fmt.dateTime(r.requestedAt)))),
          h('div', { class: 'access-request-actions' }, viewerBtn, adminBtn, dismissBtn));
      })));
  }

  function renderTable(data) {
    const rows = (data && data.users) || [];
    mount(tableSlot, dataTable({
      rows,
      pageSize: 20,
      searchKeys: ['email'],
      emptyText: t('No approved accounts yet.'),
      columns: [
        {
          key: 'email', label: t('Account'),
          render: (u) => h('div', { class: 'user-cell' },
            u.pattern ? h('span', { class: 'avatar muted', 'aria-hidden': 'true' }, icon('globe', { size: 14 })) : h('span', { class: 'avatar', 'aria-hidden': 'true' }, initials(u.email)),
            h('div', { class: 'cell-stack' },
              h('span', { class: ['cell-main', 'truncate'], title: u.email },
                u.email === '*' ? `* (${t('Every Google account (Signed-in Viewer)')})` : u.email,
                u.self ? h('span', { class: 'access-you' }, t('You')) : null),
              u.pattern
                ? h('span', { class: 'cell-sub' }, u.email === '*' ? t('Every Google account (Signed-in Viewer)') : t('Every account in this domain'))
                : h('span', { class: 'cell-sub' },
                  u.defaultPassword
                    ? t('ID / Password login (default password: admin)')
                    : (u.hasPassword ? t('ID / Password login enabled') : t('Google OAuth sign-in'))))),
        },
        {
          key: 'role', label: t('Role'), width: '124px',
          render: (u) => {
            if (!editable || !u.canChangeRole) return u.role === 'admin' ? badge(t('Administrator'), 'accent', { size: 'sm' }) : badge(t('Viewer'), 'neutral', { size: 'sm' });
            return selectInput([{ value: 'viewer', label: t('Viewer') }, { value: 'admin', label: t('Administrator') }], u.role, async (v) => {
              if (v === 'admin') {
                const ok = await confirmDialog({ title: t('Grant administrator role'), message: t('{email} will be able to change settings and run governance actions. Continue?', { email: u.email }), confirmLabel: t('Approve') });
                if (!ok) { renderTable(data); return; }
              }
              const done = await post('/api/v1/access/users', { email: u.email, role: v }, t('Role updated'));
              if (!done) renderTable(data);
            }, { 'aria-label': t('Role for {email}', { email: u.email }) });
          },
        },
        {
          key: 'source', label: t('Auth / Source'), width: '140px',
          render: (u) => {
            if (u.source === 'builtin') {
              return badge(u.defaultPassword ? t('Default (admin/admin)') : t('Built-in Admin'), u.defaultPassword ? 'warning' : 'accent', { size: 'sm' });
            }
            if (u.hasPassword) {
              return badge(t('ID / Password'), 'success', { size: 'sm' });
            }
            return u.source === 'config'
              ? badge(t('Deployment config'), 'neutral', { size: 'sm', title: t('Defined in config.yaml or environment variables') })
              : badge(t('Google OAuth'), 'info', { size: 'sm' });
          },
        },
        {
          key: 'lastLogin', label: t('Last sign-in'), width: '130px',
          render: (u) => (u.pattern ? h('span', { class: 'text-muted' }, DASH) : (u.lastLogin ? fmt.dateTime(u.lastLogin) : h('span', { class: 'text-muted' }, t('Never')))),
        },
        {
          key: 'actions', label: '', actions: true, sortable: false, width: '185px', align: 'right',
          render: (u) => {
            if (!editable) return null;
            const btns = [];
            if (!u.pattern) {
              const pwBtn = button({
                label: u.hasPassword ? t('Change password') : t('Set password'),
                size: 'sm',
                variant: u.defaultPassword ? 'primary' : 'secondary',
                icon: 'lock',
              });
              pwBtn.addEventListener('click', () => openPasswordModal(u, (updated) => apply(updated)));
              btns.push(pwBtn);
            }
            if (u.canRemove) {
              const delBtn = button({ label: t('Remove'), size: 'sm', variant: 'ghost', icon: 'trash' });
              delBtn.addEventListener('click', async () => {
                const ok = await confirmDialog({ title: t('Remove access'), message: u.pattern ? t('Accounts in {domain} will no longer be able to sign in unless they are approved individually.', { domain: u.email.slice(2) }) : t('{email} will be signed out immediately and will no longer be able to sign in.', { email: u.email }), confirmLabel: t('Remove'), tone: 'danger' });
                if (ok) post('/api/v1/access/users/delete', { email: u.email }, t('Access removed'), delBtn);
              });
              btns.push(delBtn);
            }
            return btns.length ? h('div', { class: 'row', style: { justifyContent: 'flex-end', gap: '6px' } }, btns) : null;
          },
        },
      ],
    }));
  }

  const load = async () => {
    try {
      const res = await api.get('/api/v1/access', null, signal ? { signal } : undefined);
      if (!signal || !signal.aborted) apply(res.data);
    } catch (err) {
      if (isAbort(err) || (signal && signal.aborted)) return;
      mount(tableSlot, errorState(err, { onRetry: () => { mount(tableSlot, skeleton('table')); load(); rerender(); }, compact: true }));
    }
  };
  load();
  return c;
}

export function openAccessDrawer({ onChanged } = {}) {
  if (!requireAdmin(t('Add account / Manage access control'))) return;
  const controller = new AbortController();
  const block = accessMembers({ signal: controller.signal, inDrawer: true, rerender: () => { if (onChanged) onChanged(); } }, isAdmin());
  const d = openDrawer({
    title: t('Accounts & passwords'),
    subtitle: t('Create ID/Password accounts, change passwords (including admin), or pre-register Google accounts.'),
    content: block,
    width: 920,
  });
  d.dialog.addEventListener('close', () => {
    controller.abort();
    if (onChanged) onChanged();
  });
  setTimeout(() => { if (block.emailInput) block.emailInput.focus(); }, 60);
}

function about(ctx, panel, data) {
  const b = store.boot;
  const p = data.persistence || {};
  panel.append(card({
    title: b.product.name,
    subtitle: b.product.tagline,
    body: kv([
      [t('Version'), h('span', { class: 'mono' }, b.product.version || DASH)],
      [t('Licensed to'), b.branding.organizationName || DASH],
      [t('Workspace project'), h('span', { class: 'mono' }, b.workspace.projectId || DASH)],
      [t('Cloud Run service'), b.deployment && b.deployment.service ? h('span', { class: 'mono' }, b.deployment.service) : t('Not running on Cloud Run')],
      [t('Revision'), b.deployment && b.deployment.revision ? h('span', { class: 'mono' }, b.deployment.revision) : null],
      [t('Settings storage'), `${p.backend || DASH}${p.location ? ` \u00b7 ${p.location}` : ''}`],
      [t('Durable'), p.durable ? badge(t('Yes'), 'success', { size: 'sm' }) : badge(t('No'), 'warning', { size: 'sm' })],
      [t('Last change'), p.updatedAt ? `${fmt.dateTime(p.updatedAt)}${p.updatedBy ? ` \u00b7 ${p.updatedBy}` : ''}` : null],
    ]),
  }));
  panel.append(card({
    title: t('Data handling'),
    body: h('div', { class: 'stack-sm muted' },
      h('p', null, t('The control plane reads data in place from your Google Cloud project. It does not copy logs or billing data to any external system.')),
      h('p', null, t('All queries run with the deployment service account and are restricted to read-only access, except explicitly confirmed governance actions on project IAM.')),
      h('p', null, t('Administrative actions are written to Cloud Logging (agy-admin admin_actions) for audit.'))),
  }));
}
