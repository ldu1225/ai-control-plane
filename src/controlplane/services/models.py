"""Models and tokens: per-model usage from billing, Cloud Monitoring and Antigravity."""

from __future__ import annotations

import datetime as dt
import logging
import re

from .. import timeutil
from ..cache import TTLCache
from ..gcp import GcpError
from .base import (AGY_SINK, bucket_sql, money, range_params, ratio, to_float, to_int)
from .billing import ai_clause, billing_fields, excluded_clause, is_excluded
from . import monitoring

LOG = logging.getLogger("controlplane.models")

QUALIFIER_RE = re.compile(r"(?i)(?:<=|>=|=<|=>|<|>|\u2264|\u2265|up to|over|above|below)\s*\d+(?:\.\d+)?\s*[km]\b"
                          r"(?:\s*(?:input\s+)?tokens?)?")
WORD_SPLIT_RE = re.compile(r"[\s_\-\u2013\u2014,:;/|\[\]{}]+")
PAREN_RE = re.compile(r"\(([^)]*)\)")
DIGIT_RE = re.compile(r"\d")
# Families that anchor a model name inside free-text billing SKU descriptions. Partner models of other
# families are still recognized through their billing service name (see parse_sku). No versions are listed.
FAMILIES = {"gemini", "gemma", "medgemma", "claude", "llama", "mistral", "mixtral", "codestral", "ministral",
            "pixtral", "imagen", "veo", "chirp", "lyria", "jamba", "deepseek", "qwen", "gpt", "embedding"}
# Words that can directly follow the family word as the start of a model name (besides a version).
TIERS = {"pro", "flash", "lite", "ultra", "nano", "omni", "sonnet", "opus", "haiku", "instant", "large", "medium",
         "small", "fast", "oss", "live", "embedding", "native", "image", "tts", "hd", "clip", "coder", "maverick",
         "scout"}
# Words that end a model name: modality, measure, pricing dimension or connector.
STOP_WORDS = {"text", "input", "output", "caching", "cached", "cache", "token", "tokens", "count", "predictions",
              "prediction", "generation", "generations", "characters", "character", "seconds", "second", "requests",
              "request", "read", "write", "ttl", "context", "window", "size", "from", "to", "video", "audio", "music",
              "search", "query", "queries", "grounding", "with", "for", "and", "per", "in", "on", "of", "by",
              "images", "storage", "training", "tuning", "endpoint", "hour", "hours", "node", "usage", "calls", "call"}
# Qualifiers skipped inside a model name: location, release stage, pricing tier and context band.
SKIP_WORDS = {"global", "regional", "ga", "preview", "short", "long", "thinking", "priority", "flex", "standard",
              "batch", "online", "free", "latest", "exp", "experimental"}
MODALITIES = {"text", "image", "audio", "video"}
# Words before the model name that describe the API call or the vendor rather than a product.
GENERIC_WORDS = {"generate", "content", "generatecontent", "bidi", "batch", "online", "predict", "prediction",
                 "predictions", "token", "tokens", "count", "input", "output", "cached", "cache", "caching", "text",
                 "image", "audio", "video", "for", "the", "search", "query", "grounding", "request", "requests",
                 "live", "streaming", "stream", "api", "model", "models", "vertex", "ai", "garden", "a", "an",
                 "anthropic", "google", "meta", "openai", "ai21", "labs", "nvidia", "partner", "maas"}
ACRONYMS = {"tts": "TTS", "hd": "HD", "gpt": "GPT", "oss": "OSS", "ai": "AI", "api": "API"}
VENDORS = {"gemini": "Google", "gemma": "Google", "imagen": "Google", "veo": "Google", "chirp": "Google",
           "lyria": "Google", "claude": "Anthropic", "anthropic": "Anthropic", "llama": "Meta", "meta": "Meta",
           "mistral": "Mistral AI", "mixtral": "Mistral AI", "codestral": "Mistral AI", "google": "Google"}
OTHER_MEASURES = ("images", "seconds", "characters", "requests", "other")
NEW_MODEL_DAYS = 7


