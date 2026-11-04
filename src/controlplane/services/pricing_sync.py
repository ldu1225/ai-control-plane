"""Official token prices and exchange rates: daily synchronization, change alerts and price lookups.

Cost priority in the control plane:
1. BigQuery billing export (actual cost) when it is configured and readable.
2. Token-based estimate when it is not (most customers cannot read the billing account): observed tokens
   (Cloud Monitoring publisher metrics, Antigravity inference logs) multiplied by official per-token prices.

Prices come only from official sources, in this order of precedence:
   - administrator overrides (pricing.model_overrides) and aliases (pricing.model_aliases);
   - Cloud Billing Catalog API list prices of the services named in pricing.catalog_services (and partner
     model services when pricing.include_partner_models is on); service ids are discovered by display name;
   - the Google pricing pages listed in pricing.public_sources, for models the catalog does not list yet.
   There is no built-in price table: a model without an official price stays unpriced and is reported.

Exchange rates (USD to any currency): the conversion rate Google publishes in the Cloud Billing Catalog for the
requested currency, then the public feeds in pricing.fx_sources. Without any rate, amounts stay in USD.

The check runs daily at pricing.sync_time in pricing.sync_timezone (default: the workspace timezone) from the
maintenance tick (Cloud Scheduler job plus an in-process monitor) and on demand from the FinOps page
(POST /api/v1/finops/pricing/sync). Material changes against the previous snapshot are recorded as alerts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as html_lib
from html.parser import HTMLParser
import ipaddress
import json
import logging
import re
import socket
import statistics
import urllib.error
import urllib.parse
import urllib.request

from .. import config, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso
from . import models, pricing
from .base import parse_rfc3339, require_identity_admin, to_float

LOG = logging.getLogger("controlplane.pricing_sync")

MAX_HTTP_BYTES = 3 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 15
MAX_CATALOG_SERVICE_PAGES = 4
MAX_SKU_PAGES = 8
# A per-token price above this is a per-request/per-clip SKU, not a token rate (USD per 1M tokens).
MAX_TOKEN_USD_PER_MILLION = 500.0

DOLLAR_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")
INPUT_TOK_PRICE_RE = re.compile(r"(?i)input(?:\s*\([^)]*\))?\s*tokens?\s*:\s*\$\s*(\d+(?:\.\d+)?)")
OUTPUT_TOK_PRICE_RE = re.compile(r"(?i)output(?:\s*\([^)]*\))?\s*tokens?\s*:\s*\$\s*(\d+(?:\.\d+)?)")
HEADING_RE = re.compile(r'^##\s+(.+?)(?:\s+\{%\s*id=(?:"|&quot;)([^"&]+)(?:"|&quot;)\s*%\})?\s*$', re.MULTILINE)
TAB_RE = re.compile(r'\{%\s*tab\s+title=(?:"|&quot;)([^"&]+)(?:"|&quot;)\s*%\}(.*?)\{%\s*/tab\s*%\}', re.DOTALL)
TABLE_BLOCK_RE = re.compile(r"\{%\s*table\s*%\}(.*?)\{%\s*/table\s*%\}", re.DOTALL)
NOSCRIPT_PRE_RE = re.compile(r"<noscript>\s*<pre>(.*?)</pre>\s*</noscript>", re.DOTALL | re.IGNORECASE)
MONTH_NUMS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
SCHEDULE_DATE_RE = re.compile(
    r"(?i)(?:\b|(?<=[a-z0-9]))(through|starting|effective)\s+"
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})"
)
# Words of price-page sections that are non-token media or image-only Markdoc sections without input+output tables.
NON_TEXT_MODEL_WORDS = ("image", "banana", "robotics", "veo", "imagen", "lyria", "music", "song", "clip", "track", "chirp")


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def _pricing_settings(settings):
    return (settings or {}).get("pricing") or config.DEFAULTS["pricing"]


def schedule_timezone(settings):
    p = _pricing_settings(settings)
    return p.get("sync_timezone") or ((settings or {}).get("workspace") or {}).get("timezone") or "UTC"


def schedule_info(settings, now=None):
    """{"time": "HH:MM", "timezone": tz, "nextRunAt": iso} of the daily price check."""
    p = _pricing_settings(settings)
    tz_name = schedule_timezone(settings)
    hh, mm = (int(x) for x in (p.get("sync_time") or "00:00").split(":"))
    tz = timeutil.get_zone(tz_name)
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(tz)
    run = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if run <= local:
        run += dt.timedelta(days=1)
    return {"time": "%02d:%02d" % (hh, mm), "timezone": tz_name,
            "nextRunAt": run.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def last_scheduled_time(settings, now=None):
    """The most recent scheduled run time (UTC) at or before now."""
    p = _pricing_settings(settings)
    hh, mm = (int(x) for x in (p.get("sync_time") or "00:00").split(":"))
    tz = timeutil.get_zone(schedule_timezone(settings))
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(tz)
    run = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if run > local:
        run -= dt.timedelta(days=1)
    return run.astimezone(dt.timezone.utc)


def is_due(settings, synced_at, now=None):
    """True when no sync happened since the most recent scheduled time."""
    parsed = parse_rfc3339(synced_at) if synced_at else None
    return parsed is None or parsed < last_scheduled_time(settings, now)


def _alias_map(settings):
    out = {}
    for item in _pricing_settings(settings).get("model_aliases") or []:
        left, _, right = str(item).partition("=")
        if left and right:
            out[models.normalize_model_id(left)] = models.normalize_model_id(right)
    return out


# ---------------------------------------------------------------------------
# Public page fetching (official Google documentation and exchange-rate feeds only)
# ---------------------------------------------------------------------------

def allowed_hosts(settings):
    p = _pricing_settings(settings)
    hosts = set()
    for url in list(p.get("public_sources") or []) + list(p.get("fx_sources") or []):
        host = config.public_https_host(url)
        if host:
            hosts.add(host)
    return frozenset(hosts)


def _public_address(host):
    """True when every address of host is a public unicast address (no loopback, private or link-local)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return bool(infos)


