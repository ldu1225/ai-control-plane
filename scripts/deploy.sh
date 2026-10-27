#!/usr/bin/env bash
# AI Control Plane: one-command setup and deployment to Cloud Run.
#
#   cp deploy.env.example deploy.env     # fill in your values (deploy.env is ignored by git)
#   scripts/deploy.sh                    # or: scripts/deploy.sh --config other.env [--dry-run]
#
# Safe to re-run: every step creates what is missing and updates what exists. deploy.env is the
# source of truth for the service configuration (environment variables and secrets are replaced on
# every run).
#
# Steps
#   1. Enable the required Google Cloud APIs.
#   2. Build the container image with Cloud Build (Artifact Registry), unless IMAGE is set.
#   3. Create the runtime service account and grant read-only roles plus the audit log writer
#      (and IAM write roles when ENABLE_GOVERNANCE_ACTIONS=true). ACCESS_MODE=google also gets a
#      separate sign-in bridge service account that can read only the session signing key.
#   4. Create a private, versioned Cloud Storage bucket for settings saved in the UI.
#   5. Create the session signing key in Secret Manager.
#   6. Data sources: BigQuery dataset + Log Router sink for AI logs, optional Data Access audit logs, and
#      Gemini Enterprise usage logs (observability) for every app (same steps as scripts/prepare.sh).
#   7. Deploy the dashboard and configure sign-in (ACCESS_MODE).
#   8. Optional: Cloud Scheduler job for automatic Antigravity cap enforcement.
#
# To start collecting logs before the dashboard is deployed, run scripts/prepare.sh first.
# Requirements: gcloud (recent), bq (part of the Google Cloud SDK), python3, and an account with
# Owner (or equivalent) on PROJECT_ID. Nothing is read from or written to anywhere else.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/deploy.env"
DRY_RUN=0

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

# log, info, warn, die, run, retry, grant_dataset_role and the data source steps (shared with prepare.sh).
# shellcheck source=lib/common.sh
source "$ROOT/scripts/lib/common.sh"

[[ -f "$CONFIG" ]] || die "configuration file not found: $CONFIG (copy deploy.env.example to deploy.env first)"
# shellcheck disable=SC1090
source "$CONFIG"

# ------------------------------------------------------------------------------------ settings
PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-ai-control-plane}"
ADMIN_EMAILS="${ADMIN_EMAILS:-}"
ACCESS_MODE="${ACCESS_MODE:-google}"
ALLOWED_USERS="${ALLOWED_USERS:-}"
IAP_MEMBERS="${IAP_MEMBERS:-}"
BRIDGE_MEMBERS="${BRIDGE_MEMBERS:-allAuthenticatedUsers}"
# Demo only: visitors without a session browse read-only with identifiers hidden (GUEST_REDACT_TERMS, comma
# separated; defaults to the terms in .prepublish-terms when that file exists).
GUEST_ACCESS="${GUEST_ACCESS:-false}"
GUEST_REDACT_TERMS="${GUEST_REDACT_TERMS:-}"
OAUTH_CLIENT_JSON="${OAUTH_CLIENT_JSON:-}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"
ORGANIZATION_NAME="${ORGANIZATION_NAME:-Your Organization}"
PRODUCT_NAME="${PRODUCT_NAME:-}"
DEFAULT_LOCALE="${DEFAULT_LOCALE:-en}"
WORKSPACE_TIMEZONE="${WORKSPACE_TIMEZONE:-UTC}"
AUDIT_DATASET="${AUDIT_DATASET:-ge_analytics}"
CREATE_LOG_SINK="${CREATE_LOG_SINK:-true}"
DATASET_LOCATION="${DATASET_LOCATION:-US}"
ENABLE_DATA_ACCESS_LOGS="${ENABLE_DATA_ACCESS_LOGS:-false}"
# Usage logs (observability) and user/prompt logging (sensitiveLoggingEnabled) of every Gemini Enterprise
# app in DISCOVERY_ENGINE_LOCATIONS. Without GE_PROMPT_LOGGING=true, userIamPrincipal is logged as <elided>.
ENABLE_GE_OBSERVABILITY="${ENABLE_GE_OBSERVABILITY:-true}"
GE_PROMPT_LOGGING="${GE_PROMPT_LOGGING:-true}"
BILLING_PROJECT_ID="${BILLING_PROJECT_ID:-}"
BILLING_DATASET="${BILLING_DATASET:-}"
BILLING_TABLE="${BILLING_TABLE:-}"
BILLING_ACCOUNT_ID="${BILLING_ACCOUNT_ID:-}"
# Empty: step 6 detects the locations with a Gemini Enterprise app or license (global when there is none).
DISCOVERY_ENGINE_LOCATIONS="${DISCOVERY_ENGINE_LOCATIONS:-}"
AGENT_ENGINE_REGIONS="${AGENT_ENGINE_REGIONS:-$REGION}"
ENABLE_GOVERNANCE_ACTIONS="${ENABLE_GOVERNANCE_ACTIONS:-false}"
ENFORCEMENT_SCHEDULE="${ENFORCEMENT_SCHEDULE:-}"
AI_DEFAULT_MODEL="${AI_DEFAULT_MODEL:-}"
AI_LOCATION="${AI_LOCATION:-}"
IMAGE="${IMAGE:-}"
AR_REPOSITORY="${AR_REPOSITORY:-ai-control-plane}"
SERVICE_ACCOUNT_ID="${SERVICE_ACCOUNT_ID:-${SERVICE_NAME:0:27}-sa}"
SETTINGS_BUCKET="${SETTINGS_BUCKET:-${PROJECT_ID}-${SERVICE_NAME}-settings}"
SESSION_SECRET_NAME="${SESSION_SECRET_NAME:-${SERVICE_NAME}-session-secret}"
OAUTH_SECRET_NAME="${OAUTH_SECRET_NAME:-${SERVICE_NAME}-oauth-client-secret}"
MEMORY="${MEMORY:-1Gi}"
CPU="${CPU:-1}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-3}"

