#!/usr/bin/env bash
# AI Control Plane: One-Click / One-Command Installer.
#
# Run directly in Google Cloud Shell (or macOS/Linux) — no manual git clone or config editing needed:
#
#   1) Interactive 1-click wizard (press Enter to accept smart defaults):
#      bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh)
#
#   2) Single-project automated deployment (zero prompts):
#      bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
#        --project PROJECT_ID
#
#   3) Unified Multi-Project Hub (1 dashboard monitoring multiple projects together):
#      bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
#        --project HUB_PROJECT_ID --link-projects PROJECT_B,PROJECT_C
#
#   4) Separate per-project deployments (independent Cloud Run instance in each project):
#      bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
#        --projects PROJECT_A,PROJECT_B --admins admin@example.org --viewers viewer@example.org
#
#   5) Pre-deployment log collection only:
#      bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
#        --prepare-only --project PROJECT_ID
set -euo pipefail

REPO_URL="${ACP_REPO_URL:-https://github.com/ldu1225/ai-control-plane.git}"
INSTALL_DIR="${ACP_INSTALL_DIR:-$HOME/ai-control-plane}"

show_install_help() {
  cat <<'HELP'
AI Control Plane: One-Click / One-Command Installer.

Run directly in Google Cloud Shell (or macOS/Linux) — no manual git clone or config editing needed:

  1) Interactive 1-click wizard (press Enter to accept smart defaults):
     bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh)

  2) Single-project automated deployment (zero prompts):
     bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
       --project PROJECT_ID

  3) Unified Multi-Project Hub (1 dashboard monitoring multiple projects together):
     bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
       --project HUB_PROJECT_ID --link-projects PROJECT_B,PROJECT_C

  4) Separate per-project deployments (independent Cloud Run instance in each project):
     bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
       --projects PROJECT_A,PROJECT_B --admins admin@example.org --viewers viewer@example.org

  5) Pre-deployment log collection only:
     bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
       --prepare-only --project PROJECT_ID
HELP
  exit "${1:-0}"
}
for _arg in "$@"; do
  [[ "$_arg" == "-h" || "$_arg" == "--help" ]] && show_install_help 0
done

# Detect whether we are already running from a local checkout or streamed via curl/process substitution.
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
LOCAL_ROOT=""
if [[ -n "$SCRIPT_PATH" && -f "$SCRIPT_PATH" ]]; then
  CANDIDATE_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." 2>/dev/null && pwd || true)"
  if [[ -n "$CANDIDATE_ROOT" && -f "$CANDIDATE_ROOT/scripts/deploy.sh" && -f "$CANDIDATE_ROOT/scripts/lib/common.sh" ]]; then
    LOCAL_ROOT="$CANDIDATE_ROOT"
  fi
fi

if [[ -z "$LOCAL_ROOT" ]]; then
  # Move out of any deleted or target directory first so git/python/gcloud never fail on an orphaned $PWD.
  cd "$HOME" 2>/dev/null || cd /tmp
  command -v git >/dev/null || { echo "error: git is required" >&2; exit 1; }
  if [[ -f "$INSTALL_DIR/deploy.env" ]]; then
    cp -f "$INSTALL_DIR/deploy.env" "$INSTALL_DIR/deploy.env.bak" 2>/dev/null || true
    rm -f "$INSTALL_DIR/deploy.env" 2>/dev/null || true
  fi
  UPDATED=0
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    printf '\n==> Updating AI Control Plane in %s\n' "$INSTALL_DIR"
    rm -f "$INSTALL_DIR/.git/index.lock" 2>/dev/null || true
    if git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL" >/dev/null 2>&1 \
        && (git -C "$INSTALL_DIR" fetch --depth 1 --quiet origin main >/dev/null 2>&1 \
            || git -C "$INSTALL_DIR" fetch --depth 1 --quiet origin HEAD >/dev/null 2>&1) \
        && git -C "$INSTALL_DIR" checkout -f -B main FETCH_HEAD >/dev/null 2>&1 \
        && git -C "$INSTALL_DIR" reset --hard --quiet FETCH_HEAD >/dev/null 2>&1; then
      UPDATED=1
    else
      printf '    existing checkout could not be fast-updated; performing clean re-clone...\n'
    fi
  fi
  if [[ "$UPDATED" != "1" ]]; then
    rm -rf "$INSTALL_DIR"
    printf '\n==> Downloading AI Control Plane into %s\n' "$INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
  chmod +x "$INSTALL_DIR/scripts/"*.sh 2>/dev/null || true
  cd "$INSTALL_DIR"
  exec "$INSTALL_DIR/scripts/install.sh" "$@"
