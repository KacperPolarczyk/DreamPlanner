"""Verify deployment gating and cleanup without calling Azure."""

import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "migration_runner", Path(__file__).parents[1] / "scripts/run_private_migration.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class MigrationRunnerTests(unittest.TestCase):
    def response(self, state):
        return io.BytesIO(json.dumps(state).encode())

    def test_success_requires_execution_and_image_match(self):
        sha = "a" * 40
        states = [
            io.BytesIO(b"8.8.8.8"),
            self.response(
                {
                    "status": "succeeded",
                    "exit_code": 0,
                    "run_id": "old",
                    "release_id": sha,
                }
            ),
            self.response(
                {
                    "status": "succeeded",
                    "exit_code": 0,
                    "run_id": "current",
                    "release_id": "b" * 40,
                }
            ),
            self.response(
                {
                    "status": "succeeded",
                    "exit_code": 0,
                    "run_id": "current",
                    "release_id": sha,
                }
            ),
        ]
        with (
            patch.object(runner, "az", return_value="status.example") as az,
            patch.object(
                runner.urllib.request, "urlopen", side_effect=states
            ) as urlopen,
            patch.object(runner.time, "sleep"),
            patch("builtins.print"),
        ):
            runner.run(sha, "current")
        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual(az.call_args_list[-2].args[:2], ("webapp", "stop"))
        self.assertIn("remove", az.call_args_list[-1].args)
        self.assertFalse(any("keyvault" in call.args for call in az.call_args_list))

    def test_failed_migration_stops_app_and_removes_allowlist(self):
        sha = "a" * 40
        with (
            patch.object(runner, "az", return_value="status.example") as az,
            patch.object(
                runner.urllib.request,
                "urlopen",
                side_effect=[
                    io.BytesIO(b"8.8.8.8"),
                    self.response(
                        {"status": "failed", "run_id": "current", "release_id": sha}
                    ),
                ],
            ),
            self.assertRaises(RuntimeError),
        ):
            runner.run(sha, "current")
        self.assertEqual(az.call_args_list[-2].args[:2], ("webapp", "stop"))
        self.assertIn("remove", az.call_args_list[-1].args)

    def test_mutable_image_tag_rejected_before_network(self):
        with (
            patch.object(runner.urllib.request, "urlopen") as urlopen,
            self.assertRaises(ValueError),
        ):
            runner.run("latest", "test")
        urlopen.assert_not_called()
