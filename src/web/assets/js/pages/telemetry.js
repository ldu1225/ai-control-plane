/**
 * Telemetry Extensions: auto-discovered telemetry sources, pluggable connectors and
 * standard dashboards for any new agent or model product (for example CodeMender).
 */
import { h, mount, append } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api, isAbort } from '../core/api.js';
import { store, isAdmin, rangeParams } from '../core/state.js';
import {
  pageHeader, kpi, card, dataTable, badge, statusBadge, button, banner, switchInput, openModal, openDrawer, toast, setBusy, field, textInput,
  selectInput, emptyState, errorState, load, warningsNotice, confirmDialog, codeBlock, chip, userCell,
} from '../core/components.js';
import { errorText } from '../core/messages.js';
import { barChart, hbarList, mapList, sparkline, fromTimeline, TONES, colorAt } from '../core/charts.js';
import { rangeText, chartCard, mountCharts, pageSkeleton, deltaOf } from './_shared.js';

const MAPPING_FIELDS = [
  ['time', 'Timestamp', true],
  ['user', 'User', false],
  ['operation', 'Operation', false],
  ['model', 'Model', false],
  ['tokens', 'Tokens', false],
  ['status', 'Status', false],
];

export default async function render(ctx) {
  if (ctx.params[0]) return renderConnector(ctx, ctx.params[0]);
  return renderList(ctx);
}

/* ------------------------------------------------------------------ list view */

