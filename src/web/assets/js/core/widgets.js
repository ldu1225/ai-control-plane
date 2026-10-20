/**
 * Personal widget boards. Each person pins Telemetry Extensions dashboards to the Overview or any other
 * page, chooses what each widget shows (summary, daily chart, a breakdown or the latest entries) and its
 * size, and arranges the widgets by drag and drop, with the arrow keys on the drag handle or from the
 * widget menu. Layouts are saved per person on the server (GET/POST /api/v1/layouts); widget data comes
 * from /api/v1/telemetry/connectors/data for the global period, so masking and caching match the
 * Telemetry Extensions dashboards.
 */
import { h, mount } from './dom.js';
import { icon } from './icons.js';
import { t } from './i18n.js';
import { fmt, isNum, DASH } from './format.js';
import { api, isAbort } from './api.js';
import { isModuleEnabled, rangeParams } from './state.js';
import { button, iconButton, badge, openModal, openPopover, menuItem, toast, segmented, field } from './components.js';
import { errorText } from './messages.js';
import { barChart, hbarList, mapList, fromTimeline, TONES, colorAt } from './charts.js';
import { cardWhat, shortValue, statusText } from './telemetry_view.js';

const VIEWS = ['summary', 'trend', 'operations', 'statuses', 'recent'];
const SIZES = [['s', 'Small'], ['m', 'Medium'], ['l', 'Wide']];
const MAX_WIDGETS = 24;

/* ------------------------------------------------------------------ layouts (per person, server side) */

let layouts = null;      // {page: [widget]} once loaded
let editable = true;     // false for shared read-only sessions
let readFailed = false;  // saved layouts could not be read (storage unavailable)
let pending = null;

export function loadLayouts() {
  if (layouts) return Promise.resolve(layouts);
  if (!pending) {
    pending = api.get('/api/v1/layouts').then((res) => {
      editable = res.data.editable !== false;
      readFailed = res.data.layouts === null;
      layouts = res.data.layouts || {};
      return layouts;
    }).finally(() => { pending = null; });
  }
  return pending;
}

export async function saveLayout(page, widgets) {
  const res = await api.post('/api/v1/layouts', { page, widgets });
  layouts = res.data.layouts || {};
  return layouts[page] || [];
}

export function newWidgetId() {
  const bytes = new Uint8Array(4);
  crypto.getRandomValues(bytes);
  return `w_${[...bytes].map((b) => b.toString(16).padStart(2, '0')).join('')}`;
}

/** False for shared read-only sessions, which cannot save layouts (known once layouts are loaded). */
export function canEditLayouts() {
  return editable;
}

/** True when the connector has a widget on the page (layouts must be loaded). */
export function isPinned(page, connectorId) {
  return Boolean(layouts && (layouts[page] || []).some((w) => w.connector === connectorId));
}

/** Adds a summary widget for a connector to a page, or removes its widgets when already pinned. */
export async function togglePin(page, connectorId) {
  const current = (await loadLayouts())[page] || [];
  if (current.some((w) => w.connector === connectorId)) {
    await saveLayout(page, current.filter((w) => w.connector !== connectorId));
    return false;
  }
  if (current.length >= MAX_WIDGETS) throw new Error(t('A page holds at most {n} widgets.', { n: MAX_WIDGETS }));
  await saveLayout(page, [...current, { id: newWidgetId(), connector: connectorId, view: 'summary', size: 'm' }]);
  return true;
}

/* ------------------------------------------------------------------ view helpers */

function viewsFor(conn) {
  const m = (conn && conn.mapping) || {};
  return VIEWS.filter((v) => (v !== 'operations' || m.operation) && (v !== 'statuses' || m.status));
}

function viewLabel(view, conn) {
  const cat = conn && conn.catalog;
  if (view === 'summary') return t('Summary');
  if (view === 'trend') return t('Events per day');
  if (view === 'operations') return t('By {label}', { label: t((cat && cat.operationLabel) || 'Operation') });
  if (view === 'statuses') return t('By {label}', { label: t((cat && cat.statusLabel) || 'Status') });
  return t('Latest entries');
}

function note(text, action) {
  return h('div', { class: 'wb-note' }, h('span', null, text), action || null);
}

function stat(label, value) {
  return h('div', { class: 'wb-stat' }, h('div', { class: 'wb-stat-label' }, label), h('div', { class: 'wb-stat-value' }, value));
}

