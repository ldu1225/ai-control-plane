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
  [/^Only the first (\d+) engines per location are listed\.$/, 'Only the first {a} engines per location are listed.'],
  [/^Gemini Enterprise apps in (\d+) linked project\(s\) could not be read: (.+)\. Grant the runtime service account Discovery Engine Viewer there\.$/, 'Gemini Enterprise apps in {a} linked project(s) could not be read: {b}. Grant the runtime service account Discovery Engine Viewer there.'],
  [/^Only the first (\d+) runtime revisions are listed\.$/, 'Only the first {a} runtime revisions are listed.'],
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
  [/^Dataset (\S+) reachable \(0 tables yet; log sinks create tables on first event\)$/, 'Dataset {a} reachable (0 tables yet; log sinks create tables on first event)'],
  [/^(\d+) tables found \((\d+) core log tables\)$/, '{a} tables found ({b} core log tables)'],
  [/^(\d+) license configuration\(s\) found$/, '{a} license configuration(s) found'],
  [/^Reachable in (\S+)$/, 'Reachable in {a}'],
  [/^Policy readable; missing: (.+)$/, 'Policy readable; missing: {a}'],
  [/^(\S+) reachable in (\S+)$/, '{a} reachable in {b}'],
  [/^The default model (\S+) is not available in (\S+)\.$/, 'The default model {a} is not available in {b}.'],
  // Safety gates
  [/^(\d+) of (\d+) users have a determinate state\.$/, '{a} of {b} users have a determinate state.'],
  [/^(\d+) inference log records in the window\.$/, '{a} inference log records in the window.'],
  [/^(\d+) planned suspensions; budget is (\d+) per run\.$/, '{a} planned suspensions; budget is {b} per run.'],
  [/^(\d+) planned suspensions; budget is (\d+) per run, so (\d+) wait for the next run\.$/, '{a} planned suspensions; budget is {b} per run, so {c} wait for the next run.'],
  [/^No group or domain grants on (\S+)\.$/, 'No group or domain grants on {a}.'],
  [/^(\d+) group or domain grant\(s\) on (\S+); affected users are not enforceable\.$/, '{a} group or domain grant(s) on {b}; affected users are not enforceable.'],
  // Governance results
  [/^Marker role (\S+) does not exist\. Run setup first\.$/, 'Marker role {a} does not exist. Run setup first.'],
  [/^Role (\S+) is needed to stop Antigravity for a person, but the dashboard service account is not allowed to read or create custom roles in this project\. Grant it roles\/iam\.roleAdmin and try again\.$/, 'Role {a} is needed to stop Antigravity for a person, but the dashboard service account is not allowed to read or create custom roles in this project. Grant it roles/iam.roleAdmin and try again.'],
  [/^Role (\S+) is needed to stop Antigravity for a person, but it is deleted and could not be restored \((\w+)\)\. Restore it in IAM > Roles, or enter another role ID in Settings > Governance policy\.$/, 'Role {a} is needed to stop Antigravity for a person, but it is deleted and could not be restored ({b}). Restore it in IAM > Roles, or enter another role ID in Settings > Governance policy.'],
  [/^Role (\S+) is needed to stop Antigravity for a person, but Google Cloud does not accept this role ID right now, which happens for up to 44 days after a role with the same ID was deleted\. Enter another role ID in Settings > Governance policy\.$/, 'Role {a} is needed to stop Antigravity for a person, but Google Cloud does not accept this role ID right now, which happens for up to 44 days after a role with the same ID was deleted. Enter another role ID in Settings > Governance policy.'],
  [/^Role (\S+) is needed to stop Antigravity for a person, but it could not be created because the permissions of (\S+) could not be read\.$/, 'Role {a} is needed to stop Antigravity for a person, but it could not be created because the permissions of {b} could not be read.'],
  [/^Role (\S+) is needed to stop Antigravity for a person, but it could not be prepared \((\w+)\)\. Try again in a moment\.$/, 'Role {a} is needed to stop Antigravity for a person, but it could not be prepared ({b}). Try again in a moment.'],
  [/^The IAM policy update failed \((\w+)\)\.$/, 'The IAM policy update failed ({a}).'],
  [/^The role could not be read \((\w+)\)\.$/, 'The role could not be read ({a}).'],
  [/^The deleted role could not be restored \((\w+)\)\.$/, 'The deleted role could not be restored ({a}).'],
  [/^The permissions of (\S+) could not be read\.$/, 'The permissions of {a} could not be read.'],
  [/^Created with (\d+) permissions \(businessaicode\.\* excluded\)\.$/, 'Created with {a} permissions (businessaicode.* excluded).'],
  [/^The role could not be created \((\w+)\)\.$/, 'The role could not be created ({a}).'],
  [/^This user holds (\S+) on the project, which grants full project control beyond AI services\. Remove that role in Google Cloud IAM first\.$/, 'This user holds {a} on the project, which grants full project control beyond AI services. Remove that role in Google Cloud IAM first.'],
  [/^Conditional project IAM bindings remain \((.+)\) and must be removed in Google Cloud IAM\.$/, 'Conditional project IAM bindings remain ({a}) and must be removed in Google Cloud IAM.'],
  // Automatic token prices & telemetry routing
  [/^Billed prices are in (\w+) and could not be converted to (\w+), so the public price list is used instead\.$/, 'Billed prices are in {a} and could not be converted to {b}, so the public price list is used instead.'],
  [/^No exchange rate for USD\/(\w+) is available yet, so estimates are shown in USD\.$/, 'No exchange rate for USD/{a} is available yet, so estimates are shown in USD.'],
  [/^(\d+) model\(s\) have no official price yet \((.+)\); their tokens are counted at zero cost\. Add an alias or a price in Settings > Data collection and cost\.$/, '{a} model(s) have no official price yet ({b}); their tokens are counted at zero cost. Add an alias or a price in Settings > Data collection and cost.'],
  [/^Only the first (\d+) log names per project are listed\.$/, 'Only the first {a} log names per project are listed.'],
  [/^The maximum number of routes \((\d+)\) has been reached\.$/, 'The maximum number of routes ({a}) has been reached.'],
  [/^A route can send at most (\d+) logs\.$/, 'A route can send at most {a} logs.'],
  [/^Daily budget exceeded \(([\d.]+) GiB \/ ([\d.]+) GiB\)$/, 'Daily budget exceeded ({a} GiB / {b} GiB)'],
  [/^Workspace daily budget exceeded \(([\d.]+) GiB \/ ([\d.]+) GiB\)$/, 'Workspace daily budget exceeded ({a} GiB / {b} GiB)'],
  // Request validation
  [/^Unknown settings key: (\S+)$/, 'Unknown settings key: {a}'],
  [/^Setting (\S+) cannot be reset from the UI\.$/, 'Setting {a} cannot be reset from the UI.'],
  [/^(\S+) can only be changed in config\.yaml or the environment\.$/, '{a} can only be changed in config.yaml or the environment.'],
  [/^Invalid value for (\S+): (.+)\.$/, 'Invalid value for {a}: {b}.'],
  [/^The date range must not exceed (\d+) days\.$/, 'The date range must not exceed {a} days.'],
  [/^Parameter '(\w+)' must be a date in YYYY-MM-DD format\.$/, "Parameter '{a}' must be a date in YYYY-MM-DD format."],
  [/^Parameter '(\w+)' is not a valid calendar date\.$/, "Parameter '{a}' is not a valid calendar date."],
  [/^Field 'question' must be 1 to (\d+) characters\.$/, "Field 'question' must be 1 to {a} characters."],
  [/^Field 'message' must be 1 to (\d+) characters\.$/, "Field 'message' must be 1 to {a} characters."],
  [/^Mapping '(\w+)' must be a dotted path of identifiers\.$/, "Mapping '{a}' must be a dotted path of identifiers."],
  [/^Mapping '(\w+)' refers to a column that does not exist\.$/, "Mapping '{a}' refers to a column that does not exist."],
  // Google OAuth client connection
  [/^Add (\S+) as an authorized redirect URI of this client \(Google Auth Platform > Clients > the client\), save, and connect again\. A change saved a moment ago can take a few minutes to apply\.$/, 'Add {a} as an authorized redirect URI of this client (Google Auth Platform > Clients > the client), save, and connect again. A change saved a moment ago can take a few minutes to apply.'],
  // Chargeback organization CSV import (limits)
  [/^The CSV file is larger than (\d+) MB\.$/, 'The CSV file is larger than {a} MB.'],
  [/^The CSV file has more than (\d+) rows\.$/, 'The CSV file has more than {a} rows.'],
  [/^The CSV file defines more than (\d+) units\.$/, 'The CSV file defines more than {a} units.'],
  [/^The CSV file could not be read at row (\d+)\. Check the quotes in that row\.$/, 'The CSV file could not be read at row {a}. Check the quotes in that row.'],
  // Chargeback month close and seat costs
  [/^The month close has too many rows \(more than (\d+)\)\.$/, 'The month close has too many rows (more than {a}).'],
  [/^(\d+) rows of the month close could not be copied to BigQuery\.$/, '{a} rows of the month close could not be copied to BigQuery.'],
  [/^Seat prices are in USD, so seat costs are not added to a report in (\w+)\.$/, 'Seat prices are in USD, so seat costs are not added to a report in {a}.'],
  // Workspace scope discovery (folder / organization scope)
  [/^Not every project in the scope is included: discovery stops at (\d+) projects \(workspace\.scope_max_projects\), (\d+) folders and (\d+) folder levels\.$/, 'Not every project in the scope is included: discovery stops at {a} projects (workspace.scope_max_projects), {b} folders and {c} folder levels.'],
  [/^Some folders or organizations in the scope could not be read \((.+)\)\.$/, 'Some folders or organizations in the scope could not be read ({a}).'],
  // Safety gates of the IAM deny policy method (G1, G6 capacity, G7 surge guard)
  [/^Deny policies read at (\d+) attachment point\(s\)\.$/, 'Deny policies read at {a} attachment point(s).'],
  [/^The deny policy could not be read at (\d+) of (\d+) attachment points\.$/, 'The deny policy could not be read at {a} of {b} attachment points.'],
  [/^At most (\d+) people per attachment point; the limit is (\d+)\.$/, 'At most {a} people per attachment point; the limit is {b}.'],
  [/^(\d+) people at (\S+); a warning starts at (\d+) and the limit is (\d+)\.$/, '{a} people at {b}; a warning starts at {c} and the limit is {d}.'],
  [/^(\d+) new suspensions wait because (\S+) would go over the limit of (\d+) people\.$/, '{a} new suspensions wait because {b} would go over the limit of {c} people.'],
  [/^(\d+) new suspensions of (\d+) people; the surge guard starts at (\d+) people and ([\d.]+)%\.$/, '{a} new suspensions of {b} people; the surge guard starts at {c} people and {d}%.'],
  [/^(\d+) new suspensions of (\d+) people reach the surge guard \((\d+) people and ([\d.]+)%\), so nobody is suspended in this run\.$/, '{a} new suspensions of {b} people reach the surge guard ({c} people and {d}%), so nobody is suspended in this run.'],
  // Organization and project budgets
  [/^Budget (\d+): unit (\S+) is not in the organization model\. Reload the organization tree and choose a unit from it\.$/, 'Budget {a}: unit {b} is not in the organization model. Reload the organization tree and choose a unit from it.'],
  [/^Budget (\d+) \(project (\S+)\): a hard cap is allowed only for projects of this workspace\. Choose notify or a workspace project\.$/, 'Budget {a} (project {b}): a hard cap is allowed only for projects of this workspace. Choose notify or a workspace project.'],
  [/^Budgets are in USD, so they are not measured against a chargeback report in (\w+)\.$/, 'Budgets are in USD, so they are not measured against a chargeback report in {a}.'],
  [/^The budget status is unavailable: (.+)$/, 'The budget status is unavailable: {a}'],
  // Google spend caps of project hard caps
  [/^Access to .+ was denied\. The service identity needs budget permissions on the billing account, including billing\.budgets\.configureSpendCap\.$/, 'Access was denied. The service identity needs budget permissions on the billing account, including billing.budgets.configureSpendCap.'],
  [/^Google refused the request for .+ \(([A-Za-z0-9_]+)\)\.$/, 'Google refused the request ({a}).'],
  [/^The billing account has more than (\d+) budgets, so the control plane does not manage spend caps on it\.$/, 'The billing account has more than {a} budgets, so the control plane does not manage spend caps on it.'],
  [/^The billing account is billed in ([A-Z]{3})\. Budgets are in USD, so the control plane sets spend caps only on USD billing accounts; set this cap in the Cloud console\.$/, 'The billing account is billed in {a}. Budgets are in USD, so the control plane sets spend caps only on USD billing accounts; set this cap in the Cloud console.'],
  [/^A spend cap of USD ([\d.]+) was created\.$/, 'A spend cap of USD {a} was created.'],
  [/^The spend cap is now USD ([\d.]+)\.$/, 'The spend cap is now USD {a}.'],
  [/^The project number of (\S+) could not be read\.$/, 'The project number of {a} could not be read.'],
  [/^Project (\S+) has no spend cap of the control plane\.$/, 'Project {a} has no spend cap of the control plane.'],
  [/^The spend cap of project (\S+) is not enforced \((.+)\), so there is nothing to lift\.$/, 'The spend cap of project {a} is not enforced ({b}), so there is nothing to lift.'],
  [/^The spend cap of project (\S+) is lifted until the start of the next month\. Usage can take up to an hour to resume\.$/, 'The spend cap of project {a} is lifted until the start of the next month. Usage can take up to an hour to resume.'],
  // Enforcement runs with the IAM deny policy, the enforcement job and leftover deny policies (governance.py)
  [/^The deny policy at (\d+) of (\d+) attachment points was changed outside the dashboard, so the dashboard does not change it\.$/, 'The deny policy at {a} of {b} attachment points was changed outside the dashboard, so the dashboard does not change it.'],
  [/^The deny policy could not be read at (\d+) of (\d+) attachment points \((.+)\)\.$/, 'The deny policy could not be read at {a} of {b} attachment points ({c}).'],
  [/^Deferred to the next run: the deny policy would go over the limit of (\d+) people\.$/, 'Deferred to the next run: the deny policy would go over the limit of {a} people.'],
  [/^The deny policy update failed at (.+) \(([a-z_]+)\)\.$/, 'The deny policy update failed at {a} ({b}).'],
  [/^The deny policy change was refused: (.+)$/, 'The deny policy change was refused: {a}'],
  [/^Recorded\. The enforcement job could not be started \(([a-z_]+)\); the next scheduled run applies it\.$/, 'Recorded. The enforcement job could not be started ({a}); the next scheduled run applies it.'],
  [/^The enforcement job could not be started \(([a-z_]+)\)\. The dashboard service account needs roles\/run\.jobsExecutor on the job\.$/, 'The enforcement job could not be started ({a}). The dashboard service account needs roles/run.jobsExecutor on the job.'],
  [/^Deny policies written while the enforcement method was the IAM deny policy remain at (\d+) attachment point\(s\); the people listed there stay suspended until they are removed\.$/, 'Deny policies written while the enforcement method was the IAM deny policy remain at {a} attachment point(s); the people listed there stay suspended until they are removed.'],
  [/^The deny policy id changed from (\S+) to (\S+); remove the old policy with gcloud iam policies delete\.$/, 'The deny policy id changed from {a} to {b}; remove the old policy with gcloud iam policies delete.'],
  // Marker roles left by the allow-policy method after a switch of the enforcement method (governance.release_markers)
  [/^(\d+) people still hold a marker role of the allow-policy method; they get their access role back once the current enforcement method no longer needs the marker\.$/, '{a} people still hold a marker role of the allow-policy method; they get their access role back once the current enforcement method no longer needs the marker.'],
  [/^Marker roles left by the allow-policy method could not be checked or released \(([a-z_]+)\)\. The dashboard service account needs the project IAM roles of the allow-policy method until they are released\.$/, 'Marker roles left by the allow-policy method could not be checked or released ({a}). The dashboard service account needs the project IAM roles of the allow-policy method until they are released.'],
  // IAM policy API quota and accounts IAM does not find (governance._deny_warnings, governance._apply_deny)
  [/^(\d+) deny policy attachment points: every run reads each of them, and the IAM policy API allows (\d+) policy reads and (\d+) writes per minute per project by default\. Choose the folders or organization of the workspace scope as the deny policy attachment, or ask for a higher quota\.$/, '{a} deny policy attachment points: every run reads each of them, and the IAM policy API allows {b} policy reads and {c} writes per minute per project by default. Choose the folders or organization of the workspace scope as the deny policy attachment, or ask for a higher quota.'],
  [/^(\d+) account\(s\) could not be listed in the deny policy because IAM does not find them \(deleted, or not a Google account\)\.$/, '{a} account(s) could not be listed in the deny policy because IAM does not find them (deleted, or not a Google account).'],
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
  unavailable: 'The service is temporarily unavailable.',
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