async function renderList(ctx) {
  const admin = isAdmin();
  const header = pageHeader({
    title: t('Telemetry Extensions'),
    desc: t('Bring telemetry from any new agent or model product into the control plane without code changes. Sources are discovered automatically from BigQuery and Cloud Logging.'),
    actions: [
      button({ label: t('Sink filter generator'), icon: 'code', onClick: () => openSinkGenerator([]) }),
      admin ? button({ label: t('Add connector'), icon: 'plus', variant: 'primary', onClick: () => openConnectorForm(ctx, null, null) }) : null,
    ],
  });
  const body = h('div', { class: 'stack-lg' });
  mount(ctx.root, header, body);
  mount(body, pageSkeleton(3, 2));

  let connectors = [];
  let discovery = null;
  try {
    const [cRes, dRes] = await Promise.all([
      api.get('/api/v1/telemetry/connectors', null, { signal: ctx.signal }),
      api.get('/api/v1/telemetry/discover', null, { signal: ctx.signal }).catch((e) => ({ error: e })),
    ]);
    if (ctx.signal.aborted) return null;
    connectors = cRes.data.connectors || [];
    discovery = dRes.error ? null : dRes.data;
    mount(body);
    const notice = warningsNotice([...(cRes.meta.warnings || []), ...((dRes.meta && dRes.meta.warnings) || [])]);
    if (notice) body.append(notice);
    if (dRes.error) body.append(banner({ tone: 'warning', title: t('Discovery unavailable'), text: errorText(dRes.error) }));
  } catch (err) {
    if (isAbort(err) || ctx.signal.aborted) return null;
    mount(body, card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    return null;
  }
  ctx.discovery = discovery;

  const tablesKnown = Boolean(discovery && Array.isArray(discovery.tables));
  const logsKnown = Boolean(discovery && Array.isArray(discovery.logs));
  const tables = tablesKnown ? discovery.tables : [];
  const logs = logsKnown ? discovery.logs : [];
  const candidates = tables.filter((tb) => !tb.core && !tb.registered);
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Active connectors'), value: fmt.int(connectors.filter((c) => c.enabled).length), meta: h('span', null, t('{n} registered', { n: connectors.length })) }),
    kpi({ label: t('New sources detected'), value: tablesKnown ? fmt.int(candidates.length) : DASH, meta: h('span', null, tablesKnown ? t('BigQuery tables not yet connected') : t('Dataset scan unavailable')) }),
    kpi({ label: t('Log streams not in BigQuery'), value: logsKnown ? fmt.int(logs.filter((l) => !l.routedToBigQuery).length) : DASH, meta: h('span', null, logsKnown ? t('Route them with a Log Router sink') : t('Log list unavailable')) })));

  if (candidates.length && admin) {
    body.append(banner({
      tone: 'info', iconName: 'plug',
      title: t('{n} new telemetry sources detected', { n: candidates.length }),
      text: candidates.slice(0, 4).map((c) => `${c.dataset}.${c.logical}`).join(', ') + (candidates.length > 4 ? ' ...' : ''),
    }));
  }

  const grid = h('div', { class: 'connector-grid' });
  body.append(h('div', { class: 'stack' },
    h('div', { class: 'section-title' }, t('Connectors')),
    connectors.length ? grid : card({ body: emptyState({ icon: 'plug', title: t('No connectors yet'), body: t('Register a discovered BigQuery table or a Cloud Logging filter to create a standard telemetry dashboard.'), action: admin ? button({ label: t('Add connector'), icon: 'plus', variant: 'primary', onClick: () => openConnectorForm(ctx, null, null) }) : null }) })));
  connectors.forEach((c) => grid.append(connectorCard(ctx, c)));

  body.append(card({
    title: t('Discovered BigQuery sources'), subtitle: t('Tables found in the audit and telemetry datasets'), flush: true,
    body: dataTable({
      columns: [
        { key: 'logical', label: t('Table'), render: (r) => h('div', { class: 'cell-stack' }, h('div', { class: 'row' }, h('span', { class: 'cell-main cell-mono truncate', title: r.logical }, r.logical), r.sharded ? badge(t('Sharded'), 'neutral', { size: 'sm' }) : null), h('span', { class: 'cell-sub cell-mono' }, `${r.project}.${r.dataset}`)) },
        { key: 'status', label: t('Status'), render: (r) => (r.core ? badge(t('Built-in module'), 'neutral', { size: 'sm' }) : r.registered ? badge(t('Connected'), 'success', { size: 'sm', dot: true }) : badge(t('New'), 'accent', { size: 'sm' })), value: (r) => (r.core ? 2 : r.registered ? 1 : 0) },
        { key: 'rows', label: t('Rows'), align: 'num', format: fmt.compact },
        { key: 'sizeBytes', label: t('Size'), align: 'num', format: fmt.bytes },
        { key: 'lastModified', label: t('Last updated'), render: (r) => (r.lastModified ? fmt.relative(r.lastModified) : DASH), value: (r) => r.lastModified },
        { key: 'mapping', label: t('Detected fields'), sortable: false, render: (r) => h('div', { class: 'chips' }, Object.entries(r.suggestedMapping || {}).filter(([, v]) => v).map(([k]) => chip(t((MAPPING_FIELDS.find((m) => m[0] === k) || [k, k])[1])))), csv: false },
        {
          key: 'actions', label: '', actions: true, sortable: false, align: 'num',
          render: (r) => (admin && !r.core && !r.registered ? button({ label: t('Connect'), size: 'xs', icon: 'plus', onClick: () => openConnectorForm(ctx, null, r) }) : null),
        },
      ],
      rows: tables, pageSize: 10, initialSort: { key: 'status', dir: 'asc' }, exportName: 'telemetry-discovery',
      emptyText: tablesKnown ? t('No tables were found in the scanned datasets.') : t('The datasets could not be scanned. See the data source notices above.'),
    }),
  }));

  body.append(card({
    title: t('Cloud Logging streams'), subtitle: t('Active log names in the project'), flush: true,
    body: dataTable({
      columns: [
        { key: 'shortName', label: t('Log'), render: (r) => h('span', { class: 'cell-mono truncate', title: r.logName }, r.shortName || r.logName) },
        { key: 'routedToBigQuery', label: t('BigQuery'), render: (r) => (r.routedToBigQuery ? badge(t('Routed'), 'success', { size: 'sm', dot: true }) : badge(t('Not routed'), 'warning', { size: 'sm', dot: true })), value: (r) => (r.routedToBigQuery ? 1 : 0) },
        {
          key: 'actions', label: '', actions: true, sortable: false, align: 'num',
          render: (r) => h('div', { class: 'row' },
            !r.routedToBigQuery ? button({ label: t('Sink filter'), size: 'xs', icon: 'code', onClick: () => openSinkGenerator([r.logName], logs) }) : null,
            admin ? button({ label: t('Connect'), size: 'xs', icon: 'plus', onClick: () => openConnectorForm(ctx, null, null, `logName="${r.logName}"`) }) : null),
        },
      ],
      rows: logs, pageSize: 10, initialSort: { key: 'routedToBigQuery', dir: 'asc' },
      emptyText: logsKnown ? t('No log streams were returned for this project.') : t('The log list could not be read. See the data source notices above.'),
    }),
  }));
  return null;
}

