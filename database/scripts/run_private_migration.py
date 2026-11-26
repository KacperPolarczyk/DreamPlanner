"""Run the backend image in the private migration App Service, then stop it."""

import argparse
import base64
import ipaddress
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

GROUP = "rg-dreamplanner-dev"
APP = "app-dreamplanner-migrations-77076"
REGISTRY = "acrdreamplannerdev77076.azurecr.io"


def az(*args, output=False):
    result = subprocess.run(
        ["az", *args, "--only-show-errors", "--output", "json" if output else "none"],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return json.loads(result.stdout) if output else None


def run(sha, run_id):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected a full immutable commit SHA")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", run_id):
        raise ValueError("Invalid execution ID")
    with urllib.request.urlopen("https://api.ipify.org", timeout=20) as response:
        address = ipaddress.ip_address(response.read().decode().strip())
    if address.version != 4 or not address.is_global:
        raise ValueError("Expected a public IPv4 for the status endpoint allowlist")
    rule = "migration-runner"
    script = base64.b64encode(
        Path(__file__).with_name("migration_service.py").read_bytes()
    ).decode()
    hostname = az(
        "webapp",
        "show",
        "-g",
        GROUP,
        "-n",
        APP,
        "--query",
        "defaultHostName",
        output=True,
    )
    # Deploy jobs must share backend-azure-main concurrency. Only one run at a time.
    try:
        az("webapp", "stop", "-g", GROUP, "-n", APP)
        az(
            "webapp",
            "config",
            "access-restriction",
            "add",
            "-g",
            GROUP,
            "-n",
            APP,
            "--rule-name",
            rule,
            "--action",
            "Allow",
            "--ip-address",
            f"{address}/32",
            "--priority",
            "100",
        )
        az(
            "webapp",
            "config",
            "appsettings",
            "set",
            "-g",
            GROUP,
            "-n",
            APP,
            "--settings",
            f"MIGRATION_RUN_ID={run_id}",
            f"MIGRATION_EXPECTED_RELEASE={sha}",
            f"MIGRATION_JOB_SCRIPT={script}",
        )
        az(
            "webapp",
            "config",
            "container",
            "set",
            "-g",
            GROUP,
            "-n",
            APP,
            "--container-image-name",
            f"{REGISTRY}/dreamplanner-backend:{sha}",
            "--container-registry-url",
            f"https://{REGISTRY}",
        )
        az("webapp", "start", "-g", GROUP, "-n", APP)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"https://{hostname}/status", timeout=15
                ) as response:
                    status = json.load(response)
            except (urllib.error.URLError, TimeoutError, ValueError):
                time.sleep(10)
                continue
            if status.get("run_id") != run_id or status.get("release_id") != sha:
                time.sleep(10)
                continue
            if status.get("status") == "failed":
                raise RuntimeError(
                    "Private migration failed; inspect migration app logs"
                )
            if status.get("status") == "succeeded" and status.get("exit_code") == 0:
                print(json.dumps(status))
                return
            time.sleep(10)
        raise TimeoutError("Private migration did not finish within 15 minutes")
    finally:
        try:
            az("webapp", "stop", "-g", GROUP, "-n", APP)
        finally:
            az(
                "webapp",
                "config",
                "access-restriction",
                "remove",
                "-g",
                GROUP,
                "-n",
                APP,
                "--rule-name",
                rule,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        run(args.sha, args.run_id)
    except Exception as error:  # noqa: BLE001 - expose only sanitized failure type
        # Do not dump CLI requests, settings, credentials, or provider responses.
        print(f"Private migration failed: {type(error).__name__}")
        raise SystemExit(1)
