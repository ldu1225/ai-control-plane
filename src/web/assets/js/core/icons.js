/**
 * Icon set (24x24, stroke based). Shapes are described in a compact notation and built with
 * createElementNS, so no markup strings are ever parsed into the DOM.
 *   p:<path d>   c:cx,cy,r   l:x1,y1,x2,y2   r:x,y,w,h[,rx]   pl:<points>   pg:<points>   e:cx,cy,rx,ry
 */
import { s } from './dom.js';

const ICONS = {
  grid: 'r:3,3,7,7,1.5|r:14,3,7,7,1.5|r:14,14,7,7,1.5|r:3,14,7,7,1.5',
  briefcase: 'r:2,7,20,14,2|p:M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16',
  book: 'p:M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z|p:M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z',
  share: 'c:18,5,3|c:6,12,3|c:18,19,3|l:8.59,13.51,15.42,17.49|l:15.41,6.51,8.59,10.49',
  box: 'p:M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z|pl:3.27 6.96 12 12.01 20.73 6.96|l:12,22.08,12,12',
  server: 'r:2,2,20,8,2|r:2,14,20,8,2|l:6,6,6.01,6|l:6,18,6.01,18',
  shield: 'p:M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
  'shield-check': 'p:M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z|pl:9 12 11 14 15 10',
  layers: 'pg:12 2 2 7 12 12 22 7 12 2|pl:2 17 12 22 22 17|pl:2 12 12 17 22 12',
  card: 'r:1,4,22,16,2|l:1,10,23,10',
  dollar: 'l:12,1,12,23|p:M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6',
  users: 'p:M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2|c:9,7,4|p:M23 21v-2a4 4 0 0 0-3-3.87|p:M16 3.13a4 4 0 0 1 0 7.75',
  user: 'p:M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2|c:12,7,4',
  'user-check': 'p:M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2|c:8.5,7,4|pl:17 11 19 13 23 9',
  'user-x': 'p:M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2|c:8.5,7,4|l:18,8,23,13|l:23,8,18,13',
  sliders: 'l:4,21,4,14|l:4,10,4,3|l:12,21,12,12|l:12,8,12,3|l:20,21,20,16|l:20,12,20,3|l:1,14,7,14|l:9,8,15,8|l:17,16,23,16',
  plug: 'p:M9 2v6|p:M15 2v6|p:M6 8h12v3a6 6 0 0 1-12 0z|p:M12 17v5',
  message: 'p:M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z',
  spark: 'p:M12 2.5l2.1 6.4 6.4 2.1-6.4 2.1L12 19.5l-2.1-6.4L3.5 11l6.4-2.1z|p:M19 16.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z',
  settings: 'c:12,12,3|p:M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z',
  calendar: 'r:3,4,18,18,2|l:16,2,16,6|l:8,2,8,6|l:3,10,21,10',
  'chevron-down': 'pl:6 9 12 15 18 9',
  'chevron-up': 'pl:18 15 12 9 6 15',
  'chevron-right': 'pl:9 18 15 12 9 6',
  'chevron-left': 'pl:15 18 9 12 15 6',
  'chevrons-left': 'pl:11 17 6 12 11 7|pl:18 17 13 12 18 7',
  'chevrons-right': 'pl:13 17 18 12 13 7|pl:6 17 11 12 6 7',
  'chevrons-up-down': 'pl:7 15 12 20 17 15|pl:17 9 12 4 7 9',
  x: 'l:18,6,6,18|l:6,6,18,18',
  check: 'pl:20 6 9 17 4 12',
  plus: 'l:12,5,12,19|l:5,12,19,12',
  minus: 'l:5,12,19,12',
  search: 'c:11,11,8|l:21,21,16.65,16.65',
  refresh: 'pl:23 4 23 10 17 10|pl:1 20 1 14 7 14|p:M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15',
  download: 'p:M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4|pl:7 10 12 15 17 10|l:12,15,12,3',
  upload: 'p:M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4|pl:17 8 12 3 7 8|l:12,3,12,15',
  external: 'p:M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6|pl:15 3 21 3 21 9|l:10,14,21,3',
  info: 'c:12,12,10|l:12,16,12,12|l:12,8,12.01,8',
  help: 'c:12,12,10|p:M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3|l:12,17,12.01,17',
  'alert-triangle': 'p:M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z|l:12,9,12,13|l:12,17,12.01,17',
  'alert-circle': 'c:12,12,10|l:12,8,12,12|l:12,16,12.01,16',
  'check-circle': 'p:M22 11.08V12a10 10 0 1 1-5.93-9.14|pl:22 4 12 14.01 9 11.01',
  'x-circle': 'c:12,12,10|l:15,9,9,15|l:9,9,15,15',
  slash: 'c:12,12,10|l:4.93,4.93,19.07,19.07',
  lock: 'r:3,11,18,11,2|p:M7 11V7a5 5 0 0 1 10 0v4',
  unlock: 'r:3,11,18,11,2|p:M7 11V7a5 5 0 0 1 9.9-1',
  'file-text': 'p:M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z|pl:14 2 14 8 20 8|l:16,13,8,13|l:16,17,8,17|pl:10 9 9 9 8 9',
  file: 'p:M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z|pl:13 2 13 9 20 9',
  folder: 'p:M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z',
  zap: 'pg:13 2 3 14 12 14 11 22 21 10 12 10 13 2',
  'trending-up': 'pl:23 6 13.5 15.5 8.5 10.5 1 18|pl:17 6 23 6 23 12',
  'trending-down': 'pl:23 18 13.5 8.5 8.5 13.5 1 6|pl:17 18 23 18 23 12',
  'arrow-up': 'l:12,19,12,5|pl:5 12 12 5 19 12',
  'arrow-down': 'l:12,5,12,19|pl:19 12 12 19 5 12',
  'arrow-right': 'l:5,12,19,12|pl:12 5 19 12 12 19',
  'arrow-up-right': 'l:7,17,17,7|pl:7 7 17 7 17 17',
  sun: 'c:12,12,5|l:12,1,12,3|l:12,21,12,23|l:4.22,4.22,5.64,5.64|l:18.36,18.36,19.78,19.78|l:1,12,3,12|l:21,12,23,12|l:4.22,19.78,5.64,18.36|l:18.36,5.64,19.78,4.22',
  moon: 'p:M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z',
  monitor: 'r:2,3,20,14,2|l:8,21,16,21|l:12,17,12,21',
  menu: 'l:3,12,21,12|l:3,6,21,6|l:3,18,21,18',
  sidebar: 'r:3,3,18,18,2|l:9,3,9,21',
  copy: 'r:9,9,13,13,2|p:M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1',
  trash: 'pl:3 6 5 6 21 6|p:M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2|l:10,11,10,17|l:14,11,14,17',
  edit: 'p:M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z',
  eye: 'p:M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z|c:12,12,3',
  'eye-off': 'p:M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24|l:1,1,23,23',
  filter: 'pg:22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3',
  more: 'c:12,12,1|c:19,12,1|c:5,12,1',
  logout: 'p:M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4|pl:16 17 21 12 16 7|l:21,12,9,12',
  send: 'l:22,2,11,13|pg:22 2 15 22 11 13 2 9 22 2',
  terminal: 'pl:4 17 10 11 4 5|l:12,19,20,19',
  code: 'pl:16 18 22 12 16 6|pl:8 6 2 12 8 18',
  globe: 'c:12,12,10|l:2,12,22,12|p:M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z',
  key: 'p:M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4',
  power: 'p:M18.36 6.64a9 9 0 1 1-12.73 0|l:12,2,12,12',
  pie: 'p:M21.21 15.89A10 10 0 1 1 8 2.83|p:M22 12A10 10 0 0 0 12 2v10z',
  'bar-chart': 'l:18,20,18,10|l:12,20,12,4|l:6,20,6,14',
  activity: 'pl:22 12 18 12 15 21 9 3 6 12 2 12',
  clock: 'c:12,12,10|pl:12 6 12 12 16 14',
  link: 'p:M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71|p:M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71',
  database: 'e:12,5,9,3|p:M21 12c0 1.66-4 3-9 3s-9-1.34-9-3|p:M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5',
  cpu: 'r:4,4,16,16,2|r:9,9,6,6|l:9,1,9,4|l:15,1,15,4|l:9,20,9,23|l:15,20,15,23|l:20,9,23,9|l:20,14,23,14|l:1,9,4,9|l:1,14,4,14',
  hash: 'l:4,9,20,9|l:4,15,20,15|l:10,3,8,21|l:16,3,14,21',
  command: 'p:M18 3a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0-3-3H6a3 3 0 0 0-3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3V6a3 3 0 0 0-3-3 3 3 0 0 0-3 3 3 3 0 0 0 3 3h12a3 3 0 0 0 3-3 3 3 0 0 0-3-3z',
  target: 'c:12,12,10|c:12,12,6|c:12,12,2',
  branch: 'l:6,3,6,15|c:18,6,3|c:6,18,3|p:M18 9a9 9 0 0 1-9 9',
  list: 'l:8,6,21,6|l:8,12,21,12|l:8,18,21,18|l:3,6,3.01,6|l:3,12,3.01,12|l:3,18,3.01,18',
  play: 'pg:5 3 19 12 5 21 5 3',
  gauge: 'p:M12 14l4-4|p:M3.34 19a10 10 0 1 1 17.32 0',
};

