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
# Running it again updates the deployment in place. With --project and no --config, the settings that
# scripts/install.sh saved in deploy.PROJECT_ID.env are read first; flags on the command line win.
#
# Options
#   --project ID            Google Cloud project ID to deploy into
#   --projects ID1,ID2      Deploy sequentially into multiple Google Cloud projects in one run
#   --link-projects ID1,ID2 Link secondary GCP projects for unified multi-project scanning, log routing & read IAM
#                           (alias --scan-projects; "none" clears the list deploy.env or a saved snapshot holds)
#   --prepare-linked-projects / --no-prepare-linked-projects  Onboard log sinks/APIs on the linked projects (default)
#                           or skip it
#   --auto-discover-projects / --no-auto-discover-projects  Discover readable projects of the same organization
#                           automatically (default) or not
#   --region REGION         Cloud Run, Artifact Registry & bucket region (default: asia-northeast3)
#   --admins EMAILS         Comma-separated admin emails or *@domain (default: active gcloud account; alias --admin)
#   --viewers EMAILS        Comma-separated viewer emails or *@domain; "none" clears them (aliases --viewer,
#                           --allowed-users)
#   --org-name NAME         Organization display name (default: auto-detected from GCP Org / Project)
#   --access-mode MODE      google | iap | password | google_oauth (default: google)
#   --service-name NAME     Cloud Run service name (default: ai-control-plane)
#   --billing-account ID    Cloud Billing account ID to link if the project is unbilled
#   --read-only             Skip IAM governance write roles (ENABLE_GOVERNANCE_ACTIONS=false)
#   --no-read-only          Grant them again (ENABLE_GOVERNANCE_ACTIONS=true, the default)
#   --enforcement-schedule CRON  Antigravity cap enforcement runs (default */5 * * * *: restores suspended
#                           users at each reset; suspends over-cap users only in automatic mode)
#   --no-enforcement-schedule    No scheduled runs (enforce from the dashboard after every period reset)
#   --realtime-enforcement / --no-realtime-enforcement  Pub/Sub push for immediate automatic cap enforcement
#                           (default on; off in IAP mode, with notify_only and with --read-only unless iam_deny)
#   --maintenance-schedule CRON  Cloud Scheduler tick for the daily price check, the telemetry route budget guard and
#                           the marker role hand-over (default */15 * * * *; not created in IAP mode)
#   --no-maintenance-schedule    No maintenance job (run the price check from the FinOps page)
#   --enforcement-backend MODE   iam_allow_swap (default: swap direct bindings) | iam_deny (IAM deny policy applied
#                           by a dedicated Cloud Run job) | notify_only (plan and notify, never change access)
#   --deny-attachment MODE  iam_deny: projects (default: every workspace project) | scope (its folders/organization)
#   --enforcement-job-region REGION  Region of the enforcement job and its schedule (default: --region)
#   --grant-org-roles / --no-grant-org-roles  iam_deny: grant Deny Admin / Deny Reviewer on the organization, or
#                           print the commands for an organization administrator (default)
#   --data-access-logs / --no-data-access-logs  Enable Data Access audit logs (default) or leave them unchanged
#   --log-prompts / --no-log-prompts  Gemini Enterprise usage logs with prompt/response logging (default) or without
#                           (sensitiveLoggingEnabled)
#   --dataset NAME / --dataset-location LOC / --ge-locations LIST   Audit dataset, its location (default: --region),
#                           Gemini Enterprise locations
#   --vpc-network NAME      Send Cloud Run egress through this VPC (needed when run.allowedVPCEgress forces it)
#   --vpc-subnet NAME       Subnet for --vpc-network (enable Private Google Access on it)
#   --skip-preflight-blockers  Continue even when the organization policy preflight finds blockers
#   --config FILE           Read settings from FILE (CLI flags override FILE values)
#   --dry-run               Print commands that would run without changing anything
#   -y, --yes               Accepted for scripts/install.sh compatibility (deploy.sh never prompts)
#   -h, --help              Show this help
#
# Every setting can also be put in deploy.env (see deploy.env.example for the full list).
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
CLI_LINK_PROJECTS=""
CLI_REGION=""
CLI_ADMINS=""
CLI_VIEWERS=""
CLI_ORG_NAME=""
CLI_ACCESS_MODE=""
CLI_SERVICE_NAME=""
CLI_BILLING_ACCOUNT=""
CLI_READ_ONLY=""
CLI_DATA_ACCESS_LOGS=""
CLI_PREPARE_LINKED=""
CLI_SCAN_ALL=""
CLI_ENFORCEMENT_SCHEDULE=""
CLI_REALTIME_ENFORCEMENT=""
CLI_LOG_PROMPTS=""
CLI_DATASET=""
CLI_DATASET_LOCATION=""
CLI_GE_LOCATIONS=""
CLI_ENFORCEMENT_BACKEND=""
CLI_DENY_ATTACHMENT=""
CLI_ENFORCEMENT_JOB_REGION=""
CLI_GRANT_ORG_ROLES=""
CLI_MAINTENANCE_SCHEDULE=""
CLI_VPC_NETWORK=""
CLI_VPC_SUBNET=""
CLI_PREFLIGHT_STRICT=""

# Prints the comment block at the top of this file (every line after the shebang up to `set -euo pipefail`).
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit "${1:-0}"; }
need_arg() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 needs a value"; }

normalize_cli_args "$@"
set -- ${NORMALIZED_ARGS[@]+"${NORMALIZED_ARGS[@]}"}
ORIG_ARGS=("$@")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) need_arg "$@"; CONFIG="$2"; CONFIG_EXPLICIT=1; shift 2 ;;
    --project) need_arg "$@"; CLI_PROJECT="$2"; shift 2 ;;
    --projects) need_arg "$@"; CLI_PROJECTS="$2"; shift 2 ;;
    --link-projects|--scan-projects) need_arg "$@"; CLI_LINK_PROJECTS="$2"; shift 2 ;;
    --region) need_arg "$@"; CLI_REGION="$2"; shift 2 ;;
    --admins|--admin) need_arg "$@"; CLI_ADMINS="$2"; shift 2 ;;
    --viewers|--viewer|--allowed-users) need_arg "$@"; CLI_VIEWERS="$2"; shift 2 ;;
    --org-name) need_arg "$@"; CLI_ORG_NAME="$2"; shift 2 ;;
    --access-mode) need_arg "$@"; CLI_ACCESS_MODE="$2"; shift 2 ;;
    --service-name) need_arg "$@"; CLI_SERVICE_NAME="$2"; shift 2 ;;
    --billing-account) need_arg "$@"; CLI_BILLING_ACCOUNT="$2"; shift 2 ;;
    --read-only) CLI_READ_ONLY="1"; shift ;;
    --no-read-only) CLI_READ_ONLY="0"; shift ;;
    --enforcement-schedule) need_arg "$@"; CLI_ENFORCEMENT_SCHEDULE="$2"; shift 2 ;;
    --no-enforcement-schedule) CLI_ENFORCEMENT_SCHEDULE="off"; shift ;;
    --realtime-enforcement) CLI_REALTIME_ENFORCEMENT="true"; shift ;;
    --no-realtime-enforcement) CLI_REALTIME_ENFORCEMENT="false"; shift ;;
    --maintenance-schedule) need_arg "$@"; CLI_MAINTENANCE_SCHEDULE="$2"; shift 2 ;;
    --no-maintenance-schedule) CLI_MAINTENANCE_SCHEDULE="off"; shift ;;
    --enforcement-backend) need_arg "$@"; CLI_ENFORCEMENT_BACKEND="$2"; shift 2 ;;
    --deny-attachment) need_arg "$@"; CLI_DENY_ATTACHMENT="$2"; shift 2 ;;
    --enforcement-job-region) need_arg "$@"; CLI_ENFORCEMENT_JOB_REGION="$2"; shift 2 ;;
    --grant-org-roles) CLI_GRANT_ORG_ROLES="true"; shift ;;
    --no-grant-org-roles) CLI_GRANT_ORG_ROLES="false"; shift ;;
    --data-access-logs) CLI_DATA_ACCESS_LOGS="true"; shift ;;
    --no-data-access-logs) CLI_DATA_ACCESS_LOGS="false"; shift ;;
    --log-prompts) CLI_LOG_PROMPTS="true"; shift ;;
    --no-log-prompts) CLI_LOG_PROMPTS="false"; shift ;;
    --dataset) need_arg "$@"; CLI_DATASET="$2"; shift 2 ;;
    --dataset-location) need_arg "$@"; CLI_DATASET_LOCATION="$2"; shift 2 ;;
    --ge-locations) need_arg "$@"; CLI_GE_LOCATIONS="$2"; shift 2 ;;
    --prepare-linked-projects) CLI_PREPARE_LINKED="true"; shift ;;
    --no-prepare-linked-projects) CLI_PREPARE_LINKED="false"; shift ;;
    --vpc-network) need_arg "$@"; CLI_VPC_NETWORK="$2"; shift 2 ;;
    --vpc-subnet) need_arg "$@"; CLI_VPC_SUBNET="$2"; shift 2 ;;
    --skip-preflight-blockers) CLI_PREFLIGHT_STRICT="false"; shift ;;
    --auto-discover-projects) CLI_SCAN_ALL="true"; shift ;;
    --no-auto-discover-projects) CLI_SCAN_ALL="false"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -y|--yes) shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
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
SCAN_PROJECTS="${SCAN_PROJECTS:-${LINKED_PROJECTS:-${WORKSPACE_PROJECTS:-}}}"
SCAN_ALL_PROJECTS="${SCAN_ALL_PROJECTS:-${AUTO_DISCOVER_PROJECTS:-}}"

# Apply CLI overrides on top of config file / environment values.
[[ -n "$CLI_PROJECT" ]] && PROJECT_ID="$CLI_PROJECT"
[[ -n "$CLI_LINK_PROJECTS" ]] && SCAN_PROJECTS="$CLI_LINK_PROJECTS"
[[ -n "$CLI_REGION" ]] && REGION="$CLI_REGION"
[[ -n "$CLI_ADMINS" ]] && ADMIN_EMAILS="$CLI_ADMINS"
[[ -n "$CLI_VIEWERS" ]] && ALLOWED_USERS="$CLI_VIEWERS"
[[ -n "$CLI_ORG_NAME" ]] && ORGANIZATION_NAME="$CLI_ORG_NAME"
[[ -n "$CLI_ACCESS_MODE" ]] && ACCESS_MODE="$CLI_ACCESS_MODE"
[[ -n "$CLI_SERVICE_NAME" ]] && SERVICE_NAME="$CLI_SERVICE_NAME"
[[ -n "$CLI_BILLING_ACCOUNT" ]] && BILLING_ACCOUNT_ID="$CLI_BILLING_ACCOUNT"
[[ "$CLI_READ_ONLY" == "1" ]] && ENABLE_GOVERNANCE_ACTIONS="false"
[[ "$CLI_READ_ONLY" == "0" ]] && ENABLE_GOVERNANCE_ACTIONS="true"
[[ -n "$CLI_DATA_ACCESS_LOGS" ]] && ENABLE_DATA_ACCESS_LOGS="$CLI_DATA_ACCESS_LOGS"
[[ -n "$CLI_LOG_PROMPTS" ]] && GE_PROMPT_LOGGING="$CLI_LOG_PROMPTS"
[[ -n "$CLI_PREPARE_LINKED" ]] && PREPARE_LINKED_PROJECTS="$CLI_PREPARE_LINKED"
[[ -n "$CLI_SCAN_ALL" ]] && SCAN_ALL_PROJECTS="$CLI_SCAN_ALL"
[[ -n "$CLI_ENFORCEMENT_SCHEDULE" ]] && ENFORCEMENT_SCHEDULE="$CLI_ENFORCEMENT_SCHEDULE"
[[ -n "$CLI_REALTIME_ENFORCEMENT" ]] && REALTIME_ENFORCEMENT="$CLI_REALTIME_ENFORCEMENT"
[[ -n "$CLI_ENFORCEMENT_BACKEND" ]] && ENFORCEMENT_BACKEND="$CLI_ENFORCEMENT_BACKEND"
[[ -n "$CLI_DENY_ATTACHMENT" ]] && DENY_ATTACHMENT="$CLI_DENY_ATTACHMENT"
[[ -n "$CLI_ENFORCEMENT_JOB_REGION" ]] && ENFORCEMENT_JOB_REGION="$CLI_ENFORCEMENT_JOB_REGION"
[[ -n "$CLI_GRANT_ORG_ROLES" ]] && GRANT_ORG_ROLES="$CLI_GRANT_ORG_ROLES"
# Flags the one-command installer forwards (install.sh); the installer's env snapshot cannot override them.
[[ -n "$CLI_LOG_PROMPTS" ]] && GE_PROMPT_LOGGING="$CLI_LOG_PROMPTS"
[[ -n "$CLI_DATASET" ]] && AUDIT_DATASET="$CLI_DATASET"
[[ -n "$CLI_DATASET_LOCATION" ]] && DATASET_LOCATION="$CLI_DATASET_LOCATION"
[[ -n "$CLI_GE_LOCATIONS" ]] && DISCOVERY_ENGINE_LOCATIONS="$CLI_GE_LOCATIONS"
[[ -n "$CLI_MAINTENANCE_SCHEDULE" ]] && MAINTENANCE_SCHEDULE="$CLI_MAINTENANCE_SCHEDULE"
[[ -n "$CLI_VPC_NETWORK" ]] && VPC_NETWORK="$CLI_VPC_NETWORK"
[[ -n "$CLI_VPC_SUBNET" ]] && VPC_SUBNET="$CLI_VPC_SUBNET"
[[ -n "$CLI_PREFLIGHT_STRICT" ]] && PREFLIGHT_STRICT="$CLI_PREFLIGHT_STRICT"
# "none" is an explicit empty list (--link-projects none, --viewers none): it clears what deploy.env or the installer's
# snapshot holds, where an empty flag value would be refused as a likely mistake.
is_none() { [[ "$(trim_ws "${1:-}" | tr '[:upper:]' '[:lower:]')" == "none" ]]; }
is_none "${SCAN_PROJECTS:-}" && SCAN_PROJECTS=""
is_none "${ALLOWED_USERS:-}" && ALLOWED_USERS=""

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
DATASET_LOCATION_EXPLICIT=0
[[ -n "$(trim_ws "${DATASET_LOCATION:-}")" ]] && DATASET_LOCATION_EXPLICIT=1
DATASET_LOCATION="$(trim_ws "${DATASET_LOCATION:-$REGION}")"
PREPARE_LINKED_PROJECTS="${PREPARE_LINKED_PROJECTS:-true}"
# Full coverage by default: enable Data Access audit logs, Gemini Enterprise observability + prompt logging,
# and governance IAM controls out of the box so all 13 modules and health checks work immediately.
ENABLE_DATA_ACCESS_LOGS="${ENABLE_DATA_ACCESS_LOGS:-true}"
ENABLE_GE_OBSERVABILITY="${ENABLE_GE_OBSERVABILITY:-true}"
GE_PROMPT_LOGGING="${GE_PROMPT_LOGGING:-true}"
BILLING_PROJECT_ID="$(normalize_project_id "${BILLING_PROJECT_ID:-}")"
BILLING_DATASET="$(trim_ws "${BILLING_DATASET:-}")"
BILLING_TABLE="$(trim_ws "${BILLING_TABLE:-}")"
RAW_BILLING_ACCOUNT_ID="$(trim_ws "${BILLING_ACCOUNT_ID:-}")"
BILLING_ACCOUNT_ID="$(_sanitize_billing_account_id "$RAW_BILLING_ACCOUNT_ID")"
if [[ -n "$RAW_BILLING_ACCOUNT_ID" && -z "$BILLING_ACCOUNT_ID" ]]; then
  die "invalid BILLING_ACCOUNT_ID '${RAW_BILLING_ACCOUNT_ID}' (expected XXXXXX-XXXXXX-XXXXXX or billingAccounts/XXXXXX-XXXXXX-XXXXXX)"