# ---------------------------------------------------------------------------------- validation
for tool in gcloud bq python3; do command -v "$tool" >/dev/null || die "$tool is required"; done
[[ -n "$PROJECT_ID" && "$PROJECT_ID" != your-* ]] || die "set PROJECT_ID in $CONFIG"
[[ -n "$ADMIN_EMAILS" && "$ADMIN_EMAILS" != *example.com* ]] || die "set ADMIN_EMAILS in $CONFIG"
[[ "$SERVICE_NAME" =~ ^[a-z]([a-z0-9-]{0,40}[a-z0-9])?$ ]] || die "SERVICE_NAME must be lowercase letters, digits and hyphens"
[[ ${#SETTINGS_BUCKET} -le 63 ]] || die "SETTINGS_BUCKET is longer than 63 characters; set a shorter SETTINGS_BUCKET"
case "$ACCESS_MODE" in
  google) ;;
  google_oauth) [[ -f "$OAUTH_CLIENT_JSON" ]] || die "ACCESS_MODE=google_oauth needs OAUTH_CLIENT_JSON (downloaded OAuth client JSON)" ;;
  iap) [[ -n "$IAP_MEMBERS" ]] || die "ACCESS_MODE=iap needs IAP_MEMBERS (for example group:ai-governance@example.com)" ;;
  *) die "ACCESS_MODE must be google, google_oauth or iap" ;;
esac
case "$GUEST_ACCESS" in
  true)
    [[ "$ACCESS_MODE" != "iap" ]] || die "GUEST_ACCESS=true needs ACCESS_MODE google or google_oauth"
    if [[ -z "$GUEST_REDACT_TERMS" && -f "$ROOT/.prepublish-terms" ]]; then
      GUEST_REDACT_TERMS="$(python3 - "$ROOT/.prepublish-terms" <<'PY'
import sys
terms = [line.split("#", 1)[0].strip() for line in open(sys.argv[1], encoding="utf-8")]
print(",".join(t for t in terms if t and not t.startswith("!") and "," not in t))  # "!": public phrases
PY
)"
    fi ;;
  false) ;;
  *) die "GUEST_ACCESS must be true or false" ;;
esac
if [[ -n "$ENFORCEMENT_SCHEDULE" ]]; then
  [[ "$ENABLE_GOVERNANCE_ACTIONS" == "true" ]] || die "ENFORCEMENT_SCHEDULE needs ENABLE_GOVERNANCE_ACTIONS=true"
  [[ "$ACCESS_MODE" != "iap" ]] || die "ENFORCEMENT_SCHEDULE needs ACCESS_MODE google or google_oauth (IAP blocks the scheduler's OIDC token)"
