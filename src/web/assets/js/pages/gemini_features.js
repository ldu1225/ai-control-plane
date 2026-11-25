/**
 * Gemini Enterprise feature management (#/gemini-enterprise/features).
 *
 * One matrix of every Gemini Enterprise app (columns) and every feature switch (rows). Administrators stage changes
 * cell by cell, per feature across apps, or by copying one app's switches to the others, review the diff and apply
 * everything at once. The server reads each app again and merges only the changed switches
 * (engines.patch with updateMask=features), so switches changed elsewhere in the meantime are kept.
 *
 * States are shown exactly as the API stores them: On (FEATURE_STATE_ON), Off (FEATURE_STATE_OFF) and Default (the
 * key is absent, the product default applies). Keys that start with "disable-" are restrictions: On blocks the
 * capability. The key "*" is the master switch: while it is set, the other switches of that app are ignored.
 */
import { h, mount, append, downloadText, toCsv } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';
import { fmt, DASH } from '../core/format.js';
import { api, isAbort } from '../core/api.js';
import { isAdmin } from '../core/state.js';
import { errorText } from '../core/messages.js';
import {
  card, kpi, badge, button, setBusy, chip, searchBox, selectInput, emptyState, errorState, banner, openModal, openPopover,
  menuItem, toast, warningsNotice,
} from '../core/components.js';
import { pageSkeleton, fetchRange } from './_shared.js';

const BASE = '/api/v1/gemini-enterprise/features';
const DOCS = 'https://cloud.google.com/gemini-enterprise/docs/reference/rest/v1/projects.locations.collections.engines';
const MAX_APPS_PER_APPLY = 100;

const GROUPS = [
  ['global', 'Master switch'],
  ['agents', 'Agents and automation'],
  ['assistant', 'Assistant experience'],
  ['knowledge', 'Search and knowledge'],
  ['creation', 'Content creation'],
  ['sharing', 'Sharing and collaboration'],
  ['personal', 'Personalization'],
  ['access', 'Files and access'],
  ['other', 'Other features'],
];

/* Every key of Engine.features documented in the Gemini Enterprise API reference (v1), plus keys the console sets on
   Gemini Enterprise apps that the reference does not list yet (agent sharing approval, orchestration restrictions). */
const FEATURES = [
  ['*', 'global', 'All features (master switch)'],
  ['agent-gallery', 'agents', 'Agent gallery'],
  ['no-code-agent-builder', 'agents', 'No-code agent builder'],
  ['single-agent-orchestration', 'agents', 'Single-agent orchestration'],
  ['multi-agent-orchestration', 'agents', 'Multi-agent orchestration'],
  ['workflow-agents', 'agents', 'Workflow agents'],
  ['skills', 'agents', 'Skills'],
  ['skill-sharing', 'agents', 'Skill sharing'],
  ['skill-sharing-without-admin-approval', 'agents', 'Skill sharing without administrator approval'],
  ['disable-agent-sharing', 'agents', 'Block agent sharing'],
  ['agent-sharing-without-admin-approval', 'agents', 'Agent sharing without administrator approval'],
  ['disable-single-agent-orchestration', 'agents', 'Block single-agent orchestration'],
  ['disable-multi-agent-orchestration', 'agents', 'Block multi-agent orchestration'],
  ['model-selector', 'assistant', 'Model selector'],
  ['prompt-gallery', 'assistant', 'Prompt gallery'],
  ['bi-directional-audio', 'assistant', 'Bidirectional audio'],
  ['speech-to-text', 'assistant', 'Speech to text'],
  ['feedback', 'assistant', 'Feedback'],
  ['in-app-notifications', 'assistant', 'In-app notifications'],
  ['cross-product-intelligence', 'assistant', 'Cross-product intelligence'],
  ['notebook-lm', 'knowledge', 'NotebookLM'],
  ['people-search', 'knowledge', 'People search'],
  ['people-search-org-chart', 'knowledge', 'People search organization chart'],
  ['canvas-workspace', 'creation', 'Canvas workspace'],
  ['canvas-app-builder', 'creation', 'Canvas app builder'],
  ['disable-canvas', 'creation', 'Block Canvas'],
  ['disable-image-generation', 'creation', 'Block image generation'],
  ['disable-video-generation', 'creation', 'Block video generation'],
  ['disable-talk-to-content', 'creation', 'Block talk to content'],
  ['session-sharing', 'sharing', 'Session sharing'],
  ['enable-end-user-sharing-with-groups', 'sharing', 'End-user sharing with groups'],
  ['disable-projects', 'sharing', 'Block projects'],
  ['personalization-memory', 'personal', 'Memory'],
  ['personalization-suggested-highlights', 'personal', 'Suggested highlights'],
  ['mobile-app-access', 'access', 'Mobile app access'],
  ['disable-google-drive-upload', 'access', 'Block Google Drive upload'],
  ['disable-onedrive-upload', 'access', 'Block OneDrive upload'],
  ['disable-welcome-emails', 'access', 'Block welcome emails'],
  ['sobi', 'other', 'sobi'],
];