def _model_words(low, i, for_form):
    """Model name words starting at the family word low[i], or None when the family word is only a brand
    prefix (for example "Gemini Enterprise" or "Gemini API")."""
    j = i + 1
    if j >= len(low) or not (DIGIT_RE.search(low[j]) or low[j] in TIERS):
        return None
    out = [low[i]]
    if low[i] == "embedding" and i > 0 and low[i - 1] in ("text", "multimodal"):
        out.insert(0, low[i - 1])
    while j < len(low):
        word = low[j]
        nxt = j + 1
        while nxt < len(low) and low[nxt] in SKIP_WORDS:
            nxt += 1
        following = low[nxt] if nxt < len(low) else None
        if word in SKIP_WORDS:
            j += 1
            continue
        if word == "native" and following in ("image", "audio"):
            out.extend([word, following])
            j = nxt + 1
            continue
        if word == "image":
            # "Flash Image Image Output": the first "image" names the model. In the call form
            # "... token count for Gemini 3 Pro Image" the full model name comes last.
            if following in MODALITIES or (for_form and (following is None or following in STOP_WORDS)):
                out.append(word)
                j += 1
                continue
            break
        if word in STOP_WORDS:
            break
        out.append(word)
        j += 1
    return out


def _paren_model(text, brand_family):
    """Model named in parentheses, for example "(Flash 3.5)" after a "Gemini Enterprise" product prefix."""
    low = [w.lower().strip(".") for w in WORD_SPLIT_RE.split(text or "") if w.strip(".")]
    for i, word in enumerate(low):
        if word in FAMILIES:
            words = _model_words(low, i, False)
            if words:
                return words
    if brand_family and any(DIGIT_RE.search(w) for w in low) and any(w in TIERS for w in low):
        versions = [w for w in low if DIGIT_RE.search(w)]
        rest = [w for w in low if not DIGIT_RE.search(w) and w not in SKIP_WORDS and w not in STOP_WORDS]
        return [brand_family] + versions + rest
    return None


def _is_model_service(service):
    """Partner models are billed under their own service name, for example "Claude Sonnet 4.5"."""
    words = [w for w in WORD_SPLIT_RE.split(service or "") if w]
    return bool(words) and any(DIGIT_RE.search(w) for w in words) and any(w.isalpha() for w in words)


def parse_sku(description, service=None):
    """Identifies the model behind a billing SKU.

    Returns {"key", "name", "product"}, or None when the SKU is not model usage (compute, storage, licenses).
    "product" is set when a product re-bills model usage under its own SKU, for example
    "CodeMender Gemini 3.7 Flash Global Text Input" or "Gemini Enterprise AlphaEvolve Output Tokens (Flash 3.5)".
    """
    if not description or not isinstance(description, str):
        return None
    text = QUALIFIER_RE.sub(" ", description)
    parens = PAREN_RE.findall(text)
    original = [w.strip(".") for w in WORD_SPLIT_RE.split(PAREN_RE.sub(" ", text)) if w.strip(".")]
    low = [w.lower() for w in original]
    for_form = False
    product = []
    brand_family = None
    model = None
    for i, word in enumerate(low):
        if word in FAMILIES:
            model = _model_words(low, i, for_form)
            if model:
                break
            brand_family = brand_family or word
            product.append(original[i])
            continue
        if word == "for":
            for_form = True
            continue
        if word in STOP_WORDS:
            if product and brand_family:
                break  # "Gemini Enterprise AlphaEvolve Output Tokens": the measure ends the product name
            continue
        if word not in GENERIC_WORDS and word not in SKIP_WORDS and not DIGIT_RE.search(word):
            product.append(original[i])
    if model is None:
        for group in parens:
            model = _paren_model(group, brand_family)
            if model:
                break
    if model is None:
        if not (service and _is_model_service(service)):
            return None
        model = [w.lower() for w in WORD_SPLIT_RE.split(service) if w and w.lower() not in SKIP_WORDS]
        product = []
    display_key = _base_id("-".join(model))
    if not display_key:
        return None
    return {"key": normalize_model_id(display_key), "name": display_from_key(display_key),
            "product": " ".join(product) if product else None}