fi

ROOT="$LOCAL_ROOT"
cd "$ROOT" 2>/dev/null || cd /tmp
# shellcheck source=lib/common.sh
source "$ROOT/scripts/lib/common.sh"

normalize_cli_args "$@"
set -- ${NORMALIZED_ARGS[@]+"${NORMALIZED_ARGS[@]}"}

PREPARE_ONLY=0
AUTO_YES=0
DRY_RUN=0
CLI_PROJECT=""
CLI_PROJECTS=""
CLI_LINK_PROJECTS=""
CLI_REGION=""
CLI_ADMINS=""
CLI_VIEWERS=""
EXTRA_ARGS=()
DEPLOY_ONLY_ARGS=()

need_val() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 needs a value"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prepare-only) PREPARE_ONLY=1; shift ;;
    -y|--yes) AUTO_YES=1; shift ;;
    --dry-run) DRY_RUN=1; EXTRA_ARGS+=(--dry-run); shift ;;
    --project) need_val "$@"; CLI_PROJECT="$2"; shift 2 ;;
    --projects) need_val "$@"; CLI_PROJECTS="$2"; shift 2 ;;
    --link-projects|--scan-projects) need_val "$@"; CLI_LINK_PROJECTS="$2"; shift 2 ;;
    --region) need_val "$@"; CLI_REGION="$2"; shift 2 ;;
    --admins|--admin) need_val "$@"; CLI_ADMINS="$2"; shift 2 ;;
    --viewers|--viewer|--allowed-users) need_val "$@"; CLI_VIEWERS="$2"; shift 2 ;;
    --org-name|--access-mode|--service-name|--billing-account|--config|--dataset|--dataset-location|--ge-locations|--vpc-network|--vpc-subnet)
      need_val "$@"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    --read-only|--data-access-logs|--no-data-access-logs|--prepare-linked-projects|--no-prepare-linked-projects|--auto-discover-projects|--no-auto-discover-projects|--log-prompts|--no-log-prompts|--skip-preflight-blockers)
      EXTRA_ARGS+=("$1"); shift ;;
    # Deploy-only flags (prepare.sh does not take them).
    --enforcement-schedule) need_val "$@"; DEPLOY_ONLY_ARGS+=("$1" "$2"); shift 2 ;;
    --no-enforcement-schedule) DEPLOY_ONLY_ARGS+=("$1"); shift ;;
    -h|--help) show_install_help 0 ;;
    *) echo "unknown argument: $1" >&2; show_install_help 2 ;;
  esac
done

for tool in gcloud bq python3; do command -v "$tool" >/dev/null || die "$tool is required (Google Cloud Shell has all of them)"; done
ACCOUNT="$(ensure_gcloud_auth)"

TARGET_PROJECTS="${CLI_PROJECTS:-$CLI_PROJECT}"
ACTIVE_PROJECT="$(normalize_project_id "$(gcloud config get-value project 2>/dev/null || true)")"
[[ "$ACTIVE_PROJECT" == "(unset)" ]] && ACTIVE_PROJECT=""

