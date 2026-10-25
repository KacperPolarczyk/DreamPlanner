
# Network Security Groups for DreamPlanner

locals {
  nsg_subnets = {
    frontend = "snet-frontend"
    backend  = "snet-backend"
    database = "snet-database"
  }
}

resource "azurerm_network_security_group" "dreamplanner" {
  for_each = local.nsg_subnets

  name                = "nsg-${each.key}"
  location            = data.azurerm_resource_group.dreamplanner.location
  resource_group_name = data.azurerm_resource_group.dreamplanner.name

  tags = {
    Project     = "DreamPlanner"
    Environment = "Development"
    ManagedBy   = "Terraform"
  }
}

# Associate NSGs with existing subnets

resource "azurerm_subnet_network_security_group_association" "frontend" {
  subnet_id                 = azurerm_subnet.frontend.id
  network_security_group_id = azurerm_network_security_group.dreamplanner["frontend"].id
}

resource "azurerm_subnet_network_security_group_association" "backend" {
  subnet_id                 = azurerm_subnet.backend.id
  network_security_group_id = azurerm_network_security_group.dreamplanner["backend"].id
}

resource "azurerm_subnet_network_security_group_association" "database" {
  subnet_id                 = azurerm_subnet.database.id
  network_security_group_id = azurerm_network_security_group.dreamplanner["database"].id
}

# FRONTEND - future HTTPS entry point
resource "azurerm_network_security_rule" "frontend_https" {
  name                        = "Allow-HTTPS-Internet"
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "443"
  source_address_prefix       = "Internet"
  destination_address_prefix  = "*"
  resource_group_name         = data.azurerm_resource_group.dreamplanner.name
  network_security_group_name = azurerm_network_security_group.dreamplanner["frontend"].name
}

# BACKEND - only frontend subnet may initiate API traffic
resource "azurerm_network_security_rule" "backend_allow_api" {
  name                        = "Allow-Frontend-API"
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "8000"
  source_address_prefix       = "10.13.1.0/24"
  destination_address_prefix  = "10.13.2.0/24"
  resource_group_name         = data.azurerm_resource_group.dreamplanner.name
  network_security_group_name = azurerm_network_security_group.dreamplanner["backend"].name
}

resource "azurerm_network_security_rule" "backend_deny_other" {
  name                        = "Deny-Other-VNet-Inbound"
  priority                    = 200
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "VirtualNetwork"
  destination_address_prefix  = "*"
  resource_group_name         = data.azurerm_resource_group.dreamplanner.name
  network_security_group_name = azurerm_network_security_group.dreamplanner["backend"].name
}

# DATABASE - only backend can reach PostgreSQL
resource "azurerm_network_security_rule" "database_allow_postgresql" {
  name                        = "Allow-Backend-PostgreSQL"
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "5432"
  source_address_prefix       = "10.13.2.0/24"
  destination_address_prefix  = "10.13.3.0/24"
  resource_group_name         = data.azurerm_resource_group.dreamplanner.name
  network_security_group_name = azurerm_network_security_group.dreamplanner["database"].name
}

resource "azurerm_network_security_rule" "database_deny_other" {
  name                        = "Deny-Other-VNet-Inbound"
  priority                    = 200
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "VirtualNetwork"
  destination_address_prefix  = "*"
  resource_group_name         = data.azurerm_resource_group.dreamplanner.name
  network_security_group_name = azurerm_network_security_group.dreamplanner["database"].name
}
