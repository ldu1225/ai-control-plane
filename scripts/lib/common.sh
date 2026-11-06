# shellcheck shell=bash
# Shared helpers for scripts/install.sh, scripts/deploy.sh and scripts/prepare.sh.
# Source this file; do not execute it directly.
#
# Callers set DRY_RUN (0 or 1) before calling run() or any ensure_* function. With DRY_RUN=1 commands
# that change anything are printed instead of executed.

log()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# Runs a command, or prints it with DRY_RUN=1.
run() {
  if [[ "${DRY_RUN:-0}" == "1" ]]; then printf '    [dry-run] %s\n' "$*" >&2; return 0; fi
  "$@"
}

# IAM bindings for a just-created service account can fail until it has propagated.
retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    [[ $n -ge 6 ]] && return 1
    sleep 10
  done
}

# Trims leading and trailing whitespace (including UTF-8 non-breaking spaces) from $1.
trim_ws() {
  local s="${1:-}" nbsp=$'\xc2\xa0'
  s="${s//"$nbsp"/ }"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# Normalizes CLI tokens pasted from email/messenger clients:
# - Converts leading Unicode em-dash (—), en-dash (–), horizontal bar (―), or minus (−) into --
# - Replaces non-breaking spaces with regular spaces and strips surrounding smart quotes (“ ” ‘ ’)
# - Splits --flag=value into (--flag value) so both "--project foo" and "--project=foo" work
NORMALIZED_ARGS=()
normalize_cli_args() {
  NORMALIZED_ARGS=()
  local tok k v d
  local em=$'\xe2\x80\x94' en=$'\xe2\x80\x93' hbar=$'\xe2\x80\x95' uminus=$'\xe2\x88\x92'
  local nbsp=$'\xc2\xa0' ldq=$'\xe2\x80\x9c' rdq=$'\xe2\x80\x9d' lsq=$'\xe2\x80\x98' rsq=$'\xe2\x80\x99'
  for tok in "$@"; do
    tok="${tok//"$nbsp"/ }"
    tok="${tok#"$ldq"}"; tok="${tok%"$rdq"}"
    tok="${tok#"$lsq"}"; tok="${tok%"$rsq"}"
    for d in "$em" "$en" "$hbar" "$uminus"; do
      if [[ "$tok" == "${d}${d}"* ]]; then
        tok="--${tok#"${d}${d}"}"
      elif [[ "$tok" == "${d}"* ]]; then
        tok="--${tok#"$d"}"
      fi
    done
    if [[ "$tok" == --*=* ]]; then
      k="${tok%%=*}"
      v="${tok#*=}"
      v="${v#"$ldq"}"; v="${v%"$rdq"}"
      v="${v#"$lsq"}"; v="${v%"$rsq"}"
      v="${v#\"}"; v="${v%\"}"
      v="${v#\'}"; v="${v%\'}"
      NORMALIZED_ARGS+=("$k" "$v")
    else
      NORMALIZED_ARGS+=("$tok")
    fi
  done
}

# Strips common copy-paste prefixes (such as "gcloud config set project ", "--project ", "project ",
# "projects/") and surrounding quotes/whitespace from a project ID, warning when a prefix was removed.
normalize_project_id() {
  local raw="${1:-}" cleaned prev
  local ldq=$'\xe2\x80\x9c' rdq=$'\xe2\x80\x9d' lsq=$'\xe2\x80\x98' rsq=$'\xe2\x80\x99'
  cleaned="$(trim_ws "$raw")"
  while :; do
    prev="$cleaned"
    cleaned="${cleaned#"$ldq"}"; cleaned="${cleaned%"$rdq"}"
    cleaned="${cleaned#"$lsq"}"; cleaned="${cleaned%"$rsq"}"
    cleaned="${cleaned#\"}"; cleaned="${cleaned%\"}"
    cleaned="${cleaned#\'}"; cleaned="${cleaned%\'}"
    cleaned="$(trim_ws "$cleaned")"
    if [[ "$cleaned" =~ ^[Gg][Cc][Ll][Oo][Uu][Dd][[:space:]]+[Cc][Oo][Nn][Ff][Ii][Gg][[:space:]]+[Ss][Ee][Tt][[:space:]]+[Pp][Rr][Oo][Jj][Ee][Cc][Tt][[:space:]]+(.+)$ ]]; then
      cleaned="${BASH_REMATCH[1]}"
    elif [[ "$cleaned" =~ ^--[Pp][Rr][Oo][Jj][Ee][Cc][Tt]([[:space:]]+|=)(.+)$ ]]; then
      cleaned="${BASH_REMATCH[2]}"
    elif [[ "$cleaned" =~ ^[Pp][Rr][Oo][Jj][Ee][Cc][Tt](_[Ii][Dd])?([[:space:]]+|=)(.+)$ ]]; then
      cleaned="${BASH_REMATCH[3]}"
    elif [[ "$cleaned" =~ ^[Pp][Rr][Oo][Jj][Ee][Cc][Tt][Ss]/(.+)$ ]]; then
      cleaned="${BASH_REMATCH[1]}"
    fi
    cleaned="$(trim_ws "$cleaned")"
    [[ "$cleaned" == "$prev" ]] && break
  done
  if [[ -n "$raw" && "$cleaned" != "$(trim_ws "$raw")" ]]; then
    warn "normalized PROJECT_ID '${raw}' -> '${cleaned}'"
  fi
  printf '%s' "$cleaned"
}

# Validates a Google Cloud project ID (6-30 lowercase letters, digits or hyphens, starting with a letter).
validate_project_id() {
  local pid="${1:-}" where="${2:-configuration}" lower
  [[ -n "$pid" && "$pid" != your-* && "$pid" != "PROJECT_ID" ]] || die "set PROJECT_ID in ${where}"
  if [[ "$pid" =~ [A-Z] ]]; then
    lower="$(printf '%s' "$pid" | tr '[:upper:]' '[:lower:]')"
    die "PROJECT_ID '${pid}' contains uppercase letters (looks like a project Name). Set the lowercase project ID instead (for example '${lower}')"
  fi
  [[ "$pid" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] \
    || die "invalid PROJECT_ID '${pid}' in ${where}: enter only the lowercase Google Cloud project ID (for example 'my-project-123'), without 'project ' or spaces"
}

# Builds a <=63-character default Cloud Storage bucket name from PROJECT_ID and SERVICE_NAME.
default_settings_bucket() {
  local pid="$1" svc="$2" candidate max_svc short_svc
  candidate="${pid}-${svc}-settings"
  if [[ ${#candidate} -le 63 ]]; then
    printf '%s' "$candidate"
    return 0
  fi
  max_svc=$((63 - ${#pid} - 10))
  short_svc="${svc:0:$max_svc}"
  short_svc="${short_svc%-}"
  printf '%s' "${pid}-${short_svc}-settings"
}

# Normalizes an IAM member entry for BRIDGE_MEMBERS / IAP_MEMBERS so bare emails ("alice@example.com"),
# wildcard domains ("*@example.com") or bare domains ("example.com") work without failing gcloud iap.
normalize_iam_member() {
  local m
  m="$(trim_ws "${1:-}")"
  [[ -n "$m" ]] || return 0
  case "$m" in
    allUsers|allAuthenticatedUsers|user:*|group:*|domain:*|serviceAccount:*|principal:*|principalSet:*)
      printf '%s' "$m" ;;
    \*@*)
      printf 'domain:%s' "${m#\*@}" ;;
    *@*)
      printf 'user:%s' "$m" ;;
    *.*)
      printf 'domain:%s' "$m" ;;
    *)
      printf '%s' "$m" ;;
  esac
}

# Formats an active gcloud account email as a user: or serviceAccount: IAM member.
caller_iam_member() {
  local acct
  acct="$(trim_ws "${1:-}")"
  if [[ "$acct" == *.gserviceaccount.com ]]; then
    printf 'serviceAccount:%s' "$acct"
  else
    printf 'user:%s' "$acct"
  fi
}

# iap_org_domain PROJECT
# Primary domain of the project's organization (the organization resource's display name). Prints nothing when the
# project has no organization or the caller may not read it (roles/resourcemanager.organizationViewer).
iap_org_domain() {
  local project="$1" org_id disp
  [[ "${DRY_RUN:-0}" == "1" ]] && return 0
  org_id="$(gcloud projects get-ancestors "$project" --format=json 2>/dev/null | python3 -c '
import json, sys
try:
    for item in json.load(sys.stdin):
        if item.get("type") == "organization" and item.get("id"):
            print(item["id"]); break
except Exception:
    pass
' 2>/dev/null || true)"
  [[ -n "$org_id" ]] || return 0
  disp="$(gcloud organizations describe "$org_id" --format='value(displayName)' 2>/dev/null || true)"
  disp="$(trim_ws "$disp" | tr '[:upper:]' '[:lower:]')"
  [[ "$disp" =~ ^[a-z0-9-]+(\.[a-z0-9-]+)+$ ]] && printf '%s' "$disp"
  return 0
}

# configure_iap_access PROJECT PROJECT_NUMBER REGION SERVICE MODE [EXTRA_MEMBERS]
# Lets Google accounts through Identity-Aware Proxy on SERVICE and reports exactly who IAP admits. IAP stops every
# account that roles/iap.httpsResourceAccessor does not cover on Google's own page ("You don't have access"), before
# the dashboard can show or record anything, so a refused binding must never pass silently.
#   MODE bridge (the Google sign-in bridge; the dashboard approves people afterwards):
#     - no domain restricted sharing: allAuthenticatedUsers (with IAP's Google-managed OAuth client this still means
#       accounts of the project's organization only);
#     - domain restricted sharing, or allAuthenticatedUsers refused: domain: bindings for the organization's domain and
#       for the domains of ADMIN_EMAILS / ALLOWED_USERS, of people on the project IAM policy and of Gemini Enterprise
#       license holders (public mail domains and google.com excluded).
#   MODE gate (the dashboard itself behind IAP; everyone IAP admits is a viewer): an allowlist only, no organization-
#     wide bindings.
#   Both: user: bindings for ADMIN_EMAILS / ALLOWED_USERS (domain: for *@domain entries) and EXTRA_MEMBERS
#   (BRIDGE_MEMBERS / IAP_MEMBERS). Sets IAP_OAUTH_CLIENT (managed | custom | unknown) and appends a block to
#   IAP_REPORT (printed by iap_access_report at the end of the deployment).
# Reads ADMIN_EMAILS, ALLOWED_USERS, ORGPOL_DRS, IAP_ORG_DOMAIN, DISCOVERY_ENGINE_LOCATIONS and DRY_RUN.
IAP_REPORT=()
configure_iap_access() {
  local project="$1" number="$2" region="$3" service="$4" mode="$5" extra="${6:-}" out kind a b
  local admitted="" refused="" failed="" readback=0 human
  out="$(ACP_IAP_ADMINS="${ADMIN_EMAILS:-}" ACP_IAP_VIEWERS="${ALLOWED_USERS:-}" ACP_IAP_EXTRA="$extra" \
    ACP_IAP_MODE="$mode" ACP_IAP_DRS="${ORGPOL_DRS:-0}" ACP_IAP_ORG_DOMAIN="${IAP_ORG_DOMAIN:-}" \
    ACP_IAP_GE_LOCATIONS="${DISCOVERY_ENGINE_LOCATIONS:-global}" ACP_IAP_DRY="${DRY_RUN:-0}" \
    python3 - "$project" "$number" "$region" "$service" <<'PYIAP'
import json, os, re, subprocess, sys, time, urllib.parse, urllib.request

project, number, region, service = sys.argv[1:5]
mode = os.environ.get("ACP_IAP_MODE") or "bridge"
dry = os.environ.get("ACP_IAP_DRY") == "1"
drs = os.environ.get("ACP_IAP_DRS") == "1"
ROLE = "roles/iap.httpsResourceAccessor"
SKIP = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "icloud.com", "me.com",
        "aol.com", "proton.me", "protonmail.com", "naver.com", "daum.net", "hanmail.net", "kakao.com", "nate.com",
        "google.com"}
EMAIL = re.compile(r"^[a-z0-9._%+'\-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)$")
POLICY = re.compile(r"permitted customer|allowedPolicyMemberDomains|allowedPolicyMembers|organization polic", re.I)
TRANSIENT = re.compile(r"NOT_FOUND|not found|UNAVAILABLE|DEADLINE|try again|ABORTED|concurrent|etag", re.I)


def gc(*args, timeout=120):
    try:
        r = subprocess.run(["gcloud", *args, "--quiet"], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except Exception as err:  # pylint: disable=broad-except
        return 1, "", str(err)


def emit(*fields):
    print("|".join(str(f).replace("|", "/").replace("\n", " ") for f in fields), flush=True)


def domain_of(value):
    value = (value or "").strip().lower()
    if value.startswith("*@"):
        value = "x" + value[1:]
    m = EMAIL.match(value)
    return m.group(1) if m else ""


def usable(domain):
    return bool(domain) and domain not in SKIP and not domain.endswith("gserviceaccount.com")


entries = [t for t in re.split(r"[,\s]+", (os.environ.get("ACP_IAP_ADMINS") or "") + "," +
                               (os.environ.get("ACP_IAP_VIEWERS") or "")) if t.strip()]
explicit, entry_domains = [], []
for raw in entries:
    token = raw.strip().lower()
    dom = domain_of(token)
    if not dom:
        continue
    principal = "domain:" + dom if token.startswith("*@") else "user:" + token
    if principal not in explicit:
        explicit.append(principal)
    if usable(dom) and dom not in entry_domains:
        entry_domains.append(dom)
for raw in re.split(r"[,\s]+", os.environ.get("ACP_IAP_EXTRA") or ""):
    member = raw.strip()
    if not member:
        continue
    if not re.match(r"^(allUsers|allAuthenticatedUsers|user:|group:|domain:|serviceAccount:|principal)", member):
        member = ("domain:" + member[2:]) if member.startswith("*@") else \
                 ("user:" + member) if "@" in member else ("domain:" + member)
    if member == "allUsers" or (member == "allAuthenticatedUsers" and drs):
        continue  # never public; allAuthenticatedUsers is refused under domain restricted sharing
    if member not in explicit:
        explicit.append(member)

done = set()


def bind(principal):
    if principal in done:
        return "dup"
    done.add(principal)
    if dry:
        emit("DRY", principal)
        return "dry"
    text = ""
    for attempt in range(3):
        rc, so, se = gc("iap", "web", "add-iam-policy-binding", "--resource-type=cloud-run", "--service=" + service,
                        "--region=" + region, "--project=" + project, "--member=" + principal, "--role=" + ROLE,
                        "--condition=None")
        if rc == 0:
            emit("OK", principal)
            return "ok"
        text = (se or so or "").strip()
        if POLICY.search(text):
            emit("POLICY", principal)
            return "policy"
        if attempt < 2 and TRANSIENT.search(text):
            time.sleep(6)
            continue
        break
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    emit("FAIL", principal, (lines[-1] if lines else "error")[:240])
    return "fail"


def project_domains():
    rc, so, _ = gc("projects", "get-iam-policy", project, "--format=json")
    found = []
    if rc != 0:
        return found
    try:
        policy = json.loads(so[so.index("{"):]) if "{" in so else {}
    except ValueError:
        return found
    for binding in policy.get("bindings") or []:
        for member in binding.get("members") or []:
            if member.startswith(("user:", "group:")):
                dom = domain_of(member.split(":", 1)[1])
                if usable(dom) and dom not in found:
                    found.append(dom)
    return found


def license_domains():
    if dry:
        return []
    rc, token, _ = gc("auth", "print-access-token", timeout=30)
    token = token.strip()
    if rc != 0 or not token:
        return []
    counts = {}
    for loc in [l.strip() for l in (os.environ.get("ACP_IAP_GE_LOCATIONS") or "global").split(",") if l.strip()]:
        host = ("https://discoveryengine.googleapis.com" if loc == "global"
                else "https://%s-discoveryengine.googleapis.com" % loc)
        url = "%s/v1alpha/projects/%s/locations/%s/userStores/default_user_store/userLicenses?pageSize=500" % (
            host, project, loc)
        page = ""
        for _ in range(4):
            req = urllib.request.Request(url + ("&pageToken=" + urllib.parse.quote(page) if page else ""), headers={
                "Authorization": "Bearer " + token, "X-Goog-User-Project": project})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.load(resp)
            except Exception:  # pylint: disable=broad-except
                break
            for item in data.get("userLicenses") or []:
                dom = domain_of(item.get("userPrincipal") or "")
                if usable(dom):
                    counts[dom] = counts.get(dom, 0) + 1
            page = data.get("nextPageToken") or ""
            if not page:
                break
    return sorted(counts, key=lambda d: (-counts[d], d))


opened = False
if mode == "bridge" and not drs:
    opened = bind("allAuthenticatedUsers") in ("ok", "dry")
for principal in explicit:
    bind(principal)
if mode == "bridge" and not opened:
    candidates = []
    for dom in [(os.environ.get("ACP_IAP_ORG_DOMAIN") or "").strip().lower()] + entry_domains + project_domains() + \
            license_domains():
        if usable(dom) and dom not in candidates:
            candidates.append(dom)
    for dom in candidates[:20]:
        bind("domain:" + dom)

if not dry:
    rc, so, _ = gc("iap", "web", "get-iam-policy", "--resource-type=cloud-run", "--service=" + service,
                   "--region=" + region, "--project=" + project, "--format=json")
    if rc == 0:
        try:
            policy = json.loads(so[so.index("{"):]) if "{" in so else {}
            members = sorted({m for b in policy.get("bindings") or [] if b.get("role") == ROLE
                              for m in b.get("members") or []})
            emit("MEMBERS", ",".join(members))
        except ValueError:
            pass
    custom, unreadable = False, False
    for scope in (["--resource-type=cloud-run", "--service=" + service, "--region=" + region], []):
        rc, so, _ = gc("iap", "settings", "get", "--project=" + project, *scope, "--format=json")
        if rc != 0:
            unreadable = True
            continue
        try:
            data = json.loads(so[so.index("{"):]) if "{" in so else {}
        except ValueError:
            data = {}
        if ((data.get("accessSettings") or {}).get("oauthSettings") or {}).get("clientId"):
            custom = True
            break
    emit("OAUTH", "custom" if custom else ("unknown" if unreadable else "managed"))
PYIAP
)" || true
  while IFS='|' read -r kind a b; do
    case "$kind" in
      OK) info "IAP access (${service}): ${a}" ;;
      DRY) info "[dry-run] IAP access (${service}): ${a}" ;;
      POLICY) refused+="${refused:+, }${a}"
        warn "IAP access (${service}): ${a} refused by an organization policy (domain restricted sharing)" ;;
      FAIL) failed+="${failed:+, }${a}"; warn "IAP access (${service}): ${a} not granted: ${b}" ;;
      MEMBERS) admitted="$a"; readback=1 ;;
      OAUTH) IAP_OAUTH_CLIENT="$a" ;;
    esac
  done <<<"$out"
  [[ "${DRY_RUN:-0}" == "1" ]] && return 0
  human="$(printf '%s' "$admitted" | tr ',' '\n' | grep -v '^serviceAccount:' | paste -sd, - | sed 's/,/, /g' || true)"
  IAP_REPORT+=("Google 로그인(IAP) 점검: ${service}")
  if [[ "$readback" != "1" ]]; then
    IAP_REPORT+=("  통과 허용   : 확인 불가 (IAP 정책을 읽지 못함)")
  elif [[ -z "$human" ]]; then
    IAP_REPORT+=("  통과 허용   : 없음. 지금은 아무 Google 계정도 IAP를 통과할 수 없습니다")
  else
    IAP_REPORT+=("  통과 허용   : ${human}")
  fi
  [[ -n "$refused" ]] && IAP_REPORT+=("  조직 정책 거부: ${refused} (도메인 제한 공유)")
  [[ -n "$failed" ]] && IAP_REPORT+=("  실패        : ${failed}")
  case "${IAP_OAUTH_CLIENT:-unknown}" in
    custom) IAP_REPORT+=("  OAuth       : 맞춤 OAuth 클라이언트. IAP가 허용한 조직 밖 계정도 Google 로그인 가능") ;;
    managed) IAP_REPORT+=("  OAuth       : Google 관리형. 이 프로젝트 조직${IAP_ORG_DOMAIN:+(${IAP_ORG_DOMAIN})} 계정만 Google 로그인 가능")
      IAP_REPORT+=("                조직 밖 계정(협력사, 계열사 다른 조직, 개인 Gmail)은 IAP 화면에서 막힙니다") ;;
    *) IAP_REPORT+=("  OAuth       : 확인 불가. 기본값(Google 관리형)이면 이 프로젝트 조직 계정만 Google 로그인 가능") ;;
  esac
  if [[ "$mode" == "bridge" && -n "${OAUTH_CONNECT_SECRET:-}" ]]; then
    IAP_REPORT+=("  조직 밖 계정 : 설정 > 접근 제어 > 'Google OAuth 클라이언트' 연결 (재배포 없음, README 5.4)")
    IAP_REPORT+=("                리디렉션 URI ${BASE_URL:-<대시보드 주소>}/auth/google/callback. 또는 ID/비밀번호 계정 발급")
  else
    IAP_REPORT+=("  조직 밖 계정 : 대시보드 설정 > 접근 제어에서 ID/비밀번호 계정 발급 (README 5.4)")
  fi
  IAP_REPORT+=("  허용 추가   : 대시보드 설정 > 접근 제어 > Google 로그인(IAP), 또는")
  IAP_REPORT+=("    gcloud iap web add-iam-policy-binding --resource-type=cloud-run --service=${service} --region=${region} --project=${project} --member=domain:<회사 도메인> --role=roles/iap.httpsResourceAccessor")
  IAP_REPORT+=("  반영까지 몇 분 걸릴 수 있습니다.")
  return 0
}

