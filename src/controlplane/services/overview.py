"""Overview: cross-module KPIs with previous-period deltas."""

from __future__ import annotations

import logging

from .. import timeutil
from ..gcp import GcpError
from ..security import uid_for
from .base import (ARMOR_OPS, AUDIT_DATA_ACCESS, bucket_values, iso, to_int)
from . import agents as agents_service
from . import finops as finops_service
from . import gemini_enterprise as ge_service
from . import governance
from . import model_armor as armor_service
from . import models as models_service
from . import notebooklm as nb_service

LOG = logging.getLogger("controlplane.overview")


def _kpi(value=None, previous=None, disabled=False, **extra):
    item = {"value": None if disabled else value, "previous": None if disabled else previous, "disabled": disabled}
    item.update({k: (None if disabled else v) for k, v in extra.items()})
    return item


def summary(ctx):
    rng = ctx.range
    prev = rng.previous()
    mods = ctx.settings["modules"]
    tasks = {}
    if mods.get("gemini_enterprise"):
        def ge_part():
            msrc, asrc = ge_service.ge_sources(ctx)
            if not msrc and not asrc:
                return None
            kpi = ge_service.ge_kpis(ctx, msrc, asrc, rng)
            top = ge_service._top_users(ctx, msrc, asrc, rng)
            return {"kpi": kpi, "top": top, "msrc": msrc, "asrc": asrc}
        tasks["ge"] = ge_part
        tasks["licenses"] = lambda: ctx.memo("licenses", "global", lambda c: ge_service.licenses(c))
    if mods.get("model_usage"):
        tasks["models"] = lambda: models_service.build_usage(
            ctx, rng, ctx.memo("models_usage", rng, lambda c: models_service.usage_data(c, rng)))
        tasks["models_prev"] = lambda: models_service.build_usage(
            ctx, prev, ctx.memo("models_usage", prev, lambda c: models_service.usage_data(c, prev)))
    if mods.get("finops"):
        tasks["spend"] = lambda: finops_service.ai_spend(ctx, rng)
    if mods.get("model_armor"):
        def armor_part():
            src = ctx.audit_source(ARMOR_OPS)
            return armor_service.armor_totals(ctx, src, rng) if src else None
        tasks["armor"] = armor_part
    if mods.get("agent_registry"):
        tasks["agents"] = lambda: ctx.memo("registry_data", rng, lambda c: agents_service.registry_data(c, rng))
    if mods.get("notebooklm"):
        def nb_part():
            src = ctx.audit_source(AUDIT_DATA_ACCESS)
            return nb_service.nb_totals(ctx, src, rng) if src else None
        tasks["nb"] = nb_part
    if mods.get("antigravity"):
        tasks["agy"] = lambda: governance.antigravity_usage(ctx, rng.start_ts(), rng.end_ts())
        tasks["agy_prev"] = lambda: governance.antigravity_usage(ctx, prev.start_ts(), prev.end_ts())
        tasks["actions"] = lambda: governance.audit_entries(ctx, 10)
    res = ctx.run_composite(tasks)

    labels = {
        "ge": ("bigquery:%s.%s" % (ctx.audit_dataset, ge_service.GE_MESSAGE), "Gemini Enterprise activity"),
        "licenses": ("api:discoveryengine.licenseConfigs", "The Discovery Engine license API"),
        "models": ("model_usage", "Model usage"), "models_prev": ("model_usage", "Model usage"),
        "spend": ("bigquery:%s" % ctx.ws["billing"].get("dataset"), "The billing export table"),
        "armor": ("bigquery:%s.%s" % (ctx.audit_dataset, ARMOR_OPS), "Model Armor logs"),
        "agents": ("api:aiplatform.reasoningEngines", "The agent registry"),
        "nb": ("bigquery:%s.%s" % (ctx.audit_dataset, AUDIT_DATA_ACCESS), "NotebookLM audit logs"),
        "agy": ("antigravity", "Antigravity usage"), "agy_prev": ("antigravity", "Antigravity usage"),
        "actions": ("logging:governance-audit", "The governance audit log"),
    }

    def get(name):
        if name not in res:
            return None
        ok, value = res[name]
        if ok:
            return value
        source, what = labels.get(name, (name, name))
        ctx.warn_error(source, value, what)
        return None

    ge = get("ge")
    lic = get("licenses")
    models = get("models")
    models_prev = get("models_prev")
    spend = get("spend")
    armor = get("armor")
    reg = get("agents")
    nb = get("nb")
    agy = get("agy")
    agy_prev = get("agy_prev")
    actions = get("actions")

    kpis = {}
    ge_disabled = not mods.get("gemini_enterprise")
    if ge and ge["kpi"]:
        cur = ge["kpi"]["totals"].get("cur", {})
        before = ge["kpi"]["totals"].get("prev", {})
        caps_m = ge["kpi"]["caps_m"] or {}
        kpis["activeUsers"] = _kpi(to_int(cur.get("users")), to_int(before.get("users")))
        if ge_service.prompt_caps(caps_m, ge["kpi"]["caps_a"])[0]:
            kpis["prompts"] = _kpi(to_int(cur.get("prompts")), to_int(before.get("prompts")))
        else:
            kpis["prompts"] = _kpi()
    else:
        kpis["activeUsers"] = _kpi(disabled=ge_disabled)
        kpis["prompts"] = _kpi(disabled=ge_disabled)
    kpis["licenses"] = {"purchased": lic["purchased"] if lic else None, "assigned": lic["assigned"] if lic else None,
                        "activeUsers": kpis["activeUsers"]["value"], "disabled": ge_disabled}
    if models is not None:
        kpis["modelTokens"] = _kpi(models["totals"]["tokens"]["total"],
                                   models_prev["totals"]["tokens"]["total"] if models_prev else None,
                                   source=models["tokenSource"])
        if models["tokenSource"] == "mixed":
            kpis["modelTokens"]["source"] = "monitoring"
    else:
        kpis["modelTokens"] = _kpi(disabled=not mods.get("model_usage"), source=None)
    if spend is not None:
        kpis["aiSpend"] = {"gross": spend["gross"], "credits": spend["credits"], "net": spend["net"],
                           "previousNet": spend["previousNet"], "currency": spend["currency"],
                           "hasCredits": spend["hasCredits"], "disabled": False}
    else:
        kpis["aiSpend"] = {"gross": None, "credits": None, "net": None, "previousNet": None, "currency": "USD",
                           "hasCredits": None, "disabled": not mods.get("finops")}
    if armor is not None:
        cur = armor["totals"].get("cur", {})
        before = armor["totals"].get("prev", {})
        verdict = armor["caps"]["verdict"]
        kpis["armorBlocks"] = _kpi(to_int(cur.get("blocked")) if verdict else None,
                                   to_int(before.get("blocked")) if verdict else None,
                                   operations=to_int(cur.get("ops")))
    else:
        kpis["armorBlocks"] = _kpi(disabled=not mods.get("model_armor"), operations=None)
    if reg is not None and reg.get("available"):
        items = reg["agents"]
        kpis["agents"] = {"total": len(items), "active": sum(1 for a in items if a["status"] == "active"),
                          "idle": sum(1 for a in items if a["status"] == "idle"),
                          "deprecationCandidates": sum(1 for a in items if a["status"] == "deprecation_candidate"),
                          "disabled": False}
    else:
        kpis["agents"] = {"total": None, "active": None, "idle": None, "deprecationCandidates": None,
                          "disabled": not mods.get("agent_registry")}
    if nb is not None:
        totals, _ = nb
        kpis["notebooklmPrompts"] = _kpi(to_int(totals.get("cur", {}).get("prompts")),
                                         to_int(totals.get("prev", {}).get("prompts")))
    else:
        kpis["notebooklmPrompts"] = _kpi(disabled=not mods.get("notebooklm"))
    if agy is not None and agy.ok:
        prev_tokens = sum(u["tokens"] for u in agy_prev.users.values()) if (agy_prev and agy_prev.ok) else None
        kpis["antigravityTokens"] = _kpi(sum(u["tokens"] for u in agy.users.values()), prev_tokens,
                                         users=sum(1 for u in agy.users.values() if u["requests"] > 0))
    else:
        kpis["antigravityTokens"] = _kpi(disabled=not mods.get("antigravity"), users=None)

    # Timelines and lists are null unless their source succeeded.
    spend_trend = None
    if spend is not None:
        series = [timeutil.series("gross", "Gross", spend["grossSeries"])]
        series.append(timeutil.series("net", "Net", spend["netSeries"]))
        spend_trend = timeutil.timeline(rng, series)
    token_trend = None
    if models is not None and models.get("tokenTrend") is not None:
        series = models["tokenTrend"]["series"]
        ranked = sorted(series, key=lambda s: -sum(s["values"]))
        top, rest = ranked[:6], ranked[6:]
        out_series = [dict(s) for s in top]
        if rest:
            out_series.append(timeutil.series("other", "Other",
                                              [sum(s["values"][i] for s in rest) for i in range(len(rng.buckets()))]))
        token_trend = timeutil.timeline(rng, out_series)
    armor_trend = None
    if armor is not None:
        armor_series = [timeutil.series("operations", "Operations", bucket_values(rng, armor["buckets"], "ops"))]
        if armor["caps"]["verdict"]:
            armor_series.append(timeutil.series("blocked", "Blocked", bucket_values(rng, armor["buckets"], "blocked")))
        armor_trend = timeutil.timeline(rng, armor_series)
    top_users = None
    if ge and ge["top"] is not None:
        top_users = []
        agy_users = agy.users if (agy is not None and agy.ok) else {}
        for item in ge["top"][:10]:
            top_users.append({"uid": item["uid"], "email": item["email"], "prompts": item["prompts"],
                              "agentCalls": item["agentCalls"],
                              "antigravityTokens": (agy_users.get(item["email"]) or {}).get("tokens", 0)
                              if agy is not None and agy.ok else None})
    recent = None if actions is None else []
    for item in (actions or [])[:10]:
        recent.append({"time": item["time"], "actor": item["actor"], "action": item["action"],
                       "target": item["target"], "result": item["result"]})
    return {"kpis": kpis, "spendTrend": spend_trend, "tokenTrend": token_trend, "armorTrend": armor_trend,
            "topUsers": top_users, "recentActions": recent}
