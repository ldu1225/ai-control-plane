#!/usr/bin/env bash
# AI Control Plane: removes what scripts/deploy.sh created, using the same deploy.env.
#
#   scripts/teardown.sh --project PROJECT_ID     # services, jobs, scheduler jobs, Pub/Sub, service accounts,
#                                                # IAM bindings and secrets
#   scripts/teardown.sh --delete-data            # also the settings bucket (UI settings, exception rules,
#                                                # suspension records, widget layouts), the Log Router sink and the
#                                                # routed telemetry dataset the dashboard created
#   scripts/teardown.sh --delete-roles           # also the custom roles it created, once nothing holds them
#   scripts/teardown.sh --config other.env --yes
#   scripts/teardown.sh --project PROJECT_ID --dry-run
#
# Order: everything that can suspend someone stops first (Cloud Scheduler jobs, the real-time Pub/Sub push, the
# enforcement job and the Cloud Run services). Then users suspended by Antigravity caps get their access role back, so
# nobody stays suspended after the control plane is gone (administrator blocks are kept unless
# --restore-blocked-users is given), and with the iam_deny backend the managed deny policy is deleted on the project,
# the linked projects, the projects added in the dashboard and the folders or organization of the workspace scope,
# which ends every suspension and block it lists.
#
# Never deleted automatically: the BigQuery audit dataset (it holds your logs), the Artifact Registry repository and
# the Data Access audit log configuration. The script prints the commands for them. Custom roles are kept unless
# --delete-roles is given, because a deleted role id cannot be created again for up to 37 days (a reinstall in that
# time could not create its marker roles).
#
# Options
#   --project ID            project of the deployment (default: PROJECT_ID of deploy.PROJECT_ID.env or deploy.env)
#   --config FILE           read the settings from FILE
#   --link-projects IDS     linked projects to clean too (default: SCAN_PROJECTS; none: no linked project; alias
#                           --scan-projects)
#   --region REGION         region of the services and scheduler jobs (default: REGION or asia-northeast3)
#   --enforcement-job-region REGION  region of the iam_deny enforcement job (default: ENFORCEMENT_JOB_REGION or REGION)
#   --delete-data           also delete the settings bucket, the log sink and the routed telemetry dataset
#   --delete-roles          also delete the custom roles nothing holds any more
#   --restore-blocked-users also restore users an administrator blocked
#   --dry-run               print what would be removed and change nothing
#   -y, --yes               do not ask for the project id
#   -h, --help              show this help
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$ROOT/scripts/lib/common.sh"

CONFIG="$ROOT/deploy.env"
CONFIG_EXPLICIT=0
CLI_PROJECT=""
CLI_LINK_PROJECTS=""
CLI_REGION=""
CLI_JOB_REGION=""
DELETE_DATA=0
DELETE_ROLES=0
RESTORE_BLOCKED="false"
DRY_RUN=0
YES=0
# Prints the comment block at the top of this file.
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit "${1:-0}"; }
need_arg() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 needs a value"; }

normalize_cli_args "$@"
set -- ${NORMALIZED_ARGS[@]+"${NORMALIZED_ARGS[@]}"}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) need_arg "$@"; CONFIG="$2"; CONFIG_EXPLICIT=1; shift 2 ;;
    --project) need_arg "$@"; CLI_PROJECT="$2"; shift 2 ;;
    --link-projects|--scan-projects) need_arg "$@"; CLI_LINK_PROJECTS="$2"; shift 2 ;;
    --region) need_arg "$@"; CLI_REGION="$2"; shift 2 ;;
    --enforcement-job-region) need_arg "$@"; CLI_JOB_REGION="$2"; shift 2 ;;
    --delete-data) DELETE_DATA=1; shift ;;
    --delete-roles) DELETE_ROLES=1; shift ;;
    --restore-blocked-users) RESTORE_BLOCKED="true"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -y|--yes) YES=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
done
quiet() { "$@" >/dev/null 2>&1; }

if [[ "$CONFIG_EXPLICIT" == "1" ]]; then
  [[ -f "$CONFIG" ]] || die "configuration file not found: $CONFIG"
  # shellcheck disable=SC1090
  source "$CONFIG"
elif [[ -n "$CLI_PROJECT" ]]; then
  NORM_CLI_PROJECT="$(normalize_project_id "$CLI_PROJECT")"
  if [[ -f "$ROOT/deploy.${NORM_CLI_PROJECT}.env" ]]; then
    # shellcheck disable=SC1090
    source "$ROOT/deploy.${NORM_CLI_PROJECT}.env"
  elif [[ -f "$CONFIG" ]]; then
    CFG_PID="$(bash -c 'source "$1" >/dev/null 2>&1 || true; printf "%s" "${PROJECT_ID:-}"' _ "$CONFIG")"
    CFG_PID="$(normalize_project_id "$CFG_PID")"
    if [[ -z "$CFG_PID" || "$CFG_PID" == "$NORM_CLI_PROJECT" ]]; then
      # shellcheck disable=SC1090
      source "$CONFIG"
    fi
  fi