def make_fetcher(hosts):
    """A fetcher that reads https URLs on the configured hosts only (redirects included)."""
    hosts = frozenset(h.lower() for h in hosts)

    class _SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            parsed = urllib.parse.urlparse(newurl)
            if parsed.scheme != "https" or (parsed.hostname or "").lower() not in hosts:
                raise urllib.error.URLError("redirect to an unlisted host blocked")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    def fetch(url, timeout=HTTP_TIMEOUT_SECONDS):
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in hosts:
            raise ValueError("URL host is not one of the configured price or exchange-rate sources")
        if not _public_address(host):
            raise ValueError("URL host does not resolve to a public address")
        opener = urllib.request.build_opener(_SafeRedirect())
        req = urllib.request.Request(url, headers={
            "User-Agent": "AIControlPlane-PricingMonitor/2.0",
            "Accept": "text/markdown,text/html,application/json;q=0.9,*/*;q=0.8",
        })
        with opener.open(req, timeout=timeout) as resp:  # nosec B310 - https and configured hosts only
            raw = resp.read(MAX_HTTP_BYTES + 1)
            return raw[:MAX_HTTP_BYTES].decode("utf-8", errors="replace")

    return fetch


def _page_source_id(url):
    parsed = urllib.parse.urlparse(url or "")
    host = (parsed.hostname or "page").lower()
    path = (parsed.path or "").strip("/").lower()
    if host == "cloud.google.com" and path:
        first = path.split("/", 1)[0]
        if first in ("text-to-speech", "speech-to-text"):
            return "page:%s/%s" % (host, first)
    return "page:%s" % host


# ---------------------------------------------------------------------------
# Price page parsers
# ---------------------------------------------------------------------------

def _schedule_active(text, today=None):
    """False when `text` carries an explicit effective-date window ('through Dec 31, 2026' or 'starting Jan 1, 2027')
    that does not include `today`."""
    if not text:
        return True
    m = SCHEDULE_DATE_RE.search(text)
    if not m:
        return True
    kind = m.group(1).lower()
    mon = MONTH_NUMS.get(m.group(2)[:3].lower())
    try:
        bound = dt.date(int(m.group(4)), mon, int(m.group(3)))
    except (TypeError, ValueError):
        return True
    today = today or dt.datetime.now(dt.timezone.utc).date()
    if kind == "through":
        return today <= bound
    return today >= bound


def _first_usd(text, prefer_audio=False, allow_media=False):
    if not text or "not available" in text.lower():
        return None
    if prefer_audio:
        for line in text.splitlines():
            low = line.lower()
            if "storage price" in low or "> 200k" in low or ">200k" in low or "&gt; 200k" in low:
                continue
            if "audio" in low:
                m = DOLLAR_RE.search(line)
                if m:
                    val = to_float(m.group(1))
                    if val is not None and 0.0 <= val <= 10000.0:
                        return val
    for line in text.splitlines():
        low = line.lower()
        if "storage price" in low or "> 200k" in low or ">200k" in low or "&gt; 200k" in low:
            continue
        if not allow_media and ("(audio" in low or "(video" in low or "(image" in low):
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


def parse_markdoc_pricing(raw_text, source="public_page"):
    """Standard (online, <= 200K context) token prices from Markdoc/Markdown price pages."""
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
        ident = (raw_id or raw_title).lower()
        if any(w in ident for w in NON_TEXT_MODEL_WORDS):
            continue
        key = models.normalize_model_id(raw_id) if raw_id else models.model_key(models.family_name(raw_title))
        if not key or models.is_non_token_model(key) or not models.parse_sku(key.replace("-", " ")):
            continue
        is_tts = key.endswith("-tts") or "-tts-" in key
        is_audio = key.endswith("-live") or "-live-" in key or "native-audio" in key
        is_transcribe = "transcribe" in key and "translate" not in key
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
            bullets, cur = [], []
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
                inp_price = _first_usd(paid_cell, prefer_audio=(is_audio or is_transcribe))
            elif "output price" in feature:
                out_price = _first_usd(paid_cell, prefer_audio=(is_tts or is_audio))
            elif "context caching" in feature or "caching price" in feature:
                cache_price = _first_usd(paid_cell, prefer_audio=(is_audio or is_transcribe))
        entry = {"input": inp_price, "output": out_price, "cached": cache_price, "source": source,
                 "label": models.display_name_for_id(raw_id) if raw_id else (raw_title or models.display_from_key(key))}
        if valid_token_entry(key, entry):
            parsed[key] = entry
    return parsed


class _SimpleTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.tables = []
        self._cur_table = None
        self._in_row = False
        self._in_cell = False
        self._cur_row = []
        self._cur_cell = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "table":
            self._cur_table = []
        elif t == "tr":
            self._in_row = True
            self._cur_row = []
        elif t in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cur_cell = []
        elif self._in_cell and t in ("br", "p", "div", "li"):
            self._cur_cell.append(" ")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._cur_row.append(" ".join("".join(self._cur_cell).split()))
        elif t in ("p", "div", "li") and self._in_cell:
            self._cur_cell.append(" ")
        elif t == "tr" and self._in_row:
            self._in_row = False
            if self._cur_row:
                self.rows.append(self._cur_row)
                if self._cur_table is not None:
                    self._cur_table.append(self._cur_row)
        elif t == "table":
            if self._cur_table:
                self.tables.append(self._cur_table)
            self._cur_table = None

    def handle_data(self, data):
        if self._in_cell:
            self._cur_cell.append(data)