# iap_access_report: prints the Google sign-in (IAP) blocks collected by configure_iap_access.
iap_access_report() {
  local line
  for line in ${IAP_REPORT[@]+"${IAP_REPORT[@]}"}; do
    info "$line"
  done
}

# Ensures gcloud has an authenticated active account with a valid access token; runs `gcloud auth login`
# interactively when needed. Prints the active account email to stdout.
ensure_gcloud_auth() {
  local acct
  acct="$(trim_ws "$(gcloud config get-value account 2>/dev/null || true)")"
  [[ "$acct" == "(unset)" ]] && acct=""
  if [[ -z "$acct" ]]; then
    if [[ -t 0 && "${DRY_RUN:-0}" != "1" ]]; then
      info "no active gcloud account found; starting interactive sign-in..." >&2
      gcloud auth login --quiet >&2 || die "gcloud authentication failed"
      acct="$(trim_ws "$(gcloud config get-value account 2>/dev/null || true)")"
      [[ "$acct" == "(unset)" ]] && acct=""
    fi
  fi
  [[ -n "$acct" ]] || die "no active gcloud account (run: gcloud auth login)"
  if [[ "${DRY_RUN:-0}" != "1" ]] && ! gcloud auth print-access-token >/dev/null 2>&1; then
    if [[ -t 0 ]]; then
      info "gcloud session for ${acct} has expired; starting interactive re-authentication..." >&2
      gcloud auth login "$acct" --quiet >&2 || die "gcloud authentication failed for ${acct}"
    else
      die "gcloud session for ${acct} has expired (run: gcloud auth login ${acct})"
    fi
  fi
  printf '%s' "$acct"
}

# Enables the foundational control-plane & billing APIs first so projects.testIamPermissions, IAM inspection,
# Cloud Billing checks and batch API enabling never fail on a brand-new empty project.
bootstrap_foundation_apis() {
  local project="$1"
  [[ "${DRY_RUN:-0}" == "1" ]] && return 0
  gcloud services enable \
    serviceusage.googleapis.com \
    cloudresourcemanager.googleapis.com \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    cloudbilling.googleapis.com \
    --project "$project" --quiet >/dev/null 2>&1 || true
}

