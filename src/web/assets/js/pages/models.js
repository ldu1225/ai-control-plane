/**
 * Models & Tokens: auto-discovered model catalog with per-model token and cost tracking.
 */
import { h, append, mount } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api } from '../core/api.js';
import { isAdmin } from '../core/state.js';
import {
  pageHeader, kpi, card, dataTable, badge, button, banner, segmented, openDrawer, openModal, switchInput, toast, progress, stat, chip, setBusy,
  tagMatrix, tagLegend, warningsNotice, listCell,
} from '../core/components.js';
import { errorText } from '../core/messages.js';
import { lineChart, barChart, donutChart, fromTimeline, colorAt, TONES } from '../core/charts.js';
import { pageFlow, fetchRange, rangeText, chartCard, mountCharts } from './_shared.js';

const SOURCE_LABELS = { billing: 'Billing', monitoring: 'Monitoring', antigravity: 'Antigravity' };
const sourceItems = () => [
  { key: 'billing', short: 'BIL', label: t('Billing') },
  { key: 'monitoring', short: 'MON', label: t('Monitoring') },
  { key: 'antigravity', short: 'AG', label: 'Antigravity' },
];
const MEASURE_LABELS = {
  input_tokens: 'Input tokens', output_tokens: 'Output tokens', cached_tokens: 'Cached tokens', tokens: 'Tokens',
  images: 'Images', seconds: 'Seconds', characters: 'Characters', requests: 'Requests', other: 'Other',
};

export default async function render(ctx) {
  const actions = button({ label: isAdmin() ? t('Manage catalog') : t('View catalog'), icon: 'sliders', onClick: () => openCatalog(ctx) });
  const header = pageHeader({
    title: t('Models & Tokens'),
    desc: t('Every model discovered in billing, Cloud Monitoring and Antigravity telemetry. New model series are detected and tracked automatically.'),
    actions,
  });
  return pageFlow(ctx, {
    header,
    kpiCount: 6,
    fetcher: () => fetchRange(ctx, '/api/v1/models/usage'),
    render: (body, d) => renderPage(ctx, body, d),
  });
}

