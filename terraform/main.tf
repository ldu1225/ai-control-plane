# AI Control Plane - Cloud Run deployment with a least-privilege service identity.
#
# - Read paths (dashboards) need only viewer-level roles.
# - Governance write actions (Antigravity caps, per-user allow/disallow, marker role setup) are
#   optional and disabled by default (var.enable_governance_actions).
# - Settings edited in the UI are persisted to a private, versioned GCS bucket (SETTINGS_BUCKET).
# - Identity-Aware Proxy is enabled directly on the Cloud Run service (no load balancer needed).
#   The application verifies the signed IAP header (x-goog-iap-jwt-assertion) itself.
# - Optional deny-policy enforcement (var.enforcement_backend = "iam_deny", README 8.9): a Cloud Run job from the
#   same image applies Antigravity suspensions with an IAM deny policy on a Cloud Scheduler schedule, as its own
#   service account. Organization roles are granted only with var.grant_org_roles. Off by default.
# - Optional real-time enforcement (var.realtime_enforcement): Log Router sink -> Pub/Sub -> push subscription. Only for
#   deployments behind an external HTTPS load balancer with IAP (enable_iap = false), because IAP on the Cloud Run
#   service blocks the Pub/Sub OIDC token and this module deploys IAP mode only.
#
# Not created by this module (scripts/deploy.sh does these; do them by hand or keep using the script for them):
# - the container image (build it, then set container_image), the Artifact Registry repository;
# - the BigQuery audit dataset and the Log Router sink of the AI logs (README 6.1), Data Access audit logs and the
#   Gemini Enterprise observability / prompt logging switches;
# - Secret Manager secrets and the Google sign-in modes: this module deploys IAP mode only;
# - Cloud Scheduler jobs that call the dashboard (the web enforcement schedule of iam_allow_swap / notify_only and the
#   maintenance tick): IAP rejects Cloud Scheduler's OIDC token. With IAP the maintenance work runs in-process while
#   an instance is up, and users suspended with iam_allow_swap are restored when an administrator selects Run
#   enforcement now after a period reset. For scheduled restores behind IAP use enforcement_backend = "iam_deny",
#   whose job is run through the Cloud Run Admin API;
# - the custom roles: marker roles (created from the dashboard with Create marker roles when governance actions are
#   on), the Gemini Enterprise license reader (CustomAiControlPlaneLicenseReader) and the Agent Gateway manager
#   (CustomAiControlPlaneGatewayManager) and the Gemini Enterprise feature manager
#   (CustomAiControlPlaneGeFeatureManager); without the license reader, Gemini Enterprise seats stay hidden, and without
#   the feature manager the Gemini Enterprise feature matrix is read-only;
# - grants on linked projects (SCAN_PROJECTS) and on folders or organizations of a workspace scope (roles/browser).
#
# Projects without an organization must enable IAP for Cloud Run once in the Google Cloud
# console (or configure a custom OAuth client) before applying, because OAuth clients cannot be
# created programmatically.
#
# Usage:
#   cp terraform.tfvars.example terraform.tfvars   # then edit
#   terraform init && terraform plan && terraform apply

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source = "hashicorp/google"
      # Verified against 7.39.0: google_cloud_run_v2_service.iap_enabled and
      # google_iap_web_cloud_run_service_iam_member are available in the GA provider.
      version = ">= 7.0.0, < 8.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  sa_member       = "serviceAccount:${google_service_account.dashboard_sa.email}"
  billing_project = var.billing_project_id != "" ? var.billing_project_id : var.project_id
  quota_project   = var.quota_project_id != "" ? var.quota_project_id : var.project_id
  settings_bucket = var.settings_bucket_name != "" ? var.settings_bucket_name : "${var.project_id}-control-plane-settings"

  # Signed-header audience for IAP on Cloud Run (built from variables to avoid a dependency cycle).
  iap_audience = var.iap_audience != "" ? var.iap_audience : (
    var.enable_iap ? "/projects/${data.google_project.this.number}/locations/${var.region}/services/${var.service_name}" : ""
  )

  # Read-only roles used by the dashboards.
  read_roles = [
    "roles/bigquery.dataViewer",               # audit/telemetry datasets and the billing export
    "roles/bigquery.jobUser",                  # run parameterized queries
    "roles/aiplatform.user",                   # Gemini (AI analyst, spike analysis), Agent Engine listing
    "roles/discoveryengine.viewer",            # licenses, engines, assistants, agents
    "roles/monitoring.viewer",                 # per-model token and invocation metrics, log activity
    "roles/logging.viewer",                    # inference logs, governance audit log, log discovery
    "roles/iam.securityReviewer",              # project IAM policy (Antigravity 6-state classification)
    "roles/serviceusage.serviceUsageConsumer", # required because every call sends x-goog-user-project
    "roles/logging.logWriter",                 # governance audit entries (always, with or without IAM write roles)
    "roles/logging.privateLogViewer",          # Data Access audit logs of the AI services
    "roles/cloudtrace.user",                   # agent detail page: traces of Agent Engine deployments
    "roles/modelarmor.viewer",                 # Model Armor templates and floor settings
    "roles/networkservices.viewer",            # Agent Gateway inventory
    "roles/networksecurity.viewer",            # Agent Gateway authorization policies
  ]

  # APIs some pages need that organizations sometimes restrict (agent detail traces, Model Armor, Agent Gateway and
  # Google spend caps). enable_optional_apis = false leaves them alone; those pages then report what is missing.
  optional_apis = var.enable_optional_apis ? [
    "cloudtrace.googleapis.com",
    "modelarmor.googleapis.com",
    "networkservices.googleapis.com",
    "networksecurity.googleapis.com",
    "billingbudgets.googleapis.com",
  ] : []

  # APIs the dashboards call (BigQuery, Cloud Logging and Monitoring, Gemini Enterprise, Gemini Platform,
  # IAM policy reads, the settings bucket and the public price list used for per-user cost caps).
  # Deny-policy enforcement adds Cloud Scheduler, which runs the enforcement job.
  required_apis = concat([
    "bigquery.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "discoveryengine.googleapis.com",
    "aiplatform.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
    "cloudbilling.googleapis.com",
    "serviceusage.googleapis.com",
  ], var.enable_iap ? ["iap.googleapis.com"] : [], local.deny_enabled ? ["cloudscheduler.googleapis.com"] : [], local.realtime_enabled ? ["pubsub.googleapis.com"] : [], local.optional_apis)

  # Optional governance write roles (Antigravity cap enforcement and marker role setup).
  governance_roles = [
    "roles/resourcemanager.projectIamAdmin", # atomic etag-guarded setIamPolicy
    "roles/iam.roleAdmin",                   # create marker custom roles
  ]

  # Deny-policy enforcement (README 8.9): the Cloud Run job SERVICE_NAME-enforcer runs one enforcement cycle per
  # execution as its own service account. That account reads usage, logs, BigQuery, licenses and the settings object
  # and writes audit entries; its only IAM write access is Deny Admin on the organization (grant_org_roles).
  deny_enabled = var.enforcement_backend == "iam_deny"
  # Real-time pushes start enforcement runs, which need either the deny-policy job or the governance write roles
  # (as in scripts/deploy.sh); notify_only never changes access.
  realtime_enabled        = var.realtime_enforcement && var.enforcement_backend != "notify_only" && (local.deny_enabled || var.enable_governance_actions)
  usage_events_topic_name = "${var.service_name}-agy-usage"
  usage_events_sub_id     = "${var.service_name}-agy-usage-push"
  usage_events_sub_name   = "projects/${var.project_id}/subscriptions/${local.usage_events_sub_id}"
  usage_event_path        = "/api/v1/governance/antigravity/usage-event"
  # Deterministic Cloud Run URL (https://SERVICE-PROJECT_NUMBER.REGION.run.app), known before the service exists. The
  # application accepts scheduled and Pub/Sub push calls only with a Google token whose audience is PUBLIC_BASE_URL.
  run_url                = "https://${var.service_name}-${data.google_project.this.number}.${var.region}.run.app"
  public_base_url        = var.public_base_url != "" ? trimsuffix(var.public_base_url, "/") : local.run_url
  enforcement_job_region = var.enforcement_job_region != "" ? var.enforcement_job_region : var.region
  enforcer_job_name      = "${var.service_name}-enforcer"
  enforcement_job_name   = "projects/${var.project_id}/locations/${local.enforcement_job_region}/jobs/${local.enforcer_job_name}"
  enforcement_schedule   = contains(["", "off"], trimspace(var.enforcement_schedule)) ? "" : trimspace(var.enforcement_schedule)
  enforcer_roles = [
    "roles/bigquery.dataViewer",               # audit/telemetry datasets and the billing export (prices)
    "roles/bigquery.jobUser",                  # run parameterized queries
    "roles/discoveryengine.viewer",            # licenses
    "roles/logging.viewer",                    # inference logs
    "roles/logging.privateLogViewer",          # Data Access audit logs
    "roles/logging.logWriter",                 # governance audit entries (logging.logEntries.create)
    "roles/serviceusage.serviceUsageConsumer", # required because every call sends x-goog-user-project
  ]

  # Environment overrides read by the application. Empty values are omitted so that
  # config.yaml defaults apply. PORT is reserved and injected by Cloud Run.
  env = {
    HOST                       = "0.0.0.0"
    PROJECT_ID                 = var.project_id
    QUOTA_PROJECT_ID           = var.quota_project_id
    AUDIT_DATASET_ID           = var.audit_dataset_id
    BILLING_PROJECT_ID         = var.billing_project_id
    BILLING_DATASET_ID         = var.billing_dataset_id
    BILLING_TABLE_ID           = var.billing_table_id
    BILLING_ACCOUNT_ID         = var.billing_account_id
    WORKSPACE_TIMEZONE         = var.workspace_timezone
    ORGANIZATION_NAME          = var.organization_name
    PRODUCT_NAME               = var.product_name
    DEFAULT_LOCALE             = var.default_locale
    DISCOVERY_ENGINE_LOCATIONS = join(",", var.discovery_engine_locations)
    AGENT_ENGINE_REGIONS       = join(",", var.agent_engine_regions)
    AUTH_MODE                  = var.auth_mode
    IAP_AUDIENCE               = local.iap_audience
    ADMIN_EMAILS               = join(",", var.admin_emails)
    SETTINGS_BUCKET            = google_storage_bucket.settings.name
    AI_DEFAULT_MODEL           = var.ai_default_model
    AI_LOCATION                = var.ai_location
    # Enforcement backend: set only when it is not the default, so existing deployments keep their revision.
    ENFORCEMENT_BACKEND       = var.enforcement_backend != "iam_allow_swap" ? var.enforcement_backend : ""
    DENY_ATTACHMENT           = local.deny_enabled ? var.deny_attachment : ""
    ENFORCEMENT_JOB           = local.deny_enabled ? local.enforcement_job_name : ""
    RUNTIME_SERVICE_ACCOUNT   = local.realtime_enabled ? google_service_account.dashboard_sa.email : ""
    USAGE_EVENTS_SUBSCRIPTION = local.realtime_enabled ? local.usage_events_sub_name : ""
    # Scheduled and Pub/Sub push calls are accepted only with a token for this URL (security.public_base_url).
    PUBLIC_BASE_URL = var.public_base_url != "" || local.realtime_enabled ? local.public_base_url : ""
  }
}

