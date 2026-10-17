
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }

  backend "azurerm" {}
}

provider "azurerm" {
  features {}
}

data "azurerm_resource_group" "dreamplanner" {
  name = var.resource_group_name
}

resource "azurerm_virtual_network" "dreamplanner" {
  name                = var.vnet_name
  location            = data.azurerm_resource_group.dreamplanner.location
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
  address_space       = [var.vnet_address_space]

  tags = {
    Project     = "DreamPlanner"
    Environment = "Development"
    ManagedBy   = "Terraform"
  }
}
