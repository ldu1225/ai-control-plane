/**
 * Sign-in page.
 *
 * auth_mode "google": the application's own sign-in page. "Sign in with Google" starts the
 * server-side OpenID Connect flow (/auth/google/start); only approved accounts receive a session.
 * Unapproved accounts see an access-request notice. Signed-in users can continue, switch account
 * or sign out (CSRF-protected POST followed by a full page navigation).
 * Other modes (iap, local) explain how access works and link to the dashboard.
 *
 * Security: text is always inserted as text nodes (core/dom.js), no tokens are stored client-side,
 * the post-sign-in destination is a same-origin path that the server validates again.
 */
import { h, s, mount } from './core/dom.js';
import { t, setLocale, getLocale } from './core/i18n.js';
import { icon } from './core/icons.js';
import { api, setCsrfToken } from './core/api.js';
import { applyAccent, brandMark, brandGlyph, accountAvatar } from './core/brand.js';

const PREFS_KEY = 'cp.prefs';
const card = document.getElementById('login-card');
const footer = document.getElementById('login-footer');
const aside = document.getElementById('login-aside');
const query = new URLSearchParams(window.location.search);
const isLogoutPath = window.location.pathname === '/logout';
const notice = {
  error: query.get('error'),
  signedOut: query.get('signed_out') === '1',
  reason: query.get('reason'),
};
let state = null;
let loadFailed = false;

const FEATURES = [
  ['layers', 'One view of AI usage', 'Gemini Enterprise, NotebookLM, agents, models and cost in a single workspace.'],
  ['shield-check', 'Governance with guardrails', 'Policy controls with safety checks and an auditable change history.'],
  ['lock', 'Runs in your cloud', 'Deployed in your own Google Cloud project, under your organization\'s control.'],
];

/* ------------------------------------------------------------------ preferences */

