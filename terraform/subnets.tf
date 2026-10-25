
resource "azurerm_subnet" "frontend" {
  name                 = "snet-frontend"
  resource_group_name  = data.azurerm_resource_group.dreamplanner.name
  virtual_network_name = azurerm_virtual_network.dreamplanner.name
  address_prefixes     = ["10.13.1.0/24"]
}

resource "azurerm_subnet" "backend" {
  name                 = "snet-backend"
  resource_group_name  = data.azurerm_resource_group.dreamplanner.name
  virtual_network_name = azurerm_virtual_network.dreamplanner.name
  address_prefixes     = ["10.13.2.0/24"]
}

resource "azurerm_subnet" "database" {
  name                 = "snet-database"
  resource_group_name  = data.azurerm_resource_group.dreamplanner.name
  virtual_network_name = azurerm_virtual_network.dreamplanner.name
  address_prefixes     = ["10.13.3.0/24"]

  private_endpoint_network_policies = "NetworkSecurityGroupEnabled"
}
