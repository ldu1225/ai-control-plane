#!/usr/bin/env bash
# AI Control Plane: One-Click / One-Command Installer.
#
# Run directly in Google Cloud Shell (or macOS/Linux) — no manual git clone or config editing needed:
#
#   1) Interactive 1-click wizard (press Enter to accept smart defaults):
#      bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh)
#
#   2) Fully automated single or multi-project deployment (zero prompts):
#      bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
#        --projects PROJECT_A,PROJECT_B --admins admin@example.org --viewers viewer@example.org
#
#   3) Pre-deployment log collection only:
#      bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
#        --prepare-only --project PROJECT_ID
set -euo pipefail

REPO_URL="${ACP_REPO_URL:-https://github.com/ldu1225/ai-control-plane.git}"
INSTALL_DIR="${ACP_INSTALL_DIR:-$HOME/ai-control-plane}"

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
CLI_REGION=""
CLI_ADMINS=""
CLI_VIEWERS=""
EXTRA_ARGS=()

need_val() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 needs a value"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prepare-only) PREPARE_ONLY=1; shift ;;
    -y|--yes) AUTO_YES=1; shift ;;
    --dry-run) DRY_RUN=1; EXTRA_ARGS+=(--dry-run); shift ;;
    --project) need_val "$@"; CLI_PROJECT="$2"; shift 2 ;;
    --projects) need_val "$@"; CLI_PROJECTS="$2"; shift 2 ;;
    --region) need_val "$@"; CLI_REGION="$2"; shift 2 ;;
    --admins|--admin) need_val "$@"; CLI_ADMINS="$2"; shift 2 ;;
    --viewers|--viewer|--allowed-users) need_val "$@"; CLI_VIEWERS="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

for tool in gcloud bq python3; do command -v "$tool" >/dev/null || die "$tool is required (Google Cloud Shell has all of them)"; done
ACCOUNT="$(ensure_gcloud_auth)"

TARGET_PROJECTS="${CLI_PROJECTS:-$CLI_PROJECT}"
ACTIVE_PROJECT="$(normalize_project_id "$(gcloud config get-value project 2>/dev/null || true)")"
[[ "$ACTIVE_PROJECT" == "(unset)" ]] && ACTIVE_PROJECT=""