function readPrefs() {
  try {
    const value = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch (e) {
    return {};
  }
}

function saveLocale(locale) {
  try {
    const prefs = readPrefs();
    prefs.locale = locale;
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch (e) { /* storage unavailable: the choice applies to this page only */ }
}

function initialLocale() {
  const prefs = readPrefs();
  if (prefs.locale === 'en' || prefs.locale === 'ko') return prefs.locale;
  const fromBrand = state && state.branding && state.branding.defaultLocale;
  if (fromBrand === 'en' || fromBrand === 'ko') return fromBrand;
  return String(navigator.language || '').toLowerCase().startsWith('ko') ? 'ko' : 'en';
}

function applyDefaultTheme() {
  let stored = null;
  try { stored = localStorage.getItem('cp.theme'); } catch (e) { stored = null; }
  const theme = state && state.branding && state.branding.defaultTheme;
  if (stored !== 'light' && stored !== 'dark' && (theme === 'light' || theme === 'dark')) {
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = theme;
  }
}

/* ------------------------------------------------------------------ helpers */

function productName() {
  return (state && state.product && state.product.name) || 'AI Control Plane';
}

function orgName() {
  return (state && state.branding && state.branding.organizationName) || '';
}

/** Same-origin path to open after sign-in (the server validates it again). */
function nextTarget() {
  const raw = query.get('next') || '/';
  const target = raw.includes('#') || !window.location.hash ? raw : raw + window.location.hash;
  if (!target.startsWith('/') || target.startsWith('//') || target.includes('\\') || target.length > 512) return '/';
  if (/^\/(?:login|logout|auth\/|api\/)/.test(target)) return '/';
  return target;
}

function startUrl() {
  const next = nextTarget();
  return next === '/' ? '/auth/google/start' : `/auth/google/start?next=${encodeURIComponent(next)}`;
}

function isSameOriginPath(value) {
  return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//') && !value.includes('\\');
}

/** Removes one-time notice parameters so a refresh does not repeat them. */
function cleanUrl() {
  const next = query.get('next');
  const keep = next ? `?next=${encodeURIComponent(next)}` : '';
  try {
    window.history.replaceState(null, '', `${window.location.pathname}${keep}${window.location.hash}`);
  } catch (e) { /* not critical */ }
}

function googleLogo() {
  return s('svg', { viewBox: '0 0 48 48', width: 18, height: 18, 'aria-hidden': 'true', focusable: 'false' },
    s('path', { fill: '#EA4335', d: 'M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z' }),
    s('path', { fill: '#4285F4', d: 'M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z' }),
    s('path', { fill: '#FBBC05', d: 'M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z' }),
    s('path', { fill: '#34A853', d: 'M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z' }));
}

function googleButton(label) {
  return h('a', { class: 'gsi-button', href: startUrl() },
    h('span', { class: 'gsi-logo' }, googleLogo()),
    h('span', { class: 'gsi-label' }, label || t('Sign in with Google')));
}

/** "Use another account": the bridge's IAP sign-out when configured, otherwise Google's account chooser. */
function switchAccountButton(label) {
  const url = state && state.switchAccountUrl;
  if (typeof url === 'string' && /^https:\/\/[A-Za-z0-9.-]+\/_gcp_iap\/clear_login_cookie$/.test(url)) {
    return h('a', { class: 'gsi-button', href: url },
      h('span', { class: 'gsi-logo' }, googleLogo()),
      h('span', { class: 'gsi-label' }, label));
  }
  return googleButton(label);
}

function linkButton(label, href, variant = 'secondary', iconName = null) {
  return h('a', { class: ['btn', `btn-${variant}`, 'btn-block', 'login-btn'], href },
    iconName ? icon(iconName, { size: 15 }) : null, label);
}

function alertBox({ tone, title, text }) {
  const icons = { danger: 'alert-circle', warning: 'alert-triangle', success: 'check-circle', info: 'info', neutral: 'info' };
  return h('div', { class: ['banner', `banner-${tone}`, 'login-alert'], role: tone === 'danger' || tone === 'warning' ? 'alert' : 'status' },
    h('span', { class: 'banner-icon' }, icon(icons[tone] || 'info', { size: 16 })),
    h('div', { class: 'banner-body' },
      title ? h('div', { class: 'banner-title' }, title) : null,
      text ? h('div', { class: 'banner-text' }, text) : null));
}

function noticeAlert() {
  const denied = state && state.deniedEmail;
  switch (notice.error) {
    case null:
    case '':
      break;
    case 'not_approved':
      return alertBox({
        tone: 'danger',
        title: t('Access is not approved yet'),
        text: denied
          ? t('{email} is not on the list of approved accounts for this workspace. An access request has been sent to the administrators. Sign in again after it is approved, or choose another account.', { email: denied })
          : t('This Google account is not on the list of approved accounts for this workspace. An access request has been sent to the administrators. Sign in again after it is approved, or choose another account.'),
      });
    case 'cancelled':
      return alertBox({ tone: 'neutral', title: t('Sign-in was cancelled'), text: t('Choose a Google account to continue.') });
    case 'expired':
      return alertBox({ tone: 'warning', title: t('The sign-in request expired'), text: t('Start again. If this keeps happening, allow cookies for this site.') });
    case 'unavailable':
      return alertBox({ tone: 'warning', title: t('Google sign-in is not enabled'), text: t('This deployment does not use Google sign-in.') });
    case 'rate_limited':
      return alertBox({ tone: 'warning', title: t('Too many sign-in attempts'), text: t('Wait a minute and try again.') });
    case 'signout_partial':
      return alertBox({ tone: 'warning', title: t('Signed out on this browser'), text: t('Sessions on other devices could not be ended right now. They end automatically when they expire.') });
    default:
      return alertBox({ tone: 'danger', title: t('Google sign-in could not be completed'), text: t('Try again in a moment. If the problem continues, contact your administrator.') });
  }
  if (notice.signedOut) return alertBox({ tone: 'success', title: t('You have been signed out'), text: t('Your sessions on all devices have ended.') });
  if (notice.reason === 'session') return alertBox({ tone: 'info', title: t('Your session has ended'), text: t('Sign in again to continue.') });
  return null;
}

async function signOut(btn) {
  btn.disabled = true;
  btn.classList.add('is-busy');
  try {
    const res = await api.post('/api/v1/auth/logout', {});
    const target = res && res.data && res.data.redirect;
    window.location.replace(isSameOriginPath(target) ? target : '/login?signed_out=1');
  } catch (err) {
    // 401: the session had already ended. Otherwise this browser is signed out but the
    // revocation of other sessions could not be saved.
    window.location.replace(err && err.status === 401 ? '/login?signed_out=1' : '/login?error=signout_partial');
  }
}

/* ------------------------------------------------------------------ views */

function brandHeader() {
  return h('div', { class: 'login-brand' },
    brandMark(state && state.branding, { size: 24, cls: 'login-mark' }),
    h('div', { class: 'brand-text' },
      h('span', { class: 'brand-name' }, productName()),
      orgName() ? h('span', { class: 'brand-org' }, orgName()) : null));
}

function heading(title, desc) {
  return h('div', { class: 'login-head' },
    h('h1', { class: 'login-title' }, title),
    desc ? h('p', { class: 'login-desc' }, desc) : null);
}

function note(text, iconName = 'lock') {
  return h('p', { class: 'login-note' }, icon(iconName, { size: 14 }), h('span', null, text));
}

function accountBox(user) {
  return h('div', { class: 'login-account' },
    accountAvatar(user, 'lg'),
    h('div', { class: 'login-account-text' },
      h('strong', { title: user.name || user.email }, user.name || user.email),
      user.name ? h('span', { title: user.email }, user.email) : null),
    h('span', { class: ['badge', user.role === 'admin' ? 'badge-accent' : 'badge-neutral', 'sm'] },
      user.role === 'admin' ? t('Administrator') : t('Viewer')));
}

function passwordFormView() {
  const userInput = h('input', {
    type: 'text',
    class: 'input login-input',
    name: 'username',
    placeholder: 'admin',
    autocomplete: 'username',
    autocapitalize: 'none',
    spellcheck: 'false',
    required: 'required',
    'aria-label': t('Account ID or email'),
  });
  const passInput = h('input', {
    type: 'password',
    class: 'input login-input',
    name: 'password',
    placeholder: '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022',
    autocomplete: 'current-password',
    required: 'required',
    'aria-label': t('Password'),
  });
  const errSlot = h('div', { class: 'login-form-error' });
  const submitBtn = h('button', {
    type: 'submit',
    class: ['btn', 'btn-primary', 'btn-block', 'login-btn'],
  }, icon('lock', { size: 15 }), t('Sign in'));

  const form = h('form', { class: 'login-form', novalidate: 'novalidate' },
    errSlot,
    h('label', { class: 'login-field' },
      h('span', { class: 'login-label' }, t('Account ID or email')),
      userInput),
    h('label', { class: 'login-field' },
      h('span', { class: 'login-label' }, t('Password')),
      passInput),
    submitBtn);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    mount(errSlot);
    const username = userInput.value.trim();
    const password = passInput.value;
    if (!username || !password) {
      mount(errSlot, alertBox({
        tone: 'danger',
        title: t('Enter your account ID and password'),
        text: t('Both fields are required to sign in.'),
      }));
      if (!username) userInput.focus();
      else passInput.focus();
      return;
    }
    submitBtn.disabled = true;
    submitBtn.classList.add('is-busy');
    try {
      const res = await api.post('/api/v1/auth/login', {
        username,
        password,
        next: nextTarget(),
      });
      const target = res && res.data && res.data.redirect;
      window.location.replace(isSameOriginPath(target) ? target : nextTarget());
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.classList.remove('is-busy');
      mount(errSlot, alertBox({
        tone: 'danger',
        title: t('Sign-in failed'),
        text: (err && err.message) ? t(err.message) : t('Invalid account ID or password.'),
      }));
      passInput.select();
    }
  });

  return form;
}

