"""Public model token pricing & live USD/KRW FX synchronization, change alerts, and daily monitor.

Priority hierarchy for cost calculation in the control plane:
1. BigQuery Billing Export (`gcp_billing_export_*`) when configured and readable.
2. Token-based reverse cost estimation: observed token usage (Cloud Monitoring, Antigravity
   inference logs, and telemetry connectors) multiplied by official Google Cloud public token
   prices and converted with live USD/KRW exchange rates.

Public pricing and FX rates are checked every early morning (04:00 KST) and can also be synced
on demand from the FinOps page or Cloud Scheduler (`POST /api/v1/finops/pricing/sync`). When unit
prices or FX rates change materially compared with the previous snapshot, persistent alerts are
recorded so administrators can review and acknowledge them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as html_lib
from html.parser import HTMLParser
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from .. import timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso
from . import models, pricing
from .base import require_identity_admin, to_float

LOG = logging.getLogger("controlplane.pricing_sync")

ALLOWED_PUBLIC_HOSTS = frozenset({
    "aistudio.google.com",
    "ai.google.dev",
    "cloud.google.com",
    "docs.cloud.google.com",
    "open.er-api.com",
    "api.frankfurter.dev",
})

GEMINI_PRICING_MD_URL = "https://aistudio.google.com/docs/pricing.md"
GEMINI_PRICING_HTML_URL = "https://ai.google.dev/gemini-api/docs/pricing"
AGENT_PLATFORM_PRICING_URL = "https://cloud.google.com/vertex-ai/generative-ai/pricing"
FX_PRIMARY_URL = "https://open.er-api.com/v6/latest/USD"
FX_SECONDARY_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=KRW"

MAX_HTTP_BYTES = 3 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 15
DEFAULT_USD_KRW = 1380.0
FX_ALERT_PCT_THRESHOLD = 1.0
PRICE_ALERT_PCT_THRESHOLD = 0.5
PRICE_ALERT_ABS_USD = 0.0005
EARLY_MORNING_HOUR = 4  # 04:00 local time (KST by default)

# Curated official standard online text rates (USD per 1M tokens, <= 200K context window)
# used as baseline fallback and merged with live public page + Cloud Billing Catalog sync.
BASELINE_USD_PER_MILLION = {
    "gemini-4-argon": {"input": 2.00, "output": 10.00, "cached": 0.10, "label": "Gemini 4 Argon"},
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75, "cached": 0.075, "label": "Gemini 3.8 Flash"},
    "gemini-3.6-flash": {"input": 0.75, "output": 3.75, "cached": 0.075, "label": "Gemini 3.6 Flash"},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "cached": 0.03, "label": "Gemini 3.5 Flash-Lite"},
    "gemini-3.1-pro": {"input": 2.00, "output": 12.00, "cached": 0.20, "label": "Gemini 3.1 Pro"},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50, "cached": 0.025, "label": "Gemini 3.1 Flash-Lite"},
    "gemini-3-pro": {"input": 2.00, "output": 12.00, "cached": 0.20, "label": "Gemini 3 Pro"},
    "gemini-3-flash": {"input": 0.50, "output": 3.00, "cached": 0.05, "label": "Gemini 3 Flash"},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cached": 0.125, "label": "Gemini 2.5 Pro"},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cached": 0.03, "label": "Gemini 2.5 Flash"},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cached": 0.01, "label": "Gemini 2.5 Flash-Lite"},
    "gemini-2-flash": {"input": 0.15, "output": 0.60, "cached": 0.0375, "label": "Gemini 2.0 Flash"},
    "gemini-2-flash-lite": {"input": 0.075, "output": 0.30, "cached": 0.01875, "label": "Gemini 2.0 Flash-Lite"},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00, "cached": 0.3125, "label": "Gemini 1.5 Pro"},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30, "cached": 0.01875, "label": "Gemini 1.5 Flash"},
    "claude-sonnet-4.5": {"input": 3.00, "output": 15.00, "cached": 0.30, "label": "Claude Sonnet 4.5"},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00, "cached": 0.30, "label": "Claude Sonnet 4"},
    "claude-opus-4.5": {"input": 15.00, "output": 75.00, "cached": 1.50, "label": "Claude Opus 4.5"},
    "claude-opus-4.1": {"input": 15.00, "output": 75.00, "cached": 1.50, "label": "Claude Opus 4.1"},
    "claude-haiku-4.5": {"input": 1.00, "output": 5.00, "cached": 0.10, "label": "Claude Haiku 4.5"},
    "claude-3.7-sonnet": {"input": 3.00, "output": 15.00, "cached": 0.30, "label": "Claude 3.7 Sonnet"},
    "claude-3.5-sonnet": {"input": 3.00, "output": 15.00, "cached": 0.30, "label": "Claude 3.5 Sonnet"},
    "claude-3.5-haiku": {"input": 0.80, "output": 4.00, "cached": 0.08, "label": "Claude 3.5 Haiku"},
    "text-embedding-005": {"input": 0.025, "output": 0.0, "cached": 0.0, "label": "Text Embedding 005"},
    "gemini-embedding": {"input": 0.15, "output": 0.0, "cached": 0.0, "label": "Gemini Embedding"},
    "gemini-embedding-2": {"input": 0.15, "output": 0.0, "cached": 0.0, "label": "Gemini Embedding 2"},
}

DOLLAR_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")
HEADING_RE = re.compile(r'^##\s+(.+?)(?:\s+\{%\s*id=(?:"|&quot;)([^"&]+)(?:"|&quot;)\s*%\})?\s*$', re.MULTILINE)
TAB_RE = re.compile(r'\{%\s*tab\s+title=(?:"|&quot;)([^"&]+)(?:"|&quot;)\s*%\}(.*?)\{%\s*/tab\s*%\}', re.DOTALL)
TABLE_BLOCK_RE = re.compile(r"\{%\s*table\s*%\}(.*?)\{%\s*/table\s*%\}", re.DOTALL)
NOSCRIPT_PRE_RE = re.compile(r"<noscript>\s*<pre>(.*?)</pre>\s*</noscript>", re.DOTALL | re.IGNORECASE)
NON_TEXT_MODEL_WORDS = ("image", "banana", "tts", "transcribe", "live", "native-audio", "robotics", "veo", "imagen")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_PUBLIC_HOSTS:
            raise urllib.error.URLError("redirect to untrusted host blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_public_url(url, timeout=HTTP_TIMEOUT_SECONDS):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_PUBLIC_HOSTS:
        raise ValueError("disallowed public URL host")
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    req = urllib.request.Request(url, headers={
        "User-Agent": "AIControlPlane-PricingMonitor/1.0",
        "Accept": "text/markdown,text/html,application/json;q=0.9,*/*;q=0.8",
    })
    with opener.open(req, timeout=timeout) as resp:  # nosec B310 - strict HTTPS host allowlist enforced above
        raw = resp.read(MAX_HTTP_BYTES + 1)
        if len(raw) > MAX_HTTP_BYTES:
            raw = raw[:MAX_HTTP_BYTES]
        return raw.decode("utf-8", errors="replace")


def _first_usd(text):
    if not text or "not available" in text.lower():
        return None
    # Prefer an explicit text rate when multiple modalities are listed on separate lines.
    for line in text.splitlines():
        low = line.lower()
        if "storage price" in low or "> 200k" in low or ">200k" in low or "&gt; 200k" in low:
            continue
        if "(audio" in low or "(video" in low or "(image" in low:
            continue
        m = DOLLAR_RE.search(line)
        if m:
            val = to_float(m.group(1))
            if val is not None and 0.0 <= val <= 10000.0:
                return val
    m = DOLLAR_RE.search(text)
    if m:
        val = to_float(m.group(1))
        if val is not None and 0.0 <= val <= 10000.0:
            return val
    return None


def parse_markdoc_pricing(raw_text, source="google_public_docs"):
    """Parses Markdoc/Markdown pricing sections from Google's official Gemini pricing docs."""
    if not raw_text:
        return {}
    ns = NOSCRIPT_PRE_RE.search(raw_text)
    text = html_lib.unescape(ns.group(1) if ns else raw_text)
    matches = list(HEADING_RE.finditer(text))
    parsed = {}
    for idx, match in enumerate(matches):
        raw_title = re.sub(r"[^\w\s.\-()]", "", match.group(1) or "").strip()
        raw_id = (match.group(2) or "").strip().lower()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        section = text[start:end]
        ident = raw_id or raw_title
        low_ident = ident.lower()
        if any(w in low_ident for w in NON_TEXT_MODEL_WORDS):
            continue
        key = models.normalize_model_id(raw_id) if raw_id else models.model_key(models.family_name(raw_title))
        if not key or not (key.startswith("gemini") or "embedding" in key):
            continue
        # Select the Standard tab if tabs exist; otherwise inspect the first table in the section.
        target_block = section
        tabs = TAB_RE.findall(section)
        if tabs:
            std = [body for title, body in tabs if title.strip().lower() == "standard"]
            if not std:
                continue
            target_block = std[0]
        tables = TABLE_BLOCK_RE.findall(target_block)
        if not tables:
            continue
        inp_price, out_price, cache_price = None, None, None
        for row_chunk in tables[0].split("---"):
            bullets = []
            cur = []
            for line in row_chunk.splitlines():
                stripped = line.strip()
                if stripped.startswith("* "):
                    if cur:
                        bullets.append("\n".join(cur))
                    cur = [stripped[2:].strip()]
                elif cur and stripped:
                    cur.append(stripped)
            if cur:
                bullets.append("\n".join(cur))
            if len(bullets) < 2:
                continue
            feature = bullets[0].lower()
            paid_cell = bullets[-1]
            if "input price" in feature and "caching" not in feature:
                inp_price = _first_usd(paid_cell)
            elif "output price" in feature:
                out_price = _first_usd(paid_cell)
            elif "context caching" in feature or "caching price" in feature:
                cache_price = _first_usd(paid_cell)
        if inp_price is not None or out_price is not None:
            label = models.display_name_for_id(raw_id) if raw_id else (raw_title or models.display_from_key(key))
            parsed[key] = {
                "input": inp_price,
                "output": out_price if out_price is not None else 0.0,
                "cached": cache_price,
                "source": source,
                "label": label,
            }
    return parsed


