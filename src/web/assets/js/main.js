/**
 * AI Control Plane - application shell and router.
 */
import { h, mount, append } from './core/dom.js';
import { icon } from './core/icons.js';
import { t, setLocale, getLocale } from './core/i18n.js';
import { fmt, setTimezone, parseDate } from './core/format.js';
import { api, setCsrfToken, setUnauthenticatedHandler, setForbiddenHandler, isAbort } from './core/api.js';
import {
  store, on, initState, setRange, setGranularity, setProject, setModel, isModuleEnabled, session, isAdmin, PRESETS, computeRange, savePrefs,
} from './core/state.js';
import {
  button, iconButton, segmented, openPopover, closePopover, menuItem, badge, banner, toast, initTooltips, statusBadge, errorState,
  alertPermissionRequired, requireAdmin,
} from './core/components.js';
import { errorText, serverText } from './core/messages.js';
import { applyAccent, brandMark as brandMarkFor, accountAvatar } from './core/brand.js';
import { widgetBoard } from './core/widgets.js';
import { openAccessDrawer } from './pages/settings.js';

export const ROUTES = [
  { path: 'overview', label: 'Overview', icon: 'grid', module: 'overview', group: null, range: true, load: () => import('./pages/overview.js') },
  { path: 'gemini-enterprise', label: 'Gemini Enterprise', icon: 'briefcase', module: 'gemini_enterprise', group: 'Workspace', range: true, load: () => import('./pages/gemini.js') },
  { path: 'notebooklm', label: 'NotebookLM', icon: 'book', module: 'notebooklm', group: 'Workspace', range: true, load: () => import('./pages/notebooklm.js') },
  { path: 'gateway', label: 'Agent Gateway', icon: 'share', module: 'agent_gateway', group: 'Agent Platform', range: true, load: () => import('./pages/gateway.js') },
  { path: 'registry', label: 'Agent Registry', icon: 'box', module: 'agent_registry', group: 'Agent Platform', range: true, load: () => import('./pages/registry.js') },
  { path: 'deployments', label: 'Deployments', icon: 'server', module: 'agent_deployments', group: 'Agent Platform', range: true, load: () => import('./pages/deployments.js') },
  { path: 'model-armor', label: 'Model Armor', icon: 'shield', module: 'model_armor', group: 'Agent Platform', range: true, load: () => import('./pages/armor.js') },
  { path: 'models', label: 'Models & Tokens', icon: 'layers', module: 'model_usage', group: 'Models & Cost', range: true, load: () => import('./pages/models.js') },
  { path: 'finops', label: 'Cost & FinOps', icon: 'card', module: 'finops', group: 'Models & Cost', range: true, load: () => import('./pages/finops.js') },
  { path: 'users', label: 'Users & Access', icon: 'users', module: 'users', group: 'Governance', range: true, load: () => import('./pages/users.js') },
  { path: 'antigravity', label: 'Antigravity Controls', icon: 'sliders', module: 'antigravity', group: 'Governance', range: false, load: () => import('./pages/antigravity.js') },
  { path: 'extensions', label: 'Telemetry Extensions', icon: 'plug', module: 'telemetry', group: 'Extend', range: true, load: () => import('./pages/telemetry.js') },
  { path: 'analyst', label: 'AI Analyst', icon: 'spark', module: 'analyst', group: 'Extend', range: false, load: () => import('./pages/analyst.js') },
  { path: 'settings', label: 'Settings', icon: 'settings', module: null, group: null, range: false, footer: true, load: () => import('./pages/settings.js') },
];

const PRESET_LABELS = {
  '7d': 'Last 7 days', '14d': 'Last 14 days', '30d': 'Last 30 days', '90d': 'Last 90 days', mtd: 'Month to date', prev_month: 'Previous month', custom: 'Custom range',
};

const app = document.getElementById('app');
const shell = {};
let current = { route: null, controller: null, cleanup: null };
let healthState = null;
let aiModels = null;

/* ------------------------------------------------------------------ theme */

const THEME_KEY = 'cp.theme';

function themeMeta() {
  return document.querySelector('meta[name="color-scheme"]');
}

function storedTheme() {
  try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
}

function currentTheme() {
  const v = storedTheme();
  return v === 'light' || v === 'dark' ? v : 'system';
}

function applyTheme(mode, persist = true) {
  const meta = themeMeta();
  if (mode === 'light' || mode === 'dark') {
    meta.content = mode;
    if (persist) { try { localStorage.setItem(THEME_KEY, mode); } catch (e) { /* ignore */ } }
  } else {
    meta.content = 'light dark';
    if (persist) { try { localStorage.removeItem(THEME_KEY); } catch (e) { /* ignore */ } }
  }
}

