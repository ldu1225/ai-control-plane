#!/usr/bin/env bash
# AI Control Plane: removes what scripts/deploy.sh created, using the same deploy.env.
#
#   scripts/teardown.sh --project PROJECT_ID     # services, scheduler jobs, service accounts, IAM bindings, secrets
#   scripts/teardown.sh --delete-data            # also the settings bucket (UI settings, exception rules,
#                                                # suspension records, widget layouts) and the Log Router sink
#   scripts/teardown.sh --config other.env --yes
#   scripts/teardown.sh --project PROJECT_ID --dry-run
#
# Never deleted automatically: the BigQuery audit dataset (it holds your logs), the Artifact Registry
# repository and the Data Access audit log configuration. The script prints the commands for them.
#
# Users suspended by Antigravity caps get their access role back first (nobody stays suspended after the
# control plane is gone); administrator blocks are kept unless --restore-blocked-users is given.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$ROOT/scripts/lib/common.sh"

CONFIG="$ROOT/deploy.env"
CONFIG_EXPLICIT=0
CLI_PROJECT=""
CLI_LINK_PROJECTS=""
CLI_REGION=""
DELETE_DATA=0
RESTORE_BLOCKED="false"
DRY_RUN=0
YES=0
usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; echo "  --restore-blocked-users  also restore users an administrator blocked"; exit "${1:-0}"; }
need_arg() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 needs a value"; }

normalize_cli_args "$@"
set -- ${NORMALIZED_ARGS[@]+"${NORMALIZED_ARGS[@]}"}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) need_arg "$@"; CONFIG="$2"; CONFIG_EXPLICIT=1; shift 2 ;;
    --project) need_arg "$@"; CLI_PROJECT="$2"; shift 2 ;;
    --link-projects|--scan-projects) need_arg "$@"; CLI_LINK_PROJECTS="$2"; shift 2 ;;
    --region) need_arg "$@"; CLI_REGION="$2"; shift 2 ;;
    --delete-data) DELETE_DATA=1; shift ;;
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
[[ -n "$CLI_REGION" ]] && REGION="$CLI_REGION"

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
TELEMETRY_DATASET="${TELEMETRY_DATASET:-telemetry_routed}"

echo "This removes ${SERVICE_NAME} from project ${PROJECT_ID} (${REGION})."
[[ "$DELETE_DATA" == "1" ]] && echo "It also deletes gs://${SETTINGS_BUCKET} and the log sink ${SINK}."
if [[ "$DRY_RUN" == "1" ]]; then
  restore_capped_users "$PROJECT_ID" "${GOVERNANCE_ACCESS_ROLE:-roles/discoveryengine.agentspaceUser}" "$RESTORE_BLOCKED"
  info "[dry-run] scheduler jobs: ${SERVICE_NAME}-enforce, ${SERVICE_NAME}-maintenance, ${SERVICE_NAME}-pricing-sync"
  info "[dry-run] telemetry route sinks (description \"AI Control Plane telemetry route\") on ${PROJECT_ID}${SCAN_PROJECTS:+ and linked projects}"
  info "[dry-run] Cloud Run services: ${SERVICE_NAME}, ${BRIDGE_NAME}"
  info "[dry-run] IAM bindings on ${PROJECT_ID}${SCAN_PROJECTS:+ and linked projects (${SCAN_PROJECTS})} for ${SA_EMAIL}"
  info "[dry-run] service accounts: ${SA_EMAIL}, ${BUILD_SA}, ${SCHEDULER_SA}, ${BRIDGE_SA}"
  info "[dry-run] secrets: ${SESSION_SECRET_NAME}, ${OAUTH_SECRET_NAME}, ${ADMIN_SECRET_NAME}"
  [[ "$DELETE_DATA" == "1" ]] && info "[dry-run] delete gs://${SETTINGS_BUCKET}, log sink ${SINK} and the routed telemetry dataset ${TELEMETRY_DATASET} (only if labelled managed-by=ai-control-plane)"
  exit 0
fi
if [[ "$YES" != "1" ]]; then
  read -r -p "Type the project id to continue: " answer
  [[ "$answer" == "$PROJECT_ID" ]] || die "cancelled"
fi

log "Antigravity suspensions"
restore_capped_users "$PROJECT_ID" "${GOVERNANCE_ACCESS_ROLE:-roles/discoveryengine.agentspaceUser}" "$RESTORE_BLOCKED"

log "Cloud Scheduler jobs"
for job in "${SERVICE_NAME}-enforce" "${SERVICE_NAME}-maintenance" "${SERVICE_NAME}-pricing-sync"; do
  quiet gcloud scheduler jobs delete "$job" --project "$PROJECT_ID" --location "$REGION" --quiet \
    && info "deleted ${job}" || info "${job} not found"
