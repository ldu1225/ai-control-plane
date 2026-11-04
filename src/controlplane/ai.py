"""Gemini engine on Agent Platform via google-genai: model discovery, allowlisting and JSON generation."""

from __future__ import annotations

import json
import logging
import re
import threading
import time

from .cache import TTLCache
from .config import MODEL_ID_RE
from .gcp import GcpError
from .httpkit import ApiError

LOG = logging.getLogger("controlplane.ai")

DISCOVERY_TTL = 3600
# A truncated JSON answer is retried only when the first call took at most this many seconds, so the user waits
# at most one slow call plus one quick one (each request may take up to 60 s).
JSON_RETRY_WINDOW = 25.0
_EXCLUDE_DISCOVERED = ("embedding", "-tts", "tts-", "image", "imagen", "veo", "live", "native-audio", "audio",
                       "robotics", "computer-use", "learnlm", "aqa")
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
# Pictographic and symbol ranges commonly used as emoji; stripped from model output defensively.
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF\U00002B00-\U00002BFF"
                       "\U0000FE0F\U0000200D]")


def strip_emoji(text):
    if not isinstance(text, str):
        return text
    return _EMOJI_RE.sub("", text)


def parse_model_id(model_id):
    """'gemini-4-argon' -> ('gemini', '4', 'argon'); 'gemini-3.8-flash-lite' -> ('gemini', '3.8', 'flash-lite')."""
    parts = model_id.split("-")
    family = parts[0] if parts else model_id
    version = ""
    rest = parts[1:]
    if rest and re.match(r"^\d+(\.\d+)?$", rest[0]):
        version = rest[0]
        rest = rest[1:]
    return family, version, "-".join(rest)