window.addEventListener('storage', (e) => {
  if (e.key === THEME_KEY) applyTheme(e.newValue === 'light' || e.newValue === 'dark' ? e.newValue : 'system', false);
});

/* ------------------------------------------------------------------ boot */

function fatal(title, text, retry = true) {
  app.className = '';
  app.removeAttribute('aria-busy');
  mount(app, h('div', { class: 'fatal' },
    h('div', { class: 'boot-mark' }),
    h('h1', null, title),
    h('p', null, text),
    retry ? button({ label: t('Reload'), icon: 'refresh', variant: 'primary', onClick: () => window.location.reload() }) : null));
}

let redirectingToSignIn = false;

/** Full navigation to the sign-in page; the current screen becomes the post-sign-in destination. */
function goToSignIn(reason) {
  if (redirectingToSignIn) return;
  redirectingToSignIn = true;
  const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  const params = new URLSearchParams({ next });
  if (reason) params.set('reason', reason);
  window.location.assign(`/login?${params.toString()}`);
}

async function boot() {
  initTooltips();
  setUnauthenticatedHandler(() => goToSignIn('session'));
  setForbiddenHandler((err) => alertPermissionRequired({ reason: err && err.message }));
  let bootRes;
  try {
    bootRes = await api.get('/api/v1/bootstrap');
  } catch (err) {
    if (err.status === 401) return goToSignIn(null);
    return fatal(t('Unable to load the workspace'), errorText(err) || t('The server could not be reached.'));
  }
  const data = bootRes.data;
  setCsrfToken(data.session && data.session.csrfToken);
  initState(data);
  setLocale(store.locale);
  setTimezone(data.workspace && data.workspace.timezone);
  applyAccent(data.branding && data.branding.accentColor);
  if (currentTheme() === 'system' && data.branding && ['light', 'dark'].includes(data.branding.defaultTheme)) {
    applyTheme(data.branding.defaultTheme, false);
  }
  buildShell();
  window.addEventListener('hashchange', route);
  on('range', () => { updateRangeControl(); if (current.route && current.route.range) renderPage(); });
  on('project', () => { renderWorkspaceChip(); });
  document.addEventListener('keydown', onGlobalKey);
  route();
  loadHealth(false);
}

/* ------------------------------------------------------------------ shell */

function productName() {
  return (store.boot.product && store.boot.product.name) || 'AI Control Plane';
}

function orgName() {
  return (store.boot.branding && store.boot.branding.organizationName) || '';
}

function brandMark() {
  return brandMarkFor(store.boot.branding);
}

function buildShell() {
  app.className = ['app', store.sidebarCollapsed ? 'sidebar-collapsed' : ''].join(' ');
  app.removeAttribute('aria-busy');
  shell.wsSlot = h('div', { class: 'ws-slot' });
  shell.nav = h('nav', { class: 'nav', 'aria-label': t('Primary') });
  shell.sidebar = h('aside', { class: 'sidebar' },
    h('div', { class: 'sidebar-brand' }, brandMark(),
      h('div', { class: 'brand-text' }, h('span', { class: 'brand-name' }, productName()), h('span', { class: 'brand-org' }, orgName()))),
    shell.wsSlot,
    shell.nav,
    shell.navFooter = h('div', { class: 'sidebar-footer' }));
  shell.crumbs = h('div', { class: 'crumbs' });
  shell.rangeSlot = h('div', { class: 'row' });
  shell.updated = h('span', { class: ['updated-at', 'hide-mobile'] });
  shell.healthBtn = h('button', { type: 'button', class: ['control', 'hide-mobile'], onclick: (e) => openHealth(e.currentTarget), 'aria-label': t('Data source health') },
    h('span', { class: 'dot' }), h('span', null, t('Data sources')));
  shell.modelBtn = h('button', { type: 'button', class: ['control', 'mono-value', 'hide-mobile'], onclick: (e) => openModelMenu(e.currentTarget), title: t('AI engine used for analysis') },
    icon('spark', { size: 14 }), h('span', { class: 'control-value' }), h('span', { class: 'chev' }, icon('chevron-down', { size: 13 })));
  shell.topbar = h('header', { class: 'topbar' },
    h('div', { class: 'topbar-left' },
      iconButton('menu', { title: t('Open navigation'), onClick: () => app.classList.toggle('mobile-nav-open') }),
      shell.crumbs),
    h('div', { class: 'topbar-right' },
      shell.rangeSlot,
      shell.updated,
      iconButton('refresh', { title: t('Refresh data'), onClick: () => renderPage(true) }),
      h('span', { class: ['topbar-divider', 'hide-mobile'] }),
      button({
        label: t('Add account'),
        icon: 'plus',
        size: 'sm',
        cls: 'hide-mobile',
        onClick: () => {
          if (!requireAdmin(t('Add account / Manage access control'))) return;
          openAccessDrawer({ onChanged: () => { if (current.route && (current.route.path === 'users' || current.route.path === 'settings')) renderPage(); } });
        },
      }),
      shell.healthBtn,
      iconButton('search', { title: `${t('Search and commands')} (Ctrl K)`, onClick: openPalette }),
      h('button', { type: 'button', class: 'avatar-btn', onclick: (e) => openUserMenu(e.currentTarget), 'aria-label': t('Account menu') },
        accountAvatar(displaySession()))));
  shell.banners = h('div', { class: 'banner-region' });
  shell.content = h('main', { class: 'content', id: 'main', tabindex: '-1' });
  shell.main = h('div', { class: 'main' }, shell.topbar, shell.banners, shell.content);
  const scrim = h('div', { class: 'nav-scrim', 'aria-hidden': 'true', onclick: () => app.classList.remove('mobile-nav-open') });
  mount(app, shell.sidebar, scrim, shell.main);
  if (!shell.navKeyBound) {
    shell.navKeyBound = true;
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') app.classList.remove('mobile-nav-open'); });
  }
  renderWorkspaceChip();
  renderNav();
  renderModelButton();
  renderBanners();
  updateHealthButton();
}