function buildShape(spec) {
  const idx = spec.indexOf(':');
  const kind = spec.slice(0, idx);
  const body = spec.slice(idx + 1);
  const nums = () => body.split(',').map(Number);
  switch (kind) {
    case 'p': return s('path', { d: body });
    case 'c': { const [cx, cy, r] = nums(); return s('circle', { cx, cy, r }); }
    case 'l': { const [x1, y1, x2, y2] = nums(); return s('line', { x1, y1, x2, y2 }); }
    case 'r': { const [x, y, width, height, rx] = nums(); return s('rect', { x, y, width, height, rx: rx || null }); }
    case 'pl': return s('polyline', { points: body });
    case 'pg': return s('polygon', { points: body });
    case 'e': { const [cx, cy, rx, ry] = nums(); return s('ellipse', { cx, cy, rx, ry }); }
    default: return null;
  }
}

/**
 * Build an inline SVG icon element.
 * @param {string} name icon key
 * @param {{size?: number, cls?: string, strokeWidth?: number, label?: string}} opts
 */
export function icon(name, opts = {}) {
  const { size = 16, cls = '', strokeWidth = 2, label = null } = opts;
  const def = ICONS[name] || ICONS.info;
  const svg = s('svg', {
    viewBox: '0 0 24 24',
    width: size,
    height: size,
    fill: 'none',
    stroke: 'currentColor',
    'stroke-width': strokeWidth,
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    class: cls || null,
    'aria-hidden': label ? null : 'true',
    role: label ? 'img' : null,
    'aria-label': label,
    focusable: 'false',
  });
  for (const part of def.split('|')) {
    const shape = buildShape(part);
    if (shape) svg.appendChild(shape);
  }
  return svg;
}

export function hasIcon(name) {
  return Object.prototype.hasOwnProperty.call(ICONS, name);
}
