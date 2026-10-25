
output "vnet_id" {
  description = "Azure Virtual Network ID"
  value       = azurerm_virtual_network.dreamplanner.id
}

output "vnet_name" {
  description = "Azure Virtual Network name"
  value       = azurerm_virtual_network.dreamplanner.name
}

output "vnet_address_space" {
  description = "Virtual Network address space"
  value       = azurerm_virtual_network.dreamplanner.address_space
}


output "frontend_subnet_id" {
  description = "Frontend subnet resource ID"
  value       = azurerm_subnet.frontend.id
}

output "backend_subnet_id" {
  description = "Backend subnet resource ID"
  value       = azurerm_subnet.backend.id
}

output "database_subnet_id" {
  description = "Database subnet resource ID"
  value       = azurerm_subnet.database.id
}

output "postgresql_fqdn" {
  description = "Use this hostname for both public administration and private backend connections"
  value       = azurerm_postgresql_flexible_server.dreamplanner.fqdn
}

output "postgresql_database_name" {
  value = azurerm_postgresql_flexible_server_database.dreamplanner.name
}

output "postgresql_private_ip" {
  value = azurerm_private_endpoint.postgresql.private_service_connection[0].private_ip_address
}

output "database_key_vault_name" {
  value = azurerm_key_vault.database.name
}

output "database_secret_uris" {
  description = "Secret references only; passwords are never output"
  value       = { for name, secret in azurerm_key_vault_secret.database_url : name => secret.versionless_id }
}

output "backend_identity_id" {
  value = azurerm_user_assigned_identity.backend.id
}

output "backend_identity_client_id" {
  value = azurerm_user_assigned_identity.backend.client_id
}
