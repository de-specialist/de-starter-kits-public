variable "project_id" {
  description = "Existing, dedicated non-production Google Cloud project for this starter-kit environment."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid Google Cloud project ID."
  }
}

variable "github_repository" {
  description = "GitHub repository allowed to run the trusted BigQuery CI workflow, in OWNER/REPO form."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository)) && lower(split("/", var.github_repository)[0]) == lower(var.github_owner)
    error_message = "github_repository must use OWNER/REPO form and its owner must match github_owner."
  }
}

variable "github_owner" {
  description = "GitHub organization or user that owns github_repository; also configures the GitHub provider target."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+$", var.github_owner))
    error_message = "github_owner must be a GitHub organization or user name."
  }
}

variable "github_repository_id" {
  description = "Immutable numeric GitHub repository ID used as the WIF principal attribute."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "github_repository_id must be the numeric ID shown in GitHub repository metadata."
  }
}

variable "github_repository_owner_id" {
  description = "Immutable numeric GitHub owner ID used to constrain WIF to the expected organization or user."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_owner_id))
    error_message = "github_repository_owner_id must be the numeric owner ID shown in GitHub metadata."
  }
}

variable "wif_deployment_suffix" {
  description = "Required short suffix used in the CI service account and WIF pool/provider IDs so deployments do not collide; choose a fresh value when recreating them during Google's soft-delete window."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{4,8}$", var.wif_deployment_suffix))
    error_message = "wif_deployment_suffix must contain 4 to 8 lowercase letters or digits."
  }
}

variable "confirm_nonproduction_project" {
  description = "Set true only after confirming project_id is a dedicated non-production project with no customer or production data."
  type        = bool
  default     = false

  validation {
    condition     = var.confirm_nonproduction_project
    error_message = "Set confirm_nonproduction_project=true only for a dedicated non-production project."
  }
}

variable "dataset_id" {
  description = "New isolated dataset name. The starterkit_ prefix prevents accidental use of ordinary production dataset names."
  type        = string
  default     = "starterkit_ci"

  validation {
    condition     = can(regex("^starterkit_[a-z0-9_]{1,90}$", var.dataset_id))
    error_message = "dataset_id must start with starterkit_ and contain only lowercase letters, digits, and underscores."
  }
}

variable "location" {
  description = "Pinned BigQuery dataset multi-region for the public TheLook source; changing location is unsupported."
  type        = string
  default     = "US"

  validation {
    condition     = var.location == "US"
    error_message = "location is pinned to the US multi-region for this disposable starter-kit dataset."
  }
}

variable "default_table_expiration_days" {
  description = "Default lifetime for tables created in the disposable CI dataset. Set 0 to disable expiration."
  type        = number
  default     = 30

  validation {
    condition     = var.default_table_expiration_days == 0 || (var.default_table_expiration_days >= 1 && var.default_table_expiration_days <= 365)
    error_message = "default_table_expiration_days must be 0 or between 1 and 365."
  }
}

variable "bigquery_daily_query_quota_mib" {
  description = "Optional project-wide BigQuery QueryUsagePerDay cap in MiB (1048576 = 1 TiB). 0 leaves the quota untouched. Needs the Cloud Quotas API and quota-update permission; destroy does not remove the cap."
  type        = number
  default     = 0

  validation {
    condition     = var.bigquery_daily_query_quota_mib == 0 || (var.bigquery_daily_query_quota_mib >= 1 && var.bigquery_daily_query_quota_mib <= 10485760 && floor(var.bigquery_daily_query_quota_mib) == var.bigquery_daily_query_quota_mib)
    error_message = "bigquery_daily_query_quota_mib must be 0 (disabled) or a whole number of MiB between 1 and 10485760 (10 TiB)."
  }
}