def family_name(description):
    """Model display name for a billing SKU description, or None when the SKU is not model usage."""
    info = parse_sku(description)
    return info["name"] if info else None


def model_key(name):
    key = re.sub(r"\s+", "-", (name or "").strip().lower())
    key = re.sub(r"-{2,}", "-", key).strip("-")
    return key


def display_from_key(key):
    words = []
    for part in (key or "").split("-"):
        if not part:
            continue
        if part in ACRONYMS:
            words.append(ACRONYMS[part])
        elif re.match(r"^\d", part):
            words.append(part)
        else:
            words.append(part[:1].upper() + part[1:])
    return " ".join(words)


def vendor_for(name):
    first = (name or "").split()[0].lower() if name and name.split() else ""
    if first in VENDORS:
        vendor = VENDORS[first]
        if first in ("google", "anthropic", "meta") and len(name.split()) > 1:
            return VENDORS.get(name.split()[1].lower(), vendor)
        return vendor
    return "Partner"


def _base_id(model_id):
    """Normalized model id used for display: resource path, revision, date and release-stage suffixes removed."""
    mid = (model_id or "").strip().lower()
    if "/" in mid:
        mid = mid.rsplit("/", 1)[-1]
    mid = mid.split("@", 1)[0]
    mid = re.sub(r"[\s_]+", "-", mid)
    mid = re.sub(r"-\d{8}$", "", mid)
    mid = re.sub(r"-\d{2}-\d{4}$", "", mid)
    mid = re.sub(r"-(?:preview|experimental|exp(?:-\d+)?|latest)(?=-|$)", "", mid)
    mid = re.sub(r"-\d{2}-\d{4}$", "", mid)
    if "embedding" not in mid:
        mid = re.sub(r"-\d{3}$", "", mid)
    if mid.startswith("claude-") and "." not in mid:
        mid = re.sub(r"(?<=-)(\d+)-(\d)(?=-|$)", r"\1.\2", mid)
    return re.sub(r"-{2,}", "-", mid).strip("-")


def normalize_model_id(model_id):
    """Normalizes Monitoring, Antigravity, audit log and billing model ids to catalog keys. "X.0" versions
    collapse to "X" so that billing names (Gemini 3.0 Pro) and API ids (gemini-3-pro-preview) merge."""
    return re.sub(r"(?<=-)(\d+)\.0(?=-|$)", r"\1", _base_id(model_id))


def display_name_for_id(model_id):
    """Display name for an API model id, keeping the published version format (Gemini 2.0 Flash)."""
    return display_from_key(_base_id(model_id))


