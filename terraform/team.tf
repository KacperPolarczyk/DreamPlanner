locals {
  team_members = {
    backend  = var.backend_developer_object_id
    frontend = var.frontend_developer_object_id
  }
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