function renderPage(ctx, body, d) {
  const ccy = d.currency || 'USD';
  const totals = d.totals || {};
  const tk = totals.tokens || {};
  const models = d.models || [];
  const enabled = models.filter((m) => m.enabled);
  const fresh = models.filter((m) => m.isNew);
  const invocations = totals.invocations;
  const errorCount = enabled.reduce((a, m) => a + (isNum(m.invocations) && isNum(m.errorRate) ? m.invocations * m.errorRate : 0), 0);

  if (fresh.length) {
    body.append(banner({
      tone: 'info', iconName: 'spark',
      title: t('{n} new models detected', { n: fresh.length }),
      text: `${fresh.map((m) => m.name).join(', ')} \u2014 ${fresh.every((m) => m.enabled) ? t('automatically enabled for tracking.') : t('awaiting enablement in the model catalog.')}`,
      actions: button({ label: t('Review'), size: 'sm', onClick: () => openCatalog(ctx) }),
    }));
  }

  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Total tokens'), value: fmt.compact(tk.total), meta: h('span', null, t('Source: {s}', { s: tokenSourceLabel(d.tokenSource) })), tip: t('Input, output and cached tokens across enabled models.') }),
    kpi({ label: t('Input tokens'), value: fmt.compact(tk.input), meta: isNum(tk.total) && tk.total > 0 ? h('span', null, t('{p} of total', { p: fmt.pct((tk.input || 0) / tk.total) })) : null }),
    kpi({ label: t('Output tokens'), value: fmt.compact(tk.output), meta: isNum(tk.total) && tk.total > 0 ? h('span', null, t('{p} of total', { p: fmt.pct((tk.output || 0) / tk.total) })) : null }),
    kpi({ label: t('Model spend'), value: fmt.currency(totals.cost, ccy, { compact: true }), meta: isNum(totals.productCost) && totals.productCost > 0 ? h('span', null, t('Plus {v} in product charges', { v: fmt.currency(totals.productCost, ccy, { compact: true }) })) : null, tip: t('Billing export cost attributed to model SKUs. SKUs matching the excluded patterns are not counted.') }),
    kpi({ label: t('Models tracked'), value: fmt.int(totals.modelsTracked), meta: h('span', null, t('{n} discovered', { n: fmt.int(totals.modelsDiscovered) })) }),
    kpi({ label: t('Invocations'), value: fmt.compact(invocations), meta: isNum(invocations) && invocations > 0 ? h('span', null, t('{p} error rate', { p: fmt.pct(errorCount / invocations, 2) })) : null, tip: t('Model invocations reported by Cloud Monitoring.') })));

  const row = h('div', { class: 'grid' });
  let metric = 'tokens';
  const trendHost = h('div');
  const buildTrend = () => {
    const tl = metric === 'tokens' ? d.tokenTrend : d.costTrend;
    lineChart(trendHost, {
      ...fromTimeline(tl),
      stacked: true, height: 270,
      yFormat: metric === 'tokens' ? fmt.compact : (v) => fmt.currency(v, ccy, { compact: true }),
      valueFormat: metric === 'tokens' ? fmt.compact : (v) => fmt.currency(v, ccy),
    });
  };
  const toggle = segmented([{ value: 'tokens', label: t('Tokens') }, { value: 'cost', label: t('Cost') }], metric, (v) => { metric = v; buildTrend(); });
  const trendCard = card({ title: t('Usage by model'), subtitle: rangeText(), actions: toggle, body: trendHost, source: tokenSourceLabel(d.tokenSource) });
  trendCard.build = buildTrend;
  trendCard.classList.add('span-8');
  const shareCard = chartCard({
    title: t('Token share'), subtitle: t('Enabled models'),
    build: (host) => donutChart(host, {
      items: enabled.slice().sort((a, b) => ((b.tokens && b.tokens.total) || 0) - ((a.tokens && a.tokens.total) || 0)).slice(0, 8)
        .map((m, i) => ({ label: m.name, value: (m.tokens && m.tokens.total) || 0, color: colorAt(i) })),
      format: fmt.compact, centerValue: fmt.compact(tk.total), centerLabel: t('tokens'), stacked: true, size: 170,
    }),
  });
  shareCard.classList.add('span-4');
  body.append(row);
  mountCharts(row, [trendCard, shareCard]);

  const maxCost = Math.max(...enabled.map((m) => m.cost || 0), 0) || 1;
  body.append(card({
    title: t('Model catalog usage'),
    subtitle: t('Disabled models are listed for reference and excluded from totals.'),
    flush: true,
    body: dataTable({
      columns: [
        {
          key: 'name', label: t('Model'),
          render: (m) => h('div', { class: 'cell-stack' },
            h('div', { class: 'row' }, h('span', { class: 'cell-main' }, m.name), m.isNew ? badge(t('New'), 'accent', { size: 'sm' }) : null),
            h('span', { class: 'cell-sub' }, `${m.vendor || t('Partner')} \u00b7 ${m.key}`)),
        },
        { key: 'enabled', label: t('Tracking'), render: (m) => (m.enabled ? badge(t('Enabled'), 'success', { size: 'sm', dot: true }) : badge(t('Disabled'), 'neutral', { size: 'sm' })), value: (m) => (m.enabled ? 1 : 0) },
        { key: 'sources', label: t('Sources'), title: tagLegend(sourceItems()), sortable: false, render: (m) => tagMatrix(sourceItems(), m.sources, { on: t('Reported'), off: t('Not reported') }), csv: (m) => (m.sources || []).join('|') },
        { key: 'input', label: t('Input'), align: 'num', value: (m) => (m.tokens && m.tokens.input) || 0, render: (m) => fmt.compact(m.tokens && m.tokens.input), csv: (m) => (m.tokens && m.tokens.input) || 0 },
        { key: 'output', label: t('Output'), align: 'num', value: (m) => (m.tokens && m.tokens.output) || 0, render: (m) => fmt.compact(m.tokens && m.tokens.output), csv: (m) => (m.tokens && m.tokens.output) || 0 },
        { key: 'cached', label: t('Cached'), align: 'num', value: (m) => (m.tokens && m.tokens.cached) || 0, render: (m) => fmt.compact(m.tokens && m.tokens.cached), csv: (m) => (m.tokens && m.tokens.cached) || 0 },
        { key: 'total', label: t('Total tokens'), align: 'num', value: (m) => (m.tokens && m.tokens.total) || 0, render: (m) => h('span', { class: 'fw-600' }, fmt.compact(m.tokens && m.tokens.total)), csv: (m) => (m.tokens && m.tokens.total) || 0 },
        { key: 'invocations', label: t('Invocations'), align: 'num', format: fmt.compact },
        { key: 'errorRate', label: t('Errors'), align: 'num', render: (m) => (isNum(m.errorRate) ? h('span', { class: m.errorRate > 0.02 ? 'text-danger fw-600' : '' }, fmt.pct(m.errorRate, 2)) : '\u2014') },
        { key: 'cost', label: t('Cost'), align: 'num', render: (m) => h('div', { class: 'share-cell' }, progress((m.cost || 0) / maxCost), h('span', null, fmt.currency(m.cost, ccy))), value: (m) => m.cost || 0, csv: (m) => m.cost },
      ],
      rows: d.models === null ? null : models, pageSize: 15, exportName: 'model-usage', initialSort: { key: 'total', dir: 'desc' },
      onRowClick: (m) => openModelDrawer(m, d),
    }),
  }));
  if (Array.isArray(d.products) && d.products.length) {
    const maxProduct = Math.max(...d.products.map((p) => p.cost || 0), 0) || 1;
    body.append(card({
      title: t('Usage by product'),
      subtitle: t('Products that re-bill model usage under their own billing SKUs, detected automatically. Their tokens are already counted under the underlying models.'),
      flush: true,
      source: t('Cloud Billing export'),
      body: dataTable({
        columns: [
          { key: 'name', label: t('Product'), render: (p) => h('span', { class: 'cell-main' }, p.name) },
          { key: 'models', label: t('Underlying models'), sortable: false, render: (p) => listCell((p.models || []).map((m) => m.name)), csv: (p) => (p.models || []).map((m) => m.key).join('|') },
          { key: 'tokens', label: t('Re-billed tokens'), align: 'num', format: fmt.compact },
          { key: 'cost', label: t('Charges'), align: 'num', render: (p) => h('div', { class: 'share-cell' }, progress((p.cost || 0) / maxProduct), h('span', null, fmt.currency(p.cost, ccy))), value: (p) => p.cost || 0, csv: (p) => p.cost },
        ],
        rows: d.products, searchable: false, pageSize: 10, exportName: 'product-usage', initialSort: { key: 'cost', dir: 'desc' },
      }),
    }));
  }
  return null;
}

