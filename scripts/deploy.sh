#!/usr/bin/env bash
# AI Control Plane: zero-friction one-command setup and deployment to Google Cloud Run.
#
# Quickest usage (no config file needed — auto-detects active gcloud account, billing, org & AI endpoints):
#   scripts/deploy.sh --project PROJECT_ID [--admins a@example.org] [--viewers b@example.org]
#   scripts/deploy.sh --projects proj-a,proj-b --admins a@example.org --viewers b@example.org
#   scripts/deploy.sh                         # uses deploy.env if present, otherwise active gcloud project
#
# Config-file usage (100% compatible):
#   cp deploy.env.example deploy.env
#   scripts/deploy.sh [--config deploy.env] [--dry-run]
#
# Options
#   --project ID            Google Cloud project ID to deploy into
#   --projects ID1,ID2      Deploy sequentially into multiple Google Cloud projects in one run
#   --region REGION         Cloud Run, Artifact Registry & bucket region (default: asia-northeast3)
#   --admins EMAILS         Comma-separated admin emails or *@domain (default: active gcloud account)
#   --viewers EMAILS        Comma-separated viewer emails or *@domain
#   --org-name NAME         Organization display name (default: auto-detected from GCP Org / Project)
#   --access-mode MODE      google | iap | password | google_oauth (default: google)
#   --service-name NAME     Cloud Run service name (default: ai-control-plane)
#   --billing-account ID    Cloud Billing account ID to link if the project is unbilled
#   --read-only             Skip IAM governance write roles (ENABLE_GOVERNANCE_ACTIONS=false)
#   --no-data-access-logs   Skip enabling Data Access audit logs
#   --config FILE           Read settings from FILE (CLI flags override FILE values)
#   --dry-run               Print commands that would run without changing anything
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" 2>/dev/null || cd /tmp
# shellcheck source=lib/common.sh
source "$ROOT/scripts/lib/common.sh"

CONFIG="$ROOT/deploy.env"
CONFIG_EXPLICIT=0
DRY_RUN=0
CLI_PROJECT=""
CLI_PROJECTS=""
CLI_REGION=""
CLI_ADMINS=""
CLI_VIEWERS=""
CLI_ORG_NAME=""
CLI_ACCESS_MODE=""
CLI_SERVICE_NAME=""
CLI_BILLING_ACCOUNT=""
CLI_READ_ONLY=""
CLI_DATA_ACCESS_LOGS=""

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
need_arg() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 needs a value"; }

normalize_cli_args "$@"
set -- ${NORMALIZED_ARGS[@]+"${NORMALIZED_ARGS[@]}"}
ORIG_ARGS=("$@")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) need_arg "$@"; CONFIG="$2"; CONFIG_EXPLICIT=1; shift 2 ;;
    --project) need_arg "$@"; CLI_PROJECT="$2"; shift 2 ;;
    --projects) need_arg "$@"; CLI_PROJECTS="$2"; shift 2 ;;
    --region) need_arg "$@"; CLI_REGION="$2"; shift 2 ;;
    --admins|--admin) need_arg "$@"; CLI_ADMINS="$2"; shift 2 ;;
    --viewers|--viewer|--allowed-users) need_arg "$@"; CLI_VIEWERS="$2"; shift 2 ;;
    --org-name) need_arg "$@"; CLI_ORG_NAME="$2"; shift 2 ;;
    --access-mode) need_arg "$@"; CLI_ACCESS_MODE="$2"; shift 2 ;;
    --service-name) need_arg "$@"; CLI_SERVICE_NAME="$2"; shift 2 ;;
    --billing-account) need_arg "$@"; CLI_BILLING_ACCOUNT="$2"; shift 2 ;;
    --read-only) CLI_READ_ONLY="1"; shift ;;
    --data-access-logs) CLI_DATA_ACCESS_LOGS="true"; shift ;;
    --no-data-access-logs) CLI_DATA_ACCESS_LOGS="false"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -y|--yes) shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