class _SimpleTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self._in_row = False
        self._in_cell = False
        self._cur_row = []
        self._cur_cell = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "tr":
            self._in_row = True
            self._cur_row = []
        elif t in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cur_cell = []

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._cur_row.append(" ".join("".join(self._cur_cell).split()))
        elif t == "tr" and self._in_row:
            self._in_row = False
            if self._cur_row:
                self.rows.append(self._cur_row)

    def handle_data(self, data):
        if self._in_cell:
            self._cur_cell.append(data)


def parse_html_table_pricing(html_text, source="agent_platform_public_page"):
    """Extracts standard text input/output per-1M-token USD prices from HTML pricing tables if present."""
    if not html_text or "<table" not in html_text.lower():
        return {}
    parser = _SimpleTableParser()
    try:
        parser.feed(html_text)
    except Exception:  # pylint: disable=broad-except
        return {}
    out = {}
    for row in parser.rows:
        if len(row) < 3:
            continue
        first = row[0]
        low = first.lower()
        if any(w in low for w in NON_TEXT_MODEL_WORDS) or "batch" in low or "> 200k" in low or ">200k" in low:
            continue
        fam = models.family_name(first)
        key = models.model_key(fam)
        if not key or not any(key.startswith(p) for p in ("gemini-", "claude-", "llama-", "mistral-")):
            continue
        prices = [_first_usd(c) for c in row[1:] if _first_usd(c) is not None]
        if len(prices) >= 2:
            out.setdefault(key, {
                "input": prices[0],
                "output": prices[1],
                "cached": prices[2] if len(prices) >= 3 and prices[2] < prices[0] else None,
                "source": source,
                "label": fam or models.display_from_key(key),
            })
    return out