# 1. Dedicated service identity for the Cloud Run service.
resource "google_service_account" "dashboard_sa" {
  account_id   = var.service_account_id
  display_name = "AI Control Plane runtime"
  description  = "Least-privilege identity for the AI Control Plane Cloud Run service"
  project      = var.project_id
}

# 2. Read-path roles on the workspace project.
resource "google_project_iam_member" "read" {
  for_each = toset(local.read_roles)
  project  = var.project_id
  role     = each.value
  member   = local.sa_member
}

# Billing export in a different project: read access on that dataset only.
resource "google_bigquery_dataset_iam_member" "billing_reader" {
  count      = local.billing_project != var.project_id ? 1 : 0
  project    = local.billing_project
  dataset_id = var.billing_dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = local.sa_member
}

# Separate quota project: allow it to be used as x-goog-user-project.
resource "google_project_iam_member" "quota_consumer" {
  count   = local.quota_project != var.project_id ? 1 : 0
  project = local.quota_project
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = local.sa_member
}

# Optional: read folder/organization IAM policies so inherited Antigravity grants can be
# detected (otherwise affected users are reported as UNKNOWN / ANCESTOR_UNCHECKED).
resource "google_organization_iam_member" "ancestor_policy_reader" {
  count  = var.organization_id != "" ? 1 : 0
  org_id = var.organization_id
  role   = "roles/iam.securityReviewer"
  member = local.sa_member
}

