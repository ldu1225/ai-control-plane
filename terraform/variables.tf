# Input variables for the AI Control Plane deployment.

variable "project_id" {
  type        = string
  description = "Google Cloud project that hosts the service and the audit log dataset."
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "Cloud Run region."
}

variable "service_name" {
  type        = string
  default     = "ai-control-plane"
  description = "Cloud Run service name."
}

variable "service_account_id" {
  type        = string
  default     = "ai-control-plane-sa"
  description = "Account id (6-30 characters) of the runtime service account."
}

variable "container_image" {
  type        = string
  description = "Container image to deploy, for example REGION-docker.pkg.dev/PROJECT/REPOSITORY/ai-control-plane:2.2.0."
}

variable "audit_dataset_id" {
  type        = string
  default     = "ge_analytics"
  description = "BigQuery dataset that receives the Cloud Audit, Gemini Enterprise and Model Armor log sinks."
}

variable "billing_project_id" {
  type        = string
  default     = ""
  description = "Project that contains the billing export dataset. Empty means project_id."
}

variable "billing_dataset_id" {
  type        = string
  default     = "billing_detailed_usage"
  description = "BigQuery dataset of the Cloud Billing detailed usage export."
}

variable "billing_table_id" {
  type        = string
  default     = ""
  description = "Billing export table, for example gcp_billing_export_resource_v1_XXXXXX_XXXXXX_XXXXXX. Empty disables FinOps data."
}

variable "billing_account_id" {
  type        = string
  default     = ""
  description = "Billing account id (XXXXXX-XXXXXX-XXXXXX), shown in the FinOps view."
}

variable "quota_project_id" {
  type        = string
  default     = ""
  description = "Project used for API quota and billing of API calls (x-goog-user-project). Empty means project_id."
}

variable "workspace_timezone" {
  type        = string
  default     = "UTC"
  description = "IANA time zone used for daily buckets, for example Asia/Seoul."
}

variable "organization_name" {
  type        = string
  default     = "Your Organization"
  description = "Organization name shown in the product header."
}

variable "product_name" {
  type        = string
  default     = "AI Control Plane"
  description = "Product name shown in the UI (white-label)."
}

variable "default_locale" {
  type        = string
  default     = "en"
  description = "Default UI language: en or ko (each user can switch in the UI)."

  validation {
    condition     = contains(["en", "ko"], var.default_locale)
    error_message = "default_locale must be en or ko."
  }
}

variable "discovery_engine_locations" {
  type        = list(string)
  default     = ["global"]
  description = "Gemini Enterprise (Discovery Engine) locations to read licenses, engines and agents from."
}

variable "agent_engine_regions" {
  type        = list(string)
  default     = ["us-central1"]
  description = "Agent Engine (Agent Platform) regions to list deployed agents from."
}

variable "auth_mode" {
  type        = string
  default     = "iap"
  description = "Application authentication mode. This module deploys IAP mode only (auto also resolves to iap on Cloud Run)."

  validation {
    condition     = contains(["iap", "auto"], var.auth_mode)
    error_message = "auth_mode must be iap (or auto, which resolves to iap on Cloud Run). Use scripts/deploy.sh for Google sign-in."
  }
}

variable "iap_audience" {
  type        = string
  default     = ""
  description = "Expected audience of the signed IAP header. Empty with enable_iap computes /projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME."
}

variable "enable_iap" {
  type        = bool
  default     = true
  description = "Enable Identity-Aware Proxy directly on the Cloud Run service."
}

variable "iap_members" {
  type        = list(string)
  default     = []
  description = "Principals allowed through IAP (roles/iap.httpsResourceAccessor), for example [\"group:ai-governance@example.com\"]."
}

variable "admin_emails" {
  type        = list(string)
  default     = []
  description = "Application administrators (exact emails or *@domain patterns). Other authenticated users are viewers."
}

variable "ai_default_model" {
  type        = string
  default     = "gemini-3.8-flash"
  description = "Default Gemini model for the AI analyst and spike analysis."
}

variable "ai_location" {
  type        = string
  default     = "global"
  description = "Agent Platform location for Gemini calls."
}

variable "enable_governance_actions" {
  type        = bool
  default     = false
  description = "Grant the IAM write roles needed for Antigravity cap enforcement and marker role setup."
}

variable "organization_id" {
  type        = string
  default     = ""
  description = "Optional numeric organization id. When set, the service account may read folder and organization IAM policies to detect inherited grants."
}

variable "settings_bucket_name" {
  type        = string
  default     = ""
  description = "Name of the settings bucket. Empty means PROJECT_ID-control-plane-settings."
}

variable "settings_bucket_location" {
  type        = string
  default     = ""
  description = "Location of the settings bucket. Empty means region."
}

variable "cpu" {
  type        = string
  default     = "1"
  description = "CPU limit per instance."
}

variable "memory" {
  type        = string
  default     = "1Gi"
  description = "Memory limit per instance."
}

variable "min_instances" {
  type        = number
  default     = 0
  description = "Minimum number of instances."
}

variable "max_instances" {
  type        = number
  default     = 3
  description = "Maximum number of instances."
}