class AiEngine:
    def __init__(self, gcp):
        self.gcp = gcp
        self._lock = threading.Lock()
        self._clients = {}
        self._unavailable = set()
        self._cache = TTLCache(32)

    def client(self, project, location, quota_project):
        """The cached google-genai client for one project, location and quota project."""
        return self._client(project, location, quota_project)

    def _client(self, project, location, quota_project):
        key = (project, location, quota_project)
        with self._lock:
            client = self._clients.get(key)
        if client is not None:
            return client
        creds = self.gcp.credentials(quota_project)
        try:
            import google.genai as genai
            from google.genai import types
            client = genai.Client(vertexai=True, project=project, location=location, credentials=creds,
                                  http_options=types.HttpOptions(timeout=90000))
        except Exception as err:  # pylint: disable=broad-except
            LOG.error("genai client init failed: %s", type(err).__name__)
            raise GcpError("unavailable", message=type(err).__name__)
        with self._lock:
            self._clients[key] = client
        return client

    # -- discovery ----------------------------------------------------------------
    def discover(self, settings):
        """Returns (discovered_ids, status) where status is ok | failed | disabled."""
        ai = settings["ai_engine"]
        if not ai.get("discover_models"):
            return [], "disabled"
        project = settings["workspace"]["project_id"]
        if not project or project.startswith("your-"):
            return [], "failed"
        quota = settings["workspace"].get("quota_project_id") or project
        location = ai.get("location") or "global"

        def load():
            try:
                client = self._client(project, location, quota)
                found = []
                pager = client.models.list(config={"query_base": True, "page_size": 100})
                for idx, model in enumerate(pager):
                    if idx >= 500:
                        break
                    name = getattr(model, "name", "") or ""
                    model_id = name.rsplit("/", 1)[-1].split("@", 1)[0].lower()
                    if not model_id.startswith("gemini-") or not MODEL_ID_RE.match(model_id):
                        continue
                    if any(token in model_id for token in _EXCLUDE_DISCOVERED):
                        continue
                    actions = getattr(model, "supported_actions", None)
                    if actions and "generateContent" not in actions:
                        continue
                    if model_id not in found:
                        found.append(model_id)
                return {"ids": sorted(found), "status": "ok"}
            except Exception as err:  # pylint: disable=broad-except
                LOG.warning("model discovery failed: %s", type(err).__name__)
                return {"ids": [], "status": "failed"}

        value, _ = self._cache.get_or_compute(("discover", project, location), DISCOVERY_TTL, load,
                                              cacheable=lambda v: v["status"] == "ok")
        return list(value["ids"]), value["status"]

    def catalog(self, settings):
        configured = list(settings["ai_engine"].get("models") or [])
        discovered, status = self.discover(settings)
        restrict = settings["ai_engine"].get("restrict_to_allowed", True)
        out = []
        for model_id in sorted(set(configured) | set(discovered)):
            source = "both" if (model_id in configured and model_id in discovered) else (
                "configured" if model_id in configured else "discovered")
            family, version, tier = parse_model_id(model_id)
            out.append({"id": model_id, "source": source, "family": family, "version": version, "tier": tier,
                        "allowed": True if not restrict else (model_id in configured or model_id in discovered)})
        return out, status

    def resolve_model(self, settings, requested):
        ai = settings["ai_engine"]
        model = requested or ai.get("default_model")
        if not isinstance(model, str) or not MODEL_ID_RE.match(model):
            raise ApiError("bad_request", "Invalid model identifier.")
        if ai.get("restrict_to_allowed", True):
            allowed = set(ai.get("models") or [])
            if model not in allowed and ai.get("discover_models"):
                discovered, _ = self.discover(settings)
                allowed |= set(discovered)
            if model not in allowed:
                raise ApiError("bad_request", "The requested model is not allowed in this workspace.")
        return model

    def fallback_model(self, settings, failed_model):
        """Picks a working Gemini model when an internal-only or preview model returns 404 on an external GCP org."""
        project = settings["workspace"]["project_id"]
        location = settings["ai_engine"].get("location") or "global"
        default_m = settings["ai_engine"].get("default_model") or ""
        base = [default_m] if default_m and default_m != failed_model else []
        # ai_engine.fallback_models, then the configured models (no built-in model list).
        ordered = base + list(settings["ai_engine"].get("fallback_models") or []) + \
            list(settings["ai_engine"].get("models") or [])
        candidates = tuple(dict.fromkeys(m for m in ordered if m))
        with self._lock:
            dead = set(self._unavailable)
        if settings["ai_engine"].get("discover_models"):
            discovered, status = self.discover(settings)
            if status == "ok" and discovered:
                for cand in candidates:
                    if cand in discovered and cand != failed_model and (project, location, cand) not in dead:
                        return cand
                for cand in reversed(discovered):
                    if cand != failed_model and (project, location, cand) not in dead:
                        return cand
        for cand in candidates:
            if cand != failed_model and (project, location, cand) not in dead:
                return cand
        return None

    def effective_model(self, settings, preferred):
        project = settings["workspace"]["project_id"]
        location = settings["ai_engine"].get("location") or "global"
        with self._lock:
            is_dead = (project, location, preferred) in self._unavailable
        if is_dead:
            return self.fallback_model(settings, preferred) or preferred
        return preferred

    # -- generation ---------------------------------------------------------------
    def generate(self, settings, model, contents, *, system=None, schema=None, temperature=0.2,
                 max_output_tokens=4096, thinking_level=None, meta_out=None, module="ai"):
        """Calls generate_content. Returns parsed JSON (when schema is given) or text."""
        from google.genai import types
        project = settings["workspace"]["project_id"]
        quota = settings["workspace"].get("quota_project_id") or project
        location = settings["ai_engine"].get("location") or "global"
        client = self._client(project, location, quota)
        cfg = {"temperature": temperature, "max_output_tokens": max_output_tokens,
               "labels": {"app": "control-plane", "module": module}}
        if system:
            cfg["system_instruction"] = system
        if schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_schema"] = schema
        attempts = 2 if schema is not None else 1
        started = time.monotonic()
        active_model = self.effective_model(settings, model) if type(self.gcp).__name__ == "Gcp" else model
        _apply_thinking(cfg, types, active_model, thinking_level)
        for attempt in range(1, attempts + 1):
            try:
                resp = client.models.generate_content(model=active_model, contents=contents,
                                                      config=types.GenerateContentConfig(**cfg))
            except Exception as err:  # pylint: disable=broad-except
                status = getattr(err, "code", None) or getattr(err, "status_code", None)
                if (status in (404, 429, 503, 504) and type(self.gcp).__name__ == "Gcp"
                        and (active_model == settings["ai_engine"].get("default_model")
                             or active_model.startswith("gemini-4-")
                             or active_model.startswith("gemini-3.8-"))):
                    if status == 404:
                        with self._lock:
                            self._unavailable.add((project, location, active_model))
                    fb = self.fallback_model(settings, active_model)
                    if fb:
                        LOG.info("model %s returned %s; falling back to %s", active_model, status, fb)
                        active_model = fb
                        _apply_thinking(cfg, types, active_model, thinking_level)
                        try:
                            resp = client.models.generate_content(model=active_model, contents=contents,
                                                                  config=types.GenerateContentConfig(**cfg))
                        except Exception as fb_err:  # pylint: disable=broad-except
                            fb_st = getattr(fb_err, "code", None) or getattr(fb_err, "status_code", None)
                            if fb_st == 404:
                                with self._lock:
                                    self._unavailable.add((project, location, active_model))
                                fb2 = self.fallback_model(settings, active_model)
                                if fb2 and fb2 != active_model:
                                    active_model = fb2
                                    _apply_thinking(cfg, types, active_model, thinking_level)
                                    resp = client.models.generate_content(model=active_model, contents=contents,
                                                                          config=types.GenerateContentConfig(**cfg))
                                else:
                                    raise GcpError("not_found", status=404, message=type(fb_err).__name__)
                            else:
                                kind = "denied" if fb_st in (401, 403) else "unavailable"
                                raise GcpError(kind, status=fb_st if isinstance(fb_st, int) else None,
                                               message=type(fb_err).__name__)
                    else:
                        kind = "not_found" if status == 404 else "unavailable"
                        raise GcpError(kind, status=status if isinstance(status, int) else None,
                                       message=type(err).__name__)
                else:
                    LOG.warning("generate_content failed model=%s error=%s status=%s", active_model,
                                type(err).__name__, status)
                    kind = "denied" if status in (401, 403) else ("not_found" if status == 404 else "unavailable")
                    raise GcpError(kind, status=status if isinstance(status, int) else None, message=type(err).__name__)
            if isinstance(meta_out, dict):
                meta_out["model"] = active_model
            text = (getattr(resp, "text", None) or "").strip()
            if schema is None:
                return strip_emoji(text)
            data = _parse_json(text)
            if data is not None:
                return _strip_emoji_deep(data)
            # Usually a truncated answer (thinking tokens count against max_output_tokens): retry once with
            # a larger budget before giving up, but only after a quick first call. A second full-length call
            # after a slow one would run into the request deadline and keep the user waiting twice as long.
            LOG.warning("model returned non-JSON output model=%s finish=%s attempt=%d", model,
                        _finish_reason(resp), attempt)
            if time.monotonic() - started > JSON_RETRY_WINDOW:
                break
            cfg["max_output_tokens"] = min(int(cfg["max_output_tokens"]) * 2, 32768)
        raise GcpError("invalid", message="non-JSON model output")

    def ping(self, settings, model):
        start = time.monotonic()
        self.generate(settings, model, "Reply with the single word OK.", temperature=0.0, max_output_tokens=16,
                      module="ai_test")
        return int((time.monotonic() - start) * 1000)


