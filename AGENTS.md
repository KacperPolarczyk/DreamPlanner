
# DreamPlanner — Agent Instructions

## 1. Project Overview

DreamPlanner is a student project for planning employee and contractor
availability and managing flexible work schedules.

Technology stack:
- Frontend: React, Vite, TypeScript.
- Backend: Python, FastAPI, Pydantic.
- ORM: SQLAlchemy 2.x.
- Database migrations: Alembic.
- Database: PostgreSQL.
- Local development: Docker Compose.
- Target cloud platform: Microsoft Azure.

The application follows a modular, three-layer architecture:
presentation, application/business logic, and data access.

Treat existing project documentation and explicitly agreed team
decisions as the source of truth.

Do not invent business requirements that have not been specified.

## 2. General Working Rules

- Inspect the repository before making changes.
- Read the relevant documentation and existing implementation.
- Reuse existing conventions and dependencies where possible.
- Prefer small, focused, maintainable changes.
- Avoid unrelated refactoring.
- Do not duplicate existing functionality.
- Do not introduce abstractions without a clear purpose.
- Explain significant technical decisions and their trade-offs.
- Ask for clarification when requirements or business rules are ambiguous.
- Do not silently change agreed API contracts or architecture.
- Do not overwrite unrelated changes made by other team members.

## 3. Architecture

Keep the following responsibilities separate:

- API layer: HTTP endpoints, request handling and response codes.
- Schema layer: request and response validation using Pydantic.
- Service layer: business rules and application workflows.
- Data layer: SQLAlchemy models, database sessions and queries.
- Core/configuration layer: settings and cross-cutting configuration.

Use dependency injection provided by FastAPI where appropriate.

Endpoints should remain thin. Do not put substantial business logic
directly into route handlers.

Do not couple business logic unnecessarily to HTTP request objects.

Avoid circular imports and unnecessary coupling between modules.

Do not add repositories, interfaces or other architectural layers
unless they solve a concrete problem.

## 4. Python and FastAPI

- Use the project's configured Python version.
- Follow PEP 8 and existing naming conventions.
- Add type annotations to public functions and important interfaces.
- Prefer explicit types and clear function signatures.
- Use Pydantic schemas for API input and output.
- Use SQLAlchemy 2.x conventions for ORM models and queries.
- Handle errors explicitly and return appropriate HTTP status codes.
- Never expose stack traces, credentials or internal database errors
  in API responses.
- Use synchronous or asynchronous database access consistently.
- Do not mix sync and async database patterns without a clear reason.

Do not add dependencies without explaining their purpose and obtaining
approval when they affect the project configuration.

## 5. Database and Migrations

- Use PostgreSQL as the relational database.
- Use SQLAlchemy for database access.
- Use Alembic for schema migrations.
- Do not rely on automatic table creation in production.
- Do not modify an existing migration that may already have been applied.
  Create a new migration instead.
- Review generated migrations before considering them complete.
- Keep ORM models and database migrations consistent.
- Define appropriate primary keys, foreign keys, constraints and indexes.
- Use transactions for operations that require atomicity.
- Avoid N+1 query problems where relevant.
- Never drop, reset or recreate a database without explicit approval.
- Never run destructive operations against a production database.

Local development should use Docker Compose.

Application configuration must come from environment variables
or the project's configuration system.

Never hardcode database credentials in source code.

## 6. API Design

- Expose REST endpoints under /api/v1.
- Use appropriate HTTP methods and status codes.
- Keep request and response schemas explicit.
- Use consistent naming and error response conventions.
- Validate incoming data at the API boundary.
- Do not expose internal ORM models as the public API contract.
- Preserve existing API contracts unless a change is explicitly approved.
- Document meaningful endpoint behavior and validation requirements.
- Use FastAPI's OpenAPI documentation capabilities.

Before implementing a new endpoint, check whether equivalent
functionality already exists.

## 7. Security

- Never commit secrets, passwords, tokens or private keys.
- Keep .env files out of version control.
- Maintain a safe .env.example without real credentials.
- Validate and authorize access to protected resources.
- Do not assume that an authenticated user may access every resource.
- Avoid insecure direct object references by checking resource ownership
  or permissions where required.
- Never disable security checks merely to make a test pass.
- Do not run deployment commands or modify cloud resources without
  explicit approval.
- Do not use production credentials or production data for local tests.

Authentication, authorization and role definitions must follow
the requirements agreed by the team.

## 8. Testing and Code Quality

- Write automated tests for new and modified behavior.
- Use pytest for backend tests.
- Use FastAPI's supported testing tools and HTTPX where appropriate.
- Test successful cases, validation failures and relevant edge cases.
- Test database-dependent behavior against a test database.
- Avoid tests that depend on production services or real credentials.
- Keep tests deterministic and independent of execution order.
- Do not weaken or remove tests just to obtain a passing test suite.

Use the project's configured formatter, linter and type checker.
Do not claim that a test or quality check passed unless it was executed
and completed successfully.

If a check cannot be run, explain why and identify what remains unverified.

## 9. Configuration and Azure

- Keep environment-specific configuration outside application code.
- Local development must work with a local PostgreSQL instance.
- Do not introduce Azure-specific dependencies unless required.
- Keep the application portable between local development and Azure.
- Never commit Azure credentials, connection strings or secrets.
- Do not deploy, provision or modify Azure resources without approval.

Cloud-specific configuration and deployment decisions must follow
the team's agreed infrastructure design.

## 10. Agent Workflow

For non-trivial tasks:

1. Inspect the relevant files and documentation.
2. Summarize the existing behavior and requirements.
3. Identify ambiguities, risks and affected components.
4. Present a concise implementation plan.
5. Implement the smallest complete solution.
6. Add or update tests.
7. Run relevant tests and quality checks.
8. Review the final diff for regressions and unrelated changes.
9. Report modified files, technical decisions and verification results.

For small, unambiguous changes, proceed directly when safe.

Do not claim completion if required work remains unfinished.

Do not commit, push, merge, deploy or modify production resources
unless explicitly requested and authorized.

## 11. Definition of Done

A task is complete when:

- The implementation meets the agreed requirements.
- Relevant automated tests have been added or updated.
- Applicable tests and quality checks have been executed.
- Database migrations have been reviewed when applicable.
- Documentation has been updated when necessary.
- The diff contains no unrelated changes.
- Remaining limitations and unverified behavior are reported.
