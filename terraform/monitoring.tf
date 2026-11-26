# Shared telemetry for the web application, API and database.
resource "azurerm_log_analytics_workspace" "monitoring" {
  name                = "log-dreamplanner-dev-77076"
  location            = data.azurerm_resource_group.dreamplanner.location
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.1
  tags                = local.database_tags
}

resource "azurerm_application_insights" "monitoring" {
  name                 = "appi-dreamplanner-dev-77076"
  location             = data.azurerm_resource_group.dreamplanner.location
  resource_group_name  = data.azurerm_resource_group.dreamplanner.name
  workspace_id         = azurerm_log_analytics_workspace.monitoring.id
  application_type     = "web"
  retention_in_days    = 30
  daily_data_cap_in_gb = 0.1
  tags                 = local.database_tags
}

# Use an existing resource ID here to keep a monitoring-only plan independent
# of legacy database identity declarations in this configuration.
locals {
  monitoring_postgresql_id = "${data.azurerm_resource_group.dreamplanner.id}/providers/Microsoft.DBforPostgreSQL/flexibleServers/${var.postgresql_server_name}"
  monitoring_web_apps = {
    backend  = "${data.azurerm_resource_group.dreamplanner.id}/providers/Microsoft.Web/sites/app-dreamplanner-backend-77076"
    frontend = "${data.azurerm_resource_group.dreamplanner.id}/providers/Microsoft.Web/sites/app-dreamplanner-frontend-77076"
  }
}

resource "azurerm_monitor_diagnostic_setting" "postgresql" {
  name                           = "postgresql-to-log-analytics"
  target_resource_id             = local.monitoring_postgresql_id
  log_analytics_workspace_id     = azurerm_log_analytics_workspace.monitoring.id
  log_analytics_destination_type = "Dedicated"
  enabled_log { category = "PostgreSQLLogs" }
  enabled_metric { category = "AllMetrics" }
}

resource "azurerm_monitor_diagnostic_setting" "web_apps" {
  for_each                   = local.monitoring_web_apps
  name                       = "application-to-log-analytics"
  target_resource_id         = each.value
  log_analytics_workspace_id = azurerm_log_analytics_workspace.monitoring.id
  # App Service uses its native tables and ignores destination_type.
  enabled_log { category = "AppServiceConsoleLogs" }
  enabled_log { category = "AppServiceHTTPLogs" }
  enabled_metric { category = "AllMetrics" }
}

resource "azurerm_monitor_diagnostic_setting" "hosting_plan" {
  name                       = "hosting-to-log-analytics"
  target_resource_id         = data.azurerm_service_plan.shared.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.monitoring.id
  enabled_metric { category = "AllMetrics" }
}

variable "budget_contact_emails" {
  description = "Recipients for the project's cost alerts. Set through an untracked tfvars file."
  type        = list(string)
}

resource "azurerm_consumption_budget_resource_group" "project" {
  name              = "budget-dreamplanner-monthly"
  resource_group_id = data.azurerm_resource_group.dreamplanner.id
  amount            = 100
  time_grain        = "Monthly"
  time_period {
    start_date = "2026-11-01T00:00:00Z"
    end_date   = "2027-11-01T00:00:00Z"
  }
  # Amount is denominated in the subscription's billing currency (EUR).
  notification {
    enabled        = true
    threshold      = 80
    operator       = "GreaterThanOrEqualTo"
    threshold_type = "Actual"
    contact_emails = var.budget_contact_emails
  }
}

resource "azurerm_application_insights_workbook" "operations" {
  name                = "34657a2b-273a-4d01-bbbc-fcf801e3fa10"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
  location            = data.azurerm_resource_group.dreamplanner.location
  display_name        = "DreamPlanner — monitoring"
  description         = "API response times, HTTP statuses, hosting CPU/memory and PostgreSQL metrics."
  source_id           = lower(azurerm_log_analytics_workspace.monitoring.id)
  data_json = templatefile("${path.module}/monitoring-workbook.json", {
    workspace_id = azurerm_log_analytics_workspace.monitoring.id
  })
  tags = local.database_tags
}

output "monitoring_workspace_id" {
  value = azurerm_log_analytics_workspace.monitoring.id
}
output "application_insights_connection_string" {
  value     = azurerm_application_insights.monitoring.connection_string
  sensitive = true
}
output "monitoring_workbook_id" {
  value = azurerm_application_insights_workbook.operations.id
}