function signInView() {
  const guest = Boolean(state && state.guestAccess);
  const defaultAdmin = Boolean(state && state.defaultAdminPassword);
  return [
    heading(t('Sign in'), t('Sign in with your account ID and password to continue to {product}.', { product: productName() })),
    noticeAlert(),
    defaultAdmin ? alertBox({
      tone: 'info',
      title: t('Initial administrator account'),
      text: t('Default login: ID "admin" / Password "admin". You can change this password and create customer accounts in Settings > Access control.'),
    }) : null,
    passwordFormView(),
    h('div', { class: 'login-divider' }, h('span', null, t('or'))),
    h('div', { class: 'login-actions' },
      googleButton(),
      guest ? linkButton(t('Continue as guest'), nextTarget(), 'ghost', 'arrow-right') : null,
      notice.error === 'not_approved' && state && state.switchAccountUrl ? switchAccountButton(t('Use another account')) : null),
    note(guest
      ? t('Guests can look around with identifiers hidden. Administration needs an account approved by your administrators.')
      : t('Sign in with your assigned ID and password, or use an approved Google account.')),
  ];
}

function signedInView(user) {
  const signOutBtn = h('button', { type: 'button', class: ['btn', 'btn-ghost', 'btn-block', 'login-btn'] }, icon('logout', { size: 15 }), t('Sign out'));
  signOutBtn.addEventListener('click', () => signOut(signOutBtn));
  if (isLogoutPath) {
    return [
      heading(t('Sign out'), t('Sign out of {product} on all of your devices?', { product: productName() })),
      accountBox(user),
      h('div', { class: 'login-actions' },
        (() => {
          const primary = h('button', { type: 'button', class: ['btn', 'btn-primary', 'btn-block', 'login-btn'] }, icon('logout', { size: 15 }), t('Sign out'));
          primary.addEventListener('click', () => signOut(primary));
          return primary;
        })(),
        linkButton(t('Cancel'), '/', 'ghost')),
    ];
  }
  return [
    heading(t('You are signed in'), t('Continue to {product} or switch to another Google account.', { product: productName() })),
    noticeAlert(),
    accountBox(user),
    h('div', { class: 'login-actions' },
      linkButton(t('Continue to {product}', { product: productName() }), nextTarget(), 'primary', 'arrow-right'),
      switchAccountButton(t('Use another account')),
      signOutBtn),
  ];
}

