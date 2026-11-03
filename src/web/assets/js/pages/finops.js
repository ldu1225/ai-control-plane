/**
 * Cost & FinOps: gross/net spend, credits, services, SKUs, spike investigations,
 * token-based reverse cost estimation fallback, and public pricing & USD/KRW FX monitor.
 */
import { h, mount, append } from '../core/dom.js';
import { t, getLocale } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api } from '../core/api.js';
import { store, rangeParams, isAdmin } from '../core/state.js';
import {
  pageHeader, kpi, card, dataTable, badge, button, banner, progress, chip, selectInput, segmented,
  stat, load, setBusy, toast, emptyState, markdownLite, requireAdmin, statusBadge,
} from '../core/components.js';
import { errorText } from '../core/messages.js';
import { barChart, lineChart, hbarList, mapList, TONES, colorAt } from '../core/charts.js';
import { pageFlow, rangeText, chartCard, mountCharts } from './_shared.js';

let selectedProject = 'all';
let selectedCurrency = null;

const PRICE_SOURCE_LABELS = {
  google_public_docs: 'Google Public Docs',
  agent_platform_public_page: 'Agent Platform Docs',
  cloud_billing_catalog: 'Cloud Billing Catalog',
  baseline: 'Baseline Reference',
};

function formatAlertSummary(a) {
  if (!a) return '';
  if (a.kind === 'fx') {
    const sign = isNum(a.pctChange) && a.pctChange > 0 ? '+' : '';
    return `USD/KRW: ${fmt.num(a.before, 2)} \u2192 ${fmt.num(a.after, 2)} KRW (${sign}${fmt.num(a.pctChange, 2)}%)`;
  }
  if (a.kind === 'new_model') {
    return `${a.label || a.target}: ${t('New public model rate')} ($${fmt.num(a.after, 3)} / 1M)`;
  }
  const sign = isNum(a.pctChange) && a.pctChange > 0 ? '+' : '';
  return `${a.label || a.target} (${a.field}): $${fmt.num(a.before, 3)} \u2192 $${fmt.num(a.after, 3)} / 1M (${sign}${fmt.num(a.pctChange, 1)}%)`;
}

export default async function render(ctx) {
  const actionSlot = h('div', { class: 'row wrap' });
  const header = pageHeader({
    title: t('Cost & FinOps'),
    desc: t('Billing export spend (or automatic token-based cost estimation with live USD/KRW FX), service and SKU breakdowns, and AI spike investigation.'),
    actions: actionSlot,
  });
  const queryArgs = { project: selectedProject };
  if (selectedCurrency) queryArgs.currency = selectedCurrency;
  return pageFlow(ctx, {
    header,
    kpiCount: 5,
    fetcher: () => api.get('/api/v1/finops/summary', rangeParams(queryArgs), { signal: ctx.signal }),
    render: (body, d) => {
      const projects = ['all', ...(d.projects || []).filter((p) => p !== 'all')];
      if (!projects.includes(selectedProject)) selectedProject = 'all';
      const currencies = Array.isArray(d.supportedCurrencies) && d.supportedCurrencies.length ? d.supportedCurrencies : [d.currency || 'USD'];
      const activeCcy = currencies.includes(d.currency) ? d.currency : currencies[0];

      const syncBtn = button({
        label: t('Sync pricing & FX now'),
        icon: 'refresh',
        size: 'sm',
        onClick: async () => {
          if (!requireAdmin(t('Sync public token pricing & USD/KRW FX'))) return;
          setBusy(syncBtn, true, t('Syncing'));
          try {
            const res = await api.post('/api/v1/finops/pricing/sync', {});
            const p = res.data || {};
            const fxRate = p.fx && p.fx.usdKrw ? fmt.num(p.fx.usdKrw, 2) : DASH;
            if (ctx.onBootChange) {
              ctx.onBootChange({
                pricingAlerts: {
                  syncedAt: p.syncedAt,
                  trigger: p.trigger,
                  fx: p.fx,
                  unacknowledgedCount: p.unacknowledgedCount || 0,
                  alerts: (p.alerts || []).filter((x) => !x.acknowledgedAt).slice(0, 10),
                },
              });
            }
            toast({
              title: t('Pricing & FX synced'),
              text: t('1 USD = {rate} KRW \u00b7 {n} models updated', { rate: fxRate, n: (p.models || []).length }),
              tone: (p.newAlertsCount || 0) > 0 ? 'warning' : 'success',
              timeout: 3600,
            });
            ctx.rerender();
          } catch (err) {
            setBusy(syncBtn, false);
            toast({ title: t('Sync failed'), text: errorText(err), tone: 'danger' });
          }
        },
      });

      const ccyToggle = currencies.length > 1
        ? segmented(currencies.map((c) => ({ value: c, label: c })), activeCcy, (v) => { selectedCurrency = v; ctx.rerender(); }, { label: t('Currency') })
        : null;

      mount(actionSlot,
        ccyToggle,
        h('span', { class: 'muted' }, t('Billing scope')),
        selectInput(projects.map((p) => ({ value: p, label: p === 'all' ? t('All projects') : p })), selectedProject, (v) => { selectedProject = v; ctx.rerender(); }, { 'aria-label': t('Billing scope') }),
        syncBtn);
      return renderPage(ctx, body, d);
    },
  });
}

