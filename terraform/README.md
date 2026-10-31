
# DreamPlanner — Azure Infrastructure

Terraform configuration for the DreamPlanner network, PostgreSQL and database access in Microsoft Azure.

## Architecture

The infrastructure uses a three-tier network architecture in the Azure Poland Central region.

| Component | Name | Address space |
|---|---|---|
| Resource Group | rg-dreamplanner-dev | — |
| Virtual Network | dreamplanner-vnet | 10.13.0.0/16 |
| Frontend Subnet | snet-frontend | 10.13.1.0/24 |
| Backend Subnet | snet-backend | 10.13.2.0/24 |
| Database Subnet | snet-database | 10.13.3.0/24 |

The database subnet hosts the PostgreSQL Private Endpoint. It is not delegated to the PostgreSQL service.

## Network Security

Three Network Security Groups are associated with the subnets:

- nsg-frontend — allows inbound HTTPS traffic on TCP 443.
- nsg-backend — allows inbound TCP 8000 from the frontend subnet and denies other inbound VNet traffic.
- nsg-database — allows inbound TCP 5432 from the backend subnet and denies other inbound VNet traffic.

Additional default Azure NSG rules apply.

The PostgreSQL endpoint accepts private connections from the backend subnet. A backend host must be connected to this VNet to use the private route; deploying an application somewhere in Azure alone is insufficient.

## Terraform Structure

| File | Purpose |
|---|---|
| main.tf | Azure provider and Virtual Network |
| variables.tf | Configurable infrastructure values |
| subnets.tf | Three Azure subnets |
| nsg.tf | Network Security Groups, rules and associations |
| outputs.tf | Infrastructure resource identifiers |
| database.tf | PostgreSQL, Private Endpoint, DNS, Key Vault and backend identity |
| team.tf | Team visibility, database secret access and identity assignment |
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

### Backend CI and migrations

`backend-access.tf` records the backend deployment and runtime role assignments.
`backend-migrations.tf` grants the GitHub OIDC identity read access to only
`database-url-migration` and a custom firewall-rule role on the PostgreSQL server.
It cannot change server settings, grant IAM roles or read the administrator secret
through these new migration grants. Firewall permissions cover all rules on that
server; the workflow only creates and deletes its own per-run rule.

These IAM resources were provisioned through Azure CLI. Import the existing role
assignments and custom role into the existing Terraform state before applying;
otherwise Terraform may attempt duplicate assignments. Review the complete plan:
the older database configuration still declares the deleted user-assigned backend
identity. Do not apply that stale declaration as part of enabling migrations.

The GitHub-hosted runner temporarily uses the already-enabled public PostgreSQL
endpoint with a single IPv4 firewall rule and TLS hostname verification. It does
not change the application's private database route. See the backend README for
execution order, cleanup and migration compatibility requirements.

Prerequisites:

- Azure CLI
- Terraform
- An authenticated Azure account
- Appropriate Azure RBAC permissions
- Storage Blob Data Contributor access to the state container
- Key Vault Secrets Officer access for the identity running Terraform after the vault exists
- Three distinct SQL passwords supplied as the sensitive `database_passwords` variable

The DBA's state-container access is a bootstrap role assignment outside this Terraform state.
It is scoped to the `tfstate` container, not the entire subscription. Azure `Owner` alone
does not grant blob data access.

Supply `database_passwords` with keys `admin`, `migration`, and `app` through
`TF_VAR_database_passwords` or an ignored `database.auto.tfvars`. Use three separately
generated passwords, each at least 24 characters long. Never commit this file.
Terraform state and saved plans contain secret values; only trusted infrastructure
administrators should have access to the state container. Connection strings are
stored in Key Vault and are not emitted as Terraform outputs.

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

The database is PostgreSQL 16 on `B_Standard_B1ms` with 32 GiB storage, seven-day
local backup retention and no HA or geo-redundant backup. This is a development
configuration. Compute, storage, Private Endpoint and Key Vault may consume Azure
credits; a student subscription does not by itself guarantee zero cost.

The only public database firewall rule admits `dba_public_ipv4`. Do not add the
Azure-wide `0.0.0.0` exception. Backend traffic uses the Private Endpoint and the
`privatelink.postgres.database.azure.com` DNS zone linked to the project VNet.
Always use the server FQDN, not the endpoint IP, so TLS hostname verification works.

The server and database have `prevent_destroy` protection. Review intentional
replacement or removal explicitly rather than bypassing the lifecycle guard.

## SQL initialization

Terraform creates the server and database, while `database/bootstrap.py` creates
SQL logins and grants. Key Vault secrets alone do not create SQL users.
Run from the repository root and the allowed DBA IP, with Azure CLI authenticated as the DBA:

```bash
python3 -m venv .venv
.venv/bin/pip install -r database/requirements.txt
.venv/bin/python database/bootstrap.py --vault-name kv-dreamplanner-77076
.venv/bin/python database/check_connection.py --vault-name kv-dreamplanner-77076
```

