/**
 * Settings: white-label branding, workspace data sources, module toggles, model catalog,
 * AI engine, data collection and cost (telemetry discovery, Gemini analysis, field detection hints,
 * BigQuery routing cost controls, official prices and exchange rates), governance policy,
 * access control and deployment information.
 */
import { h, mount, append } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t, getLocale } from '../core/i18n.js';
import { fmt, isNum, DASH, initials } from '../core/format.js';
import { api, isAbort } from '../core/api.js';
import { store, isAdmin, session, isModuleEnabled } from '../core/state.js';
import {
  pageHeader, card, button, banner, badge, statusBadge, switchInput, selectInput, field, textInput, toast, setBusy, kv, chip,
  errorState, skeleton, confirmDialog, emptyState, dataTable, openDrawer, alertPermissionRequired, requireAdmin,
} from '../core/components.js';
import { errorText, serverText } from '../core/messages.js';
import { fxRateText, fxSourceLabel, pricingSyncText } from './finops.js';

const TABS = [
  ['general', 'General', 'grid'],
  ['access', 'Access control', 'lock'],
  ['workspace', 'Workspace and data sources', 'database'],
  ['modules', 'Modules', 'power'],
  ['models', 'Model catalog', 'layers'],
  ['ai', 'AI engine', 'spark'],
  ['data-collection', 'Data collection and cost', 'gauge'],
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

/** One item per line, for values that may contain commas (URLs, Logging filters, display names). */
function textToLines(v) {
  return String(v || '').split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
}

/** Single words separated by commas, spaces or new lines. */
function textToWords(v) {
  return String(v || '').split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean);
}

const MODEL_KEY_RE = /^[a-z0-9][a-z0-9.\-_]{0,120}$/;
const MODEL_ID_RE = /^[a-z0-9][a-z0-9.\-]{1,80}$/;
const HINT_RE = /^[a-z0-9_.\-]{1,40}$/;
const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
/** Cloud Logging LogSeverity values, lowest first. */
const SEVERITIES = ['DEFAULT', 'DEBUG', 'INFO', 'NOTICE', 'WARNING', 'ERROR', 'CRITICAL', 'ALERT', 'EMERGENCY'];

function isHttpsUrl(v) {
  try {
    const u = new URL(String(v));
    return u.protocol === 'https:' && u.hostname.includes('.') && !u.username && !u.password;
  } catch (e) {
    return false;
  }
}

function isGoogleDocUrl(v) {
  return isHttpsUrl(v) && /(^|\.)google\.(com|dev)$/i.test(new URL(String(v)).hostname);
}

function isPair(v) {
  const parts = String(v).split('=');
  return parts.length === 2 && parts.every((p) => p.trim().length > 0 && p.trim().length <= 120);
}

function isAlias(v) {
  return isPair(v) && String(v).split('=').every((p) => MODEL_KEY_RE.test(p.trim().toLowerCase()));
}

function isHint(v) {
  return HINT_RE.test(String(v).trim().toLowerCase());
}

function intlValues(kind) {
  try {
    return typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf(kind) : [];
  } catch (e) {
    return [];
  }
}

/** Every IANA time zone the browser knows, UTC first. Values in use stay selectable even when the browser lacks them. */
function timezoneChoices(...keep) {
  const zones = intlValues('timeZone');
  const known = new Set(zones);
  const extra = keep.filter((z) => z && z !== 'UTC' && !known.has(z));
  return [...new Set(['UTC', ...extra, ...zones])];
}

/** Every ISO 4217 currency the browser knows, labelled with its name in the active language. */
function currencyChoices(...keep) {
  let names = null;
  try { names = new Intl.DisplayNames([getLocale() === 'ko' ? 'ko' : 'en'], { type: 'currency' }); } catch (e) { names = null; }
  const codes = [...new Set([...keep.filter((c) => /^[A-Z]{3}$/.test(c || '')), ...intlValues('currency')])].sort();
  return codes.map((c) => {
    let name = '';
    try { name = names ? names.of(c) : ''; } catch (e) { name = ''; }
    return { value: c, label: name && name !== c ? `${c} \u00b7 ${name}` : c };
  });
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
  const view = { general, workspace, modules, models, ai, 'data-collection': dataCollection, governance, access, about }[tab];
  view(ctx, panel, data, editable);
  return null;
}

/* ------------------------------------------------------------------ form helper */

/**
 * Build a settings card for one section.
 * fields: [{ path: ['key'] | ['billing','dataset'], label, help, type, options, mono, min, max, step }] or { heading }.
 * Types: text (default; optional suggestions and check), number (integer, range), switch, select, list (comma
 * separated), words (comma separated words in a text area), lines (one item per line, for values that may contain
 * commas), textarea, time, timezone, currency, prices (administrator model prices), color.
 * resetKeys limits the reset button and the override check to part of a section (several cards share a section);
 * showDefaults adds the deployment default (data.defaults) to the help text of single-value fields.
 */
