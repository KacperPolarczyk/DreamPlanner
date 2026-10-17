
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
