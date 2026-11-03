"""Schema-adaptive data access.

SchemaRegistry discovers tables (including date-sharded tables) through
INFORMATION_SCHEMA, reads table schemas, and builds SQL expressions that only
reference columns that exist. Table identifiers come from validated settings or
INFORMATION_SCHEMA and are validated with strict regexes before interpolation;
all values are passed as query parameters.
"""

from __future__ import annotations

import logging
import re

from .cache import TTLCache
from .config import COLUMN_SEGMENT_RE, valid_dataset, valid_project, valid_table

LOG = logging.getLogger("controlplane.schema")

SHARD_RE = re.compile(r"^(.+)_(\d{8})$")
SEGMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d{1,3})\])?$")
TYPE_ALIASES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT",
                "NUMERIC": "NUMERIC", "BIGNUMERIC": "BIGNUMERIC"}
TABLES_TTL = 600
SCHEMA_TTL = 1800


def norm_type(field_type):
    field_type = (field_type or "").upper()
    return TYPE_ALIASES.get(field_type, field_type)


def quote_ident(name):
    if not COLUMN_SEGMENT_RE.match(name or ""):
        raise ValueError("invalid identifier")
    return "`%s`" % name


def table_ref(project, dataset, table):
    if not valid_project(project) or not valid_dataset(dataset):
        raise ValueError("invalid table reference")
    if table.endswith("_*"):
        if not valid_table(table[:-2]):
            raise ValueError("invalid table reference")
    elif not valid_table(table):
        raise ValueError("invalid table reference")
    return "`%s.%s.%s`" % (project, dataset, table)


class Node:
    __slots__ = ("name", "type", "mode", "children", "strict_children")

    def __init__(self, name, ftype, mode, children=None, strict_children=False):
        self.name = name
        self.type = norm_type(ftype)
        self.mode = (mode or "NULLABLE").upper()
        self.children = children or {}
        self.strict_children = bool(strict_children)


class TableSchema:
    """Lower-cased lookup tree over a BigQuery schema."""

    def __init__(self, fields, partition_field=None, ingestion_partitioned=False):
        self.tree = self._build(fields)
        self.partition_field = partition_field
        self.ingestion_partitioned = ingestion_partitioned

    @classmethod
    def from_dicts(cls, fields, **kwargs):
        """Builds a schema from [{"name", "type", "mode", "fields": [...]}] (used by tests)."""
        return cls([_DictField(f) for f in fields], **kwargs)

    def _build(self, fields):
        tree = {}
        for field in fields or []:
            name = field.name
            if not COLUMN_SEGMENT_RE.match(name or ""):
                continue
            children = self._build(getattr(field, "fields", None) or [])
            tree[name.lower()] = Node(name, field.field_type, field.mode, children)
        return tree

    def node(self, path):
        cur = self.tree
        node = None
        for part in path.split("."):
            node = cur.get(part.lower())
            if node is None:
                return None
            cur = node.children
        return node

    def has(self, path):
        return self.node(path) is not None

    def leaf_paths(self, limit=200):
        out = []

        def walk(children, prefix):
            for node in children.values():
                if len(out) >= limit:
                    return
                path = "%s.%s" % (prefix, node.name) if prefix else node.name
                if node.type == "STRUCT" and node.children:
                    walk(node.children, path)
                else:
                    out.append(path)
        walk(self.tree, "")
        return out

    def leaf_types(self, limit=200):
        out = []

        def walk(children, prefix, repeated):
            for node in children.values():
                if len(out) >= limit:
                    return
                path = "%s.%s" % (prefix, node.name) if prefix else node.name
                rep = repeated or node.mode == "REPEATED"
                if node.type == "STRUCT" and node.children:
                    walk(node.children, path, rep)
                else:
                    out.append((path, node.type + (" REPEATED" if rep else "")))
        walk(self.tree, "", False)
        return out

    def first_timestamp(self):
        for node in self.tree.values():
            if node.type == "TIMESTAMP" and node.mode != "REPEATED":
                return node.name
        return None


