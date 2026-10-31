# CI needs only the migration secret and temporary firewall-rule management.
resource "azurerm_role_definition" "backend_migration_firewall" {
  name        = "DreamPlanner Backend Migration Firewall"
  scope       = data.azurerm_resource_group.dreamplanner.id
  description = "Manage PostgreSQL firewall rules for temporary CI migration access."
  permissions {
    actions = [
      "Microsoft.DBforPostgreSQL/flexibleServers/firewallRules/read",
      "Microsoft.DBforPostgreSQL/flexibleServers/firewallRules/write",
      "Microsoft.DBforPostgreSQL/flexibleServers/firewallRules/delete",
    ]
  }
  assignable_scopes = [data.azurerm_resource_group.dreamplanner.id]
}

resource "azurerm_role_assignment" "backend_ci_migration_firewall" {
  scope              = azurerm_postgresql_flexible_server.dreamplanner.id
  role_definition_id = azurerm_role_definition.backend_migration_firewall.role_definition_resource_id
  principal_id       = data.azurerm_user_assigned_identity.backend_ci.principal_id
  principal_type     = "ServicePrincipal"
}

resource "azurerm_role_assignment" "backend_ci_migration_secret" {
  scope                = azurerm_key_vault_secret.database_url["migration"].resource_versionless_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = data.azurerm_user_assigned_identity.backend_ci.principal_id
  principal_type       = "ServicePrincipal"
}
