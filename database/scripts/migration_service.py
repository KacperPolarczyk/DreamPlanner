"""One migration per process; expose read-only status to the allowlisted runner."""

import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    "run_id": os.environ.get("MIGRATION_RUN_ID", ""),
    "release_id": os.environ.get("RELEASE_ID", ""),
    "expected_release_id": os.environ.get("MIGRATION_EXPECTED_RELEASE", ""),
    "status": "starting",
}


def migrate():
    try:
        if (
            not STATE["run_id"]
            or STATE["run_id"] == "unconfigured"
            or not re.fullmatch(r"[0-9a-f]{40}", STATE["release_id"])
            or STATE["release_id"] != STATE["expected_release_id"]
        ):
            raise ValueError("Missing execution identity")
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                "psql-dreamplanner-dev-77076.postgres.database.azure.com",
                5432,
                type=socket.SOCK_STREAM,
            )
        }
        private_subnet = ipaddress.ip_network("10.13.3.0/24")
        if not addresses or not all(
            ipaddress.ip_address(address) in private_subnet for address in addresses
        ):
            raise ValueError("Database DNS does not resolve to its private endpoint")
        STATE["database_addresses"] = sorted(addresses)
        STATE["status"] = "running"
        result = subprocess.run(
            ["python", "-m", "src.migrate"],
            check=False,
            timeout=600,
            # The migrator already redacts credentials. Do not expose output over HTTP.
        )
        STATE["exit_code"] = result.returncode
        STATE["status"] = "succeeded" if result.returncode == 0 else "failed"
    except Exception as error:  # noqa: BLE001 - expose only sanitized failure type
        STATE["error_type"] = type(error).__name__
        STATE["status"] = "failed"


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/status":
            self.send_error(404)
            return
        data = json.dumps(STATE.copy()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    threading.Thread(target=migrate, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8000), StatusHandler).serve_forever()