elif [[ "$ENABLE_GOVERNANCE_ACTIONS" == "true" ]]; then
  # Suspended users are restored only inside an enforcement run.
  if [[ "$ACCESS_MODE" == "iap" ]]; then
    warn "governance actions without a schedule (IAP mode cannot use one): suspended users get access back only when an administrator runs enforcement after the settlement period resets"
  else
    warn "governance actions without ENFORCEMENT_SCHEDULE: suspended users get access back only when an enforcement run happens after the settlement period resets; set ENFORCEMENT_SCHEDULE (for example \"*/15 * * * *\") to restore them automatically"
  fi
fi
for flag in CREATE_LOG_SINK ENABLE_DATA_ACCESS_LOGS ENABLE_GE_OBSERVABILITY GE_PROMPT_LOGGING ENABLE_GOVERNANCE_ACTIONS; do
  [[ "${!flag}" == "true" || "${!flag}" == "false" ]] || die "$flag must be true or false"
done
DISCOVERY_ENGINE_LOCATIONS="$(printf '%s' "$DISCOVERY_ENGINE_LOCATIONS" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
[[ -z "$DISCOVERY_ENGINE_LOCATIONS" || "$DISCOVERY_ENGINE_LOCATIONS" =~ ^[a-z0-9-]+(,[a-z0-9-]+)*$ ]] \
  || die "DISCOVERY_ENGINE_LOCATIONS must be a comma-separated list such as global,us (or empty to detect)"
# Modules hidden from every user at deploy time (Settings > Modules can switch them back on).
# Keep this list in sync with MODULE_IDS in src/controlplane/config.py (checked by the unit tests).
MODULE_IDS=(overview gemini_enterprise notebooklm model_usage agent_gateway agent_registry agent_deployments
  model_armor users antigravity finops telemetry analyst)