def classify_measure(description, unit, pricing_unit=None):
    d = (description or "").lower()
    u = ("%s %s" % (unit or "", pricing_unit or "")).lower()
    if "cach" in d:
        measure = "cached_tokens"
    elif "input" in d:
        measure = "input_tokens"
    elif "output" in d:
        measure = "output_tokens"
    elif "token" in d or "token" in u:
        measure = "tokens"
    elif "image" in d:
        measure = "images"
    elif "second" in d or "video" in d or "second" in u:
        measure = "seconds"
    elif "character" in d or "character" in u:
        measure = "characters"
    elif "request" in d or "call" in d or "request" in u:
        measure = "requests"
    else:
        measure = "other"
    if measure.endswith("tokens"):
        if "second" in u:
            measure = "seconds"
        elif "character" in u:
            measure = "characters"
        elif re.search(r"\bimages?\b", u):
            measure = "images"
        elif any(x in u for x in ("byte", "hour", "month")):
            measure = "other"
    return measure


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def billing_rows(ctx, src, rng, per_bucket=True):
    f = billing_fields(src)
    if not (f["sku"] and f["cost"]):
        ctx.warn(src.label, "unsupported_schema", "The billing table is missing sku.description or cost.")
        return None
    bucket = bucket_sql(f["ts"]) if per_bucket else "DATE '1970-01-01'"
    sel_proj = getattr(ctx, "selected_project", "all") or "all"
    proj_clause = (" AND %s = @project" % f["project"]) if (sel_proj != "all" and f.get("project")) else ""
    sql = ("WITH %s\nSELECT %s AS bucket, %s AS sku, %s AS service, %s AS unit, %s AS pricing_unit, "
           "SUM(IFNULL(%s, 0)) AS amount, SUM(IFNULL(%s, 0)) AS cost, ANY_VALUE(%s) AS currency\n"
           "FROM bil b WHERE %s AND %s%s\nGROUP BY bucket, sku, service, unit, pricing_unit"
           % (src.cte("bil", start="start_ts"), bucket, f["sku"], f["service"] or "CAST(NULL AS STRING)",
              f["unit"] or "CAST(NULL AS STRING)", f["pricing_unit"] or "CAST(NULL AS STRING)",
              f["amount"] or "0", f["cost"], f["currency"] or "CAST(NULL AS STRING)", ai_clause(f),
              excluded_clause(f), proj_clause))
    params = range_params(rng, with_prev=False)
    params["excluded"] = ctx.excluded_patterns
    if sel_proj != "all" and f.get("project"):
        params["project"] = sel_proj
    return ctx.query(sql, params, "model_usage")


def antigravity_rows(ctx, rng):
    """Per bucket/model tokens from the optional BigQuery sink of inference logs."""
    src = ctx.audit_source(AGY_SINK, optional=True)
    if src is None:
        return None, None
    from .governance import agy_fields
    f = agy_fields(src)
    if not f["model"] or not f["total"]:
        return None, src
    sql = ("WITH %s\nSELECT %s AS bucket, LOWER(%s) AS model, SUM(IFNULL(%s, 0)) AS tokens, "
           "SUM(IFNULL(%s, 0)) AS prompt_tokens, SUM(IFNULL(%s, 0)) AS candidate_tokens\n"
           "FROM s t WHERE %s IS NOT NULL GROUP BY bucket, model"
           % (src.cte("s", start="start_ts"), bucket_sql(f["ts"]), f["model"], f["total"], f["prompt"] or "0",
              f["candidates"] or "0", f["model"]))
    return ctx.query(sql, range_params(rng, with_prev=False), "model_usage"), src