function renderWorkspaceChip() {
  if (!shell.wsSlot) return;
  mount(shell.wsSlot, workspaceChip());
}

function workspaceChip() {
  const ws = store.boot.workspace || {};
  if (!ws.projectId) return null;
  const projects = Array.isArray(ws.projects) && ws.projects.length ? ws.projects : [ws.projectId];
  const sel = store.project && store.project !== 'all' && projects.includes(store.project) ? store.project : 'all';
  const activeTitle = sel === 'all'
    ? (projects.length > 1 ? t('All connected projects ({n})', { n: projects.length }) : ws.projectId)
    : sel;
  const activeSub = sel === 'all'
    ? projects.slice(0, 2).join(', ') + (projects.length > 2 ? ` +${projects.length - 2}` : '')
    : (sel === ws.projectId ? t('Primary workspace project') : t('Filtered to single project'));

  return h('button', {
    type: 'button',
    class: ['workspace-chip', sel !== 'all' ? 'filtered' : ''],
    'aria-haspopup': 'dialog',
    'aria-expanded': 'false',
    title: `${t('Connected projects ({n})', { n: projects.length })}: ${projects.join(', ')}`,
    onclick: (e) => openProjectMenu(e.currentTarget, ws, projects),
  },
    h('div', { class: 'ws-head' },
      h('span', { class: 'ws-label-row' },
        h('span', { class: 'dot dot-success' }),
        h('span', { class: 'ws-label' }, t('Connected projects ({n})', { n: projects.length }))),
      h('span', { class: 'ws-chev' }, icon('chevron-down', { size: 14 }))),
    h('div', { class: 'ws-value' }, activeTitle),
    h('div', { class: 'ws-sub' }, activeSub));
}

