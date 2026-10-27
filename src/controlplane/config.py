"""Configuration schema, layered loading and validation.

Load order (later wins): built-in defaults < config.yaml < config.local.yaml <
environment overrides < persisted UI overrides (settings store, applied by
settings_store.SettingsManager).
"""

from __future__ import annotations

import copy
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
    "finops", "telemetry", "analyst",
)

# Mandatory exclusions: these patterns are always applied, even if removed from settings.
MANDATORY_EXCLUDED_SKU_PATTERNS = ("code assist", "duet ai")

DEFAULTS = {
    "product": {
        "name": "AI Control Plane",
        "tagline": "Unified governance for Gemini Enterprise and Agent Platform",
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
        "models": ["gemini-3.8-flash", "gemini-3.8-pro", "gemini-4-argon"],
        "discover_models": True,
        "restrict_to_allowed": True,
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
            "period": "daily",
            "window": "calendar",
            "token_cap": 500000,
            "request_cap": 1000,
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
            # Time zone of calendar period boundaries (daily / weekly / monthly settlement resets).
            "reset_timezone": "UTC",
            # Service accounts allowed to call the enforcement endpoint with a Google OIDC token
            # (Cloud Scheduler). Configured in config.yaml or SCHEDULER_INVOKERS only.
            "scheduler_invokers": [],
        },
    },
    "telemetry": {
        "connectors": [],
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


MODEL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9.\-_]{0,120}$")
CURRENCY_RE = re.compile(r"^([A-Z]{3})?$")


def _model_prices(value):
    """[{model, input, output}] unit prices per million tokens; model is an exact id or a prefix."""
    if not isinstance(value, list):
        raise Invalid("must be a list")
    if len(value) > 50:
        raise Invalid("must contain at most 50 items")
    out = []
    seen = set()
    price = _float(0, 1e6)
    for idx, item in enumerate(value):
        if not isinstance(item, dict) or set(item) - {"model", "input", "output"}:
            raise Invalid("item %d must be an object with model, input and output" % (idx + 1))
        model = str(item.get("model") or "").strip().lower()
        if not MODEL_KEY_RE.match(model):
            raise Invalid("item %d has an invalid model id" % (idx + 1))
        if model in seen:
            raise Invalid("model %s is listed twice" % model)
        seen.add(model)
        try:
            out.append({"model": model, "input": price(item.get("input", 0)), "output": price(item.get("output", 0))})
        except Invalid as err:
            raise Invalid("item %d price %s" % (idx + 1, err))
    return out


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
    "governance.antigravity.period": _enum("daily", "weekly", "monthly", "all"),
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
    "governance.antigravity.currency": _regex(CURRENCY_RE, "ISO 4217 currency code (for example KRW) or empty"),
    "governance.antigravity.pricing_source": _enum("auto", "manual"),
    "governance.antigravity.input_price_per_million": _float(0, 1e6),
    "governance.antigravity.output_price_per_million": _float(0, 1e6),
    "governance.antigravity.model_prices": _model_prices,
    "governance.antigravity.reset_timezone": _timezone,
    "governance.antigravity.scheduler_invokers": _list(_regex(EMAIL_RE, "service account email", lower=True), 10),
    "analyst.max_bytes_billed": _int(10000000, 10 ** 13),
    "analyst.max_rows": _int(1, 5000),
    "analyst.allow_viewers": _bool,
}
for _module in MODULE_IDS:
    VALIDATORS["modules.%s" % _module] = _bool

# Keys that exist in the schema but are never accepted from the UI.
UI_LOCKED_KEYS = {"security.auth_mode", "security.iap_audience", "security.google_client_id",
                  "security.public_base_url", "security.sign_in_bridge_url", "security.bridge_return_url",
                  "security.guest_access", "security.guest_redact_terms",
                  "governance.antigravity.scheduler_invokers"}
INTERNAL_KEYS = {"model_catalog.known_models", "telemetry.connectors"}

EDITABLE_SECTIONS = ("product", "branding", "workspace", "security", "modules", "model_catalog",
                     "ai_engine", "agent_lifecycle", "finops", "governance", "analyst", "gateway")


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
    "HOST": "server.host",
    "PORT": "server.port",
}
# Comma-separated list values.
LIST_ENV_MAP = {
    "DISCOVERY_ENGINE_LOCATIONS": "workspace.discovery_engine_locations",
    "AGENT_ENGINE_REGIONS": "workspace.agent_engine_regions",
    "SCHEDULER_INVOKERS": "governance.antigravity.scheduler_invokers",
    "GUEST_REDACT_TERMS": "security.guest_redact_terms",
}
# Boolean values: only explicit true/false spellings are accepted.
BOOL_ENV_MAP = {
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
    hook = get_path(out, "governance.antigravity.webhook_url") or ""
    if hook:
        host = urllib.parse.urlsplit(hook).hostname or "webhook"
        set_path(out, "governance.antigravity.webhook_url", "https://%s/***" % host)
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
