/**
 * Gemini Enterprise feature management & app model selection (#/gemini-enterprise/features).
 *
 * Two bulk matrices over every Gemini Enterprise app (columns):
 *   1. Feature switches (Engine.features): opt apps in or out of web-app capabilities.
 *   2. Model selection (Engine.modelConfigs): override Gemini, image, and partner models per app, enriched
 *      with live end-user resolved model availability from WidgetConfig.uiSettings.modelConfigInfo.
 *
 * Administrators stage changes cell by cell, per row across apps, or by copying one app as a baseline,
 * review the unified diff and apply everything at once. The server reads each app again and merges only
 * the changed keys (engines.patch with updateMask=features,modelConfigs), so concurrent console edits stay.
 *
 * States are shown exactly as Google stores them: On/Enabled, Off/Disabled, and Default (the key is absent,
 * the product default applies). Feature keys starting with "disable-" are restrictions: On blocks the
 * capability. The key "*" is the master switch: while it is set, other feature switches are ignored.
 */
import { h, mount, append, downloadText, toCsv } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';
import { fmt } from '../core/format.js';
import { api, isAbort } from '../core/api.js';
import { isAdmin } from '../core/state.js';
import { errorText } from '../core/messages.js';
import {
  card, kpi, badge, button, setBusy, searchBox, selectInput, segmented, emptyState, errorState, banner,
  openModal, openPopover, menuItem, toast, warningsNotice, infoTip,
} from '../core/components.js';
import { pageSkeleton, fetchRange } from './_shared.js';

const BASE = '/api/v1/gemini-enterprise/features';
const DOCS = 'https://cloud.google.com/gemini-enterprise/docs/reference/rest/v1/projects.locations.collections.engines';
const MODEL_DOCS = 'https://docs.cloud.google.com/gemini/enterprise/docs/manage-web-app-features#enable-gemini-models';
const CONSOLE_GE = 'https://console.cloud.google.com/gemini-enterprise';
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

/* Every key of Engine.features documented in the Discovery Engine v1 reference, plus keys the console sets on
   Gemini Enterprise apps (agent sharing approval, orchestration restrictions). */