function sectionForm(ctx, { title, subtitle, section, data, fields, editable, extraActions, sourceObj, resetKeys, showDefaults, intro, cls }) {
  const scoped = (doc) => (section === 'governance' ? getPath(doc, ['governance', 'antigravity']) : getPath(doc, [section])) || {};
  const effective = sourceObj || scoped(data.effective);
  const overrides = scoped(data.overrides);
  const defaults = showDefaults && data.defaults ? scoped(data.defaults) : null;
  const values = {};
  const err = h('div', { class: 'field-error', role: 'alert' });
  const entries = [];
  const controls = fields.map((f) => {
    if (f.heading) return h('div', { class: 'form-subhead' }, f.heading);
    const cur = getPath(effective, f.path);
    const overridden = getPath(overrides, f.path) !== undefined;
    let control;
    let check = null;
    let focusInvalid = null;
    let onMounted = null;
    const set = (v) => setPath(values, f.path, v);
    switch (f.type) {
      case 'switch':
        control = h('div', { class: 'row' }, switchInput({ checked: Boolean(cur), disabled: !editable, label: f.label, onChange: (v) => set(v) }));
        break;
      case 'select':
        control = selectInput(f.options, cur === undefined || cur === null ? '' : String(cur), (v) => set(f.numeric ? Number(v) : v), { disabled: !editable || null, 'aria-label': f.label });
        break;
      case 'number': {
        const input = textInput({ value: isNum(cur) ? String(cur) : '', type: 'number', attrs: { min: f.min, max: f.max, step: f.step || 1, disabled: !editable || null }, onInput: (v) => set(v === '' ? null : Number(v)) });
        check = () => numberError(input.value, f);
        control = input;
        break;
      }
      case 'list': {
        const input = textInput({ value: listToText(cur), mono: f.mono, attrs: { disabled: !editable || null, placeholder: f.placeholder || '' }, onInput: (v) => set(textToList(v)) });
        if (f.itemCheck) check = () => listError(textToList(input.value), f);
        control = input;
        break;
      }
      case 'textarea': {
        const ta = h('textarea', { class: ['textarea', f.mono ? 'mono' : ''], rows: f.rows || 3, disabled: !editable || null, placeholder: f.placeholder || '' });
        ta.value = Array.isArray(cur) ? cur.join('\n') : (cur || '');
        ta.addEventListener('input', () => set(f.asList ? textToList(ta.value) : ta.value));
        control = ta;
        break;
      }
      case 'lines':
      case 'words': {
        const words = f.type === 'words';
        const parse = (text) => (words ? textToWords(text) : textToLines(text));
        const ta = h('textarea', { class: ['textarea', f.mono ? 'mono' : ''], rows: f.rows || 3, disabled: !editable || null, placeholder: f.placeholder || '', spellcheck: 'false' });
        ta.value = Array.isArray(cur) ? cur.join(words ? ', ' : '\n') : (cur || '');
        ta.addEventListener('input', () => set(parse(ta.value)));
        if (f.itemCheck) check = () => listError(parse(ta.value), f);
        if (f.appendLine && editable) {
          onMounted = () => {
            const lines = textToLines(ta.value);
            const key = f.appendLine.split('=')[0];
            if (!lines.some((l) => l.split('=')[0].trim().toLowerCase() === key)) {
              ta.value = [...lines, f.appendLine].join('\n');
              set(parse(ta.value));
            }
            ta.scrollIntoView({ block: 'center' });
            ta.focus();
            ta.setSelectionRange(ta.value.length, ta.value.length);
          };
        }
        control = ta;
        break;
      }
      case 'time': {
        const input = textInput({ value: cur || '', type: 'time', attrs: { step: 60, disabled: !editable || null }, onInput: (v) => set(v) });
        check = () => (TIME_RE.test(input.value) ? '' : t('Enter a time such as 00:00.'));
        control = input;
        break;
      }
      case 'timezone': {
        if (!intlValues('timeZone').length) {
          const input = textInput({ value: cur || '', mono: true, attrs: { disabled: !editable || null, placeholder: f.emptyLabel || 'Area/City', maxlength: 64 }, onInput: (v) => set(v.trim()) });
          control = input;
          break;
        }
        const zones = timezoneChoices(cur, ...(f.keep || []));
        const opts = (f.emptyLabel ? [{ value: '', label: f.emptyLabel }] : []).concat(zones.map((z) => ({ value: z, label: z })));
        control = selectInput(opts, cur || '', (v) => set(v), { disabled: !editable || null, 'aria-label': f.label });
        break;
      }
      case 'currency': {
        if (!intlValues('currency').length) {
          const input = textInput({ value: cur || '', mono: true, attrs: { disabled: !editable || null, placeholder: f.emptyLabel || 'USD', maxlength: 3 }, onInput: (v) => set(v.trim().toUpperCase()) });
          check = () => (/^([A-Za-z]{3})?$/.test(input.value.trim()) ? '' : t('Enter a three-letter currency code such as USD, or leave it empty.'));
          control = input;
          break;
        }
        const opts = (f.emptyLabel ? [{ value: '', label: f.emptyLabel }] : []).concat(currencyChoices(cur));
        control = selectInput(opts, cur || '', (v) => set(v), { disabled: !editable || null, 'aria-label': f.label });
        break;
      }
      case 'prices': {
        const editor = priceEditor({ rows: Array.isArray(cur) ? cur : [], editable, onChange: (list) => set(list) });
        check = () => editor.validate();
        focusInvalid = () => editor.focusInvalid();
        if (f.focusModel && editable) onMounted = () => editor.reveal(f.focusModel);
        control = editor.el;
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
      default: {
        const input = textInput({ value: cur === undefined || cur === null ? '' : String(cur), mono: f.mono, attrs: { disabled: !editable || null, placeholder: f.placeholder || '', maxlength: f.maxlength || 512, spellcheck: f.mono ? 'false' : null }, onInput: (v) => set(v) });
        if (f.check) check = () => f.check(input.value.trim());
        if (f.suggestions && f.suggestions.length) {
          const listId = `dl-${f.path.join('-')}-${Math.random().toString(36).slice(2, 8)}`;
          input.setAttribute('list', listId);
          control = h('div', null, input, h('datalist', { id: listId }, f.suggestions.map((s) => h('option', { value: s }))));
        } else {
          control = input;
        }
      }
    }
    const def = defaults ? defaultText(f, getPath(defaults, f.path)) : null;
    const help = def ? [f.help || null, h('span', { class: 'field-default' }, t('Default: {value}', { value: def }))] : f.help;
    const label = h('span', { class: 'row' }, f.label, overridden ? h('span', { class: 'override-tag', title: t('Changed from the deployment default') }, t('Customized')) : null);
    const el = field({ label, help, control });
    const fieldErr = h('span', { class: 'field-error', role: 'alert' });
    el.append(fieldErr);
    if (f.wide) el.classList.add('field-wide');
    const focus = focusInvalid || (() => {
      const target = control.matches('input, select, textarea') ? control : control.querySelector('input, select, textarea');
      if (target) target.focus();
    });
    entries.push({ f, check, fieldErr, el, focus });
    if (onMounted) requestAnimationFrame(onMounted);
    return el;
  });
  const save = button({ label: t('Save changes'), variant: 'primary' });
  const hasOverrides = resetKeys
    ? resetKeys.some((k) => getPath(overrides, k.split('.')) !== undefined)
    : Object.keys(overrides).length > 0;
  const reset = hasOverrides && editable ? button({ label: t('Reset to defaults'), variant: 'ghost', icon: 'refresh' }) : null;
  save.addEventListener('click', async () => {
    if (!editable) {
      alertPermissionRequired({ action: `${t('Save changes')}: ${title}` });
      return;
    }
    err.textContent = '';
    entries.forEach((e) => { e.fieldErr.textContent = ''; });
    if (!Object.keys(values).length) { toast({ title: t('No changes to save'), tone: 'info', timeout: 2500 }); return; }
    const invalid = [];
    entries.forEach((e) => {
      if (!e.check || getPath(values, e.f.path) === undefined) return;
      const msg = e.check();
      if (msg) {
        e.fieldErr.textContent = msg;
        invalid.push(e);
      }
    });
    if (invalid.length) {
      err.textContent = t('Correct the highlighted fields before saving.');
      invalid[0].el.scrollIntoView({ block: 'center' });
      invalid[0].focus();
      return;
    }
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
      // Server validation names the key ("Invalid value for routing.sample_percent: ..."): show it at that field.
      const m = /^Invalid value for (\S+): /.exec((e && e.message) || '');
      const target = m ? entries.find((x) => x.f.path.join('.') === m[1]) : null;
      if (target) {
        target.fieldErr.textContent = errorText(e);
        target.el.scrollIntoView({ block: 'center' });
      } else {
        err.textContent = errorText(e);
      }
    }
  });
  if (reset) {
    reset.addEventListener('click', async () => {
      const ok = await confirmDialog({ title: t('Reset section'), message: t('Customized values in this section will revert to the deployment defaults from config.yaml and environment variables.'), confirmLabel: t('Reset'), tone: 'danger' });
      if (!ok) return;
      try {
        await api.post('/api/v1/settings/reset', { section, keys: resetKeys || [] });
        toast({ title: t('Section reset to defaults'), tone: 'success' });
        await afterSave(ctx);
      } catch (e) {
        toast({ title: t('Reset failed'), text: errorText(e), tone: 'danger' });
      }
    });
  }
  const c = card({ title, subtitle, bordered: true, cls, body: h('div', { class: 'form-grid' }, intro || null, h('div', { class: 'field-row' }, controls), err) });
  c.bodyEl.classList.add('flush');
  c.append(h('div', { class: 'form-actions' }, extraActions || null, h('span', { class: 'spacer' }), reset, save));
  return c;
}