/** Renders one widget body from the connector data (same payload as the Telemetry Extensions dashboard). */
function renderView(body, sub, w, conn, d) {
  const cat = conn.catalog || null;
  const m = conn.mapping || {};
  const k = d.kpis || {};
  const partial = Boolean(d.sample && d.sample.partial);
  const plus = partial ? '+' : '';
  const names = (d.valueNames && d.valueNames.operation) || {};
  const statusKey = (cat && cat.statusLabel) || 'Status';
  if (partial) sub.append(` \u00b7 ${t('Latest {n} entries only', { n: fmt.int(d.sample.limit) })}`);
  const chart = (height) => {
    const host = h('div', { class: 'wb-chart' });
    mount(body, host);
    barChart(host, { ...fromTimeline(d.trend, { include: ['events'], names: { events: t('Events') }, colors: { events: TONES.violet } }), integer: true, height });
    return host;
  };
  if (w.view === 'summary') {
    const stats = h('div', { class: 'wb-stats' },
      stat(t('Events'), isNum(k.events && k.events.value) ? `${fmt.compact(k.events.value)}${plus}` : DASH),
      m.user ? stat(t('Users'), isNum(k.users) ? `${fmt.int(k.users)}${plus}` : DASH) : null,
      m.tokens ? stat(t((cat && cat.tokensLabel) || 'Tokens'), isNum(k.tokens) ? `${fmt.compact(k.tokens)}${plus}` : DASH) : null,
      stat(t('Last event'), k.lastEvent ? fmt.relative(k.lastEvent) : DASH));
    const host = h('div', { class: 'wb-chart' });
    mount(body, stats, host);
    barChart(host, { ...fromTimeline(d.trend, { include: ['events'], names: { events: t('Events') }, colors: { events: TONES.violet } }), integer: true, height: 120 });
    return;
  }
  if (w.view === 'trend') { chart(210); return; }
  if (w.view === 'operations' || w.view === 'statuses') {
    const key = w.view === 'operations' ? 'operation' : 'status';
    if (!m[key]) { mount(body, note(t('This dashboard has no field for this view.'))); return; }
    const list = w.view === 'operations' ? d.topOperations : d.topStatuses;
    const label = (v) => (w.view === 'statuses' ? statusText(v, statusKey) : (v && names[v]) || shortValue(v));
    mount(body, h('div', { class: 'tx-wrap' }, hbarList(mapList(list ? list.slice(0, 6) : list, (o, i) => ({
      label: label(o[key]), title: o[key] === null || o[key] === undefined ? '' : String(o[key]), value: o.events, color: colorAt(i),
    })))));
    return;
  }
  const rows = Array.isArray(d.recent) ? d.recent.slice(0, 6) : null;
  if (!rows) { mount(body, note(t('Data source unavailable'))); return; }
  if (!rows.length) { mount(body, note(t('No data in this period'))); return; }
  mount(body, h('ul', { class: 'wb-recent' }, rows.map((r) => h('li', null,
    h('div', { class: 'wb-recent-head' },
      h('span', { class: 'wb-recent-time' }, fmt.dateTime(r.time, true)),
      r.operation ? h('span', { class: 'wb-recent-op' }, names[r.operation] || shortValue(r.operation)) : null,
      r.status !== null && r.status !== undefined ? badge(statusText(r.status, statusKey), 'neutral', { size: 'sm' }) : null),
    r.message ? h('div', { class: 'wb-recent-msg' }, r.message) : null))));
}

/* ------------------------------------------------------------------ board */

/**
 * A widget board for a page. prominent: the Overview style (always shows its header and an empty state);
 * otherwise the board only shows an "Add a widget" link until the page has widgets.
 * Returns null when the Telemetry Extensions module is off.
 */
