"""Run the fixed NFL refresh command on wkr and validate its small receipt."""
from __future__ import annotations

import base64
from datetime import datetime
import json
import re


RECEIPT_PREFIX = "SFZ_NFL_RECEIPT="
REQUIRED_FILES = {"seahawks.json", "players.json", "standings.json"}


def validate_receipt(receipt: dict, run_id: str) -> dict:
    """Only accept the successful manifest for the requested Airflow run."""
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise ValueError("NFL refresh returned an unsupported manifest")
    if receipt.get("runId") != run_id:
        raise ValueError("NFL refresh receipt belongs to a different run")
    files = receipt.get("files")
    if not isinstance(files, dict) or not REQUIRED_FILES <= files.keys():
        raise ValueError("NFL refresh receipt is missing required files")
    if set(files) - (REQUIRED_FILES | {"gameRecaps.json"}):
        raise ValueError("NFL refresh receipt has unexpected files")
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in files.values()):
        raise ValueError("NFL refresh receipt has invalid file digests")
    requests = receipt.get("requestCount")
    if isinstance(requests, bool) or not isinstance(requests, int) or requests < 1:
        raise ValueError("NFL refresh receipt has no recorded API requests")
    season = receipt.get("season")
    if isinstance(season, bool) or not isinstance(season, int) or not 2002 <= season <= 2200:
        raise ValueError("NFL refresh receipt has an invalid season")
    updated_at = datetime.fromisoformat(str(receipt.get("updatedAt", "")).replace("Z", "+00:00"))
    if updated_at.tzinfo is None:
        raise ValueError("NFL refresh timestamp must include a timezone")
    return receipt


def parse_receipt(stdout: bytes, run_id: str) -> dict:
    lines = [line[len(RECEIPT_PREFIX):] for line in stdout.decode("utf-8", "replace").splitlines()
             if line.startswith(RECEIPT_PREFIX)]
    if len(lines) != 1:
        raise ValueError("NFL refresh did not return exactly one completed manifest")
    return validate_receipt(json.loads(lines[0]), run_id)


class NflRefreshHook:
    """Use an Airflow SSH connection whose key allows only check/refresh."""

    def __init__(self, ssh_conn_id: str = "sfz_nfl_host"):
        self.ssh_conn_id = ssh_conn_id

    def refresh(self, run_id: str) -> dict:
        from airflow.providers.ssh.hooks.ssh import SSHHook

        encoded = run_id.encode("utf-8")
        if not encoded or len(encoded) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in run_id):
            raise ValueError("Invalid Airflow run ID")
        token = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
        hook = SSHHook(ssh_conn_id=self.ssh_conn_id, conn_timeout=15, cmd_timeout=3900,
                       keepalive_interval=30, conn_retry_attempts=1)
        with hook.get_conn() as client:
            status, stdout, _stderr = hook.exec_ssh_client_command(
                client, "refresh " + token, get_pty=False, environment=None, timeout=3900
            )
        if status != 0:
            raise RuntimeError(f"NFL host refresh failed with exit status {status}; inspect the task log")
        return parse_receipt(stdout, run_id)
