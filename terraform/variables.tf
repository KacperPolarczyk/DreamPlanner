
variable "resource_group_name" {
  description = "Existing Azure Resource Group"
  type        = string
  default     = "rg-dreamplanner-dev"
}

variable "vnet_name" {
  description = "DreamPlanner Virtual Network name"
  type        = string
  default     = "dreamplanner-vnet"
}

variable "vnet_address_space" {
  description = "DreamPlanner VNet address space"
  type        = string
  default     = "10.13.0.0/16"
}