def _parse_rowspan_model_table(table_rows, source, today, out):
    """Parses Vertex AI style `[Model, Type, <= 200K price, ...]` rowspan tables."""
    cur_info = None
    cur_active = True
    acc = {}
    for row in table_rows[1:]:
        if not row:
            continue
        first_low = row[0].lower()
        is_metric_cell = any(w in first_low for w in ("input", "output", "caching", "cached", "grounding"))
        cand = None if is_metric_cell else models.parse_sku(row[0])
        if cand and not cand.get("product") and not models.is_non_token_model(cand["key"]):
            cur_info = cand
            cur_active = _schedule_active(row[0], today)
            type_cell = row[1] if len(row) > 1 else ""
            price_cell = row[2] if len(row) > 2 else ""
        elif cur_info is not None and len(row) >= 2:
            type_cell = row[0]
            price_cell = row[1]
        else:
            continue
        if not cur_active:
            continue
        val = _first_usd(price_cell, allow_media=True)
        if val is None or val > MAX_TOKEN_USD_PER_MILLION:
            continue
        key = cur_info["key"]
        tlow = type_cell.lower()
        rec = acc.setdefault(key, {"label": cur_info["name"], "source": source})
        is_tts = key.endswith("-tts") or "-tts-" in key
        is_live = key.endswith("-live") or "-live-" in key or "native-audio" in key
        is_transcribe = "transcribe" in key and "translate" not in key
        is_img = key.endswith("-image") or "-image-" in key or "banana" in key
        if "cach" in tlow and "storage" not in tlow:
            prefer = "audio" in tlow if (is_live or is_transcribe) else "audio" not in tlow
            if prefer or rec.get("cached") is None:
                rec["cached"] = val
        elif "input" in tlow and "cach" not in tlow:
            prefer = "audio" in tlow if (is_live or is_transcribe) else ("text" in tlow or "audio" not in tlow)
            if prefer or rec.get("input") is None:
                rec["input"] = val
        elif "output" in tlow:
            if is_tts or is_live:
                prefer = "audio" in tlow
            elif is_img:
                prefer = "image" in tlow
            else:
                prefer = "text" in tlow or ("audio" not in tlow and "image" not in tlow)
            if prefer or rec.get("output") is None:
                rec["output"] = val
    for key, rec in acc.items():
        if valid_token_entry(key, rec):
            out.setdefault(key, rec)


def parse_html_table_pricing(html_text, source="public_page", today=None):
    """Standard online input/output prices (USD per 1M tokens) from HTML price tables."""
    if not html_text or "<table" not in html_text.lower():
        return {}
    parser = _SimpleTableParser()
    try:
        parser.feed(html_text)
    except Exception:  # pylint: disable=broad-except
        return {}
    out = {}
    svc_hint = "Cloud Text-to-Speech API" if "text-to-speech" in (source or "") else None
    tables = parser.tables or ([parser.rows] if parser.rows else [])
    for table_rows in tables:
        if not table_rows:
            continue
        hdr_low = " ".join(table_rows[0]).lower()
        if any(w in hdr_low for w in ("priority", "flex", "batch", "provisioned")):
            continue
        if len(table_rows[0]) >= 3 and "type" in table_rows[0][1].lower() and "model" in table_rows[0][0].lower():
            _parse_rowspan_model_table(table_rows, source, today, out)
            continue
        for row in table_rows:
            if len(row) < 2 or not _schedule_active(row[0], today):
                continue
            first = row[0]
            rest_text = " ".join(row[1:])
            in_m = INPUT_TOK_PRICE_RE.search(rest_text)
            out_m = OUTPUT_TOK_PRICE_RE.search(rest_text)
            if in_m or out_m:
                info = models.parse_sku(first, svc_hint)
                if info and not info.get("product") and not models.is_non_token_model(info["key"]):
                    entry = {"input": to_float(in_m.group(1)) if in_m else None,
                             "output": to_float(out_m.group(1)) if out_m else None,
                             "cached": None, "source": source, "label": info["name"]}
                    if valid_token_entry(info["key"], entry):
                        out.setdefault(info["key"], entry)
                continue
            if len(row) < 3:
                continue
            low = first.lower()
            if any(w in low for w in NON_TEXT_MODEL_WORDS) or "batch" in low or "> 200k" in low or ">200k" in low:
                continue
            info = models.parse_sku(first)
            if not info or info.get("product") or models.is_non_token_model(info["key"]):
                continue
            prices = [p for p in (_first_usd(c) for c in row[1:]) if p is not None]
            if len(prices) >= 2:
                entry = {"input": prices[0], "output": prices[1],
                         "cached": prices[2] if len(prices) >= 3 and prices[2] < prices[0] else None,
                         "source": source, "label": info["name"]}
                if valid_token_entry(info["key"], entry):
                    out.setdefault(info["key"], entry)
    return out


def parse_price_page(text, source):
    parsed = parse_markdoc_pricing(text, source=source)
    return parsed or parse_html_table_pricing(text, source=source)


def valid_token_entry(key, item):
    """True for a realistic per-1M-token model rate with a clean normalized model id."""
    if not isinstance(item, dict) or not key or not config.MODEL_ID_RE.match(key):
        return False
    if models.is_non_token_model(key):
        return False
    if sum(1 for p in key.split("-") if p in models.FAMILIES and p != "embedding") > 1:
        return False
    vals = [to_float(item.get(k)) for k in ("input", "output")]
    if all(v is None for v in vals):
        return False
    return all(v is None or 0 <= v <= MAX_TOKEN_USD_PER_MILLION for v in vals)


# ---------------------------------------------------------------------------
# Cloud Billing Catalog (official SKU list prices)
# ---------------------------------------------------------------------------

def list_catalog_services(ctx):
    """[{id, name}] of every public Cloud Billing Catalog service."""
    out = []
    token = None
    for _ in range(MAX_CATALOG_SERVICE_PAGES):
        params = {"pageSize": 5000}
        if token:
            params["pageToken"] = token
        data = ctx.rest("GET", "%s/services" % pricing.CATALOG_V1, params=params, timeout=60)
        for svc in data.get("services") or []:
            sid, name = svc.get("serviceId"), svc.get("displayName")
            if isinstance(sid, str) and isinstance(name, str) and sid and name:
                out.append({"id": sid[:40], "name": name[:120]})
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def select_model_services(services, settings):
    """Catalog services that bill model tokens or AI media/speech units: display names matching
    pricing.catalog_services, plus partner model services when enabled."""
    p = _pricing_settings(settings)
    patterns = [s.lower() for s in p.get("catalog_services") or [] if s]
    out = []
    for svc in services or []:
        low = svc["name"].lower()
        if any(pat in low for pat in patterns):
            out.append(svc)
        elif p.get("include_partner_models", True) and models._is_model_service(svc["name"]):
            info = models.parse_sku(svc["name"])
            if info and not info.get("product"):
                out.append(svc)
    seen, unique = set(), []
    for svc in out:
        if svc["id"] not in seen:
            seen.add(svc["id"])
            unique.append(svc)
    return unique