class _DictField:
    def __init__(self, d):
        self.name = d.get("name")
        self.field_type = d.get("type", "STRING")
        self.mode = d.get("mode", "NULLABLE")
        self.fields = [_DictField(x) for x in d.get("fields", [])]


# ---------------------------------------------------------------------------
# Expression building
# ---------------------------------------------------------------------------

def _parse_candidate(candidate):
    segs = []
    for raw in candidate.split("."):
        m = SEGMENT_RE.match(raw)
        if not m:
            return None
        segs.append((m.group(1), int(m.group(2)) if m.group(2) is not None else None))
    return segs


def _json_path(parts):
    path = "$"
    for name, idx in parts:
        if not COLUMN_SEGMENT_RE.match(name):
            return None
        path += "." + name
        if idx is not None:
            path += "[%d]" % idx
    return path


def build_expr(alias, schema, candidate):
    """Returns (sql, type) for one candidate path or None if the path does not exist.

    - direct column path              -> alias.`a`.`b`
    - path into a JSON or JSON-string -> JSON_VALUE(alias.`col`, '$.rest')
    - path through REPEATED fields    -> JSON_VALUE(TO_JSON_STRING(alias.`root`), '$.a[0].b')
    """
    if schema is None or not COLUMN_SEGMENT_RE.match(alias or ""):
        return None
    segs = _parse_candidate(candidate)
    if not segs:
        return None
    children = schema.tree
    resolved = []  # (actual name, index, node)
    for i, (name, idx) in enumerate(segs):
        node = children.get(name.lower())
        if node is None:
            return None
        resolved.append((node.name, idx, node))
        is_last = i == len(segs) - 1
        if node.type == "JSON" or (node.type == "STRING" and not is_last):
            rest = segs[i + 1:]
            if rest and node.children:
                mapped_rest = []
                cur_children = node.children
                cur_strict = node.strict_children
                for rname, ridx in rest:
                    if cur_children:
                        cnode = cur_children.get(rname.lower())
                        if cnode is None:
                            if cur_strict:
                                return None
                            mapped_rest.append((rname, ridx))
                            cur_children = {}
                            cur_strict = False
                        else:
                            eff_idx = 0 if (ridx is None and cnode.mode == "REPEATED") else ridx
                            mapped_rest.append((cnode.name, eff_idx))
                            cur_children = cnode.children
                            cur_strict = cnode.strict_children
                    else:
                        if cur_strict:
                            return None
                        mapped_rest.append((rname, ridx))
                rest = mapped_rest
            elif rest and node.strict_children:
                return None
            if any(n.mode == "REPEATED" or ix is not None for _, ix, n in resolved):
                root_name = resolved[0][0]
                inner = [(n, 0 if (ix is None and nd.mode == "REPEATED") else ix)
                         for n, ix, nd in resolved[1:]] + rest
                path = _json_path_with_root(resolved[0], inner)
                if path is None:
                    return None
                return "JSON_VALUE(TO_JSON_STRING(%s.%s), '%s')" % (alias, quote_ident(root_name), path), "STRING"
            base = "%s.%s" % (alias, ".".join(quote_ident(n) for n, _, _ in resolved))
            if not rest:
                return "JSON_VALUE(%s)" % base if node.type == "JSON" else base, "STRING"
            path = _json_path(rest)
            if path is None:
                return None
            return "JSON_VALUE(%s, '%s')" % (base, path), "STRING"
        if not is_last:
            if node.type != "STRUCT":
                return None
            children = node.children
    leaf = resolved[-1][2]
    if leaf.type == "STRUCT":
        return None
    repeated = any(n.mode == "REPEATED" or ix is not None for _, ix, n in resolved)
    if not repeated:
        return "%s.%s" % (alias, ".".join(quote_ident(n) for n, _, _ in resolved)), leaf.type
    inner = []
    for name, idx, node in resolved[1:]:
        if node.mode == "REPEATED" and idx is None:
            idx = 0
        inner.append((name, idx))
    path = _json_path_with_root(resolved[0], inner)
    if path is None:
        return None
    return "JSON_VALUE(TO_JSON_STRING(%s.%s), '%s')" % (alias, quote_ident(resolved[0][0]), path), "STRING"