# Multi-project orchestration: invoke this script once per project and print a combined summary.
if [[ -n "$CLI_PROJECTS" ]]; then
  IFS=',' read -r -a MULTI_LIST <<<"$CLI_PROJECTS"
  SUMMARY_LINES=()
  FORWARD_ARGS=()
  idx=0
  while [[ $idx -lt ${#ORIG_ARGS[@]} ]]; do
    arg="${ORIG_ARGS[$idx]}"
    if [[ "$arg" == "--projects" || "$arg" == "--project" ]]; then
      idx=$((idx + 2))
      continue
    fi
    FORWARD_ARGS+=("$arg")
    idx=$((idx + 1))
  done
  for raw_pid in "${MULTI_LIST[@]}"; do
    pid="$(normalize_project_id "$raw_pid")"
    [[ -n "$pid" ]] || continue
    log "========================================================================"
    log "Multi-project deployment target: ${pid}"
    log "========================================================================"
    "$0" --project "$pid" ${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}
    SUMMARY_LINES+=("${pid}")
  done
  log "All ${#SUMMARY_LINES[@]} project deployment(s) completed: ${SUMMARY_LINES[*]}"
  exit 0
fi

if [[ "$CONFIG_EXPLICIT" == "1" ]]; then
  [[ -f "$CONFIG" ]] || die "configuration file not found: $CONFIG"
  # shellcheck disable=SC1090
  source "$CONFIG"
elif [[ -n "$CLI_PROJECT" ]]; then
  PER_PROJ_CFG="$ROOT/deploy.$(normalize_project_id "$CLI_PROJECT").env"
  if [[ -f "$PER_PROJ_CFG" ]]; then
    # shellcheck disable=SC1090
    source "$PER_PROJ_CFG"
  fi
elif [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
fi

# Accept intuitive variable aliases so customers never hit naming mismatches.
ADMIN_EMAILS="${ADMIN_EMAILS:-${INITIAL_ADMINS:-${ADMINS:-${ADMIN_EMAIL:-}}}}"
ALLOWED_USERS="${ALLOWED_USERS:-${INITIAL_VIEWERS:-${VIEWERS:-${VIEWER_EMAILS:-}}}}"

# Apply CLI overrides on top of config file / environment values.
[[ -n "$CLI_PROJECT" ]] && PROJECT_ID="$CLI_PROJECT"
[[ -n "$CLI_REGION" ]] && REGION="$CLI_REGION"
[[ -n "$CLI_ADMINS" ]] && ADMIN_EMAILS="$CLI_ADMINS"
[[ -n "$CLI_VIEWERS" ]] && ALLOWED_USERS="$CLI_VIEWERS"
[[ -n "$CLI_ORG_NAME" ]] && ORGANIZATION_NAME="$CLI_ORG_NAME"
[[ -n "$CLI_ACCESS_MODE" ]] && ACCESS_MODE="$CLI_ACCESS_MODE"
[[ -n "$CLI_SERVICE_NAME" ]] && SERVICE_NAME="$CLI_SERVICE_NAME"
[[ -n "$CLI_BILLING_ACCOUNT" ]] && BILLING_ACCOUNT_ID="$CLI_BILLING_ACCOUNT"
[[ "$CLI_READ_ONLY" == "1" ]] && ENABLE_GOVERNANCE_ACTIONS="false"
[[ -n "$CLI_DATA_ACCESS_LOGS" ]] && ENABLE_DATA_ACCESS_LOGS="$CLI_DATA_ACCESS_LOGS"

for tool in gcloud bq python3; do command -v "$tool" >/dev/null || die "$tool is required"; done
ACCOUNT="$(ensure_gcloud_auth)"
CALLER_MEMBER="$(caller_iam_member "$ACCOUNT")"

# ------------------------------------------------------------------------------------ settings
PROJECT_ID="$(normalize_project_id "${PROJECT_ID:-}")"
if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == your-* || "$PROJECT_ID" == "PROJECT_ID" ]]; then
  ACTIVE_GCP_PROJECT="$(normalize_project_id "$(gcloud config get-value project 2>/dev/null || true)")"
  [[ "$ACTIVE_GCP_PROJECT" == "(unset)" ]] && ACTIVE_GCP_PROJECT=""
  if [[ -n "$ACTIVE_GCP_PROJECT" && "$CONFIG_EXPLICIT" == "0" ]]; then
    PROJECT_ID="$ACTIVE_GCP_PROJECT"
    info "using active gcloud project: ${PROJECT_ID}"
  fi
fi

REGION="$(trim_ws "${REGION:-asia-northeast3}")"
SERVICE_NAME="$(trim_ws "${SERVICE_NAME:-ai-control-plane}")"
ACCESS_MODE="$(trim_ws "${ACCESS_MODE:-google}")"
IAP_MEMBERS="${IAP_MEMBERS:-}"
BRIDGE_MEMBERS="${BRIDGE_MEMBERS:-allAuthenticatedUsers}"
GUEST_ACCESS="${GUEST_ACCESS:-false}"
GUEST_REDACT_TERMS="${GUEST_REDACT_TERMS:-}"
OAUTH_CLIENT_JSON="${OAUTH_CLIENT_JSON:-}"
GOOGLE_OAUTH_CLIENT_ID="${GOOGLE_OAUTH_CLIENT_ID:-}"
GOOGLE_OAUTH_REDIRECT_URI="${GOOGLE_OAUTH_REDIRECT_URI:-}"
GOOGLE_OAUTH_START_URL="${GOOGLE_OAUTH_START_URL:-}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"
ORGANIZATION_NAME="${ORGANIZATION_NAME:-Your Organization}"
PRODUCT_NAME="${PRODUCT_NAME:-}"
DEFAULT_LOCALE="${DEFAULT_LOCALE:-ko}"
WORKSPACE_TIMEZONE="${WORKSPACE_TIMEZONE:-Asia/Seoul}"
AUDIT_DATASET="$(trim_ws "${AUDIT_DATASET:-ge_analytics}")"
CREATE_LOG_SINK="${CREATE_LOG_SINK:-true}"
DATASET_LOCATION="$(trim_ws "${DATASET_LOCATION:-$REGION}")"
# Full coverage by default: enable Data Access audit logs, Gemini Enterprise observability + prompt logging,
# and governance IAM controls out of the box so all 13 modules and health checks work immediately.
ENABLE_DATA_ACCESS_LOGS="${ENABLE_DATA_ACCESS_LOGS:-true}"
ENABLE_GE_OBSERVABILITY="${ENABLE_GE_OBSERVABILITY:-true}"
GE_PROMPT_LOGGING="${GE_PROMPT_LOGGING:-true}"
BILLING_PROJECT_ID="$(normalize_project_id "${BILLING_PROJECT_ID:-}")"
BILLING_DATASET="$(trim_ws "${BILLING_DATASET:-}")"
BILLING_TABLE="$(trim_ws "${BILLING_TABLE:-}")"
BILLING_ACCOUNT_ID="$(trim_ws "${BILLING_ACCOUNT_ID:-}")"
DISCOVERY_ENGINE_LOCATIONS="${DISCOVERY_ENGINE_LOCATIONS:-}"
AGENT_ENGINE_REGIONS="${AGENT_ENGINE_REGIONS:-}"
ENABLE_GOVERNANCE_ACTIONS="${ENABLE_GOVERNANCE_ACTIONS:-true}"
ENFORCEMENT_SCHEDULE="${ENFORCEMENT_SCHEDULE:-}"
AI_DEFAULT_MODEL="${AI_DEFAULT_MODEL:-}"
AI_LOCATION="${AI_LOCATION:-}"
IMAGE="$(trim_ws "${IMAGE:-}")"
AR_REPOSITORY="$(trim_ws "${AR_REPOSITORY:-ai-control-plane}")"
SERVICE_ACCOUNT_ID="$(trim_ws "${SERVICE_ACCOUNT_ID:-${SERVICE_NAME:0:27}-sa}")"
SETTINGS_BUCKET="$(trim_ws "${SETTINGS_BUCKET:-}")"
SESSION_SECRET_NAME="$(trim_ws "${SESSION_SECRET_NAME:-${SERVICE_NAME}-session-secret}")"
OAUTH_SECRET_NAME="$(trim_ws "${OAUTH_SECRET_NAME:-${SERVICE_NAME}-oauth-client-secret}")"
MEMORY="${MEMORY:-1Gi}"
CPU="${CPU:-1}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-3}"

# ---------------------------------------------------------------------------------- validation
WHERE_LABEL="$( [[ -f "$CONFIG" ]] && basename "$CONFIG" || echo "--project" )"
validate_project_id "$PROJECT_ID" "$WHERE_LABEL"
[[ -z "$BILLING_PROJECT_ID" ]] || validate_project_id "$BILLING_PROJECT_ID" "${WHERE_LABEL} (BILLING_PROJECT_ID)"
[[ "$REGION" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || die "invalid REGION '${REGION}' (for example 'asia-northeast3')"

# Auto-default ADMIN_EMAILS to the active deploying gcloud account when omitted or left as template placeholder.
[[ "$ADMIN_EMAILS" != *example.com* ]] || ADMIN_EMAILS=""
if [[ -z "$ADMIN_EMAILS" && "$ACCESS_MODE" != "password" ]]; then
  ADMIN_EMAILS="$ACCOUNT"
  info "ADMIN_EMAILS defaulted to active gcloud account: ${ADMIN_EMAILS}"
fi
if [[ "$ACCESS_MODE" == "iap" && -z "$IAP_MEMBERS" ]]; then
  IAP_MEMBERS="user:${ACCOUNT}"
  info "IAP_MEMBERS defaulted to user:${ACCOUNT}"
fi

[[ "$SERVICE_NAME" =~ ^[a-z]([a-z0-9-]{0,40}[a-z0-9])?$ ]] || die "SERVICE_NAME must be lowercase letters, digits and hyphens"
[[ -n "$SETTINGS_BUCKET" ]] || SETTINGS_BUCKET="$(default_settings_bucket "$PROJECT_ID" "$SERVICE_NAME")"
[[ ${#SETTINGS_BUCKET} -le 63 ]] || die "SETTINGS_BUCKET '${SETTINGS_BUCKET}' is longer than 63 characters; set a shorter SETTINGS_BUCKET"
case "$ACCESS_MODE" in
  google|password) ;;
  google_oauth) [[ -f "$OAUTH_CLIENT_JSON" ]] || die "ACCESS_MODE=google_oauth needs OAUTH_CLIENT_JSON (downloaded OAuth client JSON)" ;;
  iap) [[ -n "$IAP_MEMBERS" ]] || die "ACCESS_MODE=iap needs IAP_MEMBERS (for example group:ai-governance@example.com)" ;;
  *) die "ACCESS_MODE must be google, google_oauth, iap or password" ;;
esac
case "$GUEST_ACCESS" in
  true)
    [[ "$ACCESS_MODE" != "iap" ]] || die "GUEST_ACCESS=true needs ACCESS_MODE google, google_oauth or password"
    if [[ -z "$GUEST_REDACT_TERMS" && -f "$ROOT/.prepublish-terms" ]]; then
      GUEST_REDACT_TERMS="$(python3 - "$ROOT/.prepublish-terms" <<'PY'
import sys
terms = [line.split("#", 1)[0].strip() for line in open(sys.argv[1], encoding="utf-8")]
print(",".join(t for t in terms if t and not t.startswith("!") and "," not in t))
PY
)"
    fi ;;
  false) ;;
  *) die "GUEST_ACCESS must be true or false" ;;
