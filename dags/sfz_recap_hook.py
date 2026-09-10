"""Invoke the fixed recap host command and validate its small manifest."""
from __future__ import annotations

import base64
from datetime import datetime
import json
import re

RECEIPT_PREFIX = "SFZ_RECAP_RECEIPT="


def validate_receipt(receipt: dict, run_id: str) -> dict:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise ValueError("Recap refresh returned an unsupported manifest")
    if receipt.get("runId") != run_id:
        raise ValueError("Recap receipt belongs to a different run")
    files = receipt.get("files")
    if not isinstance(files, dict) or set(files) != {"gameRecaps.json"}:
        raise ValueError("Recap receipt must contain gameRecaps.json")
    if not isinstance(files["gameRecaps.json"], str) or not re.fullmatch(r"[0-9a-f]{64}", files["gameRecaps.json"]):
        raise ValueError("Recap receipt has an invalid checksum")
    for field in ("generatedCount", "requestCount", "openaiRequestCount"):
        if type(receipt.get(field)) is not int or receipt[field] < 0:
            raise ValueError(f"Recap receipt has invalid {field}")
    ids = receipt.get("generatedGameIds")
    if not isinstance(ids, list) or not all(isinstance(value, str) and value for value in ids):
        raise ValueError("Recap receipt has invalid generated game IDs")
    if len(ids) != receipt["generatedCount"] or len(ids) != len(set(ids)):
        raise ValueError("Recap receipt generated count does not match its game IDs")
    if type(receipt.get("season")) is not int or not 2002 <= receipt["season"] <= 2200:
        raise ValueError("Recap receipt has an invalid season")
    for field in ("updatedAt", "nflSourceUpdatedAt"):
        parsed = datetime.fromisoformat(str(receipt.get(field, "")).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError(f"Recap receipt {field} must include a timezone")
    if not isinstance(receipt.get("nflSourceRunId"), str) or not receipt["nflSourceRunId"]:
        raise ValueError("Recap receipt is missing its NFL source run")
    if not isinstance(receipt.get("model"), str) or not receipt["model"]:
        raise ValueError("Recap receipt is missing its model")
    return receipt


def parse_receipt(stdout: bytes, run_id: str) -> dict:
    lines = [line[len(RECEIPT_PREFIX):] for line in stdout.decode("utf-8", "replace").splitlines()
             if line.startswith(RECEIPT_PREFIX)]
    if len(lines) != 1:
        raise ValueError("Recap refresh did not return exactly one completed manifest")
    return validate_receipt(json.loads(lines[0]), run_id)


class RecapRefreshHook:
    def __init__(self, ssh_conn_id: str = "sfz_recap_host"):
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
            raise RuntimeError(f"Recap host refresh failed with exit status {status}; inspect the task log")
        return parse_receipt(stdout, run_id)