def _json_path_with_root(root, inner):
    name, idx, node = root
    path = "$"
    if node.mode == "REPEATED" or idx is not None:
        path += "[%d]" % (idx or 0)
    rest = _json_path(inner)
    if rest is None:
        return None
    return path + rest[1:]


def expr(alias, schema, *candidates, cast=None):
    """COALESCE over the candidate paths that exist. Returns SQL or None.

    Mixed types are cast to a common type (cast argument, else STRING) with SAFE_CAST."""
    found = []
    for candidate in candidates:
        res = build_expr(alias, schema, candidate)
        if res and res[0] not in [s for s, _ in found]:
            found.append(res)
    if not found:
        return None
    target = norm_type(cast) if cast else None
    if target is None and len({t for _, t in found}) > 1:
        target = "STRING"
    parts = []
    for sql, ftype in found:
        if target and ftype != target:
            parts.append("SAFE_CAST(%s AS %s)" % (sql, target))
        else:
            parts.append(sql)
    return parts[0] if len(parts) == 1 else "COALESCE(%s)" % ", ".join(parts)


# ---------------------------------------------------------------------------
# Sources and the registry
# ---------------------------------------------------------------------------

class Source:
    """A resolved table (plain or date-sharded) with its schema."""

    def __init__(self, project, dataset, logical, kind, table, schema, latest=None):
        self.project = project
        self.dataset = dataset
        self.logical = logical
        self.kind = kind
        self.table = table
        self.schema = schema
        self.latest = latest

    @property
    def label(self):
        return "bigquery:%s.%s" % (self.dataset, self.logical)

    @property
    def sharded(self):
        return self.kind == "sharded"

    def fq(self):
        name = self.table + "_*" if self.sharded else self.table
        return table_ref(self.project, self.dataset, name)

    def time_column(self, preferred=("timestamp", "receiveTimestamp")):
        for name in preferred:
            node = self.schema.node(name) if self.schema else None
            if node is not None and node.type == "TIMESTAMP" and node.mode != "REPEATED" and "." not in name:
                return node.name
        return self.schema.first_timestamp() if self.schema else None

    def cte(self, name, start="start_ts", end="end_ts", time_col=None, sfx=("sfx_start", "sfx_end")):
        """`name AS (SELECT * FROM table WHERE <time filter>)` with shard suffix pruning."""
        if not COLUMN_SEGMENT_RE.match(name):
            raise ValueError("invalid CTE name")
        col = time_col or self.time_column()
        if col is None:
            raise ValueError("no time column")
        where = []
        if self.sharded:
            where.append("_TABLE_SUFFIX BETWEEN @%s AND @%s" % sfx)
        if self.schema is not None and self.schema.ingestion_partitioned:
            where.append("_PARTITIONTIME >= TIMESTAMP_SUB(@%s, INTERVAL 3 DAY)" % start)
        where.append("%s >= @%s" % (quote_ident(col), start))
        where.append("%s < @%s" % (quote_ident(col), end))
        return "%s AS (SELECT * FROM %s WHERE %s)" % (name, self.fq(), " AND ".join(where))


_SCALAR_UNION_TYPES = frozenset({"TIMESTAMP", "STRING", "INT64", "FLOAT64", "BOOL", "DATE", "NUMERIC", "BIGNUMERIC"})


def _merge_node_children(nodes):
    merged = {}
    order = []
    by_key = {}
    for parent in nodes:
        for k, child in (parent.children or {}).items():
            if k not in by_key:
                order.append(k)
                by_key[k] = []
            by_key[k].append(child)
    for k in order:
        group = by_key[k]
        first = group[0]
        sub_children = _merge_node_children(group)
        schemaless = any(n.type in ("JSON", "STRING") and not n.children for n in group)
        mode = "REPEATED" if any(n.mode == "REPEATED" for n in group) else first.mode
        ftype = "STRUCT" if sub_children else first.type
        merged[k] = Node(first.name, ftype, mode, children=sub_children, strict_children=bool(sub_children and not schemaless))
    return merged