# Enables all required and extended AI/governance APIs with per-service fallback so a single unavailable
# preview service or org restriction never aborts the entire batch.
# Usage: enable_project_apis PROJECT NEED_IAP NEED_SCHEDULER [PREPARE_ONLY]
enable_project_apis() {
  local project="$1" need_iap="${2:-1}" need_sched="${3:-0}" prepare_only="${4:-0}"
  local core_apis=() optional_apis=() api
  if [[ "$prepare_only" == "1" ]]; then
    core_apis=(
      serviceusage.googleapis.com
      cloudresourcemanager.googleapis.com
      iam.googleapis.com
      logging.googleapis.com
      bigquery.googleapis.com
      monitoring.googleapis.com
      discoveryengine.googleapis.com
      aiplatform.googleapis.com
    )
    optional_apis=(
      modelarmor.googleapis.com
      dlp.googleapis.com
      generativelanguage.googleapis.com
      geminidataanalytics.googleapis.com
      cloudtrace.googleapis.com
      cloudasset.googleapis.com
      bigquerydatatransfer.googleapis.com
    )
  else
    core_apis=(
      serviceusage.googleapis.com
      cloudresourcemanager.googleapis.com
      iam.googleapis.com
      iamcredentials.googleapis.com
      sts.googleapis.com
      run.googleapis.com
      artifactregistry.googleapis.com
      bigquery.googleapis.com
      logging.googleapis.com
      monitoring.googleapis.com
      cloudtrace.googleapis.com
      clouderrorreporting.googleapis.com
      discoveryengine.googleapis.com
      aiplatform.googleapis.com
      storage.googleapis.com
      secretmanager.googleapis.com
    )
    # Cloud Build, IAP, Cloud Scheduler and Cloud Billing Catalog have automatic runtime fallbacks
    # (local Docker build, ID/Password login, manual enforcement, and BigQuery/manual token pricing),
    # so restricted billing/build/IAP APIs never abort deployment.
    optional_apis=(
      cloudbilling.googleapis.com
      cloudbuild.googleapis.com
      iap.googleapis.com
      cloudscheduler.googleapis.com
      modelarmor.googleapis.com
      dlp.googleapis.com
      generativelanguage.googleapis.com
      geminidataanalytics.googleapis.com
      cloudasset.googleapis.com
      bigquerydatatransfer.googleapis.com
      orgpolicy.googleapis.com
    )
    [[ "$need_iap" == "1" ]] && core_apis+=(iap.googleapis.com)
    [[ "$need_sched" == "1" ]] && core_apis+=(cloudscheduler.googleapis.com)
    core_apis+=(cloudbuild.googleapis.com)
  fi

  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    run gcloud services enable "${core_apis[@]}" "${optional_apis[@]}" --project "$project"
    return 0
  fi

  if ! gcloud services enable "${core_apis[@]}" --project "$project" --quiet >/dev/null 2>&1; then
    for api in "${core_apis[@]}"; do
      if ! gcloud services enable "$api" --project "$project" --quiet >/dev/null 2>&1; then
        case "$api" in
          cloudbilling.googleapis.com|cloudbuild.googleapis.com|iap.googleapis.com|cloudscheduler.googleapis.com|cloudtrace.googleapis.com|clouderrorreporting.googleapis.com|sts.googleapis.com)
            warn "could not enable ${api} on ${project} (continuing with built-in fallback)" ;;
          discoveryengine.googleapis.com|aiplatform.googleapis.com|monitoring.googleapis.com)
            warn "could not enable ${api} on ${project} (organization policy or quota); the related dashboard pages show no data until it is enabled" ;;
          *)
            gcloud services enable "$api" --project "$project" \
              || die "could not enable required API ${api} on ${project}" ;;
        esac
      fi
    done
  fi

  if ! gcloud services enable "${optional_apis[@]}" --project "$project" --quiet >/dev/null 2>&1; then
    for api in "${optional_apis[@]}"; do
      gcloud services enable "$api" --project "$project" --quiet >/dev/null 2>&1 || true
    done
  fi
}

# Emits a normalized XXXXXX-XXXXXX-XXXXXX Cloud Billing account ID, or nothing when masked/hidden by an MSP/reseller.
_sanitize_billing_account_id() {
  local raw
  raw="$(trim_ws "${1:-}")"
  raw="${raw#billingAccounts/}"
  raw="$(printf '%s' "$raw" | tr '[:lower:]' '[:upper:]')"
  if [[ "$raw" =~ ^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$ ]]; then
    printf '%s' "$raw"
  fi
}

# Verifies that Cloud Billing is linked to PROJECT without requiring Billing Account permissions when billing
# is already attached by a central Cloud/MSP team.
# Usage: ensure_project_billing PROJECT [PREFERRED_BILLING_ACCOUNT_ID]
# Prints the linked billing account ID (XXXXXX-XXXXXX-XXXXXX) on stdout only when readable and valid.
ensure_project_billing() {
  local project="$1" preferred_ba="${2:-}" info_json enabled acct_name candidates=() cand parent_info parent_id sib_list sib sib_ba open_accts count link_err probe_err last_err=""
  [[ "${DRY_RUN:-0}" == "1" ]] && return 0
  preferred_ba="$(_sanitize_billing_account_id "$preferred_ba")"
  info_json="$(gcloud billing projects describe "$project" --format=json 2>/dev/null || true)"
  if [[ -z "$info_json" ]]; then
    # Caller has no Cloud Billing read visibility (common in MSP / enterprise orgs) — continue without failing.
    [[ -n "$preferred_ba" ]] && printf '%s' "$preferred_ba"
    return 0
  fi
  enabled="$(python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
    val = d.get("billingEnabled", None) if isinstance(d, dict) else None
    print("true" if val is True else ("false" if val is False else "unknown"))
except Exception:
    print("unknown")
' "$info_json" 2>/dev/null || echo "unknown")"
  acct_name="$(python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
    print((d.get("billingAccountName") or "").rsplit("/", 1)[-1] if isinstance(d, dict) else "")
except Exception:
    print("")
' "$info_json" 2>/dev/null || true)"
  acct_name="$(_sanitize_billing_account_id "$acct_name")"
  [[ -z "$acct_name" && -n "$preferred_ba" ]] && acct_name="$preferred_ba"

  if [[ "$enabled" == "false" ]]; then
    [[ -n "$preferred_ba" ]] && candidates+=("$preferred_ba")
    parent_info="$(gcloud projects describe "$project" --format='value(parent.id,parent.type)' 2>/dev/null || true)"
    parent_id="${parent_info%%$'\t'*}"
    if [[ -n "$parent_id" && "$parent_info" == *$'\t'folder* ]]; then
      sib_list="$(gcloud projects list --filter="parent.id=${parent_id} AND lifecycleState=ACTIVE" --format='value(projectId)' --limit=6 2>/dev/null || true)"
      while IFS= read -r sib; do
        [[ -n "$sib" && "$sib" != "$project" ]] || continue
        sib_ba="$(_sanitize_billing_account_id "$(gcloud billing projects describe "$sib" --format='value(billingAccountName)' 2>/dev/null || true)")"
        if [[ -n "$sib_ba" && " ${candidates[*]-} " != *" ${sib_ba} "* ]]; then
          candidates+=("$sib_ba")
          break
        fi
      done <<<"$sib_list"
    fi
    open_accts="$(gcloud billing accounts list --filter="open=true" --format="value(name)" 2>/dev/null || true)"
    count="$(printf '%s\n' "$open_accts" | sed '/^[[:space:]]*$/d' | wc -l | tr -d '[:space:]')"
    if [[ "$count" == "1" ]]; then
      cand="$(_sanitize_billing_account_id "$(printf '%s\n' "$open_accts" | head -n 1)")"
      [[ -n "$cand" && " ${candidates[*]-} " != *" ${cand} "* ]] && candidates+=("$cand")
    fi

    link_err="$(mktemp)"
    for cand in ${candidates[@]+"${candidates[@]}"}; do
      info "linking billing account ${cand} to ${project}..." >&2
      if gcloud billing projects link "$project" --billing-account="$cand" --quiet >/dev/null 2>"$link_err"; then
        acct_name="$cand"
        enabled="true"
        break
      fi
      last_err="$(tr '\n' ' ' <"$link_err" | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"
    done
    rm -f "$link_err"

    # Before failing, probe whether a billable service (run.googleapis.com) can actually be enabled:
    # under MSP/reseller billing or restricted quota projects, `billing projects describe` may report false
    # even when the project already has active billing attached.
    if [[ "$enabled" == "false" ]]; then
      probe_err="$(mktemp)"
      if gcloud services enable run.googleapis.com --project "$project" --quiet >/dev/null 2>"$probe_err"; then
        enabled="true"
      else
        [[ -n "$last_err" ]] || last_err="$(tr '\n' ' ' <"$probe_err" | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"
      fi
      rm -f "$probe_err"
    fi

    if [[ "$enabled" == "false" ]]; then
      die "Cloud Billing is not enabled on project ${project}${last_err:+ (${last_err})}.
  Link an active billing account in Google Cloud Console (Billing > Link a billing account) or pass --billing-account ACCOUNT_ID, then re-run.
  [안내] 배포 실행 계정에 빌링 열람/관리 권한은 없어도 되지만, 프로젝트 자체에는 결제 계정(Billing Account)이 연결되어 있어야 Cloud Run 활성화가 가능합니다. 클라우드/빌링 담당자에게 '${project}' 프로젝트 결제 계정 연결만 요청하신 뒤 동일 명령어를 다시 실행해 주세요."
    fi
  fi
  printf '%s' "$acct_name"
}

