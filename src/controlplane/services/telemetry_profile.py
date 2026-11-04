"""Dynamic description of telemetry sources (there is no built-in catalog).

Every Cloud Logging log and BigQuery table that discovery finds is described from what the project itself
reports, never from a hand-written list of known logs:

- the product name is the title of the API that writes the log, read from the Service Usage API, with the brand
  renames of telemetry.display_renames applied;
- the monitored resources are the display names and descriptions of the resource types seen in the sampled
  entries, read from the Cloud Logging monitored resource descriptors;
- the dashboard fields (time, user, model, tokens, operation, status) are inferred from sampled values (email
  ratio, cardinality, numeric types) combined with the generic name hints of telemetry.inference;
- the category follows the data: Cloud Audit Logs payloads are security, logs of the configured AI services
  (gateway.services), GenAI semantic-convention fields or model and token fields are AI, entries of platform
  resources are operations, everything else is other.

Gemini then refines titles, descriptions, groups and mappings (services/telemetry.py) and administrators can
override any card. Source ids are the sanitized log id, which is also the table name a Log Router sink creates in
BigQuery (run.googleapis.com/requests -> run_googleapis_com_requests), so a log and its exported table share one id.
"""

from __future__ import annotations

import re
import urllib.parse

from .. import config
from ..schema import SEGMENT_RE, build_expr

AI, SECURITY, OPERATIONS, OTHER = "ai", "security", "operations", "other"
CATEGORIES = (AI, SECURITY, OPERATIONS, OTHER)
FIELDS = ("operation", "status", "user", "model", "tokens")
LABELED = ("operation", "status", "tokens")
# UI labels of mapped fields whose meaning is not known more precisely.
GENERIC_LABELS = {"operation": "Operation", "status": "Status", "tokens": "Tokens"}
LOG_NAME_RE = re.compile(r"^projects/[a-z0-9][a-z0-9.:\-]{0,98}/logs/[A-Za-z0-9_.%\-]{1,512}$")
FILTER_RE = re.compile(r'^\s*logName\s*=\s*"(projects/[^/"]+/logs/[^"]+)"\s*$')
INDEX_RE = re.compile(r"\[\d{1,3}\]")
EMAIL_VALUE_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
ID_VALUE_RE = re.compile(r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}|[0-9]{6,}|"
                         r"[A-Za-z0-9_\-]{24,})$", re.I)
TIMESTAMP_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+$")
LOG_ROOTS = ("labels", "httpRequest", "jsonPayload", "protoPayload", "operation", "sourceLocation")
MAX_DISTINCT = 60
MAX_FIELDS = 160
DIRECTORY_TTL = 24 * 3600


def sanitize_log_name(short):
    """BigQuery table name of a log id (the Log Router naming rule)."""
    return re.sub(r"[./\-]", "_", short.replace("%2F", "/")).lower()


def _settings_hints(settings, key):
    inf = ((settings or {}).get("telemetry") or {}).get("inference") or {}
    return [str(h).lower() for h in (inf.get(key) or config.DEFAULTS["telemetry"]["inference"][key])]


# ---------------------------------------------------------------------------
# Field statistics from sampled entries or table schemas
# ---------------------------------------------------------------------------

def _new_stat(ftype):
    return {"type": ftype, "count": 0, "values": {}, "overflow": False, "email": 0, "idlike": 0, "ts": 0,
            "len": 0, "int": 0}


def _observe(stat, value):
    stat["count"] += 1
    if isinstance(value, bool):
        key = "true" if value else "false"
    elif isinstance(value, (int, float)):
        if float(value).is_integer():
            stat["int"] += 1
        key = repr(value)
    else:
        text = str(value)
        stat["len"] += len(text)
        if EMAIL_VALUE_RE.match(text):
            stat["email"] += 1
        if ID_VALUE_RE.match(text):
            stat["idlike"] += 1
        if TIMESTAMP_VALUE_RE.match(text):
            stat["ts"] += 1
        key = text[:120]
    if key in stat["values"]:
        stat["values"][key] += 1
    elif len(stat["values"]) < MAX_DISTINCT:
        stat["values"][key] = 1
    else:
        stat["overflow"] = True