function renderPage(ctx, body, d) {
  const ccy = d.currency || 'USD';
  store.currency = ccy;
  const k = d.kpis || {};
  const isEstimate = d.costSource === 'token_estimate';
  const pm = d.pricingMeta || {};
  const fx = pm.fx || {};
  const money = (v) => fmt.currency(v, ccy);
  const moneyC = (v) => fmt.currency(v, ccy, { compact: true });

  if (pm.unacknowledgedCount > 0 && Array.isArray(pm.alerts) && pm.alerts.length) {
    const ackBtn = isAdmin() ? button({
      label: t('Acknowledge'),
      size: 'sm',
      variant: 'primary',
      onClick: async () => {
        try {
          const res = await api.post('/api/v1/finops/pricing/ack', { all: true });
          if (ctx.onBootChange) {
            ctx.onBootChange({
              pricingAlerts: {
                ...pm,
                unacknowledgedCount: res.data.unacknowledgedCount || 0,
                alerts: res.data.alerts || [],
              },
            });
          }
          toast({ title: t('Pricing alerts acknowledged'), tone: 'success', timeout: 2200 });
          ctx.rerender();
        } catch (err) {
          toast({ title: t('Update failed'), text: errorText(err), tone: 'danger' });
        }
      },
    }) : null;
    body.append(banner({
      tone: 'warning',
      title: t('Public token pricing or USD/KRW rate changed ({n})', { n: pm.unacknowledgedCount }),
      text: pm.alerts.slice(0, 4).map(formatAlertSummary).filter(Boolean).join(' \u00b7 '),
      actions: ackBtn,
    }));
  }

  if (isEstimate) {
    const fxVal = isNum(fx.usdKrw) ? fmt.num(fx.usdKrw, 2) : DASH;
    const fxSrc = fx.source || 'live FX';
    const tokTotal = pm.tokens && isNum(pm.tokens.total) ? fmt.compact(pm.tokens.total) : '0';
    body.append(banner({
      tone: 'info',
      iconName: 'spark',
      title: t('Token-based reverse cost estimation active'),
      text: t('BigQuery Billing Export is not configured. Spend is calculated from observed token usage ({tokens} tokens) multiplied by official Google public per-1M-token rates and live FX (1 USD = {rate} KRW, source: {src}).', {
        tokens: tokTotal, rate: fxVal, src: fxSrc,
      }),
    }));
  }

  body.append(h('div', { class: 'kpi-grid' },
    kpi({
      label: isEstimate ? t('Estimated cost') : t('Gross cost'),
      value: moneyC(k.gross),
      meta: isEstimate && isNum(fx.usdKrw) ? h('span', null, `1 USD = ${fmt.num(fx.usdKrw, 2)} KRW`) : null,
      tip: isEstimate
        ? t('Reverse-calculated from observed token usage across Cloud Monitoring and Antigravity logs using official public token pricing.')
        : t('Sum of cost in the billing export for the selected scope and period.'),
    }),
    k.hasCredits ? kpi({ label: t('Credits'), value: money(k.credits), meta: h('span', null, t('Applied from credit rows in the export')), goodWhen: null }) : null,
    kpi({ label: k.hasCredits ? t('Net cost') : t('Cost vs previous period'), value: k.hasCredits ? moneyC(k.net) : moneyC(k.gross), delta: k.change, goodWhen: 'down', meta: isNum(k.previousNet) ? h('span', null, t('Previous {v}', { v: moneyC(k.previousNet) })) : null }),
    kpi({ label: t('AI share of spend'), value: fmt.pct(k.aiShare), tip: t('Share of cost from AI model SKUs (Agent Platform, Gemini and partner models).') }),
    kpi({ label: t('Spikes detected'), value: fmt.int(k.spikes), goodWhen: null, meta: h('span', null, t('Day-over-day increase at or above threshold')) })));

  const trendSeries = (key) => ((d.trend && d.trend.series) || []).find((s) => s.key === key);
  const spikeDates = new Set((d.spikes || []).map((s) => s.date));
  const buckets = (d.trend && d.trend.buckets) || [];
  const highlight = buckets.map((b, i) => (spikeDates.has(b) ? i : -1)).filter((i) => i >= 0);
  const row1 = h('div', { class: 'grid' });
  const trend = chartCard({
    title: t('Daily cost'), subtitle: rangeText(),
    source: isEstimate ? t('Token usage \u00d7 public pricing') : t('Cloud Billing export'),
    actions: highlight.length ? badge(t('{n} spikes highlighted', { n: highlight.length }), 'danger', { size: 'sm', dot: true }) : null,
    build: (host) => {
      if (k.hasCredits) {
        lineChart(host, {
          buckets, granularity: d.trend.granularity, height: 260, yFormat: moneyC, valueFormat: money,
          series: [
            { key: 'gross', name: t('Gross cost'), values: (trendSeries('gross') || {}).values || [], color: TONES.info },
            { key: 'net', name: t('Net cost'), values: (trendSeries('net') || {}).values || [], color: TONES.teal },
          ],
        });
      } else {
        barChart(host, {
          buckets, granularity: d.trend && d.trend.granularity, height: 260, yFormat: moneyC, valueFormat: money,
          series: [{ key: 'gross', name: t('Cost'), values: (trendSeries('gross') || {}).values || [], color: TONES.info }],
          highlight: store.granularity === 'day' ? highlight : [], highlightColor: TONES.danger,
        });
      }
    },
  });
  trend.classList.add('span-8');
  const services = card({
    title: t('Spend by service'), subtitle: rangeText(),
    body: hbarList(mapList(d.byService, (s, i) => ({ label: s.service, value: s.cost, sub: fmt.pct(s.share), color: colorAt(i) })), { format: moneyC }),
  });
  services.classList.add('span-4');
  body.append(row1);
  mountCharts(row1, [trend, services]);

  const row2 = h('div', { class: 'grid' });
  const maxSku = Math.max(...(d.topSkus || []).map((s) => s.cost || 0), 0) || 1;
  const skuCols = [
    { key: 'sku', label: t('SKU'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main truncate', title: r.sku }, r.sku), h('span', { class: 'cell-sub' }, r.service)) },
  ];
  if (isEstimate) {
    skuCols.push(
      { key: 'tokens', label: t('Tokens'), align: 'num', render: (r) => fmt.compact(r.tokens), value: (r) => r.tokens || 0 },
      { key: 'unitPricePerMillion', label: t('Rate / 1M'), align: 'num', render: (r) => (isNum(r.unitPricePerMillion) ? money(r.unitPricePerMillion) : DASH), value: (r) => r.unitPricePerMillion || 0 },
    );
  }
  skuCols.push(
    { key: 'share', label: t('Share'), align: 'num', render: (r) => h('div', { class: 'share-cell' }, progress((r.cost || 0) / maxSku), h('span', null, fmt.pct(r.share))), value: (r) => r.share },
    { key: 'cost', label: t('Cost'), align: 'num', format: (v) => money(v) },
  );
  const skus = card({
    title: isEstimate ? t('Estimated cost by model & token type') : t('Top SKUs'),
    subtitle: isEstimate ? t('Observed token volume multiplied by synced per-1M public rates') : t('Highest cost SKUs in the period'),
    flush: true,
    body: dataTable({
      columns: skuCols,
      rows: d.topSkus, pageSize: 10, exportName: 'finops-top-skus', initialSort: { key: 'cost', dir: 'desc' },
    }),
  });
  skus.classList.add('span-7');
  const side = k.hasCredits
    ? card({
      title: t('Credits applied'), subtitle: t('By credit type'),
      body: hbarList(mapList(d.credits, (c, i) => ({ label: c.name ? `${c.type} \u00b7 ${c.name}` : c.type, value: Math.abs(c.amount || 0), color: colorAt(i + 3) })), { format: moneyC }),
    })
    : card({
      title: t('Cost policy'), subtitle: t('How spend is measured'),
      body: h('div', { class: 'stack' },
        h('p', { class: 'muted' }, isEstimate
          ? t('Priority 1: BigQuery Billing Export when configured. Priority 2 (active): reverse cost estimation from observed tokens multiplied by official Google public pricing and live USD/KRW FX, checked automatically every day at 04:00 KST.')
          : t('No credit rows were found in the billing export for this period, so gross and net cost are identical.')),
        h('div', { class: 'field-label' }, t('Excluded SKUs')),
        h('div', { class: 'chips' }, (d.excludedSkuPatterns || []).map((p) => chip(p))),
        d.billingAccountId ? h('div', { class: 'muted' }, t('Billing account {id}', { id: d.billingAccountId })) : null),
    });
  side.classList.add('span-5');
  row2.append(skus, side);
  body.append(row2);

  const spikes = d.spikes || [];
  const spikesCard = card({
    title: t('Spike investigations'),
    subtitle: t('Days where cost rose at least {p}% versus the previous day', { p: fmt.int((store.boot.finops && store.boot.finops.spikeThresholdPct) || 50) }),
    body: spikes.length ? h('div', { class: 'stack-lg' }, spikes.map((sp) => spikeBlock(sp, ccy))) : emptyState({ icon: 'trending-up', title: t('No cost spikes'), body: t('No day-over-day cost spike exceeded the configured threshold in this period.'), compact: true }),
  });
  body.append(spikesCard);

  const pricingHost = h('div');
  const pricingCard = card({
    title: t('Public token pricing & live USD/KRW monitor'),
    subtitle: t('Checked automatically every morning at 04:00 KST against official Google pricing pages, Cloud Billing Catalog API and live FX feeds.'),
    body: pricingHost,
  });
  body.append(pricingCard);
  load(pricingHost, () => api.get('/api/v1/finops/pricing', null, { signal: ctx.signal }), (res) => {
    renderPricingMonitor(pricingHost, res.data || {});
  }, { skeletonKind: 'lines', signal: ctx.signal });
}

function renderPricingMonitor(host, p) {
  const fx = p.fx || {};
  const sched = p.schedule || {};
  const models = p.models || [];
  const sources = p.sources || [];
  const fxText = isNum(fx.usdKrw) ? `${fmt.num(fx.usdKrw, 2)} KRW` : DASH;

  const statsRow = h('div', { class: 'stat-row' },
    stat(t('Live USD/KRW rate'), fxText, fx.source ? `${fx.source}${fx.asOf ? ` \u00b7 ${fx.asOf}` : ''}` : null),
    stat(t('Last synced'), p.syncedAt ? fmt.dateTime(p.syncedAt) : t('Pending initial sync'), p.trigger ? t('Trigger: {t}', { t: p.trigger }) : null),
    stat(t('Daily monitor'), `${String(sched.dailyHourLocal ?? 4).padStart(2, '0')}:00 ${sched.timezone || 'Asia/Seoul'}`, t('Automatic early-morning check')),
    stat(t('Tracked model rates'), fmt.int(models.length), p.unacknowledgedCount > 0 ? t('{n} unacknowledged changes', { n: p.unacknowledgedCount }) : t('Up to date')));

  const sourceList = sources.length ? h('div', { class: 'row wrap' },
    sources.map((s) => h('span', { class: 'row' },
      statusBadge(s.status === 'ok' ? 'ok' : 'warning', { size: 'sm' }),
      h('span', { class: 'muted' }, `${s.id}${s.detail ? `: ${s.detail}` : ''}`)))) : null;

  const table = dataTable({
    columns: [
      {
        key: 'label', label: t('Model'),
        render: (m) => h('div', { class: 'cell-stack' },
          h('span', { class: 'cell-main' }, m.label),
          h('span', { class: 'cell-sub cell-mono' }, `${m.vendor || 'Google'} \u00b7 ${m.key}`)),
      },
      {
        key: 'source', label: t('Rate source'),
        render: (m) => badge(t(PRICE_SOURCE_LABELS[m.source] || m.source || 'baseline'), m.source === 'baseline' ? 'neutral' : 'success', { size: 'sm' }),
      },
      {
        key: 'usdIn', label: t('Input / 1M (USD)'), align: 'num',
        value: (m) => (m.usdPerMillion && m.usdPerMillion.input) || 0,
        render: (m) => (m.usdPerMillion && isNum(m.usdPerMillion.input) ? fmt.currency(m.usdPerMillion.input, 'USD', { precise: true }) : DASH),
      },
      {
        key: 'usdOut', label: t('Output / 1M (USD)'), align: 'num',
        value: (m) => (m.usdPerMillion && m.usdPerMillion.output) || 0,
        render: (m) => (m.usdPerMillion && isNum(m.usdPerMillion.output) ? fmt.currency(m.usdPerMillion.output, 'USD', { precise: true }) : DASH),
      },
      {
        key: 'krwIn', label: t('Input / 1M (KRW)'), align: 'num',
        value: (m) => (m.krwPerMillion && m.krwPerMillion.input) || 0,
        render: (m) => (m.krwPerMillion && isNum(m.krwPerMillion.input) ? fmt.currency(m.krwPerMillion.input, 'KRW') : DASH),
      },
      {
        key: 'krwOut', label: t('Output / 1M (KRW)'), align: 'num',
        value: (m) => (m.krwPerMillion && m.krwPerMillion.output) || 0,
        render: (m) => (m.krwPerMillion && isNum(m.krwPerMillion.output) ? fmt.currency(m.krwPerMillion.output, 'KRW') : DASH),
      },
    ],
    rows: models,
    pageSize: 10,
    exportName: 'public-token-pricing',
    initialSort: { key: 'label', dir: 'asc' },
  });

  append(host, [h('div', { class: 'stack' }, statsRow, sourceList, table)]);
}

function spikeBlock(sp, ccy) {
  const money = (v) => fmt.currency(v, ccy);
  const aiHost = h('div');
  const analyze = button({ label: t('Explain with AI'), icon: 'spark', size: 'sm', variant: 'primary' });
  analyze.addEventListener('click', async () => {
    setBusy(analyze, true, t('Analyzing'));
    try {
      const res = await api.post('/api/v1/finops/spikes/analyze', { date: sp.date, project: selectedProject, locale: getLocale(), model: store.model || undefined });
      const a = res.data;
      mount(aiHost, h('div', { class: 'result-card' },
        h('div', { class: 'rc-head' }, h('span', { class: 'row' }, h('strong', null, t('AI analysis')), badge(a.model || '', 'accent', { size: 'sm' })), a.cached ? h('span', { class: 'muted' }, t('Cached result')) : null),
        h('div', { class: 'rc-body stack' },
          markdownLite(a.primaryDriver || ''),
          (a.checkpoints && a.checkpoints.length) ? h('div', null, h('div', { class: 'field-label' }, t('Recommended checks')), markdownLite(a.checkpoints.map((c) => `- ${c}`).join('\n'))) : null)));
      setBusy(analyze, false);
      analyze.hidden = true;
    } catch (err) {
      setBusy(analyze, false);
      toast({ title: t('Analysis failed'), text: errorText(err), tone: 'danger' });
    }
  });
  return h('div', { class: 'stack' },
    h('div', { class: 'row between wrap' },
      h('div', { class: 'row wrap' },
        h('strong', null, fmt.date(sp.date, 'long')),
        badge(`+${fmt.num(sp.changePct, 1)}%`, 'danger', { size: 'sm' }),
        h('span', { class: 'muted' }, t('{prev} to {cur}', { prev: money(sp.previousCost), cur: money(sp.cost) }))),
      analyze),
    dataTable({
      columns: [
        { key: 'sku', label: t('Rising SKU'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main truncate', title: r.sku }, r.sku), h('span', { class: 'cell-sub' }, r.service || '')) },
        { key: 'previous', label: t('Previous day'), align: 'num', format: money },
        { key: 'current', label: t('Spike day'), align: 'num', format: money },
        { key: 'delta', label: t('Increase'), align: 'num', render: (r) => h('span', { class: 'text-danger fw-600' }, `+${money(r.delta)}`), value: (r) => r.delta },
      ],
      rows: sp.risingSkus || [], searchable: false, pageSize: 5, initialSort: { key: 'delta', dir: 'desc' },
    }),
    aiHost);
}