DISABLED_MODULES="$(printf '%s' "${DISABLED_MODULES:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
IFS=',' read -r -a disabled_list <<<"$DISABLED_MODULES"
for module in ${disabled_list[@]+"${disabled_list[@]}"}; do
  [[ -z "$module" || " ${MODULE_IDS[*]} " == *" $module "* ]] \
    || die "unknown module '$module' in DISABLED_MODULES (modules: ${MODULE_IDS[*]})"
done
if [[ -z "$BILLING_TABLE" && ",$DISABLED_MODULES," != *",finops,"* ]]; then
  info "no billing export configured (BILLING_TABLE): the cost pages stay empty until it is set in Settings;"
  info "set DISABLED_MODULES=\"finops\" in $(basename "$CONFIG") to hide them"
fi
ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
[[ -n "$ACCOUNT" ]] || die "no active gcloud account (run: gcloud auth login)"
PROJECT_NUMBER=""
GCLOUD_ERR="$(mktemp)"
trap 'rm -f "$GCLOUD_ERR"' EXIT
for attempt in 1 2 3; do
  # stdout only: gcloud may print notices on stderr, which must never end up in URLs or identities.
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' 2>"$GCLOUD_ERR" || true)"
  PROJECT_NUMBER="$(printf '%s' "$PROJECT_NUMBER" | tr -d '[:space:]')"
  [[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] && break
  [[ $attempt -lt 3 ]] || die "cannot read project ${PROJECT_ID} with ${ACCOUNT}: $(tail -n 3 "$GCLOUD_ERR")"
  sleep 5
done

SA_EMAIL="${SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
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

log "Deploying ${SERVICE_NAME} to ${PROJECT_ID} (${REGION}) as ${ACCOUNT}"
info "access mode: ${ACCESS_MODE}    dashboard URL: ${BASE_URL}"
[[ "$DRY_RUN" == "1" ]] && info "dry run: commands that change anything are printed, not executed"

# ------------------------------------------------------------------------------- 1. APIs
log "1/8 Enabling APIs"
APIS=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com bigquery.googleapis.com
  logging.googleapis.com monitoring.googleapis.com discoveryengine.googleapis.com aiplatform.googleapis.com
  cloudresourcemanager.googleapis.com iam.googleapis.com storage.googleapis.com secretmanager.googleapis.com
  cloudbilling.googleapis.com serviceusage.googleapis.com)
[[ "$ACCESS_MODE" != "google_oauth" ]] && APIS+=(iap.googleapis.com)
[[ -n "$ENFORCEMENT_SCHEDULE" ]] && APIS+=(cloudscheduler.googleapis.com)
run gcloud services enable "${APIS[@]}" --project "$PROJECT_ID"

# ------------------------------------------------------------------------------- 2. image
log "2/8 Container image"
if [[ -z "$IMAGE" ]]; then
  if ! gcloud artifacts repositories describe "$AR_REPOSITORY" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
    run gcloud artifacts repositories create "$AR_REPOSITORY" --repository-format=docker --location "$REGION" \
      --project "$PROJECT_ID" --description "AI Control Plane images"
  fi
  IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/${SERVICE_NAME}:$(date -u +%Y%m%d-%H%M%S)"
  info "building ${IMAGE}"
  run gcloud builds submit "$ROOT" --project "$PROJECT_ID" --region "$REGION" --tag "$IMAGE" || die "Cloud Build failed.
  New projects build as the Compute Engine default service account, which may need the builder role:
    gcloud projects add-iam-policy-binding ${PROJECT_ID} --role roles/cloudbuild.builds.builder \\
      --member serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com
  Then run this script again."
else
  info "using prebuilt image ${IMAGE}"
fi

# ------------------------------------------------------------------------------- 3. identity
log "3/8 Runtime service account ${SA_EMAIL}"
if ! gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
  run gcloud iam service-accounts create "$SERVICE_ACCOUNT_ID" --project "$PROJECT_ID" \
    --display-name "AI Control Plane runtime" --description "Least-privilege identity of the ${SERVICE_NAME} Cloud Run service"
fi
# Read roles plus logging.logWriter, which is always granted so that every governance record (dry runs,
# exception and review changes, enforcement runs) reaches the audit log, with or without IAM write roles.
RUNTIME_ROLES=(roles/bigquery.dataViewer roles/bigquery.jobUser roles/aiplatform.user roles/discoveryengine.viewer
  roles/monitoring.viewer roles/logging.viewer roles/iam.securityReviewer roles/serviceusage.serviceUsageConsumer
  roles/logging.logWriter)
GOVERNANCE_ROLES=(roles/resourcemanager.projectIamAdmin roles/iam.roleAdmin)
grant() {  # grant PROJECT ROLE
  run retry gcloud projects add-iam-policy-binding "$1" --member "serviceAccount:${SA_EMAIL}" --role "$2" \
    --condition=None --quiet >/dev/null
  [[ "$DRY_RUN" == "1" ]] || info "granted $2 on $1"
}
for role in "${RUNTIME_ROLES[@]}"; do grant "$PROJECT_ID" "$role"; done
if [[ "$ENABLE_GOVERNANCE_ACTIONS" == "true" ]]; then
  for role in "${GOVERNANCE_ROLES[@]}"; do grant "$PROJECT_ID" "$role"; done
else
  info "governance write roles skipped (ENABLE_GOVERNANCE_ACTIONS=false): dashboards and simulations only"
fi
# Billing export in another project: read access on that dataset only (no project-level role there).
if [[ -n "$BILLING_PROJECT_ID" && "$BILLING_PROJECT_ID" != "$PROJECT_ID" ]]; then
  if [[ -z "$BILLING_DATASET" ]]; then
    warn "BILLING_PROJECT_ID is set without BILLING_DATASET: billing read access was not granted"
  elif [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] grant BigQuery Data Viewer on ${BILLING_PROJECT_ID}:${BILLING_DATASET} (this dataset only)"
  elif retry grant_dataset_role "$BILLING_PROJECT_ID" "$BILLING_DATASET" READER "$SA_EMAIL"; then
    info "granted BigQuery Data Viewer on ${BILLING_PROJECT_ID}:${BILLING_DATASET} (this dataset only)"
    # Earlier versions granted this role on the whole billing project; the dataset grant replaces it.
    gcloud projects remove-iam-policy-binding "$BILLING_PROJECT_ID" --member "serviceAccount:${SA_EMAIL}" \
      --role roles/bigquery.dataViewer --condition=None --quiet >/dev/null 2>&1 \
      && info "removed the earlier project-level BigQuery Data Viewer on ${BILLING_PROJECT_ID}" || true
  else
    warn "could not grant read access on ${BILLING_PROJECT_ID}:${BILLING_DATASET}; grant BigQuery Data Viewer on that dataset to ${SA_EMAIL}"
  fi
fi
if [[ "$ACCESS_MODE" == "google" ]] && \
    ! gcloud iam service-accounts describe "$BRIDGE_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  # No project roles: the sign-in bridge only reads the session signing key (granted in step 5).
  run gcloud iam service-accounts create "$BRIDGE_SA_ID" --project "$PROJECT_ID" \
    --display-name "AI Control Plane sign-in bridge" --description "Identity of the ${BRIDGE_NAME} sign-in bridge"
fi

# ------------------------------------------------------------------------------- 4. settings bucket
log "4/8 Settings bucket gs://${SETTINGS_BUCKET}"
if ! gcloud storage buckets describe "gs://${SETTINGS_BUCKET}" --project "$PROJECT_ID" >/dev/null 2>&1; then
  run gcloud storage buckets create "gs://${SETTINGS_BUCKET}" --project "$PROJECT_ID" --location "$REGION" \
    --uniform-bucket-level-access --public-access-prevention
  run gcloud storage buckets update "gs://${SETTINGS_BUCKET}" --versioning
fi
run retry gcloud storage buckets add-iam-policy-binding "gs://${SETTINGS_BUCKET}" --member "serviceAccount:${SA_EMAIL}" \
  --role roles/storage.objectAdmin >/dev/null

# ------------------------------------------------------------------------------- 5. secrets
log "5/8 Session signing key (Secret Manager: ${SESSION_SECRET_NAME})"
if ! gcloud secrets describe "$SESSION_SECRET_NAME" --project "$PROJECT_ID" >/dev/null 2>&1; then
  if [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] create secret ${SESSION_SECRET_NAME} with a random 48-byte value"
  else
    python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_urlsafe(48))' \
      | gcloud secrets create "$SESSION_SECRET_NAME" --project "$PROJECT_ID" --replication-policy=automatic \
          --labels=app=ai-control-plane --data-file=- >/dev/null
    info "created"
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
  if ! gcloud secrets describe "$OAUTH_SECRET_NAME" --project "$PROJECT_ID" >/dev/null 2>&1; then
    run gcloud secrets create "$OAUTH_SECRET_NAME" --project "$PROJECT_ID" --replication-policy=automatic \
      --labels=app=ai-control-plane >/dev/null
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] add the client secret from ${OAUTH_CLIENT_JSON} as a new version of ${OAUTH_SECRET_NAME}"
  else
    python3 -c 'import json,sys; sys.stdout.write(json.load(open(sys.argv[1]))["web"]["client_secret"])' "$OAUTH_CLIENT_JSON" \
      | gcloud secrets versions add "$OAUTH_SECRET_NAME" --project "$PROJECT_ID" --data-file=- >/dev/null
  fi
  run retry gcloud secrets add-iam-policy-binding "$OAUTH_SECRET_NAME" --project "$PROJECT_ID" \
    --member "serviceAccount:${SA_EMAIL}" --role roles/secretmanager.secretAccessor --condition=None >/dev/null
  SECRETS="${SECRETS},GOOGLE_OAUTH_CLIENT_SECRET=${OAUTH_SECRET_NAME}:latest"
