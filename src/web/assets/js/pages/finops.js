/**
 * Cost & FinOps: gross/net spend, credits, services, SKUs, spike investigations,
 * token-based reverse cost estimation fallback, and official pricing & multi-currency FX monitor.
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
import { errorText, serverText } from '../core/messages.js';
import { barChart, lineChart, hbarList, mapList, TONES, colorAt } from '../core/charts.js';
import { pageFlow, rangeText, chartCard, mountCharts } from './_shared.js';

let selectedProject = 'all';
let selectedCurrency = null;

const SOURCE_LABELS = {
  cloud_billing_catalog: 'Cloud Billing Catalog',
  override: 'Administrator price',
  alias: 'Model alias',
};

export function fxRateText(rate) {
  if (!isNum(rate)) return DASH;
  if (rate >= 100) return fmt.num(rate, 2);
  if (rate >= 1) return fmt.num(rate, 4);
  return fmt.num(rate, 6);
}

export function fxSourceLabel(source) {
  if (!source) return '';
  if (source === 'cloud_billing_catalog') return t('Cloud Billing Catalog');
  if (source === 'identity') return 'USD = USD';
  if (source.startsWith('fx:')) return source.slice(3);
  return source;
}

export function priceSourceLabel(source) {
  if (!source) return DASH;
  if (SOURCE_LABELS[source]) return t(SOURCE_LABELS[source]);
  if (source.startsWith('page:')) return `${t('Official pricing page')} (${source.slice(5)})`;
  if (source.startsWith('alias:')) return `${t('Alias')} \u2192 ${source.slice(6)}`;
  return source;
}

export function pricingSyncText(p, preferredCurrency) {
  const ccy = (preferredCurrency || (p && p.displayCurrency) || 'USD').toUpperCase();
  const modelsCount = Array.isArray(p && p.models) ? p.models.length : 0;
  const rates = (p && p.fx && p.fx.rates) || {};
  if (ccy !== 'USD' && rates[ccy] && isNum(rates[ccy].rate)) {
    return t('1 USD = {rate} {currency} \u00b7 {n} models synced', {
      rate: fxRateText(rates[ccy].rate),
      currency: ccy,
      n: fmt.int(modelsCount),
    });
  }
  const rateKeys = Object.keys(rates).filter((k) => rates[k] && isNum(rates[k].rate));
  if (rateKeys.length) {
    return t('{n} models \u00b7 {k} exchange rates synced', {
      n: fmt.int(modelsCount),
      k: fmt.int(rateKeys.length),
    });
  }
  return t('{n} models synced', { n: fmt.int(modelsCount) });
}

export function formatPricingAlert(a) {
  if (!a) return '';
  if (a.kind === 'fx') {
    const sign = isNum(a.pctChange) && a.pctChange > 0 ? '+' : '';
    const pair = a.target || `USD/${a.currency || ''}`;
    const ccy = a.currency || (pair.includes('/') ? pair.split('/')[1] : '');
    return `${pair}: ${fxRateText(a.before)} \u2192 ${fxRateText(a.after)}${ccy ? ` ${ccy}` : ''} (${sign}${fmt.num(a.pctChange, 2)}%)`;
  }
  if (a.kind === 'new_model') {
    return `${a.label || a.target}: ${t('New official model price')} (${fmt.currency(a.after, 'USD', { precise: true })} / 1M)`;
  }
  const sign = isNum(a.pctChange) && a.pctChange > 0 ? '+' : '';
  const fieldLabel = a.field ? t(a.field.charAt(0).toUpperCase() + a.field.slice(1)) : '';
  return `${a.label || a.target}${fieldLabel ? ` (${fieldLabel})` : ''}: ${fmt.currency(a.before, 'USD', { precise: true })} \u2192 ${fmt.currency(a.after, 'USD', { precise: true })} / 1M (${sign}${fmt.num(a.pctChange, 1)}%)`;
}

export default async function render(ctx) {
  const actionSlot = h('div', { class: 'row wrap' });
  const header = pageHeader({
    title: t('Cost & FinOps'),
    desc: t('Billing export spend (or automatic token-based cost estimation from official Google prices and synced exchange rates), service and SKU breakdowns, and AI spike investigation.'),
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
        label: t('Check prices now'),
        icon: 'refresh',
        size: 'sm',
        onClick: async () => {
          if (!requireAdmin(t('Check official prices and exchange rates'))) return;
          setBusy(syncBtn, true, t('Checking'));
          try {
            const res = await api.post('/api/v1/finops/pricing/sync', {});
            const p = res.data || {};
            if (ctx.onBootChange) {
              ctx.onBootChange({
                pricingAlerts: {
                  syncedAt: p.syncedAt,
                  trigger: p.trigger,
                  fx: p.fx,
                  schedule: p.schedule,
                  unacknowledgedCount: p.unacknowledgedCount || 0,
                  alerts: (p.alerts || []).filter((x) => !x.acknowledgedAt).slice(0, 10),
                },
              });
            }
            toast({
              title: t('Prices and exchange rates synced'),
              text: pricingSyncText(p, activeCcy),
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

      const settingsBtn = button({
        label: t('Pricing settings'),
        icon: 'sliders',
        size: 'sm',
        variant: 'ghost',
        onClick: () => ctx.navigate('#/settings/data-collection'),
      });

      const ccyToggle = currencies.length > 1
        ? (currencies.length <= 4
          ? segmented(currencies.map((c) => ({ value: c, label: c })), activeCcy, (v) => { selectedCurrency = v; ctx.rerender(); }, { label: t('Currency') })
          : selectInput(currencies.map((c) => ({ value: c, label: c })), activeCcy, (v) => { selectedCurrency = v; ctx.rerender(); }, { 'aria-label': t('Currency') }))
        : null;

      mount(actionSlot,
        ccyToggle,
        h('span', { class: 'muted' }, t('Billing scope')),
        selectInput(projects.map((p) => ({ value: p, label: p === 'all' ? t('All projects') : p })), selectedProject, (v) => { selectedProject = v; ctx.rerender(); }, { 'aria-label': t('Billing scope') }),
        syncBtn,
        settingsBtn);
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
  const sched = pm.schedule || {};
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
      title: t('Official token prices or exchange rates changed ({n})', { n: pm.unacknowledgedCount }),
      text: pm.alerts.slice(0, 4).map(formatPricingAlert).filter(Boolean).join(' \u00b7 '),
      actions: ackBtn,
    }));
  }

  if (isEstimate) {
    const tokTotal = pm.tokens && isNum(pm.tokens.total) ? fmt.compact(pm.tokens.total) : '0';
    const fxNote = ccy !== 'USD' && isNum(fx.rate)
      ? t('1 USD = {rate} {currency} ({source})', { rate: fxRateText(fx.rate), currency: ccy, source: fxSourceLabel(fx.source) || t('exchange rate feed') })
      : 'USD';
    const baseEstText = t('BigQuery Billing Export is not configured. Spend is calculated from observed token usage ({tokens} tokens) multiplied by official Google per-1M-token rates ({fx}).', {
      tokens: tokTotal, fx: fxNote,
    });
    const nonTokenNote = t('Non-token media units (video seconds, audio clips, speech minutes) and VM/storage infrastructure require BigQuery Billing Export for actual usage totals.');
    body.append(banner({
      tone: 'info',
      iconName: 'spark',
      title: t('Token-based reverse cost estimation active'),
      text: `${baseEstText} ${nonTokenNote}`,
    }));
  }

  const unpriced = Array.isArray(pm.unpricedModels) ? pm.unpricedModels : [];
  if (unpriced.length) {
    body.append(banner({
      tone: 'warning',
      title: t('{n} models have token usage without an official price', { n: unpriced.length }),
      text: t('Tokens of unpriced models are excluded from cost estimates until an alias or administrator price is set in Settings > Data collection and cost.'),
      actions: h('div', { class: 'row wrap' },
        ...unpriced.slice(0, 3).flatMap((u) => [
          button({
            label: t('Set price: {model}', { model: u.model }),
            size: 'sm',
            variant: 'primary',
            onClick: () => ctx.navigate(`#/settings/data-collection?price=${encodeURIComponent(u.model)}`),
          }),
          button({
            label: t('Map alias: {model}', { model: u.model }),
            size: 'sm',
            variant: 'ghost',
            onClick: () => ctx.navigate(`#/settings/data-collection?alias=${encodeURIComponent(u.model)}`),
          }),
        ])),
    }));
  }

  const fxMeta = isEstimate && ccy !== 'USD' && isNum(fx.rate)
    ? h('span', null, t('1 USD = {rate} {currency}', { rate: fxRateText(fx.rate), currency: ccy }))
    : null;

  body.append(h('div', { class: 'kpi-grid' },
    kpi({
      label: isEstimate ? t('Estimated cost') : t('Gross cost'),
      value: moneyC(k.gross),
      meta: fxMeta,
      tip: isEstimate
        ? t('Reverse-calculated from observed token usage across Cloud Monitoring and Antigravity logs using official Google token prices.')
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
    source: isEstimate ? t('Token usage \u00d7 official pricing') : t('Cloud Billing export'),
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
      { key: 'unitPricePerMillion', label: t('Rate / 1M'), align: 'num', render: (r) => (isNum(r.unitPricePerMillion) ? fmt.currency(r.unitPricePerMillion, ccy, { precise: true }) : DASH), value: (r) => r.unitPricePerMillion || 0 },
    );
  }
  skuCols.push(
    { key: 'share', label: t('Share'), align: 'num', render: (r) => h('div', { class: 'share-cell' }, progress((r.cost || 0) / maxSku), h('span', null, fmt.pct(r.share))), value: (r) => r.share },
    { key: 'cost', label: t('Cost'), align: 'num', format: (v) => money(v) },
  );
  const skus = card({
    title: isEstimate ? t('Estimated cost by model & token type') : t('Top SKUs'),
    subtitle: isEstimate ? t('Observed token volume multiplied by synced official per-1M rates') : t('Highest cost SKUs in the period'),
    flush: true,
    body: dataTable({
      columns: skuCols,
      rows: d.topSkus, pageSize: 10, exportName: 'finops-top-skus', initialSort: { key: 'cost', dir: 'desc' },
    }),
  });
  skus.classList.add('span-7');

  const scheduleText = sched.time
    ? t('Checked automatically every day at {time} ({timezone}).', { time: sched.time, timezone: sched.timezone || 'UTC' })
    : t('Checked automatically on the configured daily schedule.');

  const side = k.hasCredits
    ? card({
      title: t('Credits applied'), subtitle: t('By credit type'),
      body: hbarList(mapList(d.credits, (c, i) => ({ label: c.name ? `${c.type} \u00b7 ${c.name}` : c.type, value: Math.abs(c.amount || 0), color: colorAt(i + 3) })), { format: moneyC }),
    })
    : card({
      title: t('Cost policy'), subtitle: t('How spend is measured'),
      body: h('div', { class: 'stack' },
        h('p', { class: 'muted' }, isEstimate
          ? `${t('Priority 1: BigQuery Billing Export when configured. Priority 2 (active): reverse cost estimation from observed tokens multiplied by official Google token prices and synced exchange rates.')} ${scheduleText}`
          : t('No credit rows were found in the billing export for this period, so gross and net cost are identical.')),
        (d.excludedSkuPatterns && d.excludedSkuPatterns.length) ? h('div', { class: 'field-label' }, t('Excluded SKUs')) : null,
        (d.excludedSkuPatterns && d.excludedSkuPatterns.length) ? h('div', { class: 'chips' }, d.excludedSkuPatterns.map((p) => chip(p))) : null,
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
    title: t('Official token pricing & exchange rate monitor'),
    subtitle: t('Synced from Cloud Billing Catalog API, official Google pricing pages and configured exchange rate feeds.'),
    body: pricingHost,
  });
  body.append(pricingCard);
  load(pricingHost, () => api.get('/api/v1/finops/pricing', { currency: ccy }, { signal: ctx.signal }), (res) => {
    renderPricingMonitor(ctx, pricingHost, res.data || {}, ccy);
  }, { skeletonKind: 'lines', signal: ctx.signal });
}

function renderPricingMonitor(ctx, host, p, activeCcy) {
  const fx = p.fx || {};
  const rates = fx.rates || {};
  const sched = p.schedule || {};
  const models = p.models || [];
  const unitPrices = Array.isArray(p.unitPrices) ? p.unitPrices : [];
  const sources = p.sources || [];
  const storage = p.storage || null;
  const dispCcy = (p.displayCurrency || activeCcy || 'USD').toUpperCase();
  const dispRateObj = rates[dispCcy] || null;
  const dispRate = isNum(p.displayRate) ? p.displayRate : (dispRateObj && isNum(dispRateObj.rate) ? dispRateObj.rate : (dispCcy === 'USD' ? 1 : null));

  const fxStatVal = dispCcy === 'USD'
    ? '1 USD'
    : (isNum(dispRate) ? `${fxRateText(dispRate)} ${dispCcy}` : DASH);
  const fxStatSub = dispCcy === 'USD'
    ? t('Base pricing currency')
    : (dispRateObj ? `${fxSourceLabel(dispRateObj.source)}${dispRateObj.asOf ? ` \u00b7 ${dispRateObj.asOf}` : ''}` : t('Rate not synced yet'));

  const schedLabel = sched.time ? `${sched.time} (${sched.timezone || 'UTC'})` : DASH;
  const schedSub = sched.nextRunAt ? t('Next check {when}', { when: fmt.relative(sched.nextRunAt) }) : t('Automatic daily check');

  const statsRow = h('div', { class: 'stat-row' },
    stat(dispCcy === 'USD' ? t('Pricing currency') : t('Exchange rate (USD/{currency})', { currency: dispCcy }), fxStatVal, fxStatSub),
    stat(t('Last synced'), p.syncedAt ? fmt.dateTime(p.syncedAt) : t('Not checked yet'), p.trigger ? t('Trigger: {t}', { t: p.trigger }) : null),
    stat(t('Daily schedule'), schedLabel, schedSub),
    stat(t('Tracked model rates'), fmt.int(models.length), p.unacknowledgedCount > 0 ? t('{n} unacknowledged changes', { n: p.unacknowledgedCount }) : t('Up to date')),
    unitPrices.length ? stat(t('Media & speech unit rates'), fmt.int(unitPrices.length), t('Catalog list prices')) : null);

  const otherRates = Object.entries(rates).filter(([code, r]) => code !== dispCcy && r && isNum(r.rate));
  const fxChips = otherRates.length
    ? h('div', { class: 'chips' }, otherRates.slice(0, 12).map(([code, r]) => chip(`1 USD = ${fxRateText(r.rate)} ${code}`, { title: fxSourceLabel(r.source) })))
    : null;

  const storageLine = storage && (isNum(storage.ingestPerGiB) || isNum(storage.storagePerGiBMonth))
    ? h('div', { class: 'muted' }, t('{service} routing list prices: streaming insert {ingest} / GiB \u00b7 active storage {storage} / GiB-month (USD)', {
      service: storage.service || 'BigQuery',
      ingest: isNum(storage.ingestPerGiB) ? fmt.currency(storage.ingestPerGiB, 'USD', { precise: true }) : DASH,
      storage: isNum(storage.storagePerGiBMonth) ? fmt.currency(storage.storagePerGiBMonth, 'USD', { precise: true }) : DASH,
    }))
    : null;

  const sourceList = sources.length ? h('div', { class: 'row wrap' },
    sources.map((s) => h('span', { class: 'row' },
      statusBadge(s.status === 'ok' ? 'ok' : 'warning', { size: 'sm' }),
      h('span', { class: 'muted' }, `${s.id}${s.detail ? `: ${serverText(s.detail)}` : ''}`)))) : null;

  const columns = [
    {
      key: 'label', label: t('Model'),
      render: (m) => h('div', { class: 'cell-stack' },
        h('span', { class: 'cell-main' }, m.label || m.key),
        h('span', { class: 'cell-sub cell-mono' }, `${m.vendor || 'Google'} \u00b7 ${m.key}`)),
    },
    {
      key: 'source', label: t('Rate source'),
      render: (m) => badge(priceSourceLabel(m.source), m.source === 'override' ? 'accent' : 'success', { size: 'sm' }),
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
      key: 'usdCached', label: t('Cached / 1M (USD)'), align: 'num',
      value: (m) => (m.usdPerMillion && m.usdPerMillion.cached) || 0,
      render: (m) => (m.usdPerMillion && isNum(m.usdPerMillion.cached) ? fmt.currency(m.usdPerMillion.cached, 'USD', { precise: true }) : DASH),
    },
  ];
  if (dispCcy !== 'USD') {
    columns.push(
      {
        key: 'localIn', label: t('Input / 1M ({currency})', { currency: dispCcy }), align: 'num',
        value: (m) => (m.localPerMillion && m.localPerMillion.input) || 0,
        render: (m) => (m.localPerMillion && isNum(m.localPerMillion.input) ? fmt.currency(m.localPerMillion.input, dispCcy, { precise: true }) : DASH),
      },
      {
        key: 'localOut', label: t('Output / 1M ({currency})', { currency: dispCcy }), align: 'num',
        value: (m) => (m.localPerMillion && m.localPerMillion.output) || 0,
        render: (m) => (m.localPerMillion && isNum(m.localPerMillion.output) ? fmt.currency(m.localPerMillion.output, dispCcy, { precise: true }) : DASH),
      },
    );
  }

  const table = models.length
    ? dataTable({
      columns,
      rows: models,
      pageSize: 10,
      exportName: 'official-token-pricing',
      initialSort: { key: 'label', dir: 'asc' },
    })
    : emptyState({
      icon: 'refresh',
      title: t('No synced model prices yet'),
      body: t('Select Check prices now to fetch official token prices from Cloud Billing Catalog API and Google pricing pages, or add administrator prices in Settings > Data collection and cost.'),
      compact: true,
    });

  let unitSection = null;
  if (unitPrices.length) {
    const unitCols = [
      { key: 'category', label: t('Category'), render: (u) => badge(t(u.category || ''), 'neutral', { size: 'sm' }), value: (u) => u.category || '' },
      {
        key: 'label', label: t('SKU'),
        render: (u) => h('div', { class: 'cell-stack' },
          h('span', { class: 'cell-main' }, u.label || u.key),
          h('span', { class: 'cell-sub' }, u.service || '')),
      },
      { key: 'unitLabel', label: t('Billing unit'), render: (u) => h('span', { class: 'cell-mono' }, u.unitLabel || u.unit || '') },
      {
        key: 'usdPrice', label: t('List price (USD)'), align: 'num',
        value: (u) => u.usdPrice || 0,
        render: (u) => (isNum(u.usdPrice) ? fmt.currency(u.usdPrice, 'USD', { precise: true }) : DASH),
      },
    ];
    if (dispCcy !== 'USD') {
      unitCols.push({
        key: 'localPrice', label: t('List price ({currency})', { currency: dispCcy }), align: 'num',
        value: (u) => u.localPrice || 0,
        render: (u) => (isNum(u.localPrice) ? fmt.currency(u.localPrice, dispCcy, { precise: true }) : DASH),
      });
    }
    unitSection = h('div', { class: 'stack' },
      h('div', { class: 'stack-xs' },
        h('strong', null, t('Official media & speech unit prices (non-token SKUs)')),
        h('div', { class: 'muted' }, t('Synced from Cloud Billing Catalog API for video, music, image, Text-to-Speech character and Speech-to-Text minute SKUs. Actual billed quantities require BigQuery Billing Export.'))),
      dataTable({
        columns: unitCols,
        rows: unitPrices,
        pageSize: 8,
        exportName: 'official-unit-pricing',
        initialSort: { key: 'category', dir: 'asc' },
      }));
  }

  append(host, [h('div', { class: 'stack' }, statsRow, fxChips, storageLine, sourceList, table, unitSection)]);
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