function tokenSourceLabel(src) {
  return { monitoring: t('Cloud Monitoring'), billing: t('Cloud Billing export'), mixed: t('Cloud Monitoring and Billing export') }[src] || t('Cloud Billing export');
}

function openModelDrawer(m, d) {
  const ccy = d.currency || 'USD';
  openDrawer({
    title: m.name,
    subtitle: `${m.vendor || t('Partner')} \u00b7 ${m.key}`,
    width: 860,
    content: (body) => {
      const tk = m.tokens || {};
      append(body, [
        h('div', { class: 'row wrap' }, m.enabled ? badge(t('Tracking enabled'), 'success', { dot: true }) : badge(t('Tracking disabled'), 'neutral'), m.isNew ? badge(t('New'), 'accent') : null, (m.sources || []).map((s) => chip(t(SOURCE_LABELS[s] || s)))),
        h('div', { class: 'stat-row' },
          stat(t('Input tokens'), fmt.compact(tk.input)),
          stat(t('Output tokens'), fmt.compact(tk.output)),
          stat(t('Cached tokens'), fmt.compact(tk.cached)),
          stat(t('Total tokens'), fmt.compact(tk.total)),
          stat(t('Invocations'), fmt.compact(m.invocations), isNum(m.errorRate) ? t('{p} errors', { p: fmt.pct(m.errorRate, 2) }) : null),
          stat(t('Cost'), fmt.currency(m.cost, ccy), isNum(m.share) ? t('{p} of model spend', { p: fmt.pct(m.share) }) : null)),
      ]);
      if (m.otherUsage && m.otherUsage.length) {
        append(body, [card({ title: t('Other usage units'), body: h('div', { class: 'chips' }, m.otherUsage.map((u) => chip(`${fmt.compact(u.amount)} ${u.unit}`))) })]);
      }
      const sr = d.tokenTrend && (d.tokenTrend.series || []).find((x) => x.key === m.key);
      const sc = d.costTrend && (d.costTrend.series || []).find((x) => x.key === m.key);
      if (sr || sc) {
        const host = h('div');
        append(body, [card({ title: t('Daily trend'), subtitle: rangeText(), body: host })]);
        requestAnimationFrame(() => barChart(host, {
          buckets: (d.tokenTrend || d.costTrend).buckets, granularity: (d.tokenTrend || d.costTrend).granularity, height: 210,
          series: [{ key: 'tokens', name: t('Tokens'), values: sr ? sr.values : [], color: TONES.violet }],
          valueFormat: fmt.compact,
          tooltipExtra: sc ? (i) => [{ name: t('Cost'), color: TONES.teal, value: fmt.currency(sc.values[i] || 0, ccy) }] : null,
        }));
      }
      append(body, [card({
        title: t('Billing SKUs'), subtitle: t('Raw SKUs grouped into this model'), flush: true,
        body: dataTable({
          columns: [
            { key: 'sku', label: t('SKU'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main' }, r.sku), h('span', { class: 'cell-sub' }, r.service || '')) },
            { key: 'measure', label: t('Measure'), render: (r) => badge(t(MEASURE_LABELS[r.measure] || r.measure), 'neutral', { size: 'sm' }) },
            { key: 'amount', label: t('Usage'), align: 'num', render: (r) => `${fmt.compact(r.amount)} ${r.unit || ''}`, value: (r) => r.amount },
            { key: 'cost', label: t('Cost'), align: 'num', render: (r) => fmt.currency(r.cost, ccy, { precise: true }), value: (r) => r.cost },
          ],
          rows: m.skus || [], pageSize: 12, searchable: (m.skus || []).length > 8, exportName: `skus-${m.key}`, initialSort: { key: 'cost', dir: 'desc' },
          emptyText: t('No billing SKUs were mapped to this model in the period.'),
        }),
      })]);
    },
  });
}

async function openCatalog(ctx) {
  const admin = isAdmin();
  const m = openModal({
    title: t('Model catalog'),
    subtitle: t('Choose which models are tracked. Models from new Gemini series and partner models are discovered automatically.'),
    wide: true,
    body: (el) => mount(el, h('div', { class: 'loading-row' }, h('span', { class: 'spinner' }), t('Loading catalog'))),
  });
  let catalog;
  let warnings = [];
  let existingDisabled = [];
  try {
    const [cRes, sRes] = await Promise.all([
      api.get('/api/v1/models/catalog'),
      api.get('/api/v1/settings').catch(() => null),
    ]);
    catalog = cRes.data || {};
    warnings = (cRes.meta && cRes.meta.warnings) || [];
    const eff = sRes && sRes.data && sRes.data.effective;
    existingDisabled = (eff && eff.model_catalog && Array.isArray(eff.model_catalog.disabled_models)) ? eff.model_catalog.disabled_models : [];
  } catch (err) {
    mount(m.body, banner({ tone: 'danger', title: t('Catalog unavailable'), text: errorText(err) }));
    return;
  }
  const models = Array.isArray(catalog.models) ? catalog.models : null;
  const state = new Map((models || []).map((x) => [x.key, Boolean(x.enabled)]));
  let autoEnable = Boolean(catalog.autoEnableNewModels);
  const autoSwitch = switchInput({ checked: autoEnable, disabled: !admin, label: t('Automatically enable new models'), onChange: (v) => { autoEnable = v; } });
  mount(m.body,
    warningsNotice(warnings),
    h('div', { class: 'card' },
      h('div', { class: 'setting-row' },
        h('div', { class: 'setting-info' }, h('div', { class: 'setting-title' }, t('Automatically enable new models')), h('div', { class: 'setting-desc' }, t('When a new model series appears in billing or telemetry it starts being tracked immediately.'))),
        h('div', { class: 'setting-control' }, autoSwitch)),
      h('div', { class: 'setting-row' },
        h('div', { class: 'setting-info' }, h('div', { class: 'setting-title' }, t('Excluded SKUs')), h('div', { class: 'setting-desc' }, t('These SKU patterns are never tracked.'))),
        h('div', { class: 'setting-control' }, h('div', { class: 'chips' }, (catalog.excludedSkuPatterns || []).map((p) => chip(p)))))),
    models ? dataTable({
      columns: [
        { key: 'name', label: t('Model'), render: (x) => h('div', { class: 'cell-stack' }, h('div', { class: 'row' }, h('span', { class: 'cell-main' }, x.name), x.isNew ? badge(t('New'), 'accent', { size: 'sm' }) : null), h('span', { class: 'cell-sub cell-mono' }, x.key)) },
        { key: 'vendor', label: t('Vendor') },
        { key: 'firstSeen', label: t('First seen'), render: (x) => (x.firstSeen ? fmt.date(x.firstSeen, 'long') : DASH) },
        { key: 'sources', label: t('Sources'), title: tagLegend(sourceItems()), sortable: false, render: (x) => tagMatrix(sourceItems(), x.sources, { on: t('Reported'), off: t('Not reported') }) },
        {
          key: 'enabled', label: t('Tracked'), align: 'center', value: (x) => (state.get(x.key) ? 1 : 0),
          render: (x) => switchInput({ checked: state.get(x.key), disabled: !admin, label: x.name, onChange: (v) => state.set(x.key, v) }),
        },
      ],
      rows: models, pageSize: 12, initialSort: { key: 'name', dir: 'asc' },
      emptyText: t('No models were discovered in the last 90 days.'),
    }) : banner({ tone: 'warning', title: t('Model list unavailable'), text: t('The model sources could not be read, so per-model tracking cannot be changed right now. Existing choices are preserved.') }));
  const save = button({ label: t('Save catalog'), variant: 'primary', disabled: !admin });
  const cancel = button({ label: admin ? t('Cancel') : t('Close'), onClick: () => m.close() });
  const footer = h('div', { class: 'modal-footer' }, admin ? null : h('span', { class: 'muted spacer' }, t('Only administrators can change the catalog.')), cancel, admin ? save : null);
  m.dialog.append(footer);
  save.addEventListener('click', async () => {
    setBusy(save, true, t('Saving'));
    try {
      const values = { auto_enable_new_models: autoEnable };
      if (models) {
        // Keep disabled keys that are not in the visible list so they are not silently re-enabled.
        const listed = new Set(models.map((x) => x.key));
        const kept = existingDisabled.filter((k) => !listed.has(k));
        const disabledNow = [...state.entries()].filter(([, v]) => !v).map(([k]) => k);
        values.disabled_models = [...new Set([...kept, ...disabledNow])];
      }
      await api.post('/api/v1/settings', { section: 'model_catalog', values });
      toast({ title: t('Model catalog saved'), tone: 'success' });
      m.close();
      ctx.rerender();
    } catch (err) {
      setBusy(save, false);
      toast({ title: t('Save failed'), text: errorText(err), tone: 'danger' });
    }
  });
}