esac
if [[ -n "$ENFORCEMENT_SCHEDULE" ]]; then
  [[ "$ENABLE_GOVERNANCE_ACTIONS" == "true" ]] || die "ENFORCEMENT_SCHEDULE needs ENABLE_GOVERNANCE_ACTIONS=true"
  [[ "$ACCESS_MODE" != "iap" ]] || die "ENFORCEMENT_SCHEDULE needs ACCESS_MODE google, google_oauth or password (IAP blocks the scheduler's OIDC token)"
fi
for flag in CREATE_LOG_SINK ENABLE_DATA_ACCESS_LOGS ENABLE_GE_OBSERVABILITY GE_PROMPT_LOGGING ENABLE_GOVERNANCE_ACTIONS; do
  [[ "${!flag}" == "true" || "${!flag}" == "false" ]] || die "$flag must be true or false"
done
DISCOVERY_ENGINE_LOCATIONS="$(printf '%s' "$DISCOVERY_ENGINE_LOCATIONS" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
[[ -z "$DISCOVERY_ENGINE_LOCATIONS" || "$DISCOVERY_ENGINE_LOCATIONS" =~ ^[a-z0-9-]+(,[a-z0-9-]+)*$ ]] \
  || die "DISCOVERY_ENGINE_LOCATIONS must be a comma-separated list such as global,us (or empty to detect)"

# Keep this list in sync with MODULE_IDS in src/controlplane/config.py (checked by the unit tests).
MODULE_IDS=(overview gemini_enterprise notebooklm model_usage agent_gateway agent_registry agent_deployments
  model_armor users antigravity finops telemetry analyst)
