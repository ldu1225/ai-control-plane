# shellcheck shell=bash
# Shared helpers for scripts/deploy.sh and scripts/prepare.sh. Source this file; do not execute it.
#
# Callers set DRY_RUN (0 or 1) before calling run() or any ensure_* function. With DRY_RUN=1 commands
# that change anything are printed instead of executed.

log()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# Runs a command, or prints it with DRY_RUN=1.
run() {
  if [[ "${DRY_RUN:-0}" == "1" ]]; then printf '    [dry-run] %s\n' "$*" >&2; return 0; fi
  "$@"
}

# IAM bindings for a just-created service account can fail until it has propagated.
retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    [[ $n -ge 6 ]] && return 1
    sleep 10
  done
}

# Dataset-level BigQuery access (a dataset ACL entry) for one service account; project IAM is not touched.
# grant_dataset_role PROJECT DATASET ROLE EMAIL, where ROLE is READER (Data Viewer) or WRITER (Data Editor).
# Returns non-zero when the dataset cannot be read or updated.
grant_dataset_role() {
  local file rc=0
  file="$(mktemp)"
  if ! bq --project_id "$1" show --format=prettyjson "${1}:${2}" >"$file"; then
    rm -f "$file"
    return 1
  fi
  python3 - "$file" "$3" "$4" <<'PYCODE' || rc=$?
import json, sys
path, role, email = sys.argv[1], sys.argv[2], sys.argv[3].lower()
enough = {"READER": ("READER", "WRITER", "OWNER"), "WRITER": ("WRITER", "OWNER")}[role]
dataset = json.load(open(path))
access = dataset.setdefault("access", [])
if any((a.get("userByEmail") or "").lower() == email and a.get("role") in enough for a in access):
    sys.exit(3)  # already granted
access.append({"role": role, "userByEmail": email})
json.dump(dataset, open(path, "w"))
PYCODE
  if [[ $rc -eq 0 ]]; then
    bq --project_id "$1" update --source "$file" "${1}:${2}" >/dev/null || rc=1
  elif [[ $rc -eq 3 ]]; then
    rc=0
  fi
  rm -f "$file"
  return $rc
}

# Log Router filter for the AI logs the dashboard reads (README 6.1 shows the same text).
# Cloud Logging binds OR more tightly than AND, so the audit-log clause sits in its own parentheses.
# Without them the Gemini Enterprise, Model Armor and Antigravity logs would also have to be audit logs,
# and none of them would be routed.
ai_log_filter() {
  cat <<'FILTER'
(
  (log_id("cloudaudit.googleapis.com/activity") OR log_id("cloudaudit.googleapis.com/data_access"))
  AND protoPayload.serviceName=("aiplatform.googleapis.com" OR "discoveryengine.googleapis.com" OR "modelarmor.googleapis.com" OR "businessaicode.googleapis.com" OR "notebooklm.googleapis.com" OR "geminidataanalytics.googleapis.com" OR "generativelanguage.googleapis.com")
)
OR log_id("discoveryengine.googleapis.com/gemini_enterprise_user_activity")
OR log_id("discoveryengine.googleapis.com/gen_ai_user_message")
OR log_id("modelarmor.googleapis.com/sanitize_operations")
OR log_id("businessaicode.googleapis.com/inference_response")
FILTER
}

