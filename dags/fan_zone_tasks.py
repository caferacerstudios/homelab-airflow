"""Team configuration and receipt storage for the daily news DAG."""
import base64
from datetime import date
import hashlib
import json
import os
from pathlib import Path

VARIABLE_NAME = "fan_zone_active_sites"
DEFAULT_SITES = Path("/opt/airflow/config/active-sites.json")
ARTIFACTS_ROOT = Path("/opt/airflow/artifacts")
MAX_REQUEST_BYTES = 24576
MAX_TOKEN_BYTES = 32768


def active_sites():
    """Read the Variable once; callers may use this during DAG parsing or a task."""
    from airflow.sdk import Variable
    from fan_zone_config import validate_sites
    value = Variable.get(VARIABLE_NAME, default=None, deserialize_json=True)
    if value is None:
        value = json.loads(DEFAULT_SITES.read_text())
    sites = validate_sites(value)
    return [sites[slug] for slug in sorted(sites) if sites[slug]["enabled"]]


def encode_request(run_id, site=None, publication_day=None):
    if not isinstance(run_id, str) or not run_id or len(run_id.encode()) > 512 or any(
            ord(char) < 32 or ord(char) == 127 for char in run_id):
        raise ValueError("Invalid Airflow run ID")
    if not isinstance(publication_day, str) or date.fromisoformat(publication_day).isoformat() != publication_day:
        raise ValueError("Invalid news publication day")
    request = {"runId": run_id, "publicationDay": publication_day}
    if site is not None:
        from fan_zone_config import validate_site
        if not isinstance(site, dict):
            raise ValueError("News site must be an object")
        request["site"] = validate_site(site.get("slug"), site)
    encoded = json.dumps(request, separators=(",", ":")).encode()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("News run request is too large")
    token = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
    if len(token) > MAX_TOKEN_BYTES:
        raise ValueError("News run token is too large")
    return token


def save_receipt(receipt, run_id, site):
    from fan_zone_config import validate_site
    site = validate_site(site.get("slug"), site)
    if receipt.get("team") != site["slug"] or receipt.get("runId") != run_id:
        raise ValueError("News receipt run or team mismatch")
    directory = ARTIFACTS_ROOT / site["slug"] / "news" / hashlib.sha256(run_id.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    temporary, target = directory / "receipt.json.tmp", directory / "receipt.json"
    with temporary.open("w") as out:
        json.dump(receipt, out, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, target)
    print(f"{site['city']} {site['name']} news receipt saved: {target}")
    return str(target)