# Interactive wizard when no --project / --projects flag was supplied and stdin is a terminal.
if [[ -z "$TARGET_PROJECTS" && "$AUTO_YES" == "0" && -t 0 ]]; then
  log "AI Control Plane — One-Click Setup (${ACCOUNT})"
  PROJ_RAW="$(gcloud projects list --format='value(projectId,name)' --limit=30 2>/dev/null || true)"
  PROJ_LINES=()
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
    read -r -p "  1. Project ID or number(s) (comma-separated allowed) [${PROMPT_DEFAULT}]: " INPUT_PROJ
    INPUT_PROJ="$(trim_ws "${INPUT_PROJ:-$PROMPT_DEFAULT}")"
  else
    read -r -p "  1. Project ID or number(s) (comma-separated allowed): " INPUT_PROJ
    INPUT_PROJ="$(trim_ws "$INPUT_PROJ")"
  fi
  [[ -n "$INPUT_PROJ" ]] || die "no project selected"

  # Resolve menu numbers (for example "1" or "1,2") as well as literal project IDs.
  RESOLVED_LIST=()
  IFS=',' read -r -a TOKENS <<<"$INPUT_PROJ"
  for tok in "${TOKENS[@]}"; do
    tok="$(trim_ws "$tok")"
    [[ -n "$tok" ]] || continue
    if [[ "$tok" =~ ^[0-9]+$ ]] && [[ "$tok" -ge 1 && "$tok" -le ${#PROJ_LINES[@]} ]]; then
      entry="${PROJ_LINES[$((tok - 1))]}"
      RESOLVED_LIST+=("${entry%%$'\t'*}")
    else
      RESOLVED_LIST+=("$(normalize_project_id "$tok")")
    fi
  done
  TARGET_PROJECTS="$(IFS=','; echo "${RESOLVED_LIST[*]}")"

  if [[ "$PREPARE_ONLY" == "0" ]]; then
    DEF_REGION="${CLI_REGION:-asia-northeast3}"
    read -r -p "  2. Region [${DEF_REGION}]: " INPUT_REGION
    CLI_REGION="$(trim_ws "${INPUT_REGION:-$DEF_REGION}")"

    DEF_ADMINS="${CLI_ADMINS:-$ACCOUNT}"
    read -r -p "  3. Administrator email(s) [${DEF_ADMINS}]: " INPUT_ADMINS
    CLI_ADMINS="$(trim_ws "${INPUT_ADMINS:-$DEF_ADMINS}")"

    read -r -p "  4. Viewer email(s) (optional, comma-separated) [${CLI_VIEWERS}]: " INPUT_VIEWERS
    CLI_VIEWERS="$(trim_ws "${INPUT_VIEWERS:-$CLI_VIEWERS}")"
  fi
elif [[ -z "$TARGET_PROJECTS" ]]; then
  [[ -n "$ACTIVE_PROJECT" ]] || die "pass --project PROJECT_ID (no active gcloud project)"
  TARGET_PROJECTS="$ACTIVE_PROJECT"
fi

CLI_REGION="${CLI_REGION:-asia-northeast3}"
CLI_ADMINS="${CLI_ADMINS:-$ACCOUNT}"

if [[ "$PREPARE_ONLY" == "1" ]]; then
  if [[ "$TARGET_PROJECTS" == *,* ]]; then
    exec "$ROOT/scripts/prepare.sh" --projects "$TARGET_PROJECTS" --dataset-location "$CLI_REGION" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
  else
    exec "$ROOT/scripts/prepare.sh" --project "$TARGET_PROJECTS" --dataset-location "$CLI_REGION" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
  fi
fi

# Save per-project env snapshots (git-ignored via /*.env) so re-runs or customizations are effortless.
if [[ "$DRY_RUN" != "1" ]]; then
  IFS=',' read -r -a SAVE_PROJECTS <<<"$TARGET_PROJECTS"
  for raw_pid in "${SAVE_PROJECTS[@]}"; do
    pid="$(normalize_project_id "$raw_pid")"
    [[ -n "$pid" ]] || continue
    ENV_OUT="$ROOT/deploy.${pid}.env"
    cat >"$ENV_OUT" <<EOF
PROJECT_ID="${pid}"
REGION="${CLI_REGION}"
DATASET_LOCATION="${CLI_REGION}"
DEFAULT_LOCALE="ko"
WORKSPACE_TIMEZONE="Asia/Seoul"
ACCESS_MODE="google"
ADMIN_EMAILS="${CLI_ADMINS}"
ALLOWED_USERS="${CLI_VIEWERS}"
ENABLE_DATA_ACCESS_LOGS="true"
ENABLE_GE_OBSERVABILITY="true"
GE_PROMPT_LOGGING="true"
ENABLE_GOVERNANCE_ACTIONS="true"
EOF
  done
fi

DEPLOY_CMD=("$ROOT/scripts/deploy.sh" --region "$CLI_REGION" --admins "$CLI_ADMINS")
[[ -n "$CLI_VIEWERS" ]] && DEPLOY_CMD+=(--viewers "$CLI_VIEWERS")
if [[ "$TARGET_PROJECTS" == *,* ]]; then
  DEPLOY_CMD+=(--projects "$TARGET_PROJECTS")
else
  DEPLOY_CMD+=(--project "$TARGET_PROJECTS")
fi
[[ ${#EXTRA_ARGS[@]} -gt 0 ]] && DEPLOY_CMD+=("${EXTRA_ARGS[@]}")

exec "${DEPLOY_CMD[@]}"