# The audit log writer used to be a governance role; keep an existing binding instead of re-creating it.
moved {
  from = google_project_iam_member.governance["roles/logging.logWriter"]
  to   = google_project_iam_member.read["roles/logging.logWriter"]
}

# 3. Optional governance write roles (disabled by default).
resource "google_project_iam_member" "governance" {
  for_each = var.enable_governance_actions ? toset(local.governance_roles) : toset([])
  project  = var.project_id
  role     = each.value
  member   = local.sa_member
}

# 4. Private, versioned bucket for settings saved from the UI.
resource "google_storage_bucket" "settings" {
  name                        = local.settings_bucket
  project                     = var.project_id
  location                    = var.settings_bucket_location != "" ? var.settings_bucket_location : var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 50
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket_iam_member" "settings_object_admin" {
  bucket = google_storage_bucket.settings.name
  role   = "roles/storage.objectAdmin"
  member = local.sa_member
}

# 5. Required APIs (left enabled on destroy: other workloads may use them).
resource "google_project_service" "apis" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# 6. Cloud Run v2 service (not public: there is no allUsers invoker binding).
resource "google_cloud_run_v2_service" "dashboard" {
  name        = var.service_name
  location    = var.region
  project     = var.project_id
  ingress     = var.ingress
  iap_enabled = var.enable_iap

  template {
    service_account = google_service_account.dashboard_sa.email

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
      }

      dynamic "env" {
        for_each = { for name, value in local.env : name => value if value != "" }
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.read,
    google_project_iam_member.governance,
    google_storage_bucket_iam_member.settings_object_admin,
  ]
}