function otherModeView(mode) {
  if (mode === 'iap') {
    return [
      heading(t('Sign in'), t('This workspace signs you in with your organization account through Identity-Aware Proxy.')),
      noticeAlert(),
      h('div', { class: 'login-actions' }, linkButton(t('Continue to {product}', { product: productName() }), nextTarget(), 'primary', 'arrow-right')),
      note(t('Access is managed by your Google Cloud administrators.'), 'shield'),
    ];
  }
  // local development mode (the only other mode): only this computer can open the dashboard.
  return [
    heading(t('No sign-in required'), t('This deployment runs in local development mode. Only this computer can open it.')),
    noticeAlert(),
    h('div', { class: 'login-actions' }, linkButton(t('Open the dashboard'), '/', 'primary', 'arrow-right')),
  ];
}

function errorView() {
  const retry = h('button', { type: 'button', class: ['btn', 'btn-primary', 'btn-block', 'login-btn'] }, icon('refresh', { size: 15 }), t('Try again'));
  retry.addEventListener('click', () => window.location.reload());
  return [
    heading(t('Sign in'), null),
    alertBox({ tone: 'danger', title: t('The sign-in service is unavailable'), text: t('The server could not be reached. Check your connection and try again.') }),
    h('div', { class: 'login-actions' }, retry),
  ];
}

function renderCard() {
  let body;
  if (loadFailed || !state) body = errorView();
  else if (state.authMode === 'google' && state.user) body = signedInView(state.user);
  else if (state.authMode === 'google') body = signInView();
  else body = otherModeView(state.authMode);
  mount(card, brandHeader(), body);
  card.removeAttribute('aria-busy');
}

function renderFooter() {
  const version = state && state.product && state.product.version;
  const langBtn = (locale, label) => {
    const btn = h('button', { type: 'button', 'aria-pressed': getLocale() === locale ? 'true' : 'false', lang: locale }, label);
    btn.addEventListener('click', () => {
      if (getLocale() === locale) return;
      saveLocale(locale);
      setLocale(locale);
      render();
    });
    return btn;
  };
  mount(footer,
    h('span', { class: 'login-footer-meta' }, version ? `${productName()} ${version}` : productName()),
    h('div', { class: 'login-lang', role: 'group', 'aria-label': t('Language') },
      langBtn('en', 'English'), langBtn('ko', '\ud55c\uad6d\uc5b4')));
}

function renderAside() {
  const tagline = state && state.product && state.product.tagline;
  mount(aside, h('div', { class: 'aside-inner' },
    h('div', { class: 'aside-brand' }, h('span', { class: 'aside-glyph' }, brandGlyph(22)), h('span', null, productName())),
    h('div', { class: 'aside-copy' },
      h('p', { class: 'aside-eyebrow' }, t('AI governance workspace')),
      h('h2', { class: 'aside-title' }, tagline ? t(tagline) : t('Unified governance for Gemini Enterprise and Agent Platform')),
      h('ul', { class: 'aside-features' }, FEATURES.map(([ic, title, desc]) => h('li', null,
        h('span', { class: 'aside-feature-icon' }, icon(ic, { size: 16 })),
        h('div', { class: 'aside-feature-text' }, h('strong', null, t(title)), h('span', null, t(desc))))))),
    orgName() ? h('p', { class: 'aside-foot' }, t('Licensed to {org}', { org: orgName() })) : h('span')));
}

function render() {
  document.title = `${isLogoutPath && state && state.user ? t('Sign out') : t('Sign in')} \u00b7 ${productName()}`;
  renderCard();
  renderFooter();
  renderAside();
}

async function init() {
  try {
    const res = await api.get('/api/v1/auth/state');
    state = res.data || null;
    setCsrfToken(state && state.csrfToken);
  } catch (err) {
    loadFailed = true;
  }
  setLocale(initialLocale());
  if (state) {
    applyAccent(state.branding && state.branding.accentColor);
    applyDefaultTheme();
  }
  render();
  if (notice.error || notice.signedOut || notice.reason) cleanUrl();
}

init();