def new_stats():
    return {"n": 0, "fields": {}, "resources": {}, "audit": False, "genai": False}


def add_entry(stats, entry, max_fields=MAX_FIELDS):
    """Adds one Cloud Logging LogEntry (as returned by entries.list) to `stats`."""
    if not isinstance(entry, dict):
        return
    stats["n"] += 1
    fields = stats["fields"]

    def leaf(path, value, ftype):
        stat = fields.get(path)
        if stat is None:
            if len(fields) >= max_fields:
                return
            stat = fields[path] = _new_stat(ftype)
        _observe(stat, value)

    leaf("timestamp", entry.get("timestamp") or "", "TIMESTAMP")
    if isinstance(entry.get("severity"), str):
        leaf("severity", entry["severity"], "STRING")
    resource = entry.get("resource") if isinstance(entry.get("resource"), dict) else {}
    rtype = resource.get("type")
    if isinstance(rtype, str) and rtype:
        leaf("resource.type", rtype, "STRING")
        stats["resources"][rtype] = stats["resources"].get(rtype, 0) + 1
    proto = entry.get("protoPayload") if isinstance(entry.get("protoPayload"), dict) else {}
    if str(proto.get("@type") or "").endswith("AuditLog"):
        stats["audit"] = True

    def walk(prefix, obj, depth):
        if depth > 5:
            return
        if isinstance(obj, dict):
            for key, val in obj.items():
                if not isinstance(key, str):
                    continue
                if key.startswith("gen_ai"):
                    stats["genai"] = True
                if not SEGMENT_RE.match(key):
                    continue  # keys with dots or '@' cannot be addressed by a dotted path
                walk("%s.%s" % (prefix, key) if prefix else key, val, depth + 1)
        elif isinstance(obj, list):
            if obj and not isinstance(obj[0], (dict, list)):
                walk("%s[0]" % prefix, obj[0], depth + 1)
        elif isinstance(obj, bool):
            leaf(prefix, obj, "BOOL")
        elif isinstance(obj, (int, float)):
            leaf(prefix, obj, "NUMBER")
        elif isinstance(obj, str) and obj:
            leaf(prefix, obj, "STRING")

    walk("resource.labels", resource.get("labels") if isinstance(resource.get("labels"), dict) else {}, 1)
    for root in LOG_ROOTS:
        sub = entry.get(root)
        if isinstance(sub, dict):
            walk(root, sub, 1)
    log_name = entry.get("logName") or ""
    if "gen_ai." in log_name:
        stats["genai"] = True


def entry_stats(entries, max_fields=MAX_FIELDS):
    stats = new_stats()
    for entry in entries or []:
        add_entry(stats, entry, max_fields)
    return stats


def schema_stats(schema, limit=200):
    """Statistics without values for a BigQuery table schema (types only)."""
    stats = new_stats()
    if schema is None:
        return stats
    for path, ftype in schema.leaf_types(limit):
        if "REPEATED" in (ftype or ""):
            continue
        stats["fields"][path] = _new_stat((ftype or "").split(" ")[0])
    stats["audit"] = any(name.startswith("protopayload_auditlog") for name in schema.tree)
    stats["genai"] = any("gen_ai" in path.lower() for path in stats["fields"])
    return stats


def field_types(stats):
    return {path: s["type"] for path, s in (stats or {}).get("fields", {}).items()}


def _leaf_name(path):
    return INDEX_RE.sub("", path).rsplit(".", 1)[-1].lower()


def _hint_score(path, hints):
    """Tier 3 for an exact leaf-name hint, 2 for a hint inside the leaf name, 1 for a hint elsewhere in the path,
    0 for none. Within a tier, hints earlier in the list rank higher (the bonus stays below the next tier)."""
    leaf = _leaf_name(path)
    low = INDEX_RE.sub("", path).lower()

    def bonus(pos):
        return max(0.0, 0.5 - pos * 0.01)

    for pos, hint in enumerate(hints):
        if leaf == hint:
            return 3.0 + bonus(pos)
    for pos, hint in enumerate(hints):
        if hint in leaf:
            return 2.0 + bonus(pos)
    for pos, hint in enumerate(hints):
        if hint in low:
            return 1.0 + bonus(pos)
    return 0.0