fi

# ------------------------------------------------------------------------------- 6. data sources
# Same steps as scripts/prepare.sh (scripts/lib/common.sh). Re-running updates an existing sink to the
# current filter, which also repairs sinks created by earlier versions of this script.
log "6/8 Data sources: audit log routing, Data Access logs, Gemini Enterprise usage logs"
if [[ -z "$DISCOVERY_ENGINE_LOCATIONS" ]]; then
  DISCOVERY_ENGINE_LOCATIONS="$(ge_locations "$PROJECT_ID")"
  if [[ -n "$DISCOVERY_ENGINE_LOCATIONS" ]]; then
    info "Gemini Enterprise locations: ${DISCOVERY_ENGINE_LOCATIONS} (detected; set DISCOVERY_ENGINE_LOCATIONS to choose)"
  else
    DISCOVERY_ENGINE_LOCATIONS="global"
    info "no Gemini Enterprise app or license found yet: using DISCOVERY_ENGINE_LOCATIONS=global"
  fi
fi
if [[ "$CREATE_LOG_SINK" == "true" ]]; then
  ensure_ai_log_sink "$PROJECT_ID" "$AUDIT_DATASET" "$DATASET_LOCATION" "${SERVICE_NAME}-ai-logs"
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
if [[ "$GUEST_ACCESS" == "true" ]]; then
  add_env GUEST_ACCESS true
  add_env GUEST_REDACT_TERMS "$GUEST_REDACT_TERMS"
  warn "guest access is on: anyone with the link can browse read-only, identifiers hidden (demo use only)"
fi
[[ -n "$ENFORCEMENT_SCHEDULE" ]] && add_env SCHEDULER_INVOKERS "$SCHEDULER_SA"
COMMON=(--image "$IMAGE" --project "$PROJECT_ID" --region "$REGION" --cpu-boost --quiet)
MAIN=("${COMMON[@]}" --service-account "$SA_EMAIL")

