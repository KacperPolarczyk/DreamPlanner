#!/usr/bin/env bash
# Existing App Services are shared with the application team's deployment config.
# Update only health and logging settings; never replace their app settings.
set -euo pipefail
resource_group="rg-dreamplanner-dev"
backend="app-dreamplanner-backend-77076"
frontend="app-dreamplanner-frontend-77076"
az webapp config set --resource-group "$resource_group" --name "$backend" \
  --generic-configurations '{"healthCheckPath":"/api/v1/health/ready"}' --output none
for app in "$backend" "$frontend"; do
  az webapp log config --resource-group "$resource_group" --name "$app" \
    --docker-container-logging filesystem --web-server-logging filesystem --output none
done