elif [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
fi
SCAN_PROJECTS="${SCAN_PROJECTS:-${LINKED_PROJECTS:-${WORKSPACE_PROJECTS:-}}}"
[[ -n "$CLI_PROJECT" ]] && PROJECT_ID="$CLI_PROJECT"
[[ -n "$CLI_LINK_PROJECTS" ]] && SCAN_PROJECTS="$CLI_LINK_PROJECTS"
# "none" is an explicit empty list, as in deploy.sh (--link-projects none: clean no linked project).
[[ "$(trim_ws "$SCAN_PROJECTS" | tr '[:upper:]' '[:lower:]')" == "none" ]] && SCAN_PROJECTS=""
[[ -n "$CLI_REGION" ]] && REGION="$CLI_REGION"
[[ -n "$CLI_JOB_REGION" ]] && ENFORCEMENT_JOB_REGION="$CLI_JOB_REGION"

PROJECT_ID="$(normalize_project_id "${PROJECT_ID:-}")"
if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == your-* || "$PROJECT_ID" == "PROJECT_ID" ]]; then
  if [[ "$CONFIG_EXPLICIT" == "0" ]]; then
    ACTIVE_GCP_PROJECT="$(normalize_project_id "$(gcloud config get-value project 2>/dev/null || true)")"
    [[ "$ACTIVE_GCP_PROJECT" == "(unset)" ]] && ACTIVE_GCP_PROJECT=""
    [[ -n "$ACTIVE_GCP_PROJECT" ]] && PROJECT_ID="$ACTIVE_GCP_PROJECT"
  fi
fi
REGION="$(trim_ws "${REGION:-asia-northeast3}")"
SERVICE_NAME="$(trim_ws "${SERVICE_NAME:-ai-control-plane}")"
AUDIT_DATASET="$(trim_ws "${AUDIT_DATASET:-ge_analytics}")"
AR_REPOSITORY="$(trim_ws "${AR_REPOSITORY:-ai-control-plane}")"
BILLING_PROJECT_ID="$(normalize_project_id "${BILLING_PROJECT_ID:-}")"
SERVICE_ACCOUNT_ID="$(trim_ws "${SERVICE_ACCOUNT_ID:-${SERVICE_NAME:0:27}-sa}")"
SETTINGS_BUCKET="$(trim_ws "${SETTINGS_BUCKET:-}")"
SESSION_SECRET_NAME="$(trim_ws "${SESSION_SECRET_NAME:-${SERVICE_NAME}-session-secret}")"
OAUTH_SECRET_NAME="$(trim_ws "${OAUTH_SECRET_NAME:-${SERVICE_NAME}-oauth-client-secret}")"
OAUTH_CONNECT_SECRET_NAME="$(trim_ws "${OAUTH_CONNECT_SECRET_NAME:-${SERVICE_NAME}-oauth-connected-secret}")"
ADMIN_SECRET_NAME="$(trim_ws "${ADMIN_SECRET_NAME:-${SERVICE_NAME}-admin-password}")"
WHERE_LABEL="$( [[ -f "$CONFIG" ]] && basename "$CONFIG" || echo "--project" )"
validate_project_id "$PROJECT_ID" "$WHERE_LABEL"
[[ -n "$SETTINGS_BUCKET" ]] || SETTINGS_BUCKET="$(default_settings_bucket "$PROJECT_ID" "$SERVICE_NAME")"

SA_EMAIL="${SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
BUILD_SA="${SERVICE_NAME:0:21}-build@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="${SERVICE_NAME:0:20}-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
BRIDGE_SA="${SERVICE_NAME:0:23}-signin@${PROJECT_ID}.iam.gserviceaccount.com"
BILLING_DATASET="${BILLING_DATASET:-}"
BRIDGE_NAME="${SERVICE_NAME}-signin"
SINK="${SERVICE_NAME}-ai-logs"
USAGE_EVENTS_TOPIC="${SERVICE_NAME}-agy-usage"
USAGE_EVENTS_SUB_ID="${SERVICE_NAME}-agy-usage-push"
USAGE_EVENTS_SINK="${SERVICE_NAME}-agy-usage"
TELEMETRY_DATASET="${TELEMETRY_DATASET:-telemetry_routed}"
LICENSE_READER_ROLE_ID="CustomAiControlPlaneLicenseReader"
GATEWAY_MANAGER_ROLE_ID="CustomAiControlPlaneGatewayManager"
# iam_deny enforcement job (scripts/deploy.sh): Cloud Run job, scheduler job, service account and managed deny policy.
ENFORCEMENT_JOB_REGION="$(trim_ws "${ENFORCEMENT_JOB_REGION:-$REGION}")"
ENFORCER_JOB="${SERVICE_NAME}-enforcer"
ENFORCER_SA="${SERVICE_NAME:0:21}-enforcer@${PROJECT_ID}.iam.gserviceaccount.com"