def _parse_json(text):
    """Parses a JSON object from model output: plain, fenced, or surrounded by stray text."""
    text = _FENCE_RE.sub("", text or "").strip()
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text and "}" in text else ""):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _finish_reason(resp):
    try:
        return str(resp.candidates[0].finish_reason)
    except Exception:  # pylint: disable=broad-except
        return "unknown"


def _strip_emoji_deep(value):
    if isinstance(value, str):
        return strip_emoji(value)
    if isinstance(value, list):
        return [_strip_emoji_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_emoji_deep(v) for k, v in value.items()}
    return value


def _apply_thinking(cfg, types_mod, model_id, thinking_level):
    if not thinking_level:
        cfg.pop("thinking_config", None)
        return
    thinking_cls = getattr(types_mod, "ThinkingConfig", None)
    if thinking_cls is None:
        cfg.pop("thinking_config", None)
        return
    mid = (model_id or "").lower()
    try:
        if mid.startswith(("gemini-4-", "gemini-3")):
            cfg["thinking_config"] = thinking_cls(thinking_level=thinking_level)
        elif mid.startswith("gemini-2.5-") and str(thinking_level).upper() == "LOW":
            cfg["thinking_config"] = thinking_cls(thinking_budget=512)
        else:
            cfg.pop("thinking_config", None)
    except Exception:  # pylint: disable=broad-except
        cfg.pop("thinking_config", None)