DISABLED_MODULES="$(printf '%s' "${DISABLED_MODULES:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
IFS=',' read -r -a disabled_list <<<"$DISABLED_MODULES"
for module in ${disabled_list[@]+"${disabled_list[@]}"}; do
  [[ -z "$module" || " ${MODULE_IDS[*]} " == *" $module "* ]] \
    || die "unknown module '$module' in DISABLED_MODULES (modules: ${MODULE_IDS[*]})"
done

PROJECT_NUMBER=""
GCLOUD_ERR="$(mktemp)"
IAM_CACHE="$(mktemp)"
trap 'rm -f "$GCLOUD_ERR" "$IAM_CACHE"' EXIT

# Bootstrap foundational control-plane APIs first so brand-new empty projects can answer project/IAM APIs.
bootstrap_foundation_apis "$PROJECT_ID"
export CLOUDSDK_CORE_PROJECT="$PROJECT_ID"

for attempt in 1 2 3; do
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' 2>"$GCLOUD_ERR" || true)"
  PROJECT_NUMBER="$(printf '%s' "$PROJECT_NUMBER" | tr -d '[:space:]')"
  [[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] && break
  [[ $attempt -lt 3 ]] || die "cannot read project ${PROJECT_ID} with ${ACCOUNT}: $(tail -n 3 "$GCLOUD_ERR")"
  sleep 4
done

SA_EMAIL="${SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
BUILD_SA_ID="${SERVICE_NAME:0:21}-build"
BUILD_SA="${BUILD_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
CB_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
RUN_SERVICE_AGENT="service-${PROJECT_NUMBER}@serverless-robot-prod.iam.gserviceaccount.com"
BRIDGE_NAME="${SERVICE_NAME}-signin"
RUN_URL="https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"
BASE_URL="${PUBLIC_BASE_URL:-$RUN_URL}"
BASE_URL="${BASE_URL%/}"
BRIDGE_URL="https://${BRIDGE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"
IAP_AGENT="service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"
SCHEDULER_SA_ID="${SERVICE_NAME:0:20}-scheduler"
SCHEDULER_SA="${SCHEDULER_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
BRIDGE_SA_ID="${SERVICE_NAME:0:23}-signin"
BRIDGE_SA="${BRIDGE_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
ENFORCE_URL="${BASE_URL}/api/v1/governance/antigravity/enforce"

NEED_BUILD=0; [[ -z "$IMAGE" ]] && NEED_BUILD=1
NEED_SINK=0; [[ "$CREATE_LOG_SINK" == "true" ]] && NEED_SINK=1
NEED_IAP=0; [[ "$ACCESS_MODE" == "google" || "$ACCESS_MODE" == "iap" ]] && NEED_IAP=1
NEED_SCHED=0; [[ -n "$ENFORCEMENT_SCHEDULE" ]] && NEED_SCHED=1
NEED_AUDIT=0; [[ "$ENABLE_DATA_ACCESS_LOGS" == "true" ]] && NEED_AUDIT=1

log "Deploying ${SERVICE_NAME} to ${PROJECT_ID} (${REGION}) as ${ACCOUNT}"
info "access mode: ${ACCESS_MODE}    dashboard URL: ${BASE_URL}"
[[ "$DRY_RUN" == "1" ]] && info "dry run: commands that change anything are printed, not executed"

# Verify caller IAM permissions upfront (and auto-grant missing deployment roles when caller has IAM Admin).
preflight_caller_permissions deploy "$PROJECT_ID" "$ACCOUNT" "$NEED_BUILD" "$NEED_SINK" "$NEED_IAP" "$NEED_SCHED" "$NEED_AUDIT"

# Verify Cloud Billing is linked (auto-links when accessible) and detect account ID.
DETECTED_BA="$(ensure_project_billing "$PROJECT_ID" "$BILLING_ACCOUNT_ID")"
if [[ -z "$BILLING_ACCOUNT_ID" && -n "$DETECTED_BA" ]]; then
  BILLING_ACCOUNT_ID="$DETECTED_BA"
  info "detected Cloud Billing account: ${BILLING_ACCOUNT_ID}"
fi

# ------------------------------------------------------------------------------- 1. APIs & Service Agents
log "1/8 Enabling all required AI, governance and platform APIs"
enable_project_apis "$PROJECT_ID" "$NEED_IAP" "$NEED_SCHED" 0
SID_APIS=(
  cloudbuild.googleapis.com
  artifactregistry.googleapis.com
  run.googleapis.com
  secretmanager.googleapis.com
  iap.googleapis.com
  aiplatform.googleapis.com
  discoveryengine.googleapis.com
  modelarmor.googleapis.com
  cloudscheduler.googleapis.com
  dlp.googleapis.com
  logging.googleapis.com
  bigquery.googleapis.com
)
ensure_service_identities "$PROJECT_ID" "$PROJECT_NUMBER" "${SID_APIS[@]}"

# ------------------------------------------------------------------------------- 2. settings bucket
log "2/8 Settings bucket gs://${SETTINGS_BUCKET}"
if ! gcloud storage buckets describe "gs://${SETTINGS_BUCKET}" --project "$PROJECT_ID" >/dev/null 2>&1; then
  run gcloud storage buckets create "gs://${SETTINGS_BUCKET}" --project "$PROJECT_ID" --location "$REGION" \
    --uniform-bucket-level-access --public-access-prevention
  run gcloud storage buckets update "gs://${SETTINGS_BUCKET}" --versioning
fi

# ------------------------------------------------------------------------------- 3. identity & IAM
log "3/8 Service accounts and IAM (${SA_EMAIL})"
ensure_sa() {  # ensure_sa ACCOUNT_ID EMAIL DISPLAY_NAME DESCRIPTION
  if ! gcloud iam service-accounts describe "$2" --project "$PROJECT_ID" >/dev/null 2>&1; then
    run gcloud iam service-accounts create "$1" --project "$PROJECT_ID" \
      --display-name "$3" --description "$4"
  fi
}
ensure_sa "$SERVICE_ACCOUNT_ID" "$SA_EMAIL" "AI Control Plane runtime" \
  "Least-privilege identity of the ${SERVICE_NAME} Cloud Run service"
if [[ "$ACCESS_MODE" == "google" ]]; then
  ensure_sa "$BRIDGE_SA_ID" "$BRIDGE_SA" "AI Control Plane sign-in bridge" \
    "Identity of the ${BRIDGE_NAME} sign-in bridge"
fi
if [[ -z "$IMAGE" ]]; then
  ensure_sa "$BUILD_SA_ID" "$BUILD_SA" "AI Control Plane image builder" \
    "Least-privilege Cloud Build identity for ${SERVICE_NAME}"
fi

gcloud projects get-iam-policy "$PROJECT_ID" --format=json >"$IAM_CACHE" 2>/dev/null || echo "{}" >"$IAM_CACHE"
has_project_role() {  # has_project_role MEMBER ROLE
  python3 - "$IAM_CACHE" "$1" "$2" <<'PYIAM'
import json, sys
policy = json.load(open(sys.argv[1]))
want_member, want_role = sys.argv[2].lower(), sys.argv[3]
for b in policy.get("bindings") or []:
    if b.get("role") == want_role and not b.get("condition"):
        if any(m.lower() == want_member for m in (b.get("members") or [])):
            sys.exit(0)
sys.exit(1)
PYIAM
}
grant_member() {  # grant_member PROJECT MEMBER ROLE
  if [[ "$1" == "$PROJECT_ID" ]] && has_project_role "$2" "$3"; then
    info "already granted $3 to $2"
    return 0
  fi
  run retry gcloud projects add-iam-policy-binding "$1" --member "$2" --role "$3" \
    --condition=None --quiet >/dev/null
  [[ "$DRY_RUN" == "1" ]] || info "granted $3 to $2 on $1"
}
grant_optional_role() {  # grant_optional_role PROJECT MEMBER ROLE
  if [[ "$1" == "$PROJECT_ID" ]] && has_project_role "$2" "$3"; then
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] optional role $3 -> $2"
    return 0
  fi
  gcloud projects add-iam-policy-binding "$1" --member "$2" --role "$3" \
    --condition=None --quiet >/dev/null 2>&1 \
    && info "granted $3 to $2 on $1" || true
}

# Complete runtime permissions for all 13 modules (including privateLogViewer for Data Access logs and
# logging.configWriter + bigquery.user for one-click Telemetry sink creation in the UI).
RUNTIME_ROLES=(
  roles/bigquery.dataViewer
  roles/bigquery.jobUser
  roles/bigquery.user
  roles/aiplatform.user
  roles/discoveryengine.viewer
  roles/monitoring.viewer
  roles/logging.viewer
  roles/logging.privateLogViewer
  roles/logging.logWriter
  roles/logging.configWriter
  roles/iam.securityReviewer
  roles/serviceusage.serviceUsageConsumer
)
OPTIONAL_RUNTIME_ROLES=(
  roles/modelarmor.viewer
  roles/cloudtrace.user
)
GOVERNANCE_ROLES=(roles/resourcemanager.projectIamAdmin roles/iam.roleAdmin)
for role in "${RUNTIME_ROLES[@]}"; do grant_member "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "$role"; done
for role in "${OPTIONAL_RUNTIME_ROLES[@]}"; do grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "$role"; done
if [[ "$ENABLE_GOVERNANCE_ACTIONS" == "true" ]]; then
  for role in "${GOVERNANCE_ROLES[@]}"; do grant_member "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "$role"; done
else
  info "governance write roles skipped (ENABLE_GOVERNANCE_ACTIONS=false): dashboards and simulations only"
fi
# Ensure Cloud Run service agent binding exists on brand-new projects.
grant_optional_role "$PROJECT_ID" "serviceAccount:${RUN_SERVICE_AGENT}" "roles/run.serviceAgent"
run retry gcloud storage buckets add-iam-policy-binding "gs://${SETTINGS_BUCKET}" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/storage.objectAdmin >/dev/null

# Auto-discover BigQuery Billing Export table when BILLING_TABLE is not configured.
BILLING_SCAN_PROJECT="${BILLING_PROJECT_ID:-$PROJECT_ID}"
if [[ -z "$BILLING_TABLE" ]]; then
  DETECTED_BILL="$(auto_detect_billing_export "$BILLING_SCAN_PROJECT")"
  if [[ -n "$DETECTED_BILL" ]]; then
    BILLING_DATASET="${DETECTED_BILL%%|*}"
    BILLING_TABLE="${DETECTED_BILL#*|}"
    [[ -n "$BILLING_PROJECT_ID" ]] || BILLING_PROJECT_ID="$BILLING_SCAN_PROJECT"
    info "auto-detected BigQuery billing export: ${BILLING_SCAN_PROJECT}.${BILLING_DATASET}.${BILLING_TABLE}"
  elif [[ ",$DISABLED_MODULES," != *",finops,"* ]]; then
    info "no BigQuery billing export table found yet in ${BILLING_SCAN_PROJECT} (connect later in Settings > Workspace if needed)"
  fi
fi

# Billing export in another project: read access on that dataset only (no project-level role there).
if [[ -n "$BILLING_PROJECT_ID" && "$BILLING_PROJECT_ID" != "$PROJECT_ID" ]]; then
  if [[ -z "$BILLING_DATASET" ]]; then
    warn "BILLING_PROJECT_ID is set without BILLING_DATASET: billing read access was not granted"
  elif [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] grant BigQuery Data Viewer on ${BILLING_PROJECT_ID}:${BILLING_DATASET} (this dataset only)"
  elif retry grant_dataset_role "$BILLING_PROJECT_ID" "$BILLING_DATASET" READER "$SA_EMAIL"; then
    info "granted BigQuery Data Viewer on ${BILLING_PROJECT_ID}:${BILLING_DATASET} (this dataset only)"
    gcloud projects remove-iam-policy-binding "$BILLING_PROJECT_ID" --member "serviceAccount:${SA_EMAIL}" \
      --role roles/bigquery.dataViewer --condition=None --quiet >/dev/null 2>&1 || true
  else
    warn "could not grant read access on ${BILLING_PROJECT_ID}:${BILLING_DATASET}; grant BigQuery Data Viewer on that dataset to ${SA_EMAIL}"
  fi
fi

# ------------------------------------------------------------------------------- 4. image
log "4/8 Container image"
if [[ -z "$IMAGE" ]]; then
  if ! gcloud artifacts repositories describe "$AR_REPOSITORY" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
    run gcloud artifacts repositories create "$AR_REPOSITORY" --repository-format=docker --location "$REGION" \
      --project "$PROJECT_ID" --description "AI Control Plane images"
  fi
  # Guarantee Cloud Run service agent can pull images immediately on freshly created projects.
  run gcloud artifacts repositories add-iam-policy-binding "$AR_REPOSITORY" \
    --location "$REGION" --project "$PROJECT_ID" \
    --member "serviceAccount:${RUN_SERVICE_AGENT}" --role roles/artifactregistry.reader --quiet >/dev/null 2>&1 || true

  IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/${SERVICE_NAME}:$(date -u +%Y%m%d-%H%M%S)"
  info "building ${IMAGE}"

  grant_optional_role "$PROJECT_ID" "serviceAccount:${CB_SERVICE_AGENT}" "roles/cloudbuild.serviceAgent"
  grant_member "$PROJECT_ID" "serviceAccount:${BUILD_SA}" "roles/logging.logWriter"
  run retry gcloud artifacts repositories add-iam-policy-binding "$AR_REPOSITORY" \
    --location "$REGION" --project "$PROJECT_ID" \
    --member "serviceAccount:${BUILD_SA}" --role roles/artifactregistry.writer --quiet >/dev/null 2>&1 \
    || grant_member "$PROJECT_ID" "serviceAccount:${BUILD_SA}" "roles/artifactregistry.writer"
  run retry gcloud iam service-accounts add-iam-policy-binding "$BUILD_SA" --project "$PROJECT_ID" \
    --member "$CALLER_MEMBER" --role roles/iam.serviceAccountUser --condition=None --quiet >/dev/null
  run gcloud iam service-accounts add-iam-policy-binding "$BUILD_SA" --project "$PROJECT_ID" \
    --member "serviceAccount:${CB_SERVICE_AGENT}" --role roles/iam.serviceAccountTokenCreator \
    --condition=None --quiet >/dev/null 2>&1 || true
  run retry gcloud storage buckets add-iam-policy-binding "gs://${SETTINGS_BUCKET}" \
    --member "serviceAccount:${BUILD_SA}" --role roles/storage.objectAdmin >/dev/null

  if [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] gcloud builds submit ${ROOT} --region ${REGION} --service-account ${BUILD_SA} -> ${IMAGE}"
  else
    CB_DIR="$(mktemp -d)"
    CB_CONFIG="${CB_DIR}/cloudbuild.yaml"
    BUILD_LOG="${CB_DIR}/build.log"
    cat >"$CB_CONFIG" <<EOF
steps:
- name: 'gcr.io/cloud-builders/docker'
  args: ['build', '-t', '${IMAGE}', '.']
images:
- '${IMAGE}'
serviceAccount: 'projects/${PROJECT_ID}/serviceAccounts/${BUILD_SA}'
options:
  logging: CLOUD_LOGGING_ONLY
EOF
    BUILD_OK=0
    for build_attempt in 1 2 3; do
      if gcloud builds submit "$ROOT" \
          --project "$PROJECT_ID" \
          --region "$REGION" \
          --config "$CB_CONFIG" \
          --gcs-source-staging-dir "gs://${SETTINGS_BUCKET}/_build/source" \
          2>&1 | tee "$BUILD_LOG"; then
        BUILD_OK=1
        break
      fi
      if [[ $build_attempt -lt 3 ]] && grep -Eiq 'PERMISSION_DENIED|NOT_FOUND|does not exist|serviceAccount|actAs|propagation' "$BUILD_LOG"; then
        warn "Cloud Build attempt ${build_attempt}/3 hit IAM/API propagation; retrying in 12s..."
        sleep 12
      else
        break
      fi
    done

    # Tier 2 fallback: if regional worker pool quota is 0 or unavailable (and not restricted by resourceLocations), try global Cloud Build.
    if [[ "$BUILD_OK" != "1" ]] && ! grep -Eiq 'resourceLocations' "$BUILD_LOG"; then
      warn "regional Cloud Build (${REGION}) did not succeed; retrying with global Cloud Build..."
      if gcloud builds submit "$ROOT" \
          --project "$PROJECT_ID" \
          --config "$CB_CONFIG" \
          --gcs-source-staging-dir "gs://${SETTINGS_BUCKET}/_build/source" \
          2>&1 | tee -a "$BUILD_LOG"; then
        BUILD_OK=1
      fi
    fi

    gcloud storage rm --recursive "gs://${SETTINGS_BUCKET}/_build/**" --project "$PROJECT_ID" >/dev/null 2>&1 || true
    gcloud storage buckets remove-iam-policy-binding "gs://${SETTINGS_BUCKET}" \
      --member "serviceAccount:${BUILD_SA}" --role roles/storage.objectAdmin >/dev/null 2>&1 || true

    # Tier 3 fallback: Cloud Shell and workstations with Docker build & push directly to Artifact Registry.
    if [[ "$BUILD_OK" != "1" ]]; then
      if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        warn "Cloud Build blocked or unavailable; building locally with Docker and pushing to Artifact Registry..."
        gcloud auth print-access-token 2>/dev/null \
          | docker login -u oauth2accesstoken --password-stdin "https://${REGION}-docker.pkg.dev" >/dev/null 2>&1 \
          || gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
        if (docker build --platform linux/amd64 -t "$IMAGE" "$ROOT" || docker build -t "$IMAGE" "$ROOT") \
            && docker push "$IMAGE"; then
          BUILD_OK=1
        fi
      fi
    fi
    if [[ "$BUILD_OK" != "1" ]]; then
      BUILD_TAIL="$(tail -n 20 "$BUILD_LOG" 2>/dev/null || true)"
      rm -rf "$CB_DIR"
      die "container image build failed for ${PROJECT_ID} (as ${ACCOUNT}):
${BUILD_TAIL}"
    fi
    rm -rf "$CB_DIR"
  fi
else
  info "using prebuilt image ${IMAGE}"
fi

# ------------------------------------------------------------------------------- 5. secrets
log "5/8 Session signing key (Secret Manager: ${SESSION_SECRET_NAME})"
ensure_secret_created "$SESSION_SECRET_NAME" "$PROJECT_ID" "$REGION"
if [[ "$DRY_RUN" == "1" ]]; then
  info "[dry-run] ensure ${SESSION_SECRET_NAME} has an active secret version"
else
  if [[ -z "$(gcloud secrets versions list "$SESSION_SECRET_NAME" --project "$PROJECT_ID" --filter='state=ENABLED' --limit 1 --format='value(name)' 2>/dev/null || true)" ]]; then
    python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_urlsafe(48))' \
      | gcloud secrets versions add "$SESSION_SECRET_NAME" --project "$PROJECT_ID" --data-file=- >/dev/null
    info "created secret version"
  fi