const STATE_LABELS = { on: 'On', off: 'Off', default: 'Default' };
const STATE_ICONS = { on: 'check', off: 'x', default: 'minus' };
const STATES = ['on', 'off', 'default'];
const ROW_ACTIONS = { on: 'Turn on in all shown apps', off: 'Turn off in all shown apps', default: 'Reset to default in all shown apps' };

/* ------------------------------------------------------------------ model */

function featureRows(keys) {
  const known = new Set(FEATURES.map((f) => f[0]));
  const rows = FEATURES.map(([key, group, label]) => ({ key, group, label: t(label), restriction: key.startsWith('disable-') }));
  (keys || []).filter((k) => !known.has(k)).sort().forEach((key) => {
    rows.push({ key, group: 'other', label: key, restriction: key.startsWith('disable-'), undocumented: true });
  });
  return rows;
}

function createModel(data) {
  return {
    data,
    apps: data.apps || [],
    rows: featureRows(data.keys),
    pending: new Map(), // app key -> Map(feature key -> state)
    filter: { query: '', group: 'all', mixedOnly: false, explicitOnly: false, project: 'all' },
  };
}

function original(app, key) {
  return (app.features && app.features[key]) || 'default';
}

function effective(model, app, key) {
  const p = model.pending.get(app.key);
  return p && p.has(key) ? p.get(key) : original(app, key);
}

function isPending(model, app, key) {
  const p = model.pending.get(app.key);
  return Boolean(p && p.has(key));
}

function stage(model, app, key, state) {
  let p = model.pending.get(app.key);
  if (state === original(app, key)) {
    if (p) { p.delete(key); if (!p.size) model.pending.delete(app.key); }
    return;
  }
  if (!p) { p = new Map(); model.pending.set(app.key, p); }
  p.set(key, state);
}

function pendingCount(model) {
  let n = 0;
  model.pending.forEach((p) => { n += p.size; });
  return n;
}

function visibleApps(model) {
  const f = model.filter;
  return model.apps.filter((a) => f.project === 'all' || a.project === f.project);
}

function distribution(model, apps, key) {
  const d = { on: 0, off: 0, default: 0 };
  apps.forEach((a) => { d[effective(model, a, key)] += 1; });
  return d;
}

function isMixed(d) {
  return [d.on, d.off, d.default].filter((n) => n > 0).length > 1;
}

function visibleRows(model, apps) {
  const f = model.filter;
  const q = f.query.trim().toLowerCase();
  return model.rows.filter((r) => {
    if (f.group !== 'all' && r.group !== f.group) return false;
    if (q && !`${r.label} ${r.key}`.toLowerCase().includes(q)) return false;
    const d = distribution(model, apps, r.key);
    if (f.mixedOnly && !isMixed(d)) return false;
    if (f.explicitOnly && d.default === apps.length) return false;
    return true;
  });
}

function masterSet(model, app) {
  return effective(model, app, '*') !== 'default';
}

/* ------------------------------------------------------------------ entry */

