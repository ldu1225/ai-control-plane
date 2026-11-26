/**
 * Gemini Enterprise AI developer tools & Antigravity model selection (#/gemini-enterprise/dev-tools).
 *
 * Reads live project/location-level `DevToolsConfig` resources via Discovery Engine v1alpha
 * (`GET /api/v1/gemini-enterprise/dev-tools`) across every selected workspace project and location
 * (global, us, eu, and any configured Discovery Engine locations):
 *   - Antigravity authorized Google (Gemini) models (`modelConfig.allowedModels`) and third-party
 *     partner models (`modelConfig.allowedThirdPartyModels`, e.g. Anthropic Claude Opus/Sonnet 5.5)
 *   - Master enablement (`devToolsEnabled`) & compliance logging (`loggingConfig`)
 *   - Security policies (`agentConfig`: outside-workspace file access, terminal auto-execution, sandbox)
 *   - External web access (`browserConfig`: browser access, JavaScript auto-execution)
 *   - MCP servers (`mcpConfig`: enabled toggle & `mcpAllowlist` JSON), plugins (`pluginConfig`),
 *     pay-as-you-go overage (`overageConfig`), and any dynamic feature flags (`featureConfig`).
 *
 * Google Cloud currently restricts `UpdateDevToolsConfig` writes to Google Cloud Console (external REST
 * calls return 404 Method not found), so this view provides full cross-project auditing, drift detection,
 * CSV export, and one-click Cloud Console links per project.
 */
import { h, mount, append, downloadText, toCsv } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';
import { fmt, DASH } from '../core/format.js';
import { isAbort } from '../core/api.js';
import {
  card, kpi, badge, button, chip, searchBox, selectInput, segmented, emptyState, errorState, banner, openModal,
  codeBlock, warningsNotice,
} from '../core/components.js';
import { pageSkeleton, fetchRange } from './_shared.js';

const BASE = '/api/v1/gemini-enterprise/dev-tools';
const DOCS = 'https://docs.cloud.google.com/gemini/enterprise/docs/ai-developer-tools-settings';
const consoleUrl = (project) => `https://console.cloud.google.com/gemini-enterprise${project ? `?project=${encodeURIComponent(project)}` : ''}`;

/* Verified Antigravity supported models (official Gemini Enterprise AI developer tools documentation). */
const AGY_GOOGLE_MODELS = [
  ['gemini-4-hydrogen', 'Gemini 4 Hydrogen', false],
  ['gemini-4-carbon', 'Gemini 4 Carbon', false],
  ['gemini-4-argon', 'Gemini 4 Argon', false],
  ['gemini-3.8-flash', 'Gemini 3.8 Flash', false],
  ['gemini-3.7-flash', 'Gemini 3.7 Flash', false],
  ['gemini-3.6-flash', 'Gemini 3.6 Flash', false],
  ['gemini-3.1-pro-preview', 'Gemini 3.1 Pro (Preview)', true],
];

const AGY_THIRD_PARTY_MODELS = [
  ['claude-opus-5-5', 'Anthropic Claude Opus 5.5'],
  ['claude-sonnet-5-5', 'Anthropic Claude Sonnet 5.5'],
];

const AGY_MODEL_LABELS = Object.fromEntries(
  AGY_GOOGLE_MODELS.map(([id, label]) => [id, label])
    .concat(AGY_THIRD_PARTY_MODELS.map(([id, label]) => [id, label]))
    .concat([
      ['gemini-4-neon', 'Gemini 4 Neon'],
      ['gemini-3.5-flash', 'Gemini 3.5 Flash'],
      ['gemini-3.5-flash-lite', 'Gemini 3.5 Flash-Lite'],
      ['gemini-2.5-pro', 'Gemini 2.5 Pro'],
    ]),
);

const SETTING_GROUPS = [
  ['enablement', 'Enablement'],
  ['compliance', 'Compliance logging'],
  ['security', 'Security policies'],
  ['web', 'External web access'],
  ['mcp', 'MCP, plugins and billing'],
  ['features', 'Additional features'],
];

const FILE_POLICY_LABELS = {
  deny: 'Deny',
  always_ask: 'Always ask',
  allow: 'Allow',
  default: 'Default (Deny)',
};
const FILE_POLICY_TONES = { deny: 'success', always_ask: 'warning', allow: 'danger', default: 'neutral' };

const TERMINAL_POLICY_LABELS = {
  require_review: 'Require review',
  proceed_in_sandbox: 'Proceed in sandbox',
  always_proceed: 'Always proceed',
  default: 'Default (Always proceed)',
};
const TERMINAL_POLICY_TONES = {
  require_review: 'success', proceed_in_sandbox: 'info', always_proceed: 'warning', default: 'neutral',
};

