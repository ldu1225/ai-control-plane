"""Personal widget layouts: the telemetry dashboards each person pins to the Overview and other pages.

A layout lists widgets in display order. Each widget shows one Telemetry Extensions connector in one view
(summary, daily chart, a breakdown or the latest entries) and one size. Layouts belong to the signed-in
person: they are stored per user, keyed by a hash of the email (never the email itself), next to the settings
document in the same storage backend (settings_store.UserDocStore). Shared read-only sessions cannot save.

GET  /api/v1/layouts  -> {"layouts": {page: [widget, ...]}, "editable": bool}
POST /api/v1/layouts  {"page": ..., "widgets": [...]} -> the same shape after saving
"""

from __future__ import annotations

import re

from ..httpkit import ApiError, now_iso
from ..security import uid_for
from ..settings_store import StoreConflict, StoreError
from .telemetry import CONNECTOR_ID_RE

# Pages that can hold widgets (route ids of the web UI).
PAGES = ("overview", "gemini-enterprise", "notebooklm", "gateway", "registry", "deployments", "model-armor", "models",
         "finops", "users", "antigravity")
VIEWS = ("summary", "trend", "operations", "statuses", "recent")
SIZES = ("s", "m", "l")
MAX_WIDGETS = 24
WIDGET_ID_RE = re.compile(r"^w_[0-9a-f]{8}$")


def _key(ctx):
    return uid_for(ctx.identity.email)


def _editable(ctx):
    return bool(ctx.identity.email) and ctx.identity.auth_mode != "public_readonly"


def clean_widgets(widgets):
    """Validates a widget list; raises ApiError(bad_request) with the first problem."""
    if not isinstance(widgets, list) or len(widgets) > MAX_WIDGETS:
        raise ApiError("bad_request", "Field 'widgets' must be a list of at most %d widgets." % MAX_WIDGETS)
    out, seen = [], set()
    for widget in widgets:
        if not isinstance(widget, dict):
            raise ApiError("bad_request", "Each widget must be an object.")
        wid = widget.get("id")
        if not isinstance(wid, str) or not WIDGET_ID_RE.match(wid) or wid in seen:
            raise ApiError("bad_request", "Widget ids must be unique values such as w_1a2b3c4d.")
        conn = widget.get("connector")
        if not isinstance(conn, str) or not CONNECTOR_ID_RE.match(conn):
            raise ApiError("bad_request", "Field 'connector' must be a connector id.")
        if widget.get("view") not in VIEWS:
            raise ApiError("bad_request", "Field 'view' must be one of: %s." % ", ".join(VIEWS))
        if widget.get("size") not in SIZES:
            raise ApiError("bad_request", "Field 'size' must be one of: %s." % ", ".join(SIZES))
        seen.add(wid)
        out.append({"id": wid, "connector": conn, "view": widget["view"], "size": widget["size"]})
    return out


def _read(doc):
    layouts = doc.get("layouts") if isinstance(doc, dict) else None
    out = {}
    if not isinstance(layouts, dict):
        return out
    for page in PAGES:
        try:
            widgets = clean_widgets(layouts.get(page) or [])
        except ApiError:
            widgets = []  # a document edited by hand is ignored rather than breaking the page
        if widgets:
            out[page] = widgets
    return out


def get_layouts(ctx):
    if not _editable(ctx):
        return {"layouts": {}, "editable": False}
    try:
        doc, _ = ctx.app.user_docs.load(_key(ctx))
    except StoreError:
        ctx.warn("storage:layouts", "upstream_unavailable", "Saved widget layouts could not be read.")
        return {"layouts": None, "editable": True}
    return {"layouts": _read(doc), "editable": True}


def save_layout(ctx):
    if not _editable(ctx):
        raise ApiError("forbidden", "Widget layouts cannot be saved in a shared read-only session.")
    body = ctx.body or {}
    page = body.get("page")
    if page not in PAGES:
        raise ApiError("bad_request", "Field 'page' must be one of: %s." % ", ".join(PAGES))
    widgets = clean_widgets(body.get("widgets"))

    def apply(doc):
        layouts = doc.get("layouts") if isinstance(doc.get("layouts"), dict) else {}
        if widgets:
            layouts[page] = widgets
        else:
            layouts.pop(page, None)
        doc["layouts"] = layouts
        doc["updatedAt"] = now_iso()

    try:
        doc = ctx.app.user_docs.mutate(_key(ctx), apply)
    except (StoreError, StoreConflict):
        raise ApiError("upstream_error", "The widget layout could not be saved. Try again.")
    return {"layouts": _read(doc), "editable": True}