def usage_data(ctx, rng):
    """Collects and merges all model usage for a range (internal structure)."""
    bsrc = ctx.billing_source()
    tasks = {}
    if bsrc is not None:
        tasks["billing"] = lambda: billing_rows(ctx, bsrc, rng)
    tasks["tokens"] = lambda: monitoring.token_counts(ctx, rng.start_ts(), rng.end_ts())
    tasks["invocations"] = lambda: monitoring.invocation_counts(ctx, rng.start_ts(), rng.end_ts())
    tasks["agy"] = lambda: antigravity_rows(ctx, rng)
    res = ctx.run(tasks)
    models = {}
    products = {}
    currency = None

    def model(key, name=None, authoritative=False):
        """Entry for a catalog key. Names from API model ids (authoritative) replace billing-derived names."""
        entry = models.get(key)
        if entry is None:
            entry = {"key": key, "name": name or display_from_key(key), "sources": set(), "billing": {},
                     "mon": None, "inv": None, "agy": {}, "skus": {}, "cost": 0.0, "costDaily": {},
                     "billingTokens": {"input": 0.0, "output": 0.0, "cached": 0.0, "tokens": 0.0},
                     "billingTokenDaily": {}, "other": {}, "named": authoritative,
                     "unmonitoredTokens": {"input": 0.0, "output": 0.0, "cached": 0.0, "tokens": 0.0},
                     "unmonitoredDaily": {}}
            models[key] = entry
        elif name and authoritative and not entry["named"]:
            entry["name"] = name
            entry["named"] = True
        return entry

    status = {"billing": bsrc is not None, "monitoring": False, "invocations": False, "agy": False}
    if "billing" in res:
        ok, rows = res["billing"]
        if ok and rows is not None:
            ctx.add_source(bsrc.label)
            for row in rows:
                sku = row.get("sku") or ""
                service = row.get("service") or ""
                if is_excluded(sku, ctx.excluded_patterns) or is_excluded(service, ctx.excluded_patterns):
                    continue
                info = parse_sku(sku, service)
                if info is None:
                    continue  # not model usage (compute, storage, licenses): reported by FinOps only
                currency = currency or row.get("currency")
                cost = to_float(row.get("cost")) or 0.0
                amount = to_float(row.get("amount")) or 0.0
                measure = classify_measure(sku, row.get("unit"), row.get("pricing_unit"))
                day = row.get("bucket")
                if info["product"]:
                    # Product SKUs re-bill the underlying model usage under the product's own SKU. Their cost
                    # is reported per product and their tokens are never added to the model totals.
                    prod = products.setdefault(info["product"].lower(), {
                        "name": info["product"], "cost": 0.0, "tokens": 0.0, "models": {}})
                    prod["cost"] += cost
                    prod["models"][info["key"]] = info["name"]
                    if measure.endswith("tokens"):
                        prod["tokens"] += amount
                    continue
                entry = model(info["key"], info["name"])
                entry["sources"].add("billing")
                entry["cost"] += cost
                entry["costDaily"][day] = entry["costDaily"].get(day, 0.0) + cost
                sku_entry = entry["skus"].setdefault((sku, service, measure, row.get("unit")),
                                                     {"amount": 0.0, "cost": 0.0})
                sku_entry["amount"] += amount
                sku_entry["cost"] += cost
                if measure.endswith("tokens"):
                    slot = {"input_tokens": "input", "output_tokens": "output", "cached_tokens": "cached"}.get(
                        measure, "tokens")
                    entry["billingTokens"][slot] += amount
                    entry["billingTokenDaily"][day] = entry["billingTokenDaily"].get(day, 0.0) + amount
                    if not _monitored_service(service):
                        entry["unmonitoredTokens"][slot] += amount
                        entry["unmonitoredDaily"][day] = entry["unmonitoredDaily"].get(day, 0.0) + amount
                else:
                    entry["other"][measure] = entry["other"].get(measure, 0.0) + amount
        elif not ok:
            status["billing"] = False
            ctx.warn_error(bsrc.label, rows, "The billing export table")
        else:
            status["billing"] = False
    ok, value = res["tokens"]
    label_tokens = "monitoring:%s" % monitoring.TOKEN_METRIC
    if ok:
        status["monitoring"] = True
        counts, split = value
        if counts:
            ctx.add_source(label_tokens)
        for raw_id, entry_counts in counts.items():
            key = normalize_model_id(raw_id)
            entry = model(key, display_name_for_id(raw_id), authoritative=True)
            entry["sources"].add("monitoring")
            mon = entry["mon"] or {"input": 0, "output": 0, "cached": 0, "total": 0, "daily": {}}
            for slot in ("input", "output", "cached"):
                mon[slot] += sum(entry_counts[slot].values())
            mon["total"] += sum(entry_counts["total"].values())
            for day, v in entry_counts["total"].items():
                mon["daily"][day] = mon["daily"].get(day, 0) + v
            entry["mon"] = mon
        if not split:
            ctx.warn(label_tokens, "partial_data", "Input and output token split is not available from Monitoring.")
    else:
        ctx.warn_error(label_tokens, value, "Cloud Monitoring token metrics")
    ok, value = res["invocations"]
    label_inv = "monitoring:%s" % monitoring.INVOCATION_METRIC
    if ok:
        status["invocations"] = True
        if value:
            ctx.add_source(label_inv)
        for raw_id, inv in value.items():
            key = normalize_model_id(raw_id)
            entry = model(key, display_name_for_id(raw_id), authoritative=True)
            entry["sources"].add("monitoring")
            cur = entry["inv"] or {"invocations": 0, "errors": 0}
            cur["invocations"] += inv["invocations"]
            cur["errors"] += inv["errors"]
            entry["inv"] = cur
    else:
        ctx.warn_error(label_inv, value, "Cloud Monitoring invocation metrics")
    ok, value = res["agy"]
    if ok:
        rows, src = value
        if rows is not None:
            status["agy"] = True
            ctx.add_source(src.label)
            for row in rows:
                if not row.get("model"):
                    continue
                key = normalize_model_id(row["model"])
                entry = model(key, display_name_for_id(row["model"]), authoritative=True)
                entry["sources"].add("antigravity")
                day = row.get("bucket")
                agy = entry["agy"].setdefault(day, {"tokens": 0, "prompt": 0, "candidate": 0})
                agy["tokens"] += to_int(row.get("tokens"))
                agy["prompt"] += to_int(row.get("prompt_tokens"))
                agy["candidate"] += to_int(row.get("candidate_tokens"))
    else:
        ctx.warn_error("bigquery:%s.%s" % (ctx.audit_dataset, AGY_SINK), value, "Antigravity inference logs")
    for entry in models.values():
        if is_excluded(entry["key"].replace("-", " "), ctx.excluded_patterns):
            entry["excluded"] = True
    models = {k: v for k, v in models.items() if not v.get("excluded")}
    return {"models": models, "products": products, "currency": currency or "USD", "status": status}