const JS_POLICY_LABELS = {
  disabled: 'Disabled',
  request_review: 'Request review',
  allowed: 'Allowed',
  default: 'Default (Disabled)',
};
const JS_POLICY_TONES = { disabled: 'success', request_review: 'warning', allowed: 'danger', default: 'neutral' };

const MODEL_AVAIL_LABELS = {
  allowed: 'Allowed',
  allowed_default: 'Allowed (default)',
  allowed_payg: 'Allowed (PAYG)',
  blocked: 'Blocked',
  off_default: 'Off (default)',
  tools_off: 'Tools off',
};

/* ------------------------------------------------------------------ helpers */

function modelDisplayName(id) {
  return AGY_MODEL_LABELS[id] || id;
}

function buildSettingRows(configs) {
  const rows = [
    {
      key: 'devToolsEnabled',
      group: 'enablement',
      label: t('AI developer tools'),
      apiKey: 'devToolsEnabled',
      val: (c) => (c.devToolsEnabled ? 'on' : 'off'),
      text: (c) => (c.devToolsEnabled ? t('Enabled') : t('Disabled')),
      tone: (c) => (c.devToolsEnabled ? 'success' : 'neutral'),
    },
    {
      key: 'logging.metadata',
      group: 'compliance',
      label: t('Metadata logging'),
      apiKey: 'loggingConfig.metadataLoggingEnabled',
      val: (c) => (c.logging && c.logging.metadataLoggingEnabled ? 'on' : 'off'),
      text: (c) => (c.logging && c.logging.metadataLoggingEnabled ? t('Enabled') : t('Disabled')),
      tone: (c) => (c.logging && c.logging.metadataLoggingEnabled ? 'success' : (c.devToolsEnabled ? 'warning' : 'neutral')),
    },
    {
      key: 'logging.prompts',
      group: 'compliance',
      label: t('Prompts and responses logging'),
      apiKey: 'loggingConfig.promptAndResponseLoggingEnabled',
      val: (c) => (c.logging && c.logging.promptAndResponseLoggingEnabled ? 'on' : 'off'),
      text: (c) => (c.logging && c.logging.promptAndResponseLoggingEnabled ? t('Enabled') : t('Disabled')),
      tone: (c) => (c.logging && c.logging.promptAndResponseLoggingEnabled ? 'success' : 'neutral'),
    },
    {
      key: 'agent.fileAccess',
      group: 'security',
      label: t('Outside-workspace file access'),
      apiKey: 'agentConfig.outsideWorkspaceFileAccessPolicy',
      val: (c) => (c.agent && c.agent.outsideWorkspaceFileAccessPolicy) || 'default',
      text: (c) => t(FILE_POLICY_LABELS[(c.agent && c.agent.outsideWorkspaceFileAccessPolicy) || 'default'] || 'Default (Deny)'),
      tone: (c) => FILE_POLICY_TONES[(c.agent && c.agent.outsideWorkspaceFileAccessPolicy) || 'default'] || 'neutral',
    },
    {
      key: 'agent.terminal',
      group: 'security',
      label: t('Terminal auto-execution'),
      apiKey: 'agentConfig.terminalCommandAutoExecutionPolicy',
      val: (c) => (c.agent && c.agent.terminalCommandAutoExecutionPolicy) || 'default',
      text: (c) => t(TERMINAL_POLICY_LABELS[(c.agent && c.agent.terminalCommandAutoExecutionPolicy) || 'default'] || 'Default (Always proceed)'),
      tone: (c) => TERMINAL_POLICY_TONES[(c.agent && c.agent.terminalCommandAutoExecutionPolicy) || 'default'] || 'neutral',
    },
    {
      key: 'agent.sandbox',
      group: 'security',
      label: t('Sandbox mode'),
      apiKey: 'agentConfig.sandboxModeEnabled',
      val: (c) => (c.agent && c.agent.sandboxModeEnabled ? 'on' : 'off'),
      text: (c) => (c.agent && c.agent.sandboxModeEnabled ? t('Enabled') : t('Disabled')),
      tone: (c) => (c.agent && c.agent.sandboxModeEnabled ? 'success' : 'neutral'),
    },
    {
      key: 'browser.enabled',
      group: 'web',
      label: t('Browser access'),
      apiKey: 'browserConfig.browserEnabled',
      val: (c) => (c.browser && c.browser.browserEnabled ? 'on' : 'off'),
      text: (c) => (c.browser && c.browser.browserEnabled ? t('Enabled') : t('Disabled')),
      tone: (c) => (c.browser && c.browser.browserEnabled ? 'warning' : 'neutral'),
    },
    {
      key: 'browser.js',
      group: 'web',
      label: t('Browser JavaScript auto-execution'),
      apiKey: 'browserConfig.javascriptExecutionPolicy',
      val: (c) => (c.browser && c.browser.javascriptExecutionPolicy) || 'default',
      text: (c) => t(JS_POLICY_LABELS[(c.browser && c.browser.javascriptExecutionPolicy) || 'default'] || 'Default (Disabled)'),
      tone: (c) => JS_POLICY_TONES[(c.browser && c.browser.javascriptExecutionPolicy) || 'default'] || 'neutral',
    },
    {
      key: 'mcp.enabled',
      group: 'mcp',
      label: t('MCP servers'),
      apiKey: 'mcpConfig.mcpEnabled',
      val: (c) => (c.mcp && c.mcp.mcpEnabled ? 'on' : 'off'),
      text: (c) => (c.mcp && c.mcp.mcpEnabled ? t('Enabled') : t('Disabled')),
      tone: (c) => (c.mcp && c.mcp.mcpEnabled ? 'info' : 'neutral'),
    },
    {
      key: 'mcp.allowlist',
      group: 'mcp',
      label: t('MCP allowed server configuration'),
      apiKey: 'mcpConfig.mcpAllowlist',
      val: (c) => (c.mcp && c.mcp.hasAllowlist ? `set:${c.mcp.localServerCount}:${c.mcp.remoteServerCount}` : 'none'),
      text: (c) => (c.mcp && c.mcp.hasAllowlist
        ? t('{local} local · {remote} remote', { local: fmt.int(c.mcp.localServerCount || 0), remote: fmt.int(c.mcp.remoteServerCount || 0) })
        : t('Not configured')),
      tone: (c) => (c.mcp && c.mcp.hasAllowlist ? 'info' : 'neutral'),
      isMcpAllowlist: true,
    },
    {
      key: 'plugins.enabled',
      group: 'mcp',
      label: t('Plugins'),
      apiKey: 'pluginConfig.pluginsDisabled',
      val: (c) => (c.plugins && c.plugins.pluginsDisabled ? 'off' : 'on'),
      text: (c) => (c.plugins && c.plugins.pluginsDisabled ? t('Disabled') : t('Enabled (default)')),
      tone: (c) => (c.plugins && c.plugins.pluginsDisabled ? 'warning' : 'neutral'),
    },
    {
      key: 'plugins.marketplace',
      group: 'mcp',
      label: t('Plugin marketplace'),
      apiKey: 'pluginConfig.marketplaceDisabled',
      val: (c) => (c.plugins && c.plugins.marketplaceDisabled ? 'off' : 'on'),
      text: (c) => (c.plugins && c.plugins.marketplaceDisabled ? t('Disabled') : t('Enabled (default)')),
      tone: (c) => (c.plugins && c.plugins.marketplaceDisabled ? 'warning' : 'neutral'),
    },
    {
      key: 'overage.enabled',
      group: 'mcp',
      label: t('Pay-as-you-go overage'),
      apiKey: 'overageConfig.overageEnabled',
      val: (c) => (c.overage && c.overage.overageEnabled === true ? 'on'
        : (c.overage && c.overage.overageEnabled === false ? 'off' : 'default')),
      text: (c) => (c.overage && c.overage.overageEnabled === true ? t('Enabled')
        : (c.overage && c.overage.overageEnabled === false ? t('Disabled') : t('Default'))),
      tone: (c) => (c.overage && c.overage.overageEnabled === true ? 'info' : 'neutral'),
    },
  ];

  // Dynamically append any additional fields Google returns in browserConfig (e.g. URL allowlist) or featureConfig.
  const extraBrowserKeys = new Set();
  const extraFeatureKeys = new Set();
  (configs || []).forEach((c) => {
    Object.keys((c.browser && c.browser.extra) || {}).forEach((k) => extraBrowserKeys.add(k));
    Object.keys(c.features || {}).forEach((k) => extraFeatureKeys.add(k));
  });
  [...extraBrowserKeys].sort().forEach((k) => {
    rows.push({
      key: `browser.extra.${k}`,
      group: 'web',
      label: k,
      apiKey: `browserConfig.${k}`,
      val: (c) => JSON.stringify((c.browser && c.browser.extra && c.browser.extra[k]) ?? null),
      text: (c) => formatExtraValue(c.browser && c.browser.extra && c.browser.extra[k]),
      tone: (c) => ((c.browser && c.browser.extra && c.browser.extra[k] != null) ? 'info' : 'neutral'),
    });
  });
  [...extraFeatureKeys].sort().forEach((k) => {
    rows.push({
      key: `features.${k}`,
      group: 'features',
      label: k,
      apiKey: `featureConfig.${k}`,
      val: (c) => JSON.stringify((c.features && c.features[k]) ?? null),
      text: (c) => formatExtraValue(c.features && c.features[k]),
      tone: (c) => ((c.features && c.features[k] != null) ? 'info' : 'neutral'),
    });
  });
  return rows;
}