variable "ingress" {
  type        = string
  default     = "INGRESS_TRAFFIC_ALL"
  description = "Cloud Run ingress. IAP on Cloud Run protects every ingress path, including the run.app URL."

  validation {
    condition = contains(["INGRESS_TRAFFIC_ALL", "INGRESS_TRAFFIC_INTERNAL_ONLY",
    "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"], var.ingress)
    error_message = "ingress must be INGRESS_TRAFFIC_ALL, INGRESS_TRAFFIC_INTERNAL_ONLY or INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER."
  }
}

# Antigravity cap enforcement backend (README 8.9). The defaults deploy nothing new.

variable "enforcement_backend" {
  type        = string
  default     = "iam_allow_swap"
  description = "How Antigravity suspensions take effect: iam_allow_swap (default; the dashboard swaps direct user bindings, needs enable_governance_actions), iam_deny (an IAM deny policy applied by the Cloud Run job SERVICE_NAME-enforcer on a schedule; needs an organization) or notify_only (plans and notifications only). Before switching away from iam_deny, let the job delete the deny policy (README 8.9): the job is removed with this setting."

  validation {
    condition     = contains(["iam_allow_swap", "iam_deny", "notify_only"], var.enforcement_backend)
    error_message = "enforcement_backend must be iam_allow_swap, iam_deny or notify_only."
  }
}

variable "deny_attachment" {
  type        = string
  default     = "projects"
  description = "iam_deny: where the deny policy is attached: projects (every workspace project) or scope (the folder or organization of the workspace scope, else every project)."

  validation {
    condition     = contains(["projects", "scope"], var.deny_attachment)
    error_message = "deny_attachment must be projects or scope."
  }
}

variable "enforcement_job_region" {
  type        = string
  default     = ""
  description = "iam_deny: region of the enforcement job and its Cloud Scheduler job. Empty means region."

  validation {
    condition     = var.enforcement_job_region == "" || can(regex("^[a-z]+-[a-z]+[0-9]+$", var.enforcement_job_region))
    error_message = "enforcement_job_region must be empty or a region such as asia-northeast3."
  }
}

variable "enforcement_schedule" {
  type        = string
  default     = "*/5 * * * *"
  description = "iam_deny: 5-field cron expression on which Cloud Scheduler runs the enforcement job (time zone: workspace_timezone). \"off\" or empty: runs only on request from the dashboard (Run enforcement now)."

  validation {
    condition = contains(["", "off"], trimspace(var.enforcement_schedule)) || can(regex("^\\S+(\\s+\\S+){4}$",
    trimspace(var.enforcement_schedule)))
    error_message = "enforcement_schedule must be a 5-field cron expression (for example */5 * * * *), \"off\" or empty."
  }
}

variable "realtime_enforcement" {
  type        = bool
  default     = false
  description = "Route Antigravity inference logs through a Pub/Sub push subscription to /api/v1/governance/antigravity/usage-event so automatic mode triggers enforcement right away instead of waiting for the next scheduled run. Only for deployments behind an external HTTPS load balancer with IAP (not created by this module): this module allows auth_mode iap or auto only, and IAP on the Cloud Run service itself blocks the Pub/Sub OIDC push token, so set enable_iap = false, iap_audience to the load balancer's backend service audience and public_base_url to the load balancer's URL. Also needs enable_governance_actions = true or enforcement_backend = \"iam_deny\"; nothing is created otherwise."
}

variable "public_base_url" {
  type        = string
  default     = ""
  description = "Public URL of the dashboard (for example https://ai-governance.example.com behind your load balancer). Empty means the run.app URL https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app. Pub/Sub push and scheduled calls must carry a Google token for this URL, so it is set (as PUBLIC_BASE_URL) whenever realtime_enforcement is on."

  validation {
    condition     = var.public_base_url == "" || can(regex("^https://[^/\\s]+(/.*)?$", var.public_base_url))
    error_message = "public_base_url must be empty or an https:// URL."
  }
}

variable "enable_optional_apis" {
  type        = bool
  default     = true
  description = "Also enable the APIs of the agent detail traces (cloudtrace), Model Armor, Agent Gateway (networkservices, networksecurity) and Google spend caps (billingbudgets). Set false when an organization policy restricts one of them; those pages then report what is missing."
}

variable "grant_org_roles" {
  type        = bool
  default     = false
  description = "iam_deny: grant Deny Admin (roles/iam.denyAdmin) to the enforcement job's service account and Deny Reviewer (roles/iam.denyReviewer) to the runtime service account on organization_id. Both roles can be granted on an organization only, and applying needs Organization Administrator. false: the output organization_role_commands lists the commands for an organization administrator."
}

variable "enforcer_service_account_id" {
  type        = string
  default     = "ai-control-plane-enforcer"
  description = "iam_deny: account id (6-30 characters) of the enforcement job's service account."
}

variable "scheduler_service_account_id" {
  type        = string
  default     = "ai-control-plane-scheduler"
  description = "iam_deny: account id (6-30 characters) of the service account Cloud Scheduler runs the enforcement job as. It may only run that job."
}