# ensure_ai_log_sink PROJECT DATASET LOCATION SINK
# Creates the dataset (in LOCATION, only when missing) and the Log Router sink with partitioned tables, or
# updates an existing sink to the current filter and destination. The sink's writer identity gets BigQuery
# Data Editor on this dataset only; the writer is often the project's shared Logging service agent, so
# nothing is granted at project level.
ensure_ai_log_sink() {
  local project="$1" dataset="$2" location="$3" sink="$4" dest filter writer
  if bq --project_id "$project" show --dataset "${project}:${dataset}" >/dev/null 2>&1; then
    info "dataset ${project}:${dataset} exists"
  else
    run bq --project_id "$project" --location "$location" mk --dataset \
      --description "AI audit logs routed for AI Control Plane" "${project}:${dataset}"
  fi
  dest="bigquery.googleapis.com/projects/${project}/datasets/${dataset}"
  filter="$(ai_log_filter)"
  if gcloud logging sinks describe "$sink" --project "$project" >/dev/null 2>&1; then
    run gcloud logging sinks update "$sink" "$dest" --log-filter "$filter" --project "$project" --quiet
  else
    run gcloud logging sinks create "$sink" "$dest" --use-partitioned-tables --log-filter "$filter" \
      --project "$project" --quiet
  fi
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    info "[dry-run] grant the sink writer BigQuery Data Editor on ${project}:${dataset}"
    return 0
  fi
  writer="$(gcloud logging sinks describe "$sink" --project "$project" --format='value(writerIdentity)')"
  retry grant_dataset_role "$project" "$dataset" WRITER "${writer#serviceAccount:}" \
    || die "could not grant the sink writer BigQuery Data Editor on ${project}:${dataset}"
  info "sink ${sink} -> ${project}:${dataset} (writer ${writer}: BigQuery Data Editor on this dataset only)"
}

# enable_data_access_logs PROJECT
# Data Read and Data Write audit logs for Agent Platform, Discovery Engine and Model Armor. Only the
# auditConfigs section changes; the policy keeps its etag, so a concurrent IAM change makes this fail
# instead of overwriting it.
enable_data_access_logs() {
  local project="$1" file changed
  file="$(mktemp)"
  if ! gcloud projects get-iam-policy "$project" --format=json >"$file"; then
    rm -f "$file"
    die "cannot read the IAM policy of ${project} (Data Access audit logs need Owner or Project IAM Admin)"
  fi
  changed="$(python3 - "$file" <<'PY'
import json, sys
path = sys.argv[1]
policy = json.load(open(path))
configs = policy.setdefault("auditConfigs", [])
changed = False
for service in ("aiplatform.googleapis.com", "discoveryengine.googleapis.com", "modelarmor.googleapis.com"):
    entry = next((c for c in configs if c.get("service") == service), None)
    if entry is None:
        entry = {"service": service, "auditLogConfigs": []}
        configs.append(entry)
    present = {c.get("logType") for c in entry.setdefault("auditLogConfigs", [])}
    for log_type in ("DATA_READ", "DATA_WRITE"):
        if log_type not in present:
            entry["auditLogConfigs"].append({"logType": log_type})
            changed = True
json.dump(policy, open(path, "w"))
print("yes" if changed else "no")
PY
)"
  if [[ "$changed" == "yes" ]]; then
    if ! run gcloud projects set-iam-policy "$project" "$file" --quiet >/dev/null; then
      rm -f "$file"
      die "could not update the audit log configuration of ${project}"
    fi
    if [[ "${DRY_RUN:-0}" == "1" ]]; then
      info "[dry-run] Data Access audit logs would be enabled for Agent Platform, Discovery Engine and Model Armor"
    else
      info "Data Access audit logs enabled for Agent Platform, Discovery Engine and Model Armor"
    fi
  else
    info "Data Access audit logs already enabled for Agent Platform, Discovery Engine and Model Armor"
  fi
  rm -f "$file"
}

# Locations where Gemini Enterprise apps and license configurations can live.
GE_LOCATIONS_ALL="global,us,eu"