def _monitored_service(service):
    """True when the billing service reports tokens to the Agent Platform publisher metrics in Cloud Monitoring
    (Agent Platform and partner models served on Agent Platform). Gemini API (generativelanguage) traffic is not."""
    name = (service or "").strip().lower()
    # The billing export may name Agent Platform by its former service name.
    return name == "vertex ai" or "agent platform" in name or _is_model_service(service)


def _model_tokens(entry):
    """Token figures for one model. Monitoring covers Agent Platform traffic; billing token SKUs of services that
    Monitoring does not report (Gemini API) are added on top. Without Monitoring, all billing token SKUs are
    used. Antigravity inference tokens are added in every case."""
    agy_total = sum(v["tokens"] for v in entry["agy"].values())
    agy_prompt = sum(v["prompt"] for v in entry["agy"].values())
    agy_cand = sum(v["candidate"] for v in entry["agy"].values())
    if entry["mon"] is not None:
        mon = entry["mon"]
        u = entry["unmonitoredTokens"]
        extra = u["input"] + u["output"] + u["cached"] + u["tokens"]
        tokens = {"input": int(mon["input"] + u["input"]) + agy_prompt,
                  "output": int(mon["output"] + u["output"]) + agy_cand,
                  "cached": int(mon["cached"] + u["cached"]), "total": int(mon["total"] + extra) + agy_total}
        source = "mixed" if extra else "monitoring"
    else:
        b = entry["billingTokens"]
        tokens = {"input": int(b["input"]) + agy_prompt, "output": int(b["output"]) + agy_cand,
                  "cached": int(b["cached"]),
                  "total": int(b["input"] + b["output"] + b["cached"] + b["tokens"]) + agy_total}
        source = "billing" if (b["input"] or b["output"] or b["cached"] or b["tokens"]) else None
    return tokens, source


def _daily_tokens(entry):
    daily = {}
    if entry["mon"] is not None:
        for day, v in entry["mon"]["daily"].items():
            daily[day] = daily.get(day, 0) + v
        for day, v in entry["unmonitoredDaily"].items():
            daily[day] = daily.get(day, 0) + v
    else:
        for day, v in entry["billingTokenDaily"].items():
            daily[day] = daily.get(day, 0) + v
    for day, v in entry["agy"].items():
        daily[day] = daily.get(day, 0) + v["tokens"]
    return daily


def _first_seen(entry):
    days = set(entry["costDaily"]) | set(entry["billingTokenDaily"]) | set(entry["agy"])
    if entry["mon"]:
        days |= {d for d, v in entry["mon"]["daily"].items() if v}
    days = [d for d in days if isinstance(d, dt.date)]
    return min(days) if days else None