# 7. IAP: the IAP service agent invokes the service; listed principals may pass IAP.
resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  count    = var.enable_iap ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.dashboard.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-iap.iam.gserviceaccount.com"
}

resource "google_iap_web_cloud_run_service_iam_member" "access" {
  for_each               = var.enable_iap ? toset(var.iap_members) : toset([])
  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.dashboard.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

# 8. Optional deny-policy enforcement (enforcement_backend = "iam_deny", README 8.9). Nothing below exists by default.
resource "google_service_account" "enforcer_sa" {
  count        = local.deny_enabled ? 1 : 0
  account_id   = var.enforcer_service_account_id
  display_name = "AI Control Plane enforcement job"
  description  = "Identity of the ${local.enforcer_job_name} Cloud Run job: applies Antigravity cap suspensions with an IAM deny policy"
  project      = var.project_id
}

# Read access for usage, logs, BigQuery and licenses plus audit entries; nothing else at project level.
resource "google_project_iam_member" "enforcer" {
  for_each = local.deny_enabled ? toset(local.enforcer_roles) : toset([])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.enforcer_sa[0].email}"
}

resource "google_bigquery_dataset_iam_member" "enforcer_billing_reader" {
  count      = local.deny_enabled && local.billing_project != var.project_id ? 1 : 0
  project    = local.billing_project
  dataset_id = var.billing_dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.enforcer_sa[0].email}"
}

resource "google_project_iam_member" "enforcer_quota_consumer" {
  count   = local.deny_enabled && local.quota_project != var.project_id ? 1 : 0
  project = local.quota_project
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.enforcer_sa[0].email}"
}

# The job reads the administrators' policy from the settings object and records suspensions and runs in it.
resource "google_storage_bucket_iam_member" "enforcer_settings_object_admin" {
  count  = local.deny_enabled ? 1 : 0
  bucket = google_storage_bucket.settings.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.enforcer_sa[0].email}"
}

# The same image runs one enforcement cycle per execution with the dashboard's configuration. It serves no requests,
# so it gets no secrets; PYTHONPATH lets `python -m controlplane.enforce_job` find the package in /app/src.
resource "google_cloud_run_v2_job" "enforcer" {
  count    = local.deny_enabled ? 1 : 0
  name     = local.enforcer_job_name
  location = local.enforcement_job_region
  project  = var.project_id
  # Stateless: switching enforcement_backend away from iam_deny removes it in the same apply.
  deletion_protection = false

  template {
    task_count = 1

    template {
      service_account = google_service_account.enforcer_sa[0].email
      max_retries     = 0
      timeout         = "600s"

      containers {
        image   = var.container_image
        command = ["python"]
        args    = ["-m", "controlplane.enforce_job"]

        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
        }

        dynamic "env" {
          for_each = { for name, value in merge(local.env, { PYTHONPATH = "/app/src" }) : name => value if value != "" }
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = !var.grant_org_roles || var.organization_id != ""
      error_message = "grant_org_roles needs organization_id (the numeric organization id): Deny Admin and Deny Reviewer can be granted on an organization only."
    }
  }

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.enforcer,
    google_storage_bucket_iam_member.enforcer_settings_object_admin,
  ]
}

