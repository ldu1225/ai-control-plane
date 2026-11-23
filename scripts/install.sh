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
#
#   6) Per-user caps enforced with an IAM deny policy by a dedicated Cloud Run job (README 8.9):
#      bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
#        --project PROJECT_ID --enforcement-backend iam_deny [--grant-org-roles]
#
# Running the command again updates the deployment. The settings of every run are saved in
# deploy.PROJECT_ID.env next to the scripts; a later run (or scripts/deploy.sh --project PROJECT_ID) keeps them
# unless a flag gives a new value. Run with --help for every option.
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

  6) Per-user caps enforced with an IAM deny policy by a dedicated Cloud Run job (README 8.9):
     bash <(curl -fsSL https://github.com/ldu1225/ai-control-plane/raw/main/scripts/install.sh) \
       --project PROJECT_ID --enforcement-backend iam_deny [--grant-org-roles]

Options
  --project ID / --projects ID1,ID2   Project to deploy into, or one separate deployment per project
  --link-projects ID1,ID2             Projects monitored together in this dashboard (alias --scan-projects);
                                      none clears the saved list
  --region REGION                     Region (default asia-northeast3); the BigQuery dataset follows it unless
                                      --dataset-location is given
  --admins EMAILS / --viewers EMAILS  Administrators (default: the active gcloud account) and viewers
                                      (--viewers none clears them; aliases --admin, --viewer, --allowed-users)
  --prepare-only                      Only turn on log collection (scripts/prepare.sh); it takes --dataset,
                                      --dataset-location, --ge-locations, --service-name, --[no-]data-access-logs
                                      and --[no-]log-prompts
  -y, --yes                           Never start the interactive wizard
  --dry-run                           Print what would change and change nothing
  -h, --help                          Show this help

  Passed to scripts/deploy.sh (see scripts/deploy.sh --help) and saved for later runs:
  --access-mode MODE  --org-name NAME  --service-name NAME  --billing-account ID
  --dataset NAME  --dataset-location LOC  --ge-locations LIST  --vpc-network NAME  --vpc-subnet NAME
  --read-only | --no-read-only  --[no-]data-access-logs  --[no-]log-prompts
  --[no-]prepare-linked-projects  --[no-]auto-discover-projects
  --enforcement-schedule CRON | --no-enforcement-schedule  --[no-]realtime-enforcement
  --maintenance-schedule CRON | --no-maintenance-schedule
  --enforcement-backend iam_allow_swap|iam_deny|notify_only  --deny-attachment projects|scope
  --enforcement-job-region REGION  --[no-]grant-org-roles
  Passed to scripts/deploy.sh for this run only: --config FILE  --skip-preflight-blockers

Running the command again updates the deployment in place. Settings are saved per project in
deploy.PROJECT_ID.env next to the scripts (git-ignored); later runs keep them unless a flag gives a new value.
Remove everything with scripts/teardown.sh --project PROJECT_ID.
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
    # The settings snapshots of earlier runs (deploy.PROJECT_ID.env) and the deploy.env backup survive the re-clone:
    # without them a later run would fall back to the defaults (for example another region or access mode).
    KEEP_DIR=""
    if [[ -d "$INSTALL_DIR" ]]; then
      KEEP_DIR="$(mktemp -d 2>/dev/null || true)"
      if [[ -n "$KEEP_DIR" ]]; then
        for _kept in "$INSTALL_DIR"/deploy.*.env "$INSTALL_DIR/deploy.env.bak"; do
          [[ -f "$_kept" ]] && cp -p "$_kept" "$KEEP_DIR/" 2>/dev/null || true
        done
      fi
    fi
    rm -rf "$INSTALL_DIR"
    printf '\n==> Downloading AI Control Plane into %s\n' "$INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    if [[ -n "$KEEP_DIR" ]]; then
      for _kept in "$KEEP_DIR"/*; do
        [[ -f "$_kept" ]] && cp -p "$_kept" "$INSTALL_DIR/" 2>/dev/null || true
      done
      rm -rf "$KEEP_DIR"
    fi
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
EXTRA_ARGS=()      # passed to scripts/deploy.sh
PREPARE_ARGS=()    # passed to scripts/prepare.sh (--prepare-only); it takes fewer flags than deploy.sh
# Settings given on this run (KEY and value), written to deploy.<project>.env so that later runs keep them.
SAVE_KEYS=()
SAVE_VALS=()

need_val() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 needs a value"; }
lower_trim() { trim_ws "${1:-}" | tr '[:upper:]' '[:lower:]'; }
# one_of FLAG VALUE CHOICE...: values of these flags are saved to deploy.<project>.env, which deploy.sh sources.
one_of() {
  local flag="$1" value="$2" choice
  shift 2
  for choice in "$@"; do
    [[ "$value" == "$choice" ]] && return 0
  done
  die "$flag must be one of: $*"
}
# save_setting KEY VALUE: remembers a setting given on this run (the last one given wins).
save_setting() {
  local i
  for i in ${SAVE_KEYS[@]+"${!SAVE_KEYS[@]}"}; do
    if [[ "${SAVE_KEYS[$i]}" == "$1" ]]; then
      SAVE_VALS[$i]="$2"
      return 0
    fi
  done
  SAVE_KEYS+=("$1")
  SAVE_VALS+=("$2")
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prepare-only) PREPARE_ONLY=1; shift ;;
    -y|--yes) AUTO_YES=1; shift ;;
    --dry-run) DRY_RUN=1; EXTRA_ARGS+=(--dry-run); PREPARE_ARGS+=(--dry-run); shift ;;
    --project) need_val "$@"; CLI_PROJECT="$2"; shift 2 ;;
    --projects) need_val "$@"; CLI_PROJECTS="$2"; shift 2 ;;
    --link-projects|--scan-projects) need_val "$@"; CLI_LINK_PROJECTS="$2"; shift 2 ;;
    --region) need_val "$@"; CLI_REGION="$2"; shift 2 ;;
    --admins|--admin) need_val "$@"; CLI_ADMINS="$2"; shift 2 ;;
    --viewers|--viewer|--allowed-users) need_val "$@"; CLI_VIEWERS="$2"; shift 2 ;;
    # Settings that scripts/prepare.sh takes as well.
    --dataset|--dataset-location|--ge-locations|--service-name)
      need_val "$@"
      case "$1" in
        --dataset) save_setting AUDIT_DATASET "$2" ;;
        --dataset-location) save_setting DATASET_LOCATION "$2" ;;
        --ge-locations) save_setting DISCOVERY_ENGINE_LOCATIONS "$2" ;;
        --service-name) save_setting SERVICE_NAME "$2" ;;
      esac
      EXTRA_ARGS+=("$1" "$2"); PREPARE_ARGS+=("$1" "$2"); shift 2 ;;
    --data-access-logs|--no-data-access-logs|--log-prompts|--no-log-prompts)
      case "$1" in
        --data-access-logs) save_setting ENABLE_DATA_ACCESS_LOGS true ;;
        --no-data-access-logs) save_setting ENABLE_DATA_ACCESS_LOGS false ;;
        --log-prompts) save_setting GE_PROMPT_LOGGING true ;;
        --no-log-prompts) save_setting GE_PROMPT_LOGGING false ;;
      esac
      EXTRA_ARGS+=("$1"); PREPARE_ARGS+=("$1"); shift ;;
    # Deploy settings with a value.
    --access-mode)
      need_val "$@"; one_of "$1" "$2" google google_oauth iap password
      save_setting ACCESS_MODE "$2"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    --org-name) need_val "$@"; save_setting ORGANIZATION_NAME "$2"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    --billing-account) need_val "$@"; save_setting BILLING_ACCOUNT_ID "$2"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    --vpc-network) need_val "$@"; save_setting VPC_NETWORK "$2"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    --vpc-subnet) need_val "$@"; save_setting VPC_SUBNET "$2"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    --enforcement-schedule) need_val "$@"; save_setting ENFORCEMENT_SCHEDULE "$2"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    --maintenance-schedule) need_val "$@"; save_setting MAINTENANCE_SCHEDULE "$2"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    --enforcement-backend)
      need_val "$@"; one_of "$1" "$2" iam_allow_swap iam_deny notify_only
      save_setting ENFORCEMENT_BACKEND "$2"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    --deny-attachment)
      need_val "$@"; one_of "$1" "$2" projects scope
      save_setting DENY_ATTACHMENT "$2"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    --enforcement-job-region)
      need_val "$@"
      [[ "$2" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || die "$1 must be a region such as asia-northeast3"
      save_setting ENFORCEMENT_JOB_REGION "$2"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    # Deploy switches.
    --read-only) save_setting ENABLE_GOVERNANCE_ACTIONS false; EXTRA_ARGS+=("$1"); shift ;;
    --no-read-only) save_setting ENABLE_GOVERNANCE_ACTIONS true; EXTRA_ARGS+=("$1"); shift ;;
    --prepare-linked-projects) save_setting PREPARE_LINKED_PROJECTS true; EXTRA_ARGS+=("$1"); shift ;;
    --no-prepare-linked-projects) save_setting PREPARE_LINKED_PROJECTS false; EXTRA_ARGS+=("$1"); shift ;;
    --auto-discover-projects) save_setting SCAN_ALL_PROJECTS true; EXTRA_ARGS+=("$1"); shift ;;
    --no-auto-discover-projects) save_setting SCAN_ALL_PROJECTS false; EXTRA_ARGS+=("$1"); shift ;;
    --no-enforcement-schedule) save_setting ENFORCEMENT_SCHEDULE off; EXTRA_ARGS+=("$1"); shift ;;
    --realtime-enforcement) save_setting REALTIME_ENFORCEMENT true; EXTRA_ARGS+=("$1"); shift ;;
    --no-realtime-enforcement) save_setting REALTIME_ENFORCEMENT false; EXTRA_ARGS+=("$1"); shift ;;
    --no-maintenance-schedule) save_setting MAINTENANCE_SCHEDULE off; EXTRA_ARGS+=("$1"); shift ;;
    --grant-org-roles) save_setting GRANT_ORG_ROLES true; EXTRA_ARGS+=("$1"); shift ;;
    --no-grant-org-roles) save_setting GRANT_ORG_ROLES false; EXTRA_ARGS+=("$1"); shift ;;
    # Passed to deploy.sh for this run only (never saved).
    --config) need_val "$@"; EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    --skip-preflight-blockers) EXTRA_ARGS+=("$1"); shift ;;
    -h|--help) show_install_help 0 ;;
    *) echo "unknown argument: $1" >&2; show_install_help 2 ;;
  esac
done

for tool in gcloud bq python3; do command -v "$tool" >/dev/null || die "$tool is required (Google Cloud Shell has all of them)"; done
ACCOUNT="$(ensure_gcloud_auth)"

TARGET_PROJECTS="${CLI_PROJECTS:-$CLI_PROJECT}"
ACTIVE_PROJECT="$(normalize_project_id "$(gcloud config get-value project 2>/dev/null || true)")"
[[ "$ACTIVE_PROJECT" == "(unset)" ]] && ACTIVE_PROJECT=""

# saved_setting PROJECT KEY: the value an earlier run saved for KEY in deploy.<PROJECT>.env (empty when absent).
saved_setting() {
  local file="$ROOT/deploy.${1}.env"
  [[ -f "$file" ]] || return 0
  bash -c 'set +u; source "$1" >/dev/null 2>&1 || true; key="$2"; printf "%s" "${!key:-}"' _ "$file" "$2" 2>/dev/null || true
}

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
    # A project deployed before offers its saved settings as the defaults (Enter keeps them).
    PREV_PROJECT=""
    [[ "$TARGET_PROJECTS" != *,* ]] && PREV_PROJECT="$TARGET_PROJECTS"
    if [[ "$TARGET_PROJECTS" != *,* && -z "$CLI_LINK_PROJECTS" ]]; then
      PREV_LINKS="$(saved_setting "$PREV_PROJECT" SCAN_PROJECTS)"
      read -r -p "  2. Additional projects to monitor together in this dashboard (optional, IDs/numbers) [${PREV_LINKS}${PREV_LINKS:+; none clears}]: " INPUT_LINK
      INPUT_LINK="$(trim_ws "${INPUT_LINK:-}")"
      if [[ "$(lower_trim "$INPUT_LINK")" == "none" ]]; then
        CLI_LINK_PROJECTS="none"
      elif [[ -n "$INPUT_LINK" ]]; then
        CLI_LINK_PROJECTS="$(resolve_project_csv "$INPUT_LINK")"
      fi
      STEP_OFFSET=1
    else
      STEP_OFFSET=0
    fi

    DEF_REGION="${CLI_REGION:-}"
    [[ -n "$DEF_REGION" || -z "$PREV_PROJECT" ]] || DEF_REGION="$(saved_setting "$PREV_PROJECT" REGION)"
    DEF_REGION="${DEF_REGION:-asia-northeast3}"
    read -r -p "  $((2 + STEP_OFFSET)). Region [${DEF_REGION}]: " INPUT_REGION
    CLI_REGION="$(trim_ws "${INPUT_REGION:-$DEF_REGION}")"

    DEF_ADMINS="${CLI_ADMINS:-}"
    [[ -n "$DEF_ADMINS" || -z "$PREV_PROJECT" ]] || DEF_ADMINS="$(saved_setting "$PREV_PROJECT" ADMIN_EMAILS)"
    DEF_ADMINS="${DEF_ADMINS:-$ACCOUNT}"
    read -r -p "  $((3 + STEP_OFFSET)). Administrator email(s) [${DEF_ADMINS}]: " INPUT_ADMINS
    CLI_ADMINS="$(trim_ws "${INPUT_ADMINS:-$DEF_ADMINS}")"

    DEF_VIEWERS="${CLI_VIEWERS:-}"
    [[ -n "$DEF_VIEWERS" || -z "$PREV_PROJECT" ]] || DEF_VIEWERS="$(saved_setting "$PREV_PROJECT" ALLOWED_USERS)"
    read -r -p "  $((4 + STEP_OFFSET)). Viewer email(s) (optional, comma-separated) [${DEF_VIEWERS}${DEF_VIEWERS:+; none clears}]: " INPUT_VIEWERS
    CLI_VIEWERS="$(trim_ws "${INPUT_VIEWERS:-$DEF_VIEWERS}")"
  fi
elif [[ -z "$TARGET_PROJECTS" ]]; then
  [[ -n "$ACTIVE_PROJECT" ]] || die "pass --project PROJECT_ID (no active gcloud project)"
  TARGET_PROJECTS="$ACTIVE_PROJECT"
fi

# "none" clears a saved list of linked projects or viewers (it saves an empty value). An empty flag value is still
# refused: it is more often a mistake than a wish.
[[ "$(lower_trim "$CLI_LINK_PROJECTS")" == "none" ]] && CLI_LINK_PROJECTS="none"
[[ "$(lower_trim "$CLI_VIEWERS")" == "none" ]] && CLI_VIEWERS="none"

NORMALIZED_LINK_PROJECTS=""
if [[ -n "$CLI_LINK_PROJECTS" && "$CLI_LINK_PROJECTS" != "none" ]]; then
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
  # prepare.sh takes only the log collection flags (PREPARE_ARGS); a later --dataset-location there wins.
  if [[ "$PREP_ALL" == *,* ]]; then
    exec "$ROOT/scripts/prepare.sh" --projects "$PREP_ALL" --dataset-location "${CLI_REGION:-asia-northeast3}" ${PREPARE_ARGS[@]+"${PREPARE_ARGS[@]}"}
  else
    exec "$ROOT/scripts/prepare.sh" --project "$PREP_ALL" --dataset-location "${CLI_REGION:-asia-northeast3}" ${PREPARE_ARGS[@]+"${PREPARE_ARGS[@]}"}
  fi
fi

# Per-project settings snapshots (git-ignored via /*.env): scripts/deploy.sh --project PROJECT_ID reads
# deploy.PROJECT_ID.env, so a later run (of this installer or of deploy.sh) keeps what was chosen here.
# A new snapshot starts from the defaults; an existing one keeps every line (including lines added by hand) and only
# the settings given on this run are replaced or added.
[[ -n "$CLI_REGION" ]] && save_setting REGION "$CLI_REGION"
[[ -n "$CLI_ADMINS" ]] && save_setting ADMIN_EMAILS "$CLI_ADMINS"
if [[ "$CLI_VIEWERS" == "none" ]]; then
  save_setting ALLOWED_USERS ""
elif [[ -n "$CLI_VIEWERS" ]]; then
  save_setting ALLOWED_USERS "$CLI_VIEWERS"
fi
update_snapshot() {  # update_snapshot FILE KEY=VALUE...: replaces each KEY's line in FILE, or appends it
  python3 - "$@" <<'PY'
import os, re, shlex, sys
path, pairs = sys.argv[1], sys.argv[2:]
lines = []
if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
# Plain values keep the familiar KEY="value" form; anything bash would expand inside double quotes is single-quoted.
SAFE = re.compile(r"^[A-Za-z0-9 @._,:/*+=~%-]*$")
for pair in pairs:
    key, _, value = pair.partition("=")
    text = '%s="%s"' % (key, value) if SAFE.match(value) else "%s=%s" % (key, shlex.quote(value))
    pattern = re.compile(r"^\s*(?:export\s+)?%s=" % re.escape(key))
    hits = [i for i, line in enumerate(lines) if pattern.match(line)]
    if hits:
        lines[hits[0]] = text
        for i in reversed(hits[1:]):
            del lines[i]
    else:
        lines.append(text)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
os.replace(tmp, path)
PY
}
if [[ "$DRY_RUN" != "1" || "${ACP_SAVE_ENV_ON_DRY_RUN:-0}" == "1" ]]; then
  IFS=',' read -r -a SAVE_PROJECTS <<<"$TARGET_PROJECTS"
  for raw_pid in "${SAVE_PROJECTS[@]}"; do
    pid="$(normalize_project_id "$raw_pid")"
    [[ -n "$pid" ]] || continue
    validate_project_id "$pid" "--project"
    ENV_OUT="$ROOT/deploy.${pid}.env"
    PAIRS=("PROJECT_ID=${pid}")
    if [[ ! -f "$ENV_OUT" ]]; then
      printf '# AI Control Plane settings for %s, saved by scripts/install.sh (git-ignored).\n' "$pid" >"$ENV_OUT"
      printf '# scripts/install.sh and scripts/deploy.sh --project %s read them; flags on the command line win.\n' "$pid" >>"$ENV_OUT"
      NEW_REGION="${CLI_REGION:-asia-northeast3}"
      # No DATASET_LOCATION unless --dataset-location is given (saved below): deploy.sh puts the dataset in REGION, so
      # when an organization policy refuses the first region, a re-run with --region moves the dataset location too.
      PAIRS+=("SCAN_PROJECTS=" "REGION=${NEW_REGION}" "DEFAULT_LOCALE=ko"
        "WORKSPACE_TIMEZONE=Asia/Seoul" "ACCESS_MODE=google" "ADMIN_EMAILS=${CLI_ADMINS:-$ACCOUNT}"
        "ALLOWED_USERS=" "ENABLE_DATA_ACCESS_LOGS=true" "ENABLE_GE_OBSERVABILITY=true" "GE_PROMPT_LOGGING=true"
        "ENABLE_GOVERNANCE_ACTIONS=true")
    elif [[ -n "$CLI_REGION" ]]; then
      # Earlier installers saved DATASET_LOCATION equal to the region of that run. Such a value follows a new --region
      # (it is emptied, so deploy.sh uses REGION); a different saved location or --dataset-location is kept. An
      # existing dataset never moves: deploy.sh only creates a missing one.
      OLD_REGION="$(saved_setting "$pid" REGION)"
      OLD_DSLOC="$(saved_setting "$pid" DATASET_LOCATION)"
      if [[ -n "$OLD_DSLOC" && "$(lower_trim "$OLD_DSLOC")" == "$(lower_trim "$OLD_REGION")" ]]; then
        PAIRS+=("DATASET_LOCATION=")
      fi
    fi
    if [[ "$CLI_LINK_PROJECTS" == "none" ]]; then
      PAIRS+=("SCAN_PROJECTS=")
    elif [[ -n "$NORMALIZED_LINK_PROJECTS" ]]; then
      FILT_LINKS=()
      IFS=',' read -r -a _larr <<<"$NORMALIZED_LINK_PROJECTS"
      for _lp in "${_larr[@]}"; do
        [[ -n "$_lp" && "$_lp" != "$pid" ]] && FILT_LINKS+=("$_lp")
      done
      PER_PID_LINKS=""
      [[ ${#FILT_LINKS[@]} -gt 0 ]] && PER_PID_LINKS="$(IFS=','; echo "${FILT_LINKS[*]}")"
      PAIRS+=("SCAN_PROJECTS=${PER_PID_LINKS}")
    fi
    for i in ${SAVE_KEYS[@]+"${!SAVE_KEYS[@]}"}; do
      PAIRS+=("${SAVE_KEYS[$i]}=${SAVE_VALS[$i]}")
    done
    update_snapshot "$ENV_OUT" "${PAIRS[@]}"
  done
fi

# Only the settings given on this run are passed on: deploy.sh reads the rest from deploy.<project>.env, so a re-run
# without --region or --admins keeps the region and administrators chosen before.
DEPLOY_CMD=("$ROOT/scripts/deploy.sh")
if [[ "$TARGET_PROJECTS" == *,* ]]; then
  DEPLOY_CMD+=(--projects "$TARGET_PROJECTS")
else
  DEPLOY_CMD+=(--project "$TARGET_PROJECTS")
fi
[[ -n "$CLI_REGION" ]] && DEPLOY_CMD+=(--region "$CLI_REGION")
[[ -n "$CLI_ADMINS" ]] && DEPLOY_CMD+=(--admins "$CLI_ADMINS")
[[ -n "$CLI_VIEWERS" ]] && DEPLOY_CMD+=(--viewers "$CLI_VIEWERS")   # "none" clears them in deploy.sh as well
if [[ "$CLI_LINK_PROJECTS" == "none" ]]; then
  DEPLOY_CMD+=(--link-projects none)
elif [[ -n "$NORMALIZED_LINK_PROJECTS" ]]; then
  DEPLOY_CMD+=(--link-projects "$NORMALIZED_LINK_PROJECTS")
fi
[[ ${#EXTRA_ARGS[@]} -gt 0 ]] && DEPLOY_CMD+=("${EXTRA_ARGS[@]}")

exec "${DEPLOY_CMD[@]}"