def fetch_usd_krw(catalog_rate=None, catalog_as_of=None, previous_fx=None, fetcher=_fetch_public_url):
    """Resolves live USD/KRW exchange rate with multi-source fallback."""
    checked_at = now_iso()
    for source_id, url in (("open.er-api.com", FX_PRIMARY_URL), ("api.frankfurter.dev", FX_SECONDARY_URL)):
        try:
            body = fetcher(url)
            payload = json.loads(body)
            rates = payload.get("rates") or {}
            krw = to_float(rates.get("KRW"))
            if krw and 500.0 <= krw <= 5000.0:
                as_of = str(payload.get("time_last_update_utc") or payload.get("date") or checked_at)[:60]
                return {
                    "usdKrw": round(krw, 4),
                    "previousUsdKrw": to_float((previous_fx or {}).get("usdKrw")),
                    "source": source_id,
                    "asOf": as_of,
                    "updatedAt": checked_at,
                }, {"id": "fx", "url": url, "status": "ok", "checkedAt": checked_at,
                    "detail": "1 USD = %.2f KRW" % krw, "modelsFound": 0}
        except Exception as err:  # pylint: disable=broad-except
            LOG.info("FX source %s unavailable: %s", source_id, type(err).__name__)
    if catalog_rate and 500.0 <= catalog_rate <= 5000.0:
        return {
            "usdKrw": round(float(catalog_rate), 4),
            "previousUsdKrw": to_float((previous_fx or {}).get("usdKrw")),
            "source": "cloud_billing_catalog",
            "asOf": str(catalog_as_of or checked_at)[:60],
            "updatedAt": checked_at,
        }, {"id": "fx", "url": pricing.CATALOG_V1, "status": "ok", "checkedAt": checked_at,
            "detail": "1 USD = %.2f KRW (Cloud Billing Catalog)" % catalog_rate, "modelsFound": 0}
    prev_rate = to_float((previous_fx or {}).get("usdKrw"))
    fallback_rate = prev_rate or DEFAULT_USD_KRW
    return {
        "usdKrw": round(fallback_rate, 4),
        "previousUsdKrw": prev_rate,
        "source": "cached_fx" if prev_rate else "reference_default",
        "asOf": (previous_fx or {}).get("asOf") or checked_at,
        "updatedAt": checked_at,
    }, {"id": "fx", "url": FX_PRIMARY_URL, "status": "fallback", "checkedAt": checked_at,
        "detail": "Using fallback rate %.2f KRW" % fallback_rate, "modelsFound": 0}


