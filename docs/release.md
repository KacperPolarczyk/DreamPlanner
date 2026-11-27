# Release procedure

Before freezing a candidate, merge the intended fixes and infrastructure changes.
Require successful frontend build, backend tests, secret and vulnerability scans,
and PostgreSQL integration tests on the final commit. Verify the deployed E2E
result corresponds to the candidate code. A skipped E2E scenario is not a pass.

Review open issues and the project board. Close completed tasks with evidence,
keep unresolved defects visible and explicitly defer optional upgrades.
Exercise invalid input, duplicate requests, offline/slow networking and unavailable
API/database behavior. Simulate database failures locally, not by stopping Azure.

## Database checks

Run the PostgreSQL suite described in [database CI](../database/ci.md).
From `backend`, run `uv run --no-sync pytest tests/test_health.py -q`.
This includes a real refused local PostgreSQL connection: readiness must return
a redacted 503 while liveness remains 200. It does not verify browser behavior
or recovery after a production outage.

Confirm private connectivity, migration revision and least-privilege runtime
credentials through the existing deployment procedure. Do not include secrets
in release notes or screenshots.

## Freeze and publish

1. Create `release/v1.0.0` from the verified commit. Protect against deletion and
   force pushes, require PRs and the same status checks as main.
2. Ensure CI workflows run for PRs targeting the release branch before requiring
   their checks. Restrict changes to fixes. Review deployment triggers and OIDC
   separately: the release branch does not inherit main's deployment permissions.
3. Revalidate fixes and confirm the deployed revision and E2E result.
4. Create an annotated tag on the final candidate commit:

   ```bash
   git tag -a v1.0.0-rc1 <verified-commit> -m "DreamPlanner 1.0.0 release candidate 1"
   git push origin v1.0.0-rc1
   ```

5. Publish a GitHub prerelease with implemented changes, test evidence, known
   limitations and application URLs. Do not move a published tag; create another
   candidate if a correction is necessary.

Frontend: https://app-dreamplanner-frontend-77076.azurewebsites.net

API docs: https://app-dreamplanner-backend-77076-ckceebf7f3gkg3fz.polandcentral-01.azurewebsites.net/docs