def select_storage_service(services, settings):
    want = (_pricing_settings(settings).get("storage_service") or "").strip().lower()
    if not want:
        return None
    exact = [s for s in services or [] if s["name"].strip().lower() == want]
    return exact[0] if exact else None


def _service_skus(ctx, service_id, currency="USD", max_pages=MAX_SKU_PAGES):
    token = None
    for _ in range(max_pages):
        params = {"currencyCode": currency, "pageSize": 5000}
        if token:
            params["pageToken"] = token
        data = ctx.rest("GET", "%s/services/%s/skus" % (pricing.CATALOG_V1, service_id), params=params, timeout=60)
        for sku in data.get("skus") or []:
            yield sku
        token = data.get("nextPageToken")
        if not token:
            break


def _clean_unit_label(desc):
    txt = re.sub(r"(?i)\s*-\s*(?:online\s+)?predictions?\s*$", "", (desc or "").strip())
    txt = re.sub(r"(?i)\s+(?:in|across)\s+[a-z0-9\-]+(?:\s+region)?\s*$", "", txt)
    return " ".join(txt.split())


def _extract_unit_price(desc, svc_name, pexpr):
    """(unit_key, category, label, service, unit_label, usd_price) for non-token AI media & speech SKUs, else None."""
    tiers = pexpr.get("tieredRates") or []
    raw_price = pricing.first_positive_money(tiers)
    if raw_price <= 0:
        return None
    unit = (pexpr.get("usageUnit") or "").lower()
    udesc = (pexpr.get("usageUnitDescription") or "").lower()
    factor = float(pexpr.get("baseUnitConversionFactor") or 1)
    low = (desc or "").lower()
    svc_low = (svc_name or "").lower()
    if any(w in low for w in ("offline", "priority", "flex", "provisioned", "commit", "training", "tuning",
                              "storage", "upscal", "edit", "custom voice")):
        return None
    label = _clean_unit_label(desc)
    if not label:
        return None
    if "veo" in low and unit == "s" and "batch" not in low:
        return ("veo:" + models.model_key(label), "Video", label, svc_name, "second", raw_price)
    if "lyria" in low and unit == "count" and "token" not in low and raw_price >= 0.005:
        unit_lbl = "30s clip" if "lyria 2" in low else ("track" if "pro" in low else "clip")
        return ("lyria:" + models.model_key(label), "Audio / Music", label, svc_name, unit_lbl, raw_price)
    if "imagen" in low and unit == "count" and "token" not in low and raw_price >= 0.005 and "batch" not in low:
        return ("imagen:" + models.model_key(label), "Image", label, svc_name, "image", raw_price)
    if "text-to-speech" in svc_low and unit in ("count", "char", "character") and "token" not in low:
        if "character" in low or "character" in udesc or "synthesize" in low or "voice" in low or unit in ("char", "character"):
            per_1m = raw_price / factor * 1_000_000.0
            if 0.1 <= per_1m <= 5000.0:
                return ("tts:" + models.model_key(label), "Text-to-Speech", label, svc_name, "1M characters", per_1m)
    if "speech" in svc_low and "text-to-speech" not in svc_low and unit in ("min", "s"):
        per_min = raw_price if unit == "min" else raw_price * 60.0
        if 0.0005 <= per_min <= 10.0:
            return ("stt:" + models.model_key(label), "Speech-to-Text", label, svc_name, "minute", per_min)
    return None


def _service_catalog_rows(ctx, svc):
    """([(key, direction, rank, per_million_usd, label)], [unit_row]) for one catalog service."""
    rows, unit_rows = [], []
    for sku in _service_skus(ctx, svc["id"]):
        info = ((sku.get("pricingInfo") or [None])[0]) or {}
        pexpr = info.get("pricingExpression") or {}
        tiers = pexpr.get("tieredRates") or []
        if not tiers:
            continue
        desc = sku.get("description") or ""
        unit_hit = _extract_unit_price(desc, svc["name"], pexpr)
        if unit_hit:
            unit_rows.append(unit_hit)
        if pexpr.get("usageUnit") != "count":
            continue
        hit = pricing.model_token_sku(desc, svc["name"], pexpr.get("usageUnit"), pexpr.get("usageUnitDescription"))
        if not hit:
            hit = pricing.model_cached_token_sku(desc, svc["name"], pexpr.get("usageUnit"),
                                                 pexpr.get("usageUnitDescription"))
        if not hit:
            continue
        key, direction, prod, mrank = hit
        per_million = (pricing.first_positive_money(tiers)
                       / float(pexpr.get("baseUnitConversionFactor") or 1) * 1e6)
        if per_million <= 0 or per_million > MAX_TOKEN_USD_PER_MILLION:
            continue
        rank = (1 if prod else 0, mrank, 0 if "global" in desc.lower() else 1)
        label = (models.parse_sku(desc, svc["name"]) or {}).get("name") or models.display_from_key(key)
        rows.append((key, direction, rank, per_million, label))
    return rows, unit_rows