function formatExtraValue(v) {
  if (v === null || v === undefined) return t('Default');
  if (typeof v === 'boolean') return v ? t('Enabled') : t('Disabled');
  if (Array.isArray(v)) return v.length ? t('{n} items', { n: fmt.int(v.length) }) : t('Not configured');
  if (typeof v === 'object') return t('Configured');
  return String(v);
}

function agyModelRows(data) {
  const seenGoogle = new Set(AGY_GOOGLE_MODELS.map((m) => m[0]));
  const seen3p = new Set(AGY_THIRD_PARTY_MODELS.map((m) => m[0]));
  const rows = AGY_GOOGLE_MODELS.map(([id, label, preview]) => ({
    id, label, provider: 'google', preview,
  }));
  (data.observedModels || []).filter((id) => !seenGoogle.has(id)).forEach((id) => {
    rows.push({ id, label: modelDisplayName(id), provider: 'google', preview: id.includes('preview') });
  });
  AGY_THIRD_PARTY_MODELS.forEach(([id, label]) => {
    rows.push({ id, label, provider: 'partner', preview: false });
  });
  (data.observedThirdPartyModels || []).filter((id) => !seen3p.has(id)).forEach((id) => {
    rows.push({ id, label: modelDisplayName(id), provider: 'partner', preview: id.includes('preview') });
  });
  return rows;
}

