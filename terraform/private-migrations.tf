# Run schema migrations from the same VNet integration subnet as the backend.
# Reuse the existing plan; CI starts this app only for a migration execution.
# enabled must remain true for start/stop to work; the idle app is stopped by CI.
resource "azurerm_linux_web_app" "migrations" {
  name                                           = "app-dreamplanner-migrations-77076"
  resource_group_name                            = data.azurerm_resource_group.dreamplanner.name
  location                                       = data.azurerm_resource_group.dreamplanner.location
  service_plan_id                                = data.azurerm_linux_web_app.backend_existing.service_plan_id
  virtual_network_subnet_id                      = azurerm_subnet.backend.id
  https_only                                     = true
  enabled                                        = true
  public_network_access_enabled                  = true
  ftp_publish_basic_authentication_enabled       = false
  webdeploy_publish_basic_authentication_enabled = false

  identity { type = "SystemAssigned" }

  site_config {
    always_on                               = false
    minimum_tls_version                     = "1.2"
    scm_minimum_tls_version                 = "1.2"
    ftps_state                              = "Disabled"
    container_registry_use_managed_identity = true
    ip_restriction_default_action           = "Deny"
    scm_ip_restriction_default_action       = "Deny"
    app_command_line                        = "python -c exec(__import__('base64').b64decode(__import__('os').environ['MIGRATION_JOB_SCRIPT']))"
    application_stack {
      docker_image_name   = "dreamplanner-backend:${var.migration_bootstrap_image_tag}"
      docker_registry_url = "https://${data.azurerm_container_registry.shared.login_server}"
    }
  }

  app_settings = {
    WEBSITES_PORT                       = "8000"
    WEBSITES_ENABLE_APP_SERVICE_STORAGE = "false"
    MIGRATION_JOB_SCRIPT                = filebase64("${path.module}/../database/scripts/migration_service.py")
    MIGRATION_DATABASE_URL              = "@Microsoft.KeyVault(SecretUri=https://${var.database_key_vault_name}.vault.azure.net/secrets/database-url-migration/)"
    MIGRATION_EXPECTED_RELEASE          = ""
    MIGRATION_RUN_ID                    = "unconfigured"
    PGCONNECT_TIMEOUT                   = "15"
    PGOPTIONS                           = "-c lock_timeout=30s -c statement_timeout=300s"
  }

  lifecycle {
    # CI owns the current image, temporary status allowlist, and execution ID.
    ignore_changes = [
      enabled,
      app_settings["MIGRATION_RUN_ID"],
      app_settings["MIGRATION_EXPECTED_RELEASE"],
      site_config[0].application_stack[0].docker_image_name,
      site_config[0].ip_restriction,
    ]
  }
}

variable "migration_bootstrap_image_tag" {
  description = "Existing backend image SHA used for first provisioning only. CI updates it."
  default     = "2594d9cf2e2652db92f4d3b261c36f737b1cc4a6"
  type        = string
}

resource "azurerm_role_assignment" "migration_app_pull" {
  scope                = data.azurerm_container_registry.shared.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_linux_web_app.migrations.identity[0].principal_id
}

resource "azurerm_role_assignment" "migration_app_secret" {
  scope                = "${data.azurerm_resource_group.dreamplanner.id}/providers/Microsoft.KeyVault/vaults/${var.database_key_vault_name}/secrets/database-url-migration"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_linux_web_app.migrations.identity[0].principal_id
}

resource "azurerm_role_assignment" "backend_ci_private_migrations" {
  scope                = azurerm_linux_web_app.migrations.id
  role_definition_name = "Website Contributor"
  principal_id         = data.azurerm_user_assigned_identity.backend_ci.principal_id
}