function openProjectMenu(anchor, ws, projects) {
  openPopover(anchor, (close) => {
    const sel = store.project && store.project !== 'all' && projects.includes(store.project) ? store.project : 'all';
    const selectProj = (p) => {
      close();
      setProject(p);
      toast({
        title: p === 'all' ? t('Viewing all connected projects ({n})', { n: projects.length }) : t('Filtered to project: {id}', { id: p }),
        text: p === 'all' ? projects.join(', ') : t('Showing metrics and resources for {id}', { id: p }),
        tone: 'info',
        timeout: 2400,
      });
    };
    return h('div', { class: 'ws-popover' },
      h('div', { class: 'ws-pop-header' },
        h('div', null,
          h('div', { class: 'ws-pop-title' }, t('Connected Google Cloud projects ({n})', { n: projects.length })),
          h('div', { class: 'ws-pop-sub' }, t('Select a project to filter or view all connected projects together.'))),
        ws.scanAllProjects !== false ? badge(t('Auto-discovery'), 'success', { size: 'sm', dot: true }) : null),
      h('div', { class: 'ws-project-list' },
        h('button', {
          type: 'button',
          class: ['ws-proj-item', sel === 'all' ? 'active' : ''],
          onclick: () => selectProj('all'),
        },
          h('span', { class: 'ws-proj-icon' }, icon('layers', { size: 15 })),
          h('div', { class: 'ws-proj-body' },
            h('div', { class: 'ws-proj-name' }, t('All connected projects ({n})', { n: projects.length })),
            h('div', { class: 'ws-proj-desc' }, t('Unified governance view across all {n} projects', { n: projects.length }))),
          sel === 'all' ? h('span', { class: 'ws-proj-check' }, icon('check', { size: 15 })) : null),
        h('div', { class: 'menu-sep' }),
        h('div', { class: 'menu-label' }, t('Project list ({n})', { n: projects.length })),
        projects.map((p) => {
          const isPrimary = p === ws.projectId;
          const isSelected = sel === p;
          return h('button', {
            type: 'button',
            class: ['ws-proj-item', isSelected ? 'active' : ''],
            onclick: () => selectProj(p),
          },
            h('span', { class: ['dot', isPrimary ? 'dot-info' : 'dot-success'] }),
            h('div', { class: 'ws-proj-body' },
              h('div', { class: 'ws-proj-row' },
                h('span', { class: 'mono fw-600 ws-proj-id' }, p),
                badge(isPrimary ? t('Primary') : t('Connected'), isPrimary ? 'info' : 'neutral', { size: 'sm' }))),
            isSelected ? h('span', { class: 'ws-proj-check' }, icon('check', { size: 15 })) : null);
        })),
      h('div', { class: 'ws-pop-footer' },
        button({
          label: t('Manage connected projects'),
          icon: 'settings',
          size: 'sm',
          cls: 'btn-block',
          onClick: () => { close(); window.location.hash = '#/settings/workspace'; },
        })));
  }, { align: 'start', wide: true });
}

function visibleRoutes() {
  return ROUTES.filter((r) => !r.module || isModuleEnabled(r.module));
}

function renderNav() {
  const groups = [];
  for (const r of visibleRoutes().filter((x) => !x.footer)) {
    let g = groups.find((x) => x.name === r.group);
    if (!g) { g = { name: r.group, items: [] }; groups.push(g); }
    g.items.push(r);
  }
  const active = current.route ? current.route.path : null;
  const link = (r) => h('a', {
    class: ['nav-item', r.path === active ? 'active' : ''],
    href: `#/${r.path}`,
    title: t(r.label),
    'aria-current': r.path === active ? 'page' : null,
    onclick: () => app.classList.remove('mobile-nav-open'),
  }, h('span', { class: 'nav-icon' }, icon(r.icon, { size: 17 })), h('span', { class: 'nav-label' }, t(r.label)));
  mount(shell.nav, groups.map((g) => h('div', { class: 'nav-group' },
    g.name ? h('div', { class: 'nav-group-label' }, t(g.name)) : null,
    g.items.map(link))));
  mount(shell.navFooter,
    ROUTES.filter((r) => r.footer).map(link),
    h('button', {
      type: 'button', class: ['nav-item', 'hide-mobile'], title: t('Collapse sidebar'),
      onclick: () => {
        store.sidebarCollapsed = !store.sidebarCollapsed;
        app.classList.toggle('sidebar-collapsed', store.sidebarCollapsed);
        savePrefs();
      },
    }, h('span', { class: 'nav-icon' }, icon('sidebar', { size: 17 })), h('span', { class: 'nav-label' }, t('Collapse sidebar'))));
}

function renderBanners() {
  const list = [];
  const ws = store.boot.workspace || {};
  if (!ws.configured) {
    list.push(banner({
      tone: 'warning',
      title: t('Workspace not configured'),
      text: t('Connect a Google Cloud project, the audit log dataset and the billing export table to start collecting governance data.'),
      actions: button({ label: t('Configure'), size: 'sm', variant: 'primary', onClick: () => { if (!requireAdmin(t('Configure workspace'))) return; window.location.hash = '#/settings/workspace'; } }),
    }));
  }
  const s = session();
  if (s.piiMasked) {
    list.push(banner({ tone: 'neutral', iconName: 'eye-off', title: t('Viewer mode'), text: t('Personal data such as user emails and prompt text is masked for your role.') }));
  }
  mount(shell.banners, list);
}

function renderCrumbs(r) {
  mount(shell.crumbs,
    r.group ? h('span', { class: 'crumb-group' }, t(r.group)) : null,
    r.group ? h('span', { class: 'crumb-sep' }, icon('chevron-right', { size: 13 })) : null,
    h('span', { class: 'crumb-page' }, t(r.label)));
}

