resource "snowflake_database" "dev" {
  name                        = "${var.resource_prefix}_DB"
  is_transient                = true
  data_retention_time_in_days = var.data_retention_time_in_days
  comment                     = "Disposable non-production database managed by the Snowflake starter-kit Terraform example."
}

resource "snowflake_schema" "dev" {
  name     = "${var.resource_prefix}_SCHEMA"
  database = snowflake_database.dev.name
  comment  = "Disposable non-production schema for the Snowflake starter-kit dbt example."
}

resource "snowflake_warehouse" "dev" {
  name                      = "${var.resource_prefix}_WH"
  warehouse_size            = upper(var.warehouse_size)
  auto_suspend              = var.warehouse_auto_suspend_seconds
  auto_resume               = true
  initially_suspended       = true
  enable_query_acceleration = false

  statement_timeout_in_seconds = var.warehouse_statement_timeout_seconds
  comment                      = "X-Small-or-small, auto-suspending development warehouse managed by Terraform."
}

resource "snowflake_grant_privileges_to_account_role" "dbt_database_usage" {
  account_role_name = var.dbt_role_name
  privileges        = ["USAGE"]

  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.dev.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "dbt_schema_access" {
  account_role_name = var.dbt_role_name
  privileges        = ["USAGE", "CREATE TABLE", "CREATE VIEW"]

  on_schema {
    schema_name = snowflake_schema.dev.fully_qualified_name
  }
}

resource "snowflake_grant_privileges_to_account_role" "dbt_warehouse_usage" {
  account_role_name = var.dbt_role_name
  privileges        = ["USAGE"]

  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.dev.name
  }
}