export function widgetBoard({ page, signal, navigate, prominent = false }) {
  if (!isModuleEnabled('telemetry')) return null;
  const root = h('section', { class: ['wb', prominent ? 'wb-prominent' : 'wb-quiet'], 'aria-label': t('My widgets') });
  const live = h('div', { class: 'sr-only', 'aria-live': 'polite' });
  const st = { widgets: [], connectors: null, byId: new Map(), data: new Map(), dragId: null };

  const dataFor = (id) => {
    if (!st.data.has(id)) st.data.set(id, api.get('/api/v1/telemetry/connectors/data', rangeParams({ id }), { signal }).then((res) => res.data));
    return st.data.get(id);
  };

  const persist = async (next, announce) => {
    const prev = st.widgets;
    st.widgets = next;
    draw();
    if (announce) live.textContent = announce;
    try {
      st.widgets = await saveLayout(page, next);
    } catch (err) {
      st.widgets = prev;
      draw();
      toast({ title: t('The widget layout could not be saved'), text: errorText(err), tone: 'danger' });
    }
  };
  const update = (id, patch) => persist(st.widgets.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  const remove = (id) => persist(st.widgets.filter((x) => x.id !== id), t('Widget removed'));
  const move = (id, to, keepFocus) => {
    const from = st.widgets.findIndex((x) => x.id === id);
    if (from < 0 || to < 0 || to >= st.widgets.length || to === from) return;
    const next = st.widgets.slice();
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    persist(next, t('Moved to position {n} of {total}', { n: to + 1, total: next.length }));
    if (keepFocus) requestAnimationFrame(() => { const el = root.querySelector(`[data-id="${id}"] .wb-handle`); if (el) el.focus(); });
  };

  const openMenu = (anchor, w, index) => {
    const conn = st.byId.get(w.connector);
    openPopover(anchor, (close) => h('div', { role: 'menu', 'aria-label': t('Widget options') },
      conn ? h('div', { class: 'menu-label' }, t('Show')) : null,
      conn ? viewsFor(conn).map((v) => menuItem({ label: viewLabel(v, conn), checked: w.view === v, onClick: () => { close(); update(w.id, { view: v }); } })) : null,
      conn ? h('div', { class: 'menu-sep' }) : null,
      h('div', { class: 'menu-label' }, t('Size')),
      SIZES.map(([size, label]) => menuItem({ label: t(label), checked: w.size === size, onClick: () => { close(); update(w.id, { size }); } })),
      h('div', { class: 'menu-sep' }),
      index > 0 ? menuItem({ label: t('Move earlier'), iconName: 'arrow-up', onClick: () => { close(); move(w.id, index - 1); } }) : null,
      index < st.widgets.length - 1 ? menuItem({ label: t('Move later'), iconName: 'arrow-down', onClick: () => { close(); move(w.id, index + 1); } }) : null,
      menuItem({ label: t('Remove widget'), iconName: 'trash', danger: true, onClick: () => { close(); remove(w.id); } })));
  };

  const widgetEl = (w, index) => {
    const known = Array.isArray(st.connectors);
    const conn = st.byId.get(w.connector);
    const name = conn ? conn.name : (known ? t('Deleted dashboard') : t('Dashboard'));
    const el = h('article', { class: ['wb-widget', `wb-${w.size}`], role: 'listitem', 'data-id': w.id });
    const sub = h('div', { class: 'wb-sub' }, conn ? viewLabel(w.view, conn) : '');
    const body = h('div', { class: 'wb-body' });
    let handle = null;
    if (editable) {
      // A span rather than a <button>: Firefox does not start a drag from a button element.
      handle = h('span', { class: 'wb-handle', role: 'button', tabindex: '0', title: t('Drag to move'), 'aria-label': t('Move {name}: drag, or press the arrow keys', { name }) }, icon('menu', { size: 14 }));
      handle.addEventListener('pointerdown', () => { el.draggable = true; });
      handle.addEventListener('pointerup', () => { el.draggable = false; });
      handle.addEventListener('keydown', (e) => {
        const delta = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
        if (delta) { e.preventDefault(); move(w.id, index + delta, true); }
      });
    }
    el.append(
      h('header', { class: 'wb-widget-head' },
        handle,
        h('div', { class: 'wb-titles' }, h('h3', { class: 'wb-title', title: conn ? cardWhat(conn.catalog) || name : name }, name), sub),
        h('div', { class: 'wb-tools' },
          conn ? iconButton('external', { title: t('Open dashboard'), onClick: () => navigate(`#/extensions/${encodeURIComponent(w.connector)}`) }) : null,
          editable ? iconButton('more', { title: t('Widget options'), onClick: (e) => openMenu(e.currentTarget, w, index) }) : null)),
      body);
    if (!known) {
      mount(body, note(t('Telemetry Extensions dashboards could not be loaded.'), button({ label: t('Retry'), size: 'xs', variant: 'ghost', onClick: () => reload() })));
    } else if (!conn) {
      mount(body, note(t('This dashboard was deleted in Telemetry Extensions.'), editable ? button({ label: t('Remove widget'), size: 'xs', variant: 'ghost', onClick: () => remove(w.id) }) : null));
    } else if (!conn.enabled) {
      mount(body, note(t('This dashboard is turned off in Telemetry Extensions.')));
    } else {
      mount(body, h('div', { class: 'wb-loading' }, h('span', { class: 'spinner' })));
      dataFor(conn.id).then((d) => { if (!signal.aborted && body.isConnected) renderView(body, sub, w, conn, d); }).catch((err) => {
        if (isAbort(err) || signal.aborted) return;
        st.data.delete(conn.id);
        mount(body, note(errorText(err), button({ label: t('Retry'), size: 'xs', variant: 'ghost', onClick: () => draw() })));
      });
    }
    return el;
  };

  const attachDragAndDrop = (grid) => {
    let over = null;
    let place = null;
    const clear = () => { if (over) over.classList.remove('drop-before', 'drop-after'); over = null; };
    grid.addEventListener('dragstart', (e) => {
      const el = e.target.closest && e.target.closest('.wb-widget');
      if (!el || !el.draggable) { e.preventDefault(); return; }
      st.dragId = el.dataset.id;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', st.dragId);
      el.classList.add('dragging');
    });
    grid.addEventListener('dragend', (e) => {
      const el = e.target.closest && e.target.closest('.wb-widget');
      if (el) { el.classList.remove('dragging'); el.draggable = false; }
      st.dragId = null;
      clear();
    });
    grid.addEventListener('dragover', (e) => {
      if (!st.dragId) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const el = e.target.closest && e.target.closest('.wb-widget');
      if (!el || el.dataset.id === st.dragId) { clear(); return; }
      const r = el.getBoundingClientRect();
      const wide = r.width > grid.clientWidth * 0.75;
      const before = wide ? e.clientY < r.top + r.height / 2 : e.clientX < r.left + r.width / 2;
      if (over !== el) { clear(); over = el; }
      place = before ? 'before' : 'after';
      el.classList.toggle('drop-before', before);
      el.classList.toggle('drop-after', !before);
    });
    grid.addEventListener('drop', (e) => {
      if (!st.dragId) return;
      e.preventDefault();
      const target = over;
      const id = st.dragId;
      clear();
      st.dragId = null;
      if (!target) return;
      const item = st.widgets.find((x) => x.id === id);
      const next = st.widgets.filter((x) => x.id !== id);
      let idx = next.findIndex((x) => x.id === target.dataset.id);
      if (!item || idx < 0) return;
      if (place === 'after') idx += 1;
      next.splice(idx, 0, item);
      if (next.map((x) => x.id).join() === st.widgets.map((x) => x.id).join()) return;
      persist(next, t('Moved to position {n} of {total}', { n: idx + 1, total: next.length }));
    });
  };

  const openPicker = () => {
    const enabled = (st.connectors || []).filter((c) => c.enabled);
    if (!enabled.length) { navigate('#/extensions'); return; }
    const pick = { id: enabled[0].id, view: 'summary', size: 'm' };
    const listHost = h('div', { class: 'wb-pick-list', role: 'radiogroup', 'aria-label': t('Dashboard') });
    const viewHost = h('div');
    const renderViews = () => {
      const conn = st.byId.get(pick.id);
      if (!viewsFor(conn).includes(pick.view)) pick.view = 'summary';
      mount(viewHost, segmented(viewsFor(conn).map((v) => ({ value: v, label: viewLabel(v, conn) })), pick.view, (v) => { pick.view = v; }, { label: t('Show') }));
    };
    const renderList = () => {
      mount(listHost, enabled.map((c) => {
        const input = h('input', { type: 'radio', name: 'wb-pick', value: c.id, checked: c.id === pick.id });
        input.addEventListener('change', () => { pick.id = c.id; renderViews(); });
        const count = st.widgets.filter((x) => x.connector === c.id).length;
        return h('label', { class: 'wb-pick' }, input,
          h('span', { class: 'wb-pick-text' },
            h('span', { class: 'wb-pick-name' }, c.name, count ? badge(t('On this page'), 'success', { size: 'sm' }) : null),
            c.catalog ? h('span', { class: 'wb-pick-what' }, cardWhat(c.catalog)) : null));
      }));
    };
    const add = button({ label: t('Add widget'), icon: 'plus', variant: 'primary' });
    const m = openModal({
      title: t('Add a widget'),
      subtitle: page === 'overview' ? t('Pin a Telemetry Extensions dashboard to the Overview.') : t('Pin a Telemetry Extensions dashboard to this page.'),
      wide: true,
      body: (el) => {
        renderList();
        renderViews();
        el.append(
          field({ label: t('Dashboard'), control: listHost }),
          field({ label: t('Show'), control: viewHost }),
          field({ label: t('Size'), control: h('div', null, segmented(SIZES.map(([value, label]) => ({ value, label: t(label) })), pick.size, (v) => { pick.size = v; }, { label: t('Size') })) }),
          h('p', { class: 'wb-hint' }, t('Dashboards are created in Telemetry Extensions. Widgets follow the period selected at the top.')));
      },
      footer: (el) => el.append(button({ label: t('Cancel'), onClick: () => m.close() }), add),
    });
    add.addEventListener('click', () => {
      if (st.widgets.length >= MAX_WIDGETS) { toast({ title: t('A page holds at most {n} widgets.', { n: MAX_WIDGETS }), tone: 'warning' }); return; }
      m.close();
      persist([...st.widgets, { id: newWidgetId(), connector: pick.id, view: pick.view, size: pick.size }], t('Widget added'));
    });
  };

  const draw = () => {
    const connectorsKnown = Array.isArray(st.connectors);
    const hasWidgets = st.widgets.length > 0;
    const add = editable && connectorsKnown ? button({ label: t('Add widget'), icon: 'plus', size: 'sm', variant: prominent ? 'secondary' : 'ghost', onClick: openPicker }) : null;
    if (!hasWidgets && !prominent) {
      // Other pages stay clean until a widget is pinned to them.
      mount(root, editable && connectorsKnown && st.connectors.length ? h('div', { class: 'wb-quiet-bar' }, button({ label: t('Add a widget to this page'), icon: 'plus', size: 'sm', variant: 'ghost', onClick: openPicker })) : null, live);
      return;
    }
    const head = h('div', { class: 'wb-head' },
      h('div', { class: 'wb-heading' },
        h('h2', { class: 'section-title' }, t('My widgets')),
        h('p', { class: 'wb-desc' }, readFailed ? t('Saved widget layouts could not be read. Changes may not be kept.')
          : t('Telemetry Extensions dashboards arranged your way. Drag a widget by its handle to move it.'))),
      add);
    if (!hasWidgets) {
      const empty = connectorsKnown && !st.connectors.length
        ? h('div', { class: 'wb-empty' }, h('span', null, t('No telemetry dashboards yet. Add one in Telemetry Extensions with one click, then pin it here.')),
          button({ label: t('Open Telemetry Extensions'), icon: 'plug', size: 'sm', onClick: () => navigate('#/extensions') }))
        : h('div', { class: 'wb-empty' }, h('span', null, editable ? t('Pin dashboards from Telemetry Extensions here with Add widget.') : t('No widgets on this page.')));
      mount(root, head, empty, live);
      return;
    }
    const grid = h('div', { class: 'wb-grid', role: 'list' });
    st.widgets.forEach((w, i) => grid.append(widgetEl(w, i)));
    if (editable) attachDragAndDrop(grid);
    mount(root, head, grid, live);
  };

  const reload = () => Promise.all([
    loadLayouts(),
    api.get('/api/v1/telemetry/connectors', null, { signal }).then((res) => res.data.connectors || []).catch((err) => { if (isAbort(err)) throw err; return null; }),
  ]).then(([all, connectors]) => {
    if (signal.aborted) return;
    st.widgets = all[page] || [];
    st.connectors = connectors;
    st.byId = new Map((connectors || []).map((c) => [c.id, c]));
    draw();
  }).catch((err) => {
    if (isAbort(err) || signal.aborted) return;
    if (prominent) mount(root, h('div', { class: 'wb-empty' }, h('span', null, t('Widgets could not be loaded.')), h('span', { class: 'muted' }, errorText(err))));
  });
  reload();
  return root;
}
