#!/usr/bin/env bash
# AI Control Plane: prepare the data sources before deployment (run once; safe to re-run).
#
#   scripts/prepare.sh --project PROJECT_ID [--dataset-location asia-northeast3] [--data-access-logs]
#
# Run it in Cloud Shell as a project Owner. Logs are only collected from the moment they are turned on,
# so running this early means the dashboard shows history from its first day. It changes only what the
# dashboard reads; scripts/deploy.sh later finds and reuses everything it creates.
#   1. Enable the Logging, BigQuery, Discovery Engine and Resource Manager APIs.
#   2. Create the BigQuery dataset and the Log Router sink that routes the AI logs into it (the same
#      names and filter as scripts/deploy.sh). Antigravity logs are included, so they start flowing as
#      soon as Antigravity is used.
#   3. Turn on observability (usage logs) for every Gemini Enterprise app.
#   4. With --data-access-logs: Data Access audit logs for Agent Platform, Discovery Engine and Model Armor.
#   5. Report which sources already have log entries.
#
# Options
#   --project ID            project to prepare (default: the active gcloud project)
#   --dataset-location LOC  location of a new dataset, for example asia-northeast3 or US (default: US)
#   --data-access-logs      also turn on Data Access audit logs (adds Cloud Logging volume and cost)
#   --log-prompts           log prompt/response text and user identity in Gemini Enterprise (default: on)
#   --no-log-prompts        turn on observability without sensitiveLoggingEnabled (leaves fields <elided>)
#   --ge-locations LIST     Gemini Enterprise locations to check (default: global,us,eu)
#   --dataset NAME          BigQuery dataset name (default: ge_analytics)
#   --service-name NAME     dashboard service name; the sink is NAME-ai-logs (default: ai-control-plane)
#   --dry-run               print what would change and change nothing
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$ROOT/scripts/lib/common.sh"

PROJECT_ID=""
DATASET="ge_analytics"
DATASET_LOCATION="US"
SERVICE_NAME="ai-control-plane"
GE_LOCATIONS="$GE_LOCATIONS_ALL"
DATA_ACCESS_LOGS=false
LOG_PROMPTS=true
DRY_RUN=0

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
need() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 needs a value"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) need "$@"; PROJECT_ID="$2"; shift 2 ;;
    --dataset) need "$@"; DATASET="$2"; shift 2 ;;
    --dataset-location) need "$@"; DATASET_LOCATION="$2"; shift 2 ;;
    --service-name) need "$@"; SERVICE_NAME="$2"; shift 2 ;;
    --ge-locations) need "$@"; GE_LOCATIONS="$2"; shift 2 ;;
    --data-access-logs) DATA_ACCESS_LOGS=true; shift ;;
    --log-prompts) LOG_PROMPTS=true; shift ;;
    --no-log-prompts) LOG_PROMPTS=false; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

for tool in gcloud bq python3; do command -v "$tool" >/dev/null || die "$tool is required (Cloud Shell has all of them)"; done
[[ -n "$PROJECT_ID" ]] || PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
[[ -n "$PROJECT_ID" ]] || die "pass --project PROJECT_ID (no active gcloud project)"
[[ "$DATASET" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && ${#DATASET} -le 1024 ]] || die "--dataset must be letters, digits and underscores"
[[ "$SERVICE_NAME" =~ ^[a-z]([a-z0-9-]{0,40}[a-z0-9])?$ ]] || die "--service-name must be lowercase letters, digits and hyphens"
[[ "$GE_LOCATIONS" =~ ^[a-z0-9-]+(,[a-z0-9-]+)*$ ]] || die "--ge-locations must be a comma-separated list such as global,us,eu"
ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
[[ -n "$ACCOUNT" ]] || die "no active gcloud account (run: gcloud auth login)"
gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null 2>&1 \
  || die "cannot read project ${PROJECT_ID} with ${ACCOUNT}"

log "Preparing ${PROJECT_ID} for AI Control Plane as ${ACCOUNT}"
[[ "$DRY_RUN" == "1" ]] && info "dry run: commands that change anything are printed, not executed"

log "1/5 Enabling APIs"
run gcloud services enable logging.googleapis.com bigquery.googleapis.com discoveryengine.googleapis.com \
  cloudresourcemanager.googleapis.com --project "$PROJECT_ID"

log "2/5 Routing AI logs to BigQuery (${PROJECT_ID}:${DATASET})"
ensure_ai_log_sink "$PROJECT_ID" "$DATASET" "$DATASET_LOCATION" "${SERVICE_NAME}-ai-logs"

log "3/5 Gemini Enterprise usage logs (${GE_LOCATIONS})"
ensure_ge_observability "$PROJECT_ID" "$GE_LOCATIONS" "$LOG_PROMPTS"

log "4/5 Data Access audit logs"
if [[ "$DATA_ACCESS_LOGS" == "true" ]]; then
  enable_data_access_logs "$PROJECT_ID"
else
  info "unchanged (add --data-access-logs for Agent Gateway, NotebookLM and agent deployment details)"
fi

log "5/5 Log entries in the last 30 days"
latest() {  # latest LOG_ID: timestamp of the newest entry, empty when there is none, "?" when unreadable
  gcloud logging read "log_id(\"$1\")" --project "$PROJECT_ID" --freshness 30d --limit 1 \
    --format='value(timestamp)' 2>/dev/null || echo "?"
}
report() {  # report LABEL LOG_ID WHEN_EMPTY
  local when
  when="$(latest "$2")"
  if [[ "$when" == "?" ]]; then
    info "$1: cannot read the logs (needs the Logs Viewer role)"
  elif [[ -n "$when" ]]; then
    info "$1: yes (latest ${when})"
  else
    info "$1: not yet ($3)"
  fi
}
report "Gemini Enterprise usage" "discoveryengine.googleapis.com/gemini_enterprise_user_activity" \
  "entries appear after someone uses Gemini Enterprise"
report "Model Armor" "modelarmor.googleapis.com/sanitize_operations" "only when Model Armor screens prompts"
report "Antigravity" "businessaicode.googleapis.com/inference_response" \
  "starts with Antigravity for organizations; nothing else to do"

log "Done"
info "New entries reach BigQuery within minutes; tables appear in ${PROJECT_ID}:${DATASET} with the first entry of each log."
info "Next: deploy the dashboard with scripts/deploy.sh (it reuses the dataset ${DATASET} and the sink ${SERVICE_NAME}-ai-logs)."