def catalog_prices_and_units(ctx, services):
    """({key: {"input", "output", "cached", "label"}}, [unit price dicts], {service name: SKUs used})."""
    results = ctx.run({svc["id"]: (lambda s=svc: _service_catalog_rows(ctx, s)) for svc in services})
    candidates, labels, found = {}, {}, {}
    unit_groups = {}
    for svc in services:
        ok, pair = results.get(svc["id"], (False, None))
        if not ok:
            if isinstance(pair, GcpError):
                LOG.info("catalog SKUs of %s unavailable: %s", svc["name"], pair.kind)
            found[svc["name"]] = 0
            continue
        rows, unit_rows = pair
        found[svc["name"]] = len(rows) + len(unit_rows)
        for key, direction, rank, per_million, label in rows:
            candidates.setdefault((key, direction), []).append((rank, per_million))
            labels.setdefault(key, label)
        for ukey, cat, ulabel, sname, uunit, uprice in unit_rows:
            grp = unit_groups.setdefault(ukey, {"key": ukey, "category": cat, "label": ulabel,
                                                "service": sname, "unit": uunit, "prices": []})
            grp["prices"].append(uprice)
    out = {}
    for (key, direction), items in candidates.items():
        best = min(rank for rank, _ in items)
        out.setdefault(key, {"label": labels.get(key) or models.display_from_key(key)})[direction] = round(
            statistics.median(sorted(p for r, p in items if r == best)), 6)
    unit_prices = []
    for ukey in sorted(unit_groups):
        grp = unit_groups[ukey]
        unit_prices.append({
            "key": grp["key"], "category": grp["category"], "label": grp["label"],
            "service": grp["service"], "unit": grp["unit"],
            "usdPrice": round(statistics.median(sorted(grp["prices"])), 6),
            "source": "cloud_billing_catalog",
        })
    unit_prices.sort(key=lambda x: (x["category"], x["label"]))
    return out, unit_prices, found


def catalog_model_prices(ctx, services):
    """({key: {"input", "output", "cached", "label"}} USD per 1M tokens, {service name: SKUs used})."""
    out, _units, found = catalog_prices_and_units(ctx, services)
    return out, found


def catalog_conversion_rates(ctx, service, currencies):
    """{currency: (rate, effective time)}: the USD conversion rate Google publishes for each currency."""
    out = {}
    for cur in currencies:
        if cur == "USD":
            continue
        try:
            data = ctx.rest("GET", "%s/services/%s/skus" % (pricing.CATALOG_V1, service["id"]),
                            params={"currencyCode": cur, "pageSize": 5}, timeout=30)
        except GcpError:
            continue
        for sku in data.get("skus") or []:
            info = ((sku.get("pricingInfo") or [None])[0]) or {}
            rate = to_float(info.get("currencyConversionRate"))
            if rate and rate > 0:
                out[cur] = (rate, info.get("effectiveTime"))
                break
    return out


def storage_prices(ctx, service, location=None):
    """Streaming ingestion (per GiB) and active storage (per GiB-month) list prices in USD from the catalog.
    Routed log entries reach BigQuery through the legacy streaming API, then are stored."""
    ingest, storage = [], []
    loc = (location or "").lower()
    for sku in _service_skus(ctx, service["id"], max_pages=6):
        info = ((sku.get("pricingInfo") or [None])[0]) or {}
        pexpr = info.get("pricingExpression") or {}
        tiers = pexpr.get("tieredRates") or []
        if not tiers:
            continue
        unit = (pexpr.get("usageUnit") or "").lower()
        base_unit = (pexpr.get("baseUnit") or "").lower()
        factor = to_float(pexpr.get("baseUnitConversionFactor"))
        desc = (sku.get("description") or "").lower()
        unit_price = pricing.money(tiers[-1].get("unitPrice"))
        if unit_price <= 0:
            continue
        regions = [r.lower() for r in sku.get("serviceRegions") or []]
        local = 0 if (loc and loc in regions) else 1
        if "stream" in desc and "insert" in desc:
            gib_mult = {"giby": 1.0, "miby": 1024.0, "kiby": 1048576.0, "by": 1073741824.0}.get(unit)
            if gib_mult is None and base_unit == "by" and factor and factor > 0:
                gib_mult = 1073741824.0 / factor
            if gib_mult is not None:
                ingest.append((local, unit_price * gib_mult))
        elif unit == "giby.mo" and "active" in desc and "storage" in desc and "physical" not in desc:
            storage.append((local, unit_price))

    def pick(items):
        if not items:
            return None
        best = min(r for r, _ in items)
        return round(statistics.median(sorted(p for r, p in items if r == best)), 6)

    return {"ingestPerGiB": pick(ingest), "storagePerGiBMonth": pick(storage)}


# ---------------------------------------------------------------------------
# Exchange rates
# ---------------------------------------------------------------------------

def wanted_currencies(settings, extra=()):
    out = []
    p = _pricing_settings(settings)
    ag = ((settings or {}).get("governance") or {}).get("antigravity") or {}
    for cur in (p.get("currency"), ag.get("currency")) + tuple(extra):
        cur = (cur or "").strip().upper()
        if re.match(r"^[A-Z]{3}$", cur) and cur != "USD" and cur not in out:
            out.append(cur)
    return out


def fetch_fx(currencies, settings, catalog_rates, previous, fetcher, now_str):
    """({currency: {"rate", "previous", "source", "asOf"}}, [source status]) for USD -> each currency."""
    p = _pricing_settings(settings)
    prev_rates = (previous or {}).get("rates") or {}
    rates, statuses = {}, []
    feeds = {}
    for url in p.get("fx_sources") or []:
        try:
            payload = json.loads(fetcher(url))
            feed = {k.upper(): to_float(v) for k, v in (payload.get("rates") or {}).items() if to_float(v)}
            feeds[url] = (feed, str(payload.get("time_last_update_utc") or payload.get("date") or now_str)[:60])
            statuses.append({"id": "fx:%s" % (urllib.parse.urlparse(url).hostname or "feed"), "url": url,
                             "status": "ok", "checkedAt": now_str, "detail": "%d rates" % len(feed),
                             "modelsFound": 0})
        except Exception as err:  # pylint: disable=broad-except
            statuses.append({"id": "fx:%s" % (urllib.parse.urlparse(url).hostname or "feed"), "url": url,
                             "status": "error", "checkedAt": now_str, "detail": type(err).__name__,
                             "modelsFound": 0})
    for cur in currencies:
        prev = to_float((prev_rates.get(cur) or {}).get("rate"))
        chosen = None
        if cur in catalog_rates:
            rate, as_of = catalog_rates[cur]
            chosen = (rate, "cloud_billing_catalog", str(as_of or now_str)[:60])
        else:
            for url, (feed, as_of) in feeds.items():
                if feed.get(cur):
                    chosen = (feed[cur], urllib.parse.urlparse(url).hostname or "feed", as_of)
                    break
        if chosen and prev and abs(chosen[0] - prev) / prev > 0.5:
            # A jump of more than half in one day is a feed error, not a market move: keep the last rate.
            LOG.warning("exchange rate USD/%s rejected as implausible (%.4f -> %.4f)", cur, prev, chosen[0])
            chosen = None
        if chosen:
            rates[cur] = {"rate": round(chosen[0], 6), "previous": prev, "source": chosen[1], "asOf": chosen[2]}
        elif prev:
            old = prev_rates.get(cur) or {}
            rates[cur] = {"rate": prev, "previous": prev, "source": "last_known", "asOf": old.get("asOf")}
    return rates, statuses


