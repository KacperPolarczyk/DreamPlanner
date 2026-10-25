locals {
  team_members = {
    backend  = var.backend_developer_object_id
    frontend = var.frontend_developer_object_id
  }
}

# Resource-group scope lets the backend developer create the registry and
# validate its ARM deployment before the registry resource exists.
resource "azurerm_role_assignment" "backend_registry_management" {
  scope                = data.azurerm_resource_group.dreamplanner.id
  role_definition_name = "Container Registry Contributor and Data Access Configuration Administrator"
  principal_id         = var.backend_developer_object_id
  principal_type       = "User"
}

resource "azurerm_role_assignment" "team_reader" {
  for_each             = local.team_members
  scope                = data.azurerm_resource_group.dreamplanner.id
  role_definition_name = "Reader"
  principal_id         = each.value
  principal_type       = "User"
}

resource "azurerm_role_assignment" "backend_database_secrets" {
  for_each             = toset(["app", "migration"])
  scope                = azurerm_key_vault_secret.database_url[each.key].resource_versionless_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = var.backend_developer_object_id
  principal_type       = "User"
}

resource "azurerm_role_assignment" "backend_runtime_secret" {
  scope                = azurerm_key_vault_secret.database_url["app"].resource_versionless_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.backend.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "backend_identity_operator" {
  scope                = azurerm_user_assigned_identity.backend.id
  role_definition_name = "Managed Identity Operator"
  principal_id         = var.backend_developer_object_id
  principal_type       = "User"
}

# Support independently deploying the backend and both ACR permissions modes.
resource "azurerm_role_assignment" "backend_deployment" {
  for_each = toset([
    "Contributor",
    "AcrPush",
    "Container Registry Repository Writer",
    "Container Registry Repository Catalog Lister",
  ])
  scope                = data.azurerm_resource_group.dreamplanner.id
  role_definition_name = each.value
  principal_id         = var.backend_developer_object_id
  principal_type       = "User"
}

# Inherited by registries created in this resource group. Attach this identity
# to the backend host and configure registry authentication with that identity.
resource "azurerm_role_assignment" "backend_runtime_images" {
  for_each = toset([
    "AcrPull",
    "Container Registry Repository Reader",
    "Container Registry Repository Catalog Lister",
  ])
  scope                = data.azurerm_resource_group.dreamplanner.id
  role_definition_name = each.value
  principal_id         = azurerm_user_assigned_identity.backend.principal_id
  principal_type       = "ServicePrincipal"
}