# Who may run the job, on this job only: the dashboard ("Run enforcement now", and reading the job and its executions
# for the status card) and the scheduler's service account.
resource "google_cloud_run_v2_job_iam_member" "dashboard_runner" {
  for_each = local.deny_enabled ? toset(["roles/run.jobsExecutor", "roles/run.viewer"]) : toset([])
  project  = var.project_id
  location = local.enforcement_job_region
  name     = google_cloud_run_v2_job.enforcer[0].name
  role     = each.value
  member   = local.sa_member
}

resource "google_service_account" "scheduler_sa" {
  count        = local.deny_enabled && local.enforcement_schedule != "" ? 1 : 0
  account_id   = var.scheduler_service_account_id
  display_name = "AI Control Plane enforcement scheduler"
  description  = "Cloud Scheduler identity that may only run the ${local.enforcer_job_name} Cloud Run job"
  project      = var.project_id
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runner" {
  count    = local.deny_enabled && local.enforcement_schedule != "" ? 1 : 0
  project  = var.project_id
  location = local.enforcement_job_region
  name     = google_cloud_run_v2_job.enforcer[0].name
  role     = "roles/run.jobsExecutor"
  member   = "serviceAccount:${google_service_account.scheduler_sa[0].email}"
}

# Cloud Scheduler runs the job through the Cloud Run Admin API with an OAuth token (the dashboard is not called, so
# IAP never applies).
resource "google_cloud_scheduler_job" "enforcer" {
  count            = local.deny_enabled && local.enforcement_schedule != "" ? 1 : 0
  name             = local.enforcer_job_name
  project          = var.project_id
  region           = local.enforcement_job_region
  description      = "Runs the ${local.enforcer_job_name} Cloud Run job (Antigravity deny-policy enforcement)"
  schedule         = local.enforcement_schedule
  time_zone        = var.workspace_timezone
  attempt_deadline = "180s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/${local.enforcement_job_name}:run"
    body        = base64encode("{}")
    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = google_service_account.scheduler_sa[0].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_job_iam_member.scheduler_runner,
  ]
}

# Deny Admin (manage deny policies) for the job only, Deny Reviewer (read them for the status card) for the dashboard.
# Both can be granted on an organization only; applying needs Organization Administrator there.
# TODO(security): Deny Admin is organization-wide; the job's invariants confine its writes to the fixed
# ai-control-plane-* policy. Narrow it with an IAM condition on the policy name if IAM supports one for deny policies.
resource "google_organization_iam_member" "enforcer_deny_admin" {
  count  = local.deny_enabled && var.grant_org_roles ? 1 : 0
  org_id = var.organization_id
  role   = "roles/iam.denyAdmin"
  member = "serviceAccount:${google_service_account.enforcer_sa[0].email}"
}

resource "google_organization_iam_member" "dashboard_deny_reviewer" {
  count  = local.deny_enabled && var.grant_org_roles ? 1 : 0
  org_id = var.organization_id
  role   = "roles/iam.denyReviewer"
  member = local.sa_member
}

