variable "resource_prefix" {
  description = "Unique name prefix restricted to clearly non-production DBT_KIT_DEV or DBT_KIT_CI resources."
  type        = string
  default     = "DBT_KIT_DEV_TF"

  validation {
    condition     = can(regex("^DBT_KIT_(DEV|CI)(_[A-Z0-9]+)*$", var.resource_prefix))
    error_message = "resource_prefix must begin with DBT_KIT_DEV or DBT_KIT_CI and contain only uppercase letters, digits, and underscores."
  }
}

variable "dbt_role_name" {
  description = "Name of an existing non-admin account role that dbt will use in this isolated development target."
  type        = string

  validation {
    condition = can(regex("^[A-Za-z][A-Za-z0-9_$]*$", var.dbt_role_name)) && !contains(
      ["ACCOUNTADMIN", "ORGADMIN", "PUBLIC", "SECURITYADMIN", "SYSADMIN", "USERADMIN"],
      upper(var.dbt_role_name)
    )
    error_message = "dbt_role_name must be an unquoted custom non-admin role identifier; built-in admin and PUBLIC roles are not accepted."
  }
}

variable "warehouse_size" {
  description = "Small standard warehouse size for the disposable development target."
  type        = string
  default     = "XSMALL"

  validation {
    condition     = contains(["XSMALL", "SMALL"], upper(var.warehouse_size))
    error_message = "warehouse_size must be XSMALL or SMALL for this development module."
  }
}

variable "warehouse_auto_suspend_seconds" {
  description = "Idle time before Snowflake suspends the development warehouse."
  type        = number
  default     = 60

  validation {
    condition     = var.warehouse_auto_suspend_seconds >= 60 && var.warehouse_auto_suspend_seconds <= 600
    error_message = "warehouse_auto_suspend_seconds must be between 60 and 600 seconds."
  }
}

variable "warehouse_statement_timeout_seconds" {
  description = "Maximum runtime for any single statement on the development warehouse; bounds runaway query cost."
  type        = number
  default     = 1800

  validation {
    condition     = var.warehouse_statement_timeout_seconds >= 60 && var.warehouse_statement_timeout_seconds <= 3600
    error_message = "warehouse_statement_timeout_seconds must be between 60 and 3600 seconds."
  }
}

variable "data_retention_time_in_days" {
  description = "Short Time Travel retention for this disposable development database."
  type        = number
  default     = 1

  validation {
    condition     = var.data_retention_time_in_days >= 0 && var.data_retention_time_in_days <= 1
    error_message = "data_retention_time_in_days must be 0 or 1 for this development target."
  }
}