# ---------------------------------------------------------------------------
# Snapshot diff and alerts
# ---------------------------------------------------------------------------

def _alert_id(now_str, kind, target, field):
    digest = hashlib.sha256(("%s|%s|%s|%s" % (now_str[:16], kind, target, field)).encode("utf-8")).hexdigest()[:10]
    return "chg_%s" % digest


def _alert(now_str, kind, target, label, field, before, after, pct, currency, source):
    return {"id": _alert_id(now_str, kind, target, field), "at": now_str, "kind": kind, "target": target,
            "label": label, "field": field, "before": before, "after": after, "pctChange": pct,
            "currency": currency, "source": source or "", "acknowledgedAt": None, "acknowledgedBy": None}


def diff_snapshots(prev_state, new_models, new_fx, now_str, settings=None):
    """Alerts for changed model prices, new priced models and moved exchange rates (none on the first sync)."""
    prev_state = prev_state or {}
    if not prev_state.get("syncedAt"):
        return []
    p = _pricing_settings(settings)
    fx_pct = float(p.get("alert_fx_change_pct", 1.0))
    price_pct = float(p.get("alert_price_change_pct", 0.5))
    alerts = []
    prev_rates = ((prev_state.get("fx") or {}).get("rates")) or {}
    for cur, item in sorted(((new_fx or {}).get("rates") or {}).items()):
        before = to_float((prev_rates.get(cur) or {}).get("rate"))
        after = to_float(item.get("rate"))
        if before and after and item.get("source") != "last_known":
            pct = (after - before) / before * 100.0
            if abs(pct) >= fx_pct:
                alerts.append(_alert(now_str, "fx", "USD/%s" % cur, "USD/%s" % cur, "rate", round(before, 4),
                                     round(after, 4), round(pct, 2), cur, item.get("source")))
    prev_models = {k: v for k, v in (prev_state.get("models") or {}).items() if valid_token_entry(k, v)}
    for key, cur in sorted((new_models or {}).items()):
        old = prev_models.get(key)
        label = cur.get("label") or models.display_from_key(key)
        if old is None:
            alerts.append(_alert(now_str, "new_model", key, label, "input", None, cur.get("input"), None, "USD",
                                 cur.get("source")))
            continue
        for slot in ("input", "output", "cached"):
            b_val, a_val = to_float(old.get(slot)), to_float(cur.get(slot))
            if b_val is None or a_val is None or b_val <= 0:
                continue
            pct = (a_val - b_val) / b_val * 100.0
            if abs(pct) >= price_pct:
                alerts.append(_alert(now_str, "model_price", key, label, slot, round(b_val, 6), round(a_val, 6),
                                     round(pct, 2), "USD", cur.get("source")))
    return alerts


# ---------------------------------------------------------------------------
# Synchronization
# ---------------------------------------------------------------------------

def _system_ctx(app, settings, version):
    from .base import Ctx
    from ..security import Identity
    return Ctx(app, settings, version, Identity("system", "admin", "system", "system:pricing"))