def _catalog_range(ctx, today):
    return timeutil.DateRange(today - dt.timedelta(days=89), today, "day", ctx.tz_name)


def _recently_started(ctx, keys, today):
    """Keys whose first usage within the last 90 days falls inside the new-model window."""
    rng = _catalog_range(ctx, today)
    try:
        data = ctx.memo("models_usage", rng, lambda c: usage_data(c, rng))
    except Exception:  # pylint: disable=broad-except
        LOG.warning("model first-seen lookup failed; newly observed models join the baseline")
        return set()
    recent = set()
    for key in keys:
        entry = (data.get("models") or {}).get(key)
        first = _first_seen(entry) if entry else None
        if first is not None and (today - first).days < NEW_MODEL_DAYS:
            recent.add(key)
    return recent


def model_flags(ctx, keys):
    """Returns ({key: enabled}, {key: isNew}) and records newly seen keys in the catalog snapshot."""
    cat = ctx.settings["model_catalog"]
    disabled = set(cat.get("disabled_models") or [])
    known = cat.get("known_models") or []
    today = timeutil.today_in(ctx.tz_name)
    if keys and ctx.app is not None and getattr(ctx.app, "settings", None) is not None:
        new_keys = [k for k in keys if k not in {e.get("key") for e in known if isinstance(e, dict)}]
        if new_keys:
            recent = _recently_started(ctx, new_keys, today) if known else set()
            known = ctx.app.settings.record_models(new_keys, today.isoformat(),
                                                   bool(cat.get("auto_enable_new_models", True)), recent=recent)
            if not cat.get("auto_enable_new_models", True):
                disabled |= {e.get("key") for e in known if isinstance(e, dict) and e.get("key") in new_keys
                             and not e.get("baseline")}
    known_map = {e.get("key"): e for e in known if isinstance(e, dict)}
    enabled = {}
    is_new = {}
    for key in keys:
        enabled[key] = key not in disabled
        entry = known_map.get(key)
        flag = False
        if entry and not entry.get("baseline"):
            try:
                detected = dt.date.fromisoformat(str(entry.get("detectedAt"))[:10])
                flag = (today - detected).days < NEW_MODEL_DAYS
            except ValueError:
                flag = False
        is_new[key] = flag
    return enabled, is_new


