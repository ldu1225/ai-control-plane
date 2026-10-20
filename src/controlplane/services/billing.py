"""Billing export helpers shared by Models, Overview, FinOps and Deployments."""

from __future__ import annotations

from ..schema import expr

AI_SERVICE_RE = r"(?i)(vertex ai|gemini|generative|anthropic|claude|model garden|llama|mistral|ai platform)"
AI_SKU_RE = r"(?i)(gemini|claude|llama|mistral|gemma|imagen|veo|chirp|lyria|token)"
INFRA_SKU_RE = (r"(?i)(data index|reasoningengine|reasoning engine|agent engine|vertex ai search|vcpu|memory|"
                r"storage|training|endpoint|node hour)")


def billing_fields(src, alias="b"):
    s = src.schema
    return {
        "sku": expr(alias, s, "sku.description", cast="STRING"),
        "service": expr(alias, s, "service.description", cast="STRING"),
        "amount": expr(alias, s, "usage.amount", cast="FLOAT64"),
        "unit": expr(alias, s, "usage.unit", cast="STRING"),
        "pricing_unit": expr(alias, s, "usage.pricing_unit", cast="STRING"),
        "cost": expr(alias, s, "cost", cast="FLOAT64"),
        "currency": expr(alias, s, "currency", cast="STRING"),
        "project": expr(alias, s, "project.id", cast="STRING"),
        "account": expr(alias, s, "billing_account_id", cast="STRING"),
        "resource_name": expr(alias, s, "resource.name", cast="STRING"),
        "resource_global": expr(alias, s, "resource.global_name", cast="STRING"),
        "has_credits": s.node("credits") is not None and s.node("credits.amount") is not None,
        "has_credit_type": s.node("credits.type") is not None,
        "has_credit_name": s.node("credits.name") is not None,
        "ts": "%s.%s" % (alias, "`usage_start_time`"),
    }


def credits_sql(alias="b"):
    return "IFNULL((SELECT SUM(c.amount) FROM UNNEST(%s.credits) AS c), 0)" % alias


def excluded_clause(f):
    """Never include SKUs matching the excluded patterns (parameter @excluded, lower-case)."""
    sku = "LOWER(IFNULL(%s, ''))" % f["sku"]
    svc = "LOWER(IFNULL(%s, ''))" % (f["service"] or "''")
    return ("NOT EXISTS (SELECT 1 FROM UNNEST(@excluded) AS p WHERE STRPOS(%s, p) > 0 OR STRPOS(%s, p) > 0)"
            % (sku, svc))


def ai_clause(f):
    svc = "IFNULL(%s, '')" % (f["service"] or "''")
    sku = "IFNULL(%s, '')" % f["sku"]
    return ("(REGEXP_CONTAINS(%s, r'%s') OR REGEXP_CONTAINS(%s, r'%s')) AND NOT REGEXP_CONTAINS(%s, r'%s')"
            % (svc, AI_SERVICE_RE, sku, AI_SKU_RE, sku, INFRA_SKU_RE))


def is_excluded(text, patterns):
    low = (text or "").lower()
    return any(p and p.lower() in low for p in patterns or [])