done

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
if [[ -n "$SCAN_PROJECTS" ]]; then
  IFS=',' read -r -a raw_route_projects <<<"$SCAN_PROJECTS"
  for raw_rp in "${raw_route_projects[@]}"; do
    rp="$(normalize_project_id "$raw_rp")"
    [[ -z "$rp" || "$rp" == "$PROJECT_ID" ]] && continue
    delete_route_sinks "$rp"
  done
fi

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
remove_bindings "$PROJECT_ID" "serviceAccount:${BUILD_SA}"
quiet gcloud artifacts repositories remove-iam-policy-binding "$AR_REPOSITORY" \
  --location "$REGION" --project "$PROJECT_ID" \
  --member "serviceAccount:${BUILD_SA}" --role roles/artifactregistry.writer --quiet || true

TORN_LINKED=()
if [[ -n "$SCAN_PROJECTS" ]]; then
  IFS=',' read -r -a raw_linked <<<"$SCAN_PROJECTS"
  for raw_lp in "${raw_linked[@]}"; do
    lp="$(normalize_project_id "$raw_lp")"
    [[ -z "$lp" || "$lp" == "$PROJECT_ID" ]] && continue
    [[ " ${TORN_LINKED[*]-} " == *" ${lp} "* ]] && continue
    # Every role deploy.sh granted there is a binding of this member and is removed here, including the license
    # reader custom role (projects/<linked project>/roles/CustomAiControlPlaneLicenseReader).
    remove_bindings "$lp" "serviceAccount:${SA_EMAIL}"
    TORN_LINKED+=("$lp")
  done
fi

if [[ -n "$BILLING_PROJECT_ID" && "$BILLING_PROJECT_ID" != "$PROJECT_ID" ]]; then
  remove_bindings "$BILLING_PROJECT_ID" "serviceAccount:${SA_EMAIL}"   # deployments made before dataset-level grants
  if [[ -n "$BILLING_DATASET" ]]; then
    # Dataset ACL entry added by deploy.sh (BigQuery Data Viewer on the billing export dataset only).
    file="$(mktemp)"
    if bq --project_id "$BILLING_PROJECT_ID" show --format=prettyjson "${BILLING_PROJECT_ID}:${BILLING_DATASET}" >"$file" 2>/dev/null \
        && python3 - "$file" "$SA_EMAIL" <<'PYCODE'
import json, sys
path, email = sys.argv[1], sys.argv[2].lower()
dataset = json.load(open(path))
access = dataset.get("access", [])
kept = [a for a in access if (a.get("userByEmail") or "").lower() != email]
if len(kept) == len(access):
    sys.exit(3)
dataset["access"] = kept
json.dump(dataset, open(path, "w"))
PYCODE
    then
      quiet bq --project_id "$BILLING_PROJECT_ID" update --source "$file" "${BILLING_PROJECT_ID}:${BILLING_DATASET}" \
        && info "removed ${SA_EMAIL} from ${BILLING_PROJECT_ID}:${BILLING_DATASET}" \
        || info "could not update ${BILLING_PROJECT_ID}:${BILLING_DATASET}; remove ${SA_EMAIL} from its sharing settings"
    fi
    rm -f "$file"
  fi
fi

log "Service accounts"
for sa in "$SA_EMAIL" "$BUILD_SA" "$SCHEDULER_SA" "$BRIDGE_SA"; do
  quiet gcloud iam service-accounts delete "$sa" --project "$PROJECT_ID" --quiet && info "deleted ${sa}" || info "${sa} not found"
done

log "Secrets"
for secret in "$SESSION_SECRET_NAME" "$OAUTH_SECRET_NAME" "$ADMIN_SECRET_NAME"; do
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
  # The routed telemetry dataset is deleted only when this control plane created it (label managed-by).
  if bq --project_id "$PROJECT_ID" show --format=json "${PROJECT_ID}:${TELEMETRY_DATASET}" 2>/dev/null \
      | python3 -c 'import json,sys; sys.exit(0 if (json.load(sys.stdin).get("labels") or {}).get("managed-by") == "ai-control-plane" else 3)' 2>/dev/null; then
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
info "Marker roles (delete only when no user holds them; users an administrator blocked keep their other access through them)."
info "Default IDs are shown; use your own if you changed governance.antigravity.cap_marker_role or admin_marker_role:"
info "  gcloud iam roles delete CustomAntigravityCapBlocked --project ${PROJECT_ID}"
info "  gcloud iam roles delete CustomAntigravityAdminBlocked --project ${PROJECT_ID}"
info "License reader custom role (its bindings are removed above; another dashboard in the same project may still use it):"
for lr_project in "$PROJECT_ID" ${TORN_LINKED[@]+"${TORN_LINKED[@]}"}; do
  info "  gcloud iam roles delete CustomAiControlPlaneLicenseReader --project ${lr_project}"
done
