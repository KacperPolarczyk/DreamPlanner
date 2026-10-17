
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