function connectorCard(ctx, c) {
  const stats = h('div', { class: 'connector-stats' },
    h('div', null, h('div', { class: 'cs-label' }, t('Events')), h('div', { class: 'cs-value' }, '\u2026')),
    h('div', null, h('div', { class: 'cs-label' }, t('Users')), h('div', { class: 'cs-value' }, '\u2026')),
    h('div', null, h('div', { class: 'cs-label' }, t('Tokens')), h('div', { class: 'cs-value' }, '\u2026')));
  const sparkHost = h('div');
  sparkHost.style.setProperty('height', '30px');
  const el = card({
    cls: 'connector-card',
    title: c.name,
    subtitle: c.source === 'bigquery' ? `BigQuery \u00b7 ${c.bigquery.dataset}.${c.bigquery.table}` : `Cloud Logging \u00b7 ${(c.logging && c.logging.filter) || ''}`.slice(0, 90),
    actions: c.enabled ? badge(t('Enabled'), 'success', { size: 'sm', dot: true }) : badge(t('Disabled'), 'neutral', { size: 'sm' }),
    body: h('div', null, stats, sparkHost),
    footer: h('span', null, c.lastEvent ? t('Last event {time}', { time: fmt.relative(c.lastEvent) }) : t('No events yet')),
    source: c.health === 'error' ? t('Source error') : null,
  });
  el.tabIndex = 0;
  el.addEventListener('click', () => ctx.navigate(`#/extensions/${encodeURIComponent(c.id)}`));
  el.addEventListener('keydown', (e) => { if (e.key === 'Enter') ctx.navigate(`#/extensions/${encodeURIComponent(c.id)}`); });
  if (c.enabled) {
    api.get('/api/v1/telemetry/connectors/data', rangeParams({ id: c.id }), { signal: ctx.signal }).then((res) => {
      const k = res.data.kpis || {};
      const vals = stats.querySelectorAll('.cs-value');
      vals[0].textContent = fmt.compact(k.events && k.events.value);
      vals[1].textContent = fmt.int(k.users);
      vals[2].textContent = isNum(k.tokens) ? fmt.compact(k.tokens) : DASH;
      const ev = ((res.data.trend && res.data.trend.series) || []).find((s) => s.key === 'events');
      if (ev) mount(sparkHost, sparkline(ev.values, TONES.violet));
    }).catch(() => {
      stats.querySelectorAll('.cs-value').forEach((v) => { v.textContent = DASH; });
    });
  } else {
    stats.querySelectorAll('.cs-value').forEach((v) => { v.textContent = DASH; });
  }
  return el;
}

/* ------------------------------------------------------------------ connector dashboard */

