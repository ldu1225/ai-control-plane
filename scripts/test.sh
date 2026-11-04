#!/usr/bin/env bash
# Unified test runner for AI Control Plane:
#   ./scripts/test.sh                                         # all offline suites (unit, bash -n, prepublish, analyst validate)
#   ./scripts/test.sh --live --project PROJECT_ID [--mutate]  # plus live Cloud Run E2E verification

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE=0
MUTATE=0
SKIP_OFFLINE=0
PROJECT_ID=""
REGION=""
SERVICE_NAME="ai-control-plane"
ADMIN_EMAIL=""

usage() {
  cat <<'EOF'
Usage:
  scripts/test.sh [options]

Default (no flags):
  Runs all offline suites:
    1. Unit tests (python3 -m unittest discover -s tests/unit -v)
    2. Shell syntax checks (bash -n scripts/*.sh scripts/lib/*.sh)
    3. Prepublish security/leak scan (python3 scripts/prepublish_scan.py)
    4. AI Analyst golden dataset validation (python3 eval/analyst/run_eval.py validate)

Live E2E options:
  --live                 Verify a deployed Cloud Run service end-to-end
  --project ID           GCP project ID hosting Cloud Run (required with --live)
  --region REGION        Cloud Run region (auto-detected when omitted)
  --service NAME         Cloud Run service name (default: ai-control-plane)
  --admin EMAIL          Admin email for signed session check (default: active gcloud account)
  --mutate               Also test create/pause/resume/delete mutations on Telemetry Extensions
  --live-only            Skip offline suites and run only live E2E checks
  -h, --help             Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --live) LIVE=1; shift ;;
    --mutate) MUTATE=1; shift ;;
    --live-only) LIVE=1; SKIP_OFFLINE=1; shift ;;
    --project) PROJECT_ID="${2:-}"; shift 2 ;;
    --project=*) PROJECT_ID="${1#*=}"; shift ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --region=*) REGION="${1#*=}"; shift ;;
    --service) SERVICE_NAME="${2:-}"; shift 2 ;;
    --service=*) SERVICE_NAME="${1#*=}"; shift ;;
    --admin) ADMIN_EMAIL="${2:-}"; shift 2 ;;
    --admin=*) ADMIN_EMAIL="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

cd "$ROOT"

if [[ "$SKIP_OFFLINE" != "1" ]]; then
  echo "==> [1/4] Unit tests"
  python3 -m unittest discover -s tests/unit -v

  echo "==> [2/4] Shell script syntax (bash -n)"
  for f in scripts/install.sh scripts/deploy.sh scripts/prepare.sh scripts/teardown.sh scripts/test.sh scripts/lib/common.sh; do
    bash -n "$f"
  done
  echo "    OK: all shell scripts parsed cleanly"

  echo "==> [3/4] Prepublish security scan"
  python3 scripts/prepublish_scan.py

  echo "==> [4/4] AI Analyst golden dataset validation"
  python3 eval/analyst/run_eval.py validate
fi

if [[ "$LIVE" == "1" ]]; then
  if [[ -z "$PROJECT_ID" ]]; then
    echo "ERROR: --project PROJECT_ID is required with --live" >&2
    exit 2
  fi
  if [[ -z "$REGION" ]]; then
    REGION="$(gcloud run services list --project "$PROJECT_ID" --filter="metadata.name=${SERVICE_NAME}" \
      --format='value(metadata.labels."cloud.googleapis.com/location")' --limit=1 2>/dev/null | head -n1 || true)"
  fi
  if [[ -z "$REGION" ]]; then
    echo "ERROR: could not detect Cloud Run region for ${SERVICE_NAME} in ${PROJECT_ID}; pass --region" >&2
    exit 2
  fi
  if [[ -z "$ADMIN_EMAIL" ]]; then
    ADMIN_EMAIL="$(gcloud config get-value account 2>/dev/null | head -n1 || true)"
  fi
  BASE_URL="$(gcloud run services describe "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
    --format='value(status.url)' 2>/dev/null | tail -n1)"
  if [[ -z "$BASE_URL" ]]; then
    echo "ERROR: could not resolve Cloud Run URL for ${SERVICE_NAME} in ${PROJECT_ID} (${REGION})" >&2
    exit 2
  fi

  echo "==> [Live E2E] Verifying ${BASE_URL} (project=${PROJECT_ID}, region=${REGION}, mutate=${MUTATE})"
  ACP_TEST_ROOT="$ROOT" \
  ACP_TEST_PROJECT="$PROJECT_ID" \
  ACP_TEST_REGION="$REGION" \
  ACP_TEST_SERVICE="$SERVICE_NAME" \
  ACP_TEST_URL="$BASE_URL" \
  ACP_TEST_ADMIN="$ADMIN_EMAIL" \
  ACP_TEST_MUTATE="$MUTATE" \
  python3 - <<'PYLIVE'
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

root = os.environ["ACP_TEST_ROOT"]
sys.path.insert(0, os.path.join(root, "src"))

from controlplane.auth import Signer  # noqa: E402

project = os.environ["ACP_TEST_PROJECT"]
region = os.environ["ACP_TEST_REGION"]
service = os.environ["ACP_TEST_SERVICE"]
base_url = os.environ["ACP_TEST_URL"].rstrip("/")
admin_email = os.environ["ACP_TEST_ADMIN"].strip()
do_mutate = os.environ.get("ACP_TEST_MUTATE") == "1"

svc_raw = subprocess.check_output(
    ["gcloud", "run", "services", "describe", service, f"--project={project}", f"--region={region}", "--format=json"],
    text=True,
    stderr=subprocess.DEVNULL,
)
svc_json = json.loads(svc_raw[svc_raw.index("{"):])
secret_name = f"{service}-session-secret"
for item in (((svc_json.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers", [{}])[0].get("env", []):
    if item.get("name") == "PUBLIC_BASE_URL" and item.get("value"):
        base_url = item["value"].rstrip("/")
    elif item.get("name") == "SESSION_SECRET":
        ref = ((item.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
        if ref:
            secret_name = ref

raw_secret = subprocess.check_output(
    ["gcloud", "secrets", "versions", "access", "latest", f"--secret={secret_name}", f"--project={project}"],
    text=True,
    stderr=subprocess.DEVNULL,
)
secret_lines = [
    ln.strip() for ln in raw_secret.splitlines()
    if ln.strip() and not ln.startswith("Created enterprise-certificate-proxy")
]
secret = secret_lines[-1]

session_cookie = Signer(secret.encode("utf-8"), "session").sign(
    {"email": admin_email, "name": admin_email, "picture": "", "sub": "e2e:" + admin_email, "ims": int(time.time() * 1000)},
    3600,
)
csrf_token = ""


def call_api(method, path, body=None, auth=True, timeout=60):
    url = base_url + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if auth:
        cookies = [f"__Host-cp_session={session_cookie}"]
        if csrf_token:
            cookies.append(f"__Host-cp_csrf={csrf_token}")
            headers["X-CSRF-Token"] = csrf_token
        headers["Cookie"] = "; ".join(cookies)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, elapsed_ms, (json.loads(raw) if raw.strip().startswith("{") else raw)
    except urllib.error.HTTPError as err:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        raw = err.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return err.code, elapsed_ms, parsed


# 1. Public liveness (/health) & auth state
st, ms, _ = call_api("GET", "/health", auth=False)
assert st == 200, f"/health failed: {st}"
print(f"  [OK] GET /health -> {st} ({ms:.0f} ms) [{base_url}]")

st, ms, auth_state = call_api("GET", "/api/v1/auth/state", auth=False)
assert st == 200, f"/api/v1/auth/state failed: {st} {auth_state}"
print(f"  [OK] GET /api/v1/auth/state -> {st} ({ms:.0f} ms)")

# 2. Signed admin bootstrap + CSRF extraction
st, ms, boot = call_api("GET", "/api/v1/bootstrap")
assert st == 200, f"/api/v1/bootstrap failed: {st} {boot}"
sess = ((boot.get("data") or {}).get("session") or {})
assert sess.get("role") == "admin", f"expected admin role, got {sess}"
csrf_token = sess.get("csrfToken") or ""
assert csrf_token, "missing csrfToken in bootstrap"
print(f"  [OK] GET /api/v1/bootstrap -> {st} role={sess.get('role')} ({ms:.0f} ms)")

# 3. Trigger / verify maintenance so pricing & telemetry_discovery snapshot are warm
st, ms, maint = call_api("POST", "/api/v1/system/maintenance", body={}, timeout=180)
assert st == 200, f"POST /api/v1/system/maintenance failed: {st} {maint}"
runs = (maint.get("data") or {}).get("runs") or {}
print(f"  [OK] POST /api/v1/system/maintenance -> {st} ({ms:.0f} ms) tasks={sorted(runs.keys())}")

# 4. Read all core module endpoints
ENDPOINTS = [
    "/api/v1/health",
    "/api/v1/settings",
    "/api/v1/ai/models",
    "/api/v1/overview/summary",
    "/api/v1/gemini-enterprise/summary",
    "/api/v1/gemini-enterprise/licenses",
    "/api/v1/notebooklm/summary",
    "/api/v1/models/usage",
    "/api/v1/models/catalog",
    "/api/v1/agent-platform/gateway",
    "/api/v1/agent-platform/registry",
    "/api/v1/agent-platform/deployments",
    "/api/v1/model-armor/summary",
    "/api/v1/users",
    "/api/v1/governance/antigravity/status",
    "/api/v1/finops/summary",
    "/api/v1/finops/pricing",
    "/api/v1/telemetry/connectors",
    "/api/v1/telemetry/routing",
    "/api/v1/telemetry/discover",
]

TRANSIENT_CODES = {"upstream_unavailable", "timeout", "credentials_unavailable"}
for ep in ENDPOINTS:
    st, ms, payload = call_api("GET", ep, timeout=60)
    assert st == 200, f"GET {ep} returned {st}: {payload}"
    warnings = ((payload.get("meta") or {}).get("warnings") or []) if isinstance(payload, dict) else []
    bad = [w for w in warnings if w.get("code") in TRANSIENT_CODES]
    assert not bad, f"GET {ep} emitted transient warnings: {bad}"
    print(f"  [OK] GET {ep:<38} -> {st} ({ms:6.1f} ms) warnings={len(warnings)}")

# Warm latency check on telemetry/routing and telemetry/discover
_, ms_route, _ = call_api("GET", "/api/v1/telemetry/routing")
_, ms_disc, disc_payload = call_api("GET", "/api/v1/telemetry/discover")
disc_data = (disc_payload.get("data") or {})
print(
    f"  [OK] Warm Telemetry Extensions latency: routing={ms_route:.1f} ms, discover={ms_disc:.1f} ms "
    f"(tables={len(disc_data.get('tables') or [])}, logs={len(disc_data.get('logs') or [])}, "
    f"groups={len(disc_data.get('groups') or [])})"
)
assert ms_disc < 2000.0, f"discover warm latency too high: {ms_disc:.1f} ms"

# 5. Optional E2E mutation cycle (connector + live overlay + managed BigQuery log route + AI Analyst chat)
if do_mutate:
    # Clean up any leftover E2E Smoke Route from an interrupted run
    _, _, existing_rt = call_api("GET", "/api/v1/telemetry/routing")
    for rt in ((existing_rt.get("data") or {}).get("routes") or []):
        if rt.get("title") == "E2E Smoke Route" and rt.get("id"):
            call_api("POST", "/api/v1/telemetry/routing/routes/action", body={"id": rt["id"], "action": "delete"})

    st, _, saved = call_api("POST", "/api/v1/telemetry/connectors", body={
        "name": "E2E Smoke Connector",
        "enabled": True,
        "source": "logging",
        "logging": {"filter": f'logName="projects/{project}/logs/run.googleapis.com%2Fstdout"'},
        "mapping": {"time": "timestamp", "operation": "severity"},
    })
    assert st == 200, f"create connector failed: {st} {saved}"
    conn_id = ((saved.get("data") or {}).get("connector") or {}).get("id")
    assert conn_id, f"missing connector id: {saved}"
    print(f"  [OK] Created connector {conn_id}")

    st, _, disc_live = call_api("GET", "/api/v1/telemetry/discover")
    assert st == 200
    stdout_logs = [lg for lg in ((disc_live.get("data") or {}).get("logs") or [])
                   if lg.get("shortName") == "run.googleapis.com/stdout" and lg.get("project") == project]
    if stdout_logs:
        assert stdout_logs[0].get("connectorId") == conn_id, f"live overlay missed connector: {stdout_logs[0]}"
        print(f"  [OK] Verified instant live snapshot overlay for connector {conn_id}")

    st, _, deleted = call_api("POST", "/api/v1/telemetry/connectors/delete", body={"id": conn_id})
    assert st == 200, f"delete connector failed: {st} {deleted}"
    print(f"  [OK] Deleted connector {conn_id}")

    st, _, route_res = call_api("POST", "/api/v1/telemetry/routing/routes", body={
        "title": "E2E Smoke Route",
        "logs": [f"projects/{project}/logs/run.googleapis.com%2Fstdout"],
        "samplePercent": 10,
        "retentionDays": 7,
        "dailyBudgetGb": 1.0,
    }, timeout=120)
    assert st == 200, f"create route failed: {st} {route_res}"
    route_id = ((route_res.get("data") or {}).get("route") or {}).get("id")
    assert route_id, f"missing route id: {route_res}"
    print(f"  [OK] Created managed BigQuery log route {route_id}")

    st, _, p_res = call_api("POST", "/api/v1/telemetry/routing/routes/action", body={"id": route_id, "action": "pause"})
    assert st == 200, f"pause route failed: {st} {p_res}"
    st, _, r_res = call_api("POST", "/api/v1/telemetry/routing/routes/action", body={"id": route_id, "action": "resume"})
    assert st == 200, f"resume route failed: {st} {r_res}"
    print(f"  [OK] Paused & resumed route {route_id}")

    st, _, d_res = call_api("POST", "/api/v1/telemetry/routing/routes/action", body={"id": route_id, "action": "delete"})
    assert st == 200, f"delete route failed: {st} {d_res}"
    print(f"  [OK] Deleted route {route_id}")

    st, ms_chat, chat_res = call_api("POST", "/api/v1/analyst/chat", body={
        "question": "최근 7일 동안 기록된 Gemini Enterprise 활동 건수는 몇 건인가요?",
        "history": [],
    }, timeout=90)
    assert st == 200, f"AI Analyst chat failed: {st} {chat_res}"
    ans = ((chat_res.get("data") or {}).get("answer") or "").strip()
    assert ans, f"AI Analyst returned empty answer: {chat_res}"
    print(f"  [OK] POST /api/v1/analyst/chat -> {st} ({ms_chat:.0f} ms, answer_chars={len(ans)})")

print("==> [Live E2E] ALL CHECKS PASSED")
PYLIVE
fi
