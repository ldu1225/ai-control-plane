/**
 * Dependency-free SVG charts: line / stacked area, vertical bars (stacked or grouped), donut,
 * sparkline and horizontal ranking bars. Colors are applied through CSSOM (CSP-safe).
 */
import { h, s, mount } from './dom.js';
import { fmt, isNum, addDays } from './format.js';
import { t } from './i18n.js';

export const PALETTE = ['#3B82F6', '#14B8A6', '#8B5CF6', '#F59E0B', '#EC4899', '#22C55E', '#06B6D4', '#EF4444', '#6366F1', '#84CC16', '#F97316', '#64748B'];
export const TONES = {
  success: '#22C55E', danger: '#EF4444', warning: '#F59E0B', info: '#3B82F6', neutral: '#94A3B8',
  violet: '#8B5CF6', teal: '#14B8A6', slate: '#64748B', accent: 'var(--accent)',
};

export function colorAt(i) {
  return PALETTE[i % PALETTE.length];
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function niceStep(range, ticks) {
  const raw = range / Math.max(1, ticks);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return nice * mag;
}

function niceDomain(max, ticks = 4, integer = false) {
  if (!isNum(max) || max <= 0) return { max: integer ? 4 : 1, step: integer ? 1 : 0.25 };
  let step = niceStep(max, ticks);
  if (integer) step = Math.max(1, Math.round(step));
  return { max: Math.ceil(max / step) * step, step };
}

function monotonePath(points) {
  const n = points.length;
  if (n === 0) return '';
  if (n === 1) return `M${points[0].x},${points[0].y}`;
  const dx = [];
  const m = [];
  for (let i = 0; i < n - 1; i += 1) {
    dx[i] = points[i + 1].x - points[i].x;
    m[i] = dx[i] === 0 ? 0 : (points[i + 1].y - points[i].y) / dx[i];
  }
  const tg = [m[0]];
  for (let i = 1; i < n - 1; i += 1) tg[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
  tg[n - 1] = m[n - 2];
  for (let i = 0; i < n - 1; i += 1) {
    if (m[i] === 0) { tg[i] = 0; tg[i + 1] = 0; continue; }
    const a = tg[i] / m[i];
    const b = tg[i + 1] / m[i];
    const s2 = a * a + b * b;
    if (s2 > 9) {
      const tau = 3 / Math.sqrt(s2);
      tg[i] = tau * a * m[i];
      tg[i + 1] = tau * b * m[i];
    }
  }
  let d = `M${points[0].x},${points[0].y}`;
  for (let i = 0; i < n - 1; i += 1) {
    const h3 = dx[i] / 3;
    d += `C${points[i].x + h3},${points[i].y + tg[i] * h3},${points[i + 1].x - h3},${points[i + 1].y - tg[i + 1] * h3},${points[i + 1].x},${points[i + 1].y}`;
  }
  return d;
}

function labelFor(bucket, granularity) {
  if (DATE_RE.test(bucket)) return fmt.date(bucket);
  return String(bucket);
}

function tooltipTitle(bucket, granularity) {
  if (DATE_RE.test(bucket)) {
    if (granularity === 'week') return fmt.dateRange(bucket, addDays(bucket, 6));
    return fmt.date(bucket, 'long');
  }
  return String(bucket);
}

function paint(el, prop, color) {
  el.style.setProperty(prop, color);
  return el;
}

/** Base class handling sizing, legend, tooltip and resize observation. */
class BaseChart {
  constructor(container, opts) {
    this.container = container;
    this.opts = opts;
    this.hidden = new Set();
    this.root = h('div', { class: 'chart' });
    this.svgHost = h('div');
    this.tooltip = h('div', { class: 'chart-tooltip', role: 'presentation' });
    this.legend = h('div', { class: 'chart-legend' });
    this.root.append(this.svgHost, this.tooltip);
    if (opts.legend !== false) this.root.append(this.legend);
    mount(container, this.root);
    this.lastWidth = 0;
    this.ro = new ResizeObserver(() => {
      const w = Math.floor(this.svgHost.clientWidth || this.root.clientWidth);
      if (w && Math.abs(w - this.lastWidth) > 1) {
        cancelAnimationFrame(this.raf);
        this.raf = requestAnimationFrame(() => this.render());
      }
    });
    this.ro.observe(this.root);
    this.render();
  }

  update(opts) {
    this.opts = { ...this.opts, ...opts };
    this.render();
  }

  destroy() {
    this.ro.disconnect();
  }

  seriesList() {
    return (this.opts.series || []).map((sr, i) => ({ ...sr, color: sr.color || colorAt(i) }));
  }

  renderLegend(series) {
    if (this.opts.legend === false) return;
    if (series.length <= 1 && !this.opts.forceLegend) { this.legend.replaceChildren(); return; }
    mount(this.legend, series.map((sr) => {
      const btn = h('button', {
        type: 'button',
        class: ['legend-item', this.hidden.has(sr.key) ? 'off' : ''],
        'aria-pressed': this.hidden.has(sr.key) ? 'false' : 'true',
        title: sr.name,
        onclick: () => {
          if (this.hidden.has(sr.key)) this.hidden.delete(sr.key);
          else if (this.hidden.size < series.length - 1) this.hidden.add(sr.key);
          this.render();
        },
      }, paint(h('span', { class: 'swatch' }), 'background', sr.color), h('span', { class: 'name' }, sr.name));
      return btn;
    }));
  }

  showTooltip(x, y, title, rows, total) {
    const tt = this.tooltip;
    mount(tt,
      h('div', { class: 'tt-title' }, title),
      rows.map((r) => h('div', { class: 'tt-row' },
        paint(h('span', { class: 'swatch' }), 'background', r.color),
        h('span', { class: 'tt-name' }, r.name),
        h('span', { class: 'tt-val' }, r.value))),
      total ? h('div', { class: ['tt-row', 'tt-total'] }, h('span', { class: 'tt-name' }, t('Total')), h('span', { class: 'tt-val' }, total)) : null);
    tt.classList.add('show');
    const box = this.root.getBoundingClientRect();
    const ttw = tt.offsetWidth;
    const tth = tt.offsetHeight;
    let left = x + 14;
    if (left + ttw > box.width) left = x - ttw - 14;
    left = Math.max(0, left);
    let top = y - tth / 2;
    top = Math.max(0, Math.min(top, (this.height || 200) - tth));
    tt.style.setProperty('left', `${left}px`);
    tt.style.setProperty('top', `${top}px`);
  }

  hideTooltip() {
    this.tooltip.classList.remove('show');
  }

  emptyState(height) {
    const text = this.opts.emptyText || (this.opts.unavailable ? t('Data source unavailable') : t('No data in this period'));
    const el = h('div', { class: ['chart-empty', this.opts.unavailable ? 'unavailable' : ''] }, text);
    el.style.setProperty('height', `${height}px`);
    mount(this.svgHost, el);
    this.legend.replaceChildren();
  }
}

/** Cartesian chart (line, area, bar). */
class XYChart extends BaseChart {
  render() {
    const o = this.opts;
    const buckets = o.buckets || [];
    const series = this.seriesList();
    const visible = series.filter((sr) => !this.hidden.has(sr.key));
    const height = o.height || 240;
    this.height = height;
    const width = Math.max(240, Math.floor(this.svgHost.clientWidth || this.root.clientWidth || 600));
    this.lastWidth = width;

    const hasData = buckets.length > 0 && visible.some((sr) => (sr.values || []).some((v) => isNum(v) && v !== 0));
    if (!hasData) { this.emptyState(height); return; }

    const stacked = Boolean(o.stacked);
    const isBar = o.type === 'bar';
    let max = 0;
    buckets.forEach((_, i) => {
      if (stacked) {
        const sum = visible.reduce((acc, sr) => acc + (isNum(sr.values[i]) ? sr.values[i] : 0), 0);
        max = Math.max(max, sum);
      } else {
        visible.forEach((sr) => { if (isNum(sr.values[i])) max = Math.max(max, sr.values[i]); });
      }
    });
    const yFmt = o.yFormat || fmt.compact;
    const vFmt = o.valueFormat || yFmt;
    const dom = niceDomain(max, o.ticks || 4, Boolean(o.integer));
    const ticks = [];
    for (let v = 0; v <= dom.max + dom.step / 2; v += dom.step) ticks.push(v);
    const maxLabelLen = Math.max(...ticks.map((v) => yFmt(v).length));
    const m = { top: 10, right: 8, bottom: 26, left: Math.min(80, 12 + maxLabelLen * 6.6) };
    const pw = width - m.left - m.right;
    const ph = height - m.top - m.bottom;
    const n = buckets.length;
    const band = pw / n;
    const xAt = (i) => (isBar ? m.left + band * i + band / 2 : m.left + (n === 1 ? pw / 2 : (pw * i) / (n - 1)));
    const yAt = (v) => m.top + ph - (v / dom.max) * ph;

    const svg = s('svg', { width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': o.ariaLabel || t('Chart') });
    const grid = s('g', { class: 'chart-grid' });
    const axis = s('g', { class: 'chart-axis' });
    ticks.forEach((v) => {
      const y = Math.round(yAt(v)) + 0.5;
      grid.append(s('line', { x1: m.left, x2: width - m.right, y1: y, y2: y, class: v === 0 ? 'zero' : null }));
      axis.append(s('text', { x: m.left - 8, y: y + 3.5, 'text-anchor': 'end' }, yFmt(v)));
    });
    const minLabelW = 58;
    const every = Math.max(1, Math.ceil((n * minLabelW) / pw));
    const labelIdx = [];
    for (let i = 0; i < n; i += every) labelIdx.push(i);
    if (labelIdx[labelIdx.length - 1] !== n - 1) {
      if (n - 1 - labelIdx[labelIdx.length - 1] < every && labelIdx.length > 1) labelIdx[labelIdx.length - 1] = n - 1;
      else labelIdx.push(n - 1);
    }
    labelIdx.forEach((i) => {
      const anchor = isBar ? 'middle' : (i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle');
      axis.append(s('text', { x: xAt(i), y: height - 8, 'text-anchor': anchor }, labelFor(buckets[i], o.granularity)));
    });
    svg.append(grid, axis);

    const plot = s('g');
    const highlight = new Set(o.highlight || []);
    if (isBar) {
      const groupCount = stacked ? 1 : visible.length;
      const barW = Math.max(2, Math.min(42, (band * 0.68) / groupCount));
      buckets.forEach((_, i) => {
        let acc = 0;
        visible.forEach((sr, si) => {
          const v = isNum(sr.values[i]) ? sr.values[i] : 0;
          if (v <= 0) return;
          let x;
          let y0;
          let y1;
          if (stacked) {
            x = xAt(i) - barW / 2;
            y0 = yAt(acc);
            acc += v;
            y1 = yAt(acc);
          } else {
            x = xAt(i) - (barW * groupCount) / 2 + barW * si;
            y0 = yAt(0);
            y1 = yAt(v);
          }
          const hgt = Math.max(1, y0 - y1);
          const rect = s('rect', { x, y: y1, width: barW, height: hgt, rx: Math.min(3, barW / 3), class: 'chart-bar' });
          const color = highlight.has(i) && o.highlightColor ? o.highlightColor : sr.color;
          paint(rect, 'fill', color);
          plot.append(rect);
        });
        if (highlight.has(i)) {
          const topVal = stacked ? acc : Math.max(...visible.map((sr) => (isNum(sr.values[i]) ? sr.values[i] : 0)));
          const cy = yAt(topVal) - 7;
          plot.append(paint(s('circle', { cx: xAt(i), cy, r: 3 }), 'fill', o.highlightColor || TONES.danger));
        }
      });
    } else {
      const base = new Array(n).fill(0);
      visible.forEach((sr) => {
        const pts = [];
        const lower = [];
        buckets.forEach((_, i) => {
          const v = isNum(sr.values[i]) ? sr.values[i] : 0;
          const y0 = stacked ? base[i] : 0;
          const y1 = y0 + v;
          pts.push({ x: xAt(i), y: yAt(y1) });
          lower.push({ x: xAt(i), y: yAt(y0) });
          if (stacked) base[i] = y1;
        });
        const lineD = stacked ? pts.map((p, i) => `${i ? 'L' : 'M'}${p.x},${p.y}`).join('') : monotonePath(pts);
        if (o.area !== false && (stacked || visible.length <= 2 || o.area)) {
          let areaD;
          if (stacked) {
            areaD = `${lineD}${lower.slice().reverse().map((p) => `L${p.x},${p.y}`).join('')}Z`;
          } else {
            areaD = `${lineD}L${pts[pts.length - 1].x},${yAt(0)}L${pts[0].x},${yAt(0)}Z`;
          }
          const area = s('path', { d: areaD });
          paint(area, 'fill', sr.color);
          area.style.setProperty('fill-opacity', stacked ? '0.55' : '0.10');
          plot.append(area);
        }
        const line = s('path', { d: lineD, fill: 'none', 'stroke-width': stacked ? 1.25 : 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' });
        paint(line, 'stroke', sr.color);
        if (sr.dashed) line.setAttribute('stroke-dasharray', '4 4');
        plot.append(line);
        if (n <= 2 || o.dots) {
          pts.forEach((p) => plot.append(paint(s('circle', { cx: p.x, cy: p.y, r: 2.5 }), 'fill', sr.color)));
        }
      });
    }
    svg.append(plot);

    const guide = s('line', { class: 'chart-guide', y1: m.top, y2: m.top + ph, x1: -10, x2: -10 });
    const focusDots = s('g');
    svg.append(guide, focusDots);
    const hit = s('rect', { x: m.left, y: m.top, width: pw, height: ph, class: 'chart-hit' });
    const onMove = (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = ev.clientX - rect.left;
      let idx = isBar ? Math.floor((px - m.left) / band) : Math.round(((px - m.left) / pw) * (n - 1));
      idx = Math.max(0, Math.min(n - 1, idx));
      const gx = xAt(idx);
      guide.setAttribute('x1', gx);
      guide.setAttribute('x2', gx);
      focusDots.replaceChildren();
      let total = 0;
      const rows = visible.map((sr) => {
        const v = isNum(sr.values[idx]) ? sr.values[idx] : 0;
        total += v;
        return { name: sr.name, color: sr.color, value: vFmt(v), raw: v };
      });
      if (!isBar && !stacked) {
        visible.forEach((sr) => {
          const v = isNum(sr.values[idx]) ? sr.values[idx] : 0;
          const c = s('circle', { cx: gx, cy: yAt(v), r: 3.5, stroke: '#fff', 'stroke-width': 1.5 });
          paint(c, 'fill', sr.color);
          focusDots.append(c);
        });
      }
      const sorted = o.sortTooltip === false ? rows : rows.slice().sort((a, b) => b.raw - a.raw);
      const shown = sorted.slice(0, 10);
      const extra = o.tooltipExtra ? o.tooltipExtra(idx) : null;
      if (extra) shown.push(...extra);
      this.showTooltip(gx, ev.clientY - rect.top, tooltipTitle(buckets[idx], o.granularity), shown, stacked && visible.length > 1 ? vFmt(total) : null);
    };
    hit.addEventListener('mousemove', onMove);
    hit.addEventListener('mouseleave', () => {
      guide.setAttribute('x1', -10);
      guide.setAttribute('x2', -10);
      focusDots.replaceChildren();
      this.hideTooltip();
    });
    if (o.onClick) {
      hit.style.setProperty('cursor', 'pointer');
      hit.addEventListener('click', (ev) => {
        const rect = svg.getBoundingClientRect();
        const px = ev.clientX - rect.left;
        let idx = isBar ? Math.floor((px - m.left) / band) : Math.round(((px - m.left) / pw) * (n - 1));
        idx = Math.max(0, Math.min(n - 1, idx));
        o.onClick(idx, buckets[idx]);
      });
    }
    svg.append(hit);
    mount(this.svgHost, svg);
    this.renderLegend(series);
  }
}

export function lineChart(container, opts) {
  return new XYChart(container, { type: 'line', ...opts });
}

export function barChart(container, opts) {
  return new XYChart(container, { type: 'bar', ...opts });
}

/** Convenience: build chart series from an API timeline object. */
export function fromTimeline(timeline, { names = {}, colors = {}, include = null, limit = null } = {}) {
  if (!timeline) return { buckets: [], series: [], unavailable: true };
  let series = (timeline.series || []).filter((sr) => !include || include.includes(sr.key));
  if (limit && series.length > limit) series = series.slice(0, limit);
  return {
    buckets: timeline.buckets || [],
    granularity: timeline.granularity,
    series: series.map((sr, i) => ({
      key: sr.key,
      name: names[sr.key] || sr.name || sr.key,
      values: sr.values || [],
      color: colors[sr.key] || colorAt(i),
    })),
  };
}

/** True when a chart input list is missing or carries only unknown (null) values. */
function allUnknown(list) {
  return list === null || list === undefined
    || (Array.isArray(list) && list.length > 0 && list.every((it) => it.value === null || it.value === undefined));
}

/** Map a possibly-null API list, preserving null (source unavailable) instead of turning it into []. */
export function mapList(list, fn) {
  return Array.isArray(list) ? list.map(fn) : null;
}

/** Donut chart with legend. */
export function donutChart(container, opts) {
  const items = (opts.items || []).filter((it) => isNum(it.value) && it.value > 0).map((it, i) => ({ ...it, color: it.color || colorAt(i) }));
  const total = items.reduce((a, b) => a + b.value, 0);
  const size = opts.size || 160;
  const vFmt = opts.format || fmt.int;
  if (!total) {
    const unknown = allUnknown(opts.items);
    const el = h('div', { class: ['chart-empty', unknown ? 'unavailable' : ''] }, opts.emptyText || (unknown ? t('Data source unavailable') : t('No data in this period')));
    el.style.setProperty('height', `${size}px`);
    mount(container, el);
    return;
  }
  const r = size / 2;
  const thickness = opts.thickness || 18;
  const ri = r - thickness;
  const svg = s('svg', { width: size, height: size, viewBox: `0 0 ${size} ${size}`, role: 'img', 'aria-label': opts.ariaLabel || t('Chart') });
  let angle = -Math.PI / 2;
  const gap = items.length > 1 ? 0.012 : 0;
  items.forEach((it) => {
    const frac = it.value / total;
    const a0 = angle + gap;
    const a1 = angle + frac * Math.PI * 2 - gap;
    angle += frac * Math.PI * 2;
    let el;
    if (frac >= 0.9999) {
      el = s('circle', { cx: r, cy: r, r: (r + ri) / 2, fill: 'none', 'stroke-width': thickness });
      paint(el, 'stroke', it.color);
    } else {
      const large = a1 - a0 > Math.PI ? 1 : 0;
      const p = (rad, ang) => `${r + rad * Math.cos(ang)},${r + rad * Math.sin(ang)}`;
      const d = `M${p(r, a0)}A${r},${r} 0 ${large} 1 ${p(r, a1)}L${p(ri, a1)}A${ri},${ri} 0 ${large} 0 ${p(ri, a0)}Z`;
      el = s('path', { d });
      paint(el, 'fill', it.color);
    }
    el.append(s('title', null, `${it.label}: ${vFmt(it.value)}`));
    svg.append(el);
  });
  if (opts.centerValue !== undefined) {
    svg.append(s('text', { x: r, y: r + 2, 'text-anchor': 'middle', class: 'donut-center-value' }, String(opts.centerValue)));
    if (opts.centerLabel) svg.append(s('text', { x: r, y: r + 19, 'text-anchor': 'middle', class: 'donut-center-label' }, opts.centerLabel));
  }
  const legend = h('div', { class: 'donut-legend' }, items.map((it) => h('div', { class: 'dl-row' },
    paint(h('span', { class: 'swatch round' }), 'background', it.color),
    h('span', { class: 'dl-name', title: it.label }, it.label),
    h('span', { class: 'dl-val' }, vFmt(it.value)),
    h('span', { class: 'dl-pct' }, fmt.pct(it.value / total)))));
  mount(container, h('div', { class: 'donut-host' }, h('div', { class: ['donut-wrap', opts.stacked ? 'stacked' : ''] }, svg, opts.legend === false ? null : legend)));
}

/** Tiny trend line for KPI cards. Returns an SVG element. */
export function sparkline(values, color = TONES.info) {
  const w = 72;
  const hgt = 24;
  const vals = (values || []).map((v) => (isNum(v) ? v : 0));
  const svg = s('svg', { viewBox: `0 0 ${w} ${hgt}`, preserveAspectRatio: 'none', 'aria-hidden': 'true', class: 'kpi-spark' });
  if (vals.length < 2 || vals.every((v) => v === 0)) return svg;
  const max = Math.max(...vals);
  const min = Math.min(...vals);
  const span = max - min || 1;
  const pts = vals.map((v, i) => ({ x: (i / (vals.length - 1)) * (w - 2) + 1, y: hgt - 3 - ((v - min) / span) * (hgt - 6) }));
  const d = monotonePath(pts);
  const area = s('path', { d: `${d}L${pts[pts.length - 1].x},${hgt}L${pts[0].x},${hgt}Z` });
  paint(area, 'fill', color);
  area.style.setProperty('fill-opacity', '0.12');
  const line = s('path', { d, fill: 'none', 'stroke-width': 1.6, 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke' });
  paint(line, 'stroke', color);
  svg.append(area, line);
  return svg;
}

/** Horizontal ranking bars (HTML). */
export function hbarList(items, opts = {}) {
  const vFmt = opts.format || fmt.int;
  const list = (items || []).filter((it) => isNum(it.value));
  if (!list.length) {
    const unknown = allUnknown(items);
    return h('div', { class: ['empty', 'compact', unknown ? 'warning' : ''] }, h('div', { class: 'empty-body' }, opts.emptyText || (unknown ? t('Data source unavailable') : t('No data in this period'))));
  }
  const max = opts.max || Math.max(...list.map((it) => it.value), 0) || 1;
  return h('div', { class: 'hbar-list' }, list.map((it, i) => {
    const fill = h('div', { class: 'hbar-fill' });
    fill.style.setProperty('width', `${Math.max(1.5, (it.value / max) * 100)}%`);
    if (it.color || opts.color) paint(fill, 'background', it.color || opts.color);
    const row = h('div', { class: ['hbar-row', opts.onClick ? 'clickable' : ''], title: it.title || it.label },
      h('div', { class: 'hbar-label' }, it.label),
      h('div', { class: 'hbar-value' }, vFmt(it.value), it.sub ? h('span', { class: 'sub' }, it.sub) : null),
      h('div', { class: 'hbar-track' }, fill));
    if (opts.onClick) {
      row.tabIndex = 0;
      row.addEventListener('click', () => opts.onClick(it, i));
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter') opts.onClick(it, i); });
    }
    return row;
  }));
}

/** Stacked horizontal proportion bar (e.g. IAM state distribution). */
export function proportionBar(segments) {
  const total = segments.reduce((a, b) => a + (isNum(b.value) ? b.value : 0), 0) || 1;
  return h('div', { class: 'state-bar', role: 'img', 'aria-label': segments.map((sg) => `${sg.label} ${sg.value}`).join(', ') },
    segments.filter((sg) => sg.value > 0).map((sg) => {
      const span = h('span', { title: `${sg.label}: ${sg.value}` });
      span.style.setProperty('width', `${(sg.value / total) * 100}%`);
      paint(span, 'background', sg.color);
      return span;
    }));
}