/* ------------------------------------------------------------------ date range */

function rangeLabel() {
  const r = store.range;
  if (r.preset !== 'custom') return t(PRESET_LABELS[r.preset]);
  return fmt.dateRange(r.start, r.end);
}

function updateRangeControl() {
  if (!current.route || !current.route.range) { mount(shell.rangeSlot); return; }
  const btn = h('button', {
    type: 'button', class: 'control', 'aria-haspopup': 'dialog', title: fmt.dateRange(store.range.start, store.range.end),
    onclick: (e) => openRangePicker(e.currentTarget),
  }, icon('calendar', { size: 14 }), h('span', null, rangeLabel()), h('span', { class: ['control-label', 'hide-mobile'] }, store.range.preset === 'custom' ? null : fmt.dateRange(store.range.start, store.range.end)), h('span', { class: 'chev' }, icon('chevron-down', { size: 13 })));
  const gran = segmented([{ value: 'day', label: t('Daily') }, { value: 'week', label: t('Weekly') }], store.granularity, (v) => setGranularity(v), { label: t('Granularity') });
  gran.classList.add('hide-mobile');
  mount(shell.rangeSlot, btn, gran);
}

function openRangePicker(anchor) {
  openPopover(anchor, (close) => {
    const startIn = h('input', { class: 'input', type: 'date', value: store.range.start, max: store.boot.workspace.today, 'aria-label': t('Start date') });
    const endIn = h('input', { class: 'input', type: 'date', value: store.range.end, max: store.boot.workspace.today, 'aria-label': t('End date') });
    const err = h('div', { class: 'field-error' });
    const apply = () => {
      const s = startIn.value;
      const e = endIn.value;
      if (!parseDate(s) || !parseDate(e) || s > e) { err.textContent = t('Choose a valid start and end date.'); return; }
      const span = (parseDate(e) - parseDate(s)) / 86400000;
      if (span > 399) { err.textContent = t('The maximum range is 400 days.'); return; }
      close();
      setRange('custom', s, e);
    };
    return h('div', { class: 'popover-body' },
      h('div', { class: 'menu-label' }, t('Quick ranges')),
      h('div', { class: 'preset-list' }, PRESETS.map((p) => {
        const r = computeRange(p);
        return menuItem({ label: t(PRESET_LABELS[p]), desc: fmt.dateRange(r.start, r.end), checked: store.range.preset === p, onClick: () => { close(); setRange(p); } });
      })),
      h('div', { class: 'menu-sep' }),
      h('div', { class: 'menu-label' }, t('Custom range')),
      h('div', { class: 'date-inputs' }, startIn, endIn),
      err,
      button({ label: t('Apply range'), variant: 'primary', size: 'sm', cls: 'btn-block', onClick: apply }));
  }, { wide: true });
}

/* ------------------------------------------------------------------ health */

async function loadHealth(force) {
  try {
    const res = await api.get('/api/v1/health', force ? { refresh: 1 } : null);
    healthState = res.data.checks || [];
  } catch (e) {
    healthState = [{ id: 'server', label: t('Control plane server'), status: 'error', detail: errorText(e) }];
  }
  updateHealthButton();
}

function healthTone() {
  if (!healthState) return 'neutral';
  if (healthState.some((c) => c.status === 'error')) return 'danger';
  if (healthState.some((c) => c.status === 'warning')) return 'warning';
  return 'success';
}

function updateHealthButton() {
  if (!shell.healthBtn) return;
  const d = shell.healthBtn.querySelector('.dot');
  d.className = `dot dot-${healthTone()}`;
}

function openHealth(anchor) {
  openPopover(anchor, (close) => {
    const list = h('div', { class: 'checklist' });
    const renderList = () => {
      if (!healthState) { mount(list, h('div', { class: 'loading-row' }, h('span', { class: 'spinner' }), t('Running checks'))); return; }
      mount(list, healthState.map((c) => h('div', { class: 'check-item' },
        h('span', { class: ['ci-icon', c.status === 'ok' ? 'ok' : c.status === 'error' ? 'no' : 'warn'] },
          icon(c.status === 'ok' ? 'check-circle' : c.status === 'error' ? 'x-circle' : c.status === 'unconfigured' ? 'minus' : 'alert-triangle', { size: 15 })),
        h('div', { class: 'ci-text' },
          h('div', null, t(c.label || c.id)),
          c.detail ? h('div', { class: 'ci-sub' }, serverText(c.detail)) : null),
        typeof c.latencyMs === 'number' ? h('span', { class: 'muted tabular' }, fmt.duration(c.latencyMs)) : null)));
    };
    renderList();
    const rerun = button({
      label: t('Re-run checks'), icon: 'refresh', size: 'sm',
      onClick: async () => { healthState = null; renderList(); await loadHealth(true); renderList(); },
    });
    return h('div', { class: 'popover-body' },
      h('div', { class: 'row between' }, h('strong', null, t('Data source health')), rerun),
      list);
  }, { wide: true });
}

