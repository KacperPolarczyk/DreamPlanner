# The backend host and registry already exist outside this Terraform state.
# Look them up; this configuration does not recreate or modify either resource.
variable "backend_webapp_name" {
  description = "Exact name of the existing backend Linux App Service"
  type        = string
}

variable "backend_github_oidc_main_subject" {
  description = "Verified default OIDC sub for the protected backend-production Environment: repo:OWNER/REPO:environment:backend-production"
  type        = string

  validation {
    condition     = can(regex("^repo:[^/]+/[^:]+:environment:backend-production$", var.backend_github_oidc_main_subject))
    error_message = "Use the verified default GitHub Environment subject for backend-production."
  }
}

variable "backend_acr_permissions_mode" {
  description = "Current ACR permission mode: rbac or abac; verify in Azure before applying"
  type        = string

  validation {
    condition     = contains(["rbac", "abac"], var.backend_acr_permissions_mode)
    error_message = "Set backend_acr_permissions_mode to rbac or abac."
  }
}

variable "backend_acr_abac_condition" {
  description = "In ABAC mode, paste the reviewed Azure-generated condition restricting every Repository Writer action to dreamplanner-backend; null for RBAC mode"
  type        = string
  default     = null
}

data "azurerm_linux_web_app" "backend_existing" {
  name                = var.backend_webapp_name
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
}

resource "azurerm_user_assigned_identity" "backend_ci" {
  name                = "id-dreamplanner-backend-github"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
  location            = data.azurerm_resource_group.dreamplanner.location
  tags                = local.database_tags
}

resource "azurerm_federated_identity_credential" "backend_production" {
  name                = "github-backend-production"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
  parent_id           = azurerm_user_assigned_identity.backend_ci.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = "https://token.actions.githubusercontent.com"
  subject             = var.backend_github_oidc_main_subject
}

resource "azurerm_role_assignment" "backend_ci_host" {
  scope                = data.azurerm_linux_web_app.backend_existing.id
  role_definition_name = "Website Contributor"
  principal_id         = azurerm_user_assigned_identity.backend_ci.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "backend_ci_acr_reader" {
  scope                = data.azurerm_container_registry.shared.id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.backend_ci.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "backend_ci_acr_data" {
  scope                = data.azurerm_container_registry.shared.id
  role_definition_name = var.backend_acr_permissions_mode == "rbac" ? "AcrPush" : "Container Registry Repository Writer"
  principal_id         = azurerm_user_assigned_identity.backend_ci.principal_id
  principal_type       = "ServicePrincipal"
  condition            = var.backend_acr_permissions_mode == "abac" ? var.backend_acr_abac_condition : null
  condition_version    = var.backend_acr_permissions_mode == "abac" ? "2.0" : null

  lifecycle {
    precondition {
      condition     = var.backend_acr_permissions_mode == "rbac" || can(regex("dreamplanner-backend", var.backend_acr_abac_condition))
      error_message = "ABAC mode requires a reviewed repository condition for dreamplanner-backend."
    }
  }
}

output "backend_ci_client_id" {
  value = azurerm_user_assigned_identity.backend_ci.client_id
}
