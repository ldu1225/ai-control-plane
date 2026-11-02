/**
 * Safe DOM construction helpers.
 * All text is inserted with text nodes; no innerHTML / insertAdjacentHTML is used anywhere in the app.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';
const URL_ATTRS = new Set(['href', 'src', 'action', 'formaction', 'xlink:href', 'poster', 'cite']);

/** Allow relative paths, fragments, http(s), blob: and mailto: only. */
function safeUrlAttr(value) {
  const str = String(value).trim();
  if (str.startsWith('//')) return null;
  if (/^(?:https?:|blob:|mailto:)/i.test(str)) return str;
  if (/^[a-z][a-z0-9+.-]*:/i.test(str)) return null;
  return str;
}

function setAttrs(el, attrs, isSvg) {
  if (!attrs) return;
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') {
      const cls = Array.isArray(value) ? value.filter(Boolean).join(' ') : String(value);
      if (isSvg) el.setAttribute('class', cls); else el.className = cls;
    } else if (key === 'dataset') {
      for (const [dk, dv] of Object.entries(value)) {
        if (dv !== null && dv !== undefined) el.dataset[dk] = String(dv);
      }
    } else if (key === 'style') {
      // CSSOM only (allowed by CSP); never the style attribute string.
      if (typeof value === 'string') {
        for (const part of value.split(';')) {
          const idx = part.indexOf(':');
          if (idx > 0) {
            const prop = part.slice(0, idx).trim();
            const val = part.slice(idx + 1).trim();
            if (prop && val) el.style.setProperty(prop, val);
          }
        }
      } else if (typeof value === 'object') {
        for (const [prop, v] of Object.entries(value)) {
          if (v !== null && v !== undefined) {
            const kebab = prop.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`);
            el.style.setProperty(kebab, String(v));
          }
        }
      }
    } else if (key === 'text') {
      el.textContent = String(value);
    } else if (/^on/i.test(key)) {
      // Handlers are attached as listeners only; inline handler strings are never written.
      if (typeof value === 'function') el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'value' && !isSvg) {
      el.value = String(value);
    } else if (key === 'checked' && !isSvg) {
      el.checked = Boolean(value);
    } else if (value === true) {
      el.setAttribute(key, '');
    } else if (URL_ATTRS.has(key.toLowerCase())) {
      const safe = safeUrlAttr(value);
      if (safe !== null) el.setAttribute(key, safe);
    } else {
      el.setAttribute(key, String(value));
    }
  }
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** Create an HTML element. */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs, false);
  append(el, children);
  return el;
}

/** Create an SVG element. */
export function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  setAttrs(el, attrs, true);
  append(el, children);
  return el;
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

export function mount(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export function frag(...children) {
  const f = document.createDocumentFragment();
  append(f, children);
  return f;
}

export function debounce(fn, wait = 200) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

export function uid(prefix = 'id') {
  return `${prefix}-${Math.random().toString(36).slice(2, 9)}`;
}

/** Validate a URL that is allowed to be used as an image source. */
export function safeImageUrl(url) {
  if (typeof url !== 'string' || !url) return null;
  if (/^https:\/\/[^\s"'<>]+$/i.test(url)) return url;
  if (/^\/assets\/[A-Za-z0-9_\-./]+$/.test(url) && !url.includes('..')) return url;
  return null;
}

export function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    const ta = h('textarea', { class: 'sr-only', readonly: true });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      resolve();
    } catch (e) {
      reject(e);
    } finally {
      ta.remove();
    }
  });
}

/** Trigger a client-side download of generated text content. */
export function downloadText(filename, text, mime = 'text/csv;charset=utf-8') {
  const blob = new Blob(['\ufeff', text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function toCsv(columns, rows) {
  const esc = (v) => {
    if (v === null || v === undefined) return '';
    let str = typeof v === 'object' ? JSON.stringify(v) : String(v);
    // Neutralise spreadsheet formula injection.
    if (/^[=+\-@\t\r]/.test(str)) str = `'${str}`;
    return /[",\n\r]/.test(str) ? `"${str.replace(/"/g, '""')}"` : str;
  };
  const lines = [columns.map((c) => esc(c.label)).join(',')];
  for (const row of rows) {
    lines.push(columns.map((c) => esc(c.csv ? c.csv(row) : row[c.key])).join(','));
  }
  return lines.join('\r\n');
}