NUMERIC = ("INT64", "INTEGER", "FLOAT64", "FLOAT", "NUMERIC", "BIGNUMERIC", "NUMBER")


def _is_numeric(stat):
    return any((stat.get("type") or "").startswith(t) for t in NUMERIC)


def _ratio(stat, key, n):
    return (stat.get(key) or 0) / float(max(1, stat.get("count") or 0)) if n else 0.0


def _coverage(stat, n):
    return (stat.get("count") or 0) / float(n) if n else None


def _categorical(stat, n):
    """(is a useful breakdown, distinct count) from sampled values; None when there are no values (schemas)."""
    if not n or not stat.get("count"):
        return None, None
    distinct = len(stat["values"])
    if stat["overflow"] or distinct < 2:
        return False, distinct
    if _ratio(stat, "email", n) > 0.3 or _ratio(stat, "idlike", n) > 0.5 or _ratio(stat, "ts", n) > 0.5:
        return False, distinct
    avg_len = stat["len"] / float(max(1, stat["count"])) if stat["type"] == "STRING" else 0
    if avg_len > 80:
        return False, distinct
    return distinct <= max(30, int(stat["count"] * 0.6)), distinct


def infer_mapping(stats, settings=None, kind="logging"):
    """Dashboard field mapping inferred from field statistics and name hints. Paths are LogEntry paths for logs
    and column paths for tables. None means the field could not be found."""
    stats = stats or new_stats()
    n = stats.get("n") or 0
    fields = stats.get("fields") or {}
    hints = {k: _settings_hints(settings, k) for k in ("time_hints", "user_hints", "model_hints", "token_hints",
                                                       "operation_hints", "status_hints")}
    mapping = {}
    # time: a top-level TIMESTAMP column, preferring hinted names; logs always have "timestamp".
    if kind == "logging":
        mapping["time"] = "timestamp"
    else:
        times = [p for p, s in fields.items() if s["type"].startswith("TIMESTAMP") and "." not in p]
        times.sort(key=lambda p: -_hint_score(p, hints["time_hints"]))
        mapping["time"] = times[0] if times else None
    taken = set()

    def pick(key, candidates):
        candidates = [c for c in candidates if c[1] > 0 and c[0] not in taken]
        if not candidates:
            mapping[key] = None
            return
        candidates.sort(key=lambda c: (-c[1], len(c[0])))
        mapping[key] = candidates[0][0]
        taken.add(candidates[0][0])

    # user: values that look like e-mail addresses, else hinted names.
    pick("user", [(p, (3.0 * _ratio(s, "email", n)) + _hint_score(p, hints["user_hints"]) +
                   (0.5 * (_coverage(s, n) or 0)))
                  for p, s in fields.items() if s["type"] == "STRING" and
                  (_ratio(s, "email", n) >= 0.5 or _hint_score(p, hints["user_hints"]) >= 2.0)])
    # tokens: numeric fields named like tokens, preferring totals.
    pick("tokens", [(p, _hint_score(p, hints["token_hints"]) + (0.5 if "total" in _leaf_name(p) else 0))
                    for p, s in fields.items() if _is_numeric(s) and _hint_score(p, hints["token_hints"]) >= 1.0])
    # model: hinted string fields, or values that read as model ids.
    from . import models as models_mod

    def model_like(stat):
        vals = list(stat["values"])[:10]
        return bool(vals) and sum(1 for v in vals if models_mod.parse_sku(str(v).replace("-", " "))) >= max(1, len(vals) // 2)

    pick("model", [(p, _hint_score(p, hints["model_hints"]) + (2.0 if model_like(s) else 0))
                   for p, s in fields.items() if s["type"] == "STRING" and
                   (_hint_score(p, hints["model_hints"]) >= 2.0 or model_like(s))])
    # status: a low-cardinality field with a status-like name; logs fall back to severity.
    status = []
    for p, s in fields.items():
        if p in taken or p == "timestamp":
            continue
        useful, distinct = _categorical(s, n)
        score = _hint_score(p, hints["status_hints"])
        if score < 1.0 or useful is False or (distinct is not None and distinct > 15):
            continue
        status.append((p, score + (0.5 if useful else 0) + (0.2 if p != "severity" else 0)))
    pick("status", status)
    if mapping.get("status") is None and "severity" in fields and kind == "logging":
        mapping["status"] = "severity"
        taken.add("severity")
    # operation: the most useful categorical breakdown, preferring hinted names.
    ops = []
    for p, s in fields.items():
        if p in taken or p in ("timestamp", "severity") or s["type"] not in ("STRING",):
            continue
        useful, distinct = _categorical(s, n)
        score = _hint_score(p, hints["operation_hints"])
        if useful is None:  # schema only: names decide
            if score >= 2.0:
                ops.append((p, score))
            continue
        if not useful:
            continue
        cov = _coverage(s, n) or 0
        ops.append((p, score + cov + min(distinct, 12) / 12.0 + (0.3 if p.startswith(("jsonPayload", "protoPayload")) else 0)))
    pick("operation", ops)
    if mapping.get("operation") is None and kind == "logging" and len(stats.get("resources") or {}) > 1:
        mapping["operation"] = "resource.type"
    return mapping


def is_sensitive(stats, settings=None):
    """True when a free-text field with a prompt/response-like name is present (long values when sampled)."""
    hints = _settings_hints(settings, "sensitive_hints")
    n = (stats or {}).get("n") or 0
    for path, s in ((stats or {}).get("fields") or {}).items():
        if s["type"] != "STRING":
            continue
        leaf = _leaf_name(path)
        if not any(h == leaf or leaf.endswith("_" + h) or leaf.startswith(h) for h in hints):
            continue
        if not n:
            return True
        if s["count"] and s["len"] / float(s["count"]) >= 40:
            return True
    return False


def field_label(path):
    """A readable label from a field path ("protoPayload.methodName" -> "Method name")."""
    if not path:
        return None
    leaf = INDEX_RE.sub("", path).rsplit(".", 1)[-1]
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", leaf).replace("_", " ").split()
    if not words:
        return None
    text = " ".join(words).lower()
    return text[:1].upper() + text[1:]


# ---------------------------------------------------------------------------
# Product and resource names (Service Usage API, Logging resource descriptors)
# ---------------------------------------------------------------------------

def _cache(ctx):
    return getattr(getattr(ctx, "app", None), "cache", None)


def _live(ctx):
    return type(getattr(ctx, "gcp", None)).__name__ == "Gcp"


def service_titles(ctx):
    """{service name: title} of the APIs enabled in the scanned projects (Service Usage API), {} when unreadable."""
    if not _live(ctx):
        return {}
    projects = [p for p in list(getattr(ctx, "projects", None) or [ctx.project])[:3] if config.valid_project(p or "")]
    if not projects:
        return {}
    cache = _cache(ctx)

    def fetch_project(proj):
        def _load_one():
            try:
                items, _ = ctx.gcp.paginate("https://serviceusage.googleapis.com/v1/projects/%s/services" % proj,
                                            quota_project=ctx.quota_project, items_key="services",
                                            params={"filter": "state:ENABLED"}, page_size=200, max_pages=5)
            except Exception:  # pylint: disable=broad-except
                return {}
            proj_out = {}
            for item in items:
                name = (item.get("config") or {}).get("name") or (item.get("name") or "").rsplit("/", 1)[-1]
                title = (item.get("config") or {}).get("title")
                if name and title:
                    proj_out.setdefault(name, title)
            return proj_out

        if cache is None:
            return _load_one()
        val, _ = cache.get_or_compute(("svc_titles_proj", proj), DIRECTORY_TTL, _load_one, cacheable=bool)
        return val or {}

    def load():
        out = {}
        if len(projects) > 1 and hasattr(ctx, "run"):
            res = ctx.run({p: (lambda proj=p: fetch_project(proj)) for p in projects})
            for proj in projects:
                ok, pmap = res.get(proj, (False, None))
                if ok and pmap:
                    for k, v in pmap.items():
                        out.setdefault(k, v)
        else:
            for proj in projects:
                for k, v in fetch_project(proj).items():
                    out.setdefault(k, v)
        return out

    if cache is None:
        return load()
    val, _ = cache.get_or_compute(("svc_titles", tuple(projects)), DIRECTORY_TTL, load, cacheable=bool)
    return val or {}


def resource_descriptors(ctx):
    """{resource type: {"name": display name, "description": text, "labels": [label keys]}} from Cloud Logging."""
    if not _live(ctx):
        return {}

    def load():
        try:
            items, _ = ctx.gcp.paginate("https://logging.googleapis.com/v2/monitoredResourceDescriptors",
                                        quota_project=ctx.quota_project, items_key="resourceDescriptors",
                                        page_size=1000, max_pages=3)
        except Exception:  # pylint: disable=broad-except
            return {}
        out = {}
        for item in items:
            rtype = item.get("type")
            if rtype:
                out[rtype] = {"name": item.get("displayName") or rtype, "description": item.get("description") or "",
                              "labels": [lb.get("key") for lb in item.get("labels") or [] if lb.get("key")]}
        return out

    cache = _cache(ctx)
    if cache is None:
        return load()
    val, _ = cache.get_or_compute(("resource_descriptors",), DIRECTORY_TTL, load, cacheable=bool)
    return val or {}


def _renames(settings):
    out = []
    for item in ((settings or {}).get("telemetry") or {}).get("display_renames") or []:
        left, _, right = str(item).partition("=")
        if left.strip() and right.strip():
            out.append((left.strip(), right.strip()))
    return out


def apply_renames(text, settings):
    for left, right in _renames(settings):
        text = re.sub(re.escape(left), right, text, flags=re.I)
    return text


def humanize_service(service):
    """Readable fallback for a service without a title: "cloudscheduler.googleapis.com" -> "Cloudscheduler"."""
    head = (service or "").split(".", 1)[0]
    return head[:1].upper() + head[1:] if head else service


def service_display(titles, service, settings):
    title = (titles or {}).get(service)
    if title:
        title = re.sub(r"\s+API$", "", title.strip())
        title = re.sub(r"\s+Admin$", "", title)
    else:
        title = humanize_service(service)
    return apply_renames(title, settings)


def split_log(short):
    """(service, rest) of a log id: "run.googleapis.com/requests" -> ("run.googleapis.com", "requests")."""
    short = urllib.parse.unquote(short or "")
    if "/" in short:
        head, rest = short.split("/", 1)
        if SERVICE_RE.match(head):
            return head, rest.strip("/")
    return None, short


def split_table(logical):
    """(service, rest) of an exported log table: "run_googleapis_com_requests" -> ("run.googleapis.com", "requests")."""
    m = re.match(r"^([a-z0-9]+)_googleapis_com_(.+)$", logical or "")
    if m:
        return "%s.googleapis.com" % m.group(1), m.group(2)
    return None, logical


def _pretty(text):
    words = [w for w in re.split(r"[_/\s]+", text or "") if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or text


# ---------------------------------------------------------------------------
# Category and card text
# ---------------------------------------------------------------------------

def classify(stats, service, settings, mapping, resources=None):
    """Category from the data itself: audit payloads, AI services and GenAI/model/token fields, platform resources."""
    stats = stats or new_stats()
    ai_services = {s.lower() for s in ((settings or {}).get("gateway") or {}).get("services") or []}
    if stats.get("audit"):
        return SECURITY
    if (service and service.lower() in ai_services) or stats.get("genai") or \
            ((mapping or {}).get("model") and (mapping or {}).get("tokens")):
        return AI
    rtypes = [r for r in (resources or stats.get("resources") or {}) if r and r != "global"]
    if service or rtypes:
        return OPERATIONS
    return OTHER


def resource_names(stats, descriptors, settings=None):
    out = []
    for rtype, _count in sorted((stats or {}).get("resources", {}).items(), key=lambda kv: -kv[1])[:3]:
        if rtype == "global":
            continue
        name = (descriptors or {}).get(rtype, {}).get("name") or rtype
        out.append(apply_renames(name, settings))
    return out


def describe(sid, short=None, dataset=None, built_in=None, stats=None, mapping=None, settings=None, titles=None,
             descriptors=None):
    """Card text of one source, built from its log id or table name, the API title and the resources seen."""
    stats = stats or new_stats()
    mapping = mapping or {}
    out = {"id": sid, "known": False, "builtIn": list(built_in or []), "titleParams": None, "whatParams": None}
    if short is not None:
        service, rest = split_log(short)
    else:
        service, rest = split_table(sid)
    product = service_display(titles, service, settings) if service else None
    resources = resource_names(stats, descriptors, settings)
    res_text = ", ".join(resources)
    if short is not None:
        if product:
            out["title"], out["titleParams"] = "{product} · {log}", {"product": product, "log": rest}
            if res_text:
                out["what"] = "Log entries that {product} writes to Cloud Logging under {log}, from {resources}."
                out["whatParams"] = {"product": product, "log": short, "resources": res_text}
            else:
                out["what"] = "Log entries that {product} writes to Cloud Logging under {log}."
                out["whatParams"] = {"product": product, "log": short}
        else:
            out["title"], out["titleParams"] = "{log}", {"log": short}
            if res_text:
                out["what"] = "Log entries written to Cloud Logging under the name {log}, from {resources}."
                out["whatParams"] = {"log": short, "resources": res_text}
            else:
                out["what"] = "Log entries written to Cloud Logging under the name {log}."
                out["whatParams"] = {"log": short}
    elif product:
        out["title"], out["titleParams"] = "{product} · {log}", {"product": product, "log": _pretty(rest)}
        out["what"] = "Log entries of {product} exported from Cloud Logging to the BigQuery dataset {dataset}."
        out["whatParams"] = {"product": product, "dataset": dataset or ""}
    else:
        out["title"], out["titleParams"] = "{name}", {"name": _pretty(sid)}
        out["what"] = ("A table in the BigQuery dataset {dataset}. Its dashboard counts rows over time and breaks "
                       "them down by the fields detected automatically.")
        out["whatParams"] = {"dataset": dataset or ""}
    out["category"] = classify(stats, service, settings, mapping)
    out["sensitive"] = is_sensitive(stats, settings)
    out["service"] = service
    out["product"] = product
    out["resources"] = resources
    for key in LABELED:
        path = mapping.get(key)
        if not path:
            out["%sLabel" % key] = None
        elif path == "severity":
            out["%sLabel" % key] = "Severity"
        elif path == "resource.type":
            out["%sLabel" % key] = "Resource type"
        else:
            out["%sLabel" % key] = field_label(path) or GENERIC_LABELS[key]
    return out


def title_text(card):
    """English display name of a card (the web UI translates the template instead)."""
    if not card:
        return ""
    title = card.get("title") or ""
    for key, value in (card.get("titleParams") or {}).items():
        title = title.replace("{%s}" % key, str(value))
    return title[:80]


# ---------------------------------------------------------------------------
# Connector bodies
# ---------------------------------------------------------------------------

def logging_connector(card, log_name, mapping):
    """A ready connector body that reads one log through the Logging API, or None for an unusual name."""
    if not isinstance(log_name, str) or not LOG_NAME_RE.match(log_name):
        return None
    body_map = {"time": "timestamp"}
    body_map.update({k: (mapping or {}).get(k) for k in FIELDS})
    return {"name": title_text(card), "enabled": True, "source": "logging", "bigquery": None,
            "logging": {"filter": 'logName="%s"' % log_name}, "mapping": body_map}


def _canonical(schema, path):
    """The schema spelling of a column path (BigQuery names are case-insensitive), or None."""
    out = []
    children = schema.tree
    for part in path.split("."):
        node = children.get(part.lower())
        if node is None:
            return None
        out.append(node.name)
        children = node.children
    return ".".join(out)


def _bq_candidates(schema, path):
    yield path
    head, _, rest = path.partition(".")
    low = head.lower()
    if rest and low in ("protopayload", "jsonpayload"):
        for name, node in schema.tree.items():
            if name != low and name.startswith(low):
                yield "%s.%s" % (node.name, rest)


def resolve_bigquery_path(schema, path):
    """Translates a LogEntry path to a column path of an exported table, or None if it does not exist."""
    if not path or schema is None:
        return None
    indexed = "[" in path
    bare = INDEX_RE.sub("", path)
    for candidate in _bq_candidates(schema, bare):
        if indexed:
            # A repeated record resolves to its first element; inside a JSON column the index is required.
            node = schema.node(candidate)
            if node is not None and node.type not in ("STRUCT", "JSON"):
                return _canonical(schema, candidate)
            continue
        if build_expr("t", schema, candidate) is not None:
            return _canonical(schema, candidate) or candidate
    return None


def usable_column(schema, path):
    return bool(path) and "[" not in path and all(SEGMENT_RE.match(s) for s in path.split(".")) and \
        build_expr("t", schema, path) is not None


def bigquery_connector(card, project, dataset, logical, schema, mapping):
    """A ready connector body for a table and the card with labels that match the resolved mapping, or
    (None, card) when the table has no TIMESTAMP column to chart over."""
    if schema is None or not any(n.type == "TIMESTAMP" and n.mode != "REPEATED" for n in schema.tree.values()):
        return None, card
    mapping = mapping or {}
    card = dict(card)
    time = mapping.get("time")
    node = schema.node(time) if time else None
    body_map = {"time": node.name if node is not None and node.type == "TIMESTAMP" and "." not in time else None}
    for key in FIELDS:
        path = mapping.get(key)
        resolved = None
        if path:
            resolved = path if usable_column(schema, path) else resolve_bigquery_path(schema, path)
            if resolved and "[" in resolved:
                resolved = None
        body_map[key] = resolved
        if key in LABELED and not resolved:
            card["%sLabel" % key] = None
    body = {"name": title_text(card), "enabled": True, "source": "bigquery", "logging": None,
            "bigquery": {"project": project, "dataset": dataset, "table": logical}, "mapping": body_map}
    return body, card


def table_mapping_from_log(schema, log_mapping):
    """Translates a mapping inferred from log entries to the columns of the exported table."""
    out = {"time": schema.first_timestamp() if schema is not None else None}
    for key in FIELDS:
        path = (log_mapping or {}).get(key)
        out[key] = resolve_bigquery_path(schema, path) if path else None
    if schema is not None and schema.node("timestamp") is not None:
        out["time"] = schema.node("timestamp").name
    return out


def _filter_log(conn):
    m = FILTER_RE.match((conn.get("logging") or {}).get("filter") or "")
    return urllib.parse.unquote(m.group(1).split("/logs/", 1)[1]) if m else None


def connector_source_id(conn):
    """The source id a connector reads (one log or one table), or None for a custom filter."""
    if conn.get("source") == "bigquery":
        table = ((conn.get("bigquery") or {}).get("table") or "").strip()
        if table.endswith("_*"):
            table = table[:-2]
        return table.lower() or None
    short = _filter_log(conn)
    return sanitize_log_name(short) if short else None


def for_connector(conn, settings=None):
    """Card text for a connector that reads one log or table, with labels that follow its mapping.

    Returns None for custom filters, whose meaning only the author knows (their saved meta is used instead)."""
    sid = connector_source_id(conn)
    if sid is None:
        return None
    mapping = conn.get("mapping") or {}
    if conn.get("source") == "bigquery":
        return describe(sid, dataset=(conn.get("bigquery") or {}).get("dataset"), mapping=mapping, settings=settings)
    return describe(sid, short=_filter_log(conn), mapping=mapping, settings=settings)