# Settings administrators saved in the dashboard decide part of what has to be undone: renamed marker roles and access
# role, the deny policy id, projects added in Settings > Workspace and the workspace scope (folders or organization).
# They are read once from the settings object before anything is deleted, and every value is checked before use.
SAVED_SETTINGS="$(gcloud storage cat "gs://${SETTINGS_BUCKET}/control-plane/settings.json" --project "$PROJECT_ID" 2>/dev/null \
  | python3 -c '
import json, re, sys
raw = sys.stdin.read()
try:
    doc = json.loads(raw[raw.index("{"):])
except ValueError:
    doc = {}
ov = doc.get("overrides") if isinstance(doc, dict) else None
ov = ov if isinstance(ov, dict) else {}
ws = ov.get("workspace") if isinstance(ov.get("workspace"), dict) else {}
gov = ov.get("governance") if isinstance(ov.get("governance"), dict) else {}
ag = gov.get("antigravity") if isinstance(gov.get("antigravity"), dict) else {}
PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
ROLE_ID = re.compile(r"^[A-Za-z0-9_.]{3,64}$")
ROLE = re.compile(r"^(roles/[A-Za-z0-9_.]{1,128}|(projects/[a-z][a-z0-9-]{4,28}[a-z0-9]|organizations/[0-9]{1,30})"
                  r"/roles/[A-Za-z0-9_.]{3,64})$")
NUMBER = re.compile(r"^[0-9]{1,30}$")
def emit(key, value, pattern):
    if isinstance(value, str) and pattern.match(value.strip()):
        print("%s=%s" % (key, value.strip()))
def emit_list(key, values, pattern):
    if isinstance(values, str):
        values = values.split(",")
    if isinstance(values, list):
        good = [str(v).strip() for v in values if pattern.match(str(v).strip())]
        print("%s=%s" % (key, ",".join(good)))
emit("ACCESS_ROLE", ag.get("access_role"), ROLE)
emit("CAP_MARKER", ag.get("cap_marker_role"), ROLE_ID)
emit("ADMIN_MARKER", ag.get("admin_marker_role"), ROLE_ID)
if "legacy_marker_roles" in ag:
    emit_list("LEGACY_MARKERS", ag.get("legacy_marker_roles"), ROLE_ID)
emit("DENY_POLICY_ID", ag.get("deny_policy_id"), re.compile(r"^ai-control-plane-[a-z0-9-]{1,40}$"))
emit("BACKEND", ag.get("enforcement_backend"), re.compile(r"^(iam_allow_swap|iam_deny|notify_only)$"))
emit_list("WORKSPACE_PROJECTS", ws.get("projects"), PROJECT)
emit("SCOPE_TYPE", ws.get("scope_type"), re.compile(r"^(projects|folder|organization)$"))
emit_list("SCOPE_IDS", ws.get("scope_ids"), NUMBER)
' 2>/dev/null || true)"
SAVED_ACCESS_ROLE=""; SAVED_CAP_MARKER=""; SAVED_ADMIN_MARKER=""; SAVED_LEGACY_MARKERS="-"; SAVED_DENY_POLICY_ID=""
SAVED_BACKEND=""; SAVED_WORKSPACE_PROJECTS=""; SAVED_SCOPE_TYPE=""; SAVED_SCOPE_IDS=""
while IFS='=' read -r saved_key saved_value; do
  case "$saved_key" in
    ACCESS_ROLE) SAVED_ACCESS_ROLE="$saved_value" ;;
    CAP_MARKER) SAVED_CAP_MARKER="$saved_value" ;;
    ADMIN_MARKER) SAVED_ADMIN_MARKER="$saved_value" ;;
    LEGACY_MARKERS) SAVED_LEGACY_MARKERS="$saved_value" ;;
    DENY_POLICY_ID) SAVED_DENY_POLICY_ID="$saved_value" ;;
    BACKEND) SAVED_BACKEND="$saved_value" ;;
    WORKSPACE_PROJECTS) SAVED_WORKSPACE_PROJECTS="$saved_value" ;;
    SCOPE_TYPE) SAVED_SCOPE_TYPE="$saved_value" ;;
    SCOPE_IDS) SAVED_SCOPE_IDS="$saved_value" ;;
  esac
done <<<"$SAVED_SETTINGS"

ACCESS_ROLE="${GOVERNANCE_ACCESS_ROLE:-${SAVED_ACCESS_ROLE:-roles/discoveryengine.agentspaceUser}}"
CAP_MARKER_ID="${SAVED_CAP_MARKER:-CustomAntigravityCapBlocked}"
ADMIN_MARKER_ID="${SAVED_ADMIN_MARKER:-CustomAntigravityAdminBlocked}"
[[ "$SAVED_LEGACY_MARKERS" == "-" ]] && SAVED_LEGACY_MARKERS="CustomAntigravityRestrictedUser"
CAP_ROLE_IDS="${CAP_MARKER_ID}${SAVED_LEGACY_MARKERS:+,${SAVED_LEGACY_MARKERS}}"
DENY_POLICY_ID="$(trim_ws "${DENY_POLICY_ID:-${SAVED_DENY_POLICY_ID:-ai-control-plane-caps}}")"
[[ "$DENY_POLICY_ID" =~ ^ai-control-plane-[a-z0-9-]+$ ]] || die "DENY_POLICY_ID must start with ai-control-plane-"