case "$ACCESS_MODE" in
  google)
    info "sign-in bridge ${BRIDGE_NAME} (IAP signs users in with Google; the dashboard approves accounts)"
    run gcloud run deploy "$BRIDGE_NAME" "${COMMON[@]}" --service-account "$BRIDGE_SA" --no-allow-unauthenticated --iap \
      --memory 512Mi --cpu 1 --max-instances 3 --timeout 60 \
      --set-env-vars "^##^AUTH_MODE=iap##IAP_AUDIENCE=/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${BRIDGE_NAME}##BRIDGE_RETURN_URL=${BASE_URL}##ORGANIZATION_NAME=${ORGANIZATION_NAME}" \
      --set-secrets "$SECRETS"
    run retry gcloud run services add-iam-policy-binding "$BRIDGE_NAME" --project "$PROJECT_ID" --region "$REGION" \
      --member "serviceAccount:${IAP_AGENT}" --role roles/run.invoker --condition=None >/dev/null
    IFS=',' read -r -a members <<<"$BRIDGE_MEMBERS"
    for member in "${members[@]}"; do
      member="${member// /}"
      [[ -n "$member" ]] || continue
      run gcloud iap web add-iam-policy-binding --resource-type=cloud-run --service="$BRIDGE_NAME" --region="$REGION" \
        --project="$PROJECT_ID" --member="$member" --role=roles/iap.httpsResourceAccessor --condition=None >/dev/null \
        || warn "IAP access for ${member} was refused (an organization policy may restrict it); set BRIDGE_MEMBERS, for example domain:example.com"
    done
    ENV_VARS+="##AUTH_MODE=google##SIGN_IN_BRIDGE_URL=${BRIDGE_URL}##PUBLIC_BASE_URL=${BASE_URL}"
    run gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" --no-iap --no-invoker-iam-check \
      --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
      --set-env-vars "$ENV_VARS" --set-secrets "$SECRETS"
    ;;
  google_oauth)
    ENV_VARS+="##AUTH_MODE=google##GOOGLE_OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID}##PUBLIC_BASE_URL=${BASE_URL}"
    run gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" --no-iap --no-invoker-iam-check \
      --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
      --set-env-vars "$ENV_VARS" --set-secrets "$SECRETS"
    ;;
  iap)
    ENV_VARS+="##AUTH_MODE=iap##IAP_AUDIENCE=/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE_NAME}"
    run gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" --iap --invoker-iam-check --no-allow-unauthenticated \
      --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
      --set-env-vars "$ENV_VARS" --set-secrets "$SECRETS"
    run retry gcloud run services add-iam-policy-binding "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
      --member "serviceAccount:${IAP_AGENT}" --role roles/run.invoker --condition=None >/dev/null
    IFS=',' read -r -a members <<<"$IAP_MEMBERS"
    for member in "${members[@]}"; do
      member="${member// /}"
      [[ -n "$member" ]] || continue
      run gcloud iap web add-iam-policy-binding --resource-type=cloud-run --service="$SERVICE_NAME" --region="$REGION" \
        --project="$PROJECT_ID" --member="$member" --role=roles/iap.httpsResourceAccessor --condition=None >/dev/null
      info "IAP access: ${member}"
    done
    ;;
esac

# ------------------------------------------------------------------------------- 8. scheduler
log "8/8 Automatic enforcement schedule"
if [[ -n "$ENFORCEMENT_SCHEDULE" ]]; then
  if ! gcloud iam service-accounts describe "$SCHEDULER_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
    # No project roles: it can only call the enforce endpoint, which checks its OIDC token.
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
  info "runs follow the enforcement mode chosen on the Antigravity Controls page (manual review by default)"
else
  info "skipped (ENFORCEMENT_SCHEDULE is empty): enforcement runs only from the dashboard"
fi

log "Done"
case "$ACCESS_MODE" in
  iap) info "Open ${BASE_URL} (IAP signs in: ${IAP_MEMBERS})" ;;
  *) info "Open ${BASE_URL}/login and sign in with an administrator account (${ADMIN_EMAILS})" ;;
esac
info "Image: ${IMAGE}"
info "Settings saved in the UI: gs://${SETTINGS_BUCKET}"
info "Next: Settings > Workspace and data sources shows which data sources are connected."
