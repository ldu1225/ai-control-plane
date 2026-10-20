/**
 * Minimal i18n: English source strings are keys; locale dictionaries map them to translations.
 * Placeholders use {name} syntax.
 */
import KO from '../i18n/ko.js';

const DICTS = { ko: KO };
let current = 'en';

/** English singular variants, chosen when the count placeholder ({n} or {total}) equals 1. */
const EN_SINGULAR = {
  '{n} new models detected': '{n} new model detected',
  '{n} new telemetry sources detected': '{n} new telemetry source detected',
  '{n} agents are deprecation candidates': '{n} agent is a deprecation candidate',
  '{n} deprecation candidates': '{n} deprecation candidate',
  '{n} agents': '{n} agent',
  '{n} rows': '{n} row',
  '{n} errors': '{n} error',
  '{n} users': '{n} user',
  '{n} days': '{n} day',
  '{n} requests': '{n} request',
  '{n} hours': '{n} hour',
  '{n} spikes highlighted': '{n} spike highlighted',
  '{n} IAM changes applied': '{n} IAM change applied',
  '{n} changes per cycle': '{n} change per cycle',
  '{total} records': '{total} record',
  '{n} data source notices': '{n} data source notice',
  'Activity within {n} days': 'Activity within {n} day',
  'Self-corrected after {n} attempts': 'Self-corrected after {n} attempt',
  'of {n} sanitize operations': 'of {n} sanitize operation',
};

export function setLocale(locale) {
  current = locale === 'ko' ? 'ko' : 'en';
  document.documentElement.lang = current;
}

export function getLocale() {
  return current;
}

function isOne(v) {
  return v !== undefined && v !== null && Number(String(v).replace(/,/g, '')) === 1;
}

/** True when the active locale dictionary has an entry for this source string. */
export function hasTranslation(text) {
  const dict = DICTS[current];
  return !!dict && typeof text === 'string' && Object.prototype.hasOwnProperty.call(dict, text);
}

export function t(text, vars) {
  const dict = DICTS[current];
  let src = text;
  if (current === 'en' && vars && Object.prototype.hasOwnProperty.call(EN_SINGULAR, text) && (isOne(vars.n) || isOne(vars.total))) src = EN_SINGULAR[text];
  let out = (dict && Object.prototype.hasOwnProperty.call(dict, src)) ? dict[src] : src;
  if (vars) {
    out = out.replace(/\{(\w+)\}/g, (match, key) => (Object.prototype.hasOwnProperty.call(vars, key) ? String(vars[key]) : match));
  }
  return out;
}

/** Plural helper: t1 for count === 1, tn otherwise (English); Korean has no plural forms. */
export function tn(count, one, many, vars = {}) {
  return t(count === 1 ? one : many, { count, ...vars });
}