fi
run retry gcloud secrets add-iam-policy-binding "$SESSION_SECRET_NAME" --project "$PROJECT_ID" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/secretmanager.secretAccessor --condition=None >/dev/null
if [[ "$ACCESS_MODE" == "google" ]]; then
  run retry gcloud secrets add-iam-policy-binding "$SESSION_SECRET_NAME" --project "$PROJECT_ID" \
    --member "serviceAccount:${BRIDGE_SA}" --role roles/secretmanager.secretAccessor --condition=None >/dev/null
fi
SECRETS="SESSION_SECRET=${SESSION_SECRET_NAME}:latest"

OAUTH_CLIENT_ID=""
if [[ "$ACCESS_MODE" == "google_oauth" ]]; then
  OAUTH_CLIENT_ID="$(python3 - "$OAUTH_CLIENT_JSON" "$BASE_URL" <<'PY'
import json, sys
body = json.load(open(sys.argv[1])).get("web")
if not body:
    sys.exit("the client JSON is not a 'Web application' OAuth client")
if not body.get("client_secret"):
    sys.exit("the client JSON has no client_secret (download it right after creating the client)")
expected = sys.argv[2] + "/auth/google/callback"
if expected not in (body.get("redirect_uris") or []):
    print("warning: add %s as an authorized redirect URI of the OAuth client" % expected, file=sys.stderr)