Both scripts read secrets into memory and do not print passwords. The check creates
a uniquely named disposable table as the migration login, verifies runtime CRUD,
checks TLS and denies runtime DDL, then removes the table. Run the same check from
the backend network to verify the private route; a successful DBA public connection
does not prove backend connectivity.

When rotating migration or application passwords, update the sensitive Terraform
input and Key Vault through Terraform, then rerun the bootstrap during a maintenance
window to synchronize the SQL logins. Terraform alone only rotates the administrator
login on the PostgreSQL server.

Application tables are deployed by the backend's Alembic migrations using
`dreamplanner_migrator`; `schema.sql` remains the reference model. Do not apply
both the reference SQL and an initial Alembic migration to the same database.

## Team and backend handoff

| Principal | Access |
| --- | --- |
| DBA | Existing Azure ownership; Key Vault Secrets Officer on the project vault; SQL administrator |
| Backend developer | Contributor, Reader and Container Registry Contributor and Data Access Configuration Administrator on the project resource group; AcrPush, Repository Writer and Catalog Lister for registries in the group; read only `database-url-app` and `database-url-migration`; Managed Identity Operator on the backend identity |
| Frontend developer | Reader on the project resource group; Contributor on the frontend web app; Reader and AcrPush on ACR; no database secrets or SQL login |
| `id-dreamplanner-backend` | Read only `database-url-app`; AcrPull, Repository Reader and Catalog Lister for registries in the group |

Guest users must accept their tenant invitation. Invitations are created once
outside Terraform; the guest object IDs are inputs to the role assignments.
The project tenant is `d6e86b7e-9310-46e8-9e3c-b44f06df413f`.
The backend developer can provision hosting, configure networking and deploy the
application using Contributor at resource-group scope. This also permits changing
other infrastructure in the group, including PostgreSQL; it does not grant IAM
role assignment administration. Contributor covers ACR Tasks management.
Image roles support both RBAC-only and ABAC-enabled registries, including registries
created later in this group. Attach `id-dreamplanner-backend` to the host and select
it for registry authentication; its pull roles and runtime database secret access
are already assigned. New identities require separate access assignments by an owner.
Frontend hosting and deployment roles are managed in `frontend.tf`; see
[frontend/AZURE.md](../frontend/AZURE.md) for the OIDC workflow and handoff.

The backend developer can retrieve the two named secrets after accepting the
invitation and signing in to the project tenant. Secret-scoped permissions do not
allow listing all secrets in the vault; address each secret by its exact name:

```bash
az login --tenant d6e86b7e-9310-46e8-9e3c-b44f06df413f
export DATABASE_URL="$(az keyvault secret show --vault-name kv-dreamplanner-77076 --name database-url-app --query value -o tsv)"
export MIGRATION_DATABASE_URL="$(az keyvault secret show --vault-name kv-dreamplanner-77076 --name database-url-migration --query value -o tsv)"
```

These commands put values into the process environment without printing them.
Retrieving a secret does not open a network route to PostgreSQL: use the database
connection from the backend network. The application host should obtain only the
runtime secret through its managed identity, not a developer's interactive login.

The backend host must:

1. Use the backend subnet (or supported VNet integration) and resolve the PostgreSQL FQDN through the linked private DNS zone.
2. Attach `backend_identity_id` and use its client ID when selecting a user-assigned managed identity.
3. Resolve the `app` URI from `database_secret_uris` into the server-side `DATABASE_URL` setting.
4. Use the separate migration secret only for Alembic, never for normal request handling.
5. Install the Linux `ca-certificates` package. Connection strings use `sslmode=verify-full` and the explicit CA bundle `/etc/ssl/certs/ca-certificates.crt`, configurable through `database_ssl_root_cert`.

The explicit CA path is required because a bundled libpq/OpenSSL client can use
a different default certificate directory from the host OS. On a different OS,
adjust the trusted CA path without disabling certificate or hostname verification.

No backend host is created by this database configuration. App Service requires
outbound VNet integration; a private endpoint on the web app is not a substitute.
DevOps owns the hosting-specific subnet delegation and deployment permissions.

## Verification

After deployment:

```bash
terraform plan
terraform state list
```

A successful deployment should result in no unexpected infrastructure changes.

## Future Work

- Frontend and backend hosting
- Backend VNet integration and runtime Key Vault configuration
- Alembic migrations and end-to-end API/database verification
- CI/CD pipelines using GitHub Actions
- Monitoring and additional security hardening

## Ownership

Project: DreamPlanner

Role: Project Manager / DevOps

Infrastructure managed using Terraform.

## Frontend hosting

`frontend.tf` reuses the existing Linux B1 service plan and Basic ACR through data
sources. It provisions a system-assigned identity for image pulls and a separate
GitHub OIDC identity restricted to `main`. The frontend developer has Contributor
on the frontend app only. The workflow owns the deployed image tag.

The backend subnet delegation in `subnets.tf` matches the existing App Service
VNet integration; removing it would disrupt the backend.