def _alert_id(now_str, kind, target, field):
    digest = hashlib.sha256(("%s|%s|%s|%s" % (now_str[:16], kind, target, field)).encode("utf-8")).hexdigest()[:10]
    return "chg_%s" % digest


def diff_snapshots(prev_state, new_models, new_fx, now_str):
    """Generates change alerts when synced model prices or USD/KRW FX shift relative to previous sync."""
    prev_state = prev_state or {}
    if not prev_state.get("syncedAt"):
        # First sync seeds the baseline without firing noisy alerts.
        return []
    new_alerts = []
    prev_fx_val = to_float((prev_state.get("fx") or {}).get("usdKrw"))
    cur_fx_val = to_float((new_fx or {}).get("usdKrw"))
    if prev_fx_val and cur_fx_val and prev_fx_val > 0:
        pct = (cur_fx_val - prev_fx_val) / prev_fx_val * 100.0
        if abs(pct) >= FX_ALERT_PCT_THRESHOLD:
            new_alerts.append({
                "id": _alert_id(now_str, "fx", "USD/KRW", "usdKrw"),
                "at": now_str,
                "kind": "fx",
                "target": "USD/KRW",
                "label": "USD/KRW Exchange Rate",
                "field": "usdKrw",
                "before": round(prev_fx_val, 2),
                "after": round(cur_fx_val, 2),
                "pctChange": round(pct, 2),
                "currency": "KRW",
                "source": (new_fx or {}).get("source") or "",
                "acknowledgedAt": None,
                "acknowledgedBy": None,
            })
    prev_models = prev_state.get("models") or {}
    for key, cur in sorted((new_models or {}).items()):
        old = prev_models.get(key)
        if old is None:
            if cur.get("source") != "baseline":
                new_alerts.append({
                    "id": _alert_id(now_str, "new_model", key, "input"),
                    "at": now_str,
                    "kind": "new_model",
                    "target": key,
                    "label": cur.get("label") or models.display_from_key(key),
                    "field": "input",
                    "before": None,
                    "after": cur.get("input"),
                    "pctChange": None,
                    "currency": "USD",
                    "source": cur.get("source") or "",
                    "acknowledgedAt": None,
                    "acknowledgedBy": None,
                })
            continue
        for slot in ("input", "output", "cached"):
            b_val = to_float(old.get(slot))
            a_val = to_float(cur.get(slot))
            if b_val is None or a_val is None or b_val <= 0:
                continue
            delta = abs(a_val - b_val)
            pct = (a_val - b_val) / b_val * 100.0
            if delta >= PRICE_ALERT_ABS_USD and abs(pct) >= PRICE_ALERT_PCT_THRESHOLD:
                new_alerts.append({
                    "id": _alert_id(now_str, "model_price", key, slot),
                    "at": now_str,
                    "kind": "model_price",
                    "target": key,
                    "label": cur.get("label") or models.display_from_key(key),
                    "field": slot,
                    "before": round(b_val, 6),
                    "after": round(a_val, 6),
                    "pctChange": round(pct, 2),
                    "currency": "USD",
                    "source": cur.get("source") or "",
                    "acknowledgedAt": None,
                    "acknowledgedBy": None,
                })
    return new_alerts