function modelAvailability(cfg, row) {
  if (!cfg.devToolsEnabled) return { code: 'tools_off', tone: 'neutral', icon: 'minus' };
  const m = cfg.models || {};
  if (row.provider === 'partner') {
    const enabled = (m.allowedThirdPartyModels || []).includes(row.id);
    return enabled
      ? { code: 'allowed_payg', tone: 'info', icon: 'check' }
      : { code: 'off_default', tone: 'neutral', icon: 'minus' };
  }
  const allow = m.allowedModels || [];
  if (!allow.length) return { code: 'allowed_default', tone: 'success', icon: 'check' };
  return allow.includes(row.id)
    ? { code: 'allowed', tone: 'success', icon: 'check' }
    : { code: 'blocked', tone: 'danger', icon: 'x' };
}

function hasSecurityAlert(cfg) {
  if (!cfg.devToolsEnabled) return false;
  const a = cfg.agent || {};
  const b = cfg.browser || {};
  const m = cfg.mcp || {};
  return a.outsideWorkspaceFileAccessPolicy === 'allow'
    || b.browserEnabled
    || b.javascriptExecutionPolicy === 'allowed'
    || (m.mcpEnabled && !m.hasAllowlist);
}

function openMcpModal(cfg) {
  const allowlist = (cfg.mcp && cfg.mcp.mcpAllowlist) || {};
  openModal({
    title: t('MCP allowed server configuration'),
    subtitle: `${cfg.project} \u00b7 ${cfg.location}`,
    wide: true,
    body: (el) => append(el, [
      h('p', { class: 'text-muted', style: 'margin:0 0 10px;font-size:13px' },
        t('Local servers: {local} \u00b7 Remote servers: {remote}', {
          local: fmt.int((cfg.mcp && cfg.mcp.localServerCount) || 0),
          remote: fmt.int((cfg.mcp && cfg.mcp.remoteServerCount) || 0),
        })),
      codeBlock(JSON.stringify(allowlist, null, 2), { wrap: true }),
    ]),
    footer: (el, dialog) => append(el, [button({ label: t('Close'), variant: 'primary', onClick: () => dialog.close() })]),
  });
}

/* ------------------------------------------------------------------ entry */