async function renderConnector(ctx, id) {
  const admin = isAdmin();
  const body = h('div', { class: 'stack-lg' });
  const headerSlot = h('div');
  mount(ctx.root, headerSlot, body);
  mount(headerSlot, pageHeader({ title: t('Connector'), desc: t('Loading') }));
  mount(body, pageSkeleton(4, 2));
  let res;
  try {
    res = await api.get('/api/v1/telemetry/connectors/data', rangeParams({ id }), { signal: ctx.signal });
  } catch (err) {
    if (isAbort(err) || ctx.signal.aborted) return null;
    mount(headerSlot, pageHeader({ title: t('Connector'), actions: button({ label: t('All connectors'), icon: 'chevron-left', onClick: () => ctx.navigate('#/extensions') }) }));
    mount(body, card({ body: errorState(err, { onRetry: () => ctx.rerender() }) }));
    return null;
  }
  if (ctx.signal.aborted) return null;
  const d = res.data;
  const c = d.connector || {};
  const toggle = switchInput({
    checked: c.enabled, disabled: !admin, label: t('Enabled'),
    onChange: async (v, input) => {
      try {
        await api.post('/api/v1/telemetry/connectors', { ...c, enabled: v });
        toast({ title: v ? t('Connector enabled') : t('Connector disabled'), tone: 'success' });
      } catch (err) {
        input.checked = !v;
        toast({ title: t('Update failed'), text: errorText(err), tone: 'danger' });
      }
    },
  });
  mount(headerSlot, pageHeader({
    title: c.name || id,
    desc: c.source === 'bigquery' ? `BigQuery \u00b7 ${c.bigquery.project}.${c.bigquery.dataset}.${c.bigquery.table}` : `Cloud Logging \u00b7 ${(c.logging && c.logging.filter) || ''}`,
    actions: [
      button({ label: t('All connectors'), icon: 'chevron-left', variant: 'ghost', onClick: () => ctx.navigate('#/extensions') }),
      h('label', { class: 'row' }, h('span', { class: 'muted' }, t('Enabled')), toggle),
      admin ? button({ label: t('Edit'), icon: 'edit', onClick: () => openConnectorForm(ctx, c, null) }) : null,
      admin ? button({ label: t('Delete'), icon: 'trash', variant: 'danger', onClick: () => deleteConnector(ctx, c) }) : null,
    ],
  }));
  mount(body);
  const notice = warningsNotice(res.meta && res.meta.warnings);
  if (notice) body.append(notice);

  const k = d.kpis || {};
  body.append(h('div', { class: 'kpi-grid' },
    kpi({ label: t('Events'), value: fmt.compact(k.events && k.events.value), delta: deltaOf(k.events), goodWhen: null }),
    kpi({ label: t('Active users'), value: fmt.int(k.users) }),
    kpi({ label: t('Tokens'), value: isNum(k.tokens) ? fmt.compact(k.tokens) : DASH, meta: c.mapping && c.mapping.tokens ? null : h('span', null, t('No token field mapped')) }),
    kpi({ label: t('Last event'), value: k.lastEvent ? fmt.relative(k.lastEvent) : DASH })));

  const row = h('div', { class: 'grid' });
  const trend = chartCard({
    title: t('Event volume'), subtitle: rangeText(),
    build: (host) => barChart(host, { ...fromTimeline(d.trend, { include: ['events'], names: { events: t('Events') }, colors: { events: TONES.violet } }), integer: true, height: 240 }),
  });
  trend.classList.add('span-8');
  const shortOp = (op) => {
    if (!op) return t('Unknown');
    const parts = String(op).split('.');
    return parts.length > 3 ? parts.slice(-2).join('.') : String(op);
  };
  const ops = card({ title: t('Top operations'), body: hbarList(mapList(d.topOperations, (o, i) => ({ label: shortOp(o.operation), title: o.operation || '', value: o.events, color: colorAt(i) }))) });
  ops.classList.add('span-4');
  body.append(row);
  mountCharts(row, [trend, ops]);

  const row2 = h('div', { class: 'grid' });
  const usersCard = card({
    title: t('Top users'), flush: true,
    body: dataTable({ columns: [{ key: 'user', label: t('User'), render: (r) => userCell(r.user) }, { key: 'events', label: t('Events'), align: 'num', format: fmt.int }], rows: d.topUsers || [], searchable: false, pageSize: 8, initialSort: { key: 'events', dir: 'desc' } }),
  });
  usersCard.classList.add('span-6');
  const modelsCard = card({
    title: t('Top models'), flush: true,
    body: dataTable({ columns: [{ key: 'model', label: t('Model'), render: (r) => h('span', { class: 'cell-mono' }, r.model || DASH) }, { key: 'events', label: t('Events'), align: 'num', format: fmt.int }, { key: 'tokens', label: t('Tokens'), align: 'num', format: fmt.compact }], rows: d.topModels || [], searchable: false, pageSize: 8, initialSort: { key: 'events', dir: 'desc' }, emptyText: t('No model field mapped or no data.') }),
  });
  modelsCard.classList.add('span-6');
  row2.append(usersCard, modelsCard);
  body.append(row2);

  body.append(card({
    title: t('Recent events'), subtitle: t('Latest 50 events'), flush: true,
    body: dataTable({
      columns: [
        { key: 'time', label: t('Time'), render: (r) => fmt.dateTime(r.time, true), value: (r) => r.time },
        { key: 'user', label: t('User'), render: (r) => h('span', { class: 'truncate' }, r.user || DASH) },
        { key: 'operation', label: t('Operation'), render: (r) => h('span', { class: 'cell-mono truncate', title: r.operation || '' }, r.operation || DASH) },
        { key: 'model', label: t('Model'), render: (r) => h('span', { class: 'cell-mono' }, r.model || DASH) },
        { key: 'tokens', label: t('Tokens'), align: 'num', format: fmt.int },
        { key: 'status', label: t('Status'), render: (r) => (r.status ? badge(String(r.status), 'neutral', { size: 'sm' }) : DASH) },
      ],
      rows: d.recent, pageSize: 10, exportName: `connector-${c.id || id}`,
      onRowClick: (r) => openDrawer({ title: t('Event payload'), subtitle: fmt.dateTime(r.time, true), width: 720, content: codeBlock(prettyJson(r.payload), { wrap: true }) }),
    }),
  }));
  return null;
}