def sync_pricing(app, actor="system", trigger="manual", fetcher=_fetch_public_url):
    """Fetches public pricing pages, Cloud Billing Catalog SKUs, and live USD/KRW FX; saves snapshot & alerts."""
    now_str = now_iso()
    settings_mgr = getattr(app, "settings", None)
    effective_settings, version = settings_mgr.snapshot() if settings_mgr else ({}, 1)
    prev_state = settings_mgr.pricing_state() if settings_mgr else {}

    merged = {}
    for key, base in BASELINE_USD_PER_MILLION.items():
        merged[key] = {
            "input": base.get("input"),
            "output": base.get("output"),
            "cached": base.get("cached"),
            "source": "baseline",
            "label": base.get("label") or models.display_from_key(key),
        }

    sources = []

    # 1. Official Gemini public pricing Markdown / HTML page
    md_models = {}
    try:
        md_text = fetcher(GEMINI_PRICING_MD_URL)
        md_models = parse_markdoc_pricing(md_text, source="google_public_docs")
        sources.append({"id": "gemini_public_docs", "url": GEMINI_PRICING_HTML_URL, "status": "ok",
                        "checkedAt": now_str, "detail": "Parsed %d models from official Gemini pricing docs" % len(md_models),
                        "modelsFound": len(md_models)})
    except Exception as err:  # pylint: disable=broad-except
        try:
            html_text = fetcher(GEMINI_PRICING_HTML_URL)
            md_models = parse_markdoc_pricing(html_text, source="google_public_docs")
            sources.append({"id": "gemini_public_docs", "url": GEMINI_PRICING_HTML_URL, "status": "ok",
                            "checkedAt": now_str, "detail": "Parsed %d models from Gemini pricing page" % len(md_models),
                            "modelsFound": len(md_models)})
        except Exception as err2:  # pylint: disable=broad-except
            LOG.info("Gemini public pricing fetch failed: %s / %s", type(err).__name__, type(err2).__name__)
            sources.append({"id": "gemini_public_docs", "url": GEMINI_PRICING_HTML_URL, "status": "error",
                            "checkedAt": now_str, "detail": "Public page unreachable (%s)" % type(err2).__name__,
                            "modelsFound": 0})
    for key, entry in md_models.items():
        cur = dict(merged.get(key) or {})
        cur.update({k: v for k, v in entry.items() if v is not None})
        cur["source"] = "google_public_docs"
        merged[key] = cur

    # 2. Official Google Cloud Agent Platform public pricing page
    try:
        ap_html = fetcher(AGENT_PLATFORM_PRICING_URL)
        ap_models = parse_html_table_pricing(ap_html, source="agent_platform_public_page")
        sources.append({"id": "agent_platform_page", "url": AGENT_PLATFORM_PRICING_URL, "status": "ok",
                        "checkedAt": now_str,
                        "detail": ("Page verified (%d static table models; dynamic SKUs read via Cloud Billing Catalog)"
                                   % len(ap_models)),
                        "modelsFound": len(ap_models)})
        for key, entry in ap_models.items():
            cur = dict(merged.get(key) or {})
            cur.update({k: v for k, v in entry.items() if v is not None})
            cur["source"] = "agent_platform_public_page"
            merged[key] = cur
    except Exception as err:  # pylint: disable=broad-except
        sources.append({"id": "agent_platform_page", "url": AGENT_PLATFORM_PRICING_URL, "status": "error",
                        "checkedAt": now_str, "detail": "Public page unreachable (%s)" % type(err).__name__,
                        "modelsFound": 0})

    # 3. Cloud Billing Catalog API (official SKU list for Agent Platform & Partner models)
    cat_rate, cat_eff = None, None
    if hasattr(app, "gcp") and effective_settings:
        from .base import Ctx
        from ..security import Identity
        sys_ctx = Ctx(app, effective_settings, version, Identity("system", "admin", "system", "system:pricing"))
        try:
            cat_krw, cat_rate, cat_eff = pricing.catalog_prices(sys_ctx, "KRW")
            cat_count = 0
            if cat_rate and cat_rate > 0:
                for key, entry in cat_krw.items():
                    inp_usd = round(entry["input"] / cat_rate, 6) if entry.get("input") else None
                    out_usd = round(entry["output"] / cat_rate, 6) if entry.get("output") else None
                    if inp_usd is None and out_usd is None:
                        continue
                    cur = dict(merged.get(key) or {"cached": None, "label": models.display_from_key(key)})
                    if inp_usd is not None:
                        cur["input"] = inp_usd
                    if out_usd is not None:
                        cur["output"] = out_usd
                        if cur.get("cached") is None and inp_usd is not None:
                            cur["cached"] = round(inp_usd * 0.1, 6)
                    cur["source"] = "cloud_billing_catalog"
                    cur.setdefault("label", models.display_from_key(key))
                    merged[key] = cur
                    cat_count += 1
            sources.append({"id": "cloud_billing_catalog", "url": pricing.CATALOG_V1, "status": "ok",
                            "checkedAt": now_str, "detail": "Synced %d SKU model families" % cat_count,
                            "modelsFound": cat_count})
        except GcpError as err:
            sources.append({"id": "cloud_billing_catalog", "url": pricing.CATALOG_V1, "status": "skipped",
                            "checkedAt": now_str, "detail": "Catalog API %s (using public docs + baseline)" % err.kind,
                            "modelsFound": 0})
        except Exception as err:  # pylint: disable=broad-except
            sources.append({"id": "cloud_billing_catalog", "url": pricing.CATALOG_V1, "status": "error",
                            "checkedAt": now_str, "detail": type(err).__name__, "modelsFound": 0})

    # 4. Live USD/KRW FX rate
    fx_obj, fx_source = fetch_usd_krw(catalog_rate=cat_rate, catalog_as_of=cat_eff,
                                      previous_fx=prev_state.get("fx"), fetcher=fetcher)
    sources.append(fx_source)

    # 5. Diff against previous persisted snapshot
    added_alerts = diff_snapshots(prev_state, merged, fx_obj, now_str)
    changed = (merged != (prev_state.get("models") or {})) or (
        to_float((fx_obj or {}).get("usdKrw")) != to_float(((prev_state.get("fx") or {}).get("usdKrw"))))

    if settings_mgr is not None:
        def update(state):
            existing = list(state.get("alerts") or [])
            existing_ids = {a.get("id") for a in existing}
            for a in added_alerts:
                if a["id"] not in existing_ids:
                    existing.append(a)
            state["syncedAt"] = now_str
            state["syncedBy"] = actor
            state["trigger"] = trigger
            state["fx"] = fx_obj
            state["models"] = merged
            state["sources"] = sources
            state["alerts"] = existing[-100:]
            state["lastError"] = None
            return state

        bump = bool(changed or added_alerts or not prev_state.get("syncedAt"))
        settings_mgr.mutate_pricing(update, actor, bump_cache=bump)
        saved = settings_mgr.pricing_state()
        if bump and hasattr(app, "on_settings_changed"):
            app.on_settings_changed()
    else:
        saved = {"syncedAt": now_str, "syncedBy": actor, "trigger": trigger, "fx": fx_obj,
                 "models": merged, "sources": sources, "alerts": added_alerts, "lastError": None}

    return format_pricing_response(saved, effective_settings, new_alerts_count=len(added_alerts))