/* ------------------------------------------------------------------ AI engine */

function renderModelButton() {
  const enabled = isModuleEnabled('analyst') || isModuleEnabled('finops');
  shell.modelBtn.hidden = !enabled || !store.model;
  const val = shell.modelBtn.querySelector('.control-value');
  if (val) val.textContent = store.model || '';
}

async function openModelMenu(anchor) {
  if (!aiModels) {
    try {
      const res = await api.get('/api/v1/ai/models');
      aiModels = res.data;
    } catch (e) {
      aiModels = { defaultModel: store.boot.aiEngine.defaultModel, models: (store.boot.aiEngine.models || []).map((id) => ({ id, allowed: true, source: 'configured' })) };
    }
  }
  openPopover(anchor, (close) => h('div', null,
    h('div', { class: 'menu-label' }, t('AI engine')),
    (aiModels.models || []).filter((m) => m.allowed !== false).map((m) => menuItem({
      label: m.id,
      desc: m.id === aiModels.defaultModel ? t('Workspace default') : (m.source === 'discovered' ? t('Discovered in this project') : null),
      checked: store.model === m.id,
      onClick: () => { close(); setModel(m.id); renderModelButton(); toast({ title: t('AI engine switched'), text: m.id, tone: 'success', timeout: 2500 }); },
    })),
    h('div', { class: 'menu-sep' }),
    h('div', { class: 'menu-label' }, t('Used by AI Analyst and FinOps analysis'))), { wide: false });
}

/* ------------------------------------------------------------------ user menu */

function openUserMenu(anchor) {
  const s = displaySession();
  const guest = s.authMode === 'guest';
  const theme = currentTheme();
  const display = s.name || s.email || t('Unknown user');
  openPopover(anchor, (close) => h('div', null,
    h('div', { class: 'menu-header' },
      accountAvatar(s, 'lg'),
      h('div', { class: 'who' }, h('strong', { title: s.email || '' }, display),
        s.name && s.email ? h('span', { title: s.email }, s.email) : null,
        h('span', null, guest ? t('Read-only, identifiers hidden') : `${s.role === 'admin' ? t('Administrator') : t('Viewer')} \u00b7 ${authLabel(s.authMode)}`))),
    h('div', { class: 'menu-label' }, t('Theme')),
    menuItem({ label: t('System'), iconName: 'monitor', checked: theme === 'system', onClick: () => { close(); applyTheme('system'); } }),
    menuItem({ label: t('Light'), iconName: 'sun', checked: theme === 'light', onClick: () => { close(); applyTheme('light'); } }),
    menuItem({ label: t('Dark'), iconName: 'moon', checked: theme === 'dark', onClick: () => { close(); applyTheme('dark'); } }),
    h('div', { class: 'menu-sep' }),
    h('div', { class: 'menu-label' }, t('Language')),
    menuItem({ label: 'English', iconName: 'globe', checked: getLocale() === 'en', onClick: () => { close(); switchLocale('en'); } }),
    menuItem({ label: '\ud55c\uad6d\uc5b4', iconName: 'globe', checked: getLocale() === 'ko', onClick: () => { close(); switchLocale('ko'); } }),
    h('div', { class: 'menu-sep' }),
    menuItem({ label: t('Add account'), iconName: 'plus', onClick: () => { close(); if (!requireAdmin(t('Add account / Manage access control'))) return; openAccessDrawer({ onChanged: () => { if (current.route && (current.route.path === 'users' || current.route.path === 'settings')) renderPage(); } }); } }),
    menuItem({ label: t('Access control'), iconName: 'lock', onClick: () => { close(); window.location.hash = '#/settings/access'; } }),
    menuItem({ label: t('Settings'), iconName: 'settings', onClick: () => { close(); window.location.hash = '#/settings'; } }),
    guest
      ? menuItem({ label: t('Sign in'), iconName: 'arrow-right', onClick: () => { close(); goToSignIn(null); } })
      : (s.authMode === 'iap' || s.authMode === 'google'
        ? menuItem({ label: t('Sign out'), iconName: 'logout', onClick: () => { close(); signOut(); } })
        : null)));
}

