#!/usr/bin/env bash
# AI Control Plane: prepare all AI data sources and audit logs before deployment (safe to re-run).
#
#   scripts/prepare.sh --project PROJECT_ID
#   scripts/prepare.sh --projects proj-a,proj-b
#
# Logs are recorded from the moment they are turned on, so running this early means the dashboard shows
# history from day one. scripts/deploy.sh reuses everything created here.
#   1. Enable all AI, Logging, BigQuery, Monitoring, Discovery Engine, Agent Platform and Model Armor APIs.
#   2. Create the BigQuery dataset (asia-northeast3 by default) and the Log Router sink for AI logs.
#   3. Turn on observability (usage + prompt/user identity logs) for every Gemini Enterprise app.
#   4. Enable Cloud Audit Data Access logs across all 7 AI services (Agent Platform, Gemini Enterprise,
#      Model Armor, Antigravity, NotebookLM, Gemini Data Analytics and Gemini API).
#   5. Report which sources already have log entries.
#
# Options
#   --project ID            project to prepare (default: active gcloud project)
#   --projects ID1,ID2      prepare multiple projects in one run
#   --dataset-location LOC  location of a new dataset (default: asia-northeast3)
#   --data-access-logs      turn on Data Access audit logs (default: on)
#   --no-data-access-logs   skip Data Access audit logs
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
CLI_PROJECTS=""
DATASET="ge_analytics"
DATASET_LOCATION="asia-northeast3"
SERVICE_NAME="ai-control-plane"
GE_LOCATIONS="$GE_LOCATIONS_ALL"
DATA_ACCESS_LOGS=true
LOG_PROMPTS=true
DRY_RUN=0

usage() { sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
need() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 needs a value"; }
ORIG_ARGS=("$@")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) need "$@"; PROJECT_ID="$2"; shift 2 ;;
    --projects) need "$@"; CLI_PROJECTS="$2"; shift 2 ;;
    --dataset) need "$@"; DATASET="$2"; shift 2 ;;
    --dataset-location) need "$@"; DATASET_LOCATION="$2"; shift 2 ;;
    --service-name) need "$@"; SERVICE_NAME="$2"; shift 2 ;;
    --ge-locations) need "$@"; GE_LOCATIONS="$2"; shift 2 ;;
    --data-access-logs) DATA_ACCESS_LOGS=true; shift ;;
    --no-data-access-logs) DATA_ACCESS_LOGS=false; shift ;;
    --log-prompts) LOG_PROMPTS=true; shift ;;
    --no-log-prompts) LOG_PROMPTS=false; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

if [[ -n "$CLI_PROJECTS" ]]; then
  IFS=',' read -r -a MULTI_LIST <<<"$CLI_PROJECTS"
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
    "$0" --project "$pid" ${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}
  done
  exit 0
fi

for tool in gcloud bq python3; do command -v "$tool" >/dev/null || die "$tool is required (Cloud Shell has all of them)"; done
ACCOUNT="$(ensure_gcloud_auth)"
PROJECT_ID="$(normalize_project_id "$PROJECT_ID")"
[[ -n "$PROJECT_ID" ]] || PROJECT_ID="$(normalize_project_id "$(gcloud config get-value project 2>/dev/null || true)")"
[[ "$PROJECT_ID" == "(unset)" ]] && PROJECT_ID=""
[[ -n "$PROJECT_ID" ]] || die "pass --project PROJECT_ID (no active gcloud project)"
validate_project_id "$PROJECT_ID" "--project"
[[ "$DATASET" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && ${#DATASET} -le 1024 ]] || die "--dataset must be letters, digits and underscores"
[[ "$SERVICE_NAME" =~ ^[a-z]([a-z0-9-]{0,40}[a-z0-9])?$ ]] || die "--service-name must be lowercase letters, digits and hyphens"
[[ "$GE_LOCATIONS" =~ ^[a-z0-9-]+(,[a-z0-9-]+)*$ ]] || die "--ge-locations must be a comma-separated list such as global,us,eu"

bootstrap_foundation_apis "$PROJECT_ID"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' 2>/dev/null | tr -d '[:space:]' || true)"
[[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] || die "cannot read project ${PROJECT_ID} with ${ACCOUNT}"

log "Preparing ${PROJECT_ID} for AI Control Plane as ${ACCOUNT}"
[[ "$DRY_RUN" == "1" ]] && info "dry run: commands that change anything are printed, not executed"
NEED_AUDIT_PREP=0; [[ "$DATA_ACCESS_LOGS" == "true" ]] && NEED_AUDIT_PREP=1
preflight_caller_permissions prepare "$PROJECT_ID" "$ACCOUNT" 0 1 0 0 "$NEED_AUDIT_PREP"

log "1/5 Enabling AI, Logging, BigQuery and Governance APIs"
enable_project_apis "$PROJECT_ID" 0 0 1
ensure_service_identities "$PROJECT_ID" "$PROJECT_NUMBER" \
  logging.googleapis.com bigquery.googleapis.com discoveryengine.googleapis.com aiplatform.googleapis.com modelarmor.googleapis.com dlp.googleapis.com

log "2/5 Routing AI logs to BigQuery (${PROJECT_ID}:${DATASET} in ${DATASET_LOCATION})"
ensure_ai_log_sink "$PROJECT_ID" "$DATASET" "$DATASET_LOCATION" "${SERVICE_NAME}-ai-logs"

log "3/5 Gemini Enterprise usage & prompt logs (${GE_LOCATIONS})"
ensure_ge_observability "$PROJECT_ID" "$GE_LOCATIONS" "$LOG_PROMPTS"

log "4/5 Data Access audit logs"
if [[ "$DATA_ACCESS_LOGS" == "true" ]]; then
  enable_data_access_logs "$PROJECT_ID"
else
  info "skipped (--no-data-access-logs)"
fi

log "5/5 Log entries in the last 30 days"
latest() {
  gcloud logging read "log_id(\"$1\")" --project "$PROJECT_ID" --freshness 30d --limit 1 \
    --format='value(timestamp)' 2>/dev/null || echo "?"
}
report() {
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
info "Next: deploy the dashboard with scripts/deploy.sh --project ${PROJECT_ID}"
