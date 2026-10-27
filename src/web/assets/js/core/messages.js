/**
 * Localization of server-provided text.
 *
 * The API always answers in English so that logs, scripts and support cases stay consistent.
 * This module renders that text in the active locale: fixed sentences are looked up in the
 * locale dictionary, templated sentences are matched by pattern, and failures fall back to a
 * sentence derived from the machine-readable code. Text that cannot be localized is shown
 * verbatim so that no detail is lost.
 */
import { t, getLocale, hasTranslation } from './i18n.js';

/* [pattern over the English server text, dictionary key]. Capture groups fill {a}, {b}, ... */
const TEMPLATES = [
  // Data source notices
  [/^Only the first (\d+) reasoning engines per region are listed\.$/, 'Only the first {a} reasoning engines per region are listed.'],
  [/^Only the first (\d+) engines are inspected\.$/, 'Only the first {a} engines are inspected.'],
  [/^Only the first (\d+) user licenses are shown\.$/, 'Only the first {a} user licenses are shown.'],
  [/^Only the first (\d+) log names are listed\.$/, 'Only the first {a} log names are listed.'],
  [/^Only the (\d+) most active users are listed\.$/, 'Only the {a} most active users are listed.'],
  [/^Only the first (\d+) tables of (\S+) are inspected\.$/, 'Only the first {a} tables of {b} are inspected.'],
  [/^The schema of (\d+) table\(s\) in (\S+) could not be read\.$/, 'The schema of {a} table(s) in {b} could not be read.'],
  [/^Log entries were truncated at (\d+)\.$/, 'Log entries were truncated at {a}.'],
  [/^Table not found in dataset (\S+)$/, 'Table not found in dataset {a}'],
  [/^Billing export table not found in dataset (\S+)$/, 'Billing export table not found in dataset {a}'],
  [/^Dataset (\S+) is not a valid identifier\.$/, 'Dataset {a} is not a valid identifier.'],
  [/^Dataset (\S+) was not found in project (\S+)\.$/, 'Dataset {a} was not found in project {b}.'],
  // Data source health
  [/^(\d+) tables found \((\d+) core log tables\)$/, '{a} tables found ({b} core log tables)'],
  [/^(\d+) license configuration\(s\) found$/, '{a} license configuration(s) found'],
  [/^Reachable in (\S+)$/, 'Reachable in {a}'],
  [/^Policy readable; missing: (.+)$/, 'Policy readable; missing: {a}'],
  [/^(\S+) reachable in (\S+)$/, '{a} reachable in {b}'],
  [/^The default model (\S+) is not available in (\S+)\.$/, 'The default model {a} is not available in {b}.'],
  // Safety gates
  [/^(\d+) of (\d+) users have a determinate state\.$/, '{a} of {b} users have a determinate state.'],
  [/^(\d+) inference log records in the window\.$/, '{a} inference log records in the window.'],
  [/^(\d+) planned changes; budget is (\d+)\.$/, '{a} planned changes; budget is {b}.'],
  [/^No group or domain grants on (\S+)\.$/, 'No group or domain grants on {a}.'],
  [/^(\d+) group or domain grant\(s\) on (\S+); affected users are not enforceable\.$/, '{a} group or domain grant(s) on {b}; affected users are not enforceable.'],
  // Governance results
  [/^Marker role (\S+) does not exist\. Run setup first\.$/, 'Marker role {a} does not exist. Run setup first.'],
  [/^The IAM policy update failed \((\w+)\)\.$/, 'The IAM policy update failed ({a}).'],
  [/^The role could not be read \((\w+)\)\.$/, 'The role could not be read ({a}).'],
  [/^The permissions of (\S+) could not be read\.$/, 'The permissions of {a} could not be read.'],
  [/^Created with (\d+) permissions \(businessaicode\.\* excluded\)\.$/, 'Created with {a} permissions (businessaicode.* excluded).'],
  [/^The role could not be created \((\w+)\)\.$/, 'The role could not be created ({a}).'],
  // Automatic token prices
  [/^Billed prices are in (\w+) and could not be converted to (\w+), so the public price list is used instead\.$/, 'Billed prices are in {a} and could not be converted to {b}, so the public price list is used instead.'],
  // Request validation
  [/^Unknown settings key: (\S+)$/, 'Unknown settings key: {a}'],
  [/^Setting (\S+) cannot be reset from the UI\.$/, 'Setting {a} cannot be reset from the UI.'],
  [/^(\S+) can only be changed in config\.yaml or the environment\.$/, '{a} can only be changed in config.yaml or the environment.'],
  [/^Invalid value for (\S+): (.+)\.$/, 'Invalid value for {a}: {b}.'],
  [/^The date range must not exceed (\d+) days\.$/, 'The date range must not exceed {a} days.'],
  [/^Parameter '(\w+)' must be a date in YYYY-MM-DD format\.$/, "Parameter '{a}' must be a date in YYYY-MM-DD format."],
  [/^Parameter '(\w+)' is not a valid calendar date\.$/, "Parameter '{a}' is not a valid calendar date."],
  [/^Field 'question' must be 1 to (\d+) characters\.$/, "Field 'question' must be 1 to {a} characters."],
  [/^Mapping '(\w+)' must be a dotted path of identifiers\.$/, "Mapping '{a}' must be a dotted path of identifiers."],
  [/^Mapping '(\w+)' refers to a column that does not exist\.$/, "Mapping '{a}' refers to a column that does not exist."],
];