/** The session for display: guests have no name or e-mail, so they are labelled "Guest". */
function displaySession() {
  const s = session();
  return s.authMode === 'guest' ? { ...s, name: t('Guest'), email: '', picture: '' } : s;
}

/** Ends the session server-side, then performs a full navigation so no in-memory state survives. */
async function signOut() {
  const s = session();
  if (s.authMode === 'iap') {
    window.location.assign('/_gcp_iap/clear_login_cookie');
    return;
  }
  try {
    redirectingToSignIn = true; // a 401 here means the session already ended; no second redirect
    const res = await api.post('/api/v1/auth/logout', {});
    const target = res && res.data && res.data.redirect;
    const safe = typeof target === 'string' && target.startsWith('/') && !target.startsWith('//') && !target.includes('\\');
    window.location.replace(safe ? target : '/login?signed_out=1');
  } catch (err) {
    window.location.replace(err && err.status === 401 ? '/login?signed_out=1' : '/login?error=signout_partial');
  }
}

function authLabel(mode) {
  return { google: t('Google sign-in'), iap: t('Identity-Aware Proxy'), local: t('Local development') }[mode] || mode || '';
}

function switchLocale(locale) {
  store.locale = locale;
  savePrefs();
  setLocale(locale);
  buildShell();
  route();
}

/* ------------------------------------------------------------------ command palette */

function paletteItems() {
  const items = visibleRoutes().map((r) => ({ label: t(r.label), group: r.group ? t(r.group) : t('Navigation'), iconName: r.icon, run: () => { window.location.hash = `#/${r.path}`; } }));
  const settingsTabs = [['general', 'General'], ['access', 'Access control'], ['workspace', 'Workspace and data sources'], ['modules', 'Modules'], ['models', 'Model catalog'], ['ai', 'AI engine'], ['governance', 'Governance policy'], ['about', 'About']];
  settingsTabs.forEach(([id, label]) => items.push({ label: `${t('Settings')}: ${t(label)}`, group: t('Settings'), iconName: 'settings', run: () => { window.location.hash = `#/settings/${id}`; } }));
  if (isAdmin()) {
    items.push({ label: `${t('Add account')} (${t('Approved accounts')})`, group: t('Actions'), iconName: 'plus', run: () => openAccessDrawer() });
  }
  items.push({ label: t('Refresh data'), group: t('Actions'), iconName: 'refresh', run: () => renderPage(true) });
  items.push({ label: t('Use light theme'), group: t('Actions'), iconName: 'sun', run: () => applyTheme('light') });
  items.push({ label: t('Use dark theme'), group: t('Actions'), iconName: 'moon', run: () => applyTheme('dark') });
  items.push({ label: t('Follow system theme'), group: t('Actions'), iconName: 'monitor', run: () => applyTheme('system') });
  items.push({ label: getLocale() === 'ko' ? 'Switch to English' : '\ud55c\uad6d\uc5b4\ub85c \uc804\ud658', group: t('Actions'), iconName: 'globe', run: () => switchLocale(getLocale() === 'ko' ? 'en' : 'ko') });
  return items;
}

function openPalette() {
  closePopover();
  if (document.querySelector('dialog.palette')) return;
  const input = h('input', { type: 'text', placeholder: t('Search pages, settings and actions'), 'aria-label': t('Command search'), autocomplete: 'off' });
  const list = h('div', { class: 'palette-list', role: 'listbox' });
  const all = paletteItems();
  let filtered = all;
  let sel = 0;
  const dialog = h('dialog', { class: 'palette', closedby: 'any', 'aria-label': t('Command palette') },
    h('div', { class: 'palette-input' }, icon('search', { size: 16 }), input, h('kbd', null, 'Esc')),
    list,
    h('div', { class: 'palette-footer' }, h('span', null, h('kbd', null, '\u2191'), ' ', h('kbd', null, '\u2193'), ' ', t('to navigate')), h('span', null, h('kbd', null, 'Enter'), ' ', t('to select'))));
  const render = () => {
    mount(list, filtered.length ? filtered.map((it, i) => {
      const row = h('div', { class: 'palette-item', role: 'option', 'aria-selected': i === sel ? 'true' : 'false', onclick: () => { dialog.close(); it.run(); } },
        h('span', { class: 'pi-icon' }, icon(it.iconName, { size: 15 })), h('span', null, it.label), h('span', { class: 'pi-group' }, it.group));
      row.addEventListener('mousemove', () => { if (sel !== i) { sel = i; render(); } });
      return row;
    }) : h('div', { class: 'table-empty' }, t('No matching commands')));
    const active = list.querySelector('[aria-selected="true"]');
    if (active) active.scrollIntoView({ block: 'nearest' });
  };
  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    filtered = q ? all.filter((it) => `${it.label} ${it.group}`.toLowerCase().includes(q)) : all;
    sel = 0;
    render();
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(filtered.length - 1, sel + 1); render(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(0, sel - 1); render(); }
    if (e.key === 'Enter' && filtered[sel]) { e.preventDefault(); const it = filtered[sel]; dialog.close(); it.run(); }
  });
  if (!('closedBy' in HTMLDialogElement.prototype)) {
    dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });
  }
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  render();
  dialog.showModal();
  input.focus();
}

