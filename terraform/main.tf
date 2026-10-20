# AI Control Plane - Cloud Run deployment with a least-privilege service identity.
#
# - Read paths (dashboards) need only viewer-level roles.
# - Governance write actions (Antigravity caps, per-user allow/disallow, marker role setup) are
#   optional and disabled by default (var.enable_governance_actions).
# - Settings edited in the UI are persisted to a private, versioned GCS bucket (SETTINGS_BUCKET).
# - Identity-Aware Proxy is enabled directly on the Cloud Run service (no load balancer needed).
#   The application verifies the signed IAP header (x-goog-iap-jwt-assertion) itself.
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
  ]

  # APIs the dashboards call (BigQuery, Cloud Logging and Monitoring, Gemini Enterprise, Vertex AI,
  # IAM policy reads, the settings bucket and the public price list used for per-user cost caps).
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
  ], var.enable_iap ? ["iap.googleapis.com"] : [])

  # Optional governance write roles (Antigravity cap enforcement and marker role setup).
  governance_roles = [
    "roles/resourcemanager.projectIamAdmin", # atomic etag-guarded setIamPolicy
    "roles/logging.logWriter",               # governance audit entries
    "roles/iam.roleAdmin",                   # create marker custom roles
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
