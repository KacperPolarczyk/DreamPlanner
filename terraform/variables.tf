
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

variable "postgresql_server_name" {
  description = "Globally unique PostgreSQL Flexible Server name"
  type        = string
  default     = "psql-dreamplanner-dev-77076"
}

variable "database_key_vault_name" {
  description = "Globally unique Key Vault name for database connection strings"
  type        = string
  default     = "kv-dreamplanner-77076"
}

variable "database_ssl_root_cert" {
  description = "Trusted CA bundle path in the Linux backend and DBA environment"
  type        = string
  default     = "/etc/ssl/certs/ca-certificates.crt"
}

variable "dba_public_ipv4" {
  description = "Single public IPv4 allowed to administer PostgreSQL"
  type        = string
  default     = "80.68.235.174"

  validation {
    condition     = can(cidrnetmask("${var.dba_public_ipv4}/32")) && var.dba_public_ipv4 != "0.0.0.0"
    error_message = "Provide one IPv4 address; 0.0.0.0 would allow Azure-wide access."
  }
}

variable "database_passwords" {
  description = "Distinct SQL passwords supplied through an ignored tfvars file or TF_VAR_database_passwords"
  type = object({
    admin     = string
    migration = string
    app       = string
  })
  sensitive = true

  validation {
    condition     = alltrue([for password in values(var.database_passwords) : length(password) >= 24]) && length(toset(values(var.database_passwords))) == 3
    error_message = "Supply three distinct passwords, each at least 24 characters long."
  }
}

variable "dba_object_id" {
  description = "DBA Microsoft Entra object ID in the project tenant"
  type        = string
  default     = "a2dc03ba-99e5-46c2-9d92-3bc3d6fc88c9"
}

variable "backend_developer_object_id" {
  description = "Guest object ID: merchwadawid@fastmail.com"
  type        = string
  default     = "ba1fdf41-0929-409c-b2c9-b9874a5cd1c6"
}

variable "frontend_developer_object_id" {
  description = "Guest object ID: wrx87738@student.wroclaw.merito.pl"
  type        = string
  default     = "907f9adb-272f-4762-8679-9c9ac0234366"
}