function prettyJson(text) {
  if (!text) return '';
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch (e) { return String(text); }
}

async function deleteConnector(ctx, c) {
  const ok = await confirmDialog({ title: t('Delete connector'), message: t('The connector {name} and its dashboard will be removed. The underlying data is not affected.', { name: c.name }), confirmLabel: t('Delete'), tone: 'danger' });
  if (!ok) return;
  try {
    await api.post('/api/v1/telemetry/connectors/delete', { id: c.id });
    toast({ title: t('Connector deleted'), tone: 'success' });
    ctx.navigate('#/extensions');
  } catch (err) {
    toast({ title: t('Delete failed'), text: errorText(err), tone: 'danger' });
  }
}

/* ------------------------------------------------------------------ connector form */

async function openConnectorForm(ctx, existing, table, logFilter) {
  let discovery = ctx.discovery;
  if (!discovery) {
    try { discovery = (await api.get('/api/v1/telemetry/discover')).data; } catch (e) { discovery = { tables: [], logs: [] }; }
    ctx.discovery = discovery;
  }
  const tables = (discovery.tables || []).filter((tb) => !tb.core || (existing && existing.bigquery && existing.bigquery.table === tb.logical));
  const st = {
    id: existing ? existing.id : undefined,
    name: existing ? existing.name : (table ? prettyName(table.logical) : ''),
    enabled: existing ? existing.enabled : true,
    source: existing ? existing.source : (logFilter ? 'logging' : 'bigquery'),
    tableKey: existing && existing.bigquery ? `${existing.bigquery.project}.${existing.bigquery.dataset}.${existing.bigquery.table}` : (table ? `${table.project}.${table.dataset}.${table.logical}` : ''),
    filter: existing && existing.logging ? existing.logging.filter : (logFilter || ''),
    mapping: existing ? { ...existing.mapping } : (table ? { ...(table.suggestedMapping || {}) } : {}),
  };
  const err = h('div', { class: 'field-error' });
  const sourceHost = h('div', { class: 'stack' });
  const mappingHost = h('div', { class: 'field-row' });
  const nameIn = textInput({ value: st.name, placeholder: t('For example CodeMender'), onInput: (v) => { st.name = v; } });

  const findTable = () => tables.find((tb) => `${tb.project}.${tb.dataset}.${tb.logical}` === st.tableKey);
  const renderMapping = () => {
    const tb = findTable();
    mount(mappingHost, MAPPING_FIELDS.map(([key, label, required]) => {
      let control;
      if (st.source === 'bigquery') {
        const cols = (tb && tb.columns) || [];
        control = selectInput([{ value: '', label: required ? t('Select a column') : t('Not mapped') }, ...cols.map((cname) => ({ value: cname, label: cname }))], st.mapping[key] || '', (v) => { st.mapping[key] = v || null; }, { 'aria-label': t(label) });
      } else {
        control = textInput({ value: st.mapping[key] || '', mono: true, placeholder: key === 'time' ? 'timestamp' : key === 'user' ? 'labels.user_id' : key === 'tokens' ? 'jsonPayload.metadata.totalTokenCount' : '', onInput: (v) => { st.mapping[key] = v.trim() || null; } });
      }
      return field({ label: `${t(label)}${required ? ' *' : ''}`, control });
    }));
  };
  const renderSource = () => {
    if (st.source === 'bigquery') {
      mount(sourceHost, field({
        label: t('BigQuery table'),
        help: t('Tables discovered in the audit and telemetry datasets.'),
        control: selectInput([{ value: '', label: t('Select a table') }, ...tables.map((tb) => ({ value: `${tb.project}.${tb.dataset}.${tb.logical}`, label: `${tb.dataset}.${tb.logical}${tb.sharded ? ' (sharded)' : ''}` }))], st.tableKey, (v) => {
          st.tableKey = v;
          const tb = findTable();
          if (tb) { st.mapping = { ...(tb.suggestedMapping || {}) }; if (!st.name) { st.name = prettyName(tb.logical); nameIn.value = st.name; } }
          renderMapping();
        }),
      }));
    } else {
      const ta = h('textarea', { class: 'textarea mono', rows: 3, placeholder: 'logName:"codemender"', maxlength: 2000 });
      ta.value = st.filter;
      ta.addEventListener('input', () => { st.filter = ta.value; });
      mount(sourceHost, field({ label: t('Cloud Logging filter'), help: t('Any Logging query language filter. Entries are read through the Logging API for the selected period.'), control: ta }));
    }
    renderMapping();
  };
  const save = button({ label: existing ? t('Save connector') : t('Create connector'), variant: 'primary' });
  const m = openModal({
    title: existing ? t('Edit connector') : t('Add telemetry connector'),
    subtitle: t('Map the source fields once; the control plane builds a standard dashboard for it.'),
    wide: true,
    body: (el) => append(el, [
      h('div', { class: 'field-row' },
        field({ label: t('Display name'), control: nameIn }),
        field({ label: t('Source type'), control: selectInput([{ value: 'bigquery', label: t('BigQuery table') }, { value: 'logging', label: t('Cloud Logging filter') }], st.source, (v) => { st.source = v; st.mapping = {}; renderSource(); }) })),
      sourceHost,
      h('div', { class: 'field-label' }, t('Field mapping')),
      mappingHost,
      h('label', { class: 'row' }, switchInput({ checked: st.enabled, label: t('Enable connector'), onChange: (v) => { st.enabled = v; } }), h('span', null, t('Enable connector and show it on dashboards'))),
      err]),
    footer: (el) => append(el, [button({ label: t('Cancel'), onClick: () => m.close() }), save]),
  });
  renderSource();
  save.addEventListener('click', async () => {
    err.textContent = '';
    if (!st.name.trim()) { err.textContent = t('Enter a display name.'); return; }
    if (!st.mapping.time) { err.textContent = t('Map the timestamp field.'); return; }
    const payload = { id: st.id, name: st.name.trim(), enabled: st.enabled, source: st.source, mapping: st.mapping };
    if (st.source === 'bigquery') {
      const tb = findTable();
      if (!tb) { err.textContent = t('Select a BigQuery table.'); return; }
      payload.bigquery = { project: tb.project, dataset: tb.dataset, table: tb.logical };
    } else {
      if (!st.filter.trim()) { err.textContent = t('Enter a Cloud Logging filter.'); return; }
      payload.logging = { filter: st.filter.trim() };
    }
    setBusy(save, true, t('Saving'));
    try {
      const res = await api.post('/api/v1/telemetry/connectors', payload);
      toast({ title: existing ? t('Connector saved') : t('Connector created'), tone: 'success' });
      m.close();
      const created = res.data && (res.data.connector || res.data);
      if (!existing && created && created.id) ctx.navigate(`#/extensions/${encodeURIComponent(created.id)}`);
      else ctx.rerender();
    } catch (e) {
      setBusy(save, false);
      err.textContent = errorText(e);
    }
  });
}