# Scans BigQuery datasets in PROJECT for a Cloud Billing export table (detailed resource export preferred,
# standard export fallback). Uses REST first and falls back to `bq ls` on mTLS-protected workstations.
# Prints "DATASET|TABLE" when found, or nothing.
auto_detect_billing_export() {
  local project="$1" token out="" ds_list ds tbl_json found
  [[ "${DRY_RUN:-0}" == "1" ]] && return 0
  token="$(gcloud auth print-access-token 2>/dev/null || true)"
  if [[ -n "$token" ]]; then
    out="$(ACP_ACCESS_TOKEN="$token" python3 - "$project" <<'PYBILL' 2>/dev/null || true
import json, os, sys, urllib.request

project = sys.argv[1]
token = os.environ["ACP_ACCESS_TOKEN"]
base = "https://bigquery.googleapis.com/bigquery/v2/projects/%s/datasets" % project


def get(url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


try:
    datasets = (get(base + "?maxResults=200").get("datasets") or [])
except Exception:  # pylint: disable=broad-except
    sys.exit(2)

ordered = sorted(
    [d.get("datasetReference", {}).get("datasetId", "") for d in datasets],
    key=lambda name: (0 if "billing" in name.lower() else 1, name),
)
fallback = ""
for ds in ordered:
    if not ds:
        continue
    try:
        tables = (get("%s/%s/tables?maxResults=200" % (base, ds)).get("tables") or [])
    except Exception:  # pylint: disable=broad-except
        continue
    for t in tables:
        tid = t.get("tableReference", {}).get("tableId", "")
        if tid.startswith("gcp_billing_export_resource_v1_"):
            print("%s|%s" % (ds, tid))
            sys.exit(0)
        if tid.startswith("gcp_billing_export_v1_") and not fallback:
            fallback = "%s|%s" % (ds, tid)
if fallback:
    print(fallback)
PYBILL
)"
    if [[ -n "$out" ]]; then
      printf '%s\n' "$out"
      return 0
    fi
  fi
  ds_list="$(bq --project_id="$project" ls --format=json 2>/dev/null || true)"
  [[ -n "$ds_list" && "$ds_list" == *"["* ]] || return 0
  for ds in $(python3 -c '
import json, sys
try:
    raw = sys.argv[1]
    items = json.loads(raw[raw.index("["):]) or []
    names = [d.get("datasetReference", {}).get("datasetId", "") for d in items]
    for n in sorted([x for x in names if x], key=lambda s: (0 if "billing" in s.lower() else 1, s)):
        print(n)
except Exception:
    pass
' "$ds_list"); do
    tbl_json="$(bq --project_id="$project" ls --max_results=200 --format=json "${project}:${ds}" 2>/dev/null || true)"
    [[ -n "$tbl_json" && "$tbl_json" == *"["* ]] || continue
    found="$(python3 -c '
import json, sys
ds, raw = sys.argv[1], sys.argv[2]
fallback = ""
for t in (json.loads(raw[raw.index("["):]) or []):
    tid = t.get("tableReference", {}).get("tableId", "")
    if tid.startswith("gcp_billing_export_resource_v1_"):
        print("%s|%s" % (ds, tid)); sys.exit(0)
    if tid.startswith("gcp_billing_export_v1_") and not fallback:
        fallback = "%s|%s" % (ds, tid)
if fallback:
    print(fallback)
' "$ds" "$tbl_json" 2>/dev/null || true)"
    if [[ -n "$found" ]]; then
      printf '%s\n' "$found"
      return 0
    fi
  done
}

# Detects a human-friendly organization display name from PROJECT's ancestor GCP Organization or Project Name.
auto_detect_org_name() {
  local project="$1" org_id disp proj_name
  [[ "${DRY_RUN:-0}" == "1" ]] && return 0
  org_id="$(gcloud projects get-ancestors "$project" --format=json 2>/dev/null | python3 -c '
import json, sys
try:
    for item in json.load(sys.stdin):
        if item.get("type") == "organization" and item.get("id"):
            print(item["id"]); break
except Exception:
    pass
' 2>/dev/null || true)"
  if [[ -n "$org_id" ]]; then
    disp="$(gcloud organizations describe "$org_id" --format='value(displayName)' 2>/dev/null || true)"
    disp="$(trim_ws "${disp%.*}")"
    if [[ -n "$disp" ]]; then
      printf '%s' "$disp"
      return 0
    fi
  fi
  proj_name="$(gcloud projects describe "$project" --format='value(name)' 2>/dev/null || true)"
  proj_name="$(trim_ws "$proj_name")"
  [[ -n "$proj_name" ]] && printf '%s' "$proj_name"
}

# Probes Agent Platform endpoints to pick reachable AGENT_ENGINE_REGIONS and AI_LOCATION automatically.
# Prints "AGENT_ENGINE_REGIONS|AI_LOCATION".
auto_detect_ai_locations() {
  local project="$1" region="$2" token
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    printf '%s|global' "$region"
    return 0
  fi
  token="$(gcloud auth print-access-token 2>/dev/null || true)"
  if [[ -z "$token" ]]; then
    printf '%s|global' "$region"
    return 0
  fi
  ACP_ACCESS_TOKEN="$token" python3 - "$project" "$region" <<'PYAI' || printf '%s|global' "$region"
import os, sys, urllib.error, urllib.request

project, region = sys.argv[1], sys.argv[2]
token = os.environ["ACP_ACCESS_TOKEN"]


def ok(url):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "X-Goog-User-Project": project,
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as err:
        # 404 or 400 on reasoningEngines or publishers means endpoint/location constraint; 200 means reachable.
        return err.code in (200,)
    except Exception:  # pylint: disable=broad-except
        return False


ae_candidates = [region] + (["us-central1"] if region != "us-central1" else [])
ae_ok = []
for cand in ae_candidates:
    if ok("https://%s-aiplatform.googleapis.com/v1/projects/%s/locations/%s/reasoningEngines?pageSize=1"
          % (cand, project, cand)):
        ae_ok.append(cand)
ae_regions = ",".join(ae_ok) if ae_ok else region

ai_loc = "global"
if not ok("https://aiplatform.googleapis.com/v1beta1/publishers/google/models?pageSize=1"):
    if ok("https://%s-aiplatform.googleapis.com/v1beta1/publishers/google/models?pageSize=1" % region):
        ai_loc = region

print("%s|%s" % (ae_regions, ai_loc))
PYAI
}

# _check_project_permissions MODE PROJECT NEED_BUILD NEED_SINK NEED_IAP NEED_SCHEDULER NEED_AUDIT_ADMIN
# Calls Cloud Resource Manager projects.testIamPermissions and prints a single-line JSON verdict:
#   status="ok"         every required permission is already present
#   status="bootstrap"  some operational permissions are missing, but caller has setIamPolicy so script can self-grant
#   status="denied"     caller lacks required permissions and lacks setIamPolicy to grant them
#   status="skip"       token unavailable or transient network failure (caller continues normally)
_check_project_permissions() {
  local token
  token="$(gcloud auth print-access-token 2>/dev/null || true)"
  if [[ -z "$token" ]]; then
    printf '{"status":"skip","reason":"no_token"}\n'
    return 0
  fi
  ACP_ACCESS_TOKEN="$token" python3 - "$@" <<'PYPREFLIGHT'
import json, os, sys, urllib.error, urllib.request

mode, project = sys.argv[1], sys.argv[2]
need_build = sys.argv[3] == "1"
need_sink = sys.argv[4] == "1"
need_iap = sys.argv[5] == "1"
need_scheduler = sys.argv[6] == "1"
need_audit_admin = sys.argv[7] == "1"
has_local_docker = os.environ.get("ACP_HAS_LOCAL_DOCKER") == "1"
token = os.environ["ACP_ACCESS_TOKEN"]

# Ordered pairs of (permission, minimal predefined role that grants it).
checks = [("serviceusage.services.enable", "roles/serviceusage.serviceUsageAdmin")]
if mode == "prepare":
    if need_sink:
        checks += [
            ("bigquery.datasets.create", "roles/bigquery.admin"),
            ("logging.sinks.create", "roles/logging.configWriter"),
        ]
    if need_audit_admin:
        checks += [
            ("resourcemanager.projects.getIamPolicy", "roles/resourcemanager.projectIamAdmin"),
            ("resourcemanager.projects.setIamPolicy", "roles/resourcemanager.projectIamAdmin"),
        ]
else:
    checks += [
        ("resourcemanager.projects.getIamPolicy", "roles/resourcemanager.projectIamAdmin"),
        ("resourcemanager.projects.setIamPolicy", "roles/resourcemanager.projectIamAdmin"),
        ("serviceusage.services.use", "roles/serviceusage.serviceUsageConsumer"),
        ("iam.serviceAccounts.create", "roles/iam.serviceAccountAdmin"),
        ("iam.serviceAccounts.setIamPolicy", "roles/iam.serviceAccountAdmin"),
        ("iam.serviceAccounts.actAs", "roles/iam.serviceAccountUser"),
        ("storage.buckets.create", "roles/storage.admin"),
        ("storage.buckets.setIamPolicy", "roles/storage.admin"),
        ("secretmanager.secrets.create", "roles/secretmanager.admin"),
        ("secretmanager.secrets.setIamPolicy", "roles/secretmanager.admin"),
        ("run.services.create", "roles/run.admin"),
        ("run.services.setIamPolicy", "roles/run.admin"),
    ]
    if need_build:
        checks += [
            ("cloudbuild.builds.create", "roles/cloudbuild.builds.editor"),
            ("artifactregistry.repositories.create", "roles/artifactregistry.admin"),
            ("artifactregistry.repositories.uploadArtifacts", "roles/artifactregistry.admin"),
        ]
    if need_sink:
        checks += [
            ("bigquery.datasets.create", "roles/bigquery.admin"),
            ("logging.sinks.create", "roles/logging.configWriter"),
        ]
    if need_iap:
        checks += [("iap.web.setIamPolicy", "roles/iap.admin")]
    if need_scheduler:
        checks += [("cloudscheduler.jobs.create", "roles/cloudscheduler.admin")]

all_perms = []
for perm, _ in checks:
    if perm not in all_perms:
        all_perms.append(perm)
if "resourcemanager.projects.setIamPolicy" not in all_perms:
    probe_perms = all_perms + ["resourcemanager.projects.setIamPolicy"]
else:
    probe_perms = list(all_perms)

url = "https://cloudresourcemanager.googleapis.com/v1/projects/%s:testIamPermissions" % project


def test_batch(perms):
    req = urllib.request.Request(
        url,
        data=json.dumps({"permissions": perms}).encode("utf-8"),
        method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return set(json.load(resp).get("permissions") or [])


granted = set()
valid_perms = set(probe_perms)
try:
    granted = test_batch(probe_perms)
except urllib.error.HTTPError as err:
    if err.code == 400:
        # One or more permission strings are not registered on Project in this environment; test individually.
        valid_perms = set()
        for p in probe_perms:
            try:
                got = test_batch([p])
                valid_perms.add(p)
                granted |= got
            except urllib.error.HTTPError as sub:
                if sub.code != 400:
                    print(json.dumps({"status": "skip", "reason": "http_%s" % sub.code}))
                    sys.exit(0)
    else:
        print(json.dumps({"status": "skip", "reason": "http_%s" % err.code}))
        sys.exit(0)
except Exception as exc:  # pylint: disable=broad-except
    print(json.dumps({"status": "skip", "reason": str(exc)[:80]}))
    sys.exit(0)

missing_perms = []
missing_roles = []
for perm, role in checks:
    if perm in valid_perms and perm not in granted:
        # When local Docker is running and Artifact Registry upload is permitted, Cloud Build restriction
        # does not block deployment unless the caller can self-grant Cloud Build Editor.
        if perm == "cloudbuild.builds.create" and has_local_docker \
                and "artifactregistry.repositories.uploadArtifacts" in granted \
                and "resourcemanager.projects.setIamPolicy" not in granted:
            continue
        missing_perms.append(perm)
        if role not in missing_roles:
            missing_roles.append(role)

if not missing_perms:
    print(json.dumps({"status": "ok", "missing_perms": [], "missing_roles": []}))
elif "resourcemanager.projects.setIamPolicy" in granted:
    bootstrap_roles = [r for r in missing_roles if r != "roles/resourcemanager.projectIamAdmin"]
    for extra in ("roles/discoveryengine.admin",):
        if extra not in bootstrap_roles:
            bootstrap_roles.append(extra)
    print(json.dumps({"status": "bootstrap", "missing_perms": missing_perms, "missing_roles": bootstrap_roles}))
else:
    print(json.dumps({"status": "denied", "missing_perms": missing_perms, "missing_roles": missing_roles}))
PYPREFLIGHT
}

# preflight_caller_permissions MODE PROJECT ACCOUNT [NEED_BUILD] [NEED_SINK] [NEED_IAP] [NEED_SCHEDULER] [NEED_AUDIT_ADMIN]
# Verifies caller IAM permissions upfront. When the caller has Project IAM Admin (setIamPolicy) but lacks
# operational roles (such as Cloud Build Editor, Cloud Run Admin, Secret Manager Admin or IAP Admin),
# automatically grants the missing roles to the caller and waits for IAM propagation. When the caller lacks
# setIamPolicy as well (for example Editor/Viewer only), fails fast with exact copy-paste remediation commands.
preflight_caller_permissions() {
  local mode="$1" project="$2" account="$3"
  local need_build="${4:-1}" need_sink="${5:-1}" need_iap="${6:-0}" need_sched="${7:-0}" need_audit="${8:-0}"
  local verdict status member roles_csv perms_csv role attempt docker_avail=0
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker_avail=1
  fi
  member="$(caller_iam_member "$account")"
  verdict="$(ACP_HAS_LOCAL_DOCKER="$docker_avail" _check_project_permissions "$mode" "$project" "$need_build" "$need_sink" "$need_iap" "$need_sched" "$need_audit")"
  status="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("status","skip"))' "$verdict" 2>/dev/null || echo "skip")"
  if [[ "$status" == "ok" || "$status" == "skip" ]]; then
    return 0
  fi
  perms_csv="$(python3 -c 'import json,sys; print(", ".join(json.loads(sys.argv[1]).get("missing_perms",[])))' "$verdict")"
  roles_csv="$(python3 -c 'import json,sys; print(" ".join(json.loads(sys.argv[1]).get("missing_roles",[])))' "$verdict")"
  if [[ "$status" == "bootstrap" ]]; then
    info "account ${account} has IAM admin access; granting missing deployment roles: ${roles_csv}"
    for role in $roles_csv; do
      run retry gcloud projects add-iam-policy-binding "$project" --member "$member" --role "$role" \
        --condition=None --quiet >/dev/null 2>&1 \
        || warn "could not grant ${role} to ${member}; continuing"
    done
    if [[ "${DRY_RUN:-0}" != "1" ]]; then
      for attempt in 1 2 3 4 5 6; do
        sleep 5
        verdict="$(ACP_HAS_LOCAL_DOCKER="$docker_avail" _check_project_permissions "$mode" "$project" "$need_build" "$need_sink" "$need_iap" "$need_sched" "$need_audit")"
        status="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("status","skip"))' "$verdict" 2>/dev/null || echo "skip")"
        [[ "$status" == "ok" ]] && break
      done
    fi
    return 0
  fi
  die "account ${account} lacks required permissions on project ${project}:
    missing permissions: ${perms_csv}

  Fix option 1 — log in as a Project Owner and re-run:
    gcloud auth login

  Fix option 2 — ask a Project Owner to grant Owner (or Editor + Project IAM Admin) to ${account}:
    gcloud projects add-iam-policy-binding ${project} --member=\"${member}\" --role=\"roles/owner\"

  (Least-privilege alternative instead of roles/owner:
    gcloud projects add-iam-policy-binding ${project} --member=\"${member}\" --role=\"roles/editor\"
    gcloud projects add-iam-policy-binding ${project} --member=\"${member}\" --role=\"roles/resourcemanager.projectIamAdmin\"
   or individual roles: ${roles_csv})

  [한국어 안내] 현재 로그인된 계정(${account})에 '${project}' 프로젝트 배포 권한(IAM 정책 설정/API 활성화)이 없습니다.
   1) 다른 Google 계정으로 잘못 로그인된 경우: gcloud auth login 실행 후 소유자 계정으로 다시 로그인하세요.
   2) 프로젝트 관리자에게 위 명령어로 '${account}' 계정에 소유자(roles/owner) 또는 편집자+프로젝트 IAM 관리자(roles/editor + roles/resourcemanager.projectIamAdmin) 권한 부여를 요청하신 뒤 동일 명령어를 다시 실행해 주세요."
}

