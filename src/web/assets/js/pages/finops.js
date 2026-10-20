/**
 * Cost & FinOps: gross/net spend, credits, services, SKUs and spike investigations.
 */
import { h, mount } from '../core/dom.js';
import { t, getLocale } from '../core/i18n.js';
import { fmt, isNum, DASH, addDays } from '../core/format.js';
import { api } from '../core/api.js';
import { store, rangeParams } from '../core/state.js';
import {
  pageHeader, kpi, card, dataTable, badge, button, banner, progress, chip, selectInput, setBusy, toast, emptyState, markdownLite,
} from '../core/components.js';
import { errorText } from '../core/messages.js';
import { barChart, lineChart, hbarList, mapList, TONES, colorAt } from '../core/charts.js';
import { pageFlow, rangeText, chartCard, mountCharts } from './_shared.js';

let selectedProject = 'all';

export default async function render(ctx) {
  const projectSlot = h('div', { class: 'row' });
  const header = pageHeader({
    title: t('Cost & FinOps'),
    desc: t('Billing export spend with credits, service and SKU breakdowns, and AI-assisted investigation of cost spikes.'),
    actions: projectSlot,
  });
  return pageFlow(ctx, {
    header,
    kpiCount: 5,
    fetcher: () => api.get('/api/v1/finops/summary', rangeParams({ project: selectedProject }), { signal: ctx.signal }),
    render: (body, d) => {
      const projects = ['all', ...(d.projects || []).filter((p) => p !== 'all')];
      if (!projects.includes(selectedProject)) selectedProject = 'all';
      mount(projectSlot, h('span', { class: 'muted' }, t('Billing scope')), selectInput(projects.map((p) => ({ value: p, label: p === 'all' ? t('All projects') : p })), selectedProject, (v) => { selectedProject = v; ctx.rerender(); }, { 'aria-label': t('Billing scope') }));
      return renderPage(ctx, body, d);
    },
  });
}

function renderPage(ctx, body, d) {
  const ccy = d.currency || 'USD';
  store.currency = ccy;
  const k = d.kpis || {};
  const money = (v) => fmt.currency(v, ccy);
  const moneyC = (v) => fmt.currency(v, ccy, { compact: true });
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Gross cost'), value: moneyC(k.gross), tip: t('Sum of cost in the billing export for the selected scope and period.') }),
    k.hasCredits ? kpi({ label: t('Credits'), value: money(k.credits), meta: h('span', null, t('Applied from credit rows in the export')), goodWhen: null }) : null,
    kpi({ label: k.hasCredits ? t('Net cost') : t('Cost vs previous period'), value: k.hasCredits ? moneyC(k.net) : moneyC(k.gross), delta: k.change, goodWhen: 'down', meta: isNum(k.previousNet) ? h('span', null, t('Previous {v}', { v: moneyC(k.previousNet) })) : null }),
    kpi({ label: t('AI share of spend'), value: fmt.pct(k.aiShare), tip: t('Share of cost from AI model SKUs (Vertex AI, Gemini and partner models).') }),
    kpi({ label: t('Spikes detected'), value: fmt.int(k.spikes), goodWhen: null, meta: h('span', null, t('Day-over-day increase at or above threshold')) })));

  const trendSeries = (key) => ((d.trend && d.trend.series) || []).find((s) => s.key === key);
  const spikeDates = new Set((d.spikes || []).map((s) => s.date));
  const buckets = (d.trend && d.trend.buckets) || [];
  const highlight = buckets.map((b, i) => (spikeDates.has(b) ? i : -1)).filter((i) => i >= 0);
  const row1 = h('div', { class: 'grid' });
  const trend = chartCard({
    title: t('Daily cost'), subtitle: rangeText(), source: t('Cloud Billing export'),
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
  const skus = card({
    title: t('Top SKUs'), subtitle: t('Highest cost SKUs in the period'), flush: true,
    body: dataTable({
      columns: [
        { key: 'sku', label: t('SKU'), render: (r) => h('div', { class: 'cell-stack' }, h('span', { class: 'cell-main truncate', title: r.sku }, r.sku), h('span', { class: 'cell-sub' }, r.service)) },
        { key: 'share', label: t('Share'), align: 'num', render: (r) => h('div', { class: 'share-cell' }, progress((r.cost || 0) / maxSku), h('span', null, fmt.pct(r.share))), value: (r) => r.share },
        { key: 'cost', label: t('Cost'), align: 'num', format: (v) => money(v) },
      ],
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
        h('p', { class: 'muted' }, t('No credit rows were found in the billing export for this period, so gross and net cost are identical.')),
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