function onGlobalKey(e) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    openPalette();
  }
}

/* ------------------------------------------------------------------ router */

/** Pages that show the personal widget board below their content (the Overview places its own board). */
const WIDGET_PAGES = new Set(['gemini-enterprise', 'notebooklm', 'gateway', 'registry', 'deployments', 'model-armor', 'models',
  'finops', 'users', 'antigravity']);

function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const [pathPart, queryPart] = raw.split('?');
  const segs = pathPart.split('/').filter(Boolean).map((x) => decodeURIComponent(x));
  return { path: segs[0] || 'overview', params: segs.slice(1), query: new URLSearchParams(queryPart || '') };
}

function route() {
  const { path } = parseHash();
  let r = ROUTES.find((x) => x.path === path);
  if (!r || (r.module && !isModuleEnabled(r.module))) {
    r = visibleRoutes()[0] || ROUTES.find((x) => x.path === 'settings');
    if (window.location.hash !== `#/${r.path}`) {
      window.history.replaceState(null, '', `#/${r.path}`);
    }
  }
  current.route = r;
  renderNav();
  renderCrumbs(r);
  updateRangeControl();
  renderModelButton();
  document.title = `${t(r.label)} \u00b7 ${productName()}`;
  renderPage();
}

async function renderPage(force = false) {
  const r = current.route;
  if (!r) return;
  if (current.controller) current.controller.abort();
  if (typeof current.cleanup === 'function') { try { current.cleanup(); } catch (e) { /* ignore */ } }
  current.cleanup = null;
  const controller = new AbortController();
  current.controller = controller;
  closePopover();
  const { params, query } = parseHash();
  const container = h('div', { class: 'page' });
  // Personal widgets live next to the page (not inside it), so page re-renders never remove them.
  const widgetsHost = h('div', { class: 'page-widgets' });
  mount(shell.content, h('div', { class: 'page-wrap' }, container, widgetsHost));
  if (shell.updated) shell.updated.textContent = `${t('Loading')}\u2026`;
  try {
    const mod = await r.load();
    if (controller.signal.aborted) return;
    const ctx = {
      root: container,
      params,
      query,
      signal: controller.signal,
      refresh: force,
      navigate: (hash) => { window.location.hash = hash; },
      rerender: () => renderPage(),
      onBootChange: (patch) => applyBootPatch(patch),
      reloadBootstrap: () => reloadBootstrap(),
      refreshHealth: () => loadHealth(true),
    };
    current.cleanup = await mod.default(ctx);
    if (WIDGET_PAGES.has(r.path) && !params.length && !controller.signal.aborted) {
      const board = widgetBoard({ page: r.path, signal: controller.signal, navigate: ctx.navigate });
      if (board) mount(widgetsHost, board);
    }
    store.lastUpdated = new Date();
    shell.updated.textContent = t('Updated {time}', { time: fmt.time(store.lastUpdated.toISOString()) });
  } catch (err) {
    if (isAbort(err)) return;
    if (shell.updated) shell.updated.textContent = '';
    mount(container, errorState(err, { onRetry: () => renderPage() }));
  }
}

/** Settings pages call this after saving so the shell reflects new branding / modules. */
function applyBootPatch(patch) {
  if (!patch) return;
  Object.assign(store.boot, patch);
  if (patch.branding) applyAccent(patch.branding.accentColor);
  if (patch.workspace && patch.workspace.timezone) setTimezone(patch.workspace.timezone);
  aiModels = null;
  buildShell();
  renderNav();
  if (current.route) { renderCrumbs(current.route); updateRangeControl(); }
}

export function reloadBootstrap() {
  return api.get('/api/v1/bootstrap').then((res) => {
    setCsrfToken(res.data.session && res.data.session.csrfToken);
    store.boot = res.data;
    applyBootPatch(res.data);
    return res.data;
  });
}

boot();