print(body["client_id"])
PY
)"
  info "OAuth client ${OAUTH_CLIENT_ID}"
  ensure_secret_created "$OAUTH_SECRET_NAME" "$PROJECT_ID" "$REGION"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] add the client secret from ${OAUTH_CLIENT_JSON} as a new version of ${OAUTH_SECRET_NAME}"
  else
    python3 -c 'import json,sys; sys.stdout.write(json.load(open(sys.argv[1]))["web"]["client_secret"])' "$OAUTH_CLIENT_JSON" \
      | gcloud secrets versions add "$OAUTH_SECRET_NAME" --project "$PROJECT_ID" --data-file=- >/dev/null
  fi
  run retry gcloud secrets add-iam-policy-binding "$OAUTH_SECRET_NAME" --project "$PROJECT_ID" \
    --member "serviceAccount:${SA_EMAIL}" --role roles/secretmanager.secretAccessor --condition=None >/dev/null
  SECRETS="${SECRETS},GOOGLE_OAUTH_CLIENT_SECRET=${OAUTH_SECRET_NAME}:latest"
elif [[ -n "$GOOGLE_OAUTH_CLIENT_ID" ]] && gcloud secrets describe "$OAUTH_SECRET_NAME" --project "$PROJECT_ID" >/dev/null 2>&1; then
  info "OAuth client ${GOOGLE_OAUTH_CLIENT_ID} (using Secret Manager secret ${OAUTH_SECRET_NAME})"
  run retry gcloud secrets add-iam-policy-binding "$OAUTH_SECRET_NAME" --project "$PROJECT_ID" \
    --member "serviceAccount:${SA_EMAIL}" --role roles/secretmanager.secretAccessor --condition=None >/dev/null
  SECRETS="${SECRETS},GOOGLE_OAUTH_CLIENT_SECRET=${OAUTH_SECRET_NAME}:latest"
fi

# ------------------------------------------------------------------------------- 6. data sources & auto-detection
log "6/8 Data sources: audit log routing, Data Access logs, Gemini Enterprise & Agent Platform"
if [[ -z "$ORGANIZATION_NAME" || "$ORGANIZATION_NAME" == "Your Organization" ]]; then
  DETECTED_ORG="$(auto_detect_org_name "$PROJECT_ID")"
  if [[ -n "$DETECTED_ORG" ]]; then
    ORGANIZATION_NAME="$DETECTED_ORG"
    info "auto-detected organization/workspace name: ${ORGANIZATION_NAME}"
  fi
fi

if [[ -z "$AGENT_ENGINE_REGIONS" || -z "$AI_LOCATION" ]]; then
  AI_LOC_PAIR="$(auto_detect_ai_locations "$PROJECT_ID" "$REGION")"
  [[ -n "$AGENT_ENGINE_REGIONS" ]] || AGENT_ENGINE_REGIONS="${AI_LOC_PAIR%%|*}"
  [[ -n "$AI_LOCATION" ]] || AI_LOCATION="${AI_LOC_PAIR#*|}"
  info "Agent Platform endpoints: Agent Engine=${AGENT_ENGINE_REGIONS}, AI location=${AI_LOCATION}"
fi

if [[ -z "$DISCOVERY_ENGINE_LOCATIONS" ]]; then
  DISCOVERY_ENGINE_LOCATIONS="$(ge_locations "$PROJECT_ID")"
  if [[ -n "$DISCOVERY_ENGINE_LOCATIONS" ]]; then
    info "Gemini Enterprise locations: ${DISCOVERY_ENGINE_LOCATIONS} (detected)"
  else
    DISCOVERY_ENGINE_LOCATIONS="global"
    info "no Gemini Enterprise app or license found yet: using DISCOVERY_ENGINE_LOCATIONS=global"
  fi
fi

if [[ "$CREATE_LOG_SINK" == "true" ]]; then
  ensure_ai_log_sink "$PROJECT_ID" "$AUDIT_DATASET" "$DATASET_LOCATION" "${SERVICE_NAME}-ai-logs"
  if [[ "$DRY_RUN" != "1" ]]; then
    retry grant_dataset_role "$PROJECT_ID" "$AUDIT_DATASET" WRITER "$SA_EMAIL" >/dev/null 2>&1 || true
  fi
else
  info "sink skipped (CREATE_LOG_SINK=false): the dashboard reads the existing dataset ${AUDIT_DATASET}"
fi
if [[ "$ENABLE_DATA_ACCESS_LOGS" == "true" ]]; then
  enable_data_access_logs "$PROJECT_ID"
else
  info "Data Access audit logs unchanged (ENABLE_DATA_ACCESS_LOGS=false)"
fi
if [[ "$ENABLE_GE_OBSERVABILITY" == "true" ]]; then
  ensure_ge_observability "$PROJECT_ID" "$DISCOVERY_ENGINE_LOCATIONS" "$GE_PROMPT_LOGGING"
else
  info "Gemini Enterprise observability unchanged (ENABLE_GE_OBSERVABILITY=false)"
fi

# ------------------------------------------------------------------------------- 7. deploy
log "7/8 Deploying Cloud Run"
ENV_VARS="^##^PROJECT_ID=${PROJECT_ID}##AUDIT_DATASET_ID=${AUDIT_DATASET}##WORKSPACE_TIMEZONE=${WORKSPACE_TIMEZONE}"
ENV_VARS+="##ORGANIZATION_NAME=${ORGANIZATION_NAME}##DEFAULT_LOCALE=${DEFAULT_LOCALE}##ADMIN_EMAILS=${ADMIN_EMAILS}"
ENV_VARS+="##SETTINGS_BUCKET=${SETTINGS_BUCKET}##DISCOVERY_ENGINE_LOCATIONS=${DISCOVERY_ENGINE_LOCATIONS}"
ENV_VARS+="##AGENT_ENGINE_REGIONS=${AGENT_ENGINE_REGIONS}"
add_env() { [[ -n "$2" ]] && ENV_VARS+="##$1=$2"; return 0; }
add_env PRODUCT_NAME "$PRODUCT_NAME"
add_env DISABLED_MODULES "$DISABLED_MODULES"
add_env BILLING_PROJECT_ID "$BILLING_PROJECT_ID"
add_env BILLING_DATASET_ID "$BILLING_DATASET"
add_env BILLING_TABLE_ID "$BILLING_TABLE"
add_env BILLING_ACCOUNT_ID "$BILLING_ACCOUNT_ID"
add_env AI_DEFAULT_MODEL "$AI_DEFAULT_MODEL"
add_env AI_LOCATION "$AI_LOCATION"
add_env ALLOWED_USERS "$ALLOWED_USERS"
add_env GOOGLE_OAUTH_CLIENT_ID "$GOOGLE_OAUTH_CLIENT_ID"
add_env GOOGLE_OAUTH_REDIRECT_URI "$GOOGLE_OAUTH_REDIRECT_URI"
add_env GOOGLE_OAUTH_START_URL "$GOOGLE_OAUTH_START_URL"
if [[ "$GUEST_ACCESS" == "true" ]]; then
  add_env GUEST_ACCESS true
  add_env GUEST_REDACT_TERMS "$GUEST_REDACT_TERMS"
  warn "guest access is on: anyone with the link can browse read-only, identifiers hidden (demo use only)"