# preflight_org_policies PROJECT REGION DATASET_LOCATION
# Reads the effective organization policies that decide whether this deployment can work (resource locations, domain
# restricted sharing, Cloud Run ingress / VPC egress / Binary Authorization / invoker IAM, CMEK, restricted services,
# Cloud Build worker pools) and probes for VPC Service Controls, before anything is created. Prints one line per
# finding and sets ORGPOL_* variables that later steps use to adapt automatically:
#   ORGPOL_DRS=1                  domain restricted sharing: no allUsers / allAuthenticatedUsers bindings
#   ORGPOL_REQUIRE_INVOKER_IAM=1  Cloud Run must keep the invoker IAM check (no --no-invoker-iam-check)
#   ORGPOL_BINAUTHZ=<policy>      Cloud Run revisions must name a Binary Authorization policy
#   ORGPOL_VPC_EGRESS=1           Cloud Run revisions must send all egress through a VPC (needs VPC_NETWORK/VPC_SUBNET)
#   ORGPOL_NO_DEFAULT_POOL=1      Cloud Build default pool not allowed: build locally with Docker
# Real blockers (location not allowed, no public ingress, CMEK required, required API denied, VPC-SC perimeter, VPC
# egress without a VPC) stop the run with remediation unless PREFLIGHT_STRICT=false.
preflight_org_policies() {
  local project="$1" region="$2" dslocation="${3:-$2}" out rc=0
  ORGPOL_DRS=0; ORGPOL_REQUIRE_INVOKER_IAM=0; ORGPOL_BINAUTHZ=""; ORGPOL_VPC_EGRESS=0; ORGPOL_NO_DEFAULT_POOL=0
  log "0/8 Organization policy preflight (조직 정책 사전 점검: ${project})"
  [[ "${DRY_RUN:-0}" == "1" ]] || gcloud services enable cloudresourcemanager.googleapis.com orgpolicy.googleapis.com \
    --project "$project" --quiet >/dev/null 2>&1 || true
  out="$(ACP_PF_PROJECT="$project" ACP_PF_REGION="$region" ACP_PF_DSLOC="$dslocation" \
    ACP_PF_VPC_NETWORK="${VPC_NETWORK:-}" ACP_PF_VPC_SUBNET="${VPC_SUBNET:-}" ACP_PF_IMAGE="${IMAGE:-}" \
    python3 - <<'PYORG'
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess

project = os.environ["ACP_PF_PROJECT"]
region = os.environ["ACP_PF_REGION"].lower()
dsloc = (os.environ.get("ACP_PF_DSLOC") or region).lower()
has_vpc = bool(os.environ.get("ACP_PF_VPC_NETWORK") and os.environ.get("ACP_PF_VPC_SUBNET"))
prebuilt = bool(os.environ.get("ACP_PF_IMAGE"))
LEGACY = ["gcp.resourceLocations", "iam.allowedPolicyMemberDomains", "run.allowedIngress", "run.allowedVPCEgress",
          "run.allowedBinaryAuthorizationPolicies", "gcp.restrictNonCmekServices", "serviceuser.services",
          "cloudbuild.allowedWorkerPools", "storage.publicAccessPrevention", "storage.uniformBucketLevelAccess",
          "iam.disableServiceAccountKeyCreation", "iam.automaticIamGrantsForDefaultServiceAccounts"]
MANAGED = ["run.managed.requireInvokerIam", "iam.managed.allowedPolicyMembers"]
REQUIRED_APIS = {"run.googleapis.com", "artifactregistry.googleapis.com", "storage.googleapis.com",
                 "secretmanager.googleapis.com", "bigquery.googleapis.com", "logging.googleapis.com",
                 "iam.googleapis.com", "iamcredentials.googleapis.com", "cloudresourcemanager.googleapis.com",
                 "serviceusage.googleapis.com"}
OPTIONAL_APIS = {"cloudbuild.googleapis.com": "Cloud Build (로컬 Docker 빌드로 대체)",
                 "iap.googleapis.com": "IAP 로그인 브리지 (ID/비밀번호 로그인으로 대체)",
                 "cloudscheduler.googleapis.com": "정기 작업 스케줄러 (인스턴스 내 실행으로 대체)",
                 "aiplatform.googleapis.com": "AI 애널리스트·Agent Engine 화면",
                 "discoveryengine.googleapis.com": "Gemini Enterprise·NotebookLM 화면",
                 "monitoring.googleapis.com": "모델 사용량 지표", "modelarmor.googleapis.com": "Model Armor 화면"}
CMEK_SERVICES = {"storage.googleapis.com", "bigquery.googleapis.com", "secretmanager.googleapis.com",
                 "artifactregistry.googleapis.com", "run.googleapis.com", "logging.googleapis.com",
                 "cloudbuild.googleapis.com"}


def gcloud(args):
    try:
        r = subprocess.run(["gcloud"] + args, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except Exception as err:  # pylint: disable=broad-except
        return 1, "", str(err)
    return r.returncode, r.stdout, r.stderr


def fetch(kind, name):
    base = ["resource-manager", "org-policies"] if kind == "legacy" else ["org-policies"]
    rc, so, se = gcloud(base + ["describe", "constraints/" + name, "--project", project, "--effective", "--format=json"])
    if rc != 0:
        return name, None, se
    try:
        return name, json.loads(so[so.index("{"):]), ""
    except ValueError:
        return name, None, so


with cf.ThreadPoolExecutor(max_workers=8) as pool:
    lres = {n: (p, e) for n, p, e in pool.map(lambda n: fetch("legacy", n), LEGACY)}
    mres = {n: (p, e) for n, p, e in pool.map(lambda n: fetch("managed", n), MANAGED)}

lines, blockers, sets = [], [], {}
if all(p is None for p, _ in lres.values()):
    err = "\n".join(e or "" for _, e in lres.values())
    reason = ((lres[LEGACY[0]][1] or "").strip().splitlines() or [""])[-1]
    if re.search(r"SERVICE_DISABLED|has not been used|is disabled", err):
        hint = ("Cloud Resource Manager API 가 꺼져 있습니다. 먼저 실행: gcloud services enable cloudresourcemanager.googleapis.com "
                "orgpolicy.googleapis.com --project %s" % project)
    elif re.search(r"PERMISSION_DENIED|403|does not have", err):
        hint = "정책 조회 권한이 없습니다. 조직 관리자에게 roles/orgpolicy.policyViewer 를 요청하면 정확히 점검할 수 있습니다"
    else:
        hint = reason[:160]
    lines.append("SKIP|조직 정책을 읽지 못해 사전 점검을 건너뜁니다 (%s). 배포 중 자동 대응 경로로 진행합니다." % hint)


def lp(name):
    pol = lres.get(name, (None, ""))[0] or {}
    return pol.get("listPolicy") or {}


def enforced(name):
    pol = lres.get(name, (None, ""))[0] or {}
    return bool((pol.get("booleanPolicy") or {}).get("enforced"))


def listed(policy):
    return ([str(v).lower() for v in policy.get("allowedValues") or []],
            [str(v).lower() for v in policy.get("deniedValues") or []],
            str(policy.get("allValues") or "").upper())


def managed_on(name):
    pol = mres.get(name, (None, ""))[0] or {}
    return any(r.get("enforce") for r in ((pol.get("spec") or {}).get("rules") or []))


GROUPS = {"asia": {"asia"}, "europe": {"eu", "europe"}, "us": {"us", "northamerica"},
          "northamerica": {"northamerica"}, "southamerica": {"southamerica"}, "australia": {"australia"},
          "me": {"me"}, "africa": {"africa"}}
KNOWN_GROUPS = set().union(*GROUPS.values())


def loc_match(loc, value):
    """True / False, or None for a value group this check cannot resolve. Effective policies list expanded members
    (us-west3-locations, us-west3-b, us, nam3, ...), so any concrete location that is not ours is False."""
    name = value[3:] if value.startswith("in:") else value
    cont = loc.split("-")[0]
    if name in (loc, loc + "-locations") or name.startswith(loc + "-"):
        return True
    if name.endswith("-locations"):
        group = name[:-len("-locations")]
        if group in GROUPS.get(cont, set()) or group == loc:
            return True
        if group in KNOWN_GROUPS or re.match(r"^[a-z]+-[a-z]+\d+$", group) or re.match(r"^[a-z]+\d*$", group) is None:
            return False
        return None if len(group) == 2 else False
    return False


def loc_verdict(loc, policy):
    allowed, denied, allv = listed(policy)
    if allv == "DENY" or any(loc_match(loc, v) is True for v in denied):
        return False
    if not allowed:
        return True
    hits = [loc_match(loc, v) for v in allowed]
    if any(h is True for h in hits):
        return True
    return None if any(h is None for h in hits) else False


# 1. Resource locations
pol = lp("gcp.resourceLocations")
if pol:
    allowed_txt = ", ".join((pol.get("allowedValues") or [])[:8]) or "-"
    loc_block = False
    for what, loc in (("리전", region), ("BigQuery 위치", dsloc)):
        v = loc_verdict(loc, pol)
        if v is False:
            loc_block = True
            blockers.append("gcp.resourceLocations: %s '%s' 가 허용되지 않습니다 (허용: %s). 허용된 리전으로 다시 실행하세요: "
                            "--region <허용 리전>" % (what, loc, allowed_txt))
        elif v is None:
            lines.append("WARN|gcp.resourceLocations: %s '%s' 허용 여부를 확정할 수 없습니다 (허용: %s). 실패하면 허용 리전으로 다시 실행하세요."
                         % (what, loc, allowed_txt))
    if not loc_block and all(loc_verdict(l, pol) is True for l in (region, dsloc)):
        lines.append("OK|gcp.resourceLocations: %s 허용 (Secret Manager·버킷·BigQuery·Artifact Registry 를 %s 에 생성)" % (region, region))
else:
    lines.append("OK|gcp.resourceLocations: 제한 없음")

# 2. Domain restricted sharing (legacy and managed)
allowed, denied, allv = listed(lp("iam.allowedPolicyMemberDomains"))
if allowed or allv == "DENY" or managed_on("iam.managed.allowedPolicyMembers"):
    sets["ORGPOL_DRS"] = "1"
    lines.append("AUTO|도메인 제한 공유(iam.allowedPolicyMemberDomains) 사용 중: allUsers/allAuthenticatedUsers 바인딩을 건너뛰고 "
                 "대시보드는 자체 로그인(--no-invoker-iam-check)으로 공개합니다. Google 로그인(IAP 브리지)에는 조직 도메인과 "
                 "Gemini Enterprise 사용자·프로젝트 구성원의 도메인을 허용하고, 거부된 항목은 배포 끝 'Google 로그인(IAP) 점검'에 "
                 "표시합니다. 조직 밖 계정(협력사 등)은 설정 > 접근 제어에서 Google OAuth 클라이언트를 연결하거나(재배포 없음) "
                 "ID/비밀번호 계정('admin' + Secret Manager 비밀번호로 로그인 후 발급)을 쓰세요.")
else:
    lines.append("OK|도메인 제한 공유: 없음")

# 3. Cloud Run invoker IAM (managed)
if managed_on("run.managed.requireInvokerIam"):
    sets["ORGPOL_REQUIRE_INVOKER_IAM"] = "1"
    if sets.get("ORGPOL_DRS") == "1":
        lines.append("AUTO|run.managed.requireInvokerIam + 도메인 제한 공유: 공개 진입이 불가하므로 대시보드를 Cloud Run IAP(Google "
                     "로그인)로 바로 배포합니다. 이 경우 Cloud Scheduler 대신 인스턴스 내부에서 정기 작업을 실행합니다.")
    else:
        lines.append("AUTO|run.managed.requireInvokerIam: --no-invoker-iam-check 대신 allUsers invoker 로 공개합니다.")

# 4. Cloud Run ingress
allowed, denied, allv = listed(lp("run.allowedIngress"))
if (allowed and "all" not in allowed) or "all" in denied or allv == "DENY":
    blockers.append("run.allowedIngress: Cloud Run 외부 접속(ingress=all)이 금지되어 브라우저로 대시보드에 접속할 수 없습니다 (허용: %s). "
                    "조직 관리자에게 이 프로젝트만 예외(run.allowedIngress 에 all 허용)를 요청하거나, 외부 HTTPS 부하 분산기+IAP 뒤에 "
                    "배치해야 합니다." % (", ".join(allowed) or "-"))
else:
    lines.append("OK|run.allowedIngress: 외부 접속 허용")

# 5. Cloud Run VPC egress
allowed, denied, allv = listed(lp("run.allowedVPCEgress"))
if allowed and "private-ranges-only" not in allowed:
    sets["ORGPOL_VPC_EGRESS"] = "1"
    if has_vpc:
        lines.append("AUTO|run.allowedVPCEgress=%s: VPC_NETWORK/VPC_SUBNET 으로 모든 송신을 VPC 경유(all-traffic)로 배포합니다. "
                     "서브넷에 비공개 Google 액세스(Private Google Access)가 켜져 있어야 합니다." % ",".join(allowed))
    else:
        blockers.append("run.allowedVPCEgress=%s: Cloud Run 이 VPC 를 통해서만 송신해야 합니다. 비공개 Google 액세스가 켜진 서브넷을 "
                        "지정해 다시 실행하세요: --vpc-network <네트워크> --vpc-subnet <서브넷>" % ",".join(allowed))

# 6. Binary Authorization
allowed, denied, allv = listed(lp("run.allowedBinaryAuthorizationPolicies"))
if allowed:
    sets["ORGPOL_BINAUTHZ"] = (lp("run.allowedBinaryAuthorizationPolicies").get("allowedValues") or ["default"])[0]
    lines.append("AUTO|run.allowedBinaryAuthorizationPolicies: Cloud Run 을 --binary-authorization=%s 로 배포합니다 "
                 "(정책이 이 이미지를 허용하지 않으면 보안 담당자에게 예외/증명 요청)." % sets["ORGPOL_BINAUTHZ"])

# 7. CMEK required
allowed, denied, allv = listed(lp("gcp.restrictNonCmekServices"))
need = sorted(CMEK_SERVICES if allv == "DENY" else (set(denied) & CMEK_SERVICES))
if need:
    blockers.append("gcp.restrictNonCmekServices: %s 리소스를 고객 관리 암호화 키(CMEK) 없이 만들 수 없습니다. 이 설치 스크립트는 CMEK 를 "
                    "지원하지 않으므로 조직 관리자에게 이 프로젝트만 예외를 요청하세요." % ", ".join(need))

# 8. Restricted services
allowed, denied, allv = listed(lp("serviceuser.services"))
if allowed or denied:
    blocked = set(denied)
    if allowed:
        blocked |= {a for a in REQUIRED_APIS | set(OPTIONAL_APIS) if a not in allowed}
    req = sorted(blocked & REQUIRED_APIS)
    if req:
        blockers.append("serviceuser.services: 필수 API 사용이 금지되어 있습니다: %s. 조직 관리자에게 허용을 요청하세요." % ", ".join(req))
    for api in sorted(blocked & set(OPTIONAL_APIS)):
        lines.append("WARN|serviceuser.services: %s 금지 -> %s 기능 제한" % (api, OPTIONAL_APIS[api]))

# 9. Cloud Build worker pools
allowed, denied, allv = listed(lp("cloudbuild.allowedWorkerPools"))
if (allowed or allv == "DENY") and not prebuilt:
    sets["ORGPOL_NO_DEFAULT_POOL"] = "1"
    docker = shutil.which("docker") is not None and \
        subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0
    if docker:
        lines.append("AUTO|cloudbuild.allowedWorkerPools: 기본 빌드 풀 금지 -> Cloud Build 대신 이 환경의 Docker 로 빌드합니다.")
    else:
        blockers.append("cloudbuild.allowedWorkerPools: 기본 Cloud Build 풀이 금지되어 있고 이 환경에 Docker 가 없습니다. "
                        "Google Cloud Shell(Docker 내장)에서 다시 실행하세요.")

# 10. Constraints this deployment already satisfies
for name, text in (("storage.publicAccessPrevention", "버킷 공개 차단"), ("storage.uniformBucketLevelAccess", "균일 버킷 접근"),
                   ("iam.disableServiceAccountKeyCreation", "서비스 계정 키 생성 금지"),
                   ("iam.automaticIamGrantsForDefaultServiceAccounts", "기본 서비스 계정 자동 권한 부여 금지")):
    if enforced(name):
        lines.append("OK|%s(%s) 적용됨: 이 배포는 그대로 준수합니다." % (name, text))

# 11. VPC Service Controls probe
rc, so, se = gcloud(["storage", "buckets", "list", "--project", project, "--limit", "1", "--format=value(name)"])
if rc != 0 and re.search(r"VPC Service Controls|vpcServiceControls|SECURITY_POLICY_VIOLATED|prohibited by organization", se or "", re.I):
    blockers.append("VPC Service Controls: 이 프로젝트가 서비스 경계 안에 있고 현재 실행 환경은 경계 밖입니다. 보안 담당자에게 이 계정의 "
                    "인그레스 규칙(또는 액세스 수준) 추가를 요청하거나 경계 안의 환경에서 실행하세요.")

for line in lines:
    print("LINE|" + line)
for b in blockers:
    print("BLOCK|" + b)
for k, v in sets.items():
    print("SET|%s=%s" % (k, re.sub(r"[^A-Za-z0-9_.:/-]", "", v)))
PYORG
)" || rc=$?
  local tag kind body var line blockers=()
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    tag="${line%%|*}"; line="${line#*|}"
    case "$tag" in
      LINE)
        kind="${line%%|*}"; body="${line#*|}"
        case "$kind" in
          OK) info "[OK]   ${body}" ;;
          AUTO) info "[자동] ${body}" ;;
          WARN) warn "[주의] ${body}" ;;
          SKIP) warn "[건너뜀] ${body}" ;;
          *) info "${body}" ;;
        esac ;;
      BLOCK) blockers+=("$line"); printf '    [중단] %s\n' "$line" >&2 ;;
      SET)
        var="${line%%=*}"
        case "$var" in
          ORGPOL_DRS|ORGPOL_REQUIRE_INVOKER_IAM|ORGPOL_BINAUTHZ|ORGPOL_VPC_EGRESS|ORGPOL_NO_DEFAULT_POOL)
            printf -v "$var" '%s' "${line#*=}" ;;
        esac ;;
    esac
  done <<<"$out"
  [[ "$rc" == "0" ]] || warn "organization policy preflight did not finish (exit ${rc}); continuing with runtime fallbacks"
  if [[ ${#blockers[@]} -gt 0 ]]; then
    if [[ "${PREFLIGHT_STRICT:-true}" == "false" ]]; then
      warn "PREFLIGHT_STRICT=false: continuing despite ${#blockers[@]} blocking organization policy finding(s)"
    else
      die "조직 정책 때문에 이 프로젝트에는 지금 배포할 수 없습니다 (${#blockers[@]}건). 대시보드 리소스는 아직 하나도 만들지 않았습니다.
   위 [중단] 항목을 조치한 뒤 같은 명령을 다시 실행하세요. 판단이 틀렸다고 확신하면 --skip-preflight-blockers 를 붙여 계속할 수 있습니다.
   (Organization policies block this deployment; no dashboard resources were created. Fix the [중단] items and re-run.)"
    fi
  fi
}

# ensure_service_identities PROJECT PROJECT_NUMBER SERVICE [SERVICE ...]
# Triggers creation of Google-managed service agents (such as gcp-sa-cloudbuild and gcp-sa-iap) via the
# Service Usage REST API so freshly enabled APIs never fail with "Service account service-... does not exist"
# and never prompt interactively to install `gcloud beta`.
ensure_service_identities() {
  local project="$1" project_number="$2" token gcloud_bin sdk_root svc
  shift 2
  [[ "${DRY_RUN:-0}" == "1" || $# -eq 0 ]] && return 0
  token="$(gcloud auth print-access-token 2>/dev/null || true)"
  if [[ -n "$token" ]]; then
    if ACP_ACCESS_TOKEN="$token" python3 - "$project" "$project_number" "$@" <<'PYSID' 2>/dev/null
import os, sys, urllib.error, urllib.request

project, project_number, services = sys.argv[1], sys.argv[2], sys.argv[3:]
token = os.environ["ACP_ACCESS_TOKEN"]
auth_failed = False
for svc in services:
    url = "https://serviceusage.googleapis.com/v1beta1/projects/%s/services/%s:generateServiceIdentity" % (
        project_number, svc
    )
    req = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "X-Goog-User-Project": project,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except urllib.error.HTTPError as err:
        if err.code == 401:
            auth_failed = True
            break
    except Exception:  # pylint: disable=broad-except
        pass
sys.exit(2 if auth_failed else 0)
PYSID
    then
      return 0
    fi
  fi
  # Fallback for mTLS / Context-Aware workstations when `gcloud beta` is already installed (never prompts).
  gcloud_bin="$(command -v gcloud 2>/dev/null || true)"
  sdk_root="${gcloud_bin%/bin/gcloud}"
  if [[ -n "$sdk_root" && -d "${sdk_root}/lib/surface/beta" ]]; then
    for svc in "$@"; do
      CLOUDSDK_CORE_DISABLE_PROMPTS=1 gcloud beta services identity create \
        --service="$svc" --project="$project" --quiet >/dev/null 2>&1 || true
    done
  fi
}

# ensure_secret_created SECRET_NAME PROJECT REGION
# Creates an empty Secret Manager secret if missing. Prefers regional user-managed replication in REGION so
# organizations enforcing constraints/gcp.resourceLocations (for example asia-northeast3 only) succeed, and
# falls back to automatic replication if needed.
ensure_secret_created() {
  local secret="$1" project="$2" region="$3"
  if gcloud secrets describe "$secret" --project "$project" >/dev/null 2>&1; then
    return 0
  fi
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    info "[dry-run] create secret ${secret} in ${region}"
    return 0
  fi
  gcloud secrets create "$secret" --project "$project" \
    --replication-policy=user-managed --locations="$region" \
    --labels=app=ai-control-plane >/dev/null 2>&1 \
    || gcloud secrets create "$secret" --project "$project" \
      --replication-policy=automatic \
      --labels=app=ai-control-plane >/dev/null
}

# Dataset-level BigQuery access (a dataset ACL entry) for one service account; project IAM is not touched.
# grant_dataset_role PROJECT DATASET ROLE EMAIL, where ROLE is READER (Data Viewer) or WRITER (Data Editor).
# Returns non-zero when the dataset cannot be read or updated.
grant_dataset_role() {
  local file rc=0
  file="$(mktemp)"
  if ! bq --project_id "$1" show --format=prettyjson "${1}:${2}" >"$file" 2>/dev/null; then
    rm -f "$file"
    return 1
  fi
  python3 - "$file" "$3" "$4" <<'PYCODE' || rc=$?
import json, sys
path, role, email = sys.argv[1], sys.argv[2], sys.argv[3].lower()
enough = {"READER": ("READER", "WRITER", "OWNER"), "WRITER": ("WRITER", "OWNER")}[role]
raw = open(path, encoding="utf-8", errors="replace").read()
dataset = json.loads(raw[raw.index("{"):])
access = dataset.setdefault("access", [])
if any((a.get("userByEmail") or "").lower() == email and a.get("role") in enough for a in access):
    sys.exit(3)  # already granted
access.append({"role": role, "userByEmail": email})
json.dump(dataset, open(path, "w", encoding="utf-8"))
PYCODE
  if [[ $rc -eq 0 ]]; then
    bq --project_id "$1" update --source "$file" "${1}:${2}" >/dev/null 2>&1 || rc=1
  elif [[ $rc -eq 3 ]]; then
    rc=0
  fi
  rm -f "$file"
  return $rc
}

# grant_license_reader PROJECT SA_EMAIL ROLE_ID PERMISSIONS
# Best effort, for linked projects: creates (or updates) the read-only custom role ROLE_ID with PERMISSIONS in
# PROJECT and binds it to SA_EMAIL. The predefined roles/discoveryengine.viewer has neither
# discoveryengine.licenseConfigs.list nor discoveryengine.userStores.listUserLicenses, so without this role the
# Gemini Enterprise seats of PROJECT stay hidden. Never fails the caller: when the deploying account cannot do
# it, the warning carries the exact commands for an Owner of PROJECT. With DRY_RUN=1 it only prints the plan.
grant_license_reader() {
  local project="$1" sa="$2" role_id="$3" perms="$4" role create_cmd bind_cmd
  role="projects/${project}/roles/${role_id}"
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    info "[dry-run] custom role ${role} (${perms}) -> ${sa}"
    return 0
  fi
  create_cmd="gcloud iam roles create ${role_id} --project ${project} --title \"AI Control Plane License Reader\" --permissions ${perms} --stage GA"
  bind_cmd="gcloud projects add-iam-policy-binding ${project} --member serviceAccount:${sa} --role ${role} --condition=None"
  gcloud iam roles undelete "$role_id" --project "$project" --quiet >/dev/null 2>&1 || true
  if gcloud iam roles describe "$role_id" --project "$project" >/dev/null 2>&1; then
    gcloud iam roles update "$role_id" --project "$project" --permissions "$perms" --quiet >/dev/null 2>&1 || true
  elif ! gcloud iam roles create "$role_id" --project "$project" \
      --title "AI Control Plane License Reader" \
      --description "Read-only Gemini Enterprise license config and user seat reader" \
      --permissions "$perms" --stage GA --quiet >/dev/null 2>&1; then
    warn "could not create the license reader role ${role}, so Gemini Enterprise seats in ${project} stay hidden. As an Owner of ${project}, run: ${create_cmd} && ${bind_cmd}"
    return 0
  fi
  if gcloud projects add-iam-policy-binding "$project" --member "serviceAccount:${sa}" --role "$role" \
      --condition=None --quiet >/dev/null 2>&1; then
    info "linked project ${project}: granted ${role} (Gemini Enterprise seats) to ${sa}"
  else
    warn "could not grant ${role} to ${sa}, so Gemini Enterprise seats in ${project} stay hidden. As an Owner of ${project}, run: ${bind_cmd}"
  fi
  return 0
}

# Log Router filter for the AI logs the dashboard reads (README 6.1 shows the same text).
# Cloud Logging binds OR more tightly than AND, so the audit-log clause sits in its own parentheses.
# Without them the Gemini Enterprise, Model Armor and Antigravity logs would also have to be audit logs,
# and none of them would be routed.
ai_log_filter() {
  cat <<'FILTER'
(
  (log_id("cloudaudit.googleapis.com/activity") OR log_id("cloudaudit.googleapis.com/data_access"))
  AND protoPayload.serviceName=("aiplatform.googleapis.com" OR "discoveryengine.googleapis.com" OR "modelarmor.googleapis.com" OR "businessaicode.googleapis.com" OR "notebooklm.googleapis.com" OR "geminidataanalytics.googleapis.com" OR "generativelanguage.googleapis.com")
)
OR log_id("discoveryengine.googleapis.com/gemini_enterprise_user_activity")
OR log_id("discoveryengine.googleapis.com/gen_ai_user_message")
OR log_id("modelarmor.googleapis.com/sanitize_operations")
OR log_id("businessaicode.googleapis.com/inference_response")
FILTER
}

# ensure_ai_log_sink PROJECT DATASET LOCATION SINK
# Creates the dataset (in LOCATION, only when missing) and the Log Router sink with partitioned tables, or
# updates an existing sink to the current filter and destination. The sink's writer identity gets BigQuery
# Data Editor on this dataset only; the writer is often the project's shared Logging service agent, so
# nothing is granted at project level.
ensure_ai_log_sink() {
  local project="$1" dataset="$2" location="$3" sink="$4" dest filter writer
  if bq --project_id "$project" show --dataset "${project}:${dataset}" >/dev/null 2>&1; then
    info "dataset ${project}:${dataset} exists"
  else
    run bq --project_id "$project" --location "$location" mk --dataset \
      --description "AI audit logs routed for AI Control Plane" "${project}:${dataset}" \
      || run bq --project_id "$project" --location "US" mk --dataset \
        --description "AI audit logs routed for AI Control Plane" "${project}:${dataset}"
  fi
  dest="bigquery.googleapis.com/projects/${project}/datasets/${dataset}"
  filter="$(ai_log_filter)"
  if gcloud logging sinks describe "$sink" --project "$project" >/dev/null 2>&1; then
    run gcloud logging sinks update "$sink" "$dest" --log-filter "$filter" --project "$project" --quiet
  else
    run gcloud logging sinks create "$sink" "$dest" --use-partitioned-tables --log-filter "$filter" \
      --project "$project" --quiet
  fi
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    info "[dry-run] grant the sink writer BigQuery Data Editor on ${project}:${dataset}"
    return 0
  fi
  writer="$(gcloud logging sinks describe "$sink" --project "$project" --format='value(writerIdentity)')"
  retry grant_dataset_role "$project" "$dataset" WRITER "${writer#serviceAccount:}" \
    || die "could not grant the sink writer BigQuery Data Editor on ${project}:${dataset}"
  info "sink ${sink} -> ${project}:${dataset} (writer ${writer}: BigQuery Data Editor on this dataset only)"
}

# prepare_linked_project_telemetry PROJECT DATASET LOCATION SINK CREATE_SINK ENABLE_AUDIT ENABLE_GE LOG_PROMPTS
# Best-effort, non-fatal telemetry setup for secondary projects passed via --link-projects / SCAN_PROJECTS.
# Never aborts the hub deployment when the caller only has read/IAM privileges on the linked project.
prepare_linked_project_telemetry() {
  local project="$1" dataset="$2" location="$3" sink="$4"
  local create_sink="${5:-true}" enable_audit="${6:-true}" enable_ge="${7:-true}" log_prompts="${8:-true}"
  local dest filter writer ge_locs sink_ok=0
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    info "[dry-run] linked project ${project}: best-effort APIs, ${project}:${dataset} (${location}) sink, Data Access logs & GE observability"
    return 0
  fi
  gcloud services enable logging.googleapis.com bigquery.googleapis.com monitoring.googleapis.com \
    aiplatform.googleapis.com discoveryengine.googleapis.com modelarmor.googleapis.com \
    --project "$project" --quiet >/dev/null 2>&1 || true
  if [[ "$create_sink" == "true" ]]; then
    if ! bq --project_id "$project" show --dataset "${project}:${dataset}" >/dev/null 2>&1; then
      bq --project_id "$project" --location "$location" mk --dataset \
        --description "AI audit logs routed for AI Control Plane" "${project}:${dataset}" >/dev/null 2>&1 \
        || bq --project_id "$project" --location "US" mk --dataset \
          --description "AI audit logs routed for AI Control Plane" "${project}:${dataset}" >/dev/null 2>&1 || true
    fi
    if bq --project_id "$project" show --dataset "${project}:${dataset}" >/dev/null 2>&1; then
      dest="bigquery.googleapis.com/projects/${project}/datasets/${dataset}"
      filter="$(ai_log_filter)"
      if gcloud logging sinks describe "$sink" --project "$project" >/dev/null 2>&1; then
        gcloud logging sinks update "$sink" "$dest" --log-filter "$filter" --project "$project" --quiet >/dev/null 2>&1 && sink_ok=1 || true
      else
        gcloud logging sinks create "$sink" "$dest" --use-partitioned-tables --log-filter "$filter" \
          --project "$project" --quiet >/dev/null 2>&1 && sink_ok=1 || true
      fi
      if [[ "$sink_ok" == "1" ]]; then
        writer="$(gcloud logging sinks describe "$sink" --project "$project" --format='value(writerIdentity)' 2>/dev/null || true)"
        if [[ -n "$writer" ]] && grant_dataset_role "$project" "$dataset" WRITER "${writer#serviceAccount:}" >/dev/null 2>&1; then
          info "linked project ${project}: log sink ${sink} -> ${project}:${dataset} ready"
        fi
      fi
    fi
  fi
  if [[ "$enable_audit" == "true" ]]; then
    enable_data_access_logs "$project" || true
  fi
  if [[ "$enable_ge" == "true" ]]; then
    ge_locs="$(ge_locations "$project")"
    if [[ -n "$ge_locs" ]]; then
      ensure_ge_observability "$project" "$ge_locs" "$log_prompts" || true
    fi
  fi
  return 0
}

# enable_data_access_logs PROJECT
# Enables ADMIN_READ, DATA_READ and DATA_WRITE Cloud Audit Logs across all IAM-configurable AI services
# (Agent Platform, Discovery Engine [Gemini Enterprise & NotebookLM], Model Armor, Antigravity, and Gemini
# Data Analytics). Retries on concurrent IAM etag conflicts and automatically skips any service identifier
# rejected with INVALID_ARGUMENT in restricted environments.
enable_data_access_logs() {
  local project="$1" file err_file changed attempt skip_csv="" bad_svc
  for attempt in 1 2 3 5; do
    file="$(mktemp)"
    err_file="$(mktemp)"
    if ! gcloud projects get-iam-policy "$project" --format=json >"$file" 2>/dev/null; then
      rm -f "$file" "$err_file"
      warn "cannot read IAM policy of ${project} to update Data Access audit logs; continuing"
      return 0
    fi
    changed="$(python3 - "$file" "$skip_csv" <<'PYAUDIT'
import json, sys
path = sys.argv[1]
skip = {s.strip() for s in sys.argv[2].split(",") if s.strip()}
policy = json.load(open(path))
configs = policy.setdefault("auditConfigs", [])
changed = False
services = (
    "aiplatform.googleapis.com",
    "discoveryengine.googleapis.com",
    "modelarmor.googleapis.com",
    "businessaicode.googleapis.com",
    "geminidataanalytics.googleapis.com",
)
for service in services:
    if service in skip:
        continue
    entry = next((c for c in configs if c.get("service") == service), None)
    if entry is None:
        entry = {"service": service, "auditLogConfigs": []}
        configs.append(entry)
    present = {c.get("logType") for c in entry.setdefault("auditLogConfigs", [])}
    for log_type in ("ADMIN_READ", "DATA_READ", "DATA_WRITE"):
        if log_type not in present:
            entry["auditLogConfigs"].append({"logType": log_type})
            changed = True
json.dump(policy, open(path, "w"))
print("yes" if changed else "no")
PYAUDIT
)"
    if [[ "$changed" != "yes" ]]; then
      rm -f "$file" "$err_file"
      info "Data Access audit logs already enabled across all AI services"
      return 0
    fi
    if [[ "${DRY_RUN:-0}" == "1" ]]; then
      rm -f "$file" "$err_file"
      info "[dry-run] Data Access audit logs would be enabled across all AI services"
      return 0
    fi
    if gcloud projects set-iam-policy "$project" "$file" --quiet >/dev/null 2>"$err_file"; then
      rm -f "$file" "$err_file"
      info "Data Access audit logs enabled across all AI services (Agent Platform, Gemini Enterprise, NotebookLM, Model Armor, Antigravity)"
      return 0
    fi
    bad_svc="$(python3 - "$err_file" <<'PYERR' || true
import re, sys
txt = open(sys.argv[1], errors="replace").read()
m = re.search(r"Service\s+([a-z0-9.-]+\.googleapis\.com)\s+does not", txt)
if m:
    print(m.group(1))
PYERR
)"
    rm -f "$file" "$err_file"
    if [[ -n "$bad_svc" && ",${skip_csv}," != *",${bad_svc},"* ]]; then
      skip_csv="${skip_csv:+${skip_csv},}${bad_svc}"
      continue
    fi
    sleep 3
  done
  warn "could not update Data Access audit log configuration on ${project} after retries; continuing"
}

# Locations where Gemini Enterprise apps and license configurations can live.
GE_LOCATIONS_ALL="global,us,eu"

# _ge_apps MODE PROJECT LOCATIONS LOG_PROMPTS
# Reads the Gemini Enterprise apps (engines with appType APP_TYPE_INTRANET) in the comma-separated
# LOCATIONS through the Discovery Engine API. Never fails the caller.
#   enable  turns on observability (usage logs) for every app and reports each one
#   detect  prints only the locations that have an app or a license configuration, e.g. "global,us"
_ge_apps() {
  local token
  token="$(gcloud auth print-access-token 2>/dev/null)" || token=""
  if [[ -z "$token" ]]; then
    [[ "$1" == "detect" ]] \
      || warn "no access token: turn on observability in the console (Gemini Enterprise > app > Configurations > Observability)"
    return 0
  fi
  ACP_ACCESS_TOKEN="$token" python3 - "$@" "${DRY_RUN:-0}" <<'PY' || true
import json, os, sys, urllib.error, urllib.request

mode, project, locations = sys.argv[1], sys.argv[2], sys.argv[3]
prompts, dry = sys.argv[4] == "true", sys.argv[5] == "1"
token = os.environ["ACP_ACCESS_TOKEN"]
MANUAL = "Gemini Enterprise > app > Configurations > Observability > 'Enable instrumentation of OpenTelemetry traces and logs'"


def host(location):
    return ("https://discoveryengine.googleapis.com" if location == "global"
            else "https://%s-discoveryengine.googleapis.com" % location)


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "X-Goog-User-Project": project, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def reason(err):
    try:
        detail = json.load(err).get("error", {})
        return "%s %s" % (detail.get("status") or err.code, (detail.get("message") or "")[:160])
    except Exception:  # pylint: disable=broad-except
        return "HTTP %s" % err.code


def warn(text):
    if mode == "enable":
        print("    warning: " + text, file=sys.stderr)


apps, found = 0, []
for location in [l.strip() for l in locations.split(",") if l.strip()]:
    base = "%s/v1alpha/projects/%s/locations/%s" % (host(location), project, location)
    engines, page = [], ""
    try:
        while True:
            data = call("GET", base + "/collections/default_collection/engines?pageSize=100"
                        + ("&pageToken=" + page if page else ""))
            engines += data.get("engines", [])
            page = data.get("nextPageToken") or ""
            if not page:
                break
    except urllib.error.HTTPError as err:
        warn("cannot list Gemini Enterprise apps in %s (%s)" % (location, reason(err)))
        continue
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        warn("cannot reach Discovery Engine in %s (%s)" % (location, err))
        continue
    engines = [e for e in engines if e.get("appType") == "APP_TYPE_INTRANET"]  # not search or chat apps
    if mode == "detect":
        if engines:
            found.append(location)
            continue
        try:  # seats can be distributed to a location before its first app exists
            if call("GET", base + "/licenseConfigs").get("licenseConfigs"):
                found.append(location)
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        continue
    for engine in engines:
        apps += 1
        label = "%s (%s)" % (engine.get("displayName") or engine["name"].rsplit("/", 1)[-1], location)
        current = engine.get("observabilityConfig") or {}
        want = {"observabilityEnabled": True,
                "sensitiveLoggingEnabled": bool(current.get("sensitiveLoggingEnabled")) or prompts}
        if current.get("observabilityEnabled") and bool(current.get("sensitiveLoggingEnabled")) == want["sensitiveLoggingEnabled"]:
            print("    Gemini Enterprise app %s: observability already on%s"
                  % (label, ", prompts logged" if want["sensitiveLoggingEnabled"] else ""))
            continue
        if dry:
            print("    [dry-run] Gemini Enterprise app %s: turn on observability%s"
                  % (label, " and prompt logging" if want["sensitiveLoggingEnabled"] else ""), file=sys.stderr)
            continue
        try:
            call("PATCH", "%s/v1alpha/%s?updateMask=observabilityConfig" % (host(location), engine["name"]),
                 {"observabilityConfig": want})
            print("    Gemini Enterprise app %s: observability turned on%s"
                  % (label, ", prompts logged" if want["sensitiveLoggingEnabled"] else ""))
        except urllib.error.HTTPError as err:
            warn("Gemini Enterprise app %s: could not turn on observability (%s). Needs the Gemini Enterprise "
                 "Admin role, or do it in the console: %s" % (label, reason(err), MANUAL))
if mode == "detect":
    print(",".join(found))
elif apps == 0:
    print("    no Gemini Enterprise app found in %s" % locations)
PY
}

# ensure_ge_observability PROJECT LOCATIONS [LOG_PROMPTS]
# Turns on observability (usage logs) and prompt/user logging (sensitiveLoggingEnabled, default true;
# without it Gemini Enterprise elides userIamPrincipal and text as "<elided>") for every Gemini Enterprise
# app in the comma-separated LOCATIONS. Never fails the caller: problems are reported as warnings.
ensure_ge_observability() { _ge_apps enable "$1" "$2" "${3:-true}"; }

# ge_locations PROJECT
# Prints the locations (of GE_LOCATIONS_ALL) that have a Gemini Enterprise app or license configuration,
# comma separated; prints nothing when there are none or they cannot be read.
ge_locations() { _ge_apps detect "$1" "$GE_LOCATIONS_ALL" false 2>/dev/null; }

# ensure_antigravity_marker_roles PROJECT [ACCESS_ROLE]
# Creates (or refreshes) the two marker roles per-user Antigravity caps assign instead of the access role:
# CustomAntigravityCapBlocked (suspended by a cap) and CustomAntigravityAdminBlocked (blocked by an administrator).
# Both are the access role (default Gemini Enterprise User) without businessaicode.* permissions, which is the
# custom role Google documents for restricting AI developer tools: users keep the Gemini Enterprise app and only
# lose Antigravity. Permissions that custom roles do not support are dropped and retried. Never fails the deploy.
ensure_antigravity_marker_roles() {
  local project="$1" access_role="${2:-roles/discoveryengine.agentspaceUser}"
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    info "[dry-run] Antigravity marker roles CustomAntigravityCapBlocked and CustomAntigravityAdminBlocked in ${project} (${access_role} without businessaicode.*)"
    return 0
  fi
  python3 - "$project" "$access_role" <<'PY' || warn "Antigravity marker roles are not ready in ${project}: open Antigravity controls and select Create marker roles"
import json, subprocess, sys
project, base = sys.argv[1], sys.argv[2]

def gc(*args):
    return subprocess.run(["gcloud", *args, "--quiet"], capture_output=True, text=True, stdin=subprocess.DEVNULL)

got = gc("iam", "roles", "describe", base, "--format=json")
if got.returncode != 0:
    print("    warning: cannot read %s" % base, file=sys.stderr)
    sys.exit(1)
perms = [p for p in json.loads(got.stdout).get("includedPermissions") or [] if not p.startswith("businessaicode.")]
if not perms:
    print("    warning: %s has no permissions besides businessaicode.*" % base, file=sys.stderr)
    sys.exit(1)
roles = {
    "CustomAntigravityCapBlocked": ("Antigravity Cap Blocked",
        "Assigned by AI Control Plane when a user exceeds the Antigravity cap. Same as the access role without "
        "businessaicode permissions."),
    "CustomAntigravityAdminBlocked": ("Antigravity Admin Blocked",
        "Assigned by an administrator to disallow Antigravity. Same as the access role without businessaicode "
        "permissions."),
}
ok = True
for role_id, (title, description) in roles.items():
    gc("iam", "roles", "undelete", role_id, "--project", project)
    exists = gc("iam", "roles", "describe", role_id, "--project", project, "--format=value(name)").returncode == 0
    use = list(perms)
    done = False
    for _ in range(5):
        if exists:
            res = gc("iam", "roles", "update", role_id, "--project", project, "--permissions", ",".join(use))
        else:
            res = gc("iam", "roles", "create", role_id, "--project", project, "--title", title,
                     "--description", description, "--stage", "GA", "--permissions", ",".join(use))
        if res.returncode == 0:
            print("    marker role %s %s (%d permissions, businessaicode.* excluded)"
                  % (role_id, "refreshed" if exists else "created", len(use)))
            done = True
            break
        bad = [p for p in use if p in (res.stderr or "")]
        if not bad:
            tail = (res.stderr or "").strip().splitlines()
            print("    warning: marker role %s: %s" % (role_id, tail[-1] if tail else "failed"), file=sys.stderr)
            break
        use = [p for p in use if p not in bad]
    ok = ok and done
sys.exit(0 if ok else 1)
PY
}

# antigravity_readiness PROJECT
# Prints whether per-user Antigravity usage can be measured in PROJECT: AI developer tools must be on
# (businessaicode.googleapis.com) and Metadata logging must be on so that every call writes a
# businessaicode.googleapis.com/inference_response entry with the user and token counts. Both are Gemini
# Enterprise console settings (Metadata logging is off by default); this only reports, it changes nothing.
antigravity_readiness() {
  local project="$1"
  [[ "${DRY_RUN:-0}" == "1" ]] && return 0
  if ! gcloud services list --enabled --project "$project" --filter="config.name=businessaicode.googleapis.com" \
      --format="value(config.name)" 2>/dev/null | grep -q .; then
    warn "Antigravity (AI developer tools) is not turned on in ${project}. Per-user Antigravity caps need it: Google Cloud console > Gemini Enterprise > Settings > AI developer tools."
    return 0
  fi
  if gcloud logging read 'log_id("businessaicode.googleapis.com/inference_response")' --project "$project" \
      --freshness=7d --limit=1 --format="value(timestamp)" 2>/dev/null | grep -q .; then
    info "Antigravity usage logs found in ${project}: per-user tokens, cost and caps are measured from them."
  else
    warn "No Antigravity usage log in ${project} for 7 days. Turn on Gemini Enterprise > Settings > AI developer tools > Compliance > Metadata logging (off by default; prompts and responses logging is not needed). Per-user usage is counted from then on."
  fi
}

# restore_capped_users PROJECT [ACCESS_ROLE] [INCLUDE_ADMIN_BLOCKED]
# Gives users suspended by Antigravity caps their access role back before the control plane is removed: for every
# direct, unconditional user: binding of the cap marker roles it first adds the access role, then removes the marker
# role, so the user never loses the Gemini Enterprise app. Administrator blocks are kept (and listed) unless
# INCLUDE_ADMIN_BLOCKED is true.
restore_capped_users() {
  local project="$1" access_role="${2:-roles/discoveryengine.agentspaceUser}" include_admin="${3:-false}"
  python3 - "$project" "$access_role" "$include_admin" "${DRY_RUN:-0}" <<'PY'
import json, subprocess, sys
project, access, include_admin, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "true", sys.argv[4] == "1"
cap = ["projects/%s/roles/%s" % (project, r) for r in ("CustomAntigravityCapBlocked", "CustomAntigravityRestrictedUser")]
admin = "projects/%s/roles/CustomAntigravityAdminBlocked" % project

def gc(*args):
    return subprocess.run(["gcloud", *args, "--quiet"], capture_output=True, text=True, stdin=subprocess.DEVNULL)

got = gc("projects", "get-iam-policy", project, "--format=json")
if got.returncode != 0:
    print("    warning: cannot read the IAM policy of %s; restore suspended users on the Antigravity controls page" % project,
          file=sys.stderr)
    sys.exit(0)
targets, kept = {}, []
for b in json.loads(got.stdout).get("bindings") or []:
    if b.get("condition"):
        continue
    role = b.get("role")
    for m in b.get("members") or []:
        if not str(m).startswith("user:"):
            continue
        if role in cap or (include_admin and role == admin):
            targets.setdefault(m, []).append(role)
        elif role == admin:
            kept.append(m)
if not targets:
    print("    no users suspended by Antigravity caps")
for member, roles in sorted(targets.items()):
    if dry:
        print("    [dry-run] restore %s: add %s, remove %s" % (member, access, ", ".join(roles)))
        continue
    add = gc("projects", "add-iam-policy-binding", project, "--member", member, "--role", access, "--condition=None")
    if add.returncode != 0:
        print("    warning: could not give %s back to %s; marker role kept" % (access, member), file=sys.stderr)
        continue
    for role in roles:
        gc("projects", "remove-iam-policy-binding", project, "--member", member, "--role", role, "--condition=None")
    print("    restored %s (%s)" % (member, access))
for member in sorted(set(kept)):
    print("    kept administrator block: %s (restore with --restore-blocked-users)" % member)
PY
}