# _ge_apps MODE PROJECT LOCATIONS LOG_PROMPTS
# Reads the Gemini Enterprise apps (engines with appType APP_TYPE_INTRANET) in the comma-separated
# LOCATIONS through the Discovery Engine API. Never fails the caller.
#   enable  turns on observability (usage logs) for every app and reports each one
#   detect  prints only the locations that have an app or a license configuration, e.g. "global,us"
_ge_apps() {
  local token
  token="$(gcloud auth print-access-token 2>/dev/null)" || token=""
  if [[ -z "$token" ]]; then
    [[ "$1" == "detect" ]] \
      || warn "no access token: turn on observability in the console (Gemini Enterprise > app > Configurations > Observability)"
    return 0
  fi
  ACP_ACCESS_TOKEN="$token" python3 - "$@" "${DRY_RUN:-0}" <<'PY' || true
import json, os, sys, urllib.error, urllib.request

mode, project, locations = sys.argv[1], sys.argv[2], sys.argv[3]
prompts, dry = sys.argv[4] == "true", sys.argv[5] == "1"
token = os.environ["ACP_ACCESS_TOKEN"]
MANUAL = "Gemini Enterprise > app > Configurations > Observability > 'Enable instrumentation of OpenTelemetry traces and logs'"


def host(location):
    return ("https://discoveryengine.googleapis.com" if location == "global"
            else "https://%s-discoveryengine.googleapis.com" % location)


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "X-Goog-User-Project": project, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def reason(err):
    try:
        detail = json.load(err).get("error", {})
        return "%s %s" % (detail.get("status") or err.code, (detail.get("message") or "")[:160])
    except Exception:  # pylint: disable=broad-except
        return "HTTP %s" % err.code


def warn(text):
    if mode == "enable":
        print("    warning: " + text, file=sys.stderr)


apps, found = 0, []
for location in [l.strip() for l in locations.split(",") if l.strip()]:
    base = "%s/v1alpha/projects/%s/locations/%s" % (host(location), project, location)
    engines, page = [], ""
    try:
        while True:
            data = call("GET", base + "/collections/default_collection/engines?pageSize=100"
                        + ("&pageToken=" + page if page else ""))
            engines += data.get("engines", [])
            page = data.get("nextPageToken") or ""
            if not page:
                break
    except urllib.error.HTTPError as err:
        warn("cannot list Gemini Enterprise apps in %s (%s)" % (location, reason(err)))
        continue
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        warn("cannot reach Discovery Engine in %s (%s)" % (location, err))
        continue
    engines = [e for e in engines if e.get("appType") == "APP_TYPE_INTRANET"]  # not search or chat apps
    if mode == "detect":
        if engines:
            found.append(location)
            continue
        try:  # seats can be distributed to a location before its first app exists
            if call("GET", base + "/licenseConfigs").get("licenseConfigs"):
                found.append(location)
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        continue
    for engine in engines:
        apps += 1
        label = "%s (%s)" % (engine.get("displayName") or engine["name"].rsplit("/", 1)[-1], location)
        current = engine.get("observabilityConfig") or {}
        want = {"observabilityEnabled": True,
                "sensitiveLoggingEnabled": bool(current.get("sensitiveLoggingEnabled")) or prompts}
        if current.get("observabilityEnabled") and bool(current.get("sensitiveLoggingEnabled")) == want["sensitiveLoggingEnabled"]:
            print("    Gemini Enterprise app %s: observability already on%s"
                  % (label, ", prompts logged" if want["sensitiveLoggingEnabled"] else ""))
            continue
        if dry:
            print("    [dry-run] Gemini Enterprise app %s: turn on observability%s"
                  % (label, " and prompt logging" if want["sensitiveLoggingEnabled"] else ""), file=sys.stderr)
            continue
        try:
            call("PATCH", "%s/v1alpha/%s?updateMask=observabilityConfig" % (host(location), engine["name"]),
                 {"observabilityConfig": want})
            print("    Gemini Enterprise app %s: observability turned on%s"
                  % (label, ", prompts logged" if want["sensitiveLoggingEnabled"] else ""))
        except urllib.error.HTTPError as err:
            warn("Gemini Enterprise app %s: could not turn on observability (%s). Needs the Gemini Enterprise "
                 "Admin role, or do it in the console: %s" % (label, reason(err), MANUAL))
if mode == "detect":
    print(",".join(found))
elif apps == 0:
    print("    no Gemini Enterprise app found in %s" % locations)
PY
}

# ensure_ge_observability PROJECT LOCATIONS [LOG_PROMPTS]
# Turns on observability (usage logs) and prompt/user logging (sensitiveLoggingEnabled, default true;
# without it Gemini Enterprise elides userIamPrincipal and text as "<elided>") for every Gemini Enterprise
# app in the comma-separated LOCATIONS. Never fails the caller: problems are reported as warnings.
ensure_ge_observability() { _ge_apps enable "$1" "$2" "${3:-true}"; }

# ge_locations PROJECT
# Prints the locations (of GE_LOCATIONS_ALL) that have a Gemini Enterprise app or license configuration,
# comma separated; prints nothing when there are none or they cannot be read.
ge_locations() { _ge_apps detect "$1" "$GE_LOCATIONS_ALL" false 2>/dev/null; }