# 9. Optional real-time Antigravity cap enforcement (realtime_enforcement = true). Routes each Antigravity
# inference log entry through Pub/Sub push to /api/v1/governance/antigravity/usage-event with an OIDC token
# of the runtime service account. Only for deployments behind an external HTTPS load balancer with IAP: it requires
# enable_iap = false because IAP on the Cloud Run service blocks Pub/Sub OIDC tokens, and enable_governance_actions =
# true or enforcement_backend = "iam_deny" (otherwise nothing is created). Linked projects need their own sink to this
# topic (scripts/deploy.sh creates them; Terraform does not).
resource "google_pubsub_topic" "usage_events" {
  count   = local.realtime_enabled ? 1 : 0
  name    = local.usage_events_topic_name
  project = var.project_id

  lifecycle {
    precondition {
      condition     = !var.enable_iap
      error_message = "realtime_enforcement requires enable_iap = false (IAP on the Cloud Run service blocks Pub/Sub OIDC push tokens): use it behind an external HTTPS load balancer with IAP."
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_logging_project_sink" "usage_events" {
  count                  = local.realtime_enabled ? 1 : 0
  name                   = local.usage_events_topic_name
  project                = var.project_id
  destination            = "pubsub.googleapis.com/projects/${var.project_id}/topics/${google_pubsub_topic.usage_events[0].name}"
  filter                 = "log_id(\"businessaicode.googleapis.com/inference_response\")"
  description            = "AI Control Plane real-time Antigravity usage events"
  unique_writer_identity = true

  depends_on = [google_pubsub_topic.usage_events]
}

resource "google_pubsub_topic_iam_member" "usage_events_sink_publisher" {
  count   = local.realtime_enabled ? 1 : 0
  project = var.project_id
  topic   = google_pubsub_topic.usage_events[0].name
  role    = "roles/pubsub.publisher"
  member  = google_logging_project_sink.usage_events[0].writer_identity
}

resource "google_service_account_iam_member" "pubsub_token_creator" {
  count              = local.realtime_enabled ? 1 : 0
  service_account_id = google_service_account.dashboard_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "usage_events_invoker" {
  count    = local.realtime_enabled ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.dashboard.name
  role     = "roles/run.invoker"
  member   = local.sa_member
}

resource "google_pubsub_subscription" "usage_events_push" {
  count                      = local.realtime_enabled ? 1 : 0
  name                       = local.usage_events_sub_id
  project                    = var.project_id
  topic                      = google_pubsub_topic.usage_events[0].name
  ack_deadline_seconds       = 60
  message_retention_duration = "600s"

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "60s"
  }

  push_config {
    # The run.app URL is reachable without IAP (enable_iap = false) even when users come through a load balancer; the
    # token names PUBLIC_BASE_URL, the only audience the application accepts.
    push_endpoint = "${local.run_url}${local.usage_event_path}"

    oidc_token {
      service_account_email = google_service_account.dashboard_sa.email
      audience              = "${local.public_base_url}${local.usage_event_path}"
    }
  }

  depends_on = [
    google_cloud_run_v2_service.dashboard,
    google_service_account_iam_member.pubsub_token_creator,
    google_cloud_run_v2_service_iam_member.usage_events_invoker,
  ]
}

output "service_url" {
  value       = google_cloud_run_v2_service.dashboard.uri
  description = "Service URL. With enable_iap, IAP on Cloud Run authenticates users directly (no load balancer needed); projects without an organization must enable IAP once in the console first."
}

output "service_account_email" {
  value       = google_service_account.dashboard_sa.email
  description = "Runtime service identity of the AI Control Plane."
}

output "settings_bucket" {
  value       = google_storage_bucket.settings.name
  description = "GCS bucket that persists settings saved from the UI."
}

output "iap_audience" {
  value       = local.iap_audience
  description = "Audience the application expects in the signed IAP header (IAP_AUDIENCE)."
}

output "enforcement_job" {
  value       = local.deny_enabled ? local.enforcement_job_name : ""
  description = "Cloud Run job that applies deny-policy enforcement (enforcement_backend = iam_deny); empty otherwise."
}

output "enforcer_service_account_email" {
  value       = local.deny_enabled ? google_service_account.enforcer_sa[0].email : ""
  description = "Service account of the enforcement job (the only identity that needs Deny Admin); empty without iam_deny."
}

output "usage_events_subscription" {
  value       = local.realtime_enabled ? local.usage_events_sub_name : ""
  description = "Pub/Sub push subscription for real-time Antigravity cap enforcement (realtime_enforcement = true); empty otherwise."
}

output "organization_role_commands" {
  value = local.deny_enabled && !var.grant_org_roles ? [
    "gcloud organizations add-iam-policy-binding ${var.organization_id != "" ? var.organization_id : "ORGANIZATION_ID"} --member=serviceAccount:${google_service_account.enforcer_sa[0].email} --role=roles/iam.denyAdmin --condition=None",
    "gcloud organizations add-iam-policy-binding ${var.organization_id != "" ? var.organization_id : "ORGANIZATION_ID"} --member=${local.sa_member} --role=roles/iam.denyReviewer --condition=None",
  ] : []
  description = "With iam_deny and grant_org_roles = false: the commands an organization administrator runs (until then every enforcement run stops at the safety gates)."
}