fi
DISCOVERY_ENGINE_LOCATIONS="${DISCOVERY_ENGINE_LOCATIONS:-}"
AGENT_ENGINE_REGIONS="${AGENT_ENGINE_REGIONS:-}"
ENABLE_GOVERNANCE_ACTIONS="${ENABLE_GOVERNANCE_ACTIONS:-true}"
# Antigravity cap enforcement runs. They restore suspended users when a settlement period resets (in every
# enforcement mode) and, in automatic mode only, suspend users over their cap. On by default so nobody stays
# suspended past a reset; "off" (or --no-enforcement-schedule) leaves it to "Run enforcement now" in the dashboard.
# An empty value (older deploy.env files had ENFORCEMENT_SCHEDULE="") keeps the default.
ENFORCEMENT_SCHEDULE_EXPLICIT=0
[[ -n "$(trim_ws "${ENFORCEMENT_SCHEDULE:-}")" ]] && ENFORCEMENT_SCHEDULE_EXPLICIT=1
ENFORCEMENT_SCHEDULE="$(trim_ws "${ENFORCEMENT_SCHEDULE:-}")"
[[ -n "$ENFORCEMENT_SCHEDULE" ]] || ENFORCEMENT_SCHEDULE="*/5 * * * *"
case "$(printf '%s' "$ENFORCEMENT_SCHEDULE" | tr '[:upper:]' '[:lower:]')" in off|none) ENFORCEMENT_SCHEDULE="" ;; esac
# Real-time Antigravity cap enforcement: routes businessaicode.googleapis.com/inference_response to a Pub/Sub push
# subscription posting to /api/v1/governance/antigravity/usage-event so automatic mode triggers suspension right away
# instead of waiting for the next scheduled run. The schedule stays the safety net for period resets.
REALTIME_ENFORCEMENT_EXPLICIT=0
[[ -n "$(trim_ws "${REALTIME_ENFORCEMENT:-}")" ]] && REALTIME_ENFORCEMENT_EXPLICIT=1
REALTIME_ENFORCEMENT="$(trim_ws "${REALTIME_ENFORCEMENT:-true}" | tr '[:upper:]' '[:lower:]')"
# How suspensions are applied (governance.antigravity.enforcement_backend, README 8.9). iam_allow_swap: the web service
# swaps direct bindings in the primary project's allow policy. iam_deny: suspended people are listed in an IAM deny
# policy (deny is evaluated before allow, so group, inherited and workforce access stop too), applied by the Cloud Run
# job <service>-enforcer whose service account alone gets Deny Admin on the organization (GRANT_ORG_ROLES=true grants
# it, otherwise the commands are printed); ENFORCEMENT_SCHEDULE then runs that job. notify_only: plans and
# notifications only. The backend can also be switched in the dashboard (Settings > Governance policy).
ENFORCEMENT_BACKEND="$(trim_ws "${ENFORCEMENT_BACKEND:-iam_allow_swap}" | tr '[:upper:]' '[:lower:]')"
DENY_ATTACHMENT="$(trim_ws "${DENY_ATTACHMENT:-projects}" | tr '[:upper:]' '[:lower:]')"
ENFORCEMENT_JOB_REGION="$(trim_ws "${ENFORCEMENT_JOB_REGION:-}")"
GRANT_ORG_ROLES="$(trim_ws "${GRANT_ORG_ROLES:-false}" | tr '[:upper:]' '[:lower:]')"
# Scheduler tick for scheduled work: the daily official price and exchange-rate check (at pricing.sync_time, default
# 00:00 in the workspace timezone; a setting, not a cron here), the telemetry route budget guard and the hand-over of
# marker roles when the enforcement method changes. "off" (or "none") disables it.
MAINTENANCE_SCHEDULE="$(trim_ws "${MAINTENANCE_SCHEDULE:-*/15 * * * *}")"
# Earlier releases had a fixed-time price job (PRICING_SYNC_SCHEDULE); "off"/"none" there still turns scheduling off
# unless --maintenance-schedule is given.
if [[ -z "$CLI_MAINTENANCE_SCHEDULE" ]]; then
  case "$(trim_ws "${PRICING_SYNC_SCHEDULE:-}")" in off|none) MAINTENANCE_SCHEDULE="off" ;; esac
fi
case "$(printf '%s' "$MAINTENANCE_SCHEDULE" | tr '[:upper:]' '[:lower:]')" in off|none) MAINTENANCE_SCHEDULE="off" ;; esac
PRICING_SYNC_TIME="$(trim_ws "${PRICING_SYNC_TIME:-}")"
PRICING_CURRENCY="$(trim_ws "${PRICING_CURRENCY:-}" | tr '[:lower:]' '[:upper:]')"
TELEMETRY_AI_MODEL="$(trim_ws "${TELEMETRY_AI_MODEL:-}")"
TELEMETRY_DATASET="$(trim_ws "${TELEMETRY_DATASET:-}")"
AI_DEFAULT_MODEL="${AI_DEFAULT_MODEL:-}"
AI_LOCATION="${AI_LOCATION:-}"
IMAGE="$(trim_ws "${IMAGE:-}")"
AR_REPOSITORY="$(trim_ws "${AR_REPOSITORY:-ai-control-plane}")"
SERVICE_ACCOUNT_ID="$(trim_ws "${SERVICE_ACCOUNT_ID:-${SERVICE_NAME:0:27}-sa}")"
SETTINGS_BUCKET="$(trim_ws "${SETTINGS_BUCKET:-}")"
SESSION_SECRET_NAME="$(trim_ws "${SESSION_SECRET_NAME:-${SERVICE_NAME}-session-secret}")"
OAUTH_SECRET_NAME="$(trim_ws "${OAUTH_SECRET_NAME:-${SERVICE_NAME}-oauth-client-secret}")"
OAUTH_CONNECT_SECRET_NAME="$(trim_ws "${OAUTH_CONNECT_SECRET_NAME:-${SERVICE_NAME}-oauth-connected-secret}")"
ADMIN_SECRET_NAME="$(trim_ws "${ADMIN_SECRET_NAME:-${SERVICE_NAME}-admin-password}")"
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

[[ "$SERVICE_NAME" =~ ^[a-z][a-z0-9-]{1,40}[a-z0-9]$ ]] || die "SERVICE_NAME must be 3-42 lowercase letters, digits and hyphens, starting with a letter and ending with a letter or digit"
[[ "$SERVICE_ACCOUNT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "SERVICE_ACCOUNT_ID '${SERVICE_ACCOUNT_ID}' must be 6-30 lowercase letters, digits and hyphens, starting with a letter and ending with a letter or digit"
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
[[ -z "$PRICING_SYNC_TIME" || "$PRICING_SYNC_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] \
  || die "PRICING_SYNC_TIME must be a time of day such as 00:00"
[[ -z "$PRICING_CURRENCY" || "$PRICING_CURRENCY" =~ ^[A-Z]{3}$ ]] || die "PRICING_CURRENCY must be an ISO 4217 code such as KRW"
[[ -z "$TELEMETRY_DATASET" || "$TELEMETRY_DATASET" =~ ^[A-Za-z0-9_]{1,1024}$ ]] \
  || die "TELEMETRY_DATASET must be a BigQuery dataset id (letters, digits and _)"
if [[ "$MAINTENANCE_SCHEDULE" != "off" ]]; then
  read -r -a _cron_fields <<<"$MAINTENANCE_SCHEDULE"
  [[ "${#_cron_fields[@]}" -eq 5 ]] || die "MAINTENANCE_SCHEDULE must be a 5-field cron expression (for example */15 * * * *) or off"
fi
case "$ENFORCEMENT_BACKEND" in
  iam_allow_swap|iam_deny|notify_only) ;;
  *) die "ENFORCEMENT_BACKEND must be iam_allow_swap, iam_deny or notify_only" ;;
esac
case "$DENY_ATTACHMENT" in
  projects|scope) ;;
  *) die "DENY_ATTACHMENT must be projects or scope" ;;
