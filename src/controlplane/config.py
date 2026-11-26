"""Configuration schema, layered loading and validation.

Load order (later wins): built-in defaults < config.yaml < config.local.yaml <
environment overrides < persisted UI overrides (settings store, applied by
settings_store.SettingsManager).
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import os
import re
import urllib.parse

from .httpkit import ApiError
from .timeutil import valid_timezone

LOG = logging.getLogger("controlplane.config")

MODULE_IDS = (
    "overview", "gemini_enterprise", "notebooklm", "model_usage", "agent_gateway",
    "agent_registry", "agent_deployments", "model_armor", "users", "antigravity",
    "finops", "chargeback", "telemetry", "analyst",
)

# Monthly Antigravity (AI developer tools) credit per licensed user, by Gemini Enterprise subscription tier, in USD.
# Source: Gemini Enterprise "Quotas and pay-as-you-go usage" (Standard 10, Plus 15; the Standard Emerging Market
# edition has no bundled AI developer tools quota; the Pay-as-you-go edition has no included quota) and the
# SubscriptionTier enum of the Discovery Engine API (ENTERPRISE = Standard, SEARCH_AND_ASSISTANT = Plus).
# Tiers not listed fall back to governance.antigravity.credit_per_user_month.
DEFAULT_CREDIT_BY_TIER = ("SUBSCRIPTION_TIER_ENTERPRISE=10", "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT=15",
                          "SUBSCRIPTION_TIER_ENTERPRISE_EMERGING=0", "SUBSCRIPTION_TIER_CONSUMPTION_ONLY=0")

# Mandatory exclusions: these patterns are always applied, even if removed from settings.
MANDATORY_EXCLUDED_SKU_PATTERNS = ("code assist", "duet ai")

DEFAULTS = {
    "product": {
        "name": "AI Control Plane",
        "tagline": "Unified governance for Gemini Enterprise and Gemini Platform",
    },
    "branding": {
        "organization_name": "Your Organization",
        "logo_url": "",
        "accent_color": "#2563EB",
        "default_locale": "en",
        "default_theme": "system",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8088,
        "cache_ttl_seconds": 120,
    },
    "workspace": {
        "project_id": "your-gcp-project-id",
        "projects": [],
        "scan_all_projects": True,
        "quota_project_id": "",
        "timezone": "UTC",
        "default_range_days": 14,
        "audit_dataset": "ge_analytics",
        "billing": {
            "project_id": "",
            "dataset": "billing_detailed_usage",
            "table": "gcp_billing_export_resource_v1_XXXXXX_XXXXXX_XXXXXX",
            "account_id": "",
        },
        "discovery_engine_locations": ["global"],
        "agent_engine_regions": ["us-central1"],
        "telemetry_datasets": [],
        # Which projects the workspace covers. projects: the primary project, workspace.projects and (with
        # scan_all_projects) up to 25 projects the service identity can see. folder / organization: every active
        # project under scope_ids (nested folders included), up to scope_max_projects.
        "scope_type": "projects",
        "scope_ids": [],
        "scope_max_projects": 300,
        # gemini_enterprise: keep only scope projects with a Gemini Enterprise subscription distributed to them
        # (license configurations in a discovery_engine_locations location); none: keep every scope project.
        "scope_filter": "gemini_enterprise",
        # Project that holds a central audit dataset fed by an organization or folder aggregated log sink. Empty:
        # every project has its own audit dataset (per-project sinks).
        "audit_project": "",
        # Pages of 500 user licenses read per project and location.
        "license_max_pages": 200,
    },
    "security": {
        "auth_mode": "auto",
        "iap_audience": "",
        "admins": [],
        "allowed_users": [],
        "google_client_id": "",
        "public_base_url": "",
        "session_hours": 12,
        "sign_in_bridge_url": "",
        "bridge_return_url": "",
        "mask_pii_for_viewers": False,
        "guest_access": False,
        "guest_redact_terms": [],
    },
    "storage": {
        "backend": "auto",
        "file_path": "data/settings.json",
        "gcs_bucket": "",
        "gcs_object": "control-plane/settings.json",
    },
    "bigquery": {
        "max_bytes_billed": 20000000000,
    },
    "ai_engine": {
        "location": "global",
        "default_model": "gemini-3.8-flash",
        "models": ["gemini-3.8-flash", "gemini-3.8-pro", "gemini-4-carbon", "gemini-4-argon"],
        "discover_models": True,
        "restrict_to_allowed": True,
        # Tried in order when a requested model is not available in this project (for example a preview model).
        "fallback_models": ["gemini-2.5-flash", "gemini-3-flash-preview", "gemini-2.5-pro", "gemini-2.5-flash-lite"],
    },
    "modules": {module_id: True for module_id in MODULE_IDS},
    "model_catalog": {
        "auto_enable_new_models": True,
        "disabled_models": [],
        "excluded_sku_patterns": list(MANDATORY_EXCLUDED_SKU_PATTERNS),
        "ai_grouping": False,
        # Internal bookkeeping (not user editable): [{"key": ..., "detectedAt": "YYYY-MM-DD"}]
        "known_models": [],
    },
    "gateway": {
        "services": [
            "aiplatform.googleapis.com", "discoveryengine.googleapis.com",
            "modelarmor.googleapis.com", "businessaicode.googleapis.com",
            "notebooklm.googleapis.com", "geminidataanalytics.googleapis.com",
            "generativelanguage.googleapis.com",
        ],
    },
    "agent_lifecycle": {
        "idle_days": 14,
        "deprecation_days": 50,
    },
    "finops": {
        "spike_threshold_pct": 50,
        "spike_min_delta": 1.0,
    },
    "governance": {
        "antigravity": {
            "usage_source": "auto",
            "inference_log": "businessaicode.googleapis.com%2Finference_response",
            "access_role": "roles/discoveryengine.agentspaceUser",
            "cap_marker_role": "CustomAntigravityCapBlocked",
            "admin_marker_role": "CustomAntigravityAdminBlocked",
            "legacy_marker_roles": ["CustomAntigravityRestrictedUser"],
            "privileged_roles": ["roles/owner", "roles/editor", "roles/discoveryengine.admin"],
            "extra_access_roles": [],
            # Settlement period of the fixed caps per period (the default cap model): monthly from reset_day (the 1st)
            # to the end of the month. A person who uses up a cap on the 11th stays suspended until the period ends
            # and is restored automatically on the 1st of the next month; nothing resets weekly.
            "period": "monthly",
            "window": "calendar",
            # Caps of 0 are disabled: nothing is over a cap until an administrator sets one.
            "token_cap": 0,
            "request_cap": 0,
            "change_budget_min": 10,
            "change_budget_ratio": 0.2,
            "max_log_entries": 20000,
            "audit_log_name": "agy-admin%2Fadmin_actions",
            "webhook_url": "",
            # Per-user caps: manual = over-cap users wait for administrator review; auto = the
            # scheduled enforcement run suspends them until the settlement period resets.
            "enforcement_mode": "manual",
            # Estimated cost cap per user and period, in `currency`; 0 disables it. Token prices are
            # found automatically (pricing_source=auto: what the billing account paid per token, then
            # the public Google price list); manual uses the prices below.
            "cost_cap": 0.0,
            "currency": "",               # ISO code of cost caps; empty = billing account currency
            "pricing_source": "auto",     # auto | manual
            "input_price_per_million": 0.0,
            "output_price_per_million": 0.0,
            "model_prices": [],
            # Time zone of calendar period boundaries (daily / weekly / monthly settlement resets);
            # empty = the workspace time zone.
            "reset_timezone": "",
            # Calendar periods start at reset_time (HH:MM); weekly periods on reset_weekday (0 = Monday).
            # Gemini Enterprise enforces Antigravity credits per 7 days from the project's first request,
            # so set these to that cycle to make weekly caps line up with the credit pool.
            "reset_weekday": 0,
            "reset_time": "00:00",
            # Monthly periods start on this day of the month at reset_time (1 = from the 1st to the last day; in a month
            # without the day, such as the 31st, the period starts on the month's last day).
            "reset_day": 1,
            # period "custom": periods of cycle_days days (1-92) counted from cycle_start (YYYY-MM-DD at reset_time;
            # empty = 1970-01-01), for example every 14 days from a Monday.
            "cycle_days": 30,
            "cycle_start": "",
            # Antigravity credit included per licensed user and month in USD (Gemini Enterprise Standard
            # 10, Plus 15; enforced weekly as a shared pool of credit / 4 x seats). Used by the
            # credit-based cap preset and the monthly billing report.
            "credit_per_user_month": 10.0,
            # How caps work. period (default): the fixed token, request and cost caps per settlement period above; a
            # person who reaches a cap is suspended until the period ends (monthly: the 1st of the next month) and
            # nothing resets weekly. credit_budget: each person has a weekly free share of that credit for Gemini models
            # (third-party models such as Claude are always paid) and a monthly paid budget in USD; a person whose
            # paid budget is spent is stopped only while nothing is free (the week's share is used up or they pay
            # again) and is restored at the next weekly reset or month start. Empty (default) = period, except that an
            # installation that relied on credit caps (a paid budget set, no fixed cap) keeps credit_budget until an
            # administrator chooses.
            "cap_model": "",
            "paid_budget_month": -1.0,       # USD per person and month beyond the free credit; -1 = no limit,
                                             # 0 = credits only
            "org_paid_budget_month": -1.0,   # USD for everyone together; when spent, everyone is credits only
            "credit_model_prefixes": ["gemini"],   # model ids that draw on the included credit
            # license: each person's monthly credit follows the subscription tier of their assigned license
            # (credit_by_tier), falling back to credit_per_user_month when the tier or the license is unknown;
            # fixed: credit_per_user_month for everyone. Individual limit rules always win.
            "credit_source": "license",
            "credit_by_tier": list(DEFAULT_CREDIT_BY_TIER),
            # Service accounts allowed to call the enforcement endpoint with a Google OIDC token
            # (Cloud Scheduler). Configured in config.yaml or SCHEDULER_INVOKERS only.
            "scheduler_invokers": [],
            # How a suspension is applied (services/enforcers.py):
            # iam_allow_swap: swap the person's direct user: binding for a marker role in the primary project's allow
            #   policy (one project, direct bindings only); iam_deny: list the person in an IAM deny policy managed by
            #   the control plane (works for group, inherited and workforce access and for many projects; needs Deny
            #   Admin on the organization for the identity that applies it); notify_only: plan and notify, never change
            #   access.
            "enforcement_backend": "iam_allow_swap",
            # Where the deny policy is attached: projects = every workspace project; scope = the folders or
            # organization of workspace.scope_ids (inherited by every project below), else every project.
            "deny_attachment": "projects",
            # Id of the deny policy the control plane manages at each attachment point (fixed prefix; deployment
            # setting). Other deny policies are never read for changes or modified.
            "deny_policy_id": "ai-control-plane-caps",
            # Also deny the Gemini Enterprise assistant (discoveryengine assistants.assist) to suspended people.
            "deny_ge_app": False,
            # Also deny Gemini Platform model calls (aiplatform endpoints.predict) to suspended people: Antigravity
            # signed in with Application Default Credentials or pay-as-you-go calls Gemini Platform directly.
            "deny_agent_platform": False,
            # Cloud Run job that applies suspensions with its own service account
            # (projects/PROJECT/locations/REGION/jobs/JOB). Empty: the web service applies them itself.
            # Deployment setting (config.yaml or ENFORCEMENT_JOB).
            "enforcement_job": "",
            # Pub/Sub push subscription that posts every Antigravity inference log entry to the usage-event endpoint
            # (projects/PROJECT/subscriptions/SUBSCRIPTION), so that automatic mode suspends a person as soon as a cap
            # is reached instead of at the next scheduled run. Deployment setting (config.yaml or
            # USAGE_EVENTS_SUBSCRIPTION); empty = scheduled runs only.
            "usage_events_subscription": "",
            # G7 surge guard: when one run would newly suspend at least surge_guard_min people and at least
            # surge_guard_ratio of the population, the run suspends nobody and asks for a manual review.
            "surge_guard_min": 10,
            "surge_guard_ratio": 0.5,
            # Manage a Google Cloud spend cap budget (hard stop by Google) for every project budget node marked as a
            # hard cap. Needs the billing account in workspace.billing.account_id and budget permissions.
            "spend_caps": False,
        },
    },
    # Internal chargeback: usage allocated to people, projects and the organization tree (services/chargeback.py).
    "chargeback": {
        # credit_share: what each person pays beyond their weekly free credit share (the Antigravity cap ledger);
        # list: cost at unit prices without any credit.
        "allocation_mode": "credit_share",
        # Project label whose value names an organization unit (its cost center or unit id); empty = not used.
        "project_unit_label": "",
        # Monthly price per assigned license seat by subscription tier ("TIER=USD"); empty = seats not charged.
        "seat_prices": [],
        # Optional BigQuery dataset ('dataset' or 'project.dataset') that receives a copy of every month close.
        "close_dataset": "",
    },
    "telemetry": {
        "connectors": [],
        # Discovery limits. Sources are found from what the project actually writes (Cloud Logging log list,
        # Cloud Monitoring log volume, sampled entries and BigQuery schemas); these values only bound the work.
        "discovery": {
            "max_projects": 8,
            "max_datasets_per_project": 20,
            "max_tables_per_dataset": 100,
            "activity_days": 7,
            "sample_entries": 300,          # entries read in one call to learn payload fields
            "targeted_samples": 12,         # extra per-log reads for logs the shared sample missed
            "max_log_entries": 1000,        # entries a Cloud Logging dashboard reads per view (1000 = one API call)
            "dataset_hints": ["log", "audit", "telemetry", "agent", "gemini", "event"],  # scan order only
            "hidden_logs": [],              # log id substrings never listed
        },
        # Gemini describes, maps and groups every discovered source. Results are kept per source fingerprint.
        "ai": {
            "enabled": True,
            "model": "gemini-4-argon",      # empty = ai_engine.default_model; unavailable -> automatic fallback
            "batch_size": 25,
            "max_sources": 150,
            "profile_ttl_hours": 168,
            "share_value_examples": True,   # send short, masked examples of low-cardinality values
        },
        # Generic name hints used together with value statistics (email ratio, cardinality, numeric type).
        "inference": {
            "time_hints": ["timestamp", "receivetimestamp", "event_time", "event_timestamp", "event_ts", "created_at",
                           "occurred_at", "start_time", "logged_at", "recorded_at", "ts"],
            "user_hints": ["principalemail", "user_email", "useremail", "email", "user_id", "userid", "user",
                           "actor", "principal", "caller", "account"],
            "model_hints": ["model"],
            "token_hints": ["token"],
            "operation_hints": ["method", "operation", "action", "event", "step", "tool", "command", "task",
                                "route", "function", "type", "name"],
            "status_hints": ["status", "code", "severity", "result", "outcome", "verdict", "reason", "state",
                             "phase", "level"],
            "sensitive_hints": ["prompt", "response", "content", "message", "messages", "text", "parts",
                                "instruction", "query"],
        },
        # Brand naming policy applied to service titles read from the Service Usage API ("From=To").
        "display_renames": ["Gemini Enterprise Agent Platform=Gemini Platform", "Vertex AI=Gemini Platform",
                            "Agent Platform=Gemini Platform"],
        # Log Router -> BigQuery routing created from the dashboard, with cost controls.
        "routing": {
            "dataset": "telemetry_routed",  # dedicated dataset, so retention never touches the audit dataset
            "dataset_location": "",         # empty = location of the audit dataset
            "sink_prefix": "acp-tx-",
            "sample_percent": 100.0,        # sample(insertId, ...) in the sink filter
            "min_severity": "",             # empty = every severity
            "exclusion_filters": [],        # Logging filters added as sink exclusions
            "retention_days": 30,           # partition expiration of routed tables; 0 = keep
            "daily_budget_gb": 5.0,         # all managed sinks together per local day; 0 = unlimited
            "sink_daily_gb": 1.0,           # default per sink per local day; 0 = unlimited
            "over_budget_action": "pause",  # pause (disable the sink until the next day) | alert
            "auto_resume": True,
            "max_sinks": 20,
            "follow_groups": True,          # add new logs of a routed group automatically (within budget)
            "notify_webhook_url": "",
        },
    },
    # Token prices for cost estimates when the billing export is not readable. Prices come only from
    # official sources (Cloud Billing Catalog API and Google pricing pages), never from a built-in table.
    "pricing": {
        "currency": "",                     # ISO 4217 display currency; empty = governance currency, else USD
        "sync_time": "00:00",               # local time of the daily price and exchange rate check
        "sync_timezone": "",                # empty = workspace timezone
        "public_sources": [
            "https://aistudio.google.com/docs/pricing.md",
            "https://ai.google.dev/gemini-api/docs/pricing",
            "https://cloud.google.com/vertex-ai/generative-ai/pricing",
            "https://cloud.google.com/text-to-speech/pricing",
            "https://cloud.google.com/speech-to-text/pricing",
        ],
        "fx_sources": ["https://open.er-api.com/v6/latest/USD", "https://api.frankfurter.dev/v1/latest?base=USD"],
        "catalog_services": ["Vertex AI", "Agent Platform", "Gemini API",
                             "Cloud Text-to-Speech API", "Cloud Speech API"],  # Billing Catalog display names
        "include_partner_models": True,     # partner models billed under their own service (for example Claude)
        "storage_service": "BigQuery",      # Billing Catalog service used for routing cost estimates
        "model_aliases": [],                # "my-tuned-model=gemini-2.5-flash"
        "model_overrides": [],              # [{model, input, output, cached}] USD per 1M tokens
        "alert_price_change_pct": 0.5,
        "alert_fx_change_pct": 1.0,
    },
    "analyst": {
        "max_bytes_billed": 10000000000,
        "max_rows": 500,
        # Viewers may use AI Analyst only when this is true (or viewer masking is off): query results can
        # contain free text, such as prompts, that the dashboard endpoints withhold from viewers.
        "allow_viewers": False,
    },
}

# ---------------------------------------------------------------------------
# Identifier regexes
# ---------------------------------------------------------------------------

PROJECT_RE = re.compile(r"^[a-z][a-z0-9\-]{4,61}[a-z0-9]$")
DOMAIN_PROJECT_RE = re.compile(r"^[a-z0-9\-\.:]+$")
DATASET_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
TABLE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,1024}$")
COLUMN_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
ADMIN_PATTERN_RE = re.compile(r"^\*@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,80}$")
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,120}\.googleapis\.com$")
ROLE_RE = re.compile(r"^(roles/[A-Za-z0-9_.]{1,128}|projects/[a-z0-9\-:.]{6,63}/roles/[A-Za-z0-9_.]{3,64}"
                     r"|organizations/[0-9]{1,30}/roles/[A-Za-z0-9_.]{3,64})$")
ROLE_ID_RE = re.compile(r"^[A-Za-z0-9_.]{3,64}$")
LOG_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-%/]{1,512}$")
LOCATION_RE = re.compile(r"^[a-z][a-z0-9\-]{1,40}$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
ASSET_PATH_RE = re.compile(r"^/assets/[A-Za-z0-9_\-/.]{1,200}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
WEBHOOK_HOSTS = ("hooks.slack.com", "chat.googleapis.com")
GOOGLE_CLIENT_ID_RE = re.compile(r"^([0-9]{6,30}-[a-z0-9]{8,64}\.apps\.googleusercontent\.com)?$")
# Member lists edited additively from the UI (deployment-defined entries always remain).
MEMBER_LIST_KEYS = ("security.admins", "security.allowed_users")


def valid_project(value):
    if not isinstance(value, str) or len(value) > 100:
        return False
    if PROJECT_RE.match(value):
        return True
    return ":" in value and bool(DOMAIN_PROJECT_RE.match(value))


def valid_dataset(value):
    return isinstance(value, str) and bool(DATASET_RE.match(value))


def valid_table(value):
    return isinstance(value, str) and bool(TABLE_RE.match(value))


def valid_column_path(path):
    if not isinstance(path, str) or not path or len(path) > 300:
        return False
    return all(COLUMN_SEGMENT_RE.match(seg) for seg in path.split("."))


def parse_dataset_ref(ref, default_project):
    """Parses 'dataset', 'project.dataset' or 'project:dataset'. Returns (project, dataset) or None."""
    if not isinstance(ref, str):
        return None
    ref = ref.strip()
    if ":" in ref:
        project, _, dataset = ref.rpartition(":")
    elif "." in ref:
        project, _, dataset = ref.rpartition(".")
    else:
        project, dataset = default_project, ref
    if not valid_project(project) or not valid_dataset(dataset):
        return None
    return project, dataset


def valid_webhook(url):
    if url == "":
        return True
    if not isinstance(url, str) or len(url) > 1000 or CONTROL_RE.search(url):
        return False
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme == "https" and (parts.hostname or "").lower() in WEBHOOK_HOSTS
            and parts.username is None and parts.password is None and parts.port in (None, 443))


def valid_base_url(url):
    """Public base URL used to build the OAuth redirect URI: https://host[:port] (no path, query or
    credentials). http is accepted only for localhost / 127.0.0.1 (local development)."""
    if url == "":
        return True
    if not isinstance(url, str) or len(url) > 300 or CONTROL_RE.search(url):
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        return False
    if parts.path not in ("", "/") or not parts.hostname:
        return False
    try:
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")


def valid_logo_url(url):
    if url == "":
        return True
    if not isinstance(url, str) or len(url) > 500 or CONTROL_RE.search(url):
        return False
    if any(ch in url for ch in "\"'<> \\`"):
        return False
    if url.startswith("/"):
        return bool(ASSET_PATH_RE.match(url)) and ".." not in url
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname) and parts.username is None


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

class Invalid(ValueError):
    pass


def _text(max_len, allow_empty=True):
    def check(value):
        if not isinstance(value, str):
            raise Invalid("must be a string")
        value = value.strip()
        if not allow_empty and not value:
            raise Invalid("must not be empty")
        if len(value) > max_len:
            raise Invalid("must be at most %d characters" % max_len)
        if CONTROL_RE.search(value):
            raise Invalid("must not contain control characters")
        return value
    return check


MODEL_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _int(lo, hi):
    def check(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Invalid("must be an integer")
        if isinstance(value, float):
            if not value.is_integer():
                raise Invalid("must be an integer")
            value = int(value)
        if value < lo or value > hi:
            raise Invalid("must be between %d and %d" % (lo, hi))
        return int(value)
    return check


def _float(lo, hi):
    def check(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Invalid("must be a number")
        value = float(value)
        if value != value or value < lo or value > hi:
            raise Invalid("must be between %s and %s" % (lo, hi))
        return value
    return check


def _bool(value):
    if not isinstance(value, bool):
        raise Invalid("must be true or false")
    return value


def _enum(*choices):
    def check(value):
        if value not in choices:
            raise Invalid("must be one of: %s" % ", ".join(choices))
        return value
    return check


def _regex(rx, what, lower=False):
    def check(value):
        if not isinstance(value, str):
            raise Invalid("must be a string")
        value = value.strip()
        if lower:
            value = value.lower()
        if not rx.match(value):
            raise Invalid("must be a valid %s" % what)
        return value
    return check


def _pred(fn, what, lower=False):
    def check(value):
        if not isinstance(value, str):
            raise Invalid("must be a string")
        value = value.strip()
        if lower:
            value = value.lower()
        if not fn(value):
            raise Invalid("must be a valid %s" % what)
        return value
    return check


def _list(item, max_items=100, unique=True):
    def check(value):
        if not isinstance(value, list):
            raise Invalid("must be a list")
        if len(value) > max_items:
            raise Invalid("must contain at most %d items" % max_items)
        out = []
        for idx, element in enumerate(value):
            try:
                norm = item(element)
            except Invalid as err:
                raise Invalid("item %d %s" % (idx + 1, err))
            if not unique or norm not in out:
                out.append(norm)
        return out
    return check


def _project_or_empty(value):
    if value == "":
        return ""
    return _pred(valid_project, "project id", lower=True)(value)


def _admin_entry(value):
    if not isinstance(value, str):
        raise Invalid("must be a string")
    value = value.strip().lower()
    if EMAIL_RE.match(value) or ADMIN_PATTERN_RE.match(value):
        return value
    raise Invalid("must be an email address or a *@domain pattern")


def _allowed_user_entry(value):
    if isinstance(value, str) and value.strip() in ("*", "*@*"):
        return "*"
    return _admin_entry(value)


def _dataset_ref(value):
    if not isinstance(value, str) or parse_dataset_ref(value, "placeholder-project") is None:
        raise Invalid("must be 'dataset', 'project.dataset' or 'project:dataset'")
    return value.strip()


def _timezone(value):
    if not valid_timezone(value):
        raise Invalid("must be a valid IANA timezone name")
    return value


def _sku_pattern(value):
    return _text(60, allow_empty=False)(value).lower()


TIER_RE = re.compile(r"^SUBSCRIPTION_TIER_[A-Z0-9_]{1,60}$")
SCOPE_ID_RE = re.compile(r"^[0-9]{1,30}$")
LABEL_KEY_RE = re.compile(r"^([a-z][a-z0-9_\-]{0,62})?$")
# Deny policy ids: lowercase letters, digits and hyphens, 63 characters at most, always with the control plane prefix.
DENY_POLICY_ID_RE = re.compile(r"^ai-control-plane-[a-z0-9](?:[a-z0-9-]{0,44}[a-z0-9])?$")
ENFORCEMENT_JOB_RE = re.compile(r"^(projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z][a-z0-9-]{1,30}/jobs/"
                                r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?)?$")


def _tier_amount(value):
    """'SUBSCRIPTION_TIER_X=amount' (USD, 0 to 1e6), normalized."""
    if not isinstance(value, str) or value.count("=") != 1:
        raise Invalid("must look like 'SUBSCRIPTION_TIER_NAME=amount'")
    left, right = (part.strip() for part in value.split("="))
    left = left.upper()
    if not TIER_RE.match(left):
        raise Invalid("must start with a subscription tier such as SUBSCRIPTION_TIER_ENTERPRISE")
    try:
        amount = float(right)
    except ValueError:
        raise Invalid("must end with an amount in USD")
    if amount != amount or amount < 0 or amount > 1e6:
        raise Invalid("amount must be between 0 and 1000000")
    return "%s=%s" % (left, format(amount, "g"))


def tier_amounts(values):
    """{tier: amount} from 'TIER=amount' entries; invalid entries are skipped and the first entry of a tier wins."""
    out = {}
    for value in values or ():
        try:
            norm = _tier_amount(value)
        except Invalid:
            continue
        tier, _, amount = norm.partition("=")
        out.setdefault(tier, float(amount))
    return out


def _dataset_ref_or_empty(value):
    if value == "":
        return ""
    return _dataset_ref(value)


MODEL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9.\-_]{0,120}$")
CURRENCY_RE = re.compile(r"^([A-Z]{3})?$")


def _model_prices(value):
    """[{model, input, output, cached?}] unit prices per million tokens; model is an exact id or a prefix.
    cached is the price of prompt tokens served from the prompt cache (omitted = automatic price)."""
    if not isinstance(value, list):
        raise Invalid("must be a list")
    if len(value) > 50:
        raise Invalid("must contain at most 50 items")
    out = []
    seen = set()
    price = _float(0, 1e6)
    for idx, item in enumerate(value):
        if not isinstance(item, dict) or set(item) - {"model", "input", "output", "cached"}:
            raise Invalid("item %d must be an object with model, input, output and optional cached" % (idx + 1))
        model = str(item.get("model") or "").strip().lower()
        if not MODEL_KEY_RE.match(model):
            raise Invalid("item %d has an invalid model id" % (idx + 1))
        if model in seen:
            raise Invalid("model %s is listed twice" % model)
        seen.add(model)
        try:
            entry = {"model": model, "input": price(item.get("input", 0)), "output": price(item.get("output", 0))}
            if item.get("cached") not in (None, ""):
                entry["cached"] = price(item.get("cached"))
            out.append(entry)
        except Invalid as err:
            raise Invalid("item %d price %s" % (idx + 1, err))
    return out


HINT_RE = re.compile(r"^[a-z0-9_.\-]{1,40}$")
TIME_OF_DAY_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
SINK_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_\-]{0,30}$")
BQ_LOCATION_RE = re.compile(r"^([A-Za-z][A-Za-z0-9\-]{1,40})?$")
SEVERITIES = ("", "DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")
# Google documentation hosts accepted for price pages ("official Google pricing documentation only").
OFFICIAL_DOC_SUFFIXES = (".google.com", ".google.dev")


def _hint(value):
    if not isinstance(value, str) or not HINT_RE.match(value.strip().lower()):
        raise Invalid("must be a lowercase word of letters, digits, '_', '-' or '.'")
    return value.strip().lower()


def _pair(lower_left=False, left_rx=None):
    """'Left=Right' text pairs (renames and model aliases)."""
    def check(value):
        if not isinstance(value, str) or value.count("=") != 1:
            raise Invalid("must look like 'from=to'")
        left, right = (part.strip() for part in value.split("="))
        if lower_left:
            left, right = left.lower(), right.lower()
        if not left or not right or len(left) > 120 or len(right) > 120 or CONTROL_RE.search(left + right):
            raise Invalid("must look like 'from=to' with 1 to 120 characters on each side")
        if left_rx is not None and not (left_rx.match(left) and left_rx.match(right)):
            raise Invalid("must contain valid model ids")
        return "%s=%s" % (left, right)
    return check


def _model_overrides(value):
    """[{model, input, output, cached}] USD per million tokens set by an administrator."""
    if not isinstance(value, list):
        raise Invalid("must be a list")
    if len(value) > 100:
        raise Invalid("must contain at most 100 items")
    out, seen = [], set()
    price = _float(0, 1e6)
    for idx, item in enumerate(value):
        if not isinstance(item, dict) or set(item) - {"model", "input", "output", "cached"}:
            raise Invalid("item %d must be an object with model, input, output and cached" % (idx + 1))
        model = str(item.get("model") or "").strip().lower()
        if not MODEL_KEY_RE.match(model) or model in seen:
            raise Invalid("item %d has an invalid or repeated model id" % (idx + 1))
        seen.add(model)
        try:
            row = {"model": model, "input": price(item.get("input", 0)), "output": price(item.get("output", 0))}
            row["cached"] = price(item["cached"]) if item.get("cached") is not None else None
        except Invalid as err:
            raise Invalid("item %d price %s" % (idx + 1, err))
        out.append(row)
    return out


def public_https_host(url):
    """Host of an https URL on a public DNS name (no IP literal, credentials, ports or internal names)."""
    if not isinstance(url, str) or len(url) > 500 or CONTROL_RE.search(url):
        return None
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or "." not in host or parts.username or parts.password:
        return None
    if parts.port not in (None, 443) or re.match(r"^[0-9.]+$", host) or ":" in host:
        return None
    if host == "localhost" or host.endswith((".internal", ".local", ".localhost")):
        return None
    return host


def _official_doc_url(value):
    host = public_https_host(value)
    if not host or not (host.endswith(OFFICIAL_DOC_SUFFIXES) or host in ("google.com", "google.dev")):
        raise Invalid("must be an https URL on an official Google documentation host (*.google.com, *.google.dev)")
    return value.strip()


def _public_url(value):
    if not public_https_host(value):
        raise Invalid("must be an https URL on a public host name")
    return value.strip()


def _time_of_day(value):
    if not isinstance(value, str) or not TIME_OF_DAY_RE.match(value.strip()):
        raise Invalid("must be a time of day such as 00:00")
    return value.strip()


def _timezone_or_empty(value):
    if value == "":
        return ""
    return _timezone(value)


def _currency_or_empty(value):
    """ISO 4217 code (upper-cased) or empty."""
    if not isinstance(value, str) or not CURRENCY_RE.match(value.strip().upper()):
        raise Invalid("must be a valid ISO 4217 currency code (for example KRW) or empty")
    return value.strip().upper()


def _model_or_empty(value):
    if value == "":
        return ""
    return _regex(MODEL_ID_RE, "model id")(value)


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_or_empty(value):
    """A calendar date YYYY-MM-DD from 2000-01-01 to 2100-12-31, or empty."""
    if value == "":
        return ""
    if not isinstance(value, str) or not DATE_RE.match(value.strip()):
        raise Invalid("must be a date as YYYY-MM-DD or empty")
    try:
        day = dt.date.fromisoformat(value.strip())
    except ValueError:
        raise Invalid("must be a valid calendar date")
    if not dt.date(2000, 1, 1) <= day <= dt.date(2100, 12, 31):
        raise Invalid("must be between 2000-01-01 and 2100-12-31")
    return day.isoformat()


# Pub/Sub subscription names: projects/PROJECT/subscriptions/ID (ID: 3-255 characters, starts with a letter; Pub/Sub
# also allows ~ + %, which deployments never use).
SUBSCRIPTION_RE = re.compile(r"^(projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/subscriptions/[A-Za-z][A-Za-z0-9_.-]{2,254})?$")

VALIDATORS = {
    "product.name": _text(60, allow_empty=False),
    "product.tagline": _text(140),
    "branding.organization_name": _text(120, allow_empty=False),
    "branding.logo_url": _pred(valid_logo_url, "https URL or /assets/ path"),
    "branding.accent_color": _regex(COLOR_RE, "hex color (#RRGGBB)"),
    "branding.default_locale": _enum("en", "ko"),
    "branding.default_theme": _enum("system", "light", "dark"),
    "server.host": _regex(re.compile(r"^[A-Za-z0-9.:\-]{1,253}$"), "host"),
    "server.port": _int(1, 65535),
    "server.cache_ttl_seconds": _int(0, 86400),
    "workspace.project_id": _pred(valid_project, "project id", lower=True),
    "workspace.projects": _list(_pred(valid_project, "project id", lower=True), 500),
    "workspace.scope_type": _enum("projects", "folder", "organization"),
    "workspace.scope_ids": _list(_regex(SCOPE_ID_RE, "numeric folder or organization id"), 20),
    "workspace.scope_max_projects": _int(1, 5000),
    "workspace.scope_filter": _enum("gemini_enterprise", "none"),
    "workspace.audit_project": _project_or_empty,
    "workspace.license_max_pages": _int(1, 1000),
    "workspace.scan_all_projects": _bool,
    "workspace.quota_project_id": _project_or_empty,
    "workspace.timezone": _timezone,
    "workspace.default_range_days": _int(1, 400),
    "workspace.audit_dataset": _pred(valid_dataset, "dataset id"),
    "workspace.billing.project_id": _project_or_empty,
    "workspace.billing.dataset": _pred(valid_dataset, "dataset id"),
    "workspace.billing.table": _pred(valid_table, "table id"),
    "workspace.billing.account_id": _regex(re.compile(r"^([0-9A-Fa-f]{6}-[0-9A-Fa-f]{6}-[0-9A-Fa-f]{6})?$"),
                                           "billing account id (XXXXXX-XXXXXX-XXXXXX)"),
    "workspace.discovery_engine_locations": _list(_regex(LOCATION_RE, "location"), 10),
    "workspace.agent_engine_regions": _list(_regex(LOCATION_RE, "region"), 20),
    "workspace.telemetry_datasets": _list(_dataset_ref, 20),
    "security.auth_mode": _enum("auto", "iap", "google", "local"),
    "security.iap_audience": _text(300),
    "security.admins": _list(_admin_entry, 200),
    "security.allowed_users": _list(_allowed_user_entry, 1000),
    "security.google_client_id": _regex(GOOGLE_CLIENT_ID_RE, "OAuth client id (*.apps.googleusercontent.com)"),
    "security.public_base_url": _pred(lambda v: valid_base_url(v), "https base URL (or http://localhost)"),
    "security.session_hours": _int(1, 168),
    "security.sign_in_bridge_url": _pred(lambda v: valid_base_url(v), "https base URL (or http://localhost)"),
    "security.bridge_return_url": _pred(lambda v: valid_base_url(v), "https base URL (or http://localhost)"),
    "security.mask_pii_for_viewers": _bool,
    "security.guest_access": _bool,
    "security.guest_redact_terms": _list(_text(100, allow_empty=False), 300),
    "storage.backend": _enum("auto", "file", "gcs", "memory"),
    "storage.file_path": _text(300, allow_empty=False),
    "storage.gcs_bucket": _regex(re.compile(r"^([a-z0-9][a-z0-9_.\-]{1,220}[a-z0-9])?$"), "bucket name"),
    "storage.gcs_object": _regex(re.compile(r"^[A-Za-z0-9_.\-/]{1,512}$"), "object name"),
    "bigquery.max_bytes_billed": _int(10000000, 10 ** 13),
    "ai_engine.location": _regex(LOCATION_RE, "location"),
    "ai_engine.default_model": _regex(MODEL_ID_RE, "model id"),
    "ai_engine.models": _list(_regex(MODEL_ID_RE, "model id"), 30),
    "ai_engine.discover_models": _bool,
    "ai_engine.restrict_to_allowed": _bool,
    "ai_engine.fallback_models": _list(_regex(MODEL_ID_RE, "model id"), 10),
    "model_catalog.auto_enable_new_models": _bool,
    "model_catalog.disabled_models": _list(_regex(re.compile(r"^[a-z0-9][a-z0-9.\-_]{0,120}$"), "model key"), 500),
    "model_catalog.excluded_sku_patterns": _list(_sku_pattern, 50),
    "model_catalog.ai_grouping": _bool,
    "gateway.services": _list(_regex(SERVICE_RE, "service name (*.googleapis.com)", lower=True), 50),
    "agent_lifecycle.idle_days": _int(1, 365),
    "agent_lifecycle.deprecation_days": _int(1, 730),
    "finops.spike_threshold_pct": _float(1, 100000),
    "finops.spike_min_delta": _float(0, 1e12),
    "governance.antigravity.usage_source": _enum("auto", "bigquery", "logging"),
    "governance.antigravity.inference_log": _regex(LOG_NAME_RE, "log id"),
    "governance.antigravity.access_role": _regex(ROLE_RE, "role name"),
    "governance.antigravity.cap_marker_role": _regex(ROLE_ID_RE, "custom role id"),
    "governance.antigravity.admin_marker_role": _regex(ROLE_ID_RE, "custom role id"),
    "governance.antigravity.legacy_marker_roles": _list(_regex(ROLE_ID_RE, "custom role id"), 10),
    "governance.antigravity.privileged_roles": _list(_regex(ROLE_RE, "role name"), 50),
    "governance.antigravity.extra_access_roles": _list(_regex(ROLE_RE, "role name"), 50),
    "governance.antigravity.period": _enum("daily", "weekly", "monthly", "custom", "all"),
    "governance.antigravity.window": _enum("calendar", "rolling"),
    "governance.antigravity.token_cap": _int(0, 10 ** 12),
    "governance.antigravity.request_cap": _int(0, 10 ** 9),
    "governance.antigravity.change_budget_min": _int(0, 100000),
    "governance.antigravity.change_budget_ratio": _float(0, 1),
    "governance.antigravity.max_log_entries": _int(100, 200000),
    "governance.antigravity.audit_log_name": _regex(LOG_NAME_RE, "log id"),
    "governance.antigravity.webhook_url": _pred(valid_webhook, "https webhook URL on an allowlisted host"),
    "governance.antigravity.enforcement_mode": _enum("manual", "auto"),
    "governance.antigravity.cost_cap": _float(0, 1e9),
    "governance.antigravity.currency": _currency_or_empty,
    "governance.antigravity.pricing_source": _enum("auto", "manual"),
    "governance.antigravity.input_price_per_million": _float(0, 1e6),
    "governance.antigravity.output_price_per_million": _float(0, 1e6),
    "governance.antigravity.model_prices": _model_prices,
    "governance.antigravity.reset_timezone": _timezone_or_empty,
    "governance.antigravity.reset_weekday": _int(0, 6),
    "governance.antigravity.reset_time": _pred(lambda v: bool(TIME_OF_DAY_RE.match(str(v or ""))), "time as HH:MM"),
    "governance.antigravity.reset_day": _int(1, 31),
    "governance.antigravity.cycle_days": _int(1, 92),
    "governance.antigravity.cycle_start": _date_or_empty,
    "governance.antigravity.credit_per_user_month": _float(0, 1e6),
    "governance.antigravity.cap_model": _enum("", "credit_budget", "period"),
    "governance.antigravity.paid_budget_month": _float(-1, 1e9),
    "governance.antigravity.org_paid_budget_month": _float(-1, 1e9),
    "governance.antigravity.credit_model_prefixes": _list(_regex(MODEL_PREFIX_RE, "model id prefix", lower=True), 20),
    "governance.antigravity.credit_source": _enum("license", "fixed"),
    "governance.antigravity.credit_by_tier": _list(_tier_amount, 30),
    "chargeback.allocation_mode": _enum("credit_share", "list"),
    "chargeback.project_unit_label": _regex(LABEL_KEY_RE, "project label key (lowercase letters, digits, '_' or '-')"),
    "chargeback.seat_prices": _list(_tier_amount, 30),
    "chargeback.close_dataset": _dataset_ref_or_empty,
    "governance.antigravity.scheduler_invokers": _list(_regex(EMAIL_RE, "service account email", lower=True), 10),
    "governance.antigravity.enforcement_backend": _enum("iam_allow_swap", "iam_deny", "notify_only"),
    "governance.antigravity.deny_attachment": _enum("projects", "scope"),
    "governance.antigravity.deny_policy_id": _regex(DENY_POLICY_ID_RE, "deny policy id starting with "
                                                                      "'ai-control-plane-'"),
    "governance.antigravity.deny_ge_app": _bool,
    "governance.antigravity.deny_agent_platform": _bool,
    "governance.antigravity.enforcement_job": _regex(ENFORCEMENT_JOB_RE, "Cloud Run job name "
                                                                         "(projects/PROJECT/locations/REGION/jobs/JOB)"),
    "governance.antigravity.usage_events_subscription": _regex(SUBSCRIPTION_RE, "Pub/Sub subscription name "
                                                                                "(projects/PROJECT/subscriptions/ID)"),
    "governance.antigravity.surge_guard_min": _int(1, 100000),
    "governance.antigravity.surge_guard_ratio": _float(0.01, 1.0),
    "governance.antigravity.spend_caps": _bool,
    "analyst.max_bytes_billed": _int(10000000, 10 ** 13),
    "analyst.max_rows": _int(1, 5000),
    "analyst.allow_viewers": _bool,
    # Telemetry discovery, Gemini profiling and field inference
    "telemetry.discovery.max_projects": _int(1, 50),
    "telemetry.discovery.max_datasets_per_project": _int(0, 200),
    "telemetry.discovery.max_tables_per_dataset": _int(1, 1000),
    "telemetry.discovery.activity_days": _int(1, 30),
    "telemetry.discovery.sample_entries": _int(0, 1000),
    "telemetry.discovery.targeted_samples": _int(0, 50),
    "telemetry.discovery.max_log_entries": _int(100, 50000),
    "telemetry.discovery.dataset_hints": _list(_hint, 30),
    "telemetry.discovery.hidden_logs": _list(_text(200, allow_empty=False), 100),
    "telemetry.ai.enabled": _bool,
    "telemetry.ai.model": _model_or_empty,
    "telemetry.ai.batch_size": _int(5, 60),
    "telemetry.ai.max_sources": _int(0, 500),
    "telemetry.ai.profile_ttl_hours": _int(1, 24 * 90),
    "telemetry.ai.share_value_examples": _bool,
    "telemetry.inference.time_hints": _list(_hint, 50),
    "telemetry.inference.user_hints": _list(_hint, 50),
    "telemetry.inference.model_hints": _list(_hint, 50),
    "telemetry.inference.token_hints": _list(_hint, 50),
    "telemetry.inference.operation_hints": _list(_hint, 50),
    "telemetry.inference.status_hints": _list(_hint, 50),
    "telemetry.inference.sensitive_hints": _list(_hint, 50),
    "telemetry.display_renames": _list(_pair(), 30),
    # Log Router -> BigQuery routing cost controls
    "telemetry.routing.dataset": _pred(valid_dataset, "dataset id"),
    "telemetry.routing.dataset_location": _regex(BQ_LOCATION_RE, "BigQuery location (for example asia-northeast3) or empty"),
    "telemetry.routing.sink_prefix": _regex(SINK_PREFIX_RE, "sink name prefix (lowercase letters, digits, '-' or '_')"),
    "telemetry.routing.sample_percent": _float(0.01, 100),
    "telemetry.routing.min_severity": _enum(*SEVERITIES),
    "telemetry.routing.exclusion_filters": _list(_text(2000, allow_empty=False), 20),
    "telemetry.routing.retention_days": _int(0, 3650),
    "telemetry.routing.daily_budget_gb": _float(0, 1e6),
    "telemetry.routing.sink_daily_gb": _float(0, 1e6),
    "telemetry.routing.over_budget_action": _enum("pause", "alert"),
    "telemetry.routing.auto_resume": _bool,
    "telemetry.routing.max_sinks": _int(1, 200),
    "telemetry.routing.follow_groups": _bool,
    "telemetry.routing.notify_webhook_url": _pred(valid_webhook, "https webhook URL on an allowlisted host"),
    # Token prices and exchange rates (official sources only)
    "pricing.currency": _currency_or_empty,
    "pricing.sync_time": _time_of_day,
    "pricing.sync_timezone": _timezone_or_empty,
    "pricing.public_sources": _list(_official_doc_url, 10),
    "pricing.fx_sources": _list(_public_url, 5),
    "pricing.catalog_services": _list(_text(80, allow_empty=False), 20),
    "pricing.include_partner_models": _bool,
    "pricing.storage_service": _text(80),
    "pricing.model_aliases": _list(_pair(lower_left=True, left_rx=MODEL_KEY_RE), 100),
    "pricing.model_overrides": _model_overrides,
    "pricing.alert_price_change_pct": _float(0, 1000),
    "pricing.alert_fx_change_pct": _float(0, 1000),
}
for _module in MODULE_IDS:
    VALIDATORS["modules.%s" % _module] = _bool

# Keys that exist in the schema but are never accepted from the UI.
UI_LOCKED_KEYS = {"security.auth_mode", "security.iap_audience", "security.google_client_id",
                  "security.public_base_url", "security.sign_in_bridge_url", "security.bridge_return_url",
                  "security.guest_access", "security.guest_redact_terms",
                  "governance.antigravity.scheduler_invokers", "governance.antigravity.enforcement_job",
                  "governance.antigravity.deny_policy_id", "governance.antigravity.usage_events_subscription"}
INTERNAL_KEYS = {"model_catalog.known_models", "telemetry.connectors"}

EDITABLE_SECTIONS = ("product", "branding", "workspace", "security", "modules", "model_catalog",
                     "ai_engine", "agent_lifecycle", "finops", "governance", "analyst", "gateway",
                     "telemetry", "pricing", "chargeback")


def section_prefix(section):
    return "governance.antigravity" if section == "governance" else section


# ---------------------------------------------------------------------------
# Dict helpers
# ---------------------------------------------------------------------------

def deep_merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def get_path(obj, dotted, default=None):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(obj, dotted, value):
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def del_path(obj, dotted):
    parts = dotted.split(".")
    stack = []
    cur = obj
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        stack.append((cur, part))
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)
    # prune empty parents
    for parent, key in reversed(stack):
        if isinstance(parent.get(key), dict) and not parent[key]:
            parent.pop(key)


def flatten(obj, prefix=""):
    """Flattens nested config into dotted keys, recursing only into schema sections."""
    out = {}
    for key, value in (obj or {}).items():
        dotted = "%s.%s" % (prefix, key) if prefix else str(key)
        if isinstance(value, dict) and dotted in _DICT_KEYS:
            out.update(flatten(value, dotted))
        else:
            out[dotted] = value
    return out


def _collect_dict_keys(obj, prefix=""):
    keys = set()
    for key, value in obj.items():
        dotted = "%s.%s" % (prefix, key) if prefix else key
        if isinstance(value, dict):
            keys.add(dotted)
            keys |= _collect_dict_keys(value, dotted)
    return keys


_DICT_KEYS = _collect_dict_keys(DEFAULTS)


# ---------------------------------------------------------------------------
# YAML + env loading
# ---------------------------------------------------------------------------

def repo_root():
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def config_paths(environ=None):
    environ = os.environ if environ is None else environ
    explicit = (environ.get("CONFIG_PATH") or os.environ.get("CONFIG_PATH") or "").strip()
    main = os.path.abspath(explicit) if explicit else os.path.join(repo_root(), "config.yaml")
    local = os.path.join(os.path.dirname(main), "config.local.yaml")
    return main, local


def _read_yaml(path):
    if not os.path.isfile(path):
        return None
    import yaml  # PyYAML; imported lazily so that importing this module stays cheap.
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("top-level YAML value must be a mapping")
    return data


def validate_layer(layer, source, strict_security=True):
    """Validates a nested config layer. Unknown keys and invalid values are dropped with a
    warning (fail-safe startup); an invalid security.auth_mode aborts startup."""
    clean = {}
    flat = flatten(layer)
    for dotted, value in flat.items():
        if dotted == "telemetry.connectors":
            if isinstance(value, list):
                set_path(clean, dotted, [c for c in value if isinstance(c, dict)])
            else:
                LOG.warning("ignoring invalid telemetry.connectors in %s", source)
            continue
        if dotted == "model_catalog.known_models":
            if isinstance(value, list):
                set_path(clean, dotted, value)
            continue
        validator = VALIDATORS.get(dotted)
        if validator is None:
            LOG.warning("ignoring unknown configuration key %s in %s", dotted, source)
            continue
        try:
            set_path(clean, dotted, validator(value))
        except Invalid as err:
            if dotted == "security.auth_mode" and strict_security:
                raise ValueError("invalid security.auth_mode in %s: %s" % (source, err))
            LOG.warning("ignoring invalid configuration value for %s in %s: %s", dotted, source, err)
    return clean


ENV_MAP = {
    "PROJECT_ID": "workspace.project_id",
    "QUOTA_PROJECT_ID": "workspace.quota_project_id",
    "AUDIT_DATASET_ID": "workspace.audit_dataset",
    "AUDIT_PROJECT": "workspace.audit_project",
    "SCOPE_TYPE": "workspace.scope_type",
    "ENFORCEMENT_BACKEND": "governance.antigravity.enforcement_backend",
    "ENFORCEMENT_JOB": "governance.antigravity.enforcement_job",
    "USAGE_EVENTS_SUBSCRIPTION": "governance.antigravity.usage_events_subscription",
    "DENY_ATTACHMENT": "governance.antigravity.deny_attachment",
    "BILLING_PROJECT_ID": "workspace.billing.project_id",
    "BILLING_DATASET_ID": "workspace.billing.dataset",
    "BILLING_TABLE_ID": "workspace.billing.table",
    "BILLING_ACCOUNT_ID": "workspace.billing.account_id",
    "WORKSPACE_TIMEZONE": "workspace.timezone",
    "ORGANIZATION_NAME": "branding.organization_name",
    "PRODUCT_NAME": "product.name",
    "DEFAULT_LOCALE": "branding.default_locale",
    "AUTH_MODE": "security.auth_mode",
    "IAP_AUDIENCE": "security.iap_audience",
    "GOOGLE_OAUTH_CLIENT_ID": "security.google_client_id",
    "PUBLIC_BASE_URL": "security.public_base_url",
    "SESSION_HOURS": "security.session_hours",
    "SIGN_IN_BRIDGE_URL": "security.sign_in_bridge_url",
    "BRIDGE_RETURN_URL": "security.bridge_return_url",
    "SETTINGS_BUCKET": "storage.gcs_bucket",
    "AI_DEFAULT_MODEL": "ai_engine.default_model",
    "AI_LOCATION": "ai_engine.location",
    "PRICING_CURRENCY": "pricing.currency",
    "PRICING_SYNC_TIME": "pricing.sync_time",
    "TELEMETRY_AI_MODEL": "telemetry.ai.model",
    "TELEMETRY_DATASET": "telemetry.routing.dataset",
    "TELEMETRY_DATASET_LOCATION": "telemetry.routing.dataset_location",
    "HOST": "server.host",
    "PORT": "server.port",
}
# Comma-separated list values.
LIST_ENV_MAP = {
    "SCAN_PROJECTS": "workspace.projects",
    "SCOPE_IDS": "workspace.scope_ids",
    "DISCOVERY_ENGINE_LOCATIONS": "workspace.discovery_engine_locations",
    "AGENT_ENGINE_REGIONS": "workspace.agent_engine_regions",
    "SCHEDULER_INVOKERS": "governance.antigravity.scheduler_invokers",
    "GUEST_REDACT_TERMS": "security.guest_redact_terms",
}
# Boolean values: only explicit true/false spellings are accepted.
BOOL_ENV_MAP = {
    "SCAN_ALL_PROJECTS": "workspace.scan_all_projects",
    "GUEST_ACCESS": "security.guest_access",
}
_TRUE, _FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


def env_layer(environ=None):
    environ = os.environ if environ is None else environ
    layer = {}
    for env_name, dotted in ENV_MAP.items():
        raw = environ.get(env_name)
        if raw is None or raw.strip() == "":
            continue
        value = raw.strip()
        if dotted in ("server.port", "security.session_hours"):
            try:
                value = int(value)
            except ValueError:
                LOG.warning("ignoring non-numeric %s environment variable", env_name)
                continue
        set_path(layer, dotted, value)
    for env_name, dotted in LIST_ENV_MAP.items():
        raw = environ.get(env_name, "")
        if raw.strip():
            set_path(layer, dotted, [v.strip() for v in raw.split(",") if v.strip()])
    for env_name, dotted in BOOL_ENV_MAP.items():
        raw = (environ.get(env_name) or "").strip().lower()
        if raw in _TRUE or raw in _FALSE:
            set_path(layer, dotted, raw in _TRUE)
        elif raw:
            LOG.warning("ignoring %s: use true or false", env_name)
    # Modules switched off at deploy time (for example finops without a billing export). The UI layer can
    # switch them back on.
    for module_id in (m.strip().lower() for m in environ.get("DISABLED_MODULES", "").split(",")):
        if module_id in MODULE_IDS:
            set_path(layer, "modules.%s" % module_id, False)
        elif module_id:
            LOG.warning("ignoring unknown module %r in DISABLED_MODULES", module_id)
    admins = environ.get("ADMIN_EMAILS", "")
    if admins.strip():
        set_path(layer, "security.admins", [a.strip() for a in admins.split(",") if a.strip()])
    allowed = environ.get("ALLOWED_USERS", "")
    if allowed.strip():
        set_path(layer, "security.allowed_users", [a.strip() for a in allowed.split(",") if a.strip()])
    return validate_layer(layer, "environment")


def merge_admins(*lists):
    out = []
    for items in lists:
        for item in items or []:
            norm = str(item).strip().lower()
            if norm and norm not in out:
                out.append(norm)
    return out


def load_base_settings(environ=None):
    """defaults < config.yaml < config.local.yaml < env. Member lists (security.admins and
    security.allowed_users) are unioned across layers."""
    main, local = config_paths(environ)
    settings = copy.deepcopy(DEFAULTS)
    member_lists = {dotted: [] for dotted in MEMBER_LIST_KEYS}
    loaded = []
    for path in (main, local):
        try:
            raw = _read_yaml(path)
        except Exception as err:  # pylint: disable=broad-except
            if path == local:
                LOG.warning("could not read %s: %s", os.path.basename(path), type(err).__name__)
                continue
            raise ValueError("could not read %s: %s" % (path, err))
        if raw is None:
            continue
        layer = validate_layer(raw, os.path.basename(path))
        for dotted, lists in member_lists.items():
            lists.append(get_path(layer, dotted, []))
        settings = deep_merge(settings, layer)
        loaded.append(os.path.basename(path))
    env = env_layer(environ)
    for dotted, lists in member_lists.items():
        lists.append(get_path(env, dotted, []))
    settings = deep_merge(settings, env)
    for dotted, lists in member_lists.items():
        set_path(settings, dotted, merge_admins(*lists))
    return settings, loaded


def finalize(settings):
    """Applies invariants on an effective settings dict (in place) and returns it."""
    cat = settings.setdefault("model_catalog", {})
    cat["excluded_sku_patterns"] = merge_admins(MANDATORY_EXCLUDED_SKU_PATTERNS,
                                                cat.get("excluded_sku_patterns"))
    ai = settings.setdefault("ai_engine", {})
    models = list(ai.get("models") or [])
    if ai.get("default_model") and ai["default_model"] not in models:
        models.insert(0, ai["default_model"])
    ai["models"] = models
    return settings


def workspace_configured(settings):
    project = get_path(settings, "workspace.project_id", "") or ""
    return bool(project) and not project.startswith("your-")


def billing_configured(settings):
    table = get_path(settings, "workspace.billing.table", "") or ""
    dataset = get_path(settings, "workspace.billing.dataset", "") or ""
    return bool(table) and bool(dataset) and "XXXXXX" not in table and workspace_configured(settings)


def billing_project(settings):
    return get_path(settings, "workspace.billing.project_id") or get_path(settings, "workspace.project_id")


def quota_project(settings):
    return get_path(settings, "workspace.quota_project_id") or get_path(settings, "workspace.project_id")


# ---------------------------------------------------------------------------
# UI updates (POST /api/v1/settings)
# ---------------------------------------------------------------------------

def _flatten_values(values, prefix):
    out = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ApiError("bad_request", "Invalid settings key.")
        dotted = "%s.%s" % (prefix, key)
        if isinstance(value, dict) and dotted in _DICT_KEYS:
            out.update(_flatten_values(value, dotted))
        else:
            out[dotted] = value
    return out


def validate_section_update(section, values, effective, base):
    """Validates a settings POST. Returns {dotted_key: normalized_value}.

    Raises ApiError(bad_request) on unknown sections/keys or invalid values. UI-locked keys
    (auth mode, IAP audience) are accepted only if unchanged."""
    if section not in EDITABLE_SECTIONS:
        raise ApiError("bad_request", "Unknown or read-only settings section.")
    if not isinstance(values, dict) or not values:
        raise ApiError("bad_request", "Field 'values' must be a non-empty object.")
    prefix = section_prefix(section)
    flat = _flatten_values(values, prefix)
    out = {}
    for dotted, value in flat.items():
        if dotted in INTERNAL_KEYS or dotted not in VALIDATORS:
            raise ApiError("bad_request", "Unknown settings key: %s" % dotted[len(prefix) + 1:])
        try:
            norm = VALIDATORS[dotted](value)
        except Invalid as err:
            raise ApiError("bad_request", "Invalid value for %s: %s." % (dotted[len(prefix) + 1:], err))
        if dotted in UI_LOCKED_KEYS:
            if norm != get_path(effective, dotted):
                raise ApiError("bad_request", "%s can only be changed in config.yaml or the environment."
                               % dotted)
            continue
        if dotted in MEMBER_LIST_KEYS:
            base_members = get_path(base, dotted, []) or []
            norm = [a for a in norm if a not in base_members]
        out[dotted] = norm
    # Cross-field checks against the would-be effective configuration.
    merged = copy.deepcopy(effective)
    for dotted, value in out.items():
        set_path(merged, dotted, value)
    idle = get_path(merged, "agent_lifecycle.idle_days")
    dep = get_path(merged, "agent_lifecycle.deprecation_days")
    if section == "agent_lifecycle" and idle is not None and dep is not None and dep <= idle:
        raise ApiError("bad_request", "deprecation_days must be greater than idle_days.")
    if section == "ai_engine":
        models = get_path(merged, "ai_engine.models") or []
        default = get_path(merged, "ai_engine.default_model")
        if get_path(merged, "ai_engine.restrict_to_allowed") and default not in models:
            raise ApiError("bad_request", "default_model must be one of the configured models.")
    if section == "workspace" and "workspace.billing.table" in out:
        if not valid_table(out["workspace.billing.table"]):
            raise ApiError("bad_request", "Invalid billing table id.")
    return out


def reset_keys(section, keys):
    """Returns the dotted override keys to remove for POST /api/v1/settings/reset."""
    if section not in EDITABLE_SECTIONS:
        raise ApiError("bad_request", "Unknown or read-only settings section.")
    prefix = section_prefix(section)
    if keys is None or keys == []:
        return [prefix]
    if not isinstance(keys, list) or len(keys) > 100:
        raise ApiError("bad_request", "Field 'keys' must be a list of setting names.")
    out = []
    for key in keys:
        if not isinstance(key, str):
            raise ApiError("bad_request", "Field 'keys' must contain strings.")
        dotted = "%s.%s" % (prefix, key)
        if dotted not in VALIDATORS and dotted not in _DICT_KEYS:
            raise ApiError("bad_request", "Unknown settings key: %s" % key)
        if dotted in UI_LOCKED_KEYS or dotted in INTERNAL_KEYS:
            raise ApiError("bad_request", "Setting %s cannot be reset from the UI." % key)
        out.append(dotted)
    return out


# Deployment identifiers that viewers do not need: shown as *** in the viewer's read-only settings view.
VIEWER_HIDDEN_KEYS = ("storage.gcs_bucket", "security.google_client_id", "security.iap_audience",
                      "workspace.billing.account_id", "security.guest_redact_terms")


def mask_settings_for_viewer(effective):
    """Returns the read-only settings view for viewers: e-mail addresses masked (approved accounts and the
    scheduler service accounts), the webhook URL reduced to its host, and deployment identifiers
    (VIEWER_HIDDEN_KEYS) replaced by ***. Policy values such as caps, periods and prices stay visible."""
    from .security import mask_email  # local import to avoid a cycle
    out = copy.deepcopy(effective)
    for dotted_hook in ("governance.antigravity.webhook_url", "telemetry.routing.notify_webhook_url"):
        hook = get_path(out, dotted_hook) or ""
        if hook:
            host = urllib.parse.urlsplit(hook).hostname or "webhook"
            set_path(out, dotted_hook, "https://%s/***" % host)
    for dotted in MEMBER_LIST_KEYS:
        members = get_path(out, dotted)
        if members:  # absent keys stay absent (this also masks partial override documents)
            set_path(out, dotted, [a if a.startswith("*@") else mask_email(a) for a in members])
    invokers = get_path(out, "governance.antigravity.scheduler_invokers") or []
    if invokers:
        # mask_email keeps service accounts unchanged, so mask their local part here.
        set_path(out, "governance.antigravity.scheduler_invokers",
                 ["%s***@%s" % (str(a).partition("@")[0][:2], str(a).rpartition("@")[2]) for a in invokers])
    for dotted in VIEWER_HIDDEN_KEYS:
        if get_path(out, dotted):
            set_path(out, dotted, "***")
    return out