class UnionSource:
    """Multi-project BigQuery source combining identical logical tables across sibling projects via UNION ALL."""

    def __init__(self, sources):
        if not sources:
            raise ValueError("UnionSource requires at least one Source")
        self.sources = list(sources)
        first = self.sources[0]
        self.project = first.project
        self.projects = [s.project for s in self.sources]
        self.dataset = first.dataset
        self.logical = first.logical
        self.kind = "union"
        self.table = first.table
        self.latest = first.latest
        self._col_specs, self.schema = self._unify_schemas()

    def _unify_schemas(self):
        order = []
        by_key = {}
        for src in self.sources:
            if src.schema is None:
                continue
            for k, node in src.schema.tree.items():
                if k not in by_key:
                    order.append(k)
                    by_key[k] = []
                by_key[k].append(node)
        specs = []
        unified_tree = {}
        for k in order:
            nodes = by_key[k]
            canonical = nodes[0].name
            types = {n.type for n in nodes}
            all_scalar = all(n.mode != "REPEATED" and n.type in _SCALAR_UNION_TYPES for n in nodes) and len(types) == 1
            if all_scalar:
                stype = nodes[0].type
                specs.append((k, canonical, "scalar", stype))
                unified_tree[k] = Node(canonical, stype, "NULLABLE")
            else:
                children = _merge_node_children(nodes)
                schemaless = any(n.type in ("JSON", "STRING") and not n.children for n in nodes)
                specs.append((k, canonical, "json", "JSON"))
                unified_tree[k] = Node(canonical, "JSON", "NULLABLE", children=children,
                                       strict_children=not schemaless)
        schema = TableSchema([])
        schema.tree = unified_tree
        schema.ingestion_partitioned = any(bool(s.schema and s.schema.ingestion_partitioned) for s in self.sources)
        return specs, schema

    @property
    def label(self):
        return "bigquery:%s.%s" % (self.dataset, self.logical)

    @property
    def sharded(self):
        return any(s.sharded for s in self.sources)

    def time_column(self, preferred=("timestamp", "receiveTimestamp")):
        for name in preferred:
            node = self.schema.node(name) if self.schema else None
            if node is not None and node.type == "TIMESTAMP" and node.mode != "REPEATED" and "." not in name:
                return node.name
        return self.schema.first_timestamp() if self.schema else None

    def cte(self, name, start="start_ts", end="end_ts", time_col=None, sfx=("sfx_start", "sfx_end")):
        if not COLUMN_SEGMENT_RE.match(name):
            raise ValueError("invalid CTE name")
        branches = []
        for src in self.sources:
            col = time_col if (time_col and src.schema and src.schema.node(time_col)) else src.time_column()
            if col is None:
                continue
            selects = []
            for k, canonical, mode, stype in self._col_specs:
                snode = src.schema.tree.get(k) if src.schema else None
                q_out = quote_ident(canonical)
                if snode is None:
                    selects.append("CAST(NULL AS %s) AS %s" % (stype, q_out))
                elif mode == "scalar":
                    selects.append("%s AS %s" % (quote_ident(snode.name), q_out))
                elif snode.type == "JSON" and snode.mode != "REPEATED":
                    selects.append("%s AS %s" % (quote_ident(snode.name), q_out))
                else:
                    selects.append("TO_JSON(%s) AS %s" % (quote_ident(snode.name), q_out))
            where = []
            if src.sharded:
                where.append("_TABLE_SUFFIX BETWEEN @%s AND @%s" % sfx)
            if src.schema is not None and src.schema.ingestion_partitioned:
                where.append("_PARTITIONTIME >= TIMESTAMP_SUB(@%s, INTERVAL 3 DAY)" % start)
            where.append("%s >= @%s" % (quote_ident(col), start))
            where.append("%s < @%s" % (quote_ident(col), end))
            branches.append("SELECT %s FROM %s WHERE %s" % (", ".join(selects), src.fq(), " AND ".join(where)))
        if not branches:
            raise ValueError("no time column")
        return "%s AS (%s)" % (name, " UNION ALL ".join(branches))