esac
[[ -n "$ENFORCEMENT_JOB_REGION" ]] || ENFORCEMENT_JOB_REGION="$REGION"
[[ "$ENFORCEMENT_JOB_REGION" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] \
  || die "invalid ENFORCEMENT_JOB_REGION '${ENFORCEMENT_JOB_REGION}' (for example 'asia-northeast3')"
if [[ -n "$ENFORCEMENT_SCHEDULE" ]]; then
  read -r -a _cron_fields <<<"$ENFORCEMENT_SCHEDULE"
  [[ "${#_cron_fields[@]}" -eq 5 ]] || die "ENFORCEMENT_SCHEDULE must be a 5-field cron expression (for example */5 * * * *) or off"
  if [[ "$ENFORCEMENT_BACKEND" == "iam_deny" ]]; then
    # Cloud Scheduler runs the enforcement job through the Cloud Run Admin API: the call never passes IAP and the
    # web service account needs no write role, so every access mode and read-only installs keep the schedule.
    :
  elif [[ "$ENABLE_GOVERNANCE_ACTIONS" != "true" && "$ENFORCEMENT_BACKEND" != "notify_only" ]]; then
    [[ "$ENFORCEMENT_SCHEDULE_EXPLICIT" == "1" ]] && die "ENFORCEMENT_SCHEDULE needs ENABLE_GOVERNANCE_ACTIONS=true"
    ENFORCEMENT_SCHEDULE=""  # read-only install: nothing is ever suspended, so there is nothing to restore
  elif [[ "$ACCESS_MODE" == "iap" ]]; then
    [[ "$ENFORCEMENT_SCHEDULE_EXPLICIT" == "1" ]] && die "ENFORCEMENT_SCHEDULE needs ACCESS_MODE google, google_oauth or password (IAP blocks the scheduler's OIDC token)"
    warn "IAP access mode: no Antigravity enforcement schedule (IAP blocks the scheduler's OIDC token); run enforcement from the dashboard after each period reset"
    ENFORCEMENT_SCHEDULE=""
  fi
fi
PREFLIGHT_STRICT="$(trim_ws "${PREFLIGHT_STRICT:-true}" | tr '[:upper:]' '[:lower:]')"
ENABLE_LINKED_TELEMETRY_ROUTING="$(trim_ws "${ENABLE_LINKED_TELEMETRY_ROUTING:-true}" | tr '[:upper:]' '[:lower:]')"
for flag in CREATE_LOG_SINK ENABLE_DATA_ACCESS_LOGS ENABLE_GE_OBSERVABILITY GE_PROMPT_LOGGING ENABLE_GOVERNANCE_ACTIONS PREPARE_LINKED_PROJECTS GRANT_ORG_ROLES REALTIME_ENFORCEMENT PREFLIGHT_STRICT ENABLE_LINKED_TELEMETRY_ROUTING; do
  [[ "${!flag}" == "true" || "${!flag}" == "false" ]] || die "$flag must be true or false"
done
if [[ "$REALTIME_ENFORCEMENT" == "true" ]]; then
  if [[ "$ENFORCEMENT_BACKEND" == "notify_only" ]] \
      || [[ "$ENFORCEMENT_BACKEND" != "iam_deny" && "$ENABLE_GOVERNANCE_ACTIONS" != "true" ]]; then
    REALTIME_ENFORCEMENT="false"
  elif [[ "$ACCESS_MODE" == "iap" ]]; then
    [[ "$REALTIME_ENFORCEMENT_EXPLICIT" == "1" ]] \
      && warn "IAP access mode: real-time Pub/Sub push is off (IAP blocks Pub/Sub OIDC tokens); scheduled runs still apply"
    REALTIME_ENFORCEMENT="false"
  fi
fi
if [[ -n "$SCAN_ALL_PROJECTS" ]]; then
  SCAN_ALL_PROJECTS="$(printf '%s' "$SCAN_ALL_PROJECTS" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  [[ "$SCAN_ALL_PROJECTS" == "true" || "$SCAN_ALL_PROJECTS" == "false" ]] || die "SCAN_ALL_PROJECTS must be true or false"
fi
DISCOVERY_ENGINE_LOCATIONS="$(printf '%s' "$DISCOVERY_ENGINE_LOCATIONS" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
[[ -z "$DISCOVERY_ENGINE_LOCATIONS" || "$DISCOVERY_ENGINE_LOCATIONS" =~ ^[a-z0-9-]+(,[a-z0-9-]+)*$ ]] \
  || die "DISCOVERY_ENGINE_LOCATIONS must be a comma-separated list such as global,us (or empty to detect)"

# Keep this list in sync with MODULE_IDS in src/controlplane/config.py (checked by the unit tests).
MODULE_IDS=(overview gemini_enterprise notebooklm model_usage agent_gateway agent_registry agent_deployments
  model_armor users antigravity finops chargeback telemetry analyst)
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
  PROJECT_NUMBER="$(printf '%s\n' "$PROJECT_NUMBER" | tail -n 1 | tr -d '[:space:]')"
  [[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] && break
  [[ $attempt -lt 3 ]] || die "cannot read project ${PROJECT_ID} with ${ACCOUNT}: $(tail -n 3 "$GCLOUD_ERR")
  [한국어 안내] 현재 로그인된 계정(${ACCOUNT})이 '${PROJECT_ID}' 프로젝트를 조회할 수 없습니다.
   - 프로젝트 ID 오타 여부를 확인하거나, 올바른 계정으로 로그인하세요: gcloud auth login
   - 또는 프로젝트 관리자에게 권한 부여를 요청하세요:
     gcloud projects add-iam-policy-binding ${PROJECT_ID} --member=\"${CALLER_MEMBER}\" --role=\"roles/owner\""
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
# iam_deny: the Cloud Run job that applies suspensions, its own service account and the scheduler job that runs it.
ENFORCER_SA_ID="${SERVICE_NAME:0:21}-enforcer"
ENFORCER_SA="${ENFORCER_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
ENFORCER_JOB="${SERVICE_NAME}-enforcer"
ENFORCER_SCHED_JOB="${SERVICE_NAME}-enforcer"
ENFORCEMENT_JOB_NAME="projects/${PROJECT_ID}/locations/${ENFORCEMENT_JOB_REGION}/jobs/${ENFORCER_JOB}"
ENFORCER_RUN_URI="https://run.googleapis.com/v2/${ENFORCEMENT_JOB_NAME}:run"
# Real-time Antigravity usage events: Log Router sink -> Pub/Sub topic -> push subscription -> dashboard endpoint.
USAGE_EVENTS_TOPIC="${SERVICE_NAME}-agy-usage"
USAGE_EVENTS_SUB_ID="${SERVICE_NAME}-agy-usage-push"
USAGE_EVENTS_SUB_NAME="projects/${PROJECT_ID}/subscriptions/${USAGE_EVENTS_SUB_ID}"
USAGE_EVENTS_SINK="${SERVICE_NAME}-agy-usage"
USAGE_EVENT_URL="${BASE_URL}/api/v1/governance/antigravity/usage-event"
PUBSUB_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

NEED_BUILD=0; [[ -z "$IMAGE" ]] && NEED_BUILD=1
NEED_SINK=0; [[ "$CREATE_LOG_SINK" == "true" ]] && NEED_SINK=1
NEED_IAP=0; [[ "$ACCESS_MODE" == "google" || "$ACCESS_MODE" == "iap" ]] && NEED_IAP=1
NEED_SCHED=0; [[ -n "$ENFORCEMENT_SCHEDULE" ]] && NEED_SCHED=1
# The maintenance tick is a Cloud Scheduler job as well (not created in IAP mode, where the scheduler cannot call in).
[[ "$MAINTENANCE_SCHEDULE" != "off" && "$ACCESS_MODE" != "iap" ]] && NEED_SCHED=1
NEED_AUDIT=0; [[ "$ENABLE_DATA_ACCESS_LOGS" == "true" ]] && NEED_AUDIT=1
NEED_REALTIME=0; [[ "$REALTIME_ENFORCEMENT" == "true" && "$ACCESS_MODE" != "iap" ]] && NEED_REALTIME=1

log "Deploying ${SERVICE_NAME} to ${PROJECT_ID} (${REGION}) as ${ACCOUNT}"
info "access mode: ${ACCESS_MODE}    dashboard URL: ${BASE_URL}"
[[ "$DRY_RUN" == "1" ]] && info "dry run: commands that change anything are printed, not executed"

# Verify caller IAM permissions upfront (and auto-grant missing deployment roles when caller has IAM Admin).
preflight_caller_permissions deploy "$PROJECT_ID" "$ACCOUNT" "$NEED_BUILD" "$NEED_SINK" "$NEED_IAP" "$NEED_SCHED" "$NEED_AUDIT" "$NEED_REALTIME"

# Verify Cloud Billing is linked (auto-links when accessible) and detect account ID.
DETECTED_BA="$(ensure_project_billing "$PROJECT_ID" "$BILLING_ACCOUNT_ID")"
if [[ -z "$BILLING_ACCOUNT_ID" && -n "$DETECTED_BA" ]]; then
  BILLING_ACCOUNT_ID="$DETECTED_BA"
  info "detected Cloud Billing account: ${BILLING_ACCOUNT_ID}"
fi

# Organization policies, VPC Service Controls and restricted services: adapt automatically or stop before creating
# anything, with remediation (customer projects often enforce these; see README 14).
VPC_NETWORK="$(trim_ws "${VPC_NETWORK:-}")"; VPC_SUBNET="$(trim_ws "${VPC_SUBNET:-}")"
[[ -z "$VPC_NETWORK" || -n "$VPC_SUBNET" ]] || die "--vpc-network needs --vpc-subnet"
preflight_org_policies "$PROJECT_ID" "$REGION" "$DATASET_LOCATION"
if [[ "$ORGPOL_DRS" == "1" ]]; then
  BRIDGE_MEMBERS="$(printf '%s' "$BRIDGE_MEMBERS" | tr ',' '\n' | grep -viE '^ *(allusers|allauthenticatedusers) *$' | paste -sd, - || true)"
fi
if [[ -n "$ORGPOL_BINAUTHZ" ]]; then
  run gcloud services enable binaryauthorization.googleapis.com --project "$PROJECT_ID" --quiet >/dev/null 2>&1 \
    || warn "could not enable binaryauthorization.googleapis.com (Cloud Run deploys may be rejected)"
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
if [[ "$REALTIME_ENFORCEMENT" == "true" ]]; then
  run gcloud services enable pubsub.googleapis.com --project "$PROJECT_ID" --quiet >/dev/null 2>&1 \
    || warn "could not enable pubsub.googleapis.com (real-time enforcement push will be skipped)"
  SID_APIS+=(pubsub.googleapis.com)
fi
ensure_service_identities "$PROJECT_ID" "$PROJECT_NUMBER" "${SID_APIS[@]}"

# ------------------------------------------------------------------------------- 2. settings bucket
log "2/8 Settings bucket gs://${SETTINGS_BUCKET}"
if ! gcloud storage buckets describe "gs://${SETTINGS_BUCKET}" --project "$PROJECT_ID" >/dev/null 2>&1; then
  if [[ "$DRY_RUN" == "1" ]]; then
    run gcloud storage buckets create "gs://${SETTINGS_BUCKET}" --project "$PROJECT_ID" --location "$REGION" \
      --uniform-bucket-level-access --public-access-prevention
  elif ! gcloud storage buckets create "gs://${SETTINGS_BUCKET}" --project "$PROJECT_ID" --location "$REGION" \
      --uniform-bucket-level-access --public-access-prevention 2>"$GCLOUD_ERR"; then
    if grep -Eiq 'not available|already exists|409|CONFLICT' "$GCLOUD_ERR"; then
      die "settings bucket name gs://${SETTINGS_BUCKET} is already taken globally in Cloud Storage: $(tail -n 2 "$GCLOUD_ERR")
  Set a unique SETTINGS_BUCKET (for example SETTINGS_BUCKET=\"${PROJECT_ID}-acp-settings-2\") in deploy.${PROJECT_ID}.env or deploy.env and re-run."
    fi
    die "could not create settings bucket gs://${SETTINGS_BUCKET}: $(tail -n 3 "$GCLOUD_ERR")"
  fi
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
if [[ "$ENFORCEMENT_BACKEND" == "iam_deny" ]]; then
  ensure_sa "$ENFORCER_SA_ID" "$ENFORCER_SA" "AI Control Plane enforcement job" \
    "Identity of the ${ENFORCER_JOB} Cloud Run job: applies Antigravity cap suspensions with an IAM deny policy"
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
  _bind_project_role_retry "$1" "$2" "$3" \
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
  roles/networkservices.viewer
  roles/networksecurity.viewer
)
GOVERNANCE_ROLES=(roles/resourcemanager.projectIamAdmin roles/iam.roleAdmin)
for role in "${RUNTIME_ROLES[@]}"; do grant_member "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "$role"; done
for role in "${OPTIONAL_RUNTIME_ROLES[@]}"; do grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "$role"; done
# Google Cloud's predefined roles/discoveryengine.viewer omits userStores.listUserLicenses; grant a read-only custom role (or fallback).
LICENSE_READER_ROLE_ID="CustomAiControlPlaneLicenseReader"
LICENSE_READER_ROLE="projects/${PROJECT_ID}/roles/${LICENSE_READER_ROLE_ID}"
LICENSE_READER_PERMS="discoveryengine.licenseConfigs.get,discoveryengine.licenseConfigs.list,discoveryengine.userStores.get,discoveryengine.userStores.listUserLicenses"
if [[ "$DRY_RUN" == "1" ]]; then
  info "[dry-run] custom role ${LICENSE_READER_ROLE} (${LICENSE_READER_PERMS}) -> ${SA_EMAIL}"
else
  gcloud iam roles undelete "$LICENSE_READER_ROLE_ID" --project "$PROJECT_ID" --quiet >/dev/null 2>&1 || true
  _lr_deleted=""
  if _lr_deleted="$(gcloud iam roles describe "$LICENSE_READER_ROLE_ID" --project "$PROJECT_ID" --format='value(deleted)' 2>/dev/null)"; then
    if [[ "$(printf '%s' "$_lr_deleted" | tr '[:upper:]' '[:lower:]')" == "true" ]]; then
      warn "custom role ${LICENSE_READER_ROLE} is in its 30-day deletion window; falling back to roles/discoveryengine.editor"
      grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "roles/discoveryengine.editor"
    else
      gcloud iam roles update "$LICENSE_READER_ROLE_ID" --project "$PROJECT_ID" --permissions "$LICENSE_READER_PERMS" --quiet >/dev/null 2>&1 || true
      _bind_project_role_retry "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "$LICENSE_READER_ROLE" \
        && info "granted ${LICENSE_READER_ROLE} to serviceAccount:${SA_EMAIL} on ${PROJECT_ID}" \
        || warn "could not grant ${LICENSE_READER_ROLE} to ${SA_EMAIL}; Gemini Enterprise license seats may stay hidden"
    fi
  elif gcloud iam roles create "$LICENSE_READER_ROLE_ID" --project "$PROJECT_ID" \
      --title "AI Control Plane License Reader" \
      --description "Read-only Gemini Enterprise license config and user seat reader" \
      --permissions "$LICENSE_READER_PERMS" --stage GA --quiet >/dev/null 2>&1; then
    _bind_project_role_retry "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "$LICENSE_READER_ROLE" \
      && info "granted ${LICENSE_READER_ROLE} to serviceAccount:${SA_EMAIL} on ${PROJECT_ID}" \
      || warn "could not grant ${LICENSE_READER_ROLE} to ${SA_EMAIL}; Gemini Enterprise license seats may stay hidden"
  else
    grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "roles/discoveryengine.editor"
  fi
fi
if [[ "$ENABLE_GOVERNANCE_ACTIONS" == "true" ]]; then
  for role in "${GOVERNANCE_ROLES[@]}"; do grant_member "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "$role"; done
  # Marker roles for per-user Antigravity caps (Gemini Enterprise User without businessaicode.*), created now so
  # suspensions work right after installation without the "Create marker roles" step.
  ensure_antigravity_marker_roles "$PROJECT_ID"
  # Gemini Enterprise > Feature management: change the feature switches of Gemini Enterprise apps (least privilege).
  grant_ge_feature_manager "$PROJECT_ID" "$SA_EMAIL"
  # Least-privilege custom role for managing Agent Gateway resources, IAP authorization extensions and policies.
  GATEWAY_MANAGER_ROLE_ID="CustomAiControlPlaneGatewayManager"
  GATEWAY_MANAGER_ROLE="projects/${PROJECT_ID}/roles/${GATEWAY_MANAGER_ROLE_ID}"
  GATEWAY_MANAGER_PERMS="networkservices.agentGateways.create,networkservices.agentGateways.update,networkservices.agentGateways.delete,networkservices.agentGateways.get,networkservices.agentGateways.list,networkservices.agentGateways.use,networkservices.authzExtensions.create,networkservices.authzExtensions.update,networkservices.authzExtensions.delete,networkservices.authzExtensions.get,networkservices.authzExtensions.list,networkservices.authzExtensions.use,networkservices.operations.get,networksecurity.authzPolicies.create,networksecurity.authzPolicies.delete,networksecurity.authzPolicies.get,networksecurity.authzPolicies.list,networksecurity.operations.get"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] custom role ${GATEWAY_MANAGER_ROLE} -> ${SA_EMAIL}"
  else
    gcloud iam roles undelete "$GATEWAY_MANAGER_ROLE_ID" --project "$PROJECT_ID" --quiet >/dev/null 2>&1 || true
    _gw_deleted=""
    if _gw_deleted="$(gcloud iam roles describe "$GATEWAY_MANAGER_ROLE_ID" --project "$PROJECT_ID" --format='value(deleted)' 2>/dev/null)"; then
      if [[ "$(printf '%s' "$_gw_deleted" | tr '[:upper:]' '[:lower:]')" == "true" ]]; then
        warn "custom role ${GATEWAY_MANAGER_ROLE} is in its 30-day deletion window; falling back to roles/networkservices.editor + roles/networksecurity.editor"
        grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "roles/networkservices.editor"
        grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "roles/networksecurity.editor"
      else
        gcloud iam roles update "$GATEWAY_MANAGER_ROLE_ID" --project "$PROJECT_ID" --permissions "$GATEWAY_MANAGER_PERMS" --quiet >/dev/null 2>&1 || true
        _bind_project_role_retry "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "$GATEWAY_MANAGER_ROLE" \
          && info "granted ${GATEWAY_MANAGER_ROLE} to serviceAccount:${SA_EMAIL} on ${PROJECT_ID}" \
          || warn "could not grant ${GATEWAY_MANAGER_ROLE} to ${SA_EMAIL}; Agent Gateway management may be read-only"
      fi
    elif gcloud iam roles create "$GATEWAY_MANAGER_ROLE_ID" --project "$PROJECT_ID" \
        --title "AI Control Plane Agent Gateway Manager" \
        --description "Manage Agent Gateway resources, authorization extensions and policies" \
        --permissions "$GATEWAY_MANAGER_PERMS" --stage GA --quiet >/dev/null 2>&1; then
      _bind_project_role_retry "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "$GATEWAY_MANAGER_ROLE" \
        && info "granted ${GATEWAY_MANAGER_ROLE} to serviceAccount:${SA_EMAIL} on ${PROJECT_ID}" \
        || warn "could not grant ${GATEWAY_MANAGER_ROLE} to ${SA_EMAIL}; Agent Gateway management may be read-only"
    else
      grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "roles/networkservices.editor"
      grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "roles/networksecurity.editor"
    fi
  fi
else
  info "governance write roles skipped (ENABLE_GOVERNANCE_ACTIONS=false): dashboards and simulations only"
fi
# Ensure Cloud Run service agent binding exists on brand-new projects.
grant_optional_role "$PROJECT_ID" "serviceAccount:${RUN_SERVICE_AGENT}" "roles/run.serviceAgent"
run retry gcloud storage buckets add-iam-policy-binding "gs://${SETTINGS_BUCKET}" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/storage.objectAdmin >/dev/null
# iam_deny enforcement job: the read access the runtime uses for usage (inference logs, BigQuery sink tables and the
# billing export for prices), logs and licenses, audit entries (logging.logEntries.create) and the settings object,
# which it reads the administrators' policy from and records suspensions and runs in. Nothing else at project level:
# its only IAM write access is Deny Admin on the organization (step 8).
ENFORCER_ROLES=(
  roles/bigquery.dataViewer
  roles/bigquery.jobUser
  roles/discoveryengine.viewer
  roles/logging.viewer
  roles/logging.privateLogViewer
  roles/logging.logWriter
  roles/serviceusage.serviceUsageConsumer
)
ENFORCER_LINKED_ROLES=(roles/bigquery.dataViewer roles/logging.viewer roles/logging.privateLogViewer roles/discoveryengine.viewer)
if [[ "$ENFORCEMENT_BACKEND" == "iam_deny" ]]; then
  for role in "${ENFORCER_ROLES[@]}"; do grant_member "$PROJECT_ID" "serviceAccount:${ENFORCER_SA}" "$role"; done
  grant_optional_role "$PROJECT_ID" "serviceAccount:${ENFORCER_SA}" "$LICENSE_READER_ROLE"
  run retry gcloud storage buckets add-iam-policy-binding "gs://${SETTINGS_BUCKET}" \
    --member "serviceAccount:${ENFORCER_SA}" --role roles/storage.objectAdmin >/dev/null
fi

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
    warn "BILLING_PROJECT_ID is set without BILLING_DATASET: skipping cross-project billing ACL (deployment continues normally)"
  elif [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] grant BigQuery Data Viewer on ${BILLING_PROJECT_ID}:${BILLING_DATASET} (this dataset only)"
  elif grant_dataset_role "$BILLING_PROJECT_ID" "$BILLING_DATASET" READER "$SA_EMAIL"; then
    info "granted BigQuery Data Viewer on ${BILLING_PROJECT_ID}:${BILLING_DATASET} (this dataset only)"
    gcloud projects remove-iam-policy-binding "$BILLING_PROJECT_ID" --member "serviceAccount:${SA_EMAIL}" \
      --role roles/bigquery.dataViewer --condition=None --quiet >/dev/null 2>&1 || true
  else
    warn "no caller access on external billing dataset ${BILLING_PROJECT_ID}:${BILLING_DATASET} (continuing deployment normally; FinOps can be connected later by granting BigQuery Data Viewer on that dataset to ${SA_EMAIL})"
  fi
fi
if [[ "$ENFORCEMENT_BACKEND" == "iam_deny" && -n "$BILLING_PROJECT_ID" && "$BILLING_PROJECT_ID" != "$PROJECT_ID" \
      && -n "$BILLING_DATASET" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] grant BigQuery Data Viewer on ${BILLING_PROJECT_ID}:${BILLING_DATASET} -> ${ENFORCER_SA} (this dataset only)"
  elif grant_dataset_role "$BILLING_PROJECT_ID" "$BILLING_DATASET" READER "$ENFORCER_SA"; then
    info "granted BigQuery Data Viewer on ${BILLING_PROJECT_ID}:${BILLING_DATASET} to ${ENFORCER_SA} (this dataset only)"
  else
    warn "could not grant BigQuery Data Viewer on ${BILLING_PROJECT_ID}:${BILLING_DATASET} to ${ENFORCER_SA}; the enforcement job then prices tokens from the public price list"
  fi
fi

# Linked secondary projects (--link-projects / SCAN_PROJECTS): grant read-only multi-project scanning IAM.
NORMALIZED_SCAN_PROJECTS=""
CLEAN_LINKED=()
if [[ -n "$SCAN_PROJECTS" ]]; then
  LINKED_READ_ROLES=(
    roles/bigquery.dataViewer
    roles/logging.viewer
    roles/logging.privateLogViewer
    roles/monitoring.viewer
    roles/aiplatform.viewer
    roles/discoveryengine.viewer
    roles/iam.securityReviewer
    roles/modelarmor.viewer
    roles/networkservices.viewer
    roles/networksecurity.viewer
    roles/cloudtrace.user   # agent detail page: traces of Agent Engine deployments in the linked project
  )
  # Telemetry routes of a linked project's logs are Log Router sinks in that project (cost-controlled from the hub).
  [[ "${ENABLE_LINKED_TELEMETRY_ROUTING:-true}" == "true" ]] && LINKED_READ_ROLES+=(roles/logging.configWriter)
  IFS=',' read -r -a raw_linked <<<"$SCAN_PROJECTS"
  for raw_lp in "${raw_linked[@]}"; do
    lp="$(normalize_project_id "$raw_lp")"
    [[ -z "$lp" || "$lp" == "$PROJECT_ID" ]] && continue
    [[ " ${CLEAN_LINKED[*]-} " == *" ${lp} "* ]] && continue
    validate_project_id "$lp" "--link-projects"
    CLEAN_LINKED+=("$lp")
    if [[ "$DRY_RUN" == "1" ]]; then
      info "[dry-run] grant multi-project read roles on ${lp} -> ${SA_EMAIL}"
    else
      lp_ok=0
      for lrole in "${LINKED_READ_ROLES[@]}"; do
        if gcloud projects add-iam-policy-binding "$lp" --member "serviceAccount:${SA_EMAIL}" \
            --role "$lrole" --condition=None --quiet >/dev/null 2>&1; then
          lp_ok=$((lp_ok + 1))
        fi
      done
      if [[ $lp_ok -gt 0 ]]; then
        info "linked project ${lp}: granted ${lp_ok} read-only scanning role(s) to ${SA_EMAIL}"
      else
        warn "could not grant IAM roles on linked project ${lp} with ${ACCOUNT} (grant read roles to ${SA_EMAIL} if needed)"
      fi
    fi
    # Gemini Enterprise seats of the linked project: the same read-only custom role as in the hub, because
    # roles/discoveryengine.viewer cannot list license configs or user licenses (best effort, honors DRY_RUN).
    grant_license_reader "$lp" "$SA_EMAIL" "$LICENSE_READER_ROLE_ID" "$LICENSE_READER_PERMS"
    # Gemini Enterprise > Feature management changes the apps of linked projects too (governance actions only).
    if [[ "$ENABLE_GOVERNANCE_ACTIONS" == "true" ]]; then
      grant_ge_feature_manager "$lp" "$SA_EMAIL"
    fi
    # iam_deny: the enforcement job reads usage, logs and licenses of every workspace project (read roles only).
    if [[ "$ENFORCEMENT_BACKEND" == "iam_deny" ]]; then
      if [[ "$DRY_RUN" == "1" ]]; then
        info "[dry-run] grant enforcement job read roles on ${lp} -> ${ENFORCER_SA}"
      else
        for lrole in "${ENFORCER_LINKED_ROLES[@]}"; do
          gcloud projects add-iam-policy-binding "$lp" --member "serviceAccount:${ENFORCER_SA}" \
            --role "$lrole" --condition=None --quiet >/dev/null 2>&1 \
            || warn "could not grant ${lrole} on linked project ${lp} to ${ENFORCER_SA}"
        done
      fi
      grant_license_reader "$lp" "$ENFORCER_SA" "$LICENSE_READER_ROLE_ID" "$LICENSE_READER_PERMS"
    fi
  done
  if [[ ${#CLEAN_LINKED[@]} -gt 0 ]]; then
    NORMALIZED_SCAN_PROJECTS="$(IFS=','; echo "${CLEAN_LINKED[*]}")"
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
    [[ "$ORGPOL_NO_DEFAULT_POOL" == "1" ]] && : >"$BUILD_LOG"
    for build_attempt in 1 2 3; do
      [[ "$ORGPOL_NO_DEFAULT_POOL" == "1" ]] && break
      if gcloud builds submit "$ROOT" \
          --project "$PROJECT_ID" \
          --region "$REGION" \
          --config "$CB_CONFIG" \
          --gcs-source-staging-dir "gs://${SETTINGS_BUCKET}/_build/source" \
          2>&1 | tee "$BUILD_LOG"; then
        BUILD_OK=1
        break
      fi
      if [[ $build_attempt -lt 3 ]] && grep -Eiq 'PERMISSION_DENIED|AccessDenied|denied|unauthorized|403|NOT_FOUND|does not exist|serviceAccount|actAs|propagation|UNAVAILABLE|DEADLINE_EXCEEDED' "$BUILD_LOG"; then
        warn "Cloud Build attempt ${build_attempt}/3 hit IAM/API propagation; retrying in 12s..."
        sleep 12
      else
        break
      fi
    done

    # Tier 2 fallback: if regional worker pool quota is 0 or unavailable (and not restricted by resourceLocations), try global Cloud Build.
    if [[ "$BUILD_OK" != "1" && "$ORGPOL_NO_DEFAULT_POOL" != "1" ]] && ! grep -Eiq 'resourceLocations' "$BUILD_LOG"; then
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
        HOST_ARCH="$(uname -m 2>/dev/null || echo "")"
        DOCKER_BUILT=0
        if docker build --platform linux/amd64 -t "$IMAGE" "$ROOT"; then
          DOCKER_BUILT=1
        elif [[ "$HOST_ARCH" == "x86_64" || "$HOST_ARCH" == "amd64" ]] && docker build -t "$IMAGE" "$ROOT"; then
          DOCKER_BUILT=1
        elif [[ "$HOST_ARCH" != "x86_64" && "$HOST_ARCH" != "amd64" ]]; then
          warn "local Docker could not build linux/amd64 on ${HOST_ARCH} (Cloud Run requires linux/amd64; run from Cloud Shell or enable Docker buildx/Rosetta)"
        fi
        if [[ "$DOCKER_BUILT" == "1" ]] && docker push "$IMAGE"; then
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
add_secret_version() {  # add_secret_version SECRET_NAME VALUE
  local secret="$1" value="$2" attempt
  for attempt in 1 2 3 4; do
    if printf '%s' "$value" | gcloud secrets versions add "$secret" --project "$PROJECT_ID" --data-file=- >/dev/null; then
      return 0
    fi
    [[ $attempt -lt 4 ]] && sleep 4
  done
  return 1
}

log "5/8 Session signing key (Secret Manager: ${SESSION_SECRET_NAME})"
ensure_secret_created "$SESSION_SECRET_NAME" "$PROJECT_ID" "$REGION"
if [[ "$DRY_RUN" == "1" ]]; then
  info "[dry-run] ensure ${SESSION_SECRET_NAME} has an active secret version"
else
  if [[ -z "$(gcloud secrets versions list "$SESSION_SECRET_NAME" --project "$PROJECT_ID" --filter='state=ENABLED' --limit 1 --format='value(name)' 2>/dev/null || true)" ]]; then
    _SESSION_VAL="$(python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_urlsafe(48))')"
    add_secret_version "$SESSION_SECRET_NAME" "$_SESSION_VAL"
    unset _SESSION_VAL
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
    _OAUTH_VAL="$(python3 -c 'import json,sys; sys.stdout.write(json.load(open(sys.argv[1]))["web"]["client_secret"])' "$OAUTH_CLIENT_JSON")"
    add_secret_version "$OAUTH_SECRET_NAME" "$_OAUTH_VAL"
    unset _OAUTH_VAL
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

# Google OAuth client connected later by an administrator (Settings > Access control), with no redeployment: lets
# accounts outside the organization sign in with Google (IAP's Google-managed client admits the organization's own
# accounts only). The dashboard adds the client secret as a version of this secret and reads it by exact version; its
# service account may add, read and retire versions of this one secret and nothing else. Not used when the deployment
# itself sets the OAuth client (ACCESS_MODE google_oauth or GOOGLE_OAUTH_CLIENT_ID).
OAUTH_CONNECT_SECRET=""
if [[ ("$ACCESS_MODE" == "google" || "$ACCESS_MODE" == "password") && "$SECRETS" != *GOOGLE_OAUTH_CLIENT_SECRET=* ]]; then
  if ensure_secret_created "$OAUTH_CONNECT_SECRET_NAME" "$PROJECT_ID" "$REGION"; then
    OAUTH_CONNECT_OK=1
    for role in roles/secretmanager.secretVersionManager roles/secretmanager.secretAccessor; do
      run retry gcloud secrets add-iam-policy-binding "$OAUTH_CONNECT_SECRET_NAME" --project "$PROJECT_ID" \
        --member "serviceAccount:${SA_EMAIL}" --role "$role" --condition=None >/dev/null || OAUTH_CONNECT_OK=0
    done
    if [[ "$OAUTH_CONNECT_OK" == "1" ]]; then
      OAUTH_CONNECT_SECRET="projects/${PROJECT_ID}/secrets/${OAUTH_CONNECT_SECRET_NAME}"
      info "Google OAuth client connection ready (Settings > Access control; secret ${OAUTH_CONNECT_SECRET_NAME})"
    else
      warn "the dashboard's service account could not be granted access to ${OAUTH_CONNECT_SECRET_NAME}: connecting a Google OAuth client in Settings > Access control stays unavailable (Google sign-in through IAP and ID/password sign-in work)"
    fi
  else
    warn "secret ${OAUTH_CONNECT_SECRET_NAME} could not be created: connecting a Google OAuth client in Settings > Access control stays unavailable (Google sign-in through IAP and ID/password sign-in work)"
  fi
fi

# Built-in 'admin' account: a random initial password per deployment (there is no shared default such as admin/admin).
# It is the way in when Google sign-in is unavailable (password-only fallback) and break-glass access otherwise; only
# the dashboard service reads it (the sign-in bridge does not). Change it in Settings > Access control.
ensure_secret_created "$ADMIN_SECRET_NAME" "$PROJECT_ID" "$REGION"
if [[ "$DRY_RUN" == "1" ]]; then
  info "[dry-run] ensure ${ADMIN_SECRET_NAME} holds a random initial admin password"
else
  if [[ -z "$(gcloud secrets versions list "$ADMIN_SECRET_NAME" --project "$PROJECT_ID" --filter='state=ENABLED' --limit 1 --format='value(name)' 2>/dev/null || true)" ]]; then
    _ADMIN_VAL="$(python3 -c 'import secrets,string,sys; a=string.ascii_letters+string.digits; sys.stdout.write("".join(secrets.choice(a) for _ in range(20)))')"
    add_secret_version "$ADMIN_SECRET_NAME" "$_ADMIN_VAL"
    unset _ADMIN_VAL
    info "created a random initial password for the built-in admin account (${ADMIN_SECRET_NAME})"
  fi
fi
run retry gcloud secrets add-iam-policy-binding "$ADMIN_SECRET_NAME" --project "$PROJECT_ID" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/secretmanager.secretAccessor --condition=None >/dev/null
MAIN_SECRETS="${SECRETS},ADMIN_INITIAL_PASSWORD=${ADMIN_SECRET_NAME}:latest"
ADMIN_PW_CMD="gcloud secrets versions access latest --secret=${ADMIN_SECRET_NAME} --project=${PROJECT_ID}"

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

# HUB_GE_FOUND: 1 = a Gemini Enterprise app or license configuration exists in the hub, 0 = none found,
# empty = not looked up here (DISCOVERY_ENGINE_LOCATIONS was configured). Read by the final summary.
HUB_GE_FOUND=""
if [[ -z "$DISCOVERY_ENGINE_LOCATIONS" ]]; then
  DISCOVERY_ENGINE_LOCATIONS="$(ge_locations "$PROJECT_ID")"
  if [[ -n "$DISCOVERY_ENGINE_LOCATIONS" ]]; then
    HUB_GE_FOUND=1
    info "Gemini Enterprise locations: ${DISCOVERY_ENGINE_LOCATIONS} (detected)"
  else
    HUB_GE_FOUND=0
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
AUDIT_DATASET_DISPLAY_LOC="$DATASET_LOCATION"
LINKED_DATASET_LOCATION="$DATASET_LOCATION"
if [[ "$DRY_RUN" != "1" ]]; then
  ACTUAL_AUDIT_LOC="$(dataset_location_of "$PROJECT_ID" "$AUDIT_DATASET" || true)"
  if [[ -n "$ACTUAL_AUDIT_LOC" ]]; then
    AUDIT_DATASET_DISPLAY_LOC="$ACTUAL_AUDIT_LOC"
    if [[ "$DATASET_LOCATION_EXPLICIT" != "1" && "$(printf '%s' "$ACTUAL_AUDIT_LOC" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$DATASET_LOCATION" | tr '[:upper:]' '[:lower:]')" ]]; then
      LINKED_DATASET_LOCATION="$ACTUAL_AUDIT_LOC"
      info "existing hub audit dataset ${PROJECT_ID}:${AUDIT_DATASET} is in ${ACTUAL_AUDIT_LOC}; new linked-project audit datasets will prefer ${ACTUAL_AUDIT_LOC}"
    fi
  fi
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
if [[ ${#CLEAN_LINKED[@]} -gt 0 && "$PREPARE_LINKED_PROJECTS" == "true" ]]; then
  for lp in "${CLEAN_LINKED[@]}"; do
    prepare_linked_project_telemetry "$lp" "$AUDIT_DATASET" "$LINKED_DATASET_LOCATION" "${SERVICE_NAME}-ai-logs" \
      "$CREATE_LOG_SINK" "$ENABLE_DATA_ACCESS_LOGS" "$ENABLE_GE_OBSERVABILITY" "$GE_PROMPT_LOGGING" "$DATASET_LOCATION"
  done
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
add_env SCAN_PROJECTS "$NORMALIZED_SCAN_PROJECTS"
add_env SCAN_ALL_PROJECTS "$SCAN_ALL_PROJECTS"
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
add_env OAUTH_CONNECT_SECRET "$OAUTH_CONNECT_SECRET"   # secret for an OAuth client connected in Settings > Access control
if [[ "$GUEST_ACCESS" == "true" ]]; then
  add_env GUEST_ACCESS true
  add_env GUEST_REDACT_TERMS "$GUEST_REDACT_TERMS"
  warn "guest access is on: anyone with the link can browse read-only, identifiers hidden (demo use only)"
fi
# How suspensions are applied (README 8.9). With iam_deny the dashboard's "Run now" asks Cloud Run to run the job.
add_env ENFORCEMENT_BACKEND "$ENFORCEMENT_BACKEND"
add_env DENY_ATTACHMENT "$DENY_ATTACHMENT"
[[ "$ENFORCEMENT_BACKEND" == "iam_deny" ]] && add_env ENFORCEMENT_JOB "$ENFORCEMENT_JOB_NAME"
# The maintenance tick and real-time usage-event push authenticate as this deployment's own service account.
add_env RUNTIME_SERVICE_ACCOUNT "$SA_EMAIL"
add_env PRICING_SYNC_TIME "$PRICING_SYNC_TIME"
add_env PRICING_CURRENCY "$PRICING_CURRENCY"
add_env TELEMETRY_AI_MODEL "$TELEMETRY_AI_MODEL"
add_env TELEMETRY_DATASET "$TELEMETRY_DATASET"
add_env TELEMETRY_DATASET_LOCATION "$DATASET_LOCATION"   # routed telemetry stays in the allowed region
# Primary domain of the project's organization: IAP's Google-managed OAuth client admits only its accounts, so the
# sign-in bridge allows it and the sign-in page names it. Empty when the organization cannot be read.
IAP_ORG_DOMAIN="$(iap_org_domain "$PROJECT_ID")"
[[ -n "$IAP_ORG_DOMAIN" ]] && info "organization domain: ${IAP_ORG_DOMAIN}"
add_env IAP_ORG_DOMAIN "$IAP_ORG_DOMAIN"
# The dashboard shows the enforcement schedule and the real-time push as configured from these two variables:
# SCHEDULER_INVOKERS (Cloud Scheduler's account may call the enforcement endpoint) and USAGE_EVENTS_SUBSCRIPTION (the
# push subscription). Both are set up in step 8, after this deployment, so they stay out of ENV_VARS: the Direct Cloud
# Run IAP fallback leaves them out (neither call passes IAP), the enforcement job does not need them, and step 8 takes
# out again whatever it could not set up.
LIVE_ENV=""; LIVE_SCHED_ENV=0; LIVE_RT_ENV=0
if [[ -n "$ENFORCEMENT_SCHEDULE" && "$ENFORCEMENT_BACKEND" != "iam_deny" ]]; then
  LIVE_ENV+="##SCHEDULER_INVOKERS=${SCHEDULER_SA}"; LIVE_SCHED_ENV=1
fi
if [[ "$REALTIME_ENFORCEMENT" == "true" ]]; then
  LIVE_ENV+="##USAGE_EVENTS_SUBSCRIPTION=${USAGE_EVENTS_SUB_NAME}"; LIVE_RT_ENV=1
fi
COMMON=(--image "$IMAGE" --project "$PROJECT_ID" --region "$REGION" --cpu-boost --quiet)
[[ -n "$ORGPOL_BINAUTHZ" ]] && COMMON+=(--binary-authorization "$ORGPOL_BINAUTHZ")
[[ -n "$VPC_NETWORK" ]] && COMMON+=(--network "$VPC_NETWORK" --subnet "$VPC_SUBNET" --vpc-egress all-traffic)
MAIN=("${COMMON[@]}" --service-account "$SA_EMAIL")

RUN_DEPLOY_HELP="$(gcloud run deploy --help 2>&1 || true)"
NO_IAP_ARGS=()
[[ "$RUN_DEPLOY_HELP" == *"--no-iap"* || "$RUN_DEPLOY_HELP" == *"--iap"* || "$RUN_DEPLOY_HELP" == *"--[no-]iap"* ]] && NO_IAP_ARGS=(--no-iap)
INVOKER_CHECK_ARGS=()
[[ "$RUN_DEPLOY_HELP" == *"--invoker-iam-check"* || "$RUN_DEPLOY_HELP" == *"--no-invoker-iam-check"* || "$RUN_DEPLOY_HELP" == *"--[no-]invoker-iam-check"* ]] && INVOKER_CHECK_ARGS=(--invoker-iam-check)

EFFECTIVE_ACCESS_MODE="$ACCESS_MODE"
IAP_FALLBACK=0

# Deploys the public-entry dashboard service (which enforces signed session cookies on every route).
# 1) Tries --no-invoker-iam-check (succeeds under Domain Restricted Sharing without allUsers).
# 2) Falls back to --allow-unauthenticated.
# 3) If enterprise org policies block BOTH (--no-invoker-iam-check AND allUsers), switches the dashboard itself to
#    Direct Cloud Run IAP. IAP is then the allowlist: ADMIN_EMAILS / ALLOWED_USERS (and IAP_MEMBERS) are admitted, the
#    dashboard adds accounts approved later, and the summary says so (ID/password sign-in sits behind IAP too).
deploy_main_resilient() {
  local public_env="$1"
  if [[ "$DRY_RUN" == "1" ]]; then
    run gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" ${NO_IAP_ARGS[@]+"${NO_IAP_ARGS[@]}"} --no-invoker-iam-check \
      --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
      --set-env-vars "$public_env" --set-secrets "$MAIN_SECRETS"
    return 0
  fi
  if [[ "$RUN_DEPLOY_HELP" == *"--no-invoker-iam-check"* && "$ORGPOL_REQUIRE_INVOKER_IAM" != "1" ]]; then
    if gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" ${NO_IAP_ARGS[@]+"${NO_IAP_ARGS[@]}"} --no-invoker-iam-check \
        --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
        --set-env-vars "$public_env" --set-secrets "$MAIN_SECRETS"; then
      return 0
    fi
    warn "--no-invoker-iam-check was rejected; retrying with --allow-unauthenticated..."
  fi
  if [[ "$ORGPOL_DRS" == "1" && "$ORGPOL_REQUIRE_INVOKER_IAM" == "1" ]]; then
    info "organization policies allow neither --no-invoker-iam-check nor allUsers; deploying with Cloud Run IAP"
  elif gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" ${NO_IAP_ARGS[@]+"${NO_IAP_ARGS[@]}"} ${INVOKER_CHECK_ARGS[@]+"${INVOKER_CHECK_ARGS[@]}"} --allow-unauthenticated \
      --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
      --set-env-vars "$public_env" --set-secrets "$MAIN_SECRETS"; then
    return 0
  fi
  warn "organization policy blocks unauthenticated Cloud Run entry; automatically switching ${SERVICE_NAME} to Direct Cloud Run IAP..."
  # Neither Cloud Scheduler's nor Pub/Sub's token passes IAP: the schedule and push variables (LIVE_ENV) stay out.
  local iap_env="${ENV_VARS}##AUTH_MODE=iap##IAP_AUDIENCE=/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE_NAME}"
  retry gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" --iap ${INVOKER_CHECK_ARGS[@]+"${INVOKER_CHECK_ARGS[@]}"} --no-allow-unauthenticated \
    --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
    --set-env-vars "$iap_env" --set-secrets "$MAIN_SECRETS" \
    || die "Cloud Run deployment failed under all access modes for ${PROJECT_ID}"
  LIVE_SCHED_ENV=0; LIVE_RT_ENV=0
  retry gcloud run services add-iam-policy-binding "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
    --member "serviceAccount:${IAP_AGENT}" --role roles/run.invoker --condition=None >/dev/null
  configure_iap_access "$PROJECT_ID" "$PROJECT_NUMBER" "$REGION" "$SERVICE_NAME" gate "$IAP_MEMBERS"
  grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "roles/iap.admin"
  EFFECTIVE_ACCESS_MODE="iap"
  IAP_FALLBACK=1
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
      if [[ "$BRIDGE_READY" == "1" ]]; then
        if ! probe_iap_bridge_ready "$BRIDGE_URL"; then
          BRIDGE_READY=0
          gcloud run services delete "$BRIDGE_NAME" --project "$PROJECT_ID" --region "$REGION" --quiet >/dev/null 2>&1 || true
          warn "sign-in bridge (${BRIDGE_NAME}) returned HTTP 502 (project outside a GCP Organization has no Google-managed IAP OAuth client yet); switching to built-in ID/Password sign-in. Sign in as 'admin' and connect a Google OAuth client in Settings > Access control (no redeployment), or enable IAP once in Cloud Console and re-run deploy."
          warn "[한국어 안내] 이 프로젝트는 GCP 조직(Organization)에 속하지 않은 단독 프로젝트로 Google 관리형 IAP OAuth 클라이언트가 자동 생성되지 않습니다(HTTP 502). 우선 기본 'admin' ID/비밀번호 로그인으로 자동 전환했습니다. 로그인 후 설정 > 접근 제어 > 'Google OAuth 클라이언트'를 연결하면 재배포 없이 모든 Google 계정(Gmail 포함) 로그인이 가능합니다."
        fi
      else
        warn "sign-in bridge (${BRIDGE_NAME}) with Cloud Run IAP could not be deployed; deploying the dashboard with built-in ID/Password sign-in enabled"
      fi
    fi
    if [[ "$BRIDGE_READY" == "1" ]]; then
      run retry gcloud run services add-iam-policy-binding "$BRIDGE_NAME" --project "$PROJECT_ID" --region "$REGION" \
        --member "serviceAccount:${IAP_AGENT}" --role roles/run.invoker --condition=None >/dev/null
      # Who may pass IAP to sign in (organization-wide; the dashboard approves people). Refusals are reported, and the
      # dashboard's service account may update the policy when administrators approve accounts later. Accounts outside
      # the organization need an OAuth client made in the console and connected in Settings > Access control (no
      # redeployment; OAUTH_CONNECT_SECRET above), or an ID/password account (README 5.4). Google offers no API that
      # creates such clients (IAP's OAuth Admin API is shut down).
      configure_iap_access "$PROJECT_ID" "$PROJECT_NUMBER" "$REGION" "$BRIDGE_NAME" bridge "$BRIDGE_MEMBERS"
      grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "roles/iap.admin"
      MAIN_ENV="${ENV_VARS}${LIVE_ENV}##AUTH_MODE=google##SIGN_IN_BRIDGE_URL=${BRIDGE_URL}##PUBLIC_BASE_URL=${BASE_URL}"
      MAIN_ENV+="##IAP_ACCESS_RESOURCE=projects/${PROJECT_NUMBER}/iap_web/cloud_run-${REGION}/services/${BRIDGE_NAME}"
      [[ -n "${IAP_OAUTH_CLIENT:-}" ]] && MAIN_ENV+="##IAP_OAUTH_CLIENT=${IAP_OAUTH_CLIENT}"
    else
      MAIN_ENV="${ENV_VARS}${LIVE_ENV}##AUTH_MODE=google##PUBLIC_BASE_URL=${BASE_URL}"
    fi
    deploy_main_resilient "$MAIN_ENV"
    ;;
  password)
    deploy_main_resilient "${ENV_VARS}${LIVE_ENV}##AUTH_MODE=google##PUBLIC_BASE_URL=${BASE_URL}"
    ;;
  google_oauth)
    deploy_main_resilient "${ENV_VARS}${LIVE_ENV}##AUTH_MODE=google##GOOGLE_OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID}##PUBLIC_BASE_URL=${BASE_URL}"
    ;;
  iap)
    IAP_ENV="${ENV_VARS}##AUTH_MODE=iap##IAP_AUDIENCE=/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE_NAME}"
    run retry gcloud run deploy "$SERVICE_NAME" "${MAIN[@]}" --iap ${INVOKER_CHECK_ARGS[@]+"${INVOKER_CHECK_ARGS[@]}"} --no-allow-unauthenticated \
      --memory "$MEMORY" --cpu "$CPU" --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --timeout 300 \
      --set-env-vars "$IAP_ENV" --set-secrets "$MAIN_SECRETS"
    run retry gcloud run services add-iam-policy-binding "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
      --member "serviceAccount:${IAP_AGENT}" --role roles/run.invoker --condition=None >/dev/null
    configure_iap_access "$PROJECT_ID" "$PROJECT_NUMBER" "$REGION" "$SERVICE_NAME" gate "$IAP_MEMBERS"
    grant_optional_role "$PROJECT_ID" "serviceAccount:${SA_EMAIL}" "roles/iap.admin"
    if [[ "$DRY_RUN" != "1" ]] && ! probe_iap_bridge_ready "$BASE_URL"; then
      warn "Cloud Run IAP on ${SERVICE_NAME} returned HTTP 502 (project outside a GCP Organization has no IAP OAuth client yet). Open Cloud Console > Cloud Run > ${SERVICE_NAME} > Security to initialize IAP once, or re-deploy with --access-mode google / password."
      warn "[한국어 안내] 이 프로젝트는 GCP 조직(Organization) 밖의 단독 프로젝트여서 Cloud Run IAP OAuth 클라이언트가 아직 초기화되지 않았습니다(HTTP 502). Cloud Console > Cloud Run > ${SERVICE_NAME} > 보안에서 IAP를 1회 초기화하거나, --access-mode google 또는 password로 배포하세요."
    fi
    ;;
esac

# iam_deny: the enforcement job. The same image runs one enforcement cycle per execution as the enforcer service
# account, with the dashboard's configuration and settings bucket (README 8.9). It serves no requests and nobody signs
# in to it, so it gets no secrets and no LIVE_ENV; PYTHONPATH makes `python -m controlplane.enforce_job` find the
# package in /app/src.
if [[ "$ENFORCEMENT_BACKEND" == "iam_deny" ]]; then
  JOB_DEPLOY=(--image "$IMAGE" --project "$PROJECT_ID" --region "$ENFORCEMENT_JOB_REGION"
    --service-account "$ENFORCER_SA" --command python --args=-m,controlplane.enforce_job
    --tasks 1 --max-retries 0 --task-timeout 600s --memory "$MEMORY" --cpu "$CPU"
    --set-env-vars "${ENV_VARS}##PUBLIC_BASE_URL=${BASE_URL}##PYTHONPATH=/app/src"
    --labels app=ai-control-plane --quiet)
  [[ -n "$ORGPOL_BINAUTHZ" ]] && JOB_DEPLOY+=(--binary-authorization "$ORGPOL_BINAUTHZ")
  [[ -n "$VPC_NETWORK" ]] && JOB_DEPLOY+=(--network "$VPC_NETWORK" --subnet "$VPC_SUBNET" --vpc-egress all-traffic)
  info "enforcement job ${ENFORCER_JOB} (${ENFORCEMENT_JOB_REGION}) runs as ${ENFORCER_SA}"
  run retry gcloud run jobs deploy "$ENFORCER_JOB" "${JOB_DEPLOY[@]}" \
    || die "could not deploy the enforcement job ${ENFORCER_JOB}; fix the error above and re-run, or deploy with ENFORCEMENT_BACKEND=iam_allow_swap"
fi

# ------------------------------------------------------------------------------- 8. scheduler & live readiness probe
log "8/8 Schedule & readiness check"
remove_scheduler_job() {  # remove_scheduler_job NAME LOCATION WHY: deletes a scheduler job of an earlier deployment
  gcloud scheduler jobs describe "$1" --project "$PROJECT_ID" --location "$2" >/dev/null 2>&1 || return 0
  if [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] remove scheduler job $1 ($3)"
  elif gcloud scheduler jobs delete "$1" --project "$PROJECT_ID" --location "$2" --quiet >/dev/null 2>&1; then
    info "removed scheduler job $1 ($3)"
  else
    warn "could not remove scheduler job $1 ($3); delete it in Cloud Scheduler"
  fi
  return 0
}
ensure_scheduler_sa() {
  if ! gcloud iam service-accounts describe "$SCHEDULER_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
    run gcloud iam service-accounts create "$SCHEDULER_SA_ID" --project "$PROJECT_ID" \
      --display-name "AI Control Plane enforcement scheduler"
  fi
}
WEB_SCHED_READY=0   # 1 once the -enforce job that calls the dashboard exists (SCHEDULER_INVOKERS is kept only then)
ENFORCER_SCHED_READY=0
if [[ "$ENFORCEMENT_BACKEND" == "iam_deny" ]]; then
  # Who may run the enforcement job, on this job only: the dashboard's service account ("Run now", and reading the job
  # and its executions for the status card) and the scheduler's service account (scheduled runs).
  # `gcloud run jobs add-iam-policy-binding` takes no --condition flag (job policies have no conditional bindings).
  for role in roles/run.jobsExecutor roles/run.viewer; do
    run retry gcloud run jobs add-iam-policy-binding "$ENFORCER_JOB" --project "$PROJECT_ID" \
      --region "$ENFORCEMENT_JOB_REGION" --member "serviceAccount:${SA_EMAIL}" --role "$role" \
      --quiet >/dev/null
  done
  if [[ -n "$ENFORCEMENT_SCHEDULE" ]]; then
    ensure_scheduler_sa
    run retry gcloud run jobs add-iam-policy-binding "$ENFORCER_JOB" --project "$PROJECT_ID" \
      --region "$ENFORCEMENT_JOB_REGION" --member "serviceAccount:${SCHEDULER_SA}" --role roles/run.jobsExecutor \
      --quiet >/dev/null
    # Cloud Scheduler calls the Cloud Run Admin API with an OAuth token (not the dashboard, so IAP never applies).
    SCHED_ARGS=(--project "$PROJECT_ID" --location "$ENFORCEMENT_JOB_REGION" --schedule "$ENFORCEMENT_SCHEDULE"
      --time-zone "$WORKSPACE_TIMEZONE" --uri "$ENFORCER_RUN_URI" --http-method POST --message-body '{}'
      --oauth-service-account-email "$SCHEDULER_SA" --attempt-deadline 180s --max-retry-attempts 1 --quiet)
    if gcloud scheduler jobs describe "$ENFORCER_SCHED_JOB" --project "$PROJECT_ID" --location "$ENFORCEMENT_JOB_REGION" >/dev/null 2>&1; then
      if run retry_quiet gcloud scheduler jobs update http "$ENFORCER_SCHED_JOB" "${SCHED_ARGS[@]}" --update-headers Content-Type=application/json >/dev/null; then
        ENFORCER_SCHED_READY=1
      else
        warn "could not update Cloud Scheduler job ${ENFORCER_SCHED_JOB} (Cloud Scheduler API blocked or unavailable in ${ENFORCEMENT_JOB_REGION}); select Run enforcement now in the dashboard after each period reset"
      fi
    else
      if run retry_quiet gcloud scheduler jobs create http "$ENFORCER_SCHED_JOB" "${SCHED_ARGS[@]}" --headers Content-Type=application/json >/dev/null; then
        ENFORCER_SCHED_READY=1
      else
        warn "could not create Cloud Scheduler job ${ENFORCER_SCHED_JOB} (Cloud Scheduler API blocked or unavailable in ${ENFORCEMENT_JOB_REGION}); select Run enforcement now in the dashboard after each period reset"
      fi
    fi
    [[ "$ENFORCER_SCHED_READY" == "1" ]] \
      && info "job ${ENFORCER_SCHED_JOB}: ${ENFORCEMENT_SCHEDULE} (${WORKSPACE_TIMEZONE}) -> Cloud Run job ${ENFORCER_JOB} (${ENFORCEMENT_JOB_REGION})"
  else
    remove_scheduler_job "$ENFORCER_SCHED_JOB" "$ENFORCEMENT_JOB_REGION" "ENFORCEMENT_SCHEDULE off"
    info "enforcement schedule: on-demand from the dashboard (Run enforcement now runs ${ENFORCER_JOB})"
  fi
  # One enforcer at a time: the web endpoint's scheduler of an earlier deployment is removed, and with it the
  # scheduler's right to call the web service.
  remove_scheduler_job "${SERVICE_NAME}-enforce" "$REGION" "enforcement moved to the Cloud Run job ${ENFORCER_JOB}"
  if [[ "$DRY_RUN" != "1" ]]; then
    gcloud run services remove-iam-policy-binding "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
      --member "serviceAccount:${SCHEDULER_SA}" --role roles/run.invoker --condition=None --quiet >/dev/null 2>&1 || true
  fi
  grant_org_deny_roles "$PROJECT_ID" "$ENFORCER_SA" "$SA_EMAIL" "$GRANT_ORG_ROLES"
else
  if [[ -n "$ENFORCEMENT_SCHEDULE" && "$EFFECTIVE_ACCESS_MODE" != "iap" ]]; then
    ensure_scheduler_sa
    JOB="${SERVICE_NAME}-enforce"
    JOB_ARGS=(--project "$PROJECT_ID" --location "$REGION" --schedule "$ENFORCEMENT_SCHEDULE"
      --time-zone "$WORKSPACE_TIMEZONE" --uri "$ENFORCE_URL" --http-method POST --message-body '{}'
      --oidc-service-account-email "$SCHEDULER_SA" --oidc-token-audience "$ENFORCE_URL"
      --attempt-deadline 300s --max-retry-attempts 1 --quiet)
    if gcloud scheduler jobs describe "$JOB" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
      if run retry_quiet gcloud scheduler jobs update http "$JOB" "${JOB_ARGS[@]}" --update-headers Content-Type=application/json >/dev/null; then
        WEB_SCHED_READY=1
      else
        warn "could not update Cloud Scheduler job ${JOB} (Cloud Scheduler API blocked or unavailable in ${REGION}); run enforcement from the dashboard after each period reset"
      fi
    else
      if run retry_quiet gcloud scheduler jobs create http "$JOB" "${JOB_ARGS[@]}" --headers Content-Type=application/json >/dev/null; then
        WEB_SCHED_READY=1
      else
        warn "could not create Cloud Scheduler job ${JOB} (Cloud Scheduler API blocked or unavailable in ${REGION}); run enforcement from the dashboard after each period reset"
      fi
    fi
    if [[ "$WEB_SCHED_READY" == "1" ]]; then
      info "job ${JOB}: ${ENFORCEMENT_SCHEDULE} (${WORKSPACE_TIMEZONE}) -> ${ENFORCE_URL}"
      # When the organization requires IAM invoker checks (run.managed.requireInvokerIam) the service is not public and
      # Cloud Run would reject the scheduler's OIDC call before the app sees it; restores would then never run.
      run retry gcloud run services add-iam-policy-binding "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
        --member "serviceAccount:${SCHEDULER_SA}" --role roles/run.invoker --condition=None --quiet >/dev/null \
        || warn "could not grant roles/run.invoker on ${SERVICE_NAME} to ${SCHEDULER_SA}; if the service requires IAM invoker checks, scheduled enforcement (and automatic restores) will fail"
    fi
  else
    # No scheduled runs (ENFORCEMENT_SCHEDULE off, a read-only install or IAP): the job of an earlier deployment would
    # keep calling the dashboard on the old schedule, so it is removed with the scheduler's right to call the service.
    if [[ "$EFFECTIVE_ACCESS_MODE" == "iap" ]]; then SCHED_OFF_WHY="IAP access mode"
    elif [[ "$ENABLE_GOVERNANCE_ACTIONS" != "true" && "$ENFORCEMENT_BACKEND" != "notify_only" ]]; then SCHED_OFF_WHY="read-only install"
    else SCHED_OFF_WHY="ENFORCEMENT_SCHEDULE off"; fi
    remove_scheduler_job "${SERVICE_NAME}-enforce" "$REGION" "$SCHED_OFF_WHY"
    if [[ "$DRY_RUN" != "1" ]]; then
      gcloud run services remove-iam-policy-binding "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
        --member "serviceAccount:${SCHEDULER_SA}" --role roles/run.invoker --condition=None --quiet >/dev/null 2>&1 || true
    fi
    info "enforcement schedule: on-demand from the dashboard (${SCHED_OFF_WHY})"
  fi
  # Leaving iam_deny: the job's schedule of an earlier deployment is removed (one enforcer at a time). The job and its
  # service account are kept until removed by hand (README 8.9), so a managed deny policy can still be cleaned up.
  remove_scheduler_job "$ENFORCER_SCHED_JOB" "$ENFORCEMENT_JOB_REGION" "ENFORCEMENT_BACKEND=${ENFORCEMENT_BACKEND}"
fi

# Real-time Antigravity cap enforcement (services/usage_events.py): routes each Antigravity inference log entry
# through Pub/Sub push to POST /api/v1/governance/antigravity/usage-event with an OIDC token of the dashboard's
# runtime service account. Automatic mode then starts enforcement as soon as a person reaches their cap instead of
# waiting for the next scheduled run; the scheduled run stays the safety net for period resets.
REALTIME_READY=0
if [[ "$REALTIME_ENFORCEMENT" == "true" && "$EFFECTIVE_ACCESS_MODE" != "iap" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] Pub/Sub topic ${USAGE_EVENTS_TOPIC} + log sink ${USAGE_EVENTS_SINK} -> push ${USAGE_EVENTS_SUB_ID} -> ${USAGE_EVENT_URL} (OIDC ${SA_EMAIL})"
    REALTIME_READY=1
  else
    _rt_ok=1
    if ! gcloud pubsub topics describe "$USAGE_EVENTS_TOPIC" --project "$PROJECT_ID" >/dev/null 2>&1; then
      retry gcloud pubsub topics create "$USAGE_EVENTS_TOPIC" --project "$PROJECT_ID" --quiet >/dev/null 2>&1 \
        || { warn "could not create Pub/Sub topic ${USAGE_EVENTS_TOPIC}; real-time cap enforcement skipped"; _rt_ok=0; }
    fi
    if [[ "$_rt_ok" == "1" ]]; then
      ensure_usage_events_sink "$PROJECT_ID" "$PROJECT_ID" "$USAGE_EVENTS_TOPIC" "$USAGE_EVENTS_SINK" 0 || _rt_ok=0
    fi
    if [[ "$_rt_ok" == "1" && ${#CLEAN_LINKED[@]} -gt 0 && "$PREPARE_LINKED_PROJECTS" == "true" ]]; then
      for lp in "${CLEAN_LINKED[@]}"; do
        ensure_usage_events_sink "$lp" "$PROJECT_ID" "$USAGE_EVENTS_TOPIC" "$USAGE_EVENTS_SINK" 1 || true
      done
    fi
    if [[ "$_rt_ok" == "1" ]]; then
      retry gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project "$PROJECT_ID" \
        --member "serviceAccount:${PUBSUB_AGENT}" --role roles/iam.serviceAccountTokenCreator \
        --condition=None --quiet >/dev/null 2>&1 \
        || warn "could not grant roles/iam.serviceAccountTokenCreator on ${SA_EMAIL} to ${PUBSUB_AGENT}; Pub/Sub OIDC push may fail on older projects"
      gcloud run services add-iam-policy-binding "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
        --member "serviceAccount:${SA_EMAIL}" --role roles/run.invoker --condition=None --quiet >/dev/null 2>&1 || true
      PUSH_ARGS=(--project "$PROJECT_ID" --push-endpoint="$USAGE_EVENT_URL"
        --push-auth-service-account="$SA_EMAIL" --push-auth-token-audience="$USAGE_EVENT_URL"
        --ack-deadline=60 --min-retry-delay=10s --max-retry-delay=60s --message-retention-duration=10m --quiet)
      if gcloud pubsub subscriptions describe "$USAGE_EVENTS_SUB_ID" --project "$PROJECT_ID" >/dev/null 2>&1; then
        retry gcloud pubsub subscriptions update "$USAGE_EVENTS_SUB_ID" "${PUSH_ARGS[@]}" >/dev/null 2>&1 \
          && REALTIME_READY=1 \
          || warn "could not update Pub/Sub push subscription ${USAGE_EVENTS_SUB_ID}; scheduled enforcement still applies"
      else
        retry gcloud pubsub subscriptions create "$USAGE_EVENTS_SUB_ID" --topic="$USAGE_EVENTS_TOPIC" "${PUSH_ARGS[@]}" >/dev/null 2>&1 \
          && REALTIME_READY=1 \
          || warn "could not create Pub/Sub push subscription ${USAGE_EVENTS_SUB_ID}; scheduled enforcement still applies"
      fi
      [[ "$REALTIME_READY" == "1" ]] \
        && info "real-time usage push ${USAGE_EVENTS_SUB_ID} -> ${USAGE_EVENT_URL} (OIDC ${SA_EMAIL})"
    fi
  fi
elif [[ "$DRY_RUN" != "1" ]]; then
  # Real-time enforcement off (or the dashboard fell back to IAP): remove what an earlier deployment created, the push
  # subscription first so nothing is delivered any more, then the sinks of the hub and the linked projects and the topic.
  if gcloud pubsub subscriptions describe "$USAGE_EVENTS_SUB_ID" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud pubsub subscriptions delete "$USAGE_EVENTS_SUB_ID" --project "$PROJECT_ID" --quiet >/dev/null 2>&1 \
      && info "removed Pub/Sub push subscription ${USAGE_EVENTS_SUB_ID} (real-time enforcement off)" || true
  fi
  for sink_proj in "$PROJECT_ID" ${CLEAN_LINKED[@]+"${CLEAN_LINKED[@]}"}; do
    if gcloud logging sinks describe "$USAGE_EVENTS_SINK" --project "$sink_proj" >/dev/null 2>&1; then
      gcloud logging sinks delete "$USAGE_EVENTS_SINK" --project "$sink_proj" --quiet >/dev/null 2>&1 \
        && info "removed log sink ${USAGE_EVENTS_SINK} on ${sink_proj} (real-time enforcement off)" || true
    fi
  done
  if gcloud pubsub topics describe "$USAGE_EVENTS_TOPIC" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud pubsub topics delete "$USAGE_EVENTS_TOPIC" --project "$PROJECT_ID" --quiet >/dev/null 2>&1 \
      && info "removed Pub/Sub topic ${USAGE_EVENTS_TOPIC} (real-time enforcement off)" || true
  fi
fi

# Maintenance tick: daily official price/FX check at pricing.sync_time (Settings > Data collection and cost) and the
# telemetry route budget guard. It replaces the earlier fixed-time pricing job, which is removed when found.
MAINT_JOB="${SERVICE_NAME}-maintenance"
MAINT_URL="${BASE_URL}/api/v1/system/maintenance"
LEGACY_PRICING_JOB="${SERVICE_NAME}-pricing-sync"
if [[ "$MAINTENANCE_SCHEDULE" != "off" && "$EFFECTIVE_ACCESS_MODE" != "iap" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    info "[dry-run] scheduler job ${MAINT_JOB}: ${MAINTENANCE_SCHEDULE} -> ${MAINT_URL} (OIDC ${SA_EMAIL})"
  else
    MAINT_ARGS=(--project "$PROJECT_ID" --location "$REGION" --schedule "$MAINTENANCE_SCHEDULE"
      --time-zone "Etc/UTC" --uri "$MAINT_URL" --http-method POST --message-body '{}'
      --oidc-service-account-email "$SA_EMAIL" --oidc-token-audience "$MAINT_URL"
      --attempt-deadline 600s --max-retry-attempts 1 --quiet)
    gcloud run services add-iam-policy-binding "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
      --member "serviceAccount:${SA_EMAIL}" --role roles/run.invoker --condition=None --quiet >/dev/null 2>&1 || true
    if gcloud scheduler jobs describe "$MAINT_JOB" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
      run retry_quiet gcloud scheduler jobs update http "$MAINT_JOB" "${MAINT_ARGS[@]}" --update-headers Content-Type=application/json >/dev/null \
        && info "job ${MAINT_JOB}: ${MAINTENANCE_SCHEDULE} -> ${MAINT_URL}" \
        || warn "could not update ${MAINT_JOB}; the in-process monitor still runs scheduled work while an instance is up"
    else
      run retry_quiet gcloud scheduler jobs create http "$MAINT_JOB" "${MAINT_ARGS[@]}" --headers Content-Type=application/json >/dev/null \
        && info "job ${MAINT_JOB}: ${MAINTENANCE_SCHEDULE} -> ${MAINT_URL}" \
        || warn "could not create ${MAINT_JOB}; the in-process monitor still runs scheduled work while an instance is up"
    fi
  fi
elif [[ "$EFFECTIVE_ACCESS_MODE" == "iap" ]]; then
  remove_scheduler_job "$MAINT_JOB" "$REGION" "IAP blocks Cloud Scheduler calls"
  info "maintenance schedule: IAP blocks Cloud Scheduler calls; scheduled work runs in-process while an instance is up (use ACCESS_MODE google, google_oauth or password for a reliable schedule)"
else
  remove_scheduler_job "$MAINT_JOB" "$REGION" "MAINTENANCE_SCHEDULE=off"
  info "maintenance schedule: off (MAINTENANCE_SCHEDULE=off); run the price check from the FinOps page"
fi
if [[ "$DRY_RUN" != "1" ]] && gcloud scheduler jobs describe "$LEGACY_PRICING_JOB" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs delete "$LEGACY_PRICING_JOB" --project "$PROJECT_ID" --location "$REGION" --quiet >/dev/null 2>&1 \
    && info "removed ${LEGACY_PRICING_JOB} (replaced by ${MAINT_JOB})" || true
fi

# The revision of step 7 announced the enforcement schedule and the real-time push (LIVE_ENV) before step 8 set them
# up. Whatever did not come up is taken out again, so the dashboard never shows it as configured: at most one more
# revision, and only when something failed.
STALE_ENV=()
[[ "$LIVE_SCHED_ENV" == "1" && "$WEB_SCHED_READY" != "1" ]] && STALE_ENV+=(SCHEDULER_INVOKERS)
[[ "$LIVE_RT_ENV" == "1" && "$REALTIME_READY" != "1" ]] && STALE_ENV+=(USAGE_EVENTS_SUBSCRIPTION)
if [[ ${#STALE_ENV[@]} -gt 0 && "$DRY_RUN" != "1" ]]; then
  STALE_CSV="$(IFS=','; echo "${STALE_ENV[*]}")"
  if gcloud run services update "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" \
      --remove-env-vars "$STALE_CSV" --quiet >/dev/null 2>&1; then
    info "removed ${STALE_CSV} from ${SERVICE_NAME} (not set up, so the dashboard shows it as not configured)"
  else
    warn "could not remove ${STALE_CSV} from ${SERVICE_NAME}; the dashboard may show it as configured. Run: gcloud run services update ${SERVICE_NAME} --project ${PROJECT_ID} --region ${REGION} --remove-env-vars ${STALE_CSV}"
  fi
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
  else
    warn "readiness probe ${BASE_URL}/api/v1/auth/state returned HTTP ${HTTP_CODE}; if the dashboard does not open, inspect Cloud Run logs: gcloud run services logs read ${SERVICE_NAME} --project ${PROJECT_ID} --region ${REGION} --limit 50"
  fi
  if [[ "$MAINTENANCE_SCHEDULE" != "off" && "$EFFECTIVE_ACCESS_MODE" != "iap" ]]; then
    if gcloud scheduler jobs run "$MAINT_JOB" --project "$PROJECT_ID" --location "$REGION" --quiet >/dev/null 2>&1; then
      info "triggered initial maintenance (${MAINT_JOB}: official pricing sync + telemetry discovery)"
    fi
  fi
fi

log "Deployment complete: ${PROJECT_ID}"
case "$EFFECTIVE_ACCESS_MODE" in
  iap) info "Dashboard URL : ${BASE_URL} (Google IAP sign-in)" ;;
  password) info "Dashboard URL : ${BASE_URL}/login (ID/Password sign-in: account 'admin')" ;;
  google)
    if [[ "$BRIDGE_READY" == "1" ]]; then
      info "Dashboard URL : ${BASE_URL}/login (Google sign-in: ${ADMIN_EMAILS}${ALLOWED_USERS:+, viewers: $ALLOWED_USERS})"
    else
      EFFECTIVE_ACCESS_MODE="password"
      info "Dashboard URL : ${BASE_URL}/login (ID/Password sign-in: account 'admin'; Google sign-in bridge unavailable)"
    fi ;;
  *) info "Dashboard URL : ${BASE_URL}/login (${ADMIN_EMAILS})" ;;
esac
if [[ "$EFFECTIVE_ACCESS_MODE" != "iap" ]]; then
  info "Admin password: ${ADMIN_PW_CMD}   (built-in 'admin' account; change it in Settings > Access control)"
fi
if [[ -n "$OAUTH_CONNECT_SECRET" && "$EFFECTIVE_ACCESS_MODE" != "iap" ]]; then
  info "Google OAuth  : any approved Google account (other organizations, partners, Gmail) can sign in once an administrator connects an OAuth client in Settings > Access control (no redeployment). Redirect URI: ${BASE_URL}/auth/google/callback"
fi
iap_access_report
if [[ "$IAP_FALLBACK" == "1" ]]; then
  warn "Organization policies block a public entry, so the dashboard itself runs behind IAP: only accounts IAP admits can open it, and ID/password sign-in is not available. Add people under Settings > Access control or with the gcloud command above."
  warn "[한국어 안내] 조직 정책(도메인 제한 공유 + run.managed.requireInvokerIam) 때문에 대시보드 자체를 IAP 뒤에 배포했습니다. IAP가 허용한 Google 계정만 대시보드를 열 수 있고 ID/비밀번호 로그인은 쓸 수 없습니다. 사람 추가: 설정 > 접근 제어, 또는 위 gcloud 명령."
fi
info "Audit dataset : ${PROJECT_ID}:${AUDIT_DATASET} (${AUDIT_DATASET_DISPLAY_LOC}, Data Access=${ENABLE_DATA_ACCESS_LOGS}, GE Prompt Logging=${GE_PROMPT_LOGGING})"
[[ -n "$NORMALIZED_SCAN_PROJECTS" ]] && info "Linked scan   : ${NORMALIZED_SCAN_PROJECTS}"
info "Settings store: gs://${SETTINGS_BUCKET}"
info "Container     : ${IMAGE}"
# A hub without Gemini Enterprise and no linked project usually means Gemini Enterprise runs in another project the
# runtime service account cannot read (its pages stay empty). Advice only: never fails the deployment, and a dry run
# only reuses the detection above (no extra lookup).
if [[ ${#CLEAN_LINKED[@]} -eq 0 && ",$DISABLED_MODULES," != *",gemini_enterprise,"* ]]; then
  if [[ -z "$HUB_GE_FOUND" && "$DRY_RUN" != "1" ]]; then
    if [[ -n "$(ge_locations "$PROJECT_ID" || true)" ]]; then HUB_GE_FOUND=1; else HUB_GE_FOUND=0; fi
  fi
  if [[ "$HUB_GE_FOUND" == "0" ]]; then
    warn "No Gemini Enterprise app or seats found in ${PROJECT_ID}. If Gemini Enterprise runs in another project, install into that project (recommended: governance and marker roles act on the primary project's IAM) or re-run with --link-projects <GE project>."
    warn "[한국어 안내] ${PROJECT_ID}에서 Gemini Enterprise 앱이나 라이선스 좌석을 찾지 못했습니다. Gemini Enterprise가 다른 프로젝트에서 운영 중이면 그 프로젝트에 설치하거나(권장: 거버넌스와 마커 역할은 대표 프로젝트의 IAM에 적용됩니다) --link-projects <GE 프로젝트>로 다시 실행하세요."
  fi
fi
case "$ENFORCEMENT_BACKEND" in
  iam_deny)
    if [[ "$ENFORCER_SCHED_READY" == "1" ]]; then
      info "Antigravity caps: deny-policy enforcement by the Cloud Run job ${ENFORCER_JOB} (${ENFORCEMENT_JOB_REGION}) ${ENFORCEMENT_SCHEDULE} (${WORKSPACE_TIMEZONE}); suspended users are restored automatically at each period reset"
    else
      info "Antigravity caps: deny-policy enforcement by the Cloud Run job ${ENFORCER_JOB} (${ENFORCEMENT_JOB_REGION}); no schedule: select Run enforcement now in the dashboard after each period reset"
    fi
    [[ "$REALTIME_READY" == "1" ]] && info "Real-time caps: active via Pub/Sub push (${USAGE_EVENTS_SUB_ID} -> ${USAGE_EVENT_URL})"
    if [[ ${#ORG_DENY_ROLE_COMMANDS[@]} -gt 0 ]]; then
      warn "deny-policy enforcement starts once the organization roles above are granted (until then every run stops at the safety gates)"
    fi
    antigravity_readiness "$PROJECT_ID"
    ;;
  notify_only)
    if [[ "$WEB_SCHED_READY" == "1" ]]; then
      info "Antigravity caps: notify only (plans and notifications; access never changes); runs ${ENFORCEMENT_SCHEDULE} (${WORKSPACE_TIMEZONE})"
    else
      info "Antigravity caps: notify only (plans and notifications; access never changes)"
    fi
    antigravity_readiness "$PROJECT_ID"
    ;;
  *)
    if [[ "$ENABLE_GOVERNANCE_ACTIONS" == "true" ]]; then
      if [[ "$WEB_SCHED_READY" == "1" ]]; then
        info "Antigravity caps: enforcement runs ${ENFORCEMENT_SCHEDULE} (${WORKSPACE_TIMEZONE}); suspended users are restored automatically at each period reset"
      else
        info "Antigravity caps: no enforcement schedule; select Run enforcement now in the dashboard after each period reset"
      fi
      [[ "$REALTIME_READY" == "1" ]] && info "Real-time caps: active via Pub/Sub push (${USAGE_EVENTS_SUB_ID} -> ${USAGE_EVENT_URL})"
      antigravity_readiness "$PROJECT_ID"
    fi
    ;;
esac