LINKED_TO_CLEAN=()
if [[ -n "$SCAN_PROJECTS" ]]; then
  IFS=',' read -r -a raw_clean <<<"$SCAN_PROJECTS"
  for raw_cp in "${raw_clean[@]}"; do
    cp_id="$(normalize_project_id "$raw_cp")"
    [[ -z "$cp_id" || "$cp_id" == "$PROJECT_ID" ]] && continue
    [[ " ${LINKED_TO_CLEAN[*]-} " == *" ${cp_id} "* ]] && continue
    LINKED_TO_CLEAN+=("$cp_id")
  done
fi
# Where a managed deny policy can be attached (iam_deny): the project, the linked projects, the projects added in the
# dashboard, and with a folder or organization scope its folders or organization.
DENY_POINTS=("projects/${PROJECT_ID}")
for deny_project in ${LINKED_TO_CLEAN[@]+"${LINKED_TO_CLEAN[@]}"}; do DENY_POINTS+=("projects/${deny_project}"); done
IFS=',' read -r -a saved_ws_projects <<<"$SAVED_WORKSPACE_PROJECTS"
for deny_project in ${saved_ws_projects[@]+"${saved_ws_projects[@]}"}; do
  [[ -n "$deny_project" && " ${DENY_POINTS[*]} " != *" projects/${deny_project} "* ]] && DENY_POINTS+=("projects/${deny_project}")
done
SCOPE_KIND="$(trim_ws "${SCOPE_TYPE:-$SAVED_SCOPE_TYPE}")"
SCOPE_LIST="$(trim_ws "${SCOPE_IDS:-$SAVED_SCOPE_IDS}" | tr -d '[:space:]')"
case "$SCOPE_KIND" in folder) SCOPE_PREFIX="folders" ;; organization) SCOPE_PREFIX="organizations" ;; *) SCOPE_PREFIX="" ;; esac
if [[ -n "$SCOPE_PREFIX" && -n "$SCOPE_LIST" ]]; then
  IFS=',' read -r -a scope_ids <<<"$SCOPE_LIST"
  for scope_id in ${scope_ids[@]+"${scope_ids[@]}"}; do
    [[ "$scope_id" =~ ^[0-9]{1,30}$ ]] && DENY_POINTS+=("${SCOPE_PREFIX}/${scope_id}")
  done
fi

echo "This removes ${SERVICE_NAME} from project ${PROJECT_ID} (${REGION})."
[[ "$DELETE_DATA" == "1" ]] && echo "It also deletes gs://${SETTINGS_BUCKET} and the log sink ${SINK}."
if [[ "$DRY_RUN" == "1" ]]; then
  info "[dry-run] scheduler jobs: ${SERVICE_NAME}-enforce, ${SERVICE_NAME}-maintenance, ${SERVICE_NAME}-pricing-sync, ${SERVICE_NAME}-enforcer (${ENFORCEMENT_JOB_REGION})"
  info "[dry-run] real-time usage events: Pub/Sub subscription ${USAGE_EVENTS_SUB_ID}, topic ${USAGE_EVENTS_TOPIC}, log sink ${USAGE_EVENTS_SINK} on ${PROJECT_ID}${SCAN_PROJECTS:+ and linked projects}"
  info "[dry-run] Cloud Run services: ${SERVICE_NAME}, ${BRIDGE_NAME}; Cloud Run job: ${ENFORCER_JOB} (${ENFORCEMENT_JOB_REGION})"
  restore_capped_users "$PROJECT_ID" "$ACCESS_ROLE" "$RESTORE_BLOCKED" "$CAP_ROLE_IDS" "$ADMIN_MARKER_ID"
  info "[dry-run] managed deny policy ${DENY_POLICY_ID} on ${PROJECT_ID}${SCAN_PROJECTS:+ and linked projects} (iam_deny deployments: ends every suspension and block it lists): ${DENY_POINTS[*]}"
  info "[dry-run] telemetry route sinks (description \"AI Control Plane telemetry route\") on ${PROJECT_ID}${SCAN_PROJECTS:+ and linked projects}"
  info "[dry-run] IAM bindings on ${PROJECT_ID}${SCAN_PROJECTS:+ and linked projects (${SCAN_PROJECTS})} for ${SA_EMAIL} (and ${ENFORCER_SA}); dataset access on ${PROJECT_ID}:${AUDIT_DATASET}"
  info "[dry-run] service accounts: ${SA_EMAIL}, ${BUILD_SA}, ${SCHEDULER_SA}, ${BRIDGE_SA}, ${ENFORCER_SA}"
  info "[dry-run] secrets: ${SESSION_SECRET_NAME}, ${OAUTH_SECRET_NAME}, ${OAUTH_CONNECT_SECRET_NAME}, ${ADMIN_SECRET_NAME}"
  if [[ "$DELETE_ROLES" == "1" ]]; then
    info "[dry-run] custom roles nothing holds: ${CAP_ROLE_IDS//,/, }, ${ADMIN_MARKER_ID}, ${GATEWAY_MANAGER_ROLE_ID}, ${LICENSE_READER_ROLE_ID}, ${GE_FEATURE_MANAGER_ROLE_ID} (also on linked projects)"
  fi
  [[ "$DELETE_DATA" == "1" ]] && info "[dry-run] delete gs://${SETTINGS_BUCKET}, log sink ${SINK} and the routed telemetry dataset ${TELEMETRY_DATASET} (only if labelled managed-by=ai-control-plane)"
  exit 0
