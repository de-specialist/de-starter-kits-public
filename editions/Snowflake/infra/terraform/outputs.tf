output "database_name" {
  description = "Isolated development database name for the dbt profile."
  value       = snowflake_database.dev.name
}

output "schema_name" {
  description = "Isolated development schema name for the dbt profile."
  value       = snowflake_schema.dev.name
}

output "warehouse_name" {
  description = "Auto-suspending development warehouse name for the dbt profile."
  value       = snowflake_warehouse.dev.name
}

output "dbt_role_name" {
  description = "Existing non-admin dbt role granted access to these resources."
  value       = var.dbt_role_name
}