fi
[[ -n "$ENFORCEMENT_SCHEDULE" ]] && add_env SCHEDULER_INVOKERS "$SCHEDULER_SA"
COMMON=(--image "$IMAGE" --project "$PROJECT_ID" --region "$REGION" --cpu-boost --quiet)
MAIN=("${COMMON[@]}" --service-account "$SA_EMAIL")

RUN_DEPLOY_HELP="$(gcloud run deploy --help 2>&1 || true)"
NO_IAP_ARGS=()
[[ "$RUN_DEPLOY_HELP" == *"--no-iap"* || "$RUN_DEPLOY_HELP" == *"--iap"* ]] && NO_IAP_ARGS=(--no-iap)

# Collects explicit user: and domain: IAP principals from ADMIN_EMAILS, ALLOWED_USERS and active ACCOUNT.
bind_iap_principals() {  # bind_iap_principals SERVICE_NAME
  local svc="$1" principals fallback_member
  principals="$(python3 - "$ADMIN_EMAILS" "$ALLOWED_USERS" "$ACCOUNT" <<'PYIAP'
import sys
public_domains = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "icloud.com", "naver.com", "daum.net", "hanmail.net", "kakao.com"}
seen = set()
for raw in sys.argv[1:]:
    for token in raw.split(","):
        token = token.strip().lower()
        if "@" not in token:
            continue
        domain = token.rpartition("@")[2]
        cands = []
        if domain and domain not in public_domains:
            cands.append(f"domain:{domain}")
        if not token.startswith("*@"):
            cands.append(f"user:{token}")
        for cand in cands:
            if cand and cand not in seen:
                seen.add(cand)
                print(cand)
PYIAP
  )"
  while IFS= read -r fallback_member; do
    [[ -n "$fallback_member" ]] || continue
    run gcloud iap web add-iam-policy-binding --resource-type=cloud-run --service="$svc" --region="$REGION" \
      --project="$PROJECT_ID" --member="$fallback_member" --role=roles/iap.httpsResourceAccessor --condition=None >/dev/null 2>&1 \
      && info "IAP access (${svc}): ${fallback_member}" || true
  done <<<"$principals"
}

EFFECTIVE_ACCESS_MODE="$ACCESS_MODE"

# Deploys the public-entry dashboard service (which enforces signed session cookies on every route).
# 1) Tries --no-invoker-iam-check (succeeds under Domain Restricted Sharing without allUsers).
# 2) Falls back to --allow-unauthenticated.
# 3) If enterprise org policies block BOTH (--no-invoker-iam-check AND allUsers), automatically switches the
#    main dashboard service to Direct Cloud Run IAP and binds ADMIN_EMAILS / ALLOWED_USERS / org domain!
deploy_main_resilient() {
  local public_env="$1"
  if [[ "$DRY_RUN" == "1" ]]; then
    run gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" ${NO_IAP_ARGS[@]+"${NO_IAP_ARGS[@]}"} --no-invoker-iam-check \
      --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
      --set-env-vars "$public_env" --set-secrets "$SECRETS"
    return 0
  fi
  if [[ "$RUN_DEPLOY_HELP" == *"--no-invoker-iam-check"* ]]; then
    if gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" ${NO_IAP_ARGS[@]+"${NO_IAP_ARGS[@]}"} --no-invoker-iam-check \
        --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
        --set-env-vars "$public_env" --set-secrets "$SECRETS"; then
      return 0
    fi
    warn "--no-invoker-iam-check was rejected; retrying with --allow-unauthenticated..."
  fi
  if gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" ${NO_IAP_ARGS[@]+"${NO_IAP_ARGS[@]}"} --allow-unauthenticated \
      --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
      --set-env-vars "$public_env" --set-secrets "$SECRETS"; then
    return 0
  fi
  warn "organization policy blocks unauthenticated Cloud Run entry; automatically switching ${SERVICE_NAME} to Direct Cloud Run IAP..."
  local iap_env="${ENV_VARS}##AUTH_MODE=iap##IAP_AUDIENCE=/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE_NAME}"
  retry gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" --iap --invoker-iam-check --no-allow-unauthenticated \
    --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
    --set-env-vars "$iap_env" --set-secrets "$SECRETS" \
    || die "Cloud Run deployment failed under all access modes for ${PROJECT_ID}"
  retry gcloud run services add-iam-policy-binding "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
    --member "serviceAccount:${IAP_AGENT}" --role roles/run.invoker --condition=None >/dev/null
  bind_iap_principals "$SERVICE_NAME"
  EFFECTIVE_ACCESS_MODE="iap"
}

BRIDGE_READY=0
case "$ACCESS_MODE" in
  google)
    info "sign-in bridge ${BRIDGE_NAME} (IAP signs users in with Google; the dashboard approves accounts)"
    if [[ "$DRY_RUN" == "1" ]]; then
      run gcloud run deploy "$BRIDGE_NAME" "${COMMON[@]}" --service-account "$BRIDGE_SA" --no-allow-unauthenticated --iap \
        --memory 512Mi --cpu 1 --max-instances 3 --timeout 60 \
        --set-env-vars "^##^AUTH_MODE=iap##IAP_AUDIENCE=/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${BRIDGE_NAME}##BRIDGE_RETURN_URL=${BASE_URL}##ORGANIZATION_NAME=${ORGANIZATION_NAME}" \
        --set-secrets "$SECRETS"
      BRIDGE_READY=1
    else
      BRIDGE_LOG="$(mktemp)"
      for bridge_attempt in 1 2 3; do
        if gcloud run deploy "$BRIDGE_NAME" "${COMMON[@]}" --service-account "$BRIDGE_SA" --no-allow-unauthenticated --iap \
            --memory 512Mi --cpu 1 --max-instances 3 --timeout 60 \
            --set-env-vars "^##^AUTH_MODE=iap##IAP_AUDIENCE=/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${BRIDGE_NAME}##BRIDGE_RETURN_URL=${BASE_URL}##ORGANIZATION_NAME=${ORGANIZATION_NAME}" \
            --set-secrets "$SECRETS" 2>&1 | tee "$BRIDGE_LOG"; then
          BRIDGE_READY=1
          break
        fi
        if [[ $bridge_attempt -lt 3 ]] && grep -Eiq 'does not exist|serviceAccount|propagation|not ready|DEADLINE_EXCEEDED|UNAVAILABLE' "$BRIDGE_LOG" \
            && ! grep -Eiq 'organization|FAILED_PRECONDITION|not supported' "$BRIDGE_LOG"; then
          warn "sign-in bridge attempt ${bridge_attempt}/3 waiting on IAM/agent propagation; retrying in 8s..."
          sleep 8
        else
          break
        fi
      done
      rm -f "$BRIDGE_LOG"
      if [[ "$BRIDGE_READY" != "1" ]]; then
        warn "sign-in bridge (${BRIDGE_NAME}) with Cloud Run IAP could not be deployed; deploying the dashboard with built-in ID/Password sign-in enabled"
      fi
    fi
    if [[ "$BRIDGE_READY" == "1" ]]; then
      run retry gcloud run services add-iam-policy-binding "$BRIDGE_NAME" --project "$PROJECT_ID" --region "$REGION" \
        --member "serviceAccount:${IAP_AGENT}" --role roles/run.invoker --condition=None >/dev/null
      IFS=',' read -r -a members <<<"$BRIDGE_MEMBERS"
      for member in "${members[@]}"; do
        member="$(normalize_iam_member "$member")"
        [[ -n "$member" ]] || continue
        run gcloud iap web add-iam-policy-binding --resource-type=cloud-run --service="$BRIDGE_NAME" --region="$REGION" \
          --project="$PROJECT_ID" --member="$member" --role=roles/iap.httpsResourceAccessor --condition=None >/dev/null 2>&1 || true
      done
      bind_iap_principals "$BRIDGE_NAME"
      grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "roles/iap.admin"
      if [[ "$DRY_RUN" -eq 0 ]]; then
        IAP_BRAND="projects/${PROJECT_NUMBER}/brands/${PROJECT_NUMBER}"
        IAP_CLIENT_JSON="$(gcloud iap oauth-clients list "$IAP_BRAND" --project="$PROJECT_ID" --format=json 2>/dev/null || true)"
        if [[ -z "$IAP_CLIENT_JSON" || "$IAP_CLIENT_JSON" == "[]" ]]; then
          gcloud iap oauth-clients create "$IAP_BRAND" --display_name="${SERVICE_NAME}-iap" --project="$PROJECT_ID" --quiet >/dev/null 2>&1 || true
          IAP_CLIENT_JSON="$(gcloud iap oauth-clients list "$IAP_BRAND" --project="$PROJECT_ID" --format=json 2>/dev/null || true)"
        fi
        IAP_SETTINGS_FILE="$(mktemp)"
        if python3 - "$IAP_CLIENT_JSON" "$IAP_SETTINGS_FILE" <<'PY' 2>/dev/null
