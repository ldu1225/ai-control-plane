"""Google Cloud access: ADC credentials, REST helper and BigQuery execution."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import logging
import re
import threading
import time
import urllib.parse

LOG = logging.getLogger("controlplane.gcp")

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
QUERY_TIMEOUT_SECONDS = 60
_PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUOTED_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")


def log_safe(message, limit=200):
    """Upstream error text for server logs: quoted literals (which can echo user questions or prompt text)
    and e-mail addresses are removed, and the result is cut to `limit` characters. Clients never receive
    upstream messages; they get a curated message instead."""
    text = _QUOTED_RE.sub("\x00", str(message or "").replace("\x00", ""))
    text = re.sub(r"['\"][^'\"]*$", "\x00", text)  # an unterminated literal runs to the end
    text = _EMAIL_RE.sub("<email>", text.replace("\x00", "'?'"))
    return text[:limit]


def sql_parameter_names(sql):
    """Named query parameters (@name) outside string literals, comments and quoted identifiers.

    Text such as '%@example.com' inside a literal is not a parameter, and @@system variables
    are ignored."""
    names = set()
    i, n = 0, len(sql or "")
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "#" or (ch == "-" and nxt == "-"):
            end = sql.find("\n", i)
            i = n if end == -1 else end + 1
            continue
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch * 3 if ch != "`" and sql.startswith(ch * 3, i) else ch
            j = i + len(quote)
            while j < n and not sql.startswith(quote, j):
                j += 2 if (sql[j] == "\\" and ch != "`") else 1
            i = j + len(quote)
            continue
        if ch == "@":
            if nxt == "@":
                i += 2
                while i < n and (sql[i].isalnum() or sql[i] == "_"):
                    i += 1
                continue
            m = _PARAM_NAME_RE.match(sql, i + 1)
            if m:
                names.add(m.group(0))
                i = m.end()
                continue
        i += 1
    return names


class GcpError(Exception):
    """An upstream Google Cloud failure. `kind` is one of: credentials, not_found, denied,
    invalid, cost_limit, timeout, conflict, precondition, unavailable, http, network."""

    def __init__(self, kind, status=None, message="", reason=""):
        super().__init__(message or kind)
        self.kind = kind
        self.status = status
        self.message = message or ""
        self.reason = reason or ""


def _kind_for_status(status, reason=""):
    if status == 404:
        return "not_found"
    if status in (401, 403):
        return "denied"
    if status == 409 or reason == "ABORTED":
        return "conflict"
    if status == 412:
        return "precondition"
    if status == 429:
        return "unavailable"
    if status == 400:
        return "invalid"
    if status and status >= 500:
        return "unavailable"
    return "http"


def _google_error(payload):
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        err = payload["error"]
        return str(err.get("message") or "")[:500], str(err.get("status") or "")
    return "", ""


class Gcp:
    """Shared credentials, thread-local HTTP sessions, BigQuery clients and a worker pool."""

    def __init__(self, max_workers=32):
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._base = None
        self._base_error_at = 0.0
        self._scoped = {}
        self._bq = {}
        self._local = threading.local()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers,
                                                          thread_name_prefix="cp-query")
        self.subpool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers,
                                                             thread_name_prefix="cp-sub")
        self.orchestrator = concurrent.futures.ThreadPoolExecutor(max_workers=16,
                                                                  thread_name_prefix="cp-orch")

    # -- credentials ------------------------------------------------------------
    def credentials(self, quota_project):
        with self._lock:
            if self._base is None:
                if self._base_error_at and time.monotonic() - self._base_error_at < 30:
                    raise GcpError("credentials", message="Application Default Credentials unavailable")
                try:
                    import google.auth
                    creds, _ = google.auth.default(scopes=SCOPES)
                except Exception as err:  # pylint: disable=broad-except
                    self._base_error_at = time.monotonic()
                    LOG.error("ADC unavailable: %s", type(err).__name__)
                    raise GcpError("credentials", message=type(err).__name__)
                self._base = creds
            creds = self._scoped.get(quota_project)
            if creds is None:
                creds = self._base
                if quota_project and hasattr(creds, "with_quota_project"):
                    try:
                        creds = creds.with_quota_project(quota_project)
                    except Exception:  # pylint: disable=broad-except
                        creds = self._base
                self._scoped[quota_project] = creds
            return creds

    def auth_headers(self, quota_project):
        creds = self.credentials(quota_project)
        with self._refresh_lock:
            if not creds.valid:
                try:
                    import google.auth.transport.requests as ga_requests
                    creds.refresh(ga_requests.Request())
                except Exception as err:  # pylint: disable=broad-except
                    LOG.error("credential refresh failed: %s", type(err).__name__)
                    raise GcpError("credentials", message=type(err).__name__)
            headers = {}
            creds.apply(headers)
        if quota_project:
            headers["x-goog-user-project"] = quota_project
        return headers

    def _session(self):
        sess = getattr(self._local, "session", None)
        if sess is None:
            import requests
            sess = requests.Session()
            self._local.session = sess
        return sess

    # -- REST ---------------------------------------------------------------------
    def request(self, method, url, *, quota_project, params=None, json_body=None, data=None,
                headers=None, timeout=30, expect_json=True):
        """Performs an authenticated REST call. Returns (payload, response_headers)."""
        import requests
        hdrs = self.auth_headers(quota_project)
        if headers:
            hdrs.update(headers)
        for attempt in (1, 2):
            try:
                resp = self._session().request(method, url, params=params, json=json_body, data=data,
                                               headers=hdrs, timeout=timeout, allow_redirects=False)
            except requests.exceptions.Timeout:
                raise GcpError("timeout", message="request timed out")
            except requests.exceptions.RequestException as err:
                raise GcpError("network", message=type(err).__name__)
            if resp.status_code == 401 and attempt == 1:
                creds = self.credentials(quota_project)
                with self._refresh_lock:
                    try:
                        import google.auth.transport.requests as ga_requests
                        creds.refresh(ga_requests.Request())
                    except Exception:  # pylint: disable=broad-except
                        break
                hdrs.update(self.auth_headers(quota_project))
                continue
            break
        payload = None
        if expect_json and resp.content:
            try:
                payload = resp.json()
            except ValueError:
                payload = None
        if resp.status_code >= 300:
            message, reason = _google_error(payload)
            parsed = urllib.parse.urlsplit(url)
            LOG.warning("upstream error method=%s host=%s path=%s status=%s reason=%s message=%s", method,
                        parsed.hostname, parsed.path[:160], resp.status_code, reason, log_safe(message))
            raise GcpError(_kind_for_status(resp.status_code, reason), status=resp.status_code,
                           message=message, reason=reason)
        if not expect_json:
            return resp.content, resp.headers
        return payload if payload is not None else {}, resp.headers

    def paginate(self, url, *, quota_project, items_key, params=None, max_pages=10, page_size=None,
                 method="GET", json_body=None, token_param="pageToken"):
        """Collects items across pages. Returns (items, truncated)."""
        items = []
        params = dict(params or {})
        body = dict(json_body or {})
        if page_size:
            if method == "GET":
                params["pageSize"] = page_size
            else:
                body["pageSize"] = page_size
        token = None
        for _ in range(max_pages):
            if token:
                if method == "GET":
                    params[token_param] = token
                else:
                    body[token_param] = token
            payload, _ = self.request(method, url, quota_project=quota_project, params=params or None,
                                      json_body=body if method != "GET" else None)
            items.extend(payload.get(items_key) or [])
            token = payload.get("nextPageToken")
            if not token:
                return items, False
        return items, True

    # -- BigQuery -----------------------------------------------------------------
    def bq_client(self, quota_project):
        creds = self.credentials(quota_project)
        with self._lock:
            client = self._bq.get(quota_project)
            if client is None:
                from google.cloud import bigquery
                client = bigquery.Client(project=quota_project, credentials=creds)
                self._bq[quota_project] = client
            return client

    @staticmethod
    def build_params(sql, params):
        """Converts {name: value | (type, value)} into BigQuery parameters used by `sql`."""
        from google.cloud import bigquery
        used = sql_parameter_names(sql)
        out = []
        for name, value in (params or {}).items():
            if name not in used:
                continue
            ptype = None
            if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
                ptype, value = value
            if isinstance(value, (list, set, frozenset)):
                values = list(value)
                out.append(bigquery.ArrayQueryParameter(name, ptype or "STRING", values))
                continue
            if ptype is None:
                if isinstance(value, bool):
                    ptype = "BOOL"
                elif isinstance(value, int):
                    ptype = "INT64"
                elif isinstance(value, float):
                    ptype = "FLOAT64"
                elif isinstance(value, dt.datetime):
                    ptype = "TIMESTAMP"
                elif isinstance(value, dt.date):
                    ptype = "DATE"
                else:
                    ptype = "STRING"
            out.append(bigquery.ScalarQueryParameter(name, ptype, value))
        missing = used - {p.name for p in out}
        if missing:
            raise ValueError("missing query parameters: %s" % ", ".join(sorted(missing)))
        return out

    def query(self, sql, params=None, *, quota_project, module, max_bytes, max_rows=None,
              timeout=QUERY_TIMEOUT_SECONDS):
        """Runs a parameterized query. Returns a QueryResult. Raises GcpError."""
        from google.api_core import exceptions as gexc
        from google.cloud import bigquery
        try:
            query_parameters = self.build_params(sql, params)
        except ValueError as err:
            LOG.warning("query parameter error module=%s: %s", module, err)
            raise GcpError("invalid", message=str(err))
        client = self.bq_client(quota_project)
        job_config = bigquery.QueryJobConfig(
            query_parameters=query_parameters,
            use_query_cache=True,
            labels={"app": "control-plane", "module": re.sub(r"[^a-z0-9_\-]", "_", module.lower())[:63]},
            maximum_bytes_billed=int(max_bytes),
        )
        job = None
        try:
            job = client.query(sql, job_config=job_config, timeout=30)
            rows_iter = job.result(timeout=timeout, max_results=max_rows)
            schema = [(f.name, f.field_type) for f in (rows_iter.schema or [])]
            rows = [dict(row.items()) for row in rows_iter]
            total = getattr(rows_iter, "total_rows", None)
            return QueryResult(rows, schema, job.total_bytes_processed, total)
        except (concurrent.futures.TimeoutError, TimeoutError):
            if job is not None:
                try:
                    job.cancel()
                except Exception:  # pylint: disable=broad-except
                    pass
            raise GcpError("timeout", message="query timed out")
        except gexc.GoogleAPICallError as err:
            raise _classify_api_error(err, module)
        except GcpError:
            raise
        except Exception as err:  # pylint: disable=broad-except
            name = type(err).__name__
            if "RefreshError" in name or "DefaultCredentialsError" in name or "TransportError" in name:
                raise GcpError("credentials", message=name)
            if "Timeout" in name:
                raise GcpError("timeout", message=name)
            LOG.warning("query failure module=%s error=%s", module, name)
            raise GcpError("http", message=name)

    # -- concurrency --------------------------------------------------------------
    def run_parallel(self, tasks, pool=None):
        """Runs {name: callable} concurrently. Returns {name: (ok, value_or_exception)}."""
        current = threading.current_thread().name
        if pool is None:
            pool = self.subpool if current.startswith("cp-query") else self.pool
        inline = (current.startswith("cp-sub") or
                  (pool is self.pool and current.startswith("cp-query")) or
                  (pool is self.orchestrator and current.startswith("cp-orch")))
        results = {}
        if inline or len(tasks) <= 1:
            for name, fn in tasks.items():
                try:
                    results[name] = (True, fn())
                except Exception as err:  # pylint: disable=broad-except
                    results[name] = (False, err)
            return results
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                results[name] = (True, fut.result())
            except Exception as err:  # pylint: disable=broad-except
                results[name] = (False, err)
        return results


class QueryResult:
    def __init__(self, rows, schema, bytes_processed, total_rows):
        self.rows = rows
        self.schema = schema
        self.bytes_processed = bytes_processed or 0
        self.total_rows = total_rows


def _classify_api_error(err, module):
    status = getattr(err, "code", None)
    message = str(getattr(err, "message", "") or "")[:500]
    reasons = []
    for item in getattr(err, "errors", None) or []:
        if isinstance(item, dict) and item.get("reason"):
            reasons.append(str(item["reason"]))
    reason = ",".join(reasons)
    LOG.warning("bigquery error module=%s status=%s reason=%s message=%s", module, status, reason,
                log_safe(message))
    if "bytesBilledLimitExceeded" in reason or "bytes billed" in message.lower():
        return GcpError("cost_limit", status=status, message=message, reason=reason)
    if status == 404 or "notFound" in reason:
        return GcpError("not_found", status=status, message=message, reason=reason)
    if status in (401, 403) or "accessDenied" in reason:
        return GcpError("denied", status=status, message=message, reason=reason)
    if status == 400:
        return GcpError("invalid", status=status, message=message, reason=reason)
    return GcpError(_kind_for_status(status or 0), status=status, message=message, reason=reason)