/** Deployment default of a single-value field as display text; null for lists and tables. */
function defaultText(f, v) {
  if (v === undefined || v === null || ['list', 'words', 'lines', 'textarea', 'prices', 'color'].includes(f.type)) return null;
  if (f.type === 'switch') return v ? t('On') : t('Off');
  if (v === '') return f.emptyLabel || null;
  if (f.type === 'select') {
    const opt = (f.options || []).find((o) => String(o.value) === String(v));
    return opt ? opt.label : String(v);
  }
  if (f.type === 'number') return isNum(v) ? fmt.num(v, 6) : String(v);
  return String(v);
}

function numberError(raw, f) {
  const text = String(raw === undefined || raw === null ? '' : raw).trim();
  const v = Number(text);
  if (!text || !Number.isFinite(v)) return t('Enter a number.');
  if (f.integer && !Number.isInteger(v)) return t('Enter a whole number.');
  if (f.range) {
    const lo = isNum(f.min) ? f.min : null;
    const hi = isNum(f.max) ? f.max : null;
    if ((lo !== null && v < lo) || (hi !== null && v > hi)) {
      if (lo !== null && hi !== null) return t('Enter a value from {min} to {max}.', { min: fmt.num(lo, 4), max: fmt.num(hi, 4) });
      return lo !== null ? t('Enter a value of at least {min}.', { min: fmt.num(lo, 4) }) : t('Enter a value of at most {max}.', { max: fmt.num(hi, 4) });
    }
  }
  return '';
}

function listError(items, f) {
  if (f.maxItems && items.length > f.maxItems) return t('Enter at most {n} items.', { n: f.maxItems });
  const bad = items.find((x) => !f.itemCheck(x));
  if (bad === undefined) return '';
  return f.itemError ? f.itemError(bad) : t('"{value}" is not valid here.', { value: bad });
}

/**
 * Editor for pricing.model_overrides: one row per model with input, output and cached input prices in USD per
 * 1M tokens. Values are read back exactly as stored; an empty cached price is sent as null (the input price is used).
 */
