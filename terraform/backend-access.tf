# Existing resources owned by the backend team. Import is not required for data sources.
data "azurerm_user_assigned_identity" "backend_ci" {
  name                = "id-dreamplanner-backend-github"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
}

data "azurerm_linux_web_app" "backend_existing" {
  name                = "app-dreamplanner-backend-77076"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
}

resource "azurerm_role_assignment" "backend_ci_push" {
  scope                = data.azurerm_container_registry.shared.id
  role_definition_name = "AcrPush"
  principal_id         = data.azurerm_user_assigned_identity.backend_ci.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "backend_ci_deploy" {
  scope                = data.azurerm_linux_web_app.backend_existing.id
  role_definition_name = "Website Contributor"
  principal_id         = data.azurerm_user_assigned_identity.backend_ci.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "backend_system_pull" {
  scope                = data.azurerm_container_registry.shared.id
  role_definition_name = "AcrPull"
  principal_id         = data.azurerm_linux_web_app.backend_existing.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "backend_system_secrets" {
  scope                = azurerm_key_vault.database.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = data.azurerm_linux_web_app.backend_existing.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# This file does not own the App Service configuration. The separate repair script
# switches its Key Vault reference and sidecar image pull to SystemAssigned.
# Do not apply the entire existing configuration without reviewing the plan:
# database.tf still declares the deleted id-dreamplanner-backend identity.
