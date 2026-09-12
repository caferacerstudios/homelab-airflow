"""Invoke the fixed recap host command and validate its small manifest."""
from __future__ import annotations

import base64
from datetime import datetime
import json
import re
import hashlib
import os
from pathlib import Path

ARTIFACTS_ROOT = Path("/opt/airflow/artifacts")

RECEIPT_PREFIX = "SFZ_RECAP_RECEIPT="


def validate_receipt(receipt: dict, run_id: str, site=None) -> dict:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise ValueError("Recap refresh returned an unsupported manifest")
    if receipt.get("runId") != run_id:
        raise ValueError("Recap receipt belongs to a different run")
    if site is not None and receipt.get("team") != site["slug"]:
        raise ValueError("Recap receipt belongs to a different team")
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


def parse_receipt(stdout: bytes, run_id: str, site=None) -> dict:
    lines = [line[len(RECEIPT_PREFIX):] for line in stdout.decode("utf-8", "replace").splitlines()
             if line.startswith(RECEIPT_PREFIX)]
    if len(lines) != 1:
        raise ValueError("Recap refresh did not return exactly one completed manifest")
    return validate_receipt(json.loads(lines[0]), run_id, site)


def encode_request(run_id, site=None):
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id.encode()) > 512 or any(
            ord(char) < 32 or ord(char) == 127 for char in run_id):
        raise ValueError("Invalid Airflow run ID")
    if site is None:
        raw = run_id.encode("utf-8")  # Existing Seattle command remains supported.
    else:
        from fan_zone_config import validate_site
        selected = validate_site(site.get("slug"), site)
        raw = json.dumps({"runId": run_id, "site": selected}, separators=(",", ":")).encode()
    if len(raw) > 24576:
        raise ValueError("Recap run request is too large")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def save_receipt(receipt, run_id, site):
    validate_receipt(receipt, run_id, site)
    from fan_zone_config import validate_site
    selected = validate_site(site.get("slug"), site)
    directory = ARTIFACTS_ROOT / selected["slug"] / "recaps" / hashlib.sha256(run_id.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target, temporary = directory / "receipt.json", directory / "receipt.json.tmp"
    with temporary.open("w") as out:
        json.dump(receipt, out, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, target)
    print(f"{selected['city']} {selected['name']} recap receipt saved: {target}; generated={receipt['generatedCount']}")
    return str(target)


class RecapRefreshHook:
    def __init__(self, ssh_conn_id: str = "sfz_recap_host"):
        self.ssh_conn_id = ssh_conn_id

    def refresh(self, run_id: str, site=None) -> dict:
        from airflow.providers.ssh.hooks.ssh import SSHHook
        token = encode_request(run_id, site)
        hook = SSHHook(ssh_conn_id=self.ssh_conn_id, conn_timeout=15, cmd_timeout=3900,
                       keepalive_interval=30, conn_retry_attempts=1)
        with hook.get_conn() as client:
            status, stdout, _stderr = hook.exec_ssh_client_command(
                client, "refresh " + token, get_pty=False, environment=None, timeout=3900
            )
        if status != 0:
            raise RuntimeError(f"Recap host refresh failed with exit status {status}; inspect the task log")
        return parse_receipt(stdout, run_id, site)