fi
if [[ "$YES" != "1" ]]; then
  read -r -p "Type the project id to continue: " answer
  [[ "$answer" == "$PROJECT_ID" ]] || die "cancelled"
fi

# 1. Stop everything that can suspend someone before anybody is restored: otherwise a scheduled run, a usage event
# or a running instance could suspend a user again after the restore below, with no control plane left to undo it.
log "Stopping enforcement"
quiet gcloud scheduler jobs delete "$ENFORCER_JOB" --project "$PROJECT_ID" --location "$ENFORCEMENT_JOB_REGION" --quiet \
  && info "deleted scheduler job ${ENFORCER_JOB}" || info "scheduler job ${ENFORCER_JOB} not found"
for job in "${SERVICE_NAME}-enforce" "${SERVICE_NAME}-maintenance" "${SERVICE_NAME}-pricing-sync"; do
  quiet gcloud scheduler jobs delete "$job" --project "$PROJECT_ID" --location "$REGION" --quiet \
    && info "deleted ${job}" || info "${job} not found"
done
quiet gcloud pubsub subscriptions delete "$USAGE_EVENTS_SUB_ID" --project "$PROJECT_ID" --quiet \
  && info "deleted Pub/Sub subscription ${USAGE_EVENTS_SUB_ID}" || info "Pub/Sub subscription ${USAGE_EVENTS_SUB_ID} not found"
for sink_proj in "$PROJECT_ID" ${LINKED_TO_CLEAN[@]+"${LINKED_TO_CLEAN[@]}"}; do
  quiet gcloud logging sinks delete "$USAGE_EVENTS_SINK" --project "$sink_proj" --quiet \
    && info "deleted sink ${USAGE_EVENTS_SINK} on ${sink_proj}" || info "sink ${USAGE_EVENTS_SINK} not found on ${sink_proj}"
done
quiet gcloud pubsub topics delete "$USAGE_EVENTS_TOPIC" --project "$PROJECT_ID" --quiet \
  && info "deleted Pub/Sub topic ${USAGE_EVENTS_TOPIC}" || info "Pub/Sub topic ${USAGE_EVENTS_TOPIC} not found"
quiet gcloud run jobs delete "$ENFORCER_JOB" --project "$PROJECT_ID" --region "$ENFORCEMENT_JOB_REGION" --quiet \
  && info "deleted Cloud Run job ${ENFORCER_JOB}" || info "Cloud Run job ${ENFORCER_JOB} not found"
for svc in "$SERVICE_NAME" "$BRIDGE_NAME"; do
  quiet gcloud run services delete "$svc" --project "$PROJECT_ID" --region "$REGION" --quiet \
    && info "deleted ${svc}" || info "${svc} not found"
done

# 2. Users suspended with the allow-policy method (marker roles) get their access role back.
log "Antigravity suspensions"
restore_capped_users "$PROJECT_ID" "$ACCESS_ROLE" "$RESTORE_BLOCKED" "$CAP_ROLE_IDS" "$ADMIN_MARKER_ID"

# 3. iam_deny (README 8.9): deleting the managed deny policy ends every suspension and block it lists (no cap outlives
# the control plane). Deny policies are touched only for deployments that used deny-policy enforcement.
log "Deny-policy enforcement"
ENFORCER_FOUND=0
if [[ "${ENFORCEMENT_BACKEND:-}" == "iam_deny" || "$SAVED_BACKEND" == "iam_deny" ]] \
    || quiet gcloud iam service-accounts describe "$ENFORCER_SA" --project "$PROJECT_ID"; then
  ENFORCER_FOUND=1