def effective_fx(ctx):
    """Returns {"usdKrw": float, "source": str, "asOf": str, "updatedAt": str}."""
    settings_mgr = getattr(getattr(ctx, "app", None), "settings", None)
    state = settings_mgr.pricing_state() if settings_mgr is not None else {}
    fx = state.get("fx")
    if isinstance(fx, dict) and to_float(fx.get("usdKrw")):
        return fx
    return {"usdKrw": DEFAULT_USD_KRW, "previousUsdKrw": None, "source": "reference_default",
            "asOf": None, "updatedAt": None}


def preferred_currency(ctx, explicit=None):
    raw = (explicit or ctx.params.get("currency") or "").strip().upper()
    if raw in ("KRW", "USD"):
        return raw
    gov_cur = ((ctx.settings.get("governance") or {}).get("antigravity") or {}).get("currency")
    if gov_cur in ("KRW", "USD"):
        return gov_cur
    if (ctx.settings.get("branding") or {}).get("default_locale") == "ko" or ctx.tz_name == "Asia/Seoul":
        return "KRW"
    return "USD"


def effective_token_rates(ctx, currency=None):
    """Returns ({model_key: {"input", "output", "cached", "source", "label"}}, currency, fx_dict)
    where rates are per 1M tokens in `currency` (KRW or USD)."""
    target = preferred_currency(ctx, currency)
    fx = effective_fx(ctx)
    factor = float(fx["usdKrw"]) if target == "KRW" else 1.0
    settings_mgr = getattr(getattr(ctx, "app", None), "settings", None)
    synced = (settings_mgr.pricing_state().get("models") or {}) if settings_mgr is not None else {}

    combined = {}
    for key, base in BASELINE_USD_PER_MILLION.items():
        combined[key] = dict(base, source="baseline")
    for key, item in synced.items():
        cur = dict(combined.get(key) or {"label": models.display_from_key(key)})
        cur.update({k: v for k, v in item.items() if v is not None})
        combined[key] = cur

    out = {}
    for key, item in combined.items():
        inp = to_float(item.get("input"))
        outp = to_float(item.get("output"))
        cached = to_float(item.get("cached"))
        if cached is None and inp is not None:
            cached = inp * 0.1
        out[key] = {
            "input": (inp * factor) if inp is not None else 0.0,
            "output": (outp * factor) if outp is not None else 0.0,
            "cached": (cached * factor) if cached is not None else 0.0,
            "inputUsd": inp,
            "outputUsd": outp,
            "cachedUsd": cached,
            "source": item.get("source") or "baseline",
            "label": item.get("label") or models.display_from_key(key),
        }
    return out, target, fx