export default async function render(ctx, header) {
  const body = h('div', { class: 'stack-lg' });
  mount(ctx.root, header, body);
  mount(body, pageSkeleton(4, 2));

  let res;
  try {
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
  if (data.configs === null || data.configs === undefined) {
    body.append(card({
      body: emptyState({
        icon: 'sliders', tone: 'warning', title: t('AI developer tools settings could not be read'),
        body: t('Grant the runtime service account Discovery Engine Viewer in each project and check that the Discovery Engine API is enabled.'),
      }),
    }));
    return null;
  }
  if (!data.configs.length) {
    body.append(card({
      body: emptyState({
        icon: 'sliders', title: t('No AI developer tools configurations found'),
        body: t('No project in the current workspace returned a Discovery Engine DevToolsConfig resource.'),
      }),
    }));
    return null;
  }

  const state = {
    data,
    configs: data.configs,
    settingRows: buildSettingRows(data.configs),
    modelRows: agyModelRows(data),
    modelView: 'summary', // 'summary' | 'matrix'
    filter: {
      query: '',
      project: 'all',
      enabledOnly: data.configs.some((c) => c.devToolsEnabled),
      mixedOnly: false,
    },
  };

  const view = {
    kpis: h('div', { class: 'kpi-grid' }),
    modelsHost: h('div'),
    settingsHost: h('div'),
  };
  const repaint = () => paint(state, view);

  body.append(view.kpis);
  body.append(banner({
    tone: 'info',
    iconName: 'lock',
    title: t('Live read-only view (Google Cloud Console required for edits)'),
    text: t('Google Cloud currently restricts DevToolsConfig writes (UpdateDevToolsConfig) to Google Cloud Console. Use this page to audit AI developer tools settings and Antigravity authorized models across all projects and locations, then open Cloud Console to make changes.'),
    actions: [
      h('a', {
        class: 'btn btn-secondary btn-sm',
        href: consoleUrl(state.configs[0] && state.configs[0].project),
        target: '_blank',
        rel: 'noopener noreferrer',
      }, t('Open Cloud Console'), ' ', icon('external', { size: 12 })),
    ],
  }));

  const toolbar = buildToolbar(state, repaint);
  const modelSwitchHost = h('div', { class: 'row' });
  const renderModelSwitch = () => mount(modelSwitchHost,
    segmented([
      { value: 'summary', label: t('Policy summary') },
      { value: 'matrix', label: t('Model matrix') },
    ], state.modelView, (v) => { state.modelView = v; renderModelSwitch(); paintModels(state, view.modelsHost); }, { label: t('Antigravity model view') }),
    button({ label: t('Export CSV'), icon: 'download', size: 'sm', onClick: () => exportModelsCsv(state) }));
  renderModelSwitch();

  body.append(card({
    title: t('Antigravity model selection'),
    subtitle: t('Authorized Google (Gemini) and third-party models per project and location. An empty Google model list permits all supported Gemini models.'),
    actions: modelSwitchHost,
    flush: true,
    source: `Discovery Engine DevToolsConfig.modelConfig (${data.apiVersion || 'v1alpha'})`,
    body: h('div', null, toolbar, view.modelsHost),
  }));

  const settingsActions = h('div', { class: 'row' },
    button({ label: t('Export CSV'), icon: 'download', size: 'sm', onClick: () => exportSettingsCsv(state) }),
    h('a', { class: 'btn btn-secondary btn-sm', href: DOCS, target: '_blank', rel: 'noopener noreferrer' },
      t('Documentation'), ' ', icon('external', { size: 12 })));

  body.append(card({
    title: t('AI developer tools settings'),
    subtitle: t('Enablement, compliance logging, security policies, external web access, MCP servers and plugins across locations.'),
    actions: settingsActions,
    flush: true,
    source: `Discovery Engine DevToolsConfig (${data.apiVersion || 'v1alpha'})`,
    body: view.settingsHost,
  }));

  repaint();
  return null;
}

/* ------------------------------------------------------------------ toolbar & KPIs */

function visibleConfigs(state) {
  const f = state.filter;
  return state.configs.filter((c) => {
    if (f.project !== 'all' && c.project !== f.project) return false;
    if (f.enabledOnly && !c.devToolsEnabled) return false;
    return true;
  });
}

function buildToolbar(state, repaint) {
  const f = state.filter;
  const projects = [...new Set(state.configs.map((c) => c.project))];
  const projectPick = projects.length > 1
    ? selectInput(
      [{ value: 'all', label: t('All projects') }].concat(projects.map((p) => ({ value: p, label: p }))),
      f.project,
      (v) => { f.project = v; repaint(); },
      { 'aria-label': t('Project') },
    )
    : null;
  const toggles = h('div', { class: 'row wrap' });
  const renderToggles = () => mount(toggles,
    chip(t('Enabled locations only'), {
      pressed: f.enabledOnly,
      title: t('Show only project locations where AI developer tools are turned on'),
      onClick: () => { f.enabledOnly = !f.enabledOnly; renderToggles(); repaint(); },
    }),
    chip(t('Mixed settings only'), {
      pressed: f.mixedOnly,
      title: t('Show only settings whose value differs between the shown locations'),
      onClick: () => { f.mixedOnly = !f.mixedOnly; renderToggles(); repaint(); },
    }));
  renderToggles();
  return h('div', { class: 'fm-toolbar' },
    h('div', { class: 'table-toolbar' },
      h('div', { class: 'left' },
        searchBox({ placeholder: t('Search models or settings'), onInput: (v) => { f.query = v; repaint(); } }),
        projectPick,
        toggles),
      h('div', { class: 'right' })));
}

function paint(state, view) {
  paintKpis(state, view.kpis);
  paintModels(state, view.modelsHost);
  paintSettings(state, view.settingsHost);
}

function paintKpis(state, el) {
  const all = state.configs.filter((c) => state.filter.project === 'all' || c.project === state.filter.project);
  const enabled = all.filter((c) => c.devToolsEnabled);
  const allowlisted = enabled.filter((c) => c.models && c.models.mode === 'allowlist').length;
  const with3p = enabled.filter((c) => c.models && (c.models.allowedThirdPartyModels || []).length > 0).length;
  const metaOn = enabled.filter((c) => c.logging && c.logging.metadataLoggingEnabled).length;
  const metaMissing = enabled.length - metaOn;
  const alerts = enabled.filter(hasSecurityAlert).length;

  mount(el,
    kpi({
      label: t('Enabled locations'),
      value: `${fmt.int(enabled.length)} / ${fmt.int(all.length)}`,
      meta: t('{n} projects', { n: fmt.int(new Set(all.map((c) => c.project)).size) }),
      tip: t('Project and location pairs where AI developer tools are enabled.'),
    }),
    kpi({
      label: t('Antigravity model policy'),
      value: allowlisted ? t('{n} custom', { n: fmt.int(allowlisted) }) : t('All Gemini'),
      meta: with3p ? t('{n} with third-party models', { n: fmt.int(with3p) }) : t('No third-party models enabled'),
      tip: t('Empty allowlist permits all supported Gemini models. Third-party models require opt-in and PAYG billing.'),
    }),
    kpi({
      label: t('Metadata logging'),
      value: enabled.length ? `${fmt.int(metaOn)} / ${fmt.int(enabled.length)}` : DASH,
      meta: metaMissing ? t('{n} enabled locations missing metadata logs', { n: fmt.int(metaMissing) }) : t('Every enabled location logs metadata'),
      tip: t('Metadata logging records Antigravity usage and third-party model token consumption.'),
    }),
    kpi({
      label: t('Permissive security scopes'),
      value: fmt.int(alerts),
      meta: alerts ? t('External files, browser or unrestricted MCP active') : t('No permissive security overrides detected'),
      tip: t('Enabled locations with outside-workspace file access set to Allow, browser access enabled, JS auto-execution allowed, or MCP enabled without an allowlist.'),
    }));
}

/* ------------------------------------------------------------------ Antigravity model selection */

function paintModels(state, el) {
  const configs = visibleConfigs(state);
  if (!configs.length) {
    mount(el, emptyState({
      icon: 'filter',
      title: t('No locations match the filters'),
      body: t('Turn off "Enabled locations only" or select another project to inspect all locations.'),
      compact: true,
    }));
    return;
  }
  if (state.modelView === 'matrix') {
    paintModelMatrix(state, configs, el);
    return;
  }
  const q = state.filter.query.trim().toLowerCase();
  const filtered = configs.filter((c) => {
    if (!q) return true;
    const text = `${c.project} ${c.location} ${(c.models.allowedModels || []).join(' ')} ${(c.models.allowedThirdPartyModels || []).join(' ')}`.toLowerCase();
    return text.includes(q);
  });
  if (!filtered.length) {
    mount(el, emptyState({ icon: 'filter', title: t('No locations match the search'), body: t('Clear the search filter.'), compact: true }));
    return;
  }

  const head = h('tr', null,
    h('th', { scope: 'col', class: 'dt-scope-col' }, t('Project and location')),
    h('th', { scope: 'col' }, t('Status')),
    h('th', { scope: 'col' }, t('Google model policy')),
    h('th', { scope: 'col' }, t('Authorized Google models')),
    h('th', { scope: 'col' }, t('Third-party models')),
    h('th', { scope: 'col', class: 'dt-act-col' }, t('Console')));

  const rows = filtered.map((c) => {
    const m = c.models || {};
    const gList = m.allowedModels || [];
    const pList = m.allowedThirdPartyModels || [];
    const googleCell = !c.devToolsEnabled
      ? h('span', { class: 'text-muted' }, t('AI developer tools disabled'))
      : (gList.length
        ? h('div', { class: 'dt-model-chips' }, gList.map((id) => badge(modelDisplayName(id), 'success', { size: 'sm', title: id })))
        : h('div', { class: 'dt-model-chips' },
          badge(t('All supported Gemini models'), 'info', { size: 'sm' }),
          h('span', { class: 'dt-hint' }, t('Newly released Gemini models are allowed automatically'))));

    const partnerCell = !c.devToolsEnabled
      ? h('span', { class: 'text-muted' }, DASH)
      : (pList.length
        ? h('div', { class: 'dt-model-chips' }, pList.map((id) => badge(`${modelDisplayName(id)} · PAYG`, 'accent', { size: 'sm', title: id })))
        : h('span', { class: 'text-muted' }, t('Off (default)')));

    return h('tr', { class: 'fm-row' },
      h('th', { scope: 'row', class: 'dt-scope-col' },
        h('div', { class: 'cell-stack' },
          h('span', { class: 'cell-main' }, c.project),
          h('span', { class: 'fm-app-meta' }, h('span', { class: 'fm-pill' }, c.location)))),
      h('td', null, badge(c.devToolsEnabled ? t('Enabled') : t('Disabled'), c.devToolsEnabled ? 'success' : 'neutral', { size: 'sm', dot: true })),
      h('td', null, c.devToolsEnabled
        ? badge(m.mode === 'allowlist' ? t('Custom allowlist ({n})', { n: gList.length }) : t('All Gemini (default)'),
          m.mode === 'allowlist' ? 'warning' : 'neutral', { size: 'sm' })
        : h('span', { class: 'text-muted' }, DASH)),
      h('td', null, googleCell),
      h('td', null, partnerCell),
      h('td', { class: 'dt-act-col' },
        h('a', {
          class: 'btn btn-secondary btn-sm',
          href: consoleUrl(c.project),
          target: '_blank',
          rel: 'noopener noreferrer',
          title: t('Open Gemini Enterprise settings in Google Cloud Console'),
        }, t('Edit in Console'), ' ', icon('external', { size: 12 }))));
  });

  mount(el, h('div', { class: 'fm-wrap' },
    h('table', { class: 'fm-table dt-summary-table' },
      h('caption', { class: 'visually-hidden' }, t('Antigravity authorized models by project and location')),
      h('thead', null, head),
      h('tbody', null, rows))));
}

function paintModelMatrix(state, configs, el) {
  const q = state.filter.query.trim().toLowerCase();
  const rows = state.modelRows.filter((r) => !q || `${r.label} ${r.id}`.toLowerCase().includes(q));
  if (!rows.length) {
    mount(el, emptyState({ icon: 'filter', title: t('No models match the search'), body: t('Clear the search filter.'), compact: true }));
    return;
  }
  const head = h('tr', null,
    h('th', { class: 'fm-feature-col', scope: 'col' }, t('Antigravity model')),
    configs.map((c) => scopeHeader(c)));

  const bodyRows = [];
  let provider = null;
  rows.forEach((r) => {
    if (r.provider !== provider) {
      provider = r.provider;
      const label = provider === 'partner' ? t('Third-party models (PAYG)') : t('Google (Gemini) models');
      bodyRows.push(h('tr', { class: 'fm-group' },
        h('th', { class: 'fm-feature-col', scope: 'rowgroup' }, label),
        h('td', { colspan: configs.length })));
    }
    const vals = configs.map((c) => modelAvailability(c, r));
    const mixed = new Set(vals.map((v) => v.code)).size > 1;
    if (state.filter.mixedOnly && !mixed) return;
    bodyRows.push(h('tr', { class: ['fm-row', mixed ? 'mixed' : ''] },
      h('th', { class: 'fm-feature-col', scope: 'row' },
        h('div', { class: 'fm-feature-text' },
          h('span', { class: 'fm-feature-name' }, r.label,
            r.preview ? badge(t('Preview'), 'neutral', { size: 'sm' }) : null,
            r.provider === 'partner' ? badge(t('3P · PAYG'), 'accent', { size: 'sm' }) : null),
          h('span', { class: 'fm-feature-key' }, r.id))),
      vals.map((v) => h('td', { class: 'fm-cell-td' },
        badge(t(MODEL_AVAIL_LABELS[v.code] || v.code), v.tone, { size: 'sm', dot: v.tone !== 'neutral' })))));
  });

  mount(el, h('div', { class: 'fm-wrap' },
    h('table', { class: 'fm-table' },
      h('caption', { class: 'visually-hidden' }, t('Antigravity model availability matrix')),
      h('thead', null, head),
      h('tbody', null, bodyRows))));
}

/* ------------------------------------------------------------------ AI developer tools settings matrix */

function scopeHeader(c) {
  return h('th', { class: 'fm-app-col', scope: 'col' },
    h('div', { class: 'fm-app' },
      h('div', { class: 'fm-app-top' },
        h('span', { class: 'fm-app-name', title: c.project }, c.project),
        h('a', {
          class: 'icon-btn fm-col-menu',
          href: consoleUrl(c.project),
          target: '_blank',
          rel: 'noopener noreferrer',
          title: t('Edit in Google Cloud Console'),
          'aria-label': t('Edit {project} in Google Cloud Console', { project: c.project }),
        }, icon('external', { size: 13 }))),
      h('span', { class: 'fm-app-meta' },
        h('span', { class: 'fm-pill' }, c.location),
        badge(c.devToolsEnabled ? t('On') : t('Off'), c.devToolsEnabled ? 'success' : 'neutral', { size: 'sm' }))));
}

function paintSettings(state, el) {
  const configs = visibleConfigs(state);
  if (!configs.length) {
    mount(el, emptyState({
      icon: 'filter',
      title: t('No locations match the filters'),
      body: t('Turn off "Enabled locations only" or select another project to inspect all locations.'),
      compact: true,
    }));
    return;
  }
  const q = state.filter.query.trim().toLowerCase();
  const rows = state.settingRows.filter((r) => {
    if (q && !`${r.label} ${r.apiKey}`.toLowerCase().includes(q)) return false;
    const distinct = new Set(configs.map((c) => r.val(c)));
    if (state.filter.mixedOnly && distinct.size <= 1) return false;
    return true;
  });
  if (!rows.length) {
    mount(el, emptyState({ icon: 'filter', title: t('No settings match the filters'), body: t('Clear the search or turn off Mixed settings only.'), compact: true }));
    return;
  }

  const head = h('tr', null,
    h('th', { class: 'fm-feature-col', scope: 'col' }, t('Setting')),
    configs.map((c) => scopeHeader(c)));

  const bodyRows = [];
  let group = null;
  rows.forEach((row) => {
    if (row.group !== group) {
      group = row.group;
      const gLabel = (SETTING_GROUPS.find((g) => g[0] === group) || [group, group])[1];
      bodyRows.push(h('tr', { class: 'fm-group' },
        h('th', { class: 'fm-feature-col', scope: 'rowgroup' }, t(gLabel)),
        h('td', { colspan: configs.length })));
    }
    const mixed = new Set(configs.map((c) => row.val(c))).size > 1;
    bodyRows.push(h('tr', { class: ['fm-row', mixed ? 'mixed' : ''] },
      h('th', { class: 'fm-feature-col', scope: 'row' },
        h('div', { class: 'fm-feature-text' },
          h('span', { class: 'fm-feature-name' }, row.label, mixed ? badge(t('Mixed'), 'warning', { size: 'sm' }) : null),
          h('span', { class: 'fm-feature-key' }, row.apiKey))),
      configs.map((c) => {
        if (row.isMcpAllowlist && c.mcp && c.mcp.hasAllowlist) {
          return h('td', { class: 'fm-cell-td' },
            button({
              label: row.text(c),
              icon: 'code',
              size: 'sm',
              onClick: () => openMcpModal(c),
            }));
        }
        return h('td', { class: 'fm-cell-td' },
          badge(row.text(c), row.tone(c), { size: 'sm', dot: row.tone(c) !== 'neutral' }));
      })));
  });

  mount(el, h('div', { class: 'fm-wrap' },
    h('table', { class: 'fm-table' },
      h('caption', { class: 'visually-hidden' }, t('AI developer tools settings across projects and locations')),
      h('thead', null, head),
      h('tbody', null, bodyRows))));
}

/* ------------------------------------------------------------------ export */

function exportModelsCsv(state) {
  const configs = visibleConfigs(state);
  const columns = [
    { key: 'project', label: t('Project') },
    { key: 'location', label: t('Location') },
    { key: 'devToolsEnabled', label: t('AI developer tools'), csv: (c) => (c.devToolsEnabled ? 'enabled' : 'disabled') },
    { key: 'mode', label: t('Google model policy'), csv: (c) => (c.models && c.models.mode) || 'all_gemini' },
    { key: 'allowedModels', label: t('Authorized Google models'), csv: (c) => ((c.models && c.models.allowedModels) || []).join('; ') },
    { key: 'allowedThirdPartyModels', label: t('Third-party models'), csv: (c) => ((c.models && c.models.allowedThirdPartyModels) || []).join('; ') },
  ];
  downloadText(`antigravity-models-${new Date().toISOString().slice(0, 10)}.csv`, toCsv(columns, configs));
}

function exportSettingsCsv(state) {
  const configs = visibleConfigs(state);
  const columns = [
    { key: 'label', label: t('Setting') },
    { key: 'apiKey', label: t('Key') },
  ].concat(configs.map((c) => ({ key: c.key, label: c.key, csv: (r) => r.text(c) })));
  downloadText(`ai-developer-tools-settings-${new Date().toISOString().slice(0, 10)}.csv`, toCsv(columns, state.settingRows));
}
