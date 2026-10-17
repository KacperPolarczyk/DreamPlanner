
# DreamPlanner — Azure Infrastructure

Terraform configuration for the DreamPlanner network infrastructure deployed to Microsoft Azure.

## Architecture

The infrastructure uses a three-tier network architecture in the Azure Poland Central region.

| Component | Name | Address space |
|---|---|---|
| Resource Group | rg-dreamplanner-dev | — |
| Virtual Network | dreamplanner-vnet | 10.13.0.0/16 |
| Frontend Subnet | snet-frontend | 10.13.1.0/24 |
| Backend Subnet | snet-backend | 10.13.2.0/24 |
| Database Subnet | snet-database | 10.13.3.0/24 |

The database subnet is reserved for a future Azure Private Endpoint for PostgreSQL Flexible Server. It is not delegated to the PostgreSQL service.

## Network Security

Three Network Security Groups are associated with the subnets:

- nsg-frontend — allows inbound HTTPS traffic on TCP 443.
- nsg-backend — allows inbound TCP 8000 from the frontend subnet and denies other inbound VNet traffic.
- nsg-database — allows inbound TCP 5432 from the backend subnet and denies other inbound VNet traffic.

Additional default Azure NSG rules apply.

These rules define the intended network access policy. Application-level connectivity has not yet been tested.

## Terraform Structure

| File | Purpose |
|---|---|
| main.tf | Azure provider and Virtual Network |
| variables.tf | Configurable infrastructure values |
| subnets.tf | Three Azure subnets |
| nsg.tf | Network Security Groups, rules and associations |
| outputs.tf | Infrastructure resource identifiers |
| backend.hcl | Remote state backend configuration |
| .terraform.lock.hcl | Locked provider dependencies |

## Remote State

Terraform state is stored in Azure Blob Storage.

- Resource Group: rg-dreamplanner-dev
- Storage Account: st19398dreamplanner7439
- Container: tfstate
- Blob key: dreamplanner-dev.tfstate

The backend uses Microsoft Entra ID authentication through Azure CLI.

The Terraform state must not be committed to Git.

## Deployment

Prerequisites:

- Azure CLI
- Terraform
- An authenticated Azure account
- Appropriate Azure RBAC permissions
- Storage Blob Data Contributor access to the state container

Run:

```bash
az login
export ARM_SUBSCRIPTION_ID=$(az account show --query id -o tsv)
export ARM_USE_AZUREAD=true
export ARM_USE_CLI=true

cd terraform

terraform init -backend-config=backend.hcl
terraform fmt -check
terraform validate
terraform plan
terraform apply
```

Review the Terraform plan before applying any changes.

## Verification

After deployment:

```bash
terraform plan
terraform state list
```

A successful deployment should result in no unexpected infrastructure changes.

## Future Work

- PostgreSQL Flexible Server deployment
- Azure Private Endpoint and Private DNS configuration
- Frontend and backend hosting
- CI/CD pipelines using GitHub Actions
- Monitoring and additional security hardening

## Ownership

Project: DreamPlanner

Role: Project Manager / DevOps

Infrastructure managed using Terraform.
