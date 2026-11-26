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

/** Default product glyph (Central Core): Google 'G' ring converging on a single central command core. */
export function brandGlyph(size = 28) {
  return s('svg', { viewBox: '0 0 48 48', width: size, height: size, 'aria-hidden': 'true', focusable: 'false', class: 'brand-glyph' },
    s('path', { fill: '#EA4335', d: 'M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z' }),
    s('path', { fill: '#4285F4', d: 'M46.98 24.55c0-1.57-.15-3.09-.38-4.55H31.78a8.75 8.75 0 0 1 -0.62 9.02h5.77c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z' }),
    s('path', { fill: '#FBBC05', d: 'M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z' }),
    s('path', { fill: '#34A853', d: 'M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z' }),
    s('circle', { cx: 24, cy: 24, r: 6.55, class: 'brand-core-outer' }),
    s('circle', { cx: 24, cy: 24, r: 2.3, class: 'brand-core-inner' }));
}

/** Brand mark: the configured logo (https:// or /assets/ only) or the default glyph. */
export function brandMark(branding, { size = 30, cls = '' } = {}) {
  const url = safeImageUrl(branding && branding.logoUrl);
  if (url) return h('span', { class: ['brand-mark', 'has-img', cls] }, h('img', { src: url, alt: '' }));
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