/*
 * Upstream failure sentences have the form "<subject> <cause>". The subject is always rendered
 * next to the message (data source chip, health check label or model name), so the localized
 * sentence states only the cause and the remedy. Checked after exact sentences and TEMPLATES.
 */
const UPSTREAM = [
  [/^Access to .+ was denied for the service identity\.$/, 'Access was denied for the service identity. Check the IAM roles of the runtime service account.'],
  [/^.+ timed out\.$/, 'The request timed out.'],
  [/^.+ exceeded the configured bytes-billed limit\.$/, 'The query exceeded the configured bytes-billed limit.'],
  [/^.+ could not be queried\.$/, 'The query could not be run.'],
  [/^.+ rejected the request\.$/, 'The request was rejected by the service.'],
  [/^.+ is temporarily unavailable\.$/, 'The service is temporarily unavailable.'],
  [/^.+ could not be processed\.$/, 'The data could not be processed.'],
  [/^.+ was not found\.$/, 'The resource was not found.'],
];

/* Fallback sentences by warning code (data source notices). */
const NOTICE_CODES = {
  access_denied: 'Access was denied for the service identity. Check the IAM roles of the runtime service account.',
  not_found: 'The resource was not found.',
  credentials_unavailable: 'Google Cloud credentials are unavailable.',
  timeout: 'The request timed out.',
  cost_limit: 'The query exceeded the configured bytes-billed limit.',
  query_failed: 'The query could not be run.',
  api_error: 'The request was rejected by the service.',
  upstream_unavailable: 'The service is temporarily unavailable.',
  internal_error: 'The data could not be processed.',
  invalid: 'The query definition is invalid.',
  truncated: 'Only part of the results is shown.',
  partial_data: 'Only part of the data is available.',
  unsupported_schema: 'The table schema is not supported.',
  not_configured: 'The data source is not configured.',
  table_not_found: 'The table was not found.',
  dataset_not_found: 'The dataset was not found.',
  disabled: 'This connector is disabled.',
};

/* Fallback sentences by API error code (toasts, banners, inline form errors). */
const ERROR_CODES = {
  bad_request: 'The request is invalid.',
  forbidden: 'Administrator privileges are required.',
  csrf_failed: 'CSRF validation failed. Reload the page and try again.',
  rate_limited: 'Too many requests. Please retry shortly.',
  unauthenticated: 'Authentication is required.',
  module_disabled: 'This module is disabled in the workspace settings.',
  not_configured: 'The workspace is not configured. Set workspace.project_id first.',
  not_found: 'The resource was not found.',
  conflict: 'The request conflicts with the current state. Reload and try again.',
  upstream_error: 'An upstream Google Cloud service is unavailable.',
  network: 'The server could not be reached.',
  internal: 'An internal error occurred.',
};

const DIGITS = /^\d+$/;

function fill(key, groups) {
  const tag = getLocale() === 'ko' ? 'ko-KR' : 'en-US';
  const vars = {};
  groups.forEach((g, i) => {
    vars[String.fromCharCode(97 + i)] = DIGITS.test(g) ? Number(g).toLocaleString(tag) : g;
  });
  return t(key, vars);
}

function translate(text) {
  if (typeof text !== 'string' || !text) return null;
  if (hasTranslation(text)) return t(text);
  for (const list of [TEMPLATES, UPSTREAM]) {
    for (const [re, key] of list) {
      const m = re.exec(text);
      if (m && hasTranslation(key)) return fill(key, m.slice(1));
    }
  }
  return null;
}

/** Server text in the active locale; text without a translation is returned verbatim. */
export function serverText(text) {
  if (text === undefined || text === null || text === '') return '';
  const s = String(text);
  if (getLocale() === 'en') return s;
  return translate(s) || s;
}

/** Data source notice {source, code, message}: translated message, else a sentence for the code. */
export function noticeText(w) {
  if (!w) return '';
  const msg = w.message || '';
  if (getLocale() === 'en') return msg || w.code || '';
  const known = translate(msg);
  if (known) return known;
  if (NOTICE_CODES[w.code] && hasTranslation(NOTICE_CODES[w.code])) return t(NOTICE_CODES[w.code]);
  return msg || w.code || '';
}

/** Error text for toasts, banners and form errors (ApiError or Error). */
export function errorText(err) {
  if (!err) return '';
  const msg = err.message || '';
  if (getLocale() === 'en') return msg;
  const known = translate(msg);
  if (known) return known;
  // Some endpoints (AI Analyst) already answer in the requested language.
  if (/[\uac00-\ud7a3]/.test(msg)) return msg;
  const fallback = ERROR_CODES[err.code];
  if (fallback && hasTranslation(fallback)) return t(fallback);
  return msg;
}

/** Safety gate abort reason ("G4_CHANGE_BUDGET: <detail>; G1_...: <detail>") in the active locale. */
export function abortReasonText(reason) {
  if (!reason) return '';
  return String(reason).split(/;\s(?=G\d_[A-Z0-9_]+:)/).map((part) => {
    const m = /^(G\d_[A-Z0-9_]+):\s(.*)$/.exec(part);
    return m ? `${m[1]}: ${serverText(m[2])}` : serverText(part);
  }).join('; ');
}
