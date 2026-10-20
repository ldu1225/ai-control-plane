/**
 * Brand and identity helpers shared by the application shell and the sign-in page.
 * Everything is built with the safe DOM helpers (no markup strings).
 */
import { h, s, safeImageUrl } from './dom.js';
import { initials } from './format.js';

const HEX_RE = /^#[0-9A-Fa-f]{6}$/;
const PHOTO_RE = /^https:\/\/[^\s"'<>]+$/i;

/** Applies the white-label accent color (validated #RRGGBB) and a readable contrast color. */
export function applyAccent(hex) {
  if (typeof hex !== 'string' || !HEX_RE.test(hex)) return;
  const root = document.documentElement;
  root.style.setProperty('--accent', hex);
  const r = parseInt(hex.slice(1, 3), 16) / 255;
  const g = parseInt(hex.slice(3, 5), 16) / 255;
  const b = parseInt(hex.slice(5, 7), 16) / 255;
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  root.style.setProperty('--accent-contrast', lum > 0.62 ? '#101828' : '#FFFFFF');
}

/** Default product glyph (stacked layers) drawn in the current text color. */
export function brandGlyph(size = 22) {
  const line = { fill: 'none', stroke: 'currentColor', 'stroke-width': 2.2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' };
  return s('svg', { viewBox: '0 0 32 32', width: size, height: size, 'aria-hidden': 'true', focusable: 'false' },
    s('path', { d: 'M16 7 25 11.5 16 16 7 11.5Z', fill: 'currentColor' }),
    s('path', { d: 'M7 16 16 20.5 25 16', ...line, opacity: '.85' }),
    s('path', { d: 'M7 20.5 16 25 25 20.5', ...line, opacity: '.6' }));
}

/** Brand mark: the configured logo (https:// or /assets/ only) or the default glyph. */
export function brandMark(branding, { size = 22, cls = '' } = {}) {
  const url = safeImageUrl(branding && branding.logoUrl);
  if (url) return h('span', { class: ['brand-mark', cls] }, h('img', { src: url, alt: '' }));
  return h('span', { class: ['brand-mark', cls], 'aria-hidden': 'true' }, brandGlyph(size));
}

/** Account avatar: the Google profile photo when available (https only), otherwise initials. */
export function accountAvatar({ picture, email, name } = {}, cls = '') {
  const fallback = initials(email || name || 'User');
  const el = h('span', { class: ['avatar', cls], 'aria-hidden': 'true' });
  if (typeof picture === 'string' && PHOTO_RE.test(picture)) {
    const img = h('img', { src: picture, alt: '', referrerpolicy: 'no-referrer', decoding: 'async' });
    img.addEventListener('error', () => {
      el.classList.remove('has-photo');
      el.replaceChildren(fallback);
    }, { once: true });
    el.classList.add('has-photo');
    el.append(img);
  } else {
    el.textContent = fallback;
  }
  return el;
}