def build_usage(ctx, rng, data=None):
    data = data or usage_data(ctx, rng)
    entries = data["models"]
    enabled, is_new = model_flags(ctx, sorted(entries))
    status = data["status"]
    models = []
    totals_tokens = {"input": 0, "output": 0, "cached": 0, "total": 0}
    total_cost = 0.0
    total_inv = 0
    token_sources = set()
    for key, entry in entries.items():
        tokens, tsource = _model_tokens(entry)
        if tsource == "mixed":
            token_sources.update(("monitoring", "billing"))
        elif tsource:
            token_sources.add(tsource)
        cost = round(entry["cost"], 6) if status["billing"] else None
        inv = entry["inv"]
        item = {"key": key, "name": entry["name"], "vendor": vendor_for(entry["name"]),
                "enabled": enabled.get(key, True), "isNew": is_new.get(key, False),
                "sources": sorted(entry["sources"]), "tokens": tokens,
                "invocations": inv["invocations"] if inv else None,
                "errorRate": ratio(inv["errors"], inv["invocations"]) if inv else None,
                "otherUsage": [{"unit": m, "amount": round(entry["other"][m], 3)} for m in OTHER_MEASURES
                               if entry["other"].get(m)],
                "cost": money(cost) if cost is not None else None, "share": None,
                "skus": sorted(({"sku": sku, "service": service, "measure": measure, "unit": unit,
                                 "amount": round(v["amount"], 3), "cost": money(v["cost"])}
                                for (sku, service, measure, unit), v in entry["skus"].items()),
                               key=lambda s: -(s["cost"] or 0))}
        models.append(item)
        if item["enabled"]:
            for slot in totals_tokens:
                totals_tokens[slot] += tokens[slot]
            total_cost += cost or 0.0
            total_inv += inv["invocations"] if inv else 0
    for item in models:
        if item["enabled"] and status["billing"] and total_cost:
            item["share"] = ratio(item["cost"] or 0.0, total_cost)
    models.sort(key=lambda m: (-(m["cost"] or 0), -m["tokens"]["total"], m["key"]))
    enabled_entries = [entries[m["key"]] for m in models if m["enabled"]]
    token_series = []
    cost_series = []
    for entry in enabled_entries:
        daily = _daily_tokens(entry)
        token_series.append(timeutil.series(entry["key"], entry["name"],
                                            timeutil.fold(rng, list(daily.items()), int)))
        if status["billing"]:
            cost_series.append(timeutil.series(entry["key"], entry["name"],
                                               [round(v, 2) for v in timeutil.fold(rng, list(entry["costDaily"].items()))]))
    any_tokens = bool(status["monitoring"] or status["billing"] or status["agy"])
    any_source = any_tokens or bool(status["invocations"])
    if not any_tokens:
        token_source = None
        totals_tokens = {"input": None, "output": None, "cached": None, "total": None}
        for item in models:
            item["tokens"] = {"input": None, "output": None, "cached": None, "total": None}
    elif token_sources == {"monitoring"}:
        token_source = "monitoring"
    elif "monitoring" in token_sources:
        token_source = "mixed"
    elif not token_sources:
        token_source = "monitoring" if status["monitoring"] else "billing"
    else:
        token_source = "billing"
    products = None
    product_cost = None
    if status["billing"]:
        products = []
        for prod in (data.get("products") or {}).values():
            products.append({"name": prod["name"], "cost": money(prod["cost"]),
                             "tokens": int(round(prod["tokens"])),
                             "models": [{"key": k, "name": entries[k]["name"] if k in entries else name}
                                        for k, name in sorted(prod["models"].items())]})
        products.sort(key=lambda p: (-(p["cost"] or 0), p["name"]))
        product_cost = money(sum(p["cost"] for p in (data.get("products") or {}).values()))
    # Aggregates, lists and timelines are null unless a contributing source succeeded.
    return {"currency": data["currency"], "models": models if any_source else None,
            "totals": {"tokens": totals_tokens, "cost": money(total_cost) if status["billing"] else None,
                       "productCost": product_cost,
                       "invocations": total_inv if status["invocations"] else None,
                       "modelsTracked": sum(1 for m in models if m["enabled"]) if any_source else None,
                       "modelsDiscovered": len(models) if any_source else None},
            "products": products,
            "tokenTrend": timeutil.timeline(rng, token_series) if any_tokens else None,
            "costTrend": timeutil.timeline(rng, cost_series) if status["billing"] else None,
            "tokenSource": token_source}


def usage(ctx):
    rng = ctx.range
    data = ctx.memo("models_usage", rng, lambda c: usage_data(c, rng))
    return build_usage(ctx, rng, data)


def catalog(ctx):
    today = timeutil.today_in(ctx.tz_name)
    rng = _catalog_range(ctx, today)
    data = ctx.memo("models_usage", rng, lambda c: usage_data(c, rng))
    entries = data["models"]
    enabled, is_new = model_flags(ctx, sorted(entries))
    out = []
    for key, entry in sorted(entries.items()):
        first = _first_seen(entry)
        out.append({"key": key, "name": entry["name"], "vendor": vendor_for(entry["name"]),
                    "enabled": enabled.get(key, True), "isNew": is_new.get(key, False),
                    "firstSeen": first.isoformat() if first else None, "sources": sorted(entry["sources"])})
    cat = ctx.settings["model_catalog"]
    sourced = any(bool(v) for v in data["status"].values())
    return {"models": out if sourced else None,
            "autoEnableNewModels": bool(cat.get("auto_enable_new_models", True)),
            "excludedSkuPatterns": list(cat.get("excluded_sku_patterns") or [])}
