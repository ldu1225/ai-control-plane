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

# Trims leading and trailing whitespace from $1.
trim_ws() {
  local s="${1:-}"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# Strips common copy-paste prefixes (such as "gcloud config set project ", "--project ", "project ",
# "projects/") and surrounding quotes/whitespace from a project ID, warning when a prefix was removed.
normalize_project_id() {
  local raw="${1:-}" cleaned prev
  cleaned="$(trim_ws "$raw")"
  while :; do
    prev="$cleaned"
    cleaned="${cleaned#\"}"; cleaned="${cleaned%\"}"
    cleaned="${cleaned#\'}"; cleaned="${cleaned%\'}"
    cleaned="$(trim_ws "$cleaned")"
    if [[ "$cleaned" =~ ^[Gg][Cc][Ll][Oo][Uu][Dd][[:space:]]+[Cc][Oo][Nn][Ff][Ii][Gg][[:space:]]+[Ss][Ee][Tt][[:space:]]+[Pp][Rr][Oo][Jj][Ee][Cc][Tt][[:space:]]+(.+)$ ]]; then
      cleaned="${BASH_REMATCH[1]}"
    elif [[ "$cleaned" =~ ^--[Pp][Rr][Oo][Jj][Ee][Cc][Tt]([[:space:]]+|=)(.+)$ ]]; then
      cleaned="${BASH_REMATCH[2]}"
    elif [[ "$cleaned" =~ ^[Pp][Rr][Oo][Jj][Ee][Cc][Tt](_[Ii][Dd])?([[:space:]]+|=)(.+)$ ]]; then
      cleaned="${BASH_REMATCH[3]}"
    elif [[ "$cleaned" =~ ^[Pp][Rr][Oo][Jj][Ee][Cc][Tt][Ss]/(.+)$ ]]; then
      cleaned="${BASH_REMATCH[1]}"
    fi
    cleaned="$(trim_ws "$cleaned")"
    [[ "$cleaned" == "$prev" ]] && break
  done
  if [[ -n "$raw" && "$cleaned" != "$(trim_ws "$raw")" ]]; then
    warn "normalized PROJECT_ID '${raw}' -> '${cleaned}'"
  fi
  printf '%s' "$cleaned"
}

# Validates a Google Cloud project ID (6-30 lowercase letters, digits or hyphens, starting with a letter).
validate_project_id() {
  local pid="${1:-}" where="${2:-configuration}" lower
  [[ -n "$pid" && "$pid" != your-* && "$pid" != "PROJECT_ID" ]] || die "set PROJECT_ID in ${where}"
  if [[ "$pid" =~ [A-Z] ]]; then
    lower="$(printf '%s' "$pid" | tr '[:upper:]' '[:lower:]')"
    die "PROJECT_ID '${pid}' contains uppercase letters (looks like a project Name). Set the lowercase project ID instead (for example '${lower}')"
  fi
  [[ "$pid" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] \
    || die "invalid PROJECT_ID '${pid}' in ${where}: enter only the lowercase Google Cloud project ID (for example 'my-project-123'), without 'project ' or spaces"
}

# Builds a <=63-character default Cloud Storage bucket name from PROJECT_ID and SERVICE_NAME.
default_settings_bucket() {
  local pid="$1" svc="$2" candidate max_svc short_svc
  candidate="${pid}-${svc}-settings"
  if [[ ${#candidate} -le 63 ]]; then
    printf '%s' "$candidate"
    return 0
  fi
  max_svc=$((63 - ${#pid} - 10))
  short_svc="${svc:0:$max_svc}"
  short_svc="${short_svc%-}"
  printf '%s' "${pid}-${short_svc}-settings"
}

# Normalizes an IAM member entry for BRIDGE_MEMBERS / IAP_MEMBERS so bare emails ("alice@example.com"),
# wildcard domains ("*@example.com") or bare domains ("example.com") work without failing gcloud iap.
normalize_iam_member() {
  local m
  m="$(trim_ws "${1:-}")"
  [[ -n "$m" ]] || return 0
  case "$m" in
    allUsers|allAuthenticatedUsers|user:*|group:*|domain:*|serviceAccount:*|principal:*|principalSet:*)
      printf '%s' "$m" ;;
    \*@*)
      printf 'domain:%s' "${m#\*@}" ;;
    *@*)
      printf 'user:%s' "$m" ;;
    *.*)
      printf 'domain:%s' "$m" ;;
    *)
      printf '%s' "$m" ;;
  esac
}

# Formats an active gcloud account email as a user: or serviceAccount: IAM member.
caller_iam_member() {
  local acct
  acct="$(trim_ws "${1:-}")"
  if [[ "$acct" == *.gserviceaccount.com ]]; then
    printf 'serviceAccount:%s' "$acct"
  else
    printf 'user:%s' "$acct"
  fi
}

# _check_project_permissions MODE PROJECT NEED_BUILD NEED_SINK NEED_IAP NEED_SCHEDULER NEED_AUDIT_ADMIN
# Calls Cloud Resource Manager projects.testIamPermissions and prints a single-line JSON verdict:
#   status="ok"         every required permission is already present
#   status="bootstrap"  some operational permissions are missing, but caller has setIamPolicy so script can self-grant
#   status="denied"     caller lacks required permissions and lacks setIamPolicy to grant them
#   status="skip"       token unavailable or transient network failure (caller continues normally)
_check_project_permissions() {
  local token
  token="$(gcloud auth print-access-token 2>/dev/null || true)"
  if [[ -z "$token" ]]; then
    printf '{"status":"skip","reason":"no_token"}\n'
    return 0
  fi
  ACP_ACCESS_TOKEN="$token" python3 - "$@" <<'PYPREFLIGHT'
import json, os, sys, urllib.error, urllib.request

mode, project = sys.argv[1], sys.argv[2]
need_build = sys.argv[3] == "1"
need_sink = sys.argv[4] == "1"
need_iap = sys.argv[5] == "1"
need_scheduler = sys.argv[6] == "1"
need_audit_admin = sys.argv[7] == "1"
token = os.environ["ACP_ACCESS_TOKEN"]

# Ordered pairs of (permission, minimal predefined role that grants it).
checks = [("serviceusage.services.enable", "roles/serviceusage.serviceUsageAdmin")]
if mode == "prepare":
    if need_sink:
        checks += [
            ("bigquery.datasets.create", "roles/bigquery.admin"),
            ("logging.sinks.create", "roles/logging.configWriter"),
        ]
    if need_audit_admin:
        checks += [
            ("resourcemanager.projects.getIamPolicy", "roles/resourcemanager.projectIamAdmin"),
            ("resourcemanager.projects.setIamPolicy", "roles/resourcemanager.projectIamAdmin"),
        ]
else:
    checks += [
        ("resourcemanager.projects.getIamPolicy", "roles/resourcemanager.projectIamAdmin"),
        ("resourcemanager.projects.setIamPolicy", "roles/resourcemanager.projectIamAdmin"),
        ("serviceusage.services.use", "roles/serviceusage.serviceUsageConsumer"),
        ("iam.serviceAccounts.create", "roles/iam.serviceAccountAdmin"),
        ("iam.serviceAccounts.setIamPolicy", "roles/iam.serviceAccountAdmin"),
        ("iam.serviceAccounts.actAs", "roles/iam.serviceAccountUser"),
        ("storage.buckets.create", "roles/storage.admin"),
        ("storage.buckets.setIamPolicy", "roles/storage.admin"),
        ("secretmanager.secrets.create", "roles/secretmanager.admin"),
        ("secretmanager.secrets.setIamPolicy", "roles/secretmanager.admin"),
        ("run.services.create", "roles/run.admin"),
        ("run.services.setIamPolicy", "roles/run.admin"),
    ]
    if need_build:
        checks += [
            ("cloudbuild.builds.create", "roles/cloudbuild.builds.editor"),
            ("artifactregistry.repositories.create", "roles/artifactregistry.admin"),
            ("artifactregistry.repositories.uploadArtifacts", "roles/artifactregistry.admin"),
        ]
    if need_sink:
        checks += [
            ("bigquery.datasets.create", "roles/bigquery.admin"),
            ("logging.sinks.create", "roles/logging.configWriter"),
        ]
    if need_iap:
        checks += [("iap.web.setIamPolicy", "roles/iap.admin")]
    if need_scheduler:
        checks += [("cloudscheduler.jobs.create", "roles/cloudscheduler.admin")]

all_perms = []
for perm, _ in checks:
    if perm not in all_perms:
        all_perms.append(perm)
if "resourcemanager.projects.setIamPolicy" not in all_perms:
    probe_perms = all_perms + ["resourcemanager.projects.setIamPolicy"]
else:
    probe_perms = list(all_perms)

url = "https://cloudresourcemanager.googleapis.com/v1/projects/%s:testIamPermissions" % project


def test_batch(perms):
    req = urllib.request.Request(
        url,
        data=json.dumps({"permissions": perms}).encode("utf-8"),
        method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return set(json.load(resp).get("permissions") or [])


granted = set()
valid_perms = set(probe_perms)
try:
    granted = test_batch(probe_perms)
except urllib.error.HTTPError as err:
    if err.code == 400:
        # One or more permission strings are not registered on Project in this environment; test individually.
        valid_perms = set()
        for p in probe_perms:
            try:
                got = test_batch([p])
                valid_perms.add(p)
                granted |= got
            except urllib.error.HTTPError as sub:
                if sub.code != 400:
                    print(json.dumps({"status": "skip", "reason": "http_%s" % sub.code}))
                    sys.exit(0)
    else:
        print(json.dumps({"status": "skip", "reason": "http_%s" % err.code}))
        sys.exit(0)
except Exception as exc:  # pylint: disable=broad-except
    print(json.dumps({"status": "skip", "reason": str(exc)[:80]}))
    sys.exit(0)

missing_perms = []
missing_roles = []
for perm, role in checks:
    if perm in valid_perms and perm not in granted:
        missing_perms.append(perm)
        if role not in missing_roles:
            missing_roles.append(role)

if not missing_perms:
    print(json.dumps({"status": "ok", "missing_perms": [], "missing_roles": []}))
elif "resourcemanager.projects.setIamPolicy" in granted:
    bootstrap_roles = [r for r in missing_roles if r != "roles/resourcemanager.projectIamAdmin"]
    print(json.dumps({"status": "bootstrap", "missing_perms": missing_perms, "missing_roles": bootstrap_roles}))
else:
    print(json.dumps({"status": "denied", "missing_perms": missing_perms, "missing_roles": missing_roles}))
PYPREFLIGHT
}

# preflight_caller_permissions MODE PROJECT ACCOUNT [NEED_BUILD] [NEED_SINK] [NEED_IAP] [NEED_SCHEDULER] [NEED_AUDIT_ADMIN]
# Verifies caller IAM permissions upfront. When the caller has Project IAM Admin (setIamPolicy) but lacks
# operational roles (such as Cloud Build Editor, Cloud Run Admin, Secret Manager Admin or IAP Admin),
# automatically grants the missing roles to the caller and waits for IAM propagation. When the caller lacks
# setIamPolicy as well (for example Editor/Viewer only), fails fast with exact copy-paste remediation commands.
preflight_caller_permissions() {
  local mode="$1" project="$2" account="$3"
  local need_build="${4:-1}" need_sink="${5:-1}" need_iap="${6:-0}" need_sched="${7:-0}" need_audit="${8:-0}"
  local verdict status member roles_csv perms_csv role attempt
  member="$(caller_iam_member "$account")"
  verdict="$(_check_project_permissions "$mode" "$project" "$need_build" "$need_sink" "$need_iap" "$need_sched" "$need_audit")"
  status="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("status","skip"))' "$verdict" 2>/dev/null || echo "skip")"
  if [[ "$status" == "ok" || "$status" == "skip" ]]; then
    return 0
  fi
  perms_csv="$(python3 -c 'import json,sys; print(", ".join(json.loads(sys.argv[1]).get("missing_perms",[])))' "$verdict")"
  roles_csv="$(python3 -c 'import json,sys; print(" ".join(json.loads(sys.argv[1]).get("missing_roles",[])))' "$verdict")"
  if [[ "$status" == "bootstrap" ]]; then
    info "account ${account} has IAM admin access; granting missing deployment roles: ${roles_csv}"
    for role in $roles_csv; do
      run retry gcloud projects add-iam-policy-binding "$project" --member "$member" --role "$role" \
        --condition=None --quiet >/dev/null \
        || warn "could not grant ${role} to ${member}; continuing"
    done
    if [[ "${DRY_RUN:-0}" != "1" ]]; then
      for attempt in 1 2 3 4 5 6; do
        sleep 5
        verdict="$(_check_project_permissions "$mode" "$project" "$need_build" "$need_sink" "$need_iap" "$need_sched" "$need_audit")"
        status="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("status","skip"))' "$verdict" 2>/dev/null || echo "skip")"
        [[ "$status" == "ok" ]] && break
      done
    fi
    return 0
  fi
  die "account ${account} lacks required permissions on project ${project}:
    missing permissions: ${perms_csv}

  Fix option 1 — log in as a Project Owner and re-run:
    gcloud auth login

  Fix option 2 — ask a Project Owner to grant Owner (or the least-privilege roles below) to ${account}:
    gcloud projects add-iam-policy-binding ${project} --member=\"${member}\" --role=\"roles/owner\"

  (Least-privilege alternative instead of roles/owner: ${roles_csv})"
}

# ensure_service_identities PROJECT PROJECT_NUMBER SERVICE [SERVICE ...]
# Triggers creation of Google-managed service agents (such as gcp-sa-cloudbuild and gcp-sa-iap) via the
# Service Usage REST API so freshly enabled APIs never fail with "Service account service-... does not exist"
# and never prompt interactively to install `gcloud beta`.
ensure_service_identities() {
  local project="$1" project_number="$2" token
  shift 2
  [[ "${DRY_RUN:-0}" == "1" || $# -eq 0 ]] && return 0
  token="$(gcloud auth print-access-token 2>/dev/null || true)"
  [[ -n "$token" ]] || return 0
  ACP_ACCESS_TOKEN="$token" python3 - "$project" "$project_number" "$@" <<'PYSID' || true
import json, os, sys, urllib.error, urllib.request

project, project_number, services = sys.argv[1], sys.argv[2], sys.argv[3:]
token = os.environ["ACP_ACCESS_TOKEN"]
for svc in services:
    url = "https://serviceusage.googleapis.com/v1beta1/projects/%s/services/%s:generateServiceIdentity" % (
        project_number, svc
    )
    req = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "X-Goog-User-Project": project,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except Exception:  # pylint: disable=broad-except
        pass
PYSID
}

# ensure_secret_created SECRET_NAME PROJECT REGION
# Creates an empty Secret Manager secret if missing. Prefers regional user-managed replication in REGION so
# organizations enforcing constraints/gcp.resourceLocations (for example asia-northeast3 only) succeed, and
# falls back to automatic replication if needed.
ensure_secret_created() {
  local secret="$1" project="$2" region="$3"
  if gcloud secrets describe "$secret" --project "$project" >/dev/null 2>&1; then
    return 0
  fi
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    info "[dry-run] create secret ${secret} in ${region}"
    return 0
  fi
  gcloud secrets create "$secret" --project "$project" \
    --replication-policy=user-managed --locations="$region" \
    --labels=app=ai-control-plane >/dev/null 2>&1 \
    || gcloud secrets create "$secret" --project "$project" \
      --replication-policy=automatic \
      --labels=app=ai-control-plane >/dev/null
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