def lookup_model_rate(rates, raw_model_id):
    """Finds rate dict for a model ID or normalized family key, with family prefix fallback."""
    key = models.normalize_model_id(str(raw_model_id or ""))
    if key in rates:
        return key, rates[key]
    for cand_key, rate in rates.items():
        if key.startswith(cand_key) or cand_key.startswith(key):
            return key, rate
    if "flash-lite" in key and "gemini-2.5-flash-lite" in rates:
        return key, rates["gemini-2.5-flash-lite"]
    if "flash" in key and "gemini-2.5-flash" in rates:
        return key, rates["gemini-2.5-flash"]
    if "pro" in key and "gemini-2.5-pro" in rates:
        return key, rates["gemini-2.5-pro"]
    return key, None


def pricing_alert_summary(settings_mgr):
    """Compact summary embedded in /api/v1/bootstrap and /api/v1/finops/summary."""
    if settings_mgr is None:
        return {"syncedAt": None, "fx": None, "unacknowledgedCount": 0, "alerts": []}
    state = settings_mgr.pricing_state()
    unack = [a for a in (state.get("alerts") or []) if not a.get("acknowledgedAt")]
    unack.sort(key=lambda a: a.get("at") or "", reverse=True)
    return {
        "syncedAt": state.get("syncedAt"),
        "trigger": state.get("trigger"),
        "fx": state.get("fx"),
        "unacknowledgedCount": len(unack),
        "alerts": unack[:10],
    }


def format_pricing_response(state, effective_settings, new_alerts_count=0):
    state = state or {}
    fx = state.get("fx") or {"usdKrw": DEFAULT_USD_KRW, "previousUsdKrw": None,
                             "source": "reference_default", "asOf": None, "updatedAt": None}
    rate = to_float(fx.get("usdKrw")) or DEFAULT_USD_KRW
    raw_models = state.get("models") or {}
    merged = {}
    for k, b in BASELINE_USD_PER_MILLION.items():
        merged[k] = dict(b, source="baseline")
    merged.update(raw_models)

    model_rows = []
    for key, item in sorted(merged.items()):
        inp = to_float(item.get("input"))
        outp = to_float(item.get("output"))
        cached = to_float(item.get("cached"))
        model_rows.append({
            "key": key,
            "label": item.get("label") or models.display_from_key(key),
            "vendor": models.vendor_for(item.get("label") or key),
            "source": item.get("source") or "baseline",
            "usdPerMillion": {"input": inp, "output": outp, "cached": cached},
            "krwPerMillion": {
                "input": round(inp * rate, 2) if inp is not None else None,
                "output": round(outp * rate, 2) if outp is not None else None,
                "cached": round(cached * rate, 2) if cached is not None else None,
            },
        })
    alerts = list(state.get("alerts") or [])
    alerts.sort(key=lambda a: a.get("at") or "", reverse=True)
    unack = [a for a in alerts if not a.get("acknowledgedAt")]
    tz_name = ((effective_settings or {}).get("workspace") or {}).get("timezone") or "Asia/Seoul"
    if tz_name == "UTC":
        tz_name = "Asia/Seoul"
    return {
        "syncedAt": state.get("syncedAt"),
        "syncedBy": state.get("syncedBy"),
        "trigger": state.get("trigger"),
        "fx": fx,
        "models": model_rows,
        "sources": state.get("sources") or [],
        "alerts": alerts[:50],
        "unacknowledgedCount": len(unack),
        "newAlertsCount": new_alerts_count,
        "schedule": {
            "enabled": True,
            "dailyHourLocal": EARLY_MORNING_HOUR,
            "timezone": tz_name,
            "description": "%02d:00 %s daily automatic check" % (EARLY_MORNING_HOUR, tz_name),
        },
    }