PROJ_LINES=()
resolve_project_csv() {  # resolve_project_csv RAW_CSV -> comma-separated project IDs
  local raw="$1" tok entry out=()
  IFS=',' read -r -a _toks <<<"$raw"
  for tok in ${_toks[@]+"${_toks[@]}"}; do
    tok="$(trim_ws "$tok")"
    [[ -n "$tok" ]] || continue
    if [[ "$tok" =~ ^[0-9]+$ ]] && [[ ${#PROJ_LINES[@]} -gt 0 && "$tok" -ge 1 && "$tok" -le ${#PROJ_LINES[@]} ]]; then
      entry="${PROJ_LINES[$((tok - 1))]}"
      out+=("${entry%%$'\t'*}")
    else
      out+=("$(normalize_project_id "$tok")")
    fi
  done
  if [[ ${#out[@]} -gt 0 ]]; then
    (IFS=','; echo "${out[*]}")
  fi
}

# Interactive wizard when no --project / --projects flag was supplied and stdin is a terminal.
if [[ -z "$TARGET_PROJECTS" && "$AUTO_YES" == "0" && ( -t 0 || "${ACP_FORCE_WIZARD:-0}" == "1" ) ]]; then
  log "AI Control Plane — One-Click Setup (${ACCOUNT})"
  PROJ_RAW="$(gcloud projects list --format='value(projectId,name)' --limit=30 2>/dev/null || true)"
  while IFS= read -r line; do
    [[ -n "$line" ]] && PROJ_LINES+=("$line")
  done <<<"$PROJ_RAW"

  if [[ ${#PROJ_LINES[@]} -gt 0 ]]; then
    printf '\n  Accessible Google Cloud projects:\n'
    idx=1
    for entry in "${PROJ_LINES[@]}"; do
      pid="${entry%%$'\t'*}"
      pname="${entry#*$'\t'}"
      marker=""
      [[ "$pid" == "$ACTIVE_PROJECT" ]] && marker="  <-- current"
      printf '    [%2d] %-36s (%s)%s\n' "$idx" "$pid" "$pname" "$marker"
      idx=$((idx + 1))
    done
    printf '\n'
  fi

  PROMPT_DEFAULT="${ACTIVE_PROJECT}"
  if [[ -n "$PROMPT_DEFAULT" ]]; then
    read -r -p "  1. Host project ID or number(s) [${PROMPT_DEFAULT}]: " INPUT_PROJ
    INPUT_PROJ="$(trim_ws "${INPUT_PROJ:-$PROMPT_DEFAULT}")"
  else
    read -r -p "  1. Host project ID or number(s): " INPUT_PROJ
    INPUT_PROJ="$(trim_ws "$INPUT_PROJ")"
  fi
  [[ -n "$INPUT_PROJ" ]] || die "no project selected"
  TARGET_PROJECTS="$(resolve_project_csv "$INPUT_PROJ")"
  [[ -n "$TARGET_PROJECTS" ]] || die "no project selected"

  if [[ "$PREPARE_ONLY" == "0" ]]; then
    if [[ "$TARGET_PROJECTS" != *,* && -z "$CLI_LINK_PROJECTS" ]]; then
      read -r -p "  2. Additional projects to monitor together in this dashboard (optional, IDs/numbers) []: " INPUT_LINK
      INPUT_LINK="$(trim_ws "${INPUT_LINK:-}")"
      [[ -n "$INPUT_LINK" ]] && CLI_LINK_PROJECTS="$(resolve_project_csv "$INPUT_LINK")"
      STEP_OFFSET=1
    else
      STEP_OFFSET=0
    fi

    DEF_REGION="${CLI_REGION:-asia-northeast3}"
    read -r -p "  $((2 + STEP_OFFSET)). Region [${DEF_REGION}]: " INPUT_REGION
    CLI_REGION="$(trim_ws "${INPUT_REGION:-$DEF_REGION}")"

    DEF_ADMINS="${CLI_ADMINS:-$ACCOUNT}"
    read -r -p "  $((3 + STEP_OFFSET)). Administrator email(s) [${DEF_ADMINS}]: " INPUT_ADMINS
    CLI_ADMINS="$(trim_ws "${INPUT_ADMINS:-$DEF_ADMINS}")"

    read -r -p "  $((4 + STEP_OFFSET)). Viewer email(s) (optional, comma-separated) [${CLI_VIEWERS}]: " INPUT_VIEWERS
    CLI_VIEWERS="$(trim_ws "${INPUT_VIEWERS:-$CLI_VIEWERS}")"
  fi
elif [[ -z "$TARGET_PROJECTS" ]]; then
  [[ -n "$ACTIVE_PROJECT" ]] || die "pass --project PROJECT_ID (no active gcloud project)"
  TARGET_PROJECTS="$ACTIVE_PROJECT"
fi

CLI_REGION="${CLI_REGION:-asia-northeast3}"
CLI_ADMINS="${CLI_ADMINS:-$ACCOUNT}"

NORMALIZED_LINK_PROJECTS=""
if [[ -n "$CLI_LINK_PROJECTS" ]]; then
  LINK_OUT=()
  IFS=',' read -r -a RAW_LINK_ARR <<<"$CLI_LINK_PROJECTS"
  for rlp in "${RAW_LINK_ARR[@]}"; do
    nlp="$(normalize_project_id "$rlp")"
    [[ -n "$nlp" ]] || continue
    [[ " ${LINK_OUT[*]-} " == *" ${nlp} "* ]] || LINK_OUT+=("$nlp")
  done
  [[ ${#LINK_OUT[@]} -gt 0 ]] && NORMALIZED_LINK_PROJECTS="$(IFS=','; echo "${LINK_OUT[*]}")"
fi

if [[ "$PREPARE_ONLY" == "1" ]]; then
  PREP_LIST=()
  IFS=',' read -r -a _raw_prep <<<"${TARGET_PROJECTS}${NORMALIZED_LINK_PROJECTS:+,${NORMALIZED_LINK_PROJECTS}}"
  for _pp in "${_raw_prep[@]}"; do
    _npp="$(normalize_project_id "$_pp")"
    [[ -n "$_npp" ]] || continue
    [[ " ${PREP_LIST[*]-} " == *" ${_npp} "* ]] || PREP_LIST+=("$_npp")
  done
  PREP_ALL="$(IFS=','; echo "${PREP_LIST[*]}")"
  if [[ "$PREP_ALL" == *,* ]]; then
    exec "$ROOT/scripts/prepare.sh" --projects "$PREP_ALL" --dataset-location "$CLI_REGION" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
  else
    exec "$ROOT/scripts/prepare.sh" --project "$PREP_ALL" --dataset-location "$CLI_REGION" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
  fi
fi

# Save per-project env snapshots (git-ignored via /*.env) so re-runs or customizations are effortless.
# Opt-outs given on this run are saved too, so a later scripts/deploy.sh --project PROJECT_ID keeps them.
SAVE_DATA_ACCESS="true"
SAVE_LOG_PROMPTS="true"
SAVE_GOVERNANCE="true"
for _xa in ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}; do
  case "$_xa" in
    --data-access-logs) SAVE_DATA_ACCESS="true" ;;
    --no-data-access-logs) SAVE_DATA_ACCESS="false" ;;
    --log-prompts) SAVE_LOG_PROMPTS="true" ;;
    --no-log-prompts) SAVE_LOG_PROMPTS="false" ;;
    --read-only) SAVE_GOVERNANCE="false" ;;
  esac
done
if [[ "$DRY_RUN" != "1" || "${ACP_SAVE_ENV_ON_DRY_RUN:-0}" == "1" ]]; then
  IFS=',' read -r -a SAVE_PROJECTS <<<"$TARGET_PROJECTS"
  for raw_pid in "${SAVE_PROJECTS[@]}"; do
    pid="$(normalize_project_id "$raw_pid")"
    [[ -n "$pid" ]] || continue
    PER_PID_LINKS=""
    if [[ -n "$NORMALIZED_LINK_PROJECTS" ]]; then
      FILT_LINKS=()
      IFS=',' read -r -a _larr <<<"$NORMALIZED_LINK_PROJECTS"
      for _lp in "${_larr[@]}"; do
        [[ -n "$_lp" && "$_lp" != "$pid" ]] && FILT_LINKS+=("$_lp")
      done
      [[ ${#FILT_LINKS[@]} -gt 0 ]] && PER_PID_LINKS="$(IFS=','; echo "${FILT_LINKS[*]}")"
    fi
    ENV_OUT="$ROOT/deploy.${pid}.env"
    cat >"$ENV_OUT" <<EOF
PROJECT_ID="${pid}"
SCAN_PROJECTS="${PER_PID_LINKS}"
REGION="${CLI_REGION}"
DATASET_LOCATION="${CLI_REGION}"
DEFAULT_LOCALE="ko"
WORKSPACE_TIMEZONE="Asia/Seoul"
ACCESS_MODE="google"
ADMIN_EMAILS="${CLI_ADMINS}"
ALLOWED_USERS="${CLI_VIEWERS}"
ENABLE_DATA_ACCESS_LOGS="${SAVE_DATA_ACCESS}"
ENABLE_GE_OBSERVABILITY="true"
GE_PROMPT_LOGGING="${SAVE_LOG_PROMPTS}"
ENABLE_GOVERNANCE_ACTIONS="${SAVE_GOVERNANCE}"
EOF
  done
fi

DEPLOY_CMD=("$ROOT/scripts/deploy.sh" --region "$CLI_REGION" --admins "$CLI_ADMINS")
[[ -n "$CLI_VIEWERS" ]] && DEPLOY_CMD+=(--viewers "$CLI_VIEWERS")
[[ -n "$NORMALIZED_LINK_PROJECTS" ]] && DEPLOY_CMD+=(--link-projects "$NORMALIZED_LINK_PROJECTS")
if [[ "$TARGET_PROJECTS" == *,* ]]; then
  DEPLOY_CMD+=(--projects "$TARGET_PROJECTS")
else
  DEPLOY_CMD+=(--project "$TARGET_PROJECTS")
fi
[[ ${#EXTRA_ARGS[@]} -gt 0 ]] && DEPLOY_CMD+=("${EXTRA_ARGS[@]}")
[[ ${#DEPLOY_ONLY_ARGS[@]} -gt 0 ]] && DEPLOY_CMD+=("${DEPLOY_ONLY_ARGS[@]}")

exec "${DEPLOY_CMD[@]}"