fi
remove_deny_policy() {  # remove_deny_policy RESOURCE (projects/ID, folders/ID or organizations/ID)
  local point="cloudresourcemanager.googleapis.com/$1" err
  if err="$(gcloud iam policies delete "$DENY_POLICY_ID" --attachment-point="$point" --kind=denypolicies --quiet 2>&1 >/dev/null)"; then
    info "deleted deny policy ${DENY_POLICY_ID} on ${1#projects/}"
    return 0
  fi
  case "$err" in
    *NOT_FOUND*|*"not found"*|*"does not exist"*) info "no deny policy ${DENY_POLICY_ID} on ${1#projects/}" ;;
    *) info "could not delete deny policy ${DENY_POLICY_ID} on ${1#projects/} (needs Deny Admin on the organization); run: gcloud iam policies delete ${DENY_POLICY_ID} --attachment-point=${point} --kind=denypolicies" ;;
  esac
  return 0
}
ORG_ID=""
if [[ "$ENFORCER_FOUND" == "1" ]]; then
  ORG_ID="$(project_org_id "$PROJECT_ID" || true)"
  for deny_point in "${DENY_POINTS[@]}"; do
    remove_deny_policy "$deny_point"
  done
  info "a deny policy on other projects the dashboard discovered, or on a scope configured nowhere above, is deleted there by a Deny Admin: gcloud iam policies delete ${DENY_POLICY_ID} --attachment-point=cloudresourcemanager.googleapis.com/folders/FOLDER_ID --kind=denypolicies (organization: .../organizations/${ORG_ID:-ORGANIZATION_ID})"
fi

# Telemetry routes created from the dashboard are Log Router sinks that keep exporting (and costing) once the control
# plane that enforces their budgets is gone, so they are always removed. They are recognized by their description.
log "Telemetry route sinks"
delete_route_sinks() {  # delete_route_sinks PROJECT
  local names name
  names="$(gcloud logging sinks list --project "$1" \
    --filter='description:"AI Control Plane telemetry route"' --format='value(name)' 2>/dev/null || true)"
  for name in $names; do
    quiet gcloud logging sinks delete "$name" --project "$1" --quiet && info "deleted sink ${name} on $1" \
      || info "could not delete sink ${name} on $1; delete it in Logging > Log Router"
  done
  return 0
}
delete_route_sinks "$PROJECT_ID"
for rp in ${LINKED_TO_CLEAN[@]+"${LINKED_TO_CLEAN[@]}"}; do
  delete_route_sinks "$rp"
done

log "Project IAM bindings"
remove_bindings() {  # remove_bindings PROJECT MEMBER
  local roles
  roles="$(gcloud projects get-iam-policy "$1" --flatten='bindings[].members' \
    --filter="bindings.members:$2" --format='value(bindings.role)' 2>/dev/null || true)"
  for role in $roles; do
    if retry quiet gcloud projects remove-iam-policy-binding "$1" --member "$2" --role "$role" --condition=None --quiet; then
      info "removed ${role} from $2 on $1"
    else
      info "could not remove ${role} from $2 on $1; remove it in IAM & Admin > IAM"
    fi
  done
  return 0
}
remove_bindings "$PROJECT_ID" "serviceAccount:${SA_EMAIL}"
remove_bindings "$PROJECT_ID" "serviceAccount:${BUILD_SA}"
[[ "$ENFORCER_FOUND" == "1" ]] && remove_bindings "$PROJECT_ID" "serviceAccount:${ENFORCER_SA}"
quiet gcloud artifacts repositories remove-iam-policy-binding "$AR_REPOSITORY" \
  --location "$REGION" --project "$PROJECT_ID" \
  --member "serviceAccount:${BUILD_SA}" --role roles/artifactregistry.writer --quiet || true

TORN_LINKED=()
for lp in ${LINKED_TO_CLEAN[@]+"${LINKED_TO_CLEAN[@]}"}; do
  # Every role deploy.sh granted there is a binding of this member and is removed here, including the license
  # reader custom role (projects/<linked project>/roles/CustomAiControlPlaneLicenseReader).
  remove_bindings "$lp" "serviceAccount:${SA_EMAIL}"
  [[ "$ENFORCER_FOUND" == "1" ]] && remove_bindings "$lp" "serviceAccount:${ENFORCER_SA}"
  TORN_LINKED+=("$lp")
done

# remove_dataset_access PROJECT DATASET EMAIL...: drops the dataset ACL entries deploy.sh added for these accounts
# (BigQuery Data Viewer on the billing export, Data Editor on the audit dataset). Other entries, such as the log sink's
# writer identity, stay.
remove_dataset_access() {
  local project="$1" dataset="$2" file
  shift 2
  file="$(mktemp)"
  if bq --project_id "$project" show --format=prettyjson "${project}:${dataset}" >"$file" 2>/dev/null \
      && python3 - "$file" "$@" <<'PYCODE'
import json, sys
path, emails = sys.argv[1], {e.lower() for e in sys.argv[2:]}
raw = open(path, encoding="utf-8", errors="replace").read()
dataset = json.loads(raw[raw.index("{"):])
access = dataset.get("access", [])
kept = [a for a in access if (a.get("userByEmail") or "").lower() not in emails]
if len(kept) == len(access):
    sys.exit(3)
dataset["access"] = kept
json.dump(dataset, open(path, "w", encoding="utf-8"))
PYCODE
  then
    quiet bq --project_id "$project" update --source "$file" "${project}:${dataset}" \
      && info "removed $* from ${project}:${dataset}" \
      || info "could not update ${project}:${dataset}; remove $* from its sharing settings"
  fi
  rm -f "$file"
  return 0
}
if [[ -n "$BILLING_PROJECT_ID" && "$BILLING_PROJECT_ID" != "$PROJECT_ID" ]]; then
  remove_bindings "$BILLING_PROJECT_ID" "serviceAccount:${SA_EMAIL}"   # deployments made before dataset-level grants
  if [[ -n "$BILLING_DATASET" ]]; then
    ACL_EMAILS=("$SA_EMAIL")
    [[ "$ENFORCER_FOUND" == "1" ]] && ACL_EMAILS+=("$ENFORCER_SA")
    remove_dataset_access "$BILLING_PROJECT_ID" "$BILLING_DATASET" "${ACL_EMAILS[@]}"
  fi