def sync_pricing(app, actor="system", trigger="manual", fetcher=None):
    """Reads official prices and exchange rates, stores the snapshot and records change alerts."""
    now_str = now_iso()
    settings_mgr = getattr(app, "settings", None)
    settings, version = settings_mgr.snapshot() if settings_mgr else (config.finalize(dict(config.DEFAULTS)), 1)
    prev_state = settings_mgr.pricing_state() if settings_mgr else {}
    p = _pricing_settings(settings)
    fetcher = fetcher or make_fetcher(allowed_hosts(settings))
    sources = []
    merged = {}

    # 1. Official Google price pages (fill models the catalog does not list yet).
    for url in p.get("public_sources") or []:
        sid = _page_source_id(url)
        try:
            found = parse_price_page(fetcher(url), source=sid)
            for key, entry in found.items():
                if valid_token_entry(key, entry):
                    cur = dict(merged.get(key) or {})
                    cur.update({k: v for k, v in entry.items() if v is not None})
                    merged[key] = cur
            sources.append({"id": sid, "url": url, "status": "ok", "checkedAt": now_str,
                            "detail": "%d models" % len(found), "modelsFound": len(found)})
        except Exception as err:  # pylint: disable=broad-except
            LOG.info("price page %s unavailable: %s", url, type(err).__name__)
            sources.append({"id": sid, "url": url, "status": "error", "checkedAt": now_str,
                            "detail": type(err).__name__, "modelsFound": 0})

    # 2. Cloud Billing Catalog list prices (official SKUs) win over page prices.
    catalog_services = list(prev_state.get("catalogServices") or [])
    storage = dict(prev_state.get("storage") or {}) or None
    unit_prices = list(prev_state.get("unitPrices") or [])
    catalog_rates = {}
    currencies = wanted_currencies(settings)
    if getattr(app, "gcp", None) is not None and config.workspace_configured(settings):
        ctx = _system_ctx(app, settings, version)
        try:
            all_services = list_catalog_services(ctx)
            model_services = select_model_services(all_services, settings)
            catalog_services = model_services
            cat_models, cat_units, _per_service = catalog_prices_and_units(ctx, model_services)
            if cat_units:
                unit_prices = cat_units
            for key, entry in cat_models.items():
                if not valid_token_entry(key, entry):
                    continue
                cur = dict(merged.get(key) or {})
                cur.update({k: v for k, v in entry.items() if v is not None})
                cur["source"] = "cloud_billing_catalog"
                merged[key] = cur
            sources.append({"id": "cloud_billing_catalog", "url": pricing.CATALOG_V1, "status": "ok",
                            "checkedAt": now_str,
                            "detail": "%d models from %d services" % (len(cat_models), len(model_services)),
                            "modelsFound": len(cat_models)})
            if model_services:
                catalog_rates = catalog_conversion_rates(ctx, model_services[0], currencies)
            store_svc = select_storage_service(all_services, settings)
            if store_svc:
                loc = (settings.get("telemetry") or {}).get("routing", {}).get("dataset_location") or ""
                storage = dict(storage_prices(ctx, store_svc, loc), service=store_svc["name"], currency="USD",
                               source="cloud_billing_catalog", updatedAt=now_str)
        except GcpError as err:
            sources.append({"id": "cloud_billing_catalog", "url": pricing.CATALOG_V1, "status": "error",
                            "checkedAt": now_str, "detail": "Catalog API %s" % err.kind, "modelsFound": 0})
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("catalog price sync failed: %s", type(err).__name__)
            sources.append({"id": "cloud_billing_catalog", "url": pricing.CATALOG_V1, "status": "error",
                            "checkedAt": now_str, "detail": type(err).__name__, "modelsFound": 0})

    # A failed run keeps the previous official prices instead of dropping models.
    if not merged:
        merged = {k: v for k, v in (prev_state.get("models") or {}).items() if valid_token_entry(k, v)}

    # 3. Exchange rates.
    rates, fx_status = fetch_fx(currencies, settings, catalog_rates, prev_state.get("fx"), fetcher, now_str)
    sources.extend(fx_status)
    fx_obj = {"base": "USD", "rates": rates, "updatedAt": now_str}

    added_alerts = diff_snapshots(prev_state, merged, fx_obj, now_str, settings)
    changed = (merged != (prev_state.get("models") or {})
               or unit_prices != (prev_state.get("unitPrices") or [])
               or rates != ((prev_state.get("fx") or {}).get("rates") or {}))

    if settings_mgr is not None:
        def update(state):
            existing = list(state.get("alerts") or [])
            ids = {a.get("id") for a in existing}
            existing.extend(a for a in added_alerts if a["id"] not in ids)
            state.update({"syncedAt": now_str, "syncedBy": actor, "trigger": trigger, "fx": fx_obj,
                          "models": merged, "unitPrices": unit_prices, "sources": sources,
                          "alerts": existing[-100:], "lastError": None,
                          "catalogServices": catalog_services, "storage": storage})
            return state

        bump = bool(changed or added_alerts or not prev_state.get("syncedAt"))
        settings_mgr.mutate_pricing(update, actor, bump_cache=bump)
        saved = settings_mgr.pricing_state()
        if bump and hasattr(app, "on_settings_changed"):
            app.on_settings_changed()
    else:
        saved = {"syncedAt": now_str, "syncedBy": actor, "trigger": trigger, "fx": fx_obj, "models": merged,
                 "unitPrices": unit_prices, "sources": sources, "alerts": added_alerts, "lastError": None,
                 "catalogServices": catalog_services, "storage": storage}
    return format_pricing_response(saved, settings, new_alerts_count=len(added_alerts))


# ---------------------------------------------------------------------------
# Lookups used by FinOps, Models & Tokens and routing estimates
# ---------------------------------------------------------------------------

def _state(ctx):
    settings_mgr = getattr(getattr(ctx, "app", None), "settings", None)
    return settings_mgr.pricing_state() if settings_mgr is not None else {}


def fx_rate(ctx, currency):
    """{"rate", "source", "asOf"} for USD -> currency, {"rate": 1.0} for USD, None when unknown."""
    cur = (currency or "USD").upper()
    if cur == "USD":
        return {"rate": 1.0, "source": "identity", "asOf": None}
    item = (((_state(ctx).get("fx") or {}).get("rates")) or {}).get(cur)
    if isinstance(item, dict) and to_float(item.get("rate")):
        return {"rate": float(item["rate"]), "source": item.get("source"), "asOf": item.get("asOf")}
    return None


def preferred_currency(ctx, explicit=None):
    """Explicit request > pricing.currency > governance currency > USD (no locale or region guesses)."""
    for raw in (explicit, (ctx.params or {}).get("currency") if hasattr(ctx, "params") else None,
                _pricing_settings(ctx.settings).get("currency"),
                ((ctx.settings.get("governance") or {}).get("antigravity") or {}).get("currency")):
        cur = (raw or "").strip().upper()
        if re.match(r"^[A-Z]{3}$", cur):
            return cur
    return "USD"


def supported_currencies(ctx):
    out = ["USD"]
    for cur in [preferred_currency(ctx)] + wanted_currencies(ctx.settings):
        if cur not in out and fx_rate(ctx, cur):
            out.append(cur)
    return out


def effective_token_rates(ctx, currency=None):
    """({model key: {"input", "output", "cached", "inputUsd", ..., "source", "label"}} per 1M tokens in the
    returned currency, currency, fx info). Falls back to USD when no exchange rate is known."""
    target = preferred_currency(ctx, currency)
    fx = fx_rate(ctx, target)
    if fx is None:
        if target != "USD":
            ctx.warn("pricing:fx", "fx_unavailable", "No exchange rate for USD/%s is available yet, so estimates "
                                                    "are shown in USD." % target)
        target, fx = "USD", {"rate": 1.0, "source": "identity", "asOf": None}
    factor = float(fx["rate"])
    combined = {}
    for key, item in (_state(ctx).get("models") or {}).items():
        if valid_token_entry(key, item):
            combined[key] = dict(item)
    for item in _pricing_settings(ctx.settings).get("model_overrides") or []:
        combined[item["model"]] = {"input": item.get("input"), "output": item.get("output"),
                                   "cached": item.get("cached"), "source": "override",
                                   "label": models.display_from_key(item["model"])}
    out = {}
    for key, item in combined.items():
        inp, outp, cached = (to_float(item.get(k)) for k in ("input", "output", "cached"))
        assumed = cached is None and inp is not None
        cached_val = inp if assumed else cached
        out[key] = {
            "input": inp * factor if inp is not None else None,
            "output": outp * factor if outp is not None else None,
            "cached": cached_val * factor if cached_val is not None else None,
            "inputUsd": inp, "outputUsd": outp, "cachedUsd": cached_val, "cachedAssumed": assumed,
            "source": item.get("source") or "unknown",
            "label": item.get("label") or models.display_from_key(key),
        }
    fx_out = {"base": "USD", "currency": target, "rate": factor, "source": fx.get("source"), "asOf": fx.get("asOf")}
    return out, target, fx_out