export default async function render(ctx, header) {
  const body = h('div', { class: 'stack-lg' });
  mount(ctx.root, header, body);
  mount(body, pageSkeleton(4, 1));
  let res;
  try {
    // The Refresh button (ctx.refresh) skips the server's 60-second cache so newly created apps appear at once.
    res = await fetchRange(ctx, BASE, ctx.refresh ? { refresh: 1 } : {});
  } catch (err) {
    if (isAbort(err) || ctx.signal.aborted) return null;
    mount(body, card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    return null;
  }
  if (ctx.signal.aborted) return null;
  mount(body);
  const notice = warningsNotice(res.meta && res.meta.warnings);
  if (notice) body.append(notice);
  const data = res.data || {};
  if (data.apps === null || data.apps === undefined) {
    body.append(card({
      body: emptyState({
        icon: 'sliders', tone: 'warning', title: t('Gemini Enterprise apps could not be read'),
        body: t('Grant the runtime service account Discovery Engine Viewer in each project and check that the Gemini Enterprise locations are configured in Settings > Workspace.'),
      }),
    }));
    return null;
  }
  if (!data.apps.length) {
    body.append(card({
      body: emptyState({
        icon: 'sliders', title: t('No Gemini Enterprise apps'),
        body: data.otherEngines
          ? t('{n} search apps were found, but none of them is a Gemini Enterprise app.', { n: data.otherEngines })
          : t('No Gemini Enterprise app exists in the selected projects and locations.'),
      }),
    }));
    return null;
  }
  const model = createModel(data);
  const view = { kpis: h('div', { class: 'kpi-grid' }), matrix: h('div'), bar: h('div', { class: 'fm-actionbar', hidden: true }) };
  const refresh = () => paint(ctx, model, view);
  const toolbar = buildToolbar(model, refresh);
  const actions = h('div', { class: 'row' },
    button({ label: t('Export CSV'), icon: 'download', size: 'sm', onClick: () => exportCsv(model) }),
    h('a', { class: 'btn btn-secondary btn-sm', href: DOCS, target: '_blank', rel: 'noopener noreferrer' }, t('API reference'), ' ', icon('external', { size: 12 })));
  body.append(view.kpis);
  if (!isAdmin()) {
    body.append(banner({ tone: 'info', iconName: 'lock', title: t('Read-only view'), text: t('Administrators can change features for many apps at once.') }));
  }
  body.append(card({
    title: t('Feature matrix'),
    subtitle: t('{apps} apps \u00b7 {projects} projects \u00b7 Locations: {locations}', {
      apps: fmt.int(model.apps.length), projects: fmt.int(new Set(model.apps.map((a) => a.project)).size), locations: (data.locations || []).join(', ') || DASH,
    }),
    actions, flush: true, source: `Discovery Engine engines.list / engines.patch (${data.apiVersion || 'v1'})`,
    body: h('div', null, toolbar, legend(), view.matrix, view.bar),
  }));
  refresh();
  const onLeave = (e) => { if (pendingCount(model)) { e.preventDefault(); e.returnValue = ''; } };
  window.addEventListener('beforeunload', onLeave);
  return () => window.removeEventListener('beforeunload', onLeave);
}

/* ------------------------------------------------------------------ toolbar, legend, KPIs */

function buildToolbar(model, refresh) {
  const f = model.filter;
  const groupChips = h('div', { class: 'row wrap fm-chips', role: 'group', 'aria-label': t('Feature category') });
  const renderChips = () => {
    mount(groupChips, [['all', 'All']].concat(GROUPS).map(([id, label]) => chip(t(label), {
      pressed: f.group === id, onClick: () => { f.group = id; renderChips(); refresh(); },
    })));
  };
  renderChips();
  const toggles = h('div', { class: 'row' });
  const renderToggles = () => mount(toggles,
    chip(t('Mixed only'), { pressed: f.mixedOnly, title: t('Features whose state differs between the shown apps'), onClick: () => { f.mixedOnly = !f.mixedOnly; renderToggles(); refresh(); } }),
    chip(t('Explicit only'), { pressed: f.explicitOnly, title: t('Features that at least one shown app sets explicitly'), onClick: () => { f.explicitOnly = !f.explicitOnly; renderToggles(); refresh(); } }));
  renderToggles();
  const projects = [...new Set(model.apps.map((a) => a.project))];
  const projectPick = projects.length > 1
    ? selectInput([{ value: 'all', label: t('All projects') }].concat(projects.map((p) => ({ value: p, label: p }))), f.project,
      (v) => { f.project = v; refresh(); }, { 'aria-label': t('Project') })
    : null;
  return h('div', { class: 'fm-toolbar' },
    h('div', { class: 'table-toolbar' },
      h('div', { class: 'left' }, searchBox({ placeholder: t('Search features'), onInput: (v) => { f.query = v; refresh(); } }), projectPick, toggles),
      h('div', { class: 'right' })),
    h('div', { class: 'fm-chip-row' }, groupChips));
}

function legend() {
  return h('div', { class: 'fm-legend' },
    STATES.map((s) => h('span', { class: 'fm-legend-item' }, stateGlyph(s), t(STATE_LABELS[s]))),
    h('span', { class: 'fm-legend-item' }, h('span', { class: 'fm-glyph pending' }, icon('edit', { size: 11 })), t('Pending change')),
    h('span', { class: 'fm-legend-note' }, t('Default: the switch is not set and the product default applies. Restriction: On blocks the capability.')));
}

function stateGlyph(state) {
  return h('span', { class: ['fm-glyph', state] }, icon(STATE_ICONS[state], { size: 11 }));
}

function paintKpis(model, el) {
  const apps = visibleApps(model);
  const explicit = apps.reduce((n, a) => n + Object.keys(a.features || {}).length, 0);
  const mixed = model.rows.filter((r) => isMixed(distribution(model, apps, r.key))).length;
  const masters = apps.filter((a) => masterSet(model, a)).length;
  const pending = pendingCount(model);
  mount(el,
    kpi({ label: t('Gemini Enterprise apps'), value: fmt.int(apps.length), meta: t('{n} projects', { n: fmt.int(new Set(apps.map((a) => a.project)).size) }), tip: t('Apps in the selected projects and configured locations.') }),
    kpi({ label: t('Explicit switches'), value: fmt.int(explicit), meta: t('{n} features documented', { n: fmt.int(FEATURES.length) }), tip: t('Switches set to On or Off. Unset switches follow the product default.') }),
    kpi({ label: t('Mixed features'), value: fmt.int(mixed), meta: masters ? t('{n} apps use the master switch', { n: fmt.int(masters) }) : t('Same state in every app when 0'), tip: t('Features whose state differs between the shown apps.') }),
    kpi({ label: t('Pending changes'), value: fmt.int(pending), meta: t('{n} apps', { n: fmt.int(model.pending.size) }), tip: t('Staged changes that are not applied yet.') }));
}

/* ------------------------------------------------------------------ matrix */

function paint(ctx, model, view) {
  paintKpis(model, view.kpis);
  const apps = visibleApps(model);
  const rows = visibleRows(model, apps);
  const admin = isAdmin();
  const repaint = () => paint(ctx, model, view);
  if (!rows.length) {
    mount(view.matrix, emptyState({ icon: 'filter', title: t('No features match the filters'), body: t('Clear the search or choose another category.'), compact: true }));
  } else {
    const head = h('tr', null,
      h('th', { class: 'fm-feature-col', scope: 'col' }, t('Feature')),
      h('th', { class: 'fm-dist-col', scope: 'col' }, t('Across apps')),
      apps.map((app) => appHeader(model, app, apps, admin, repaint)));
    const bodyRows = [];
    let group = null;
    rows.forEach((row) => {
      if (row.group !== group) {
        group = row.group;
        const label = (GROUPS.find((g) => g[0] === group) || [group, group])[1];
        bodyRows.push(h('tr', { class: 'fm-group' }, h('th', { class: 'fm-feature-col', scope: 'rowgroup', colspan: 2 }, t(label)), h('td', { colspan: apps.length })));
      }
      bodyRows.push(featureRow(model, row, apps, admin, repaint));
    });
    mount(view.matrix, h('div', { class: 'fm-wrap' }, h('table', { class: 'fm-table' }, h('thead', null, head), h('tbody', null, bodyRows))));
  }
  paintActionBar(ctx, model, view, repaint);
}

function appHeader(model, app, apps, admin, repaint) {
  const pend = model.pending.get(app.key);
  const master = masterSet(model, app);
  const menu = admin ? h('button', {
    type: 'button', class: 'icon-btn fm-col-menu', title: t('App actions'), 'aria-label': t('App actions'), 'aria-haspopup': 'menu',
    onclick: (e) => openPopover(e.currentTarget, (close) => h('div', { role: 'menu' },
      menuItem({
        label: t('Use as baseline for all shown apps'), desc: t('Copy every switch of this app to the other shown apps'), iconName: 'copy',
        onClick: () => { close(); copyBaseline(model, app, apps); repaint(); },
      }),
      menuItem({
        label: t('Reset this app to defaults'), desc: t('Stage Default for every explicit switch'), iconName: 'refresh',
        onClick: () => { close(); model.rows.forEach((r) => stage(model, app, r.key, 'default')); repaint(); },
      }),
      pend ? menuItem({ label: t('Discard changes of this app'), iconName: 'x', onClick: () => { close(); model.pending.delete(app.key); repaint(); } }) : null)),
  }, icon('more', { size: 15 })) : null;
  return h('th', { class: ['fm-app-col', pend ? 'has-pending' : ''], scope: 'col' },
    h('div', { class: 'fm-app' },
      h('div', { class: 'fm-app-top' },
        h('span', { class: 'fm-app-name', title: app.displayName }, app.displayName),
        menu),
      h('span', { class: 'fm-app-id', title: app.engineId }, app.engineId),
      h('span', { class: 'fm-app-meta' }, h('span', { class: 'fm-pill', title: app.project }, app.project), h('span', { class: 'fm-pill' }, app.location)),
      master ? badge(t('Master switch {state}', { state: t(STATE_LABELS[effective(model, app, '*')]) }), 'warning', { size: 'sm', dot: true, title: t('While the master switch is set, the other switches of this app are ignored.') }) : null,
      pend ? badge(t('{n} pending', { n: pend.size }), 'accent', { size: 'sm' }) : null));
}

function featureRow(model, row, apps, admin, repaint) {
  const d = distribution(model, apps, row.key);
  const total = apps.length || 1;
  const distText = t('On {on}, Off {off}, Default {def}', { on: d.on, off: d.off, def: d.default });
  const bar = h('div', { class: 'fm-dist-bar', role: 'img', 'aria-label': distText, title: distText },
    STATES.map((s) => {
      if (!d[s]) return null;
      const seg = h('span', { class: ['fm-seg', s] });
      seg.style.setProperty('flex-grow', String(d[s] / total));
      return seg;
    }));
  const rowMenu = admin ? h('button', {
    type: 'button', class: 'icon-btn fm-row-menu', title: t('Set for all shown apps'), 'aria-label': t('Set for all shown apps'), 'aria-haspopup': 'menu',
    onclick: (e) => openPopover(e.currentTarget, (close) => h('div', { role: 'menu' },
      STATES.map((s) => menuItem({
        label: t(ROW_ACTIONS[s]),
        iconName: STATE_ICONS[s],
        onClick: () => { close(); apps.forEach((a) => stage(model, a, row.key, s)); repaint(); },
      })),
      apps.some((a) => isPending(model, a, row.key)) ? menuItem({
        label: t('Discard changes of this feature'), iconName: 'refresh',
        onClick: () => { close(); apps.forEach((a) => stage(model, a, row.key, original(a, row.key))); repaint(); },
      }) : null), { align: 'start' }),
  }, icon('sliders', { size: 14 })) : null;
  return h('tr', { class: ['fm-row', isMixed(d) ? 'mixed' : ''] },
    h('th', { class: 'fm-feature-col', scope: 'row' },
      h('div', { class: 'fm-feature' },
        h('div', { class: 'fm-feature-text' },
          h('span', { class: 'fm-feature-name' }, row.label,
            row.restriction ? badge(t('Restriction'), 'neutral', { size: 'sm', title: t('On blocks this capability.') }) : null,
            row.key === '*' ? badge(t('Overrides all'), 'warning', { size: 'sm', title: t('While it is set, every other switch of the app is ignored.') }) : null,
            row.undocumented ? badge(t('Not in reference'), 'neutral', { size: 'sm', title: t('Set on an app but not listed in the API reference.') }) : null),
          h('span', { class: 'fm-feature-key' }, row.key)),
        rowMenu)),
    h('td', { class: 'fm-dist-col' }, h('div', { class: 'fm-dist' }, bar,
      h('span', { class: 'fm-dist-text', title: distText, 'aria-hidden': 'true' }, `${d.on}\u00b7${d.off}\u00b7${d.default}`),
      isMixed(d) ? badge(t('Mixed'), 'warning', { size: 'sm' }) : null)),
    apps.map((app) => cell(model, row, app, admin, repaint)));
}

function cell(model, row, app, admin, repaint) {
  const state = effective(model, app, row.key);
  const pending = isPending(model, app, row.key);
  const ignored = row.key !== '*' && masterSet(model, app);
  const from = original(app, row.key);
  const title = pending
    ? t('{feature} in {app}: {from} \u2192 {to} (pending)', { feature: row.label, app: app.displayName, from: t(STATE_LABELS[from]), to: t(STATE_LABELS[state]) })
    : t('{feature} in {app}: {state}', { feature: row.label, app: app.displayName, state: t(STATE_LABELS[state]) });
  const content = [stateGlyph(state), h('span', { class: 'fm-cell-text' }, t(STATE_LABELS[state])), pending ? h('span', { class: 'fm-cell-dot', 'aria-hidden': 'true' }) : null];
  const cls = ['fm-cell', state, pending ? 'pending' : '', ignored ? 'ignored' : ''];
  if (!admin) return h('td', { class: 'fm-cell-td' }, h('span', { class: cls, title }, content));
  const btn = h('button', {
    type: 'button', class: cls, title, 'aria-haspopup': 'menu',
    onclick: (e) => openPopover(e.currentTarget, (close) => h('div', { role: 'menu' },
      h('div', { class: 'fm-pop-head' }, h('div', { class: 'fm-pop-title' }, row.label), h('div', { class: 'fm-pop-sub' }, `${app.displayName} \u00b7 ${row.key}`)),
      STATES.map((s) => menuItem({
        label: t(STATE_LABELS[s]),
        desc: s === from ? t('Current value') : (s === 'default' ? t('Remove the switch; the product default applies') : null),
        iconName: STATE_ICONS[s], checked: s === state,
        onClick: () => { close(); stage(model, app, row.key, s); repaint(); },
      })),
      ignored ? h('div', { class: 'fm-pop-note' }, t('Ignored while the master switch of this app is set.')) : null)),
  }, content);
  return h('td', { class: 'fm-cell-td' }, btn);
}

function copyBaseline(model, source, apps) {
  apps.forEach((target) => {
    if (target.key === source.key) return;
    model.rows.forEach((r) => stage(model, target, r.key, effective(model, source, r.key)));
  });
}

/* ------------------------------------------------------------------ apply */

function paintActionBar(ctx, model, view, repaint) {
  const n = pendingCount(model);
  view.bar.hidden = !n;
  if (!n) { mount(view.bar); return; }
  const tooMany = model.pending.size > MAX_APPS_PER_APPLY;
  mount(view.bar,
    h('div', { class: 'fm-actionbar-text' }, icon('edit', { size: 15 }),
      h('span', null, t('{n} changes in {apps} apps are staged', { n: fmt.int(n), apps: fmt.int(model.pending.size) })),
      tooMany ? badge(t('At most {n} apps per apply', { n: MAX_APPS_PER_APPLY }), 'danger', { size: 'sm' }) : null),
    h('div', { class: 'row' },
      button({ label: t('Discard all'), onClick: () => { model.pending.clear(); repaint(); } }),
      button({ label: t('Review and apply'), icon: 'check', variant: 'primary', disabled: tooMany, onClick: () => openReview(ctx, model) })));
}

function buildChanges(model) {
  const out = [];
  model.apps.forEach((app) => {
    const p = model.pending.get(app.key);
    if (!p || !p.size) return;
    const set = {};
    p.forEach((state, key) => { set[key] = state; });
    out.push({ app, change: { project: app.project, location: app.location, engineId: app.engineId, set } });
  });
  return out;
}

function stateBadge(state) {
  return h('span', { class: ['fm-cell', 'static', state] }, stateGlyph(state), h('span', { class: 'fm-cell-text' }, t(STATE_LABELS[state])));
}

function labelOf(model, key) {
  const row = model.rows.find((r) => r.key === key);
  return row ? row.label : key;
}

function diffList(model, app, set) {
  return h('ul', { class: 'fm-diff' }, Object.entries(set).map(([key, to]) => h('li', null,
    h('span', { class: 'fm-diff-name' }, labelOf(model, key), h('span', { class: 'fm-feature-key' }, key)),
    h('span', { class: 'fm-diff-states' }, stateBadge(original(app, key)), icon('arrow-right', { size: 13 }), stateBadge(to)))));
}

const RESULT_TONES = { updated: 'success', unchanged: 'neutral', failed: 'danger' };
const RESULT_LABELS = { updated: 'Updated', unchanged: 'Unchanged', failed: 'Failed' };
const ERROR_HINTS = {
  denied: 'The runtime service account needs discoveryengine.engines.update on this project (role AI Control Plane GE Feature Manager or Discovery Engine Editor).',
  invalid: 'Google rejected a feature key or state for this app. The key may not be available for this app or edition.',
  not_found: 'The app no longer exists.',
};

function openReview(ctx, model) {
  const items = buildChanges(model);
  if (!items.length) return;
  const total = items.reduce((n, it) => n + Object.keys(it.change.set).length, 0);
  const touchesMaster = items.some((it) => '*' in it.change.set);
  let applied = false;
  const m = openModal({
    title: t('Review feature changes'),
    subtitle: t('{n} changes in {apps} apps', { n: fmt.int(total), apps: fmt.int(items.length) }),
    wide: true,
    onClose: () => { if (applied) ctx.rerender(); },
    body: (el) => append(el, [
      banner({ tone: 'info', title: t('Only the listed switches change'), text: t('Each app is read again right before the update, and only these switches are merged into its current settings. Every apply is written to the governance audit log.') }),
      touchesMaster ? banner({ tone: 'warning', title: t('The master switch changes'), text: t('While the master switch is set, every other switch of that app is ignored.') }) : null,
      h('div', { class: 'fm-review' }, items.map(({ app, change }) => h('section', { class: 'fm-review-app' },
        h('div', { class: 'fm-review-head' },
          h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main' }, app.displayName), h('span', { class: 'cell-sub cell-mono' }, `${app.project} \u00b7 ${app.location} \u00b7 ${app.engineId}`)),
          badge(t('{n} changes', { n: Object.keys(change.set).length }), 'accent', { size: 'sm' })),
        diffList(model, app, change.set))))]),
    footer: (el, dialog) => {
      const cancel = button({ label: t('Cancel'), onClick: () => dialog.close() });
      const go = button({
        label: t('Apply to {n} apps', { n: fmt.int(items.length) }), icon: 'check', variant: 'primary',
        onClick: async () => {
          setBusy(go, true, t('Applying'));
          cancel.disabled = true;
          try {
            const res = await api.post(`${BASE}/apply`, { changes: items.map((it) => it.change) });
            applied = true;
            showResults(model, m, res.data || {}, items);
          } catch (err) {
            setBusy(go, false);
            cancel.disabled = false;
            toast({ title: t('Feature changes were not applied'), text: errorText(err), tone: 'danger' });
          }
        },
      });
      append(el, [cancel, go]);
    },
  });
}

function showResults(model, m, d, items) {
  const byKey = new Map(items.map((it) => [it.app.key, it.app]));
  const results = d.results || [];
  const tone = d.failed ? (d.updated ? 'warning' : 'danger') : 'success';
  mount(m.body,
    banner({
      tone,
      title: d.failed ? t('Some apps were not updated') : t('Feature changes applied'),
      text: t('Updated {u} \u00b7 Unchanged {s} \u00b7 Failed {f}', { u: fmt.int(d.updated || 0), s: fmt.int(d.unchanged || 0), f: fmt.int(d.failed || 0) })
        + (d.auditLogged === false ? ` \u00b7 ${t('The audit entry could not be written.')}` : ''),
    }),
    h('div', { class: 'fm-review' }, results.map((r) => {
      const app = byKey.get(r.key);
      return h('section', { class: 'fm-review-app' },
        h('div', { class: 'fm-review-head' },
          h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main' }, app ? app.displayName : r.engineId), h('span', { class: 'cell-sub cell-mono' }, r.key)),
          badge(t(RESULT_LABELS[r.status] || r.status), RESULT_TONES[r.status] || 'neutral', { size: 'sm', dot: true })),
        r.status === 'failed' ? h('p', { class: 'fm-result-error' }, icon('alert-circle', { size: 14 }), t(ERROR_HINTS[r.error] || 'Google Cloud did not accept the change. Try again in a moment.')) : null);
    })));
  mount(m.footer, button({ label: t('Close'), variant: 'primary', onClick: () => m.close() }));
  model.pending.clear();
  toast({ title: d.failed ? t('Some apps were not updated') : t('Feature changes applied'), tone: d.failed ? 'warning' : 'success' });
}

/* ------------------------------------------------------------------ export */

function exportCsv(model) {
  const apps = visibleApps(model);
  const rows = visibleRows(model, apps);
  const columns = [{ key: 'label', label: t('Feature') }, { key: 'key', label: t('Key') }]
    .concat(apps.map((a) => ({ key: a.key, label: `${a.displayName} (${a.key})`, csv: (r) => effective(model, a, r.key) })));
  downloadText(`gemini-enterprise-features-${new Date().toISOString().slice(0, 10)}.csv`, toCsv(columns, rows));
}