function priceEditor({ rows, editable, onChange }) {
  const items = [];
  let invalidTarget = null;
  const list = h('div', { class: 'price-rows' });
  const head = h('div', { class: ['price-row', 'price-head'], 'aria-hidden': 'true' },
    h('span', null, t('Model ID')), h('span', null, t('Input')), h('span', null, t('Output')), h('span', null, t('Cached input')), h('span'));
  const empty = h('div', { class: 'price-empty' }, t('No administrator prices. Official prices are used for every model.'));
  const read = (input) => (input.value.trim() === '' ? null : Number(input.value));
  const rowValue = (r) => ({ model: r.model.value.trim().toLowerCase(), input: read(r.input), output: read(r.output), cached: read(r.cached) });
  const isBlank = (x) => !x.model && x.input === null && x.output === null && x.cached === null;
  const collect = () => items.map(rowValue).filter((x) => !isBlank(x));
  const emit = () => onChange(collect());
  const sync = () => {
    empty.hidden = items.length > 0;
    head.hidden = items.length === 0;
  };
  const price = (v, label, placeholder) => textInput({ value: isNum(v) ? String(v) : '', type: 'number', attrs: { min: 0, step: 'any', inputmode: 'decimal', 'aria-label': label, placeholder: placeholder || '', disabled: !editable || null } });
  const addRow = (item = {}) => {
    const row = {
      model: textInput({ value: item.model || '', mono: true, attrs: { 'aria-label': t('Model ID'), placeholder: t('Model ID'), maxlength: 121, spellcheck: 'false', autocomplete: 'off', disabled: !editable || null } }),
      input: price(item.input, t('Input price in USD per 1M tokens')),
      output: price(item.output, t('Output price in USD per 1M tokens')),
      cached: price(item.cached, t('Cached input price in USD per 1M tokens'), t('Same as input')),
    };
    const remove = editable ? button({ icon: 'trash', variant: 'ghost', size: 'sm', title: t('Remove') }) : h('span');
    row.el = h('div', { class: 'price-row' }, row.model, row.input, row.output, row.cached, remove);
    if (editable) {
      remove.addEventListener('click', () => {
        items.splice(items.indexOf(row), 1);
        row.el.remove();
        sync();
        emit();
      });
    }
    [row.model, row.input, row.output, row.cached].forEach((x) => x.addEventListener('input', () => { row.el.classList.remove('invalid'); emit(); }));
    items.push(row);
    list.append(row.el);
    sync();
    return row;
  };
  const validate = () => {
    invalidTarget = null;
    items.forEach((r) => r.el.classList.remove('invalid'));
    const seen = new Set();
    let count = 0;
    for (let i = 0; i < items.length; i += 1) {
      const r = items[i];
      const x = rowValue(r);
      if (isBlank(x)) continue;
      count += 1;
      const n = i + 1;
      const fail = (msg, target) => {
        r.el.classList.add('invalid');
        invalidTarget = target;
        return msg;
      };
      if (!MODEL_KEY_RE.test(x.model)) return fail(t('Row {n}: enter a model ID of lowercase letters, digits, ".", "-" or "_".', { n }), r.model);
      if (seen.has(x.model)) return fail(t('Row {n}: {model} is listed more than once.', { n, model: x.model }), r.model);
      seen.add(x.model);
      if (x.input === null || x.output === null) return fail(t('Row {n}: enter the input and output prices.', { n }), x.input === null ? r.input : r.output);
      if ([x.input, x.output, x.cached].some((v) => v !== null && (!Number.isFinite(v) || v < 0 || v > 1e6))) {
        return fail(t('Row {n}: prices must be between 0 and 1,000,000.', { n }), r.input);
      }
    }
    return count > 100 ? t('Enter at most {n} models.', { n: 100 }) : '';
  };
  (rows || []).forEach((item) => addRow(item));
  sync();
  const addBtn = editable ? button({ label: t('Add model price'), icon: 'plus', size: 'sm', onClick: () => addRow().model.focus() }) : null;
  return {
    el: h('div', { class: 'price-editor' }, head, list, empty, addBtn ? h('div', null, addBtn) : null),
    validate,
    focusInvalid: () => { if (invalidTarget) invalidTarget.focus(); },
    /** Shows the row of a model (added when missing, for links from Cost & FinOps) and focuses its input price. */
    reveal: (model) => {
      const existing = items.find((r) => r.model.value.trim().toLowerCase() === model);
      const row = existing || addRow({ model });
      if (!existing) emit();
      row.el.scrollIntoView({ block: 'center' });
      row.input.focus();
    },
  };
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
  const wsBoot = (store.boot && store.boot.workspace) || {};
  const activeProjects = wsBoot.projects || [];
  const projectDetails = Array.isArray(wsBoot.projectDetails) && wsBoot.projectDetails.length
    ? wsBoot.projectDetails
    : activeProjects.map((p) => ({
      id: p,
      name: p,
      parent: null,
      sameOrg: null,
      source: p === getPath(data.effective, ['workspace', 'project_id']) ? 'primary' : 'configured',
    }));
  if (projectDetails.length > 0) {
    const sourceBadge = (s) => {
      if (s === 'primary') return badge(t('Primary'), 'info', { size: 'sm' });
      if (s === 'configured') return badge(t('Explicitly linked'), 'accent', { size: 'sm' });
      return badge(t('Auto-discovered'), 'success', { size: 'sm' });
    };
    panel.append(card({
      title: t('Scanned Google Cloud projects ({n})', { n: projectDetails.length }),
      subtitle: t('Unified multi-project scanning across Agent Registry, Deployments, Gemini Enterprise, BigQuery audit sinks, Cloud Monitoring, Cloud Logging and FinOps.'),
      flush: true,
      body: h('div', null,
        dataTable({
          columns: [
            { key: 'id', label: t('Project ID'), render: (r) => h('span', { class: 'cell-mono fw-600' }, r.id) },
            { key: 'name', label: t('Display name'), render: (r) => r.name || r.id },
            { key: 'source', label: t('Grouping criteria'), render: (r) => sourceBadge(r.source) },
            {
              key: 'parent', label: t('Organization / Folder'),
              render: (r) => h('div', { class: 'row wrap' },
                h('span', { class: 'cell-mono muted' }, r.parent || DASH),
                r.sameOrg ? badge(t('Same organization'), 'neutral', { size: 'sm' }) : null),
            },
          ],
          rows: projectDetails,
          searchable: projectDetails.length > 8,
          pageSize: 10,
          initialSort: { key: 'source', dir: 'asc' },
        }),
        h('div', { class: 'form-actions' },
          h('span', { class: 'muted' }, t('Grouping rules: 1) Primary host project, 2) Explicitly linked projects below, 3) Active projects auto-discovered via Cloud Resource Manager (excluding auth bridge projects). Grant cross-project read IAM via scripts/deploy.sh --project {id} --link-projects projA,projB.', { id: getPath(data.effective, ['workspace', 'project_id']) || 'PROJECT_ID' })))),
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
      { path: ['timezone'], label: t('Reporting timezone'), type: 'timezone' },
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
      { path: ['fallback_models'], label: t('Fallback models'), type: 'list', mono: true, itemCheck: (v) => MODEL_ID_RE.test(v), itemError: (v) => t('"{value}" is not a valid model ID.', { value: v }), help: t('Comma separated model IDs tried in order when a requested model is not available in this project, for example a preview model.') },
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
  panel.append(sectionForm(ctx, {
    title: t('Antigravity cap policy'), subtitle: t('Defaults for cap evaluation and enforcement cycles.'), section: 'governance', data, editable,
    fields: [
      { path: ['enforcement_mode'], label: t('Enforcement mode'), type: 'select', options: [{ value: 'manual', label: t('Manual review') }, { value: 'auto', label: t('Automatic suspension') }], help: t('Automatic suspends users over their cap until the period resets; manual waits for an administrator decision.') },
      { path: ['token_cap'], label: t('Token cap per user'), type: 'number', min: 0, step: 1000, help: t('0 disables the token cap.') },
      { path: ['request_cap'], label: t('Request cap per user'), type: 'number', min: 0, step: 10, help: t('0 disables the request cap.') },
      { path: ['cost_cap'], label: t('Cost cap per person'), type: 'number', min: 0, step: 'any', help: t('Amount in the currency below. 0 disables it. Individual amounts are set on the Antigravity Controls page.') },
      { path: ['currency'], label: t('Currency'), type: 'currency', emptyLabel: t('Billing account currency'), help: t('Cost caps and estimates are shown in this currency') },
      { path: ['pricing_source'], label: t('Token prices'), type: 'select', options: [{ value: 'auto', label: t('Automatic (recommended)') }, { value: 'manual', label: t('Enter prices') }], help: t('Automatic uses what your billing account paid per token, then the public Google price list.') },
      { path: ['input_price_per_million'], label: t('Input price per 1M tokens'), type: 'number', min: 0, step: 0.01, help: t('Used only when token prices are entered') },
      { path: ['output_price_per_million'], label: t('Output price per 1M tokens'), type: 'number', min: 0, step: 0.01, help: t('Used only when token prices are entered') },
      { path: ['period'], label: t('Settlement period'), type: 'select', options: [['daily', 'Daily'], ['weekly', 'Weekly'], ['monthly', 'Monthly'], ['all', 'Last 90 days']].map(([v, l]) => ({ value: v, label: t(l) })) },
      { path: ['window'], label: t('Window'), type: 'select', options: [{ value: 'calendar', label: t('Calendar (fixed reset)') }, { value: 'rolling', label: t('Rolling') }] },
      { path: ['reset_timezone'], label: t('Reset time zone'), type: 'timezone', keep: [workspaceTz], help: t('Calendar periods reset at local midnight') },
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

/* ------------------------------------------------------------------ data collection and cost */

/** Last and next automatic price check and the synced exchange rates, from the bootstrap pricing summary. */
function pricingStatus() {
  const pa = (store.boot && store.boot.pricingAlerts) || {};
  const sched = pa.schedule || {};
  const rates = Object.entries((pa.fx && pa.fx.rates) || {}).filter(([, r]) => r && isNum(r.rate));
  const parts = [pa.syncedAt ? t('Last checked {when}', { when: fmt.dateTime(pa.syncedAt) }) : t('Not checked yet')];
  if (sched.time) parts.push(t('Checked daily at {time} ({timezone})', { time: sched.time, timezone: sched.timezone || DASH }));
  if (sched.nextRunAt) parts.push(t('Next check {when}', { when: fmt.relative(sched.nextRunAt) }));
  return h('div', { class: 'form-status' },
    h('span', { class: 'form-status-text' }, icon('clock', { size: 14 }), h('span', null, parts.join(' \u00b7 '))),
    rates.length ? h('div', { class: 'chips' }, rates.map(([cur, r]) => chip(t('1 USD = {rate} {currency}', { rate: fxRateText(r.rate), currency: cur }), { title: fxSourceLabel(r.source) }))) : null);
}

function dataCollection(ctx, panel, data, editable) {
  const eff = data.effective || {};
  const wsTz = getPath(eff, ['workspace', 'timezone']) || 'UTC';
  const capCurrency = getPath(eff, ['governance', 'antigravity', 'currency']) || '';
  const aiDefault = getPath(eff, ['ai_engine', 'default_model']) || '';
  const aiModels = getPath(eff, ['ai_engine', 'models']) || [];
  const finopsOn = isModuleEnabled('finops');
  const telemetryOn = isModuleEnabled('telemetry');
  // Links from Cost & FinOps: ?price=<model> opens a price row, ?alias=<model> starts an alias line.
  const query = ctx.query || new URLSearchParams();
  const linked = (key) => {
    const v = String(query.get(key) || '').trim().toLowerCase();
    return editable && MODEL_KEY_RE.test(v) ? v : null;
  };
  const priceModel = linked('price');
  const aliasModel = linked('alias');
  if (query.has('price') || query.has('alias')) window.history.replaceState(null, '', '#/settings/data-collection');

  const pricingActions = [];
  if (finopsOn && editable) {
    const syncBtn = button({ label: t('Check prices now'), icon: 'refresh', title: t('Reads official prices and exchange rates with the saved settings.') });
    syncBtn.addEventListener('click', async () => {
      setBusy(syncBtn, true, t('Checking'));
      try {
        const p = (await api.post('/api/v1/finops/pricing/sync', {})).data || {};
        toast({ title: t('Prices and exchange rates synced'), text: pricingSyncText(p, p.displayCurrency), tone: (p.newAlertsCount || 0) > 0 ? 'warning' : 'success', timeout: 4000 });
        await afterSave(ctx);
      } catch (e) {
        setBusy(syncBtn, false);
        toast({ title: t('Sync failed'), text: errorText(e), tone: 'danger' });
      }
    });
    pricingActions.push(syncBtn);
  }
  if (finopsOn) pricingActions.push(button({ label: t('Cost & FinOps'), icon: 'card', variant: 'ghost', onClick: () => ctx.navigate('#/finops') }));
  const httpsError = (v) => t('"{value}" is not an https address.', { value: v });
  panel.append(sectionForm(ctx, {
    title: t('Prices and exchange rates'),
    subtitle: t('Official token prices and exchange rates for cost estimates when the billing export cannot be read. Nothing is built in: prices come from the sources below.'),
    section: 'pricing', data, editable, showDefaults: true,
    intro: pricingStatus(),
    extraActions: pricingActions.length ? h('div', { class: 'row wrap' }, pricingActions) : null,
    fields: [
      { heading: t('Currency and schedule') },
      { path: ['currency'], label: t('Display currency'), type: 'currency', emptyLabel: t('Automatic ({currency})', { currency: capCurrency || 'USD' }), help: t('Currency of cost estimates and price tables, converted from USD with the synced exchange rate. Automatic uses the Antigravity cost cap currency, otherwise USD.') },
      { path: ['sync_time'], label: t('Daily check time'), type: 'time', help: t('Local time of the automatic price and exchange rate check.') },
      { path: ['sync_timezone'], label: t('Check timezone'), type: 'timezone', emptyLabel: t('Workspace timezone ({tz})', { tz: wsTz }), help: t('Empty = workspace timezone.') },
      { heading: t('Official sources') },
      { path: ['catalog_services'], label: t('Cloud Billing Catalog services'), type: 'lines', rows: 3, help: t('Display names of the services whose list prices are read, one per line.') },
      { path: ['include_partner_models'], label: t('Include partner models'), type: 'switch', help: t('Also read list prices of partner models that are billed under their own Cloud Billing service.') },
      { path: ['storage_service'], label: t('Storage price service'), help: t('Cloud Billing Catalog service whose streaming ingestion and storage prices are used for telemetry routing estimates.'), check: (v) => (v.length <= 80 ? '' : t('Enter at most {n} characters.', { n: 80 })) },
      { path: ['public_sources'], label: t('Google pricing pages'), type: 'lines', mono: true, rows: 3, wide: true, maxItems: 10, itemCheck: isGoogleDocUrl, itemError: (v) => t('"{value}" is not an https address on *.google.com or *.google.dev.', { value: v }), help: t('One https address per line on *.google.com or *.google.dev. Read for models that the Cloud Billing Catalog does not list yet.') },
      { path: ['fx_sources'], label: t('Exchange rate feeds'), type: 'lines', mono: true, rows: 2, wide: true, maxItems: 5, itemCheck: isHttpsUrl, itemError: httpsError, help: t('One https address per line that returns JSON rates based on USD. Used when the Cloud Billing Catalog has no rate for a currency.') },
      { heading: t('Model prices') },
      { path: ['model_aliases'], label: t('Model aliases'), type: 'lines', mono: true, rows: 3, wide: true, maxItems: 100, placeholder: 'my-tuned-model=gemini-2.5-flash', appendLine: aliasModel ? `${aliasModel}=` : null, itemCheck: isAlias, itemError: (v) => t('"{value}" must look like model=priced-model with lowercase model IDs.', { value: v }), help: t('One per line as model=priced-model. A model without an official price, such as a tuned model, uses the price of the target model listed in Cost & FinOps.') },
      { path: ['model_overrides'], label: t('Administrator prices'), type: 'prices', wide: true, focusModel: priceModel, help: t('USD per 1M tokens. An administrator price replaces the official price of that model. Leave Cached input empty to use the input price for cached tokens.') },
      { heading: t('Change alerts') },
      { path: ['alert_price_change_pct'], label: t('Price change alert (%)'), type: 'number', min: 0, max: 1000, step: 0.1, range: true, help: t('Record an alert when a model price changes by at least this percentage between two checks. 0 = every change.') },
      { path: ['alert_fx_change_pct'], label: t('Exchange rate alert (%)'), type: 'number', min: 0, max: 1000, step: 0.1, range: true, help: t('Record an alert when an exchange rate moves by at least this percentage between two checks. 0 = every change.') },
    ],
  }));

  panel.append(sectionForm(ctx, {
    title: t('Telemetry discovery'),
    subtitle: t('Sources are found from what your projects actually write to Cloud Logging and BigQuery. These limits only bound the work of each discovery run.'),
    section: 'telemetry', resetKeys: ['discovery'], data, editable, showDefaults: true,
    fields: [
      { path: ['discovery', 'max_projects'], label: t('Projects scanned'), type: 'number', min: 1, max: 50, integer: true, range: true, help: t('Projects read per discovery run, the workspace project first.') },
      { path: ['discovery', 'max_datasets_per_project'], label: t('Datasets per project'), type: 'number', min: 0, max: 200, integer: true, range: true, help: t('BigQuery datasets scanned for telemetry tables in each project. 0 = skip BigQuery tables.') },
      { path: ['discovery', 'max_tables_per_dataset'], label: t('Tables per dataset'), type: 'number', min: 1, max: 1000, integer: true, range: true, help: t('Tables read from each dataset. The rest are skipped with a notice.') },
      { path: ['discovery', 'activity_days'], label: t('Activity window (days)'), type: 'number', min: 1, max: 30, integer: true, range: true, help: t('Days of log volume used to rank sources and draw their activity.') },
      { path: ['discovery', 'sample_entries'], label: t('Sampled log entries'), type: 'number', min: 0, max: 1000, step: 10, integer: true, range: true, help: t('Recent entries read in one call to learn the payload fields. 0 = no sampling.') },
      { path: ['discovery', 'targeted_samples'], label: t('Extra samples for busy logs'), type: 'number', min: 0, max: 50, integer: true, range: true, help: t('Additional reads for busy logs that the shared sample missed.') },
      { path: ['discovery', 'max_log_entries'], label: t('Entries per dashboard view'), type: 'number', min: 100, max: 50000, step: 100, integer: true, range: true, help: t('Log entries a Cloud Logging dashboard reads per view. Every 1,000 entries is one API call.') },
      { path: ['discovery', 'dataset_hints'], label: t('Dataset name hints'), type: 'list', mono: true, maxItems: 30, itemCheck: isHint, itemError: (v) => t('"{value}" must be a lowercase word of letters, digits, "_", "-" or ".".', { value: v }), help: t('Comma separated. Datasets whose names contain these words are scanned first; nothing is excluded.') },
      { path: ['discovery', 'hidden_logs'], label: t('Hidden logs'), type: 'lines', mono: true, rows: 3, wide: true, maxItems: 100, help: t('Logs whose name contains any of these texts are never listed, one per line.') },
    ],
  }));

  let reanalyze = null;
  if (telemetryOn && editable) {
    reanalyze = button({ label: t('Analyze sources again'), icon: 'spark' });
    reanalyze.addEventListener('click', async () => {
      const ok = await confirmDialog({ title: t('Analyze sources again'), message: t('Saved Gemini analyses are cleared, so the next discovery analyzes every source again with the current settings. This uses Gemini requests.'), confirmLabel: t('Clear analyses') });
      if (!ok) return;
      setBusy(reanalyze, true, t('Clearing'));
      try {
        const r = (await api.post('/api/v1/telemetry/profiles/refresh', {})).data || {};
        toast({ title: t('Saved analyses cleared'), text: t('{n} sources are analyzed again at the next discovery.', { n: fmt.int(r.removed || 0) }), tone: 'success' });
      } catch (e) {
        toast({ title: t('The change was not applied'), text: errorText(e), tone: 'danger' });
      } finally {
        setBusy(reanalyze, false);
      }
    });
  }
  panel.append(sectionForm(ctx, {
    title: t('Gemini analysis of telemetry'),
    subtitle: t('Gemini describes each discovered source, maps its dashboard fields and groups related logs. Results are kept per source and reused until they expire or the fields change.'),
    section: 'telemetry', resetKeys: ['ai'], data, editable, showDefaults: true, extraActions: reanalyze,
    fields: [
      { path: ['ai', 'enabled'], label: t('Analyze sources with Gemini'), type: 'switch', help: t('When off, sources are described and mapped by rules only.') },
      { path: ['ai', 'model'], label: t('Gemini model'), mono: true, suggestions: aiModels, placeholder: aiDefault, emptyLabel: t('AI engine default ({model})', { model: aiDefault || DASH }), check: (v) => (!v || MODEL_ID_RE.test(v) ? '' : t('Enter a model ID of lowercase letters, digits, "." or "-", or leave it empty.')), help: t('Empty = the AI engine default model. An unavailable model falls back automatically.') },
      { path: ['ai', 'batch_size'], label: t('Sources per request'), type: 'number', min: 5, max: 60, integer: true, range: true, help: t('Sources sent to Gemini in one request.') },
      { path: ['ai', 'max_sources'], label: t('Sources analyzed per run'), type: 'number', min: 0, max: 500, integer: true, range: true, help: t('The busiest sources first. 0 = no Gemini analysis.') },
      { path: ['ai', 'profile_ttl_hours'], label: t('Reuse an analysis for (hours)'), type: 'number', min: 1, max: 2160, integer: true, range: true, help: t('A source is analyzed again after this time, or earlier when its fields change.') },
      { path: ['ai', 'share_value_examples'], label: t('Share masked value examples'), type: 'switch', help: t('Send up to four masked examples of fields with few distinct values for better labels. Fields that match the sensitive hints are never sampled.') },
    ],
  }));

  const hintError = (v) => t('"{value}" must be a lowercase word of letters, digits, "_", "-" or ".".', { value: v });
  const hint = (key, label, help) => ({ path: ['inference', key], label, type: 'words', mono: true, rows: 2, maxItems: 50, itemCheck: isHint, itemError: hintError, help });
  panel.append(sectionForm(ctx, {
    title: t('Field detection hints'),
    subtitle: t('Words in field names that help find the dashboard fields of a source, together with value statistics. Separate words with commas; an empty list uses the default hints.'),
    section: 'telemetry', resetKeys: ['inference', 'display_renames'], data, editable, cls: 'settings-form-2col',
    fields: [
      hint('time_hints', t('Time fields'), t('Names of the event time field.')),
      hint('user_hints', t('User fields'), t('Names of the user field, together with the share of e-mail-like values.')),
      hint('model_hints', t('Model fields'), t('Names of the model field.')),
      hint('token_hints', t('Token fields'), t('Names of numeric token count fields.')),
      hint('operation_hints', t('Operation fields'), t('Names of the operation or event type field.')),
      hint('status_hints', t('Status fields'), t('Names of the status or result field.')),
      hint('sensitive_hints', t('Sensitive fields'), t('Names of free-text fields such as prompts. Sources with them are marked sensitive, and their values are never summarized or sent as examples.')),
      { path: ['display_renames'], label: t('Product name replacements'), type: 'lines', rows: 2, maxItems: 30, itemCheck: isPair, itemError: (v) => t('"{value}" must look like From=To.', { value: v }), help: t('One per line as From=To. Applied to product names read from Google Cloud.') },
    ],
  }));

  const routingAction = telemetryOn ? button({ label: t('Telemetry Extensions'), icon: 'plug', variant: 'ghost', onClick: () => ctx.navigate('#/extensions') }) : null;
  panel.append(sectionForm(ctx, {
    title: t('BigQuery routing cost controls'),
    subtitle: t('Defaults for routes that copy log entries to BigQuery with the Log Router. Routed entries are billed as BigQuery streaming inserts and storage; each route can override these values.'),
    section: 'telemetry', resetKeys: ['routing'], data, editable, showDefaults: true, extraActions: routingAction,
    fields: [
      { heading: t('Destination') },
      { path: ['routing', 'dataset'], label: t('Routed dataset'), mono: true, check: (v) => (/^[A-Za-z0-9_]{1,1024}$/.test(v) ? '' : t('Use letters, digits and underscores only.')), help: t('BigQuery dataset that receives routed entries. It is created on first use.') },
      { path: ['routing', 'dataset_location'], label: t('Dataset location'), mono: true, placeholder: t('Audit dataset location'), emptyLabel: t('Audit dataset location'), check: (v) => (/^([A-Za-z][A-Za-z0-9-]{1,40})?$/.test(v) ? '' : t('Enter a BigQuery location such as US, EU or a region, or leave it empty.')), help: t('Empty = the location of the audit log dataset.') },
      { path: ['routing', 'sink_prefix'], label: t('Sink name prefix'), mono: true, check: (v) => (/^[a-z][a-z0-9_-]{0,30}$/.test(v) ? '' : t('Start with a lowercase letter and use up to 31 lowercase letters, digits, "-" or "_".')), help: t('Log Router sinks whose names start with this prefix are managed here.') },
      { path: ['routing', 'retention_days'], label: t('Retention (days)'), type: 'number', min: 0, max: 3650, integer: true, range: true, help: t('Partition expiration of routed tables. 0 = keep data indefinitely.') },
      { heading: t('Volume') },
      { path: ['routing', 'sample_percent'], label: t('Sample (%)'), type: 'number', min: 0.01, max: 100, step: 0.01, range: true, help: t('Share of entries routed, sampled by insert ID. 100 = every entry.') },
      { path: ['routing', 'min_severity'], label: t('Minimum severity'), type: 'select', options: [{ value: '', label: t('Any severity') }].concat(SEVERITIES.map((s) => ({ value: s, label: t('{severity} and above', { severity: s }) }))), help: t('Entries below this severity are not routed.') },
      { path: ['routing', 'exclusion_filters'], label: t('Exclusion filters'), type: 'lines', mono: true, rows: 3, wide: true, maxItems: 20, placeholder: 'httpRequest.requestUrl:"/healthz"', help: t('One Cloud Logging filter per line. Matching entries are never routed, for example health checks.') },
      { heading: t('Budgets') },
      { path: ['routing', 'daily_budget_gb'], label: t('Daily budget for all routes (GiB)'), type: 'number', min: 0, max: 1e6, step: 'any', range: true, help: t('GiB per local day for all routes together; 0 = unlimited.') },
      { path: ['routing', 'sink_daily_gb'], label: t('Daily budget per route (GiB)'), type: 'number', min: 0, max: 1e6, step: 'any', range: true, help: t('Default GiB per local day for each route; a route can set its own. 0 = unlimited.') },
      { path: ['routing', 'over_budget_action'], label: t('When over budget'), type: 'select', options: [{ value: 'pause', label: t('Pause the route') }, { value: 'alert', label: t('Alert only') }], help: t('Pausing disables the sink, so no more entries are billed that day.') },
      { path: ['routing', 'auto_resume'], label: t('Resume automatically'), type: 'switch', help: t('Paused routes resume at the start of the next local day.') },
      { path: ['routing', 'max_sinks'], label: t('Maximum routes'), type: 'number', min: 1, max: 200, integer: true, range: true, help: t('Log Router sinks that this workspace may create.') },
      { path: ['routing', 'follow_groups'], label: t('Follow log groups'), type: 'switch', help: t('Logs that join a routed group later are added to its route while the volume stays within budget.') },
      { path: ['routing', 'notify_webhook_url'], label: t('Notification webhook'), mono: true, wide: true, placeholder: 'https://chat.googleapis.com/...', check: (v) => (!v || isHttpsUrl(v) ? '' : t('Enter an https address or leave it empty.')), help: t('Slack or Google Chat webhook (https) notified when a route is paused or over budget. Empty = no notifications.') },
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
