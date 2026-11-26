"""Safety checks for the private migration service; no Azure or database access."""

import importlib.util
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "migration_service", Path(__file__).parents[1] / "scripts/migration_service.py"
)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class PrivateMigrationTests(unittest.TestCase):
    def setUp(self):
        service.STATE.clear()
        service.STATE.update(run_id="test-1", release_id="a" * 40, status="starting")

    def test_public_dns_never_runs_migrations(self):
        with (
            patch.object(
                service.socket,
                "getaddrinfo",
                return_value=[(None, None, None, None, ("8.8.8.8", 5432))],
            ),
            patch.object(service.subprocess, "run") as run,
        ):
            service.migrate()
        run.assert_not_called()
        self.assertEqual(service.STATE["status"], "failed")

    def test_private_dns_success(self):
        with (
            patch.object(
                service.socket,
                "getaddrinfo",
                return_value=[(None, None, None, None, ("10.13.3.4", 5432))],
            ),
            patch.object(
                service.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as run,
        ):
            service.migrate()
        run.assert_called_once()
        self.assertEqual(service.STATE["status"], "succeeded")
        self.assertEqual(service.STATE["database_addresses"], ["10.13.3.4"])

    def test_migration_failure_is_not_retried(self):
        with (
            patch.object(
                service.socket,
                "getaddrinfo",
                return_value=[(None, None, None, None, ("10.13.3.4", 5432))],
            ),
            patch.object(
                service.subprocess, "run", return_value=SimpleNamespace(returncode=1)
            ) as run,
        ):
            service.migrate()
        run.assert_called_once()
        self.assertEqual(service.STATE["status"], "failed")

    def test_timeout_does_not_disclose_command_or_credentials(self):
        with (
            patch.object(
                service.socket,
                "getaddrinfo",
                return_value=[(None, None, None, None, ("10.13.3.4", 5432))],
            ),
            patch.object(
                service.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("sensitive-command", 600),
            ),
        ):
            service.migrate()
        self.assertEqual(service.STATE["status"], "failed")
        self.assertNotIn("sensitive-command", str(service.STATE))

    def test_missing_run_identity_never_runs_migrations(self):
        service.STATE["run_id"] = ""
        with patch.object(service.subprocess, "run") as run:
            service.migrate()
        run.assert_not_called()
        self.assertEqual(service.STATE["status"], "failed")


if __name__ == "__main__":
    unittest.main()