def group_tables(rows):
    """Groups INFORMATION_SCHEMA rows into logical tables. Plain tables win over shards."""
    plain = {}
    shards = {}
    for name, ttype in rows:
        if not isinstance(name, str) or not valid_table(name):
            continue
        m = SHARD_RE.match(name)
        if m:
            shards.setdefault(m.group(1), []).append(m.group(2))
        plain[name] = ttype
    groups = {}
    for logical, suffixes in shards.items():
        if logical in plain:
            continue
        latest = max(suffixes)
        groups[logical] = {"kind": "sharded", "table": logical, "latest": "%s_%s" % (logical, latest),
                           "shards": len(suffixes), "type": plain.get("%s_%s" % (logical, latest))}
    for name, ttype in plain.items():
        m = SHARD_RE.match(name)
        if m and m.group(1) in groups:
            continue
        groups[name] = {"kind": "table", "table": name, "latest": name, "shards": 0, "type": ttype}
    return groups


class SchemaRegistry:
    def __init__(self, gcp):
        self.gcp = gcp
        self._tables = TTLCache(256)
        self._schemas = TTLCache(1024)

    def tables(self, project, dataset, quota_project, max_bytes):
        """Returns {logical: info}. Raises GcpError (not_found when the dataset is missing)."""
        if not valid_project(project) or not valid_dataset(dataset):
            raise ValueError("invalid dataset reference")

        def load():
            sql = "SELECT table_name, table_type FROM `%s.%s.INFORMATION_SCHEMA.TABLES`" % (project, dataset)
            res = self.gcp.query(sql, {}, quota_project=quota_project, module="schema", max_bytes=max_bytes)
            return group_tables([(r.get("table_name"), r.get("table_type")) for r in res.rows])

        value, _ = self._tables.get_or_compute(("tables", project, dataset), TABLES_TTL, load)
        return value

    def resolve(self, project, dataset, logical, quota_project, max_bytes):
        groups = self.tables(project, dataset, quota_project, max_bytes)
        info = groups.get(logical)
        if info is None:
            return {"kind": "missing", "fq": None}
        name = info["table"] + ("_*" if info["kind"] == "sharded" else "")
        return {"kind": info["kind"], "fq": "%s.%s.%s" % (project, dataset, name), "table": info["table"],
                "latest": info["latest"]}

    def schema(self, project, dataset, table, quota_project):
        """Schema of a concrete table (for sharded tables pass the latest shard)."""
        if not valid_project(project) or not valid_dataset(dataset) or not valid_table(table):
            raise ValueError("invalid table reference")

        def load():
            from .gcp import GcpError, _classify_api_error
            client = self.gcp.bq_client(quota_project)
            try:
                tbl = client.get_table("%s.%s.%s" % (project, dataset, table), timeout=30)
            except Exception as err:  # pylint: disable=broad-except
                if hasattr(err, "code") and hasattr(err, "message"):
                    raise _classify_api_error(err, "schema")
                raise GcpError("http", message=type(err).__name__)
            partitioning = getattr(tbl, "time_partitioning", None)
            ingestion = bool(partitioning is not None and not getattr(partitioning, "field", None))
            return {"schema": TableSchema(tbl.schema, partition_field=getattr(partitioning, "field", None),
                                          ingestion_partitioned=ingestion),
                    "rows": getattr(tbl, "num_rows", None), "bytes": getattr(tbl, "num_bytes", None),
                    "modified": getattr(tbl, "modified", None)}

        value, _ = self._schemas.get_or_compute(("schema", project, dataset, table), SCHEMA_TTL, load)
        return value

    def source(self, project, dataset, logical, quota_project, max_bytes):
        """Returns a Source or None when the logical table does not exist."""
        res = self.resolve(project, dataset, logical, quota_project, max_bytes)
        if res["kind"] == "missing":
            return None
        meta = self.schema(project, dataset, res["latest"], quota_project)
        return Source(project, dataset, logical, res["kind"], res["table"], meta["schema"], latest=res["latest"])

    def table_source(self, project, dataset, table, quota_project):
        """Source for an exact (non-discovered) table such as the billing export table."""
        meta = self.schema(project, dataset, table, quota_project)
        return Source(project, dataset, table, "table", table, meta["schema"], latest=table)

    def invalidate(self):
        self._tables.clear()
        self._schemas.clear()