def get_pricing(ctx):
    settings_mgr = getattr(ctx.app, "settings", None)
    state = settings_mgr.pricing_state() if settings_mgr is not None else {}
    return format_pricing_response(state, ctx.settings)


def trigger_sync(ctx, scheduled=False):
    is_sched = bool(scheduled or getattr(ctx.identity, "auth_mode", "") == "scheduler")
    if not is_sched:
        require_identity_admin(ctx)
    actor = "system:cloud-scheduler" if is_sched else (ctx.identity.email or "admin")
    trigger = "scheduled" if is_sched else "manual"
    res = sync_pricing(ctx.app, actor=actor, trigger=trigger)
    if hasattr(ctx.app, "on_settings_changed"):
        ctx.app.on_settings_changed()
    return res


def acknowledge_alerts(ctx):
    require_identity_admin(ctx)
    settings_mgr = getattr(ctx.app, "settings", None)
    if settings_mgr is None:
        return {"acknowledged": 0, "unacknowledgedCount": 0, "alerts": []}
    target_id = str((ctx.body or {}).get("id") or "").strip()
    ack_all = bool((ctx.body or {}).get("all"))
    if not target_id and not ack_all:
        raise ApiError("bad_request", "Specify 'id' or 'all': true to acknowledge pricing alerts.")
    now_str = now_iso()
    actor = ctx.identity.email or "admin"

    def apply(state):
        count = 0
        for item in state.get("alerts") or []:
            if item.get("acknowledgedAt"):
                continue
            if ack_all or item.get("id") == target_id:
                item["acknowledgedAt"] = now_str
                item["acknowledgedBy"] = actor
                count += 1
        return count

    acked = settings_mgr.mutate_pricing(apply, actor, bump_cache=True)
    if hasattr(ctx.app, "on_settings_changed"):
        ctx.app.on_settings_changed()
    summary = pricing_alert_summary(settings_mgr)
    return {"acknowledged": acked, "unacknowledgedCount": summary["unacknowledgedCount"], "alerts": summary["alerts"]}


class PricingMonitor:
    """Background daemon that performs initial sync (if stale) and daily 04:00 local-time checks."""

    def __init__(self, app):
        self.app = app
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="pricing-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _monitor_tz(self):
        try:
            eff, _ = self.app.settings.snapshot()
            tz_name = (eff.get("workspace") or {}).get("timezone") or "Asia/Seoul"
            if tz_name == "UTC":
                tz_name = "Asia/Seoul"
            return timeutil.get_zone(tz_name)
        except Exception:  # pylint: disable=broad-except
            return timeutil.get_zone("Asia/Seoul")

    def _needs_initial_sync(self):
        state = self.app.settings.pricing_state()
        synced_at = state.get("syncedAt")
        if not synced_at:
            return True
        from .base import parse_rfc3339
        parsed = parse_rfc3339(synced_at)
        if parsed is None:
            return True
        age_hours = (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds() / 3600.0
        return age_hours >= 20.0

    def _needs_early_morning_sync(self):
        tz = self._monitor_tz()
        now_local = dt.datetime.now(tz)
        if now_local.hour != EARLY_MORNING_HOUR:
            return False
        state = self.app.settings.pricing_state()
        synced_at = state.get("syncedAt")
        if not synced_at:
            return True
        from .base import parse_rfc3339
        parsed = parse_rfc3339(synced_at)
        if parsed is None:
            return True
        return parsed.astimezone(tz).date() < now_local.date()

    def _loop(self):
        if self._stop.wait(3.0):
            return
        try:
            if self._needs_initial_sync():
                LOG.info("running startup public pricing & FX sync")
                sync_pricing(self.app, actor="system:startup", trigger="startup")
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("startup pricing sync failed: %s", type(err).__name__)
        while not self._stop.wait(900.0):  # check every 15 minutes
            try:
                if self._needs_early_morning_sync():
                    LOG.info("running daily early-morning public pricing & FX monitor")
                    sync_pricing(self.app, actor="system:daily-monitor", trigger="scheduled")
            except Exception as err:  # pylint: disable=broad-except
                LOG.warning("daily pricing monitor failed: %s", type(err).__name__)