function prettyName(logical) {
  return String(logical || '').replace(/_googleapis_com/g, '').split('_').filter(Boolean).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

/* ------------------------------------------------------------------ sink filter generator */

function openSinkGenerator(preselected = [], logs = []) {
  const ws = (store.boot && store.boot.workspace) || {};
  const project = ws.projectId || 'PROJECT_ID';
  const selected = new Set(preselected);
  const st = { dataset: ws.auditDataset || 'ge_analytics', sink: 'control-plane-telemetry', custom: '' };
  const out = h('div', { class: 'stack' });
  const known = [...new Set([...(logs || []).filter((l) => !l.routedToBigQuery).map((l) => l.logName), ...preselected])];
  const build = () => {
    const parts = [...selected].map((ln) => `logName="${ln}"`);
    if (st.custom.trim()) parts.push(`(${st.custom.trim()})`);
    const filter = parts.join(' OR ') || `logName="projects/${project}/logs/LOG_ID"`;
    const cmd = [
      `gcloud logging sinks create ${st.sink} \\`,
      `  bigquery.googleapis.com/projects/${project}/datasets/${st.dataset} \\`,
      `  --log-filter='${filter.replace(/'/g, "'\\''")}' \\`,
      '  --use-partitioned-tables \\',
      `  --project=${project}`,
      '',
      '# Grant the sink writer identity access to the dataset:',
      `WRITER=$(gcloud logging sinks describe ${st.sink} --project=${project} --format='value(writerIdentity)')`,
      `bq add-iam-policy-binding --member="$WRITER" --role=roles/bigquery.dataEditor ${project}:${st.dataset}`,
    ].join('\n');
    mount(out,
      field({ label: t('Log Router filter'), control: codeBlock(filter, { wrap: true }) }),
      field({ label: t('gcloud commands'), control: codeBlock(cmd) }));
  };
  const datasetIn = textInput({ value: st.dataset, mono: true, onInput: (v) => { st.dataset = v.replace(/[^A-Za-z0-9_]/g, ''); build(); } });
  const sinkIn = textInput({ value: st.sink, mono: true, onInput: (v) => { st.sink = v.replace(/[^A-Za-z0-9_\-.]/g, ''); build(); } });
  const custom = h('textarea', { class: 'textarea mono', rows: 2, placeholder: 'logName:"codemender"' });
  custom.addEventListener('input', () => { st.custom = custom.value; build(); });
  openModal({
    title: t('Sink filter generator'),
    subtitle: t('Route new product telemetry to BigQuery so it can be discovered and connected.'),
    wide: true,
    body: (el) => {
      append(el, [
        h('div', { class: 'field-row' }, field({ label: t('Sink name'), control: sinkIn }), field({ label: t('Destination dataset'), control: datasetIn })),
        known.length ? field({ label: t('Log streams'), control: h('div', { class: 'stack-sm' }, known.map((ln) => {
          const cb = h('input', { type: 'checkbox', class: 'checkbox', checked: selected.has(ln) });
          cb.addEventListener('change', () => { if (cb.checked) selected.add(ln); else selected.delete(ln); build(); });
          return h('label', { class: 'row' }, cb, h('span', { class: 'cell-mono' }, ln));
        })) }) : null,
        field({ label: t('Additional filter (optional)'), help: t('For example a product log name or resource type.'), control: custom }),
        out,
      ]);
      build();
    },
  });
}