const FEATURES = [
  ['*', 'global', 'All features (master switch)'],
  ['agent-gallery', 'agents', 'Agent gallery'],
  ['no-code-agent-builder', 'agents', 'No-code agent builder'],
  ['single-agent-orchestration', 'agents', 'Single-agent orchestration'],
  ['multi-agent-orchestration', 'agents', 'Multi-agent orchestration'],
  ['workflow-agents', 'agents', 'Workflow agents'],
  ['agent-marketplace', 'agents', 'Agent marketplace'],
  ['agent-scheduling', 'agents', 'Agent scheduling'],
  ['preview-agents', 'agents', 'Preview agents'],
  ['plugins', 'agents', 'Plugins'],
  ['skills', 'agents', 'Skills'],
  ['skill-sharing', 'agents', 'Skill sharing'],
  ['skill-sharing-without-admin-approval', 'agents', 'Skill sharing without administrator approval'],
  ['skill-security-scan', 'agents', 'Skill security scan'],
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
  ['remote-vm', 'assistant', 'Remote VM'],
  ['license-tier-badge', 'assistant', 'Show license tier badge'],
  ['notebook-lm', 'knowledge', 'Gemini Notebook'],
  ['people-search', 'knowledge', 'People search'],
  ['people-search-org-chart', 'knowledge', 'People search organization chart'],
  ['code-extraction', 'knowledge', 'Code extraction'],
  ['canvas-workspace', 'creation', 'Canvas workspace'],
  ['canvas-app-builder', 'creation', 'Canvas app builder'],
  ['canvas-app-sharing', 'creation', 'Canvas app sharing'],
  ['disable-canvas', 'creation', 'Block Canvas'],
  ['disable-image-generation', 'creation', 'Block image generation'],
  ['disable-video-generation', 'creation', 'Block video generation'],
  ['music-generation', 'creation', 'Music generation'],
  ['speech-generation', 'creation', 'Speech generation'],
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

const MODEL_GROUPS = [
  ['gemini', 'Gemini models'],
  ['image', 'Image generation models'],
  ['partner', 'Third-party models'],
  ['other', 'Other models'],
];

/* Fallback Gemini Enterprise catalog used only when WidgetConfig (modelInfoView=ADMIN) cannot be read.
   Entry shape: [modelId, group, displayName, flags]. */
const FALLBACK_MODELS = [
  ['gemini-4-neon', 'gemini', 'Gemini 4 Neon', {}],
  ['gemini-4-hydrogen', 'gemini', 'Gemini 4 Hydrogen', {}],
  ['gemini-4-carbon', 'gemini', 'Gemini 4 Carbon', {}],
  ['gemini-4-argon', 'gemini', 'Gemini 4 Argon', {}],
  ['gemini-3.8-flash', 'gemini', 'Gemini 3.8 Flash', { defaultOn: true }],
  ['gemini-3.7-flash', 'gemini', 'Gemini 3.7 Flash', {}],
  ['gemini-3.5-flash', 'gemini', 'Gemini 3.5 Flash', { defaultOn: true, lockedOn: true }],
  ['gemini-3.1-pro-preview', 'gemini', 'Gemini 3.1 Pro', { preview: true }],
  ['gemini-2.5-pro', 'gemini', 'Gemini 2.5 Pro', { defaultOn: true }],
  ['gemini-3.1-flash-image', 'image', 'Gemini 3.1 Flash Image', {}],
  ['gemini-3-pro-image', 'image', 'Gemini 3 Pro Image', { globalOnly: true }],
  ['gemini-2.5-flash-image', 'image', 'Gemini 2.5 Flash Image', { defaultOn: true }],
  ['claude-opus-5-5', 'partner', 'Claude Opus 5.5', { partner: true }],
  ['claude-sonnet-5-5', 'partner', 'Claude Sonnet 5.5', { partner: true }],
];

const STATE_LABELS = { on: 'On', off: 'Off', default: 'Default' };
const MODEL_STATE_LABELS = { on: 'Enabled', off: 'Disabled', default: 'Default' };
const STATE_ICONS = { on: 'check', off: 'x', default: 'minus' };
const STATES = ['on', 'off', 'default'];
const ROW_ACTIONS = {
  on: 'Turn on in all shown apps',
  off: 'Turn off in all shown apps',
  default: 'Reset to default in all shown apps',
};
const MODEL_ROW_ACTIONS = {
  on: 'Enable in all shown apps',
  off: 'Disable in all shown apps',
  default: 'Reset to default in all shown apps',
};

/* ------------------------------------------------------------------ model */

function featureRows(keys) {
  const known = new Set(FEATURES.map((f) => f[0]));
  const rows = FEATURES.map(([key, group, label]) => ({
    key, group, label: t(label), restriction: key.startsWith('disable-'),
  }));
  (keys || []).filter((k) => !known.has(k)).sort().forEach((key) => {
    rows.push({ key, group: 'other', label: key, restriction: key.startsWith('disable-'), undocumented: true });
  });
  return rows;
}

function modelRows(modelKeys, resolvedMeta, availableModels) {
  const byGroup = Object.fromEntries(MODEL_GROUPS.map(([id]) => [id, []]));
  const seen = new Set();
  const meta = resolvedMeta || {};
  const push = (row) => {
    const bucket = byGroup[row.group] || byGroup.other;
    bucket.push(row);
    seen.add(row.key);
  };

  if (Array.isArray(availableModels) && availableModels.length) {
    // Authoritative Gemini Enterprise model registry from Discovery Engine WidgetConfig (modelInfoView=ADMIN).
    availableModels.forEach((item) => {
      if (!item || !item.modelId || seen.has(item.modelId)) return;
      const key = item.modelId;
      const group = byGroup[item.group] ? item.group : 'other';
      const regions = Array.isArray(item.regions) ? item.regions : [];
      push({
        key,
        group,
        label: item.displayName || key,
        description: item.description || null,
        defaultOn: Boolean(item.enabledByDefault),
        lockedOn: Boolean(item.lockedOn),
        preview: Boolean(item.isPreview),
        partner: group === 'partner',
        globalOnly: regions.length === 1 && regions[0] === 'global',
        regions,
      });
    });
  } else {
    FALLBACK_MODELS.forEach(([key, group, label, flags]) => {
      const live = meta[key];
      push({
        key,
        group,
        label: (live && live.displayName) || label,
        description: (live && live.description) || null,
        defaultOn: Boolean(flags.defaultOn),
        lockedOn: Boolean(flags.lockedOn),
        preview: Boolean(flags.preview || (live && live.isPreview)),
        partner: Boolean(flags.partner),
        globalOnly: Boolean(flags.globalOnly),
        regions: [],
      });
    });
  }

  // Keep any legacy/previous model IDs already explicitly stored on an app so administrators can review or clear them.
  [...(modelKeys || [])].filter((k) => !seen.has(k)).sort().forEach((key) => {
    const live = meta[key];
    const isPartner = key.startsWith('claude-') || key.startsWith('anthropic-');
    const isImage = key.includes('image');
    const isGemini = key.startsWith('gemini-');
    push({
      key,
      group: isPartner ? 'partner' : (isImage ? 'image' : (isGemini ? 'gemini' : 'other')),
      label: (live && live.displayName) || key,
      description: (live && live.description) || null,
      defaultOn: false,
      lockedOn: false,
      preview: Boolean((live && live.isPreview) || key.includes('preview')),
      partner: isPartner,
      globalOnly: false,
      regions: [],
      legacy: true,
    });
  });

  return MODEL_GROUPS.flatMap(([id]) => byGroup[id]);
}

function createModel(data, initialSection) {
  const available = Array.isArray(data.availableModels) ? data.availableModels : [];
  return {
    data,
    apps: data.apps || [],
    section: initialSection === 'models' ? 'models' : 'features',
    rows: featureRows(data.keys),
    liveCatalogCount: available.length,
    modelRows: modelRows(data.modelKeys, data.resolvedModelMeta, available),
    pending: new Map(), // app key -> Map(feature key -> state)
    pendingModels: new Map(), // app key -> Map(modelId -> state)
    filter: {
      query: '', group: 'all', mixedOnly: false, explicitOnly: false, project: 'all',
      modelQuery: '', modelGroup: 'all', modelMixedOnly: false, modelExplicitOnly: false,
    },
  };
}

function activeMap(model, section = model.section) {
  return section === 'models' ? model.pendingModels : model.pending;
}

function original(app, key, section = 'features') {
  const src = section === 'models' ? app.models : app.features;
  return (src && src[key]) || 'default';
}

function effective(model, app, key, section = model.section) {
  const p = activeMap(model, section).get(app.key);
  return p && p.has(key) ? p.get(key) : original(app, key, section);
}

function isPending(model, app, key, section = model.section) {
  const p = activeMap(model, section).get(app.key);
  return Boolean(p && p.has(key));
}

function stage(model, app, key, state, section = model.section) {
  const map = activeMap(model, section);
  let p = map.get(app.key);
  if (state === original(app, key, section)) {
    if (p) { p.delete(key); if (!p.size) map.delete(app.key); }
    return;
  }
  if (!p) { p = new Map(); map.set(app.key, p); }
  p.set(key, state);
}

function pendingCountOf(map) {
  let n = 0;
  map.forEach((p) => { n += p.size; });
  return n;
}

function totalPendingCount(model) {
  return pendingCountOf(model.pending) + pendingCountOf(model.pendingModels);
}

function totalPendingApps(model) {
  const keys = new Set([...model.pending.keys(), ...model.pendingModels.keys()]);
  return keys.size;
}

function visibleApps(model) {
  const f = model.filter;
  return model.apps.filter((a) => f.project === 'all' || a.project === f.project);
}

function distribution(model, apps, key, section = model.section) {
  const d = { on: 0, off: 0, default: 0 };
  apps.forEach((a) => { d[effective(model, a, key, section)] += 1; });
  return d;
}

function isMixed(d) {
  return [d.on, d.off, d.default].filter((n) => n > 0).length > 1;
}

function visibleFeatureRows(model, apps) {
  const f = model.filter;
  const q = f.query.trim().toLowerCase();
  return model.rows.filter((r) => {
    if (f.group !== 'all' && r.group !== f.group) return false;
    if (q && !`${r.label} ${r.key}`.toLowerCase().includes(q)) return false;
    const d = distribution(model, apps, r.key, 'features');
    if (f.mixedOnly && !isMixed(d)) return false;
    if (f.explicitOnly && d.default === apps.length) return false;
    return true;
  });
}

function visibleModelRows(model, apps) {
  const f = model.filter;
  const q = f.modelQuery.trim().toLowerCase();
  return model.modelRows.filter((r) => {
    if (f.modelGroup !== 'all' && r.group !== f.modelGroup) return false;
    if (q && !`${r.label} ${r.key}`.toLowerCase().includes(q)) return false;
    const d = distribution(model, apps, r.key, 'models');
    if (f.modelMixedOnly && !isMixed(d)) return false;
    if (f.modelExplicitOnly && d.default === apps.length) return false;
    return true;
  });
}

function masterSet(model, app) {
  return effective(model, app, '*', 'features') !== 'default';
}

function selectorDisabled(model, app) {
  return effective(model, app, 'model-selector', 'features') === 'off';
}

function isResolved(app, modelId) {
  return Array.isArray(app.resolvedModels) && app.resolvedModels.includes(modelId);
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
  const initialSection = ctx.params[1] === 'models' ? 'models' : 'features';
  const model = createModel(data, initialSection);
  const scannedProjects = Array.isArray(data.projects) && data.projects.length
    ? data.projects
    : [...new Set(model.apps.map((a) => a.project))];
  const appProjectSet = new Set(model.apps.map((a) => a.project));
  model.scope = {
    scanned: scannedProjects,
    empty: scannedProjects.filter((p) => !appProjectSet.has(p)),
    locations: data.locations || [],
    source: model.liveCatalogCount
      ? `Discovery Engine engines.list / engines.patch (${data.apiVersion || 'v1'}) \u00b7 widgetConfigs (v1alpha ADMIN)`
      : `Discovery Engine engines.list / engines.patch (${data.apiVersion || 'v1'})`,
  };
  const view = {
    kpis: h('div', { class: 'kpi-grid' }),
    switchHost: h('div', { class: 'fm-head-left' }),
    actionsHost: h('div', { class: 'fm-head-right' }),
    toolbarHost: h('div'),
    legendHost: h('div', { class: 'fm-legend' }),
    matrix: h('div'),
    bar: h('div', { class: 'fm-actionbar', hidden: true, role: 'region', 'aria-label': t('Pending changes') }),
  };
  const refresh = () => paint(ctx, model, view);

  body.append(view.kpis);
  if (!isAdmin()) {
    body.append(banner({
      tone: 'neutral',
      iconName: 'lock',
      title: t('Read-only view'),
      text: t('Administrators can change features and model selection for many apps at once.'),
    }));
  }
  body.append(card({
    flush: true,
    body: h('div', null,
      h('div', { class: 'fm-head' }, view.switchHost, view.actionsHost),
      view.toolbarHost,
      view.matrix,
      view.bar),
  }));

  refresh();
  const onLeave = (e) => { if (totalPendingCount(model)) { e.preventDefault(); e.returnValue = ''; } };
  window.addEventListener('beforeunload', onLeave);
  return () => window.removeEventListener('beforeunload', onLeave);
}

/* ------------------------------------------------------------------ section switcher, toolbar, legend, KPIs */

function paintSectionBar(ctx, model, view, repaint) {
  const fPending = pendingCountOf(model.pending);
  const mPending = pendingCountOf(model.pendingModels);
  const fLabel = fPending ? `${t('Feature switches')} \u00b7 ${fmt.int(fPending)}` : t('Feature switches');
  const mLabel = mPending ? `${t('Model selection')} \u00b7 ${fmt.int(mPending)}` : t('Model selection');
  const isModels = model.section === 'models';

  mount(view.switchHost,
    segmented([
      { value: 'features', label: fLabel },
      { value: 'models', label: mLabel },
    ], model.section, (v) => {
      model.section = v;
      repaint();
    }, { label: t('Configuration section') }),
    isModels && model.liveCatalogCount
      ? h('span', { class: 'fm-synced', title: t('Synced live from Discovery Engine WidgetConfig (modelInfoView=ADMIN).') },
        h('span', { class: 'dot dot-success', 'aria-hidden': 'true' }),
        t('{n} Gemini Enterprise models synced', { n: fmt.int(model.liveCatalogCount) }))
      : null);

  mount(view.actionsHost,
    button({ label: t('Export CSV'), icon: 'download', size: 'sm', variant: 'ghost', onClick: () => exportCsv(model) }),
    h('a', {
      class: 'btn btn-ghost btn-sm',
      href: isModels ? MODEL_DOCS : DOCS,
      target: '_blank',
      rel: 'noopener noreferrer',
    }, isModels ? t('Model guide') : t('API reference'), icon('external', { size: 12 })),
    infoTip(`${t('Data source')}: ${model.scope.source}`));
}

function paintToolbar(model, view, refresh) {
  if (view.toolbarSection === model.section) return;
  view.toolbarSection = model.section;
  const f = model.filter;
  const isModels = model.section === 'models';
  const groups = isModels ? MODEL_GROUPS : GROUPS;

  const category = selectInput(
    [{ value: 'all', label: isModels ? t('All models') : t('All categories') }]
      .concat(groups.map(([id, label]) => ({ value: id, label: t(label) }))),
    isModels ? f.modelGroup : f.group,
    (v) => {
      if (isModels) f.modelGroup = v; else f.group = v;
      refresh();
    },
    { 'aria-label': isModels ? t('Model category') : t('Feature category') },
  );

  const projects = [...new Set(model.apps.map((a) => a.project))];
  const projectPick = projects.length > 1
    ? selectInput([{ value: 'all', label: t('All projects') }].concat(projects.map((p) => ({ value: p, label: p }))), f.project,
      (v) => { f.project = v; refresh(); }, { 'aria-label': t('Project') })
    : null;

  // One "show" control instead of two independent toggles: everything, rows that differ between apps, or overrides.
  const mixed = isModels ? f.modelMixedOnly : f.mixedOnly;
  const explicit = isModels ? f.modelExplicitOnly : f.explicitOnly;
  const show = segmented([
    { value: 'all', label: t('All') },
    { value: 'mixed', label: t('Differs by app'), title: isModels ? t('Models whose state differs between the shown apps') : t('Features whose state differs between the shown apps') },
    { value: 'explicit', label: t('Overridden'), title: isModels ? t('Models that at least one shown app overrides explicitly') : t('Features that at least one shown app sets explicitly') },
  ], mixed ? 'mixed' : (explicit ? 'explicit' : 'all'), (v) => {
    if (isModels) { f.modelMixedOnly = v === 'mixed'; f.modelExplicitOnly = v === 'explicit'; } else { f.mixedOnly = v === 'mixed'; f.explicitOnly = v === 'explicit'; }
    refresh();
  }, { label: t('Show') });

  mount(view.toolbarHost, h('div', { class: 'fm-filters' },
    searchBox({
      value: isModels ? f.modelQuery : f.query,
      placeholder: isModels ? t('Search models') : t('Search features'),
      onInput: (v) => {
        if (isModels) f.modelQuery = v; else f.query = v;
        refresh();
      },
    }),
    category,
    projectPick,
    show,
    h('span', { class: 'fm-filters-spacer' }),
    view.legendHost));
}

function paintLegend(model, el) {
  const isModels = model.section === 'models';
  const labels = isModels ? MODEL_STATE_LABELS : STATE_LABELS;
  const item = (mark, text) => h('span', { class: 'fm-legend-item' }, mark, text);
  mount(el,
    item(stateMark('on'), t(labels.on)),
    item(stateMark('off'), t(labels.off)),
    isModels ? item(stateMark('default', { impliedOn: true }), t('Default on')) : null,
    item(stateMark('default'), t(labels.default)),
    isModels ? item(h('span', { class: 'fm-live-dot', 'aria-hidden': 'true' }), t('Active in web app')) : null,
    infoTip(isModels
      ? t('Default: Google applies the product default for the app region. Third-party models require PAYG overage and one-time Terms of Service acceptance in Cloud Console.')
      : t('Default: the switch is not set and the product default applies. Restriction: On blocks the capability.')));
}

/** State mark: explicit settings are solid, product defaults are dashed (green check when the default is on). */
function stateMark(state, { impliedOn = false, small = false } = {}) {
  const glyph = state === 'default' ? (impliedOn ? 'check' : 'minus') : STATE_ICONS[state];
  return h('span', {
    class: ['fm-mark', state, state === 'default' && impliedOn ? 'implied-on' : '', small ? 'sm' : ''],
    'aria-hidden': 'true',
  }, icon(glyph, { size: small ? 10 : 12, strokeWidth: 2.6 }));
}

function paintKpis(model, el) {
  const apps = visibleApps(model);
  const isModels = model.section === 'models';
  const totalPend = totalPendingCount(model);
  const totalPendApps = totalPendingApps(model);
  const activeProjCount = new Set(apps.map((a) => a.project)).size;
  const scannedProjCount = model.scope.scanned.length || activeProjCount;
  const projMeta = model.filter.project === 'all' && scannedProjCount > activeProjCount
    ? t('{active}/{total} projects', { active: fmt.int(activeProjCount), total: fmt.int(scannedProjCount) })
    : t('{n} projects', { n: fmt.int(activeProjCount) });
  const appsTip = [
    t('Apps in the selected projects and configured locations.'),
    model.scope.locations.length ? t('Locations: {locations}', { locations: model.scope.locations.join(', ') }) : null,
    model.scope.empty.length ? t('No Gemini Enterprise apps: {projects}', { projects: model.scope.empty.join(', ') }) : null,
  ].filter(Boolean).join(' ');
  const pendingKpi = kpi({
    label: t('Pending changes'),
    value: fmt.int(totalPend),
    meta: totalPendApps ? t('{n} apps', { n: fmt.int(totalPendApps) }) : null,
    tip: t('Staged feature and model changes that are not applied yet.'),
  });
  if (isModels) {
    const explicitModels = apps.reduce((n, a) => n + Object.keys(a.models || {}).length, 0);
    const mixedModels = model.modelRows.filter((r) => isMixed(distribution(model, apps, r.key, 'models'))).length;
    const selectorOff = apps.filter((a) => selectorDisabled(model, a)).length;
    mount(el,
      kpi({ label: t('Gemini Enterprise apps'), value: fmt.int(apps.length), meta: projMeta, tip: appsTip }),
      kpi({
        label: t('Explicit model overrides'),
        value: fmt.int(explicitModels),
        meta: t('{n} models in catalog', { n: fmt.int(model.modelRows.length) }),
        tip: t('Models set to Enabled or Disabled in Engine.modelConfigs. Unset models follow product defaults.'),
      }),
      kpi({
        label: t('Mixed models'),
        value: fmt.int(mixedModels),
        meta: selectorOff ? t('{n} apps have model selector off', { n: fmt.int(selectorOff) }) : null,
        tip: t('Models whose override state differs between the shown apps.'),
      }),
      pendingKpi);
    return;
  }

  const explicit = apps.reduce((n, a) => n + Object.keys(a.features || {}).length, 0);
  const mixed = model.rows.filter((r) => isMixed(distribution(model, apps, r.key, 'features'))).length;
  const masters = apps.filter((a) => masterSet(model, a)).length;
  mount(el,
    kpi({ label: t('Gemini Enterprise apps'), value: fmt.int(apps.length), meta: projMeta, tip: appsTip }),
    kpi({ label: t('Explicit switches'), value: fmt.int(explicit), meta: t('{n} features documented', { n: fmt.int(FEATURES.length) }), tip: t('Switches set to On or Off. Unset switches follow the product default.') }),
    kpi({ label: t('Mixed features'), value: fmt.int(mixed), meta: masters ? t('{n} apps use the master switch', { n: fmt.int(masters) }) : null, tip: t('Features whose state differs between the shown apps.') }),
    pendingKpi);
}

/* ------------------------------------------------------------------ matrix */

function paint(ctx, model, view) {
  const repaint = () => paint(ctx, model, view);
  paintKpis(model, view.kpis);
  paintSectionBar(ctx, model, view, repaint);
  paintToolbar(model, view, repaint);
  paintLegend(model, view.legendHost);

  const isModels = model.section === 'models';
  const apps = visibleApps(model);
  const rows = isModels ? visibleModelRows(model, apps) : visibleFeatureRows(model, apps);
  const groups = isModels ? MODEL_GROUPS : GROUPS;
  const admin = isAdmin();

  if (!rows.length) {
    mount(view.matrix, emptyState({
      icon: 'filter',
      title: isModels ? t('No models match the filters') : t('No features match the filters'),
      body: t('Clear the search or choose another category.'),
      compact: true,
    }));
  } else {
    const head = h('tr', null,
      h('th', { class: 'fm-feature-col', scope: 'col' }, isModels ? t('Model') : t('Feature')),
      h('th', { class: 'fm-dist-col', scope: 'col' }, t('Across apps')),
      apps.map((app) => appHeader(model, app, apps, admin, repaint)));
    const bodyRows = [];
    let group = null;
    rows.forEach((row) => {
      if (row.group !== group) {
        group = row.group;
        const label = (groups.find((g) => g[0] === group) || [group, group])[1];
        bodyRows.push(h('tr', { class: 'fm-group' },
          h('th', { class: 'fm-feature-col', scope: 'rowgroup', colspan: 2 }, t(label)),
          h('td', { colspan: apps.length })));
      }
      bodyRows.push(matrixRow(model, row, apps, admin, repaint));
    });
    mount(view.matrix, h('div', { class: 'fm-wrap' },
      h('table', { class: 'fm-table fm-matrix' },
        h('caption', { class: 'visually-hidden' }, isModels ? t('Gemini Enterprise app model selection matrix') : t('Gemini Enterprise app feature switch matrix')),
        h('thead', null, head),
        h('tbody', null, bodyRows))));
  }
  paintActionBar(ctx, model, view, repaint);
}

function appHeader(model, app, apps, admin, repaint) {
  const isModels = model.section === 'models';
  const pendMap = activeMap(model);
  const pend = pendMap.get(app.key);
  const totalAppPend = ((model.pending.get(app.key) && model.pending.get(app.key).size) || 0)
    + ((model.pendingModels.get(app.key) && model.pendingModels.get(app.key).size) || 0);
  const master = !isModels && masterSet(model, app);
  const selOff = isModels && selectorDisabled(model, app);
  const allSectionRows = isModels ? model.modelRows : model.rows;

  const menu = admin ? h('button', {
    type: 'button', class: 'icon-btn fm-col-menu', title: t('App actions'), 'aria-label': `${t('App actions')}: ${app.displayName}`, 'aria-haspopup': 'menu',
    onclick: (e) => openPopover(e.currentTarget, (close) => h('div', { role: 'menu' },
      menuItem({
        label: t('Use as baseline for all shown apps'),
        desc: isModels ? t('Copy every model override of this app to the other shown apps') : t('Copy every switch of this app to the other shown apps'),
        iconName: 'copy',
        onClick: () => { close(); copyBaseline(model, app, apps, allSectionRows); repaint(); },
      }),
      menuItem({
        label: t('Reset this app to defaults'),
        desc: isModels ? t('Stage Default for every explicit model override') : t('Stage Default for every explicit switch'),
        iconName: 'refresh',
        onClick: () => { close(); allSectionRows.forEach((r) => stage(model, app, r.key, 'default')); repaint(); },
      }),
      pend ? menuItem({
        label: t('Discard changes of this app'),
        iconName: 'x',
        onClick: () => { close(); pendMap.delete(app.key); repaint(); },
      }) : null)),
  }, icon('more', { size: 15 })) : null;

  const where = `${app.project} \u00b7 ${app.location}`;
  const flags = [
    master ? h('span', { class: 'fm-tag warn', title: t('While the master switch is set, the other switches of this app are ignored.') }, t('Master switch {state}', { state: t(STATE_LABELS[effective(model, app, '*', 'features')]) })) : null,
    selOff ? h('span', { class: 'fm-tag warn', title: t('End users cannot pick models while the model-selector feature switch is Off.') }, t('Model selector off')) : null,
    totalAppPend ? h('span', { class: 'fm-tag info' }, t('{n} pending', { n: totalAppPend })) : null,
  ].filter(Boolean);
  return h('th', { class: ['fm-app-col', totalAppPend ? 'has-pending' : ''], scope: 'col' },
    h('div', { class: 'fm-app' },
      h('span', { class: 'fm-app-name', title: `${app.displayName} (${app.engineId})` }, app.displayName),
      h('span', { class: 'fm-app-sub', title: where }, where),
      flags.length ? h('span', { class: 'fm-app-flags' }, flags) : null,
      menu));
}

function fmTag(text, tone, tip) {
  return h('span', { class: ['fm-tag', tone || ''], title: tip || null }, text);
}

function matrixRow(model, row, apps, admin, repaint) {
  const isModels = model.section === 'models';
  const d = distribution(model, apps, row.key);
  const total = apps.length || 1;
  const distText = isModels
    ? t('Enabled {on}, Disabled {off}, Default {def}', { on: d.on, off: d.off, def: d.default })
    : t('On {on}, Off {off}, Default {def}', { on: d.on, off: d.off, def: d.default });
  const bar = h('div', { class: 'fm-dist-bar', role: 'img', 'aria-label': distText, title: distText },
    STATES.map((s) => {
      if (!d[s]) return null;
      const seg = h('span', { class: ['fm-seg', s] });
      seg.style.setProperty('flex-grow', String(d[s] / total));
      return seg;
    }));
  const rowActions = isModels ? MODEL_ROW_ACTIONS : ROW_ACTIONS;
  const rowMenu = admin ? h('button', {
    type: 'button', class: 'icon-btn fm-row-menu', title: t('Set for all shown apps'), 'aria-label': `${t('Set for all shown apps')}: ${row.label}`, 'aria-haspopup': 'menu',
    onclick: (e) => openPopover(e.currentTarget, (close) => h('div', { role: 'menu' },
      STATES.map((s) => menuItem({
        label: t(rowActions[s]),
        iconName: STATE_ICONS[s],
        onClick: () => { close(); apps.forEach((a) => stage(model, a, row.key, s)); repaint(); },
      })),
      apps.some((a) => isPending(model, a, row.key)) ? menuItem({
        label: isModels ? t('Discard changes of this model') : t('Discard changes of this feature'),
        iconName: 'refresh',
        onClick: () => { close(); apps.forEach((a) => stage(model, a, row.key, original(a, row.key, model.section))); repaint(); },
      }) : null), { align: 'start' }),
  }, icon('more', { size: 15 })) : null;

  const tags = [];
  if (isModels) {
    if (row.lockedOn) {
      tags.push(h('span', {
        class: 'fm-lock', tabindex: 0, role: 'img',
        'data-tip': t('Always on by default in Gemini Enterprise (cannot be turned off).'), 'aria-label': t('Always on'),
      }, icon('lock', { size: 12 })));
    }
    if (row.preview) tags.push(fmTag(t('Preview')));
    if (row.partner) tags.push(fmTag(t('3P · PAYG'), 'violet', t('Requires pay-as-you-go usage above quota and one-time Terms of Service acceptance in Google Cloud Console.')));
    if (row.globalOnly) tags.push(fmTag(t('Global'), '', t('Available in the global region only; regional apps display a warning before routing to global.')));
    if (row.legacy) tags.push(fmTag(t('Legacy override'), '', t('Previously set in Engine.modelConfigs on an app, not listed in the current Gemini Enterprise model registry.')));
  } else {
    if (row.restriction) tags.push(fmTag(t('Restriction'), '', t('On blocks this capability.')));
    if (row.key === '*') tags.push(fmTag(t('Overrides all'), 'warn', t('While it is set, every other switch of the app is ignored.')));
    if (row.undocumented) tags.push(fmTag(t('Not in reference'), '', t('Set on an app but not listed in the API reference.')));
  }

  return h('tr', { class: ['fm-row', isMixed(d) ? 'mixed' : ''] },
    h('th', { class: 'fm-feature-col', scope: 'row' },
      h('div', { class: 'fm-feature' },
        h('div', { class: 'fm-feature-text' },
          h('span', { class: 'fm-feature-name', title: row.description || null }, row.label, ...tags),
          h('span', { class: 'fm-feature-key' }, row.key)),
        rowMenu)),
    h('td', { class: 'fm-dist-col' }, h('div', { class: 'fm-dist' }, bar,
      isMixed(d) ? h('span', { class: 'fm-mixed' }, t('Mixed')) : null)),
    apps.map((app) => cell(model, row, app, admin, repaint)));
}

/** Label of a state as shown to people; model defaults say whether the default is on or off. */
function stateLabel(model, row, state) {
  const isModels = model.section === 'models';
  const labels = isModels ? MODEL_STATE_LABELS : STATE_LABELS;
  if (!isModels || state !== 'default' || row.legacy) return t(labels[state]);
  return row.defaultOn || row.lockedOn ? t('Default (on)') : t('Default (off)');
}

function cell(model, row, app, admin, repaint) {
  const isModels = model.section === 'models';
  const labels = isModels ? MODEL_STATE_LABELS : STATE_LABELS;
  const state = effective(model, app, row.key);
  const pending = isPending(model, app, row.key);
  const ignored = !isModels && row.key !== '*' && masterSet(model, app);
  const resolved = isModels && isResolved(app, row.key);
  const outOfRegion = isModels && Array.isArray(row.regions) && row.regions.length > 0 && !row.regions.includes(app.location);
  const impliedOn = isModels && state === 'default' && Boolean(row.defaultOn || row.lockedOn);
  const from = original(app, row.key, model.section);
  const title = pending
    ? t('{feature} in {app}: {from} \u2192 {to} (pending)', { feature: row.label, app: app.displayName, from: stateLabel(model, row, from), to: stateLabel(model, row, state) })
    : (resolved
      ? t('{feature} in {app}: {state} (active in web app)', { feature: row.label, app: app.displayName, state: stateLabel(model, row, state) })
      : t('{feature} in {app}: {state}', { feature: row.label, app: app.displayName, state: stateLabel(model, row, state) }));

  const content = [
    stateMark(state, { impliedOn }),
    resolved ? h('span', { class: 'fm-live-dot', 'aria-hidden': 'true' }) : null,
    pending ? h('span', { class: 'fm-cell-dot', 'aria-hidden': 'true' }) : null,
  ];
  const cls = ['fm-cell', state, pending ? 'pending' : '', ignored ? 'ignored' : ''];
  if (!admin) return h('td', { class: 'fm-cell-td' }, h('span', { class: cls, title, role: 'img', 'aria-label': title }, content));
  const btn = h('button', {
    type: 'button', class: cls, title, 'aria-label': title, 'aria-haspopup': 'menu',
    onclick: (e) => openPopover(e.currentTarget, (close) => h('div', { role: 'menu' },
      h('div', { class: 'fm-pop-head' },
        h('div', { class: 'fm-pop-title' }, row.label),
        h('div', { class: 'fm-pop-sub' }, `${app.displayName} \u00b7 ${app.location}`)),
      STATES.map((s) => menuItem({
        label: s === 'default' ? stateLabel(model, row, s) : t(labels[s]),
        desc: s === from ? t('Current value') : null,
        iconName: STATE_ICONS[s],
        checked: s === state,
        onClick: () => { close(); stage(model, app, row.key, s); repaint(); },
      })),
      resolved ? h('div', { class: 'fm-pop-info' }, t('Currently available in the web app model selector.')) : null,
      isModels && row.lockedOn ? popNote(t('Always on by default in Gemini Enterprise (cannot be turned off).')) : null,
      outOfRegion ? popNote(t('Supported natively in: {regions}. Enabling in {loc} may route traffic to global.', { regions: row.regions.join(', '), loc: app.location })) : null,
      isModels && row.partner ? popNote(t('Third-party models require PAYG overage and one-time Terms of Service acceptance in Google Cloud Console.')) : null,
      isModels && selectorDisabled(model, app) ? popNote(t('The model-selector feature switch is Off for this app.')) : null,
      ignored ? popNote(t('Ignored while the master switch of this app is set.')) : null)),
  }, content);
  return h('td', { class: 'fm-cell-td' }, btn);
}

function popNote(text) {
  return h('div', { class: 'fm-pop-note' }, icon('alert-triangle', { size: 12 }), h('span', null, text));
}

function copyBaseline(model, source, apps, sectionRows) {
  apps.forEach((target) => {
    if (target.key === source.key) return;
    sectionRows.forEach((r) => stage(model, target, r.key, effective(model, source, r.key)));
  });
}

/* ------------------------------------------------------------------ apply */

function paintActionBar(ctx, model, view, repaint) {
  const n = totalPendingCount(model);
  const appCount = totalPendingApps(model);
  view.bar.hidden = !n;
  if (!n) { mount(view.bar); return; }
  const tooMany = appCount > MAX_APPS_PER_APPLY;
  const fCount = pendingCountOf(model.pending);
  const mCount = pendingCountOf(model.pendingModels);
  const detail = fCount && mCount
    ? ` (${t('{f} features · {m} models', { f: fmt.int(fCount), m: fmt.int(mCount) })})`
    : '';
  mount(view.bar,
    h('span', { class: 'fm-actionbar-count', 'aria-hidden': 'true' }, fmt.int(n)),
    h('span', { class: 'fm-actionbar-text' }, t('{n} changes in {apps} apps', { n: fmt.int(n), apps: fmt.int(appCount) }) + detail),
    tooMany ? badge(t('At most {n} apps per apply', { n: MAX_APPS_PER_APPLY }), 'danger', { size: 'sm' }) : null,
    button({ label: t('Discard'), variant: 'ghost', size: 'sm', onClick: () => { model.pending.clear(); model.pendingModels.clear(); repaint(); } }),
    button({ label: t('Review and apply'), variant: 'primary', size: 'sm', disabled: tooMany, onClick: () => openReview(ctx, model) }));
}

function buildChanges(model) {
  const out = [];
  model.apps.forEach((app) => {
    const pf = model.pending.get(app.key);
    const pm = model.pendingModels.get(app.key);
    if ((!pf || !pf.size) && (!pm || !pm.size)) return;
    const change = { project: app.project, location: app.location, engineId: app.engineId };
    if (pf && pf.size) {
      const set = {};
      pf.forEach((state, key) => { set[key] = state; });
      change.set = set;
    }
    if (pm && pm.size) {
      const setModels = {};
      pm.forEach((state, key) => { setModels[key] = state; });
      change.setModels = setModels;
    }
    out.push({ app, change });
  });
  return out;
}

function stateBadge(state, isModels = false) {
  const labels = isModels ? MODEL_STATE_LABELS : STATE_LABELS;
  return h('span', { class: 'fm-state' }, stateMark(state, { small: true }), t(labels[state]));
}

function labelOf(model, key, isModels = false) {
  const list = isModels ? model.modelRows : model.rows;
  const row = list.find((r) => r.key === key);
  return row ? row.label : key;
}

function diffList(model, app, change) {
  const items = [];
  Object.entries(change.set || {}).forEach(([key, to]) => {
    items.push(h('li', null,
      h('span', { class: 'fm-diff-name' }, labelOf(model, key, false), h('span', { class: 'fm-feature-key' }, key)),
      h('span', { class: 'fm-diff-states' }, stateBadge(original(app, key, 'features'), false), icon('arrow-right', { size: 13 }), stateBadge(to, false))));
  });
  Object.entries(change.setModels || {}).forEach(([key, to]) => {
    items.push(h('li', null,
      h('span', { class: 'fm-diff-name' },
        h('span', { class: 'fm-feature-name' }, labelOf(model, key, true), badge(t('Model'), 'info', { size: 'sm' })),
        h('span', { class: 'fm-feature-key' }, key)),
      h('span', { class: 'fm-diff-states' }, stateBadge(original(app, key, 'models'), true), icon('arrow-right', { size: 13 }), stateBadge(to, true))));
  });
  return h('ul', { class: 'fm-diff' }, items);
}

const RESULT_TONES = { updated: 'success', unchanged: 'neutral', failed: 'danger' };
const RESULT_LABELS = { updated: 'Updated', unchanged: 'Unchanged', failed: 'Failed' };
const ERROR_HINTS = {
  denied: 'The runtime service account needs discoveryengine.engines.update on this project (role AI Control Plane GE Feature Manager or Discovery Engine Editor).',
  invalid: 'Google rejected a feature key, model ID, or state for this app. Check that the key or model is supported for this app, region, and billing configuration.',
  not_found: 'The app no longer exists.',
};

function openReview(ctx, model) {
  const items = buildChanges(model);
  if (!items.length) return;
  const total = items.reduce(
    (n, it) => n + Object.keys(it.change.set || {}).length + Object.keys(it.change.setModels || {}).length,
    0,
  );
  const touchesMaster = items.some((it) => it.change.set && '*' in it.change.set);
  const enablesPartner = items.some((it) => Object.entries(it.change.setModels || {}).some(([k, v]) => {
    const r = model.modelRows.find((row) => row.key === k);
    return v === 'on' && ((r && r.partner) || k.startsWith('claude-'));
  }));
  let applied = false;
  const m = openModal({
    title: t('Review changes'),
    subtitle: t('{n} changes in {apps} apps', { n: fmt.int(total), apps: fmt.int(items.length) }),
    wide: true,
    onClose: () => { if (applied) ctx.rerender(); },
    body: (el) => append(el, [
      banner({
        tone: 'info',
        title: t('Only the listed settings change'),
        text: t('Each app is read again right before the update, and only these feature switches and model overrides are merged into its current settings. Every apply is written to the governance audit log.'),
      }),
      touchesMaster ? banner({ tone: 'warning', title: t('The master switch changes'), text: t('While the master switch is set, every other switch of that app is ignored.') }) : null,
      enablesPartner ? banner({
        tone: 'warning',
        title: t('Third-party model prerequisites'),
        text: t('Enabling Anthropic Claude models requires Pay-as-you-go usage above quota and one-time Terms of Service acceptance per project in Google Cloud Console. Without both, Google Cloud may reject or withhold the model.'),
        actions: [h('a', { class: 'btn btn-secondary btn-sm', href: CONSOLE_GE, target: '_blank', rel: 'noopener noreferrer' }, t('Open Cloud Console'), ' ', icon('external', { size: 12 }))],
      }) : null,
      h('div', { class: 'fm-review' }, items.map(({ app, change }) => {
        const count = Object.keys(change.set || {}).length + Object.keys(change.setModels || {}).length;
        return h('section', { class: 'fm-review-app' },
          h('div', { class: 'fm-review-head' },
            h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main' }, app.displayName), h('span', { class: 'cell-sub cell-mono' }, `${app.project} \u00b7 ${app.location} \u00b7 ${app.engineId}`)),
            badge(t('{n} changes', { n: count }), 'accent', { size: 'sm' })),
          diffList(model, app, change));
      }))]),
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
            toast({ title: t('Changes were not applied'), text: errorText(err), tone: 'danger' });
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
      title: d.failed ? t('Some apps were not updated') : t('Changes applied'),
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
  model.pendingModels.clear();
  toast({ title: d.failed ? t('Some apps were not updated') : t('Changes applied'), tone: d.failed ? 'warning' : 'success' });
}

/* ------------------------------------------------------------------ export */

function exportCsv(model) {
  const isModels = model.section === 'models';
  const apps = visibleApps(model);
  const rows = isModels ? visibleModelRows(model, apps) : visibleFeatureRows(model, apps);
  const columns = [
    { key: 'label', label: isModels ? t('Model') : t('Feature') },
    { key: 'key', label: t('Key') },
  ].concat(apps.map((a) => ({
    key: a.key,
    label: `${a.displayName} (${a.key})`,
    csv: (r) => effective(model, a, r.key),
  })));
  const prefix = isModels ? 'gemini-enterprise-models' : 'gemini-enterprise-features';
  downloadText(`${prefix}-${new Date().toISOString().slice(0, 10)}.csv`, toCsv(columns, rows));
}
