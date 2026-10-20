#!/usr/bin/env bash
# AI Control Plane: removes what scripts/deploy.sh created, using the same deploy.env.
#
#   scripts/teardown.sh                  # services, scheduler job, service accounts, IAM bindings, secrets
#   scripts/teardown.sh --delete-data    # also the settings bucket (UI settings, exception rules,
#                                        # suspension records, widget layouts) and the Log Router sink
#   scripts/teardown.sh --config other.env --yes
#
# Never deleted automatically: the BigQuery audit dataset (it holds your logs), the Artifact Registry
# repository and the Data Access audit log configuration. The script prints the commands for them.
#
# Users suspended by the dashboard keep the cap marker role until they are restored: restore them on
# the Antigravity Controls page (or set the policy caps to 0 and run enforcement) before tearing down.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/deploy.env"
DELETE_DATA=0
YES=0
usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --delete-data) DELETE_DATA=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done
log()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
quiet() { "$@" >/dev/null 2>&1; }
# IAM policy edits can fail transiently (concurrent changes); retry a few times.
retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    [[ $n -ge 4 ]] && return 1
    sleep 5
  done
}

[[ -f "$CONFIG" ]] || die "configuration file not found: $CONFIG"
# shellcheck disable=SC1090
source "$CONFIG"
PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-ai-control-plane}"
AUDIT_DATASET="${AUDIT_DATASET:-ge_analytics}"
AR_REPOSITORY="${AR_REPOSITORY:-ai-control-plane}"
BILLING_PROJECT_ID="${BILLING_PROJECT_ID:-}"
SERVICE_ACCOUNT_ID="${SERVICE_ACCOUNT_ID:-${SERVICE_NAME:0:27}-sa}"
SETTINGS_BUCKET="${SETTINGS_BUCKET:-${PROJECT_ID}-${SERVICE_NAME}-settings}"
SESSION_SECRET_NAME="${SESSION_SECRET_NAME:-${SERVICE_NAME}-session-secret}"
OAUTH_SECRET_NAME="${OAUTH_SECRET_NAME:-${SERVICE_NAME}-oauth-client-secret}"
[[ -n "$PROJECT_ID" && "$PROJECT_ID" != your-* ]] || die "set PROJECT_ID in $CONFIG"

SA_EMAIL="${SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="${SERVICE_NAME:0:20}-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
BRIDGE_NAME="${SERVICE_NAME}-signin"
SINK="${SERVICE_NAME}-ai-logs"

echo "This removes ${SERVICE_NAME} from project ${PROJECT_ID} (${REGION})."
[[ "$DELETE_DATA" == "1" ]] && echo "It also deletes gs://${SETTINGS_BUCKET} and the log sink ${SINK}."
if [[ "$YES" != "1" ]]; then
  read -r -p "Type the project id to continue: " answer
  [[ "$answer" == "$PROJECT_ID" ]] || die "cancelled"
fi

log "Cloud Scheduler job"
quiet gcloud scheduler jobs delete "${SERVICE_NAME}-enforce" --project "$PROJECT_ID" --location "$REGION" --quiet \
  && info "deleted ${SERVICE_NAME}-enforce" || info "not found"

log "Cloud Run services"
for svc in "$SERVICE_NAME" "$BRIDGE_NAME"; do
  quiet gcloud run services delete "$svc" --project "$PROJECT_ID" --region "$REGION" --quiet \
    && info "deleted ${svc}" || info "${svc} not found"
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
[[ -n "$BILLING_PROJECT_ID" && "$BILLING_PROJECT_ID" != "$PROJECT_ID" ]] && remove_bindings "$BILLING_PROJECT_ID" "serviceAccount:${SA_EMAIL}"

log "Service accounts"
for sa in "$SA_EMAIL" "$SCHEDULER_SA"; do
  quiet gcloud iam service-accounts delete "$sa" --project "$PROJECT_ID" --quiet && info "deleted ${sa}" || info "${sa} not found"
done

log "Secrets"
for secret in "$SESSION_SECRET_NAME" "$OAUTH_SECRET_NAME"; do
  quiet gcloud secrets delete "$secret" --project "$PROJECT_ID" --quiet && info "deleted ${secret}" || info "${secret} not found"
done

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
else
  info "kept gs://${SETTINGS_BUCKET} and the log sink ${SINK} (use --delete-data to remove them)"
fi

log "Kept on purpose"
info "BigQuery dataset (your audit logs):   bq rm -r -d ${PROJECT_ID}:${AUDIT_DATASET}"
info "Container images:                     gcloud artifacts repositories delete ${AR_REPOSITORY} --location ${REGION} --project ${PROJECT_ID}"
info "Data Access audit logs: IAM & Admin > Audit Logs in the Google Cloud console"