fi
# The audit dataset itself is kept (it holds your logs); only the dashboard's own access entry goes.
remove_dataset_access "$PROJECT_ID" "$AUDIT_DATASET" "$SA_EMAIL"

if [[ "$ENFORCER_FOUND" == "1" ]]; then
  # Organization roles of deny-policy enforcement, removed while the accounts still exist (bindings of a deleted
  # account remain as deleted:serviceAccount:... entries, which grant nothing).
  log "Organization roles"
  [[ -n "$ORG_ID" ]] || ORG_ID="$(project_org_id "$PROJECT_ID" || true)"
  for pair in "serviceAccount:${ENFORCER_SA}|roles/iam.denyAdmin" "serviceAccount:${SA_EMAIL}|roles/iam.denyReviewer"; do
    member="${pair%%|*}"
    role="${pair#*|}"
    if [[ -n "$ORG_ID" ]] && quiet gcloud organizations remove-iam-policy-binding "$ORG_ID" --member="$member" \
        --role="$role" --condition=None --quiet; then
      info "removed ${role} from ${member} on organization ${ORG_ID}"
    else
      info "${role} of ${member} not removed (absent, or an organization administrator removes it): gcloud organizations remove-iam-policy-binding ${ORG_ID:-ORGANIZATION_ID} --member=${member} --role=${role} --condition=None"
    fi
  done
fi

log "Service accounts"
for sa in "$SA_EMAIL" "$BUILD_SA" "$SCHEDULER_SA" "$BRIDGE_SA" "$ENFORCER_SA"; do
  quiet gcloud iam service-accounts delete "$sa" --project "$PROJECT_ID" --quiet && info "deleted ${sa}" || info "${sa} not found"
done

log "Secrets"
for secret in "$SESSION_SECRET_NAME" "$OAUTH_SECRET_NAME" "$OAUTH_CONNECT_SECRET_NAME" "$ADMIN_SECRET_NAME"; do
  quiet gcloud secrets delete "$secret" --project "$PROJECT_ID" --quiet && info "deleted ${secret}" || info "${secret} not found"
done

# Custom roles (--delete-roles): deleted only when no binding of the project's IAM policy uses them any more. The
# marker roles stay while another AI Control Plane deployment in the project still holds the license reader role.
role_holders() {  # role_holders PROJECT: prints "ROLE_NAME COUNT" for every custom role of PROJECT that has members
  gcloud projects get-iam-policy "$1" --format=json 2>/dev/null | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    policy = json.loads(raw[raw.index("{"):])
except ValueError:
    sys.exit(3)
counts = {}
for binding in policy.get("bindings") or []:
    role = binding.get("role") or ""
    if "/roles/" in role:
        counts[role] = counts.get(role, 0) + len(binding.get("members") or [])
for role, count in counts.items():
    print("%s %d" % (role, count))
'
}
delete_unused_role() {  # delete_unused_role PROJECT ROLE_ID HOLDERS WHY_KEPT
  local name="projects/$1/roles/$2" count
  count="$(printf '%s\n' "$3" | awk -v r="$name" '$1 == r { print $2 }')"
  if [[ -n "$4" ]]; then
    info "kept ${name} ($4)"
  elif [[ -n "$count" && "$count" != "0" ]]; then
    info "kept ${name} (${count} member(s) still hold it)"
  elif quiet gcloud iam roles delete "$2" --project "$1" --quiet; then
    info "deleted custom role ${name} (gcloud iam roles undelete $2 --project $1 restores it within 7 days)"
  else
    info "custom role ${name} not found or not deleted"
  fi
  return 0
}
if [[ "$DELETE_ROLES" == "1" ]]; then
  log "Custom roles"
  if HOLDERS="$(role_holders "$PROJECT_ID")"; then
    OTHER_DEPLOYMENT=""
    printf '%s\n' "$HOLDERS" | awk -v r="projects/${PROJECT_ID}/roles/${LICENSE_READER_ROLE_ID}" '$1 == r && $2 > 0 { f = 1 } END { exit !f }' \
      && OTHER_DEPLOYMENT="another AI Control Plane deployment in ${PROJECT_ID} still uses the marker roles"
    IFS=',' read -r -a marker_ids <<<"${CAP_ROLE_IDS},${ADMIN_MARKER_ID}"
    for marker_id in "${marker_ids[@]}"; do
      [[ -n "$marker_id" ]] && delete_unused_role "$PROJECT_ID" "$marker_id" "$HOLDERS" "$OTHER_DEPLOYMENT"
    done
    delete_unused_role "$PROJECT_ID" "$GATEWAY_MANAGER_ROLE_ID" "$HOLDERS" ""
    delete_unused_role "$PROJECT_ID" "$GE_FEATURE_MANAGER_ROLE_ID" "$HOLDERS" ""
    delete_unused_role "$PROJECT_ID" "$LICENSE_READER_ROLE_ID" "$HOLDERS" ""
  else
    info "cannot read the IAM policy of ${PROJECT_ID}; custom roles kept"
  fi
  for lr_project in ${TORN_LINKED[@]+"${TORN_LINKED[@]}"}; do
    if LINKED_HOLDERS="$(role_holders "$lr_project")"; then
      delete_unused_role "$lr_project" "$LICENSE_READER_ROLE_ID" "$LINKED_HOLDERS" ""
      delete_unused_role "$lr_project" "$GE_FEATURE_MANAGER_ROLE_ID" "$LINKED_HOLDERS" ""
    else
      info "cannot read the IAM policy of ${lr_project}; ${LICENSE_READER_ROLE_ID} kept there"
    fi
  done