def lookup_model_rate(rates, raw_model_id, aliases=None):
    """(normalized key, rate or None): exact key, administrator alias, or the same model with a numeric
    revision suffix (gemini-3.8-flash-002 -> gemini-3.8-flash). Never guesses another model's price."""
    key = models.normalize_model_id(str(raw_model_id or ""))
    if not key:
        return key, None
    if key in rates:
        return key, rates[key]
    target = (aliases or {}).get(key)
    if target and target in rates:
        return key, rates[target]
    best = None
    for cand in rates:
        if key.startswith(cand + "-") and re.match(r"^[0-9][0-9\-]*$", key[len(cand) + 1:]):
            if best is None or len(cand) > len(best):
                best = cand
    return key, (rates[best] if best else None)


def model_aliases(ctx):
    return _alias_map(ctx.settings)


# ---------------------------------------------------------------------------
# API responses
# ---------------------------------------------------------------------------

def pricing_alert_summary(settings_mgr):
    """Compact summary embedded in /api/v1/bootstrap and /api/v1/finops/summary."""
    if settings_mgr is None:
        return {"syncedAt": None, "fx": None, "unacknowledgedCount": 0, "alerts": []}
    state = settings_mgr.pricing_state()
    unack = sorted((a for a in (state.get("alerts") or []) if not a.get("acknowledgedAt")),
                   key=lambda a: a.get("at") or "", reverse=True)
    return {"syncedAt": state.get("syncedAt"), "trigger": state.get("trigger"), "fx": state.get("fx"),
            "unacknowledgedCount": len(unack), "alerts": unack[:10]}


def format_pricing_response(state, settings, new_alerts_count=0, currency=None):
    state = state or {}
    p = _pricing_settings(settings)
    fx = state.get("fx") or {"base": "USD", "rates": {}}
    explicit_cur = (currency or "").strip().upper()
    if not re.match(r"^[A-Z]{3}$", explicit_cur):
        explicit_cur = ""
    display = (explicit_cur or p.get("currency") or (((settings or {}).get("governance") or {}).get("antigravity") or {})
               .get("currency") or "USD").upper()
    display_rate = to_float(((fx.get("rates") or {}).get(display) or {}).get("rate")) if display != "USD" else 1.0
    overrides = {o["model"]: o for o in p.get("model_overrides") or []}
    rows = []
    for key, item in sorted((state.get("models") or {}).items()):
        if not valid_token_entry(key, item) or key in overrides:
            continue
        rows.append((key, item))
    for key, item in overrides.items():
        rows.append((key, {"input": item.get("input"), "output": item.get("output"), "cached": item.get("cached"),
                           "source": "override", "label": models.display_from_key(key)}))
    model_rows = []
    for key, item in rows:
        usd = {k: to_float(item.get(k)) for k in ("input", "output", "cached")}
        local = {k: (round(v * display_rate, 4) if (v is not None and display_rate) else None) for k, v in usd.items()}
        model_rows.append({"key": key, "label": item.get("label") or models.display_from_key(key),
                           "vendor": models.vendor_for(item.get("label") or key), "source": item.get("source"),
                           "usdPerMillion": usd, "localPerMillion": local})
    unit_rows = []
    for u in state.get("unitPrices") or []:
        if not isinstance(u, dict) or to_float(u.get("usdPrice")) is None:
            continue
        u_usd = float(u["usdPrice"])
        unit_rows.append({
            "key": u.get("key") or "",
            "category": u.get("category") or "Media",
            "label": u.get("label") or "",
            "service": u.get("service") or "",
            "unit": u.get("unit") or "unit",
            "source": u.get("source") or "cloud_billing_catalog",
            "usdPrice": u_usd,
            "localPrice": round(u_usd * display_rate, 6) if display_rate else None,
        })
    alerts = sorted(state.get("alerts") or [], key=lambda a: a.get("at") or "", reverse=True)
    return {
        "syncedAt": state.get("syncedAt"), "syncedBy": state.get("syncedBy"), "trigger": state.get("trigger"),
        "fx": fx, "displayCurrency": display, "displayRate": display_rate,
        "models": model_rows, "unitPrices": unit_rows,
        "sources": state.get("sources") or [], "storage": state.get("storage"),
        "alerts": alerts[:50], "unacknowledgedCount": sum(1 for a in alerts if not a.get("acknowledgedAt")),
        "newAlertsCount": new_alerts_count,
        "schedule": dict(schedule_info(settings), enabled=True),
        "aliases": list(p.get("model_aliases") or []),
    }


def get_pricing(ctx):
    settings_mgr = getattr(ctx.app, "settings", None)
    state = settings_mgr.pricing_state() if settings_mgr is not None else {}
    req_cur = (ctx.params or {}).get("currency") if hasattr(ctx, "params") else None
    return format_pricing_response(state, ctx.settings, currency=req_cur)


def trigger_sync(ctx, scheduled=False):
    is_sched = bool(scheduled or getattr(ctx.identity, "auth_mode", "") == "scheduler")
    if not is_sched:
        require_identity_admin(ctx)
    actor = ctx.identity.email if is_sched else (ctx.identity.email or "admin")
    res = sync_pricing(ctx.app, actor=actor, trigger="scheduled" if is_sched else "manual")
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
            if not item.get("acknowledgedAt") and (ack_all or item.get("id") == target_id):
                item["acknowledgedAt"] = now_str
                item["acknowledgedBy"] = actor
                count += 1
        return count

    acked = settings_mgr.mutate_pricing(apply, actor, bump_cache=True)
    if hasattr(ctx.app, "on_settings_changed"):
        ctx.app.on_settings_changed()
    summary = pricing_alert_summary(settings_mgr)
    return {"acknowledged": acked, "unacknowledgedCount": summary["unacknowledgedCount"], "alerts": summary["alerts"]}