import json, sys
try:
    clients = json.loads((sys.argv[1] or "").strip() or "[]")
except Exception:
    sys.exit(1)
if not isinstance(clients, list) or not clients:
    sys.exit(1)
c = clients[0]
cid = c.get("name", "").rpartition("/")[2]
secret = c.get("secret", "")
if not cid or not secret:
    sys.exit(1)
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    fh.write(f"access_settings:\n  oauth_settings:\n    client_id: {cid}\n    client_secret: {secret}\n")
PY
        then
          gcloud iap settings set "$IAP_SETTINGS_FILE" --project="$PROJECT_ID" --resource-type=cloud-run --region="$REGION" --service="$BRIDGE_NAME" --quiet >/dev/null 2>&1 \
            && info "sign-in bridge custom IAP OAuth client configured" || true
        fi
        rm -f "$IAP_SETTINGS_FILE"
      fi
      MAIN_ENV="${ENV_VARS}##AUTH_MODE=google##SIGN_IN_BRIDGE_URL=${BRIDGE_URL}##PUBLIC_BASE_URL=${BASE_URL}"
    else
      MAIN_ENV="${ENV_VARS}##AUTH_MODE=google##PUBLIC_BASE_URL=${BASE_URL}"
    fi
    deploy_main_resilient "$MAIN_ENV"
    ;;
  password)
    deploy_main_resilient "${ENV_VARS}##AUTH_MODE=google##PUBLIC_BASE_URL=${BASE_URL}"
    ;;
  google_oauth)
    deploy_main_resilient "${ENV_VARS}##AUTH_MODE=google##GOOGLE_OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID}##PUBLIC_BASE_URL=${BASE_URL}"
    ;;
  iap)
    IAP_ENV="${ENV_VARS}##AUTH_MODE=iap##IAP_AUDIENCE=/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE_NAME}"
    run retry gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" --iap --invoker-iam-check --no-allow-unauthenticated \
      --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
      --set-env-vars "$IAP_ENV" --set-secrets "$SECRETS"
    run retry gcloud run services add-iam-policy-binding "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
      --member "serviceAccount:${IAP_AGENT}" --role roles/run.invoker --condition=None >/dev/null
    IFS=',' read -r -a members <<<"$IAP_MEMBERS"
    for member in "${members[@]}"; do
      member="$(normalize_iam_member "$member")"
      [[ -n "$member" ]] || continue
      run gcloud iap web add-iam-policy-binding --resource-type=cloud-run --service="$SERVICE_NAME" --region="$REGION" \
        --project="$PROJECT_ID" --member="$member" --role=roles/iap.httpsResourceAccessor --condition=None >/dev/null 2>&1 \
        && info "IAP access: ${member}" || true
    done
    bind_iap_principals "$SERVICE_NAME"
    ;;
esac

# ------------------------------------------------------------------------------- 8. scheduler & live readiness probe
log "8/8 Schedule & readiness check"
if [[ -n "$ENFORCEMENT_SCHEDULE" && "$EFFECTIVE_ACCESS_MODE" != "iap" ]]; then
  if ! gcloud iam service-accounts describe "$SCHEDULER_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
    run gcloud iam service-accounts create "$SCHEDULER_SA_ID" --project "$PROJECT_ID" \
      --display-name "AI Control Plane enforcement scheduler"
  fi
  JOB="${SERVICE_NAME}-enforce"
  JOB_ARGS=(--project "$PROJECT_ID" --location "$REGION" --schedule "$ENFORCEMENT_SCHEDULE"
    --time-zone "$WORKSPACE_TIMEZONE" --uri "$ENFORCE_URL" --http-method POST --message-body '{}'
    --oidc-service-account-email "$SCHEDULER_SA" --oidc-token-audience "$ENFORCE_URL"
    --attempt-deadline 300s --max-retry-attempts 1 --quiet)
  if gcloud scheduler jobs describe "$JOB" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
    run retry gcloud scheduler jobs update http "$JOB" "${JOB_ARGS[@]}" --update-headers Content-Type=application/json >/dev/null
  else
    run retry gcloud scheduler jobs create http "$JOB" "${JOB_ARGS[@]}" --headers Content-Type=application/json >/dev/null
  fi
  info "job ${JOB}: ${ENFORCEMENT_SCHEDULE} (${WORKSPACE_TIMEZONE}) -> ${ENFORCE_URL}"
else
  info "enforcement schedule: on-demand from the dashboard (ENFORCEMENT_SCHEDULE empty)"
fi

if [[ "$DRY_RUN" != "1" ]]; then
  HTTP_CODE="$(python3 - "${BASE_URL}/api/v1/auth/state" <<'PYPROBE' || echo "000"
import sys, urllib.error, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
opener = urllib.request.build_opener(NoRedirect)
try:
    with opener.open(urllib.request.Request(sys.argv[1]), timeout=15) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("000")
PYPROBE
)"
  if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "302" || "$HTTP_CODE" == "401" ]]; then
    info "live readiness verified (${BASE_URL} responded HTTP ${HTTP_CODE})"
  fi
fi

log "Deployment complete: ${PROJECT_ID}"
case "$EFFECTIVE_ACCESS_MODE" in
  iap) info "Dashboard URL : ${BASE_URL} (Google IAP sign-in)" ;;
  password) info "Dashboard URL : ${BASE_URL}/login (ID/Password: initial account admin / admin)" ;;
  google)
    if [[ "$BRIDGE_READY" == "1" ]]; then
      info "Dashboard URL : ${BASE_URL}/login (Google sign-in: ${ADMIN_EMAILS}${ALLOWED_USERS:+, viewers: $ALLOWED_USERS})"
    else
      info "Dashboard URL : ${BASE_URL}/login (ID/Password: initial account admin / admin)"
    fi ;;
  *) info "Dashboard URL : ${BASE_URL}/login (${ADMIN_EMAILS})" ;;
esac
info "Audit dataset : ${PROJECT_ID}:${AUDIT_DATASET} (${DATASET_LOCATION}, Data Access=${ENABLE_DATA_ACCESS_LOGS}, GE Prompt Logging=${GE_PROMPT_LOGGING})"
info "Settings store: gs://${SETTINGS_BUCKET}"
info "Container     : ${IMAGE}"