fi

if [[ "$DELETE_DATA" == "1" ]]; then
  log "Settings bucket and log sink"
  quiet gcloud storage rm --recursive --all-versions "gs://${SETTINGS_BUCKET}" --project "$PROJECT_ID" \
    && info "deleted gs://${SETTINGS_BUCKET}" || info "gs://${SETTINGS_BUCKET} not found"
  # The sink's writer identity is usually the project's shared Logging service agent: its roles are
  # never touched here (other sinks depend on them).
  if gcloud logging sinks describe "$SINK" --project "$PROJECT_ID" >/dev/null 2>&1; then
    quiet gcloud logging sinks delete "$SINK" --project "$PROJECT_ID" --quiet && info "deleted sink ${SINK}"
  else
    info "sink ${SINK} not found"
  fi
  # The routed telemetry dataset is deleted only when this control plane created it (label managed-by).
  if bq --project_id "$PROJECT_ID" show --format=json "${PROJECT_ID}:${TELEMETRY_DATASET}" 2>/dev/null \
      | python3 -c 'import json,sys; raw=sys.stdin.read(); data=json.loads(raw[raw.index("{"):]); sys.exit(0 if (data.get("labels") or {}).get("managed-by") == "ai-control-plane" else 3)' 2>/dev/null; then
    quiet bq --project_id "$PROJECT_ID" rm -r -f -d "${PROJECT_ID}:${TELEMETRY_DATASET}" \
      && info "deleted dataset ${PROJECT_ID}:${TELEMETRY_DATASET}" || info "could not delete ${PROJECT_ID}:${TELEMETRY_DATASET}"
  else
    info "dataset ${TELEMETRY_DATASET} not found or not created by the control plane; kept"
  fi
else
  info "kept gs://${SETTINGS_BUCKET}, the log sink ${SINK} and the routed telemetry dataset ${TELEMETRY_DATASET} (use --delete-data to remove them)"
fi

log "Kept on purpose"
info "BigQuery dataset (your audit logs):   bq rm -r -d ${PROJECT_ID}:${AUDIT_DATASET}"
info "Container images:                     gcloud artifacts repositories delete ${AR_REPOSITORY} --location ${REGION} --project ${PROJECT_ID}"
info "Data Access audit logs: IAM & Admin > Audit Logs in the Google Cloud console"
info "Agent Gateway resources created from the dashboard are yours and stay: Network Services > Agent Gateways"
if [[ "$DELETE_ROLES" != "1" ]]; then
  info "Custom roles (run again with --delete-roles to delete the ones nothing holds; a deleted role id cannot be created again for up to 37 days):"
  info "Marker roles (delete only when no user holds them; users an administrator blocked keep their other access through them):"
  info "  gcloud iam roles delete ${CAP_MARKER_ID} --project ${PROJECT_ID}"
  info "  gcloud iam roles delete ${ADMIN_MARKER_ID} --project ${PROJECT_ID}"
  info "License reader and gateway manager custom roles (bindings are removed above; another dashboard in the same project may still use them):"
  info "  gcloud iam roles delete ${GATEWAY_MANAGER_ROLE_ID} --project ${PROJECT_ID}"
  for lr_project in "$PROJECT_ID" ${TORN_LINKED[@]+"${TORN_LINKED[@]}"}; do
    info "  gcloud iam roles delete ${LICENSE_READER_ROLE_ID} --project ${lr_project}"
    info "  gcloud iam roles delete ${GE_FEATURE_MANAGER_ROLE_ID} --project ${lr_project}"
  done
fi
