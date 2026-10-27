"""Automatic token prices for per-user cost estimates (Antigravity cost caps).

Administrators set cost caps as amounts (for example 1,000,000 KRW per person); they do not need to
know token prices. The price per model and direction (input / output tokens) comes from, in order:

1. billing  - what the billing account actually paid per token in the last 30 days: SUM(cost) /
              SUM(usage.amount) over the model's standard text token SKUs in the billing export
              (negotiated prices included; credits are not subtracted).
2. catalog  - the public Google Cloud price list (Cloud Billing Catalog API: Agent Platform and Gemini API
              services) for models without billed usage.

Amounts are converted to the cap currency with the rate Google publishes in the Catalog
(pricingInfo.currencyConversionRate, USD to the requested currency). Nothing is guessed: when a source
is unavailable the affected models stay unpriced and a warning says why.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field

from .. import timeutil
from ..gcp import GcpError
from ..schema import expr
from . import models
from .base import range_params, to_float
from .billing import ai_clause, billing_fields, excluded_clause

LOG = logging.getLogger("controlplane.pricing")

CATALOG_V1 = "https://cloudbilling.googleapis.com/v1"
CATALOG_LABEL = "api:cloudbilling.services.skus"
# Cloud Billing Catalog service ids (stable public identifiers).
CATALOG_SERVICES = (("C7E2-9256-1C43", "Agent Platform"), ("AEFD-7695-64FA", "Gemini API"))
BILLING_DAYS = 30
CACHE_SECONDS = 6 * 3600
MAX_CATALOG_PAGES = 8
# SKU variants that do not carry the standard online text price.
VARIANT_WORDS = {"batch", "flex", "priority", "offpeak", "peak", "caching", "cached", "cache", "live", "provisioned",
                 "tuning", "tuned", "audio", "image", "images", "video", "cyber", "bidi", "embedding", "embeddings",
                 "grounding", "search", "contributor", "deferred", "offline"}
LONG_CONTEXT_RE = re.compile(r"(?i)(?:>|\u2265|over|above)\s*\d|long context|long input|long output")
WORD_RE = re.compile(r"[a-z0-9.]+")


def token_sku(description, service=None, unit=None, pricing_unit=None):
    """(model key, "input" | "output", product) for a standard text token SKU, else None."""
    if not description or not isinstance(description, str):
        return None
    low = description.lower()
    if set(WORD_RE.findall(low)) & VARIANT_WORDS or LONG_CONTEXT_RE.search(low):
        return None
    info = models.parse_sku(description, service)
    if not info:
        return None
    measure = models.classify_measure(description, unit, pricing_unit)
    if measure == "input_tokens":
        return info["key"], "input", info["product"]
    if measure == "output_tokens":
        return info["key"], "output", info["product"]
    return None


# ---------------------------------------------------------------------------
# Billing export: effective prices
# ---------------------------------------------------------------------------

def effective_prices(rows):
    """({key: {"input": per_million, "output": per_million}}, currency, usd_rate) from billing rows
    {sku, service, unit, pricing_unit, amount, cost, currency, fx}. Prices are in the billing currency;
    usd_rate is the average USD-to-billing-currency rate of the rows (currency_conversion_rate)."""
    pure, product = {}, {}
    currency = None
    rates = []
    for row in rows or []:
        hit = token_sku(row.get("sku"), row.get("service"), row.get("unit"), row.get("pricing_unit"))
        amount = to_float(row.get("amount")) or 0.0
        cost = to_float(row.get("cost")) or 0.0
        if not hit or amount <= 0 or cost <= 0:
            continue
        key, direction, prod = hit
        agg = (product if prod else pure).setdefault((key, direction), [0.0, 0.0])
        agg[0] += cost
        agg[1] += amount
        currency = currency or row.get("currency")
        rate = to_float(row.get("fx"))
        if rate:
            rates.append(rate)
    out = {}
    # Product re-billing SKUs (for example CodeMender) only fill models without their own SKUs.
    for source in (product, pure):
        for (key, direction), (cost, amount) in source.items():
            out.setdefault(key, {})[direction] = cost / amount * 1e6
    return out, currency, (statistics.mean(rates) if rates else None)


def billing_price_rows(ctx, src):
    f = billing_fields(src)
    if not (f["sku"] and f["cost"] and f["amount"]):
        return []
    rate = expr("b", src.schema, "currency_conversion_rate", cast="FLOAT64") or "CAST(NULL AS FLOAT64)"
    null = "CAST(NULL AS STRING)"
    rng = timeutil.parse_range({}, ctx.tz_name, BILLING_DAYS)
    sql = ("WITH %s\nSELECT %s AS sku, %s AS service, %s AS unit, %s AS pricing_unit, SUM(IFNULL(%s, 0)) AS amount, "
           "SUM(IFNULL(%s, 0)) AS cost, ANY_VALUE(%s) AS currency, AVG(%s) AS fx\nFROM bil b WHERE %s AND %s\n"
           "GROUP BY sku, service, unit, pricing_unit"
           % (src.cte("bil", start="start_ts"), f["sku"], f["service"] or null, f["unit"] or null,
              f["pricing_unit"] or null, f["amount"], f["cost"], f["currency"] or null, rate, ai_clause(f),
              excluded_clause(f)))
    params = range_params(rng, with_prev=False)
    params["excluded"] = ctx.excluded_patterns
    return ctx.query(sql, params, "antigravity")


# ---------------------------------------------------------------------------
# Cloud Billing Catalog: public prices
# ---------------------------------------------------------------------------

def _money(unit):
    return int((unit or {}).get("units") or 0) + ((unit or {}).get("nanos") or 0) / 1e9


def catalog_entries(skus):
    """Yields (key, direction, rank, price_per_token, conversion_rate, effective_time) for standard text
    token SKUs priced per token count. rank prefers own model SKUs and global endpoints."""
    for sku in skus or []:
        info = ((sku.get("pricingInfo") or [None])[0]) or {}
        pexpr = info.get("pricingExpression") or {}
        rate = to_float(info.get("currencyConversionRate"))
        if pexpr.get("usageUnit") != "count":
            continue
        hit = token_sku(sku.get("description"), None, pexpr.get("usageUnit"), pexpr.get("usageUnitDescription"))
        tiers = pexpr.get("tieredRates") or []
        if not hit or not tiers:
            yield None, None, None, None, rate, info.get("effectiveTime")
            continue
        price = _money(tiers[-1].get("unitPrice")) / float(pexpr.get("baseUnitConversionFactor") or 1)
        key, direction, prod = hit
        desc = (sku.get("description") or "").lower()
        yield key, direction, (1 if prod else 0, 0 if "global" in desc else 1), price, rate, info.get("effectiveTime")


def catalog_prices(ctx, currency):
    """({key: {"input": per_million, "output": per_million}}, usd_rate, effective_time) in `currency`."""
    candidates = {}
    usd_rate, effective = None, None
    for service_id, _name in CATALOG_SERVICES:
        token = None
        for _ in range(MAX_CATALOG_PAGES):
            params = {"currencyCode": currency, "pageSize": 5000}
            if token:
                params["pageToken"] = token
            data = ctx.rest("GET", "%s/services/%s/skus" % (CATALOG_V1, service_id), params=params, timeout=60)
            for key, direction, rank, price, rate, eff in catalog_entries(data.get("skus")):
                if usd_rate is None and rate:
                    usd_rate, effective = rate, eff
                if key and price and price > 0:
                    candidates.setdefault((key, direction), []).append((rank, price))
            token = data.get("nextPageToken")
            if not token:
                break
    out = {}
    for (key, direction), items in candidates.items():
        best = min(rank for rank, _ in items)
        out.setdefault(key, {})[direction] = statistics.median(sorted(p for r, p in items if r == best)) * 1e6
    return out, usd_rate, effective


# ---------------------------------------------------------------------------
# Combined price table
# ---------------------------------------------------------------------------

@dataclass
class PriceTable:
    currency: str
    prices: dict = field(default_factory=dict)  # key -> {"input", "output", "source"}
    fx: dict = None                              # {"from": "USD", "to": currency, "rate", "asOf"}
    counts: dict = field(default_factory=dict)   # {"billing": n, "catalog": n}

    def lookup(self, model):
        return self.prices.get(models.normalize_model_id(str(model or "")))


def combine(currency, billing, billing_currency, billing_usd_rate, catalog, usd_rate, effective):
    """Pure merge of both sources into a PriceTable in `currency`. Returns (table, warning or None)."""
    warning = None
    factor = None
    if billing:
        if not billing_currency or billing_currency == currency:
            factor = 1.0
        elif usd_rate:
            if billing_currency == "USD":
                factor = usd_rate
            elif billing_usd_rate:
                factor = usd_rate / billing_usd_rate
        if factor is None:
            warning = ("Billed prices are in %s and could not be converted to %s, so the public price list is "
                       "used instead." % (billing_currency, currency))
    prices = {}
    for key, entry in catalog.items():
        prices[key] = dict(entry, source="catalog")
    billed = 0
    if factor:
        for key, entry in billing.items():
            merged = dict(prices.get(key) or {})
            merged.update({d: v * factor for d, v in entry.items()})
            merged["source"] = "billing"
            prices[key] = merged
            billed += 1
    fx = None
    if usd_rate and currency != "USD":
        fx = {"from": "USD", "to": currency, "rate": round(usd_rate, 6), "asOf": effective}
    counts = {"billing": billed, "catalog": sum(1 for e in prices.values() if e["source"] == "catalog")}
    return PriceTable(currency, prices, fx, counts), warning


def build_price_table(ctx, currency):
    """Billing and catalog prices in `currency` ("" = billing currency). Never raises for upstream
    failures; they become warnings and the affected models stay unpriced."""
    billing, billing_currency, billing_rate = {}, None, None
    src = ctx.billing_source()
    if src is not None:
        try:
            billing, billing_currency, billing_rate = effective_prices(billing_price_rows(ctx, src))
            ctx.add_source(src.label)
        except GcpError as err:
            ctx.warn_error(src.label, err, "Prices from the billing export")
    cap_currency = currency or billing_currency or "USD"
    catalog, usd_rate, effective = {}, None, None
    try:
        catalog, usd_rate, effective = catalog_prices(ctx, cap_currency)
        ctx.add_source(CATALOG_LABEL)
    except GcpError as err:
        ctx.warn_error(CATALOG_LABEL, err, "The Google Cloud price list")
    table, warning = combine(cap_currency, billing, billing_currency, billing_rate, catalog, usd_rate, effective)
    if warning:
        ctx.warn("pricing:fx", "fx_unavailable", warning)
    if not table.prices:
        ctx.warn("pricing:auto", "no_prices", "No token prices could be determined from the billing export or the "
                                             "public price list, so estimated cost is unavailable.")
    return table


def price_table(ctx, currency):
    """Cached price table (6 hours per settings version and currency)."""
    return ctx.memo("token_prices", currency or "", lambda c: build_price_table(c, currency), ttl=CACHE_SECONDS)
