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
  description = "Container image to deploy, for example REGION-docker.pkg.dev/PROJECT/REPOSITORY/ai-control-plane:2.0.0."
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
