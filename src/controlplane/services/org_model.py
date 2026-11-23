"""Organization model for internal chargeback: units (divisions, departments, teams), the people in them and the
projects they own.

The model is the aux document "org_model" (settings_store.AuxDocStore). A CSV import replaces it as a whole; it is
never edited in place:

    {"version": 3, "updatedAt": "2026-11-19T03:00:00Z", "updatedBy": "admin@example.com",
     "source": {"kind": "csv", "sha256": "<hex>", "rows": 1200, "importedAt": "...", "importedBy": "..."},
     "units": {"u_1a2b3c4d5e": {"name": "Korea", "parent": "u_5e4d3c2b1a", "path": "Sales/Korea",
                                "costCenter": "KR-100"}},
     "members": {"alice@example.com": "u_1a2b3c4d5e"},
     "projects": {"sales-analytics": "u_1a2b3c4d5e"}}

- A unit id is "u_" + the first 10 hex digits of SHA-256(normalized path, lower-cased), so a unit keeps its id across
  imports. Normalizing a path splits it on "/", trims each segment, collapses inner whitespace to one space and drops
  empty segments. Ancestor units are created implicitly.
- Members are person keys (identity.person_key): Google accounts and workforce identities. Service accounts, groups
  and domains are not people; their usage is attributed through their project.
- The virtual unit UNASSIGNED collects usage that no rule attributes. It is never stored.
- The raw CSV is never stored, logged or audited: only the parsed model and the SHA-256 of the file.

CSV file (UTF-8, optional byte order mark, at most 5 MB and 200,000 rows), header principal,unit_path[,cost_center]:
- principal: an e-mail address, user:EMAIL, a workforce principal URI
  (principal://iam.googleapis.com/locations/global/workforcePools/POOL/subject/SUBJECT), project:PROJECT_ID, or
  empty (the row only describes a unit);
- unit_path: "Division/Department/Team", at most 10 levels of at most 120 characters each;
- cost_center: optional, for the unit at unit_path.
Rows are numbered by the line where the record starts (the header is normally row 1).

Attribution (OrgModel.unit_for): the person's unit > the project's unit > the unit whose cost center (or id) is the
value of the project label chargeback.project_unit_label > UNASSIGNED.

POST /api/v1/org/import {"csv", "dryRun" (default true), "expectSha256"}  check or apply a CSV file (administrators)
GET  /api/v1/org/tree  the unit tree with member counts and project ids (never member e-mails)
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import threading
import weakref
from collections.abc import Mapping

from .. import config, identity
from ..httpkit import ApiError, now_iso
from ..settings_store import StoreConflict, StoreError, aux_docs

LOG = logging.getLogger("controlplane.org_model")

DOC = "org_model"
UNASSIGNED = "u_unassigned"
UNASSIGNED_NAME = "Unassigned"
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_ROWS = 200000
MAX_UNITS = 20000
MAX_DEPTH = 10
MAX_SEGMENT = 120
MAX_COST_CENTER = 60
MAX_PRINCIPAL = 700         # longer cells are refused unparsed (a workforce URI with a 512-character subject fits)
MAX_EMAIL = 254
MAX_ISSUES = 200            # errors and warnings returned by one check; all of them are counted
MAX_VALUE = 200
HEADERS = (("principal", "unit_path"), ("principal", "unit_path", "cost_center"))
# Used with fullmatch.
COST_CENTER_RE = re.compile(r"[A-Za-z0-9_.\-]+")
UNIT_ID_RE = re.compile(r"u_[0-9a-f]{10}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
# Control characters (C0, DEL, C1) and invisible formatting characters that make different names look the same
# (line and paragraph separators, bidirectional embeddings, overrides and isolates, the byte order mark).
HIDDEN_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]")

_UNASSIGNED_UNIT = {"id": UNASSIGNED, "name": UNASSIGNED_NAME, "parent": None, "path": UNASSIGNED_NAME,
                    "costCenter": "", "depth": 0}


class _ReadOnly(dict):
    """A dict that refuses changes: a loaded model is cached and shared by concurrent requests. Copies made with
    dict(), copy.copy, copy.deepcopy or pickle are plain dicts; JSON encoding works as for any dict."""

    def _refuse(self, *args, **kwargs):
        raise TypeError("the organization model is read-only")

    __setitem__ = __delitem__ = __ior__ = clear = pop = popitem = setdefault = update = _refuse

    def __reduce__(self):
        return dict, (dict(self),)

    def copy(self):
        return dict(self)


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text(value, limit):
    return value[:limit] if isinstance(value, str) and value else None


def _shown(value):
    """An offending value as returned to the administrator: clipped, with control characters replaced."""
    return HIDDEN_RE.sub("\ufffd", str(value)[:MAX_VALUE])


def _quoted(path):
    return "'%s'" % (path if len(path) <= 80 else path[:77] + "...")


def split_path(value):
    """Normalized segments of a unit path: " Sales // Korea  Team " -> ["Sales", "Korea Team"]."""
    if not isinstance(value, str):
        return []
    return [seg for seg in (" ".join(part.split()) for part in value.split("/")) if seg]


def _hash_id(lowered):
    return "u_" + hashlib.sha256(lowered.encode("utf-8")).hexdigest()[:10]


def unit_id(path):
    """Stable id of the unit at `path` (a path string or a list of segments); ValueError for an empty path."""
    segments = split_path(path if isinstance(path, str) else "/".join(path))
    if not segments:
        raise ValueError("empty unit path")
    return _hash_id("/".join(segments).lower())


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

class _Issues:
    """Row errors or warnings: the first MAX_ISSUES are kept, all of them are counted."""

    def __init__(self):
        self.items = []
        self.count = 0

    def add(self, row, code, message, value):
        self.count += 1
        if len(self.items) < MAX_ISSUES:
            self.items.append({"row": row, "code": code, "message": message, "value": _shown(value)})


class _Parsed:
    """The model read from one CSV file, with its row errors and warnings."""

    def __init__(self):
        self.rows = 0
        self.units = {}         # unit id -> {"name", "parent", "path", "costCenter"} (document form)
        self.members = {}       # person key -> unit id
        self.projects = {}      # project id -> unit id
        self.errors = _Issues()
        self.warnings = _Issues()
        self._lowered = {}      # unit id -> lower-cased path (detects two paths with the same id)
        self._leaf = {}         # lower-cased path -> unit id (later rows of a unit skip the ancestor walk)
        self._first = {}        # ("person" | "project" | "cost_center", key) -> first row that set it

    def add(self, n, fields, width):
        if len(fields) > width:
            self.errors.add(n, "bad_row", "The row has %d columns but the header has %d." % (len(fields), width),
                            ",".join(fields[:width + 2]))
            return
        cells = list(fields) + [""] * (width - len(fields))
        principal, raw_path = cells[0].strip(), cells[1]
        target = self._principal(n, principal)
        segments = self._segments(n, raw_path)
        center = self._cost_center(n, cells[2].strip()) if width > 2 else ""
        uid = self._unit(n, segments, raw_path) if segments else None
        if uid is None:
            return
        if center:
            self._set_cost_center(n, uid, center)
        if target is not None and target[0]:
            self._map(n, target[0], target[1], uid, principal)

    def _principal(self, n, raw):
        """("person", key), ("project", project id), ("", None) for an empty cell, or None after an error."""
        if not raw:
            return "", None
        if raw[:8].lower() == "project:":
            project = raw[8:].strip().lower()
            if config.valid_project(project):
                return "project", project
            self.errors.add(n, "bad_project", "The value after 'project:' is not a valid project id.", raw)
            return None
        found = identity.parse(raw) if len(raw) <= MAX_PRINCIPAL else None
        if found is not None and found.is_person and \
                (found.kind != identity.GOOGLE_USER or len(found.key) <= MAX_EMAIL):
            return "person", found.key
        if found is not None and found.kind == identity.SERVICE_ACCOUNT:
            message = "Service accounts cannot be unit members. Map their project with project:PROJECT_ID instead."
        else:
            message = ("The principal must be an e-mail address, user:EMAIL, a workforce principal URI or "
                       "project:PROJECT_ID. Groups and domains are not supported.")
        self.errors.add(n, "bad_principal", message, raw)
        return None

    def _segments(self, n, raw):
        """Normalized segments of a unit path cell, or None after an error. Control characters are checked before
        whitespace is normalized, because normalizing would turn some of them into spaces."""
        if HIDDEN_RE.search(raw):
            self.errors.add(n, "bad_path", "The unit path contains control or invisible characters, for example a "
                                           "tab or a line break inside quotes.", raw)
            return None
        if "\ufffd" in raw:
            self.errors.add(n, "bad_path", "The unit path contains characters that could not be read. Save the "
                                           "file as CSV UTF-8.", raw)
            return None
        segments = split_path(raw)
        if not segments:
            self.errors.add(n, "bad_path", "The unit path is empty.", raw)
            return None
        if any(len(seg) > MAX_SEGMENT for seg in segments):
            self.errors.add(n, "segment_too_long", "Each unit name in the path can have at most %d characters."
                            % MAX_SEGMENT, raw)
            return None
        if len(segments) > MAX_DEPTH:
            self.errors.add(n, "path_too_deep", "The unit path has %d levels but at most %d are allowed."
                            % (len(segments), MAX_DEPTH), raw)
            return None
        return segments

    def _cost_center(self, n, raw):
        """The cost center cell ("" when empty), or None after an error."""
        if not raw or (len(raw) <= MAX_COST_CENTER and COST_CENTER_RE.fullmatch(raw)):
            return raw
        self.errors.add(n, "bad_cost_center", "The cost center can have at most %d letters, digits, '_', '.' or "
                                              "'-'." % MAX_COST_CENTER, raw)
        return None

    def _unit(self, n, segments, raw):
        """Id of the unit at `segments`, created with its ancestors when new; None after an error."""
        lowered = "/".join(segments).lower()
        found = self._leaf.get(lowered)
        if found is not None:
            return found
        parent = None
        for depth in range(1, len(segments) + 1):
            key = "/".join(segments[:depth]).lower()
            uid = _hash_id(key)
            known = self._lowered.get(uid)
            if known is None:
                if len(self.units) >= MAX_UNITS:
                    raise ApiError("bad_request", "The CSV file defines more than %d units." % MAX_UNITS)
                name = segments[depth - 1]
                path = name if parent is None else "%s/%s" % (self.units[parent]["path"], name)
                self.units[uid] = {"name": name, "parent": parent, "path": path, "costCenter": ""}
                self._lowered[uid] = key
            elif known != key:
                self.errors.add(n, "bad_path", "The unit path gets the same id as the unit %s. Rename one of them."
                                % _quoted(self.units[uid]["path"]), raw)
                return None
            parent = uid
        self._leaf[lowered] = parent
        return parent

    def _set_cost_center(self, n, uid, center):
        unit = self.units[uid]
        if not unit["costCenter"]:
            unit["costCenter"] = center
            self._first[("cost_center", uid)] = n
        elif unit["costCenter"].lower() != center.lower():
            self.errors.add(n, "cost_center_conflict", "The unit %s has cost center %s in row %d and %s in row %d."
                            % (_quoted(unit["path"]), unit["costCenter"], self._first[("cost_center", uid)], center,
                               n), center)

    def _map(self, n, kind, key, uid, raw):
        """Maps a person or a project to a unit; a second mapping is a warning (same unit) or an error."""
        table = self.members if kind == "person" else self.projects
        first = self._first.get((kind, key))
        if first is None:
            table[key] = uid
            self._first[(kind, key)] = n
        elif table[key] == uid:
            self.warnings.add(n, "duplicate", "Row %d already maps this %s to the same unit." % (first, kind), raw)
        else:
            self.errors.add(n, "principal_conflict" if kind == "person" else "project_conflict",
                            "This %s is mapped to %s in row %d and to %s in row %d."
                            % (kind, _quoted(self.units[table[key]]["path"]), first,
                               _quoted(self.units[uid]["path"]), n), raw)

    def summary(self, digest, current):
        return {"dryRun": True, "valid": not self.errors.count, "sha256": digest, "rows": self.rows,
                "units": len(self.units), "members": len(self.members), "projects": len(self.projects),
                "errors": self.errors.items, "errorCount": self.errors.count,
                "warnings": self.warnings.items, "warningCount": self.warnings.count,
                "diff": _diff(self, current)}


def _diff(parsed, current):
    """Counts of what applying `parsed` changes in the stored model `current` (an OrgModel)."""
    old_members, old_projects = current.members, current.projects
    return {
        "unitsAdded": sum(1 for uid in parsed.units if uid not in current.units),
        "unitsRemoved": sum(1 for uid in current.units if uid not in parsed.units),
        "membersAdded": sum(1 for key in parsed.members if key not in old_members),
        "membersRemoved": sum(1 for key in old_members if key not in parsed.members),
        "membersMoved": sum(1 for key, uid in parsed.members.items() if old_members.get(key, uid) != uid),
        "projectsChanged": sum(1 for pid, uid in parsed.projects.items() if old_projects.get(pid) != uid)
        + sum(1 for pid in old_projects if pid not in parsed.projects),
    }


def _csv_text(value):
    """(text without the byte order mark, SHA-256 hex of its UTF-8 bytes) of the uploaded file."""
    if value is None:
        raise ApiError("bad_request", "The CSV file is empty.")
    if not isinstance(value, str):
        raise ApiError("bad_request", "Field 'csv' must be the text of a CSV file.")
    too_large = ApiError("bad_request", "The CSV file is larger than %d MB." % (MAX_CSV_BYTES // (1024 * 1024)))
    if len(value) > MAX_CSV_BYTES:  # every character takes at least one byte
        raise too_large
    try:
        data = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ApiError("bad_request", "The CSV file must be UTF-8 text.")
    if len(data) > MAX_CSV_BYTES:
        raise too_large
    if value.startswith("\ufeff"):
        value, data = value[1:], data[3:]
    if not value.strip():
        raise ApiError("bad_request", "The CSV file is empty.")
    return value, hashlib.sha256(data).hexdigest()


def _parse(text):
    """Reads CSV text. File-level problems raise ApiError; row problems are collected as errors and warnings."""
    parsed = _Parsed()
    reader = csv.reader(io.StringIO(text, newline=""))
    width, line = 0, 0
    try:
        for fields in reader:
            # A record can span several lines (line breaks inside quotes): it is numbered by its first line.
            n, line = line + 1, reader.line_num
            if not any(cell.strip() for cell in fields):
                continue  # blank lines and rows of empty cells
            if not width:
                if tuple(cell.strip().lower() for cell in fields) not in HEADERS:
                    raise ApiError("bad_request", "The CSV header must be principal,unit_path,cost_center.")
                width = len(fields)
                continue
            parsed.rows += 1
            if parsed.rows > MAX_ROWS:
                raise ApiError("bad_request", "The CSV file has more than %d rows." % MAX_ROWS)
            parsed.add(n, fields, width)
    except csv.Error:
        raise ApiError("bad_request", "The CSV file could not be read at row %d. Check the quotes in that row."
                       % (line + 1))
    if not width:
        raise ApiError("bad_request", "The CSV file is empty.")
    return parsed


def _require_admin(ctx):
    """Defense in depth: the route is registered for administrators only."""
    if getattr(getattr(ctx, "identity", None), "role", None) != "admin":
        raise ApiError("forbidden", "Administrator privileges are required.")


def _save(ctx, parsed, digest):
    """Replaces the stored model with `parsed`; returns the new version."""
    now = now_iso()
    actor = str(getattr(ctx.identity, "email", "") or "")[:254]

    def apply(doc):
        version = _count(doc.get("version")) + 1
        doc.clear()
        doc.update({"version": version, "updatedAt": now, "updatedBy": actor,
                    "source": {"kind": "csv", "sha256": digest, "rows": parsed.rows, "importedAt": now,
                               "importedBy": actor},
                    "units": parsed.units, "members": parsed.members, "projects": parsed.projects})
        return version
    store = aux_docs(getattr(ctx, "app", None))
    try:
        return store.mutate(DOC, apply)
    except StoreConflict:
        raise ApiError("conflict", "The organization model was changed at the same time. Run the check again.")
    except (StoreError, OSError) as err:
        LOG.error("organization model could not be saved: %s", type(err).__name__)
        raise ApiError("upstream_error", "The organization model could not be saved. Try again.")
    finally:
        _forget(store)


def _audit(ctx, payload):
    from . import governance  # imported here to avoid an import cycle
    try:
        return bool(governance.write_audit(ctx, "org_import", payload))
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("organization model audit entry failed: %s", type(err).__name__)
        return False


def import_csv(ctx):
    """POST /api/v1/org/import {"csv": text, "dryRun": true|false (default true), "expectSha256": hex (optional)}.

    A dry run returns the check: counts, row errors and warnings (the first MAX_ISSUES of each, with errorCount and
    warningCount) and the difference to the stored model; nothing changes. dryRun false applies a file without
    errors (and, with expectSha256, only the file that was checked): the whole model is replaced (version + 1), the
    governance audit log records the counts and the SHA-256 of the file, and cached responses are dropped."""
    _require_admin(ctx)
    body = ctx.body if isinstance(getattr(ctx, "body", None), dict) else {}
    dry_run = body.get("dryRun")
    if dry_run is None:
        dry_run = True
    if not isinstance(dry_run, bool):
        raise ApiError("bad_request", "Field 'dryRun' must be true or false.")
    expect = body.get("expectSha256")
    text, digest = _csv_text(body.get("csv"))
    parsed = _parse(text)
    result = parsed.summary(digest, load(ctx))
    if dry_run:
        return result
    if parsed.errors.count:
        raise ApiError("bad_request", "The CSV file has errors. Fix them and run the check again.")
    if expect not in (None, "") and (not isinstance(expect, str) or expect.strip().lower() != digest):
        raise ApiError("bad_request", "The file changed after the check. Run the check again.")
    version = _save(ctx, parsed, digest)
    counts = {key: result[key] for key in ("rows", "units", "members", "projects")}
    LOG.info("organization model imported version=%d rows=%d units=%d members=%d projects=%d", version,
             counts["rows"], counts["units"], counts["members"], counts["projects"])
    audit_logged = _audit(ctx, dict(counts, version=version, sha256=digest))
    invalidate = getattr(getattr(ctx, "app", None), "invalidate_governance", None)
    if callable(invalidate):
        invalidate()
    result.update({"dryRun": False, "applied": True, "version": version, "auditLogged": audit_logged})
    return result


# ---------------------------------------------------------------------------
# Read-only model
# ---------------------------------------------------------------------------

def _read_source(raw):
    if not isinstance(raw, dict):
        return _ReadOnly()
    digest = raw.get("sha256")
    return _ReadOnly(kind=_text(raw.get("kind"), 20),
                     sha256=digest if isinstance(digest, str) and SHA256_RE.fullmatch(digest) else None,
                     rows=_count(raw.get("rows")), importedAt=_text(raw.get("importedAt"), 40),
                     importedBy=_text(raw.get("importedBy"), 254))


def _read_units(raw):
    """(units, roots, children, depth-first order) of the document's units. Units with an invalid id or name and
    units in parent cycles are left out; a unit whose parent is unknown becomes a top-level unit. Depths and paths
    are rebuilt from the names."""
    names, parents, centers = {}, {}, {}
    for uid, item in (raw.items() if isinstance(raw, dict) else ()):
        if not isinstance(uid, str) or not UNIT_ID_RE.fullmatch(uid) or not isinstance(item, dict):
            continue
        name = " ".join(item["name"].split()) if isinstance(item.get("name"), str) else ""
        if not name or len(name) > MAX_SEGMENT or "/" in name or HIDDEN_RE.search(name):
            continue
        center = item.get("costCenter")
        names[uid] = name
        parents[uid] = item.get("parent")
        centers[uid] = center if isinstance(center, str) and len(center) <= MAX_COST_CENTER and \
            COST_CENTER_RE.fullmatch(center) else ""

    def by_name(uid):
        return names[uid].casefold(), names[uid], uid

    children = {uid: [] for uid in names}
    roots = []
    for uid, parent in parents.items():
        if isinstance(parent, str) and parent in names and parent != uid:
            children[parent].append(uid)
        else:
            roots.append(uid)
    for kids in children.values():
        kids.sort(key=by_name)
    roots.sort(key=by_name)
    units, order = {}, []
    stack = [(uid, None) for uid in reversed(roots)]
    while stack:  # units in a parent cycle are never reached from a top-level unit
        uid, parent = stack.pop()
        above = units.get(parent)
        units[uid] = _ReadOnly(id=uid, name=names[uid], parent=parent, costCenter=centers[uid],
                               path=names[uid] if above is None else "%s/%s" % (above["path"], names[uid]),
                               depth=0 if above is None else above["depth"] + 1)
        order.append(uid)
        stack.extend((child, uid) for child in reversed(children[uid]))
    return units, tuple(roots), {uid: tuple(children[uid]) for uid in order}, tuple(order)


def _read_members(raw, units):
    out = {}
    for key, uid in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(uid, str) and uid in units and isinstance(key, str) and len(key) <= MAX_PRINCIPAL and \
                identity.is_person_key(key):
            out[key] = uid
    return out


def _read_projects(raw, units):
    out = {}
    for pid, uid in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(uid, str) and uid in units and config.valid_project(pid) and not HIDDEN_RE.search(pid):
            out[pid] = uid
    return out


def _label_value(labels, key):
    """Lower-cased value of label `key` in a {key: value} mapping or a billing export list of {"key", "value"}."""
    value = None
    if isinstance(labels, Mapping):
        value = labels.get(key)
    elif isinstance(labels, (list, tuple)):
        value = next((item.get("value") for item in labels if isinstance(item, Mapping) and item.get("key") == key),
                     None)
    return value.strip().lower() if isinstance(value, str) else ""


class OrgModel:
    """Read-only index of one org_model document. Use load(ctx), which caches it per stored generation.

    version, updated_at, updated_by, source  document metadata (0, None, None, {} without a document)
    units     {unit id: {"id", "name", "parent", "path", "costCenter", "depth"}}, depth 0 for top-level units
    members   {person key: unit id}
    projects  {project id: unit id}
    empty     True when there are no units
    The mappings refuse changes (a cached model is shared by concurrent requests). A hand-edited or damaged document
    is read defensively: invalid entries, units in parent cycles and mappings to unknown units are left out."""

    def __init__(self, doc=None):
        doc = doc if isinstance(doc, dict) else {}
        self.version = _count(doc.get("version"))
        self.updated_at = _text(doc.get("updatedAt"), 40)
        self.updated_by = _text(doc.get("updatedBy"), 254)
        self.source = _read_source(doc.get("source"))
        units, self._roots, self._children, self._order = _read_units(doc.get("units"))
        self.units = _ReadOnly(units)
        self.members = _ReadOnly(_read_members(doc.get("members"), units))
        self.projects = _ReadOnly(_read_projects(doc.get("projects"), units))
        self.empty = not units
        # Project label values name a cost center case-insensitively; several units with the same cost center
        # resolve to the highest one, then to the first path.
        self._by_cost_center = {}
        for unit in sorted((u for u in units.values() if u["costCenter"]),
                           key=lambda u: (u["depth"], u["path"].casefold(), u["path"], u["id"])):
            self._by_cost_center.setdefault(unit["costCenter"].lower(), unit["id"])

    def unit(self, unit_id):
        """The unit (a copy); the virtual Unassigned unit for UNASSIGNED and unknown ids."""
        found = self.units.get(unit_id) if isinstance(unit_id, str) else None
        return dict(found if found is not None else _UNASSIGNED_UNIT)

    def ancestors(self, unit_id):
        """[unit_id, parent, ..., top-level unit]; [UNASSIGNED] for UNASSIGNED and unknown ids."""
        if not isinstance(unit_id, str) or unit_id not in self.units:
            return [UNASSIGNED]
        out = []
        while unit_id is not None:
            out.append(unit_id)
            unit_id = self.units[unit_id]["parent"]
        return out

    def children(self):
        """{unit id: [child unit ids sorted by name]} for every unit (an empty list for units without children)."""
        return {uid: list(kids) for uid, kids in self._children.items()}

    def roots(self):
        """Top-level unit ids sorted by name."""
        return list(self._roots)

    def dfs_order(self):
        """Every unit id in depth-first order: top-level units by name, each followed by its children by name."""
        return list(self._order)

    def unit_for(self, person_key, project, labels=None, label_key=""):
        """(unit id, via) for usage by `person_key` in `project`, by precedence:
        "member"      the person's unit;
        "project"     the project's unit from the CSV file;
        "label"       the project label `label_key` (in `labels`, a mapping or a billing export list of
                      {"key", "value"}) names a unit's cost center, case-insensitively, or a unit id;
        "unassigned"  (UNASSIGNED, "unassigned") otherwise. An empty label_key skips the label step."""
        if isinstance(person_key, str) and person_key:
            uid = self.members.get(person_key)
            if uid is None and len(person_key) <= MAX_PRINCIPAL:
                key = identity.person_key(person_key)
                uid = self.members.get(key) if key else None
            if uid is not None:
                return uid, "member"
        if isinstance(project, str) and project:
            uid = self.projects.get(project) or self.projects.get(project.strip().lower())
            if uid is not None:
                return uid, "project"
        if label_key:
            value = _label_value(labels, label_key)
            uid = (self._by_cost_center.get(value) or (value if value in self.units else None)) if value else None
            if uid is not None:
                return uid, "label"
        return UNASSIGNED, "unassigned"


_MODELS = weakref.WeakKeyDictionary()   # aux document store -> (generation, OrgModel)
_MODELS_LOCK = threading.Lock()


def _forget(store):
    try:
        with _MODELS_LOCK:
            _MODELS.pop(store, None)
    except TypeError:  # a store that cannot be weakly referenced is never cached
        pass


def load(ctx):
    """The stored model as an OrgModel. The index is rebuilt only when the document changed (cached per document
    store and generation). Raises ApiError(upstream_error) when the document cannot be read."""
    store = aux_docs(getattr(ctx, "app", None))
    try:
        doc, generation = store.load(DOC)
    except (StoreError, OSError) as err:
        LOG.warning("organization model could not be read: %s", type(err).__name__)
        raise ApiError("upstream_error", "The organization model could not be read. Try again.")
    try:
        with _MODELS_LOCK:
            hit = _MODELS.get(store)
    except TypeError:
        hit = None
    if hit is not None and hit[0] == generation:
        return hit[1]
    model = OrgModel(doc)
    try:
        with _MODELS_LOCK:
            _MODELS[store] = (generation, model)
    except TypeError:
        pass
    return model


def tree(ctx):
    """GET /api/v1/org/tree: the units in depth-first order with their direct member count, the member count
    including all sub-units and their project ids. Member e-mails are never included."""
    model = load(ctx)
    direct = {}
    for uid in model.members.values():
        direct[uid] = direct.get(uid, 0) + 1
    total = dict(direct)
    order = model.dfs_order()
    for uid in reversed(order):  # every unit comes after its parent in depth-first order
        parent = model.units[uid]["parent"]
        if parent is not None:
            total[parent] = total.get(parent, 0) + total.get(uid, 0)
    projects = {}
    for pid, uid in model.projects.items():
        projects.setdefault(uid, []).append(pid)
    settings = getattr(ctx, "settings", None)
    chargeback = settings.get("chargeback") if isinstance(settings, dict) else None
    label_key = chargeback.get("project_unit_label") if isinstance(chargeback, dict) else ""
    units = []
    for uid in order:
        unit = model.units[uid]
        units.append({"id": uid, "name": unit["name"], "parent": unit["parent"], "path": unit["path"],
                      "costCenter": unit["costCenter"], "depth": unit["depth"], "members": direct.get(uid, 0),
                      "membersTotal": total.get(uid, 0), "projects": sorted(projects.get(uid, ()))})
    return {"version": model.version, "updatedAt": model.updated_at, "updatedBy": model.updated_by,
            "source": dict(model.source), "empty": model.empty,
            "labelKey": label_key if isinstance(label_key, str) else "",
            "counts": {"units": len(model.units), "members": len(model.members), "projects": len(model.projects)},
            "units": units}
