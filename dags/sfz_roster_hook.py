"""Request a team roster publication through the dedicated restricted SSH key."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

RECEIPT_PREFIX = "SFZ_ROSTER_RECEIPT="
FILES = {"roster.json", "injuries.json", "transactions.json"}
ARTIFACTS = Path("/opt/airflow/artifacts")


def encode_request(run_id: str, site: dict) -> str:
    from fan_zone_config import validate_site
    if (not isinstance(run_id, str) or not run_id.strip() or len(run_id.encode()) > 512
            or any(ord(c) < 32 or ord(c) == 127 for c in run_id)):
        raise ValueError("Invalid Airflow run ID")
    if not isinstance(site, dict):
        raise ValueError("Roster site must be an object")
    selected = validate_site(site.get("slug"), site)
    payload = json.dumps({"runId": run_id, "site": selected}, separators=(",", ":")).encode()
    if len(payload) > 24576:
        raise ValueError("Roster request is too large")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def validate_receipt(receipt: dict, run_id: str, site: dict) -> dict:
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or receipt.get("pipeline") != "roster"):
        raise ValueError("Roster refresh returned an unsupported manifest")
    if receipt.get("team") != site["slug"] or receipt.get("runId") != run_id:
        raise ValueError("Roster receipt belongs to another team or run")
    files = receipt.get("files")
    if not isinstance(files, dict) or set(files) != FILES:
        raise ValueError("Roster receipt must contain all three team files")
    if not all(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) for digest in files.values()):
        raise ValueError("Roster receipt contains invalid file checksums")
    requests = receipt.get("requestCount")
    if type(requests) is not int or not 0 <= requests <= 30:
        raise ValueError("Roster receipt has an invalid source request count")
    try:
        updated = datetime.fromisoformat(receipt["updatedAt"].replace("Z", "+00:00"))
    except (TypeError, KeyError, ValueError, AttributeError) as exc:
        raise ValueError("Roster receipt has an invalid timestamp") from exc
    if updated.tzinfo is None or updated > datetime.now(timezone.utc):
        raise ValueError("Roster receipt timestamp must be timezone-qualified and not future")
    return receipt


def parse_receipt(stdout: bytes, run_id: str, site: dict) -> dict:
    records = [line[len(RECEIPT_PREFIX):] for line in stdout.decode("utf-8", "replace").splitlines()
               if line.startswith(RECEIPT_PREFIX)]
    if len(records) != 1:
        raise ValueError("Roster runner must return exactly one completed manifest")
    return validate_receipt(json.loads(records[0]), run_id, site)


def save_receipt(receipt: dict, run_id: str, site: dict, root: Path = ARTIFACTS) -> str:
    from fan_zone_config import validate_site
    site = validate_site(site.get("slug"), site)
    validate_receipt(receipt, run_id, site)
    directory = Path(root) / site["slug"] / "roster" / hashlib.sha256(run_id.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "receipt.json"
    temporary = directory / "receipt.json.tmp"
    with temporary.open("w") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    print(f"{site['city']} {site['name']} roster receipt saved: {target}")
    return str(target)


class RosterRefreshHook:
    def __init__(self, ssh_conn_id: str = "sfz_roster_host"):
        self.ssh_conn_id = ssh_conn_id

    def refresh(self, run_id: str, site: dict) -> dict:
        from airflow.providers.ssh.hooks.ssh import SSHHook
        token = encode_request(run_id, site)
        hook = SSHHook(ssh_conn_id=self.ssh_conn_id, conn_timeout=15, cmd_timeout=540,
                       keepalive_interval=30, conn_retry_attempts=1)
        with hook.get_conn() as client:
            status, stdout, _stderr = hook.exec_ssh_client_command(
                client, "refresh " + token, get_pty=False, environment=None, timeout=540)
        if status != 0:
            raise RuntimeError(f"Roster host refresh failed with exit {status}. Inspect the task/host log and publication receipt before retrying.")
        return parse_receipt(stdout, run_id, site)
