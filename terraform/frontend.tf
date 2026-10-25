# Existing backend-owned shared hosting and registry; do not recreate them.
data "azurerm_service_plan" "shared" {
  name                = "ASP-rgdreamplannerdev-a12c"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
}

data "azurerm_container_registry" "shared" {
  name                = "acrdreamplannerdev77076"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
}

resource "azurerm_linux_web_app" "frontend" {
  name                = "app-dreamplanner-frontend-77076"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
  location            = data.azurerm_service_plan.shared.location
  service_plan_id     = data.azurerm_service_plan.shared.id
  https_only          = true

  identity {
    type = "SystemAssigned"
  }

  site_config {
    always_on                               = true
    ftps_state                              = "Disabled"
    minimum_tls_version                     = "1.2"
    container_registry_use_managed_identity = true
    application_stack {
      docker_image_name   = "dreamplanner-frontend:latest"
      docker_registry_url = "https://${data.azurerm_container_registry.shared.login_server}"
    }
  }

  app_settings = {
    WEBSITES_PORT                       = "80"
    WEBSITES_ENABLE_APP_SERVICE_STORAGE = "false"
  }

  # CI owns the deployed image tag; Terraform retains ownership of the registry.
  lifecycle {
    ignore_changes = [site_config[0].application_stack[0].docker_image_name]
  }
  tags = local.database_tags
}

resource "azurerm_user_assigned_identity" "frontend_ci" {
  name                = "id-dreamplanner-frontend-github"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
  location            = data.azurerm_resource_group.dreamplanner.location
  tags                = local.database_tags
}

resource "azurerm_federated_identity_credential" "frontend_main" {
  name                = "github-main"
  resource_group_name = data.azurerm_resource_group.dreamplanner.name
  parent_id           = azurerm_user_assigned_identity.frontend_ci.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = "https://token.actions.githubusercontent.com"
  subject             = "repo:KacperPolarczyk@95491287/DreamPlanner@1402865452:ref:refs/heads/main"
}

resource "azurerm_role_assignment" "frontend_developer_host" {
  scope                = azurerm_linux_web_app.frontend.id
  role_definition_name = "Contributor"
  principal_id         = var.frontend_developer_object_id
  principal_type       = "User"
}

resource "azurerm_role_assignment" "frontend_ci_host" {
  scope                = azurerm_linux_web_app.frontend.id
  role_definition_name = "Website Contributor"
  principal_id         = azurerm_user_assigned_identity.frontend_ci.principal_id
  principal_type       = "ServicePrincipal"
}

# The existing ACR uses RBAC Registry Permissions (LegacyRegistryPermissions).
# These grants apply to this registry only, not the resource group.
resource "azurerm_role_assignment" "frontend_push" {
  for_each = {
    developer = { id = var.frontend_developer_object_id, type = "User" }
    ci        = { id = azurerm_user_assigned_identity.frontend_ci.principal_id, type = "ServicePrincipal" }
  }
  scope                = data.azurerm_container_registry.shared.id
  role_definition_name = "AcrPush"
  principal_id         = each.value.id
  principal_type       = each.value.type
}

resource "azurerm_role_assignment" "frontend_registry_reader" {
  for_each = {
    developer = { id = var.frontend_developer_object_id, type = "User" }
    ci        = { id = azurerm_user_assigned_identity.frontend_ci.principal_id, type = "ServicePrincipal" }
  }
  scope                = data.azurerm_container_registry.shared.id
  role_definition_name = "Reader"
  principal_id         = each.value.id
  principal_type       = each.value.type
}

resource "azurerm_role_assignment" "frontend_pull" {
  scope                = data.azurerm_container_registry.shared.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_linux_web_app.frontend.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

output "frontend_url" {
  value = "https://${azurerm_linux_web_app.frontend.default_hostname}"
}

output "frontend_github_configuration" {
  value = {
    client_id       = azurerm_user_assigned_identity.frontend_ci.client_id
    tenant_id       = data.azurerm_client_config.current.tenant_id
    subscription_id = data.azurerm_client_config.current.subscription_id
    registry        = data.azurerm_container_registry.shared.name
    login_server    = data.azurerm_container_registry.shared.login_server
    webapp          = azurerm_linux_web_app.frontend.name
  }
}
