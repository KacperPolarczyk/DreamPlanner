data "azurerm_client_config" "current" {}

locals {
  database_tags = {
    Project     = "DreamPlanner"
    Environment = "Development"
    ManagedBy   = "Terraform"
  }
  database_logins = {
    admin     = "dreamplanner_admin"
    migration = "dreamplanner_migrator"
    app       = "dreamplanner_app"
  }
}

resource "azurerm_postgresql_flexible_server" "dreamplanner" {
  name                          = var.postgresql_server_name
  resource_group_name           = data.azurerm_resource_group.dreamplanner.name
  location                      = data.azurerm_resource_group.dreamplanner.location
  zone                          = "1"
  version                       = "16"
  sku_name                      = "B_Standard_B1ms"
  storage_mb                    = 32768
  storage_tier                  = "P4"
  auto_grow_enabled             = false
  backup_retention_days         = 7
  geo_redundant_backup_enabled  = false
  public_network_access_enabled = true
  administrator_login           = local.database_logins.admin
  administrator_password        = var.database_passwords.admin

  authentication {
    password_auth_enabled         = true
    active_directory_auth_enabled = false
  }

  tags = local.database_tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_postgresql_flexible_server_database" "dreamplanner" {
  name      = "dreamplanner"
  server_id = azurerm_postgresql_flexible_server.dreamplanner.id
  charset   = "UTF8"
  collation = "en_US.utf8"

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "dba" {
  name             = "allow-dba"
  server_id        = azurerm_postgresql_flexible_server.dreamplanner.id
  start_ip_address = var.dba_public_ipv4
  end_ip_address   = var.dba_public_ipv4
}

resource "azurerm_postgresql_flexible_server_configuration" "require_tls" {
  name      = "require_secure_transport"
  server_id = azurerm_postgresql_flexible_server.dreamplanner.id
  value     = "on"
}

resource "azurerm_private_dns_zone" "postgresql" {
  name                = "privatelink.postgres.database.azure.com"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
  tags                = local.database_tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgresql" {
  name                  = "postgresql-dreamplanner-vnet"
  resource_group_name   = data.azurerm_resource_group.dreamplanner.name
  private_dns_zone_name = azurerm_private_dns_zone.postgresql.name
  virtual_network_id    = azurerm_virtual_network.dreamplanner.id
  registration_enabled  = false
  tags                  = local.database_tags
}

resource "azurerm_private_endpoint" "postgresql" {
  name                = "pe-dreamplanner-postgresql"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
  location            = data.azurerm_resource_group.dreamplanner.location
  subnet_id           = azurerm_subnet.database.id
  tags                = local.database_tags

  private_service_connection {
    name                           = "postgresql-dreamplanner"
    private_connection_resource_id = azurerm_postgresql_flexible_server.dreamplanner.id
    subresource_names              = ["postgresqlServer"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "postgresql"
    private_dns_zone_ids = [azurerm_private_dns_zone.postgresql.id]
  }

  depends_on = [azurerm_subnet_network_security_group_association.database]
}

# Attach this identity to the backend host to read only its runtime secret.
resource "azurerm_user_assigned_identity" "backend" {
  name                = "id-dreamplanner-backend"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
  location            = data.azurerm_resource_group.dreamplanner.location
  tags                = local.database_tags
}

resource "azurerm_key_vault" "database" {
  name                       = var.database_key_vault_name
  resource_group_name        = data.azurerm_resource_group.dreamplanner.name
  location                   = data.azurerm_resource_group.dreamplanner.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  soft_delete_retention_days = 7
  purge_protection_enabled   = true
  tags                       = local.database_tags
}

resource "azurerm_role_assignment" "database_secrets_dba" {
  scope                = azurerm_key_vault.database.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = var.dba_object_id
  principal_type       = "User"
}

resource "azurerm_key_vault_secret" "database_url" {
  for_each     = local.database_logins
  name         = "database-url-${each.key}"
  key_vault_id = azurerm_key_vault.database.id
  content_type = "PostgreSQL connection URI; libpq with certificate verification"
  value        = "postgresql://${each.value}:${urlencode(var.database_passwords[each.key])}@${azurerm_postgresql_flexible_server.dreamplanner.fqdn}:5432/${azurerm_postgresql_flexible_server_database.dreamplanner.name}?sslmode=verify-full&sslrootcert=${urlencode(var.database_ssl_root_cert)}"
  tags         = local.database_tags

  depends_on = [azurerm_role_assignment.database_secrets_dba]
}
