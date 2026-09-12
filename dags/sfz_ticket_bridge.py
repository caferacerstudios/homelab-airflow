"""Small, team-specific JSON requests to the same-host EventSpy queue."""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from fan_zone_config import eventspy_request_id, eventspy_slot, validate_site


class TicketCollectorHook:
    """The Airflow worker has queue access; it cannot run host commands."""

    def __init__(self, root="/opt/airflow/ticket-bridge"):
        self.root = Path(root)

    def request(self, operation, *, slot=None, site=None, timeout=3900):
        if operation == "collect":
            site = validate_site(site.get("slug"), site) if isinstance(site, dict) else None
            if site is None or "eventspy" not in site or not site["enabled"] or not isinstance(slot, str):
                raise ValueError("Collection requires an enabled site, EventSpy settings, and scheduled slot")
            slot = eventspy_slot(slot)
            request_id = eventspy_request_id(slot, site)
        elif operation == "health" and slot is None and site is None:
            request_id = hashlib.sha256(f"health:{uuid.uuid4()}".encode()).hexdigest()
        else:
            raise ValueError("Unsupported bridge request")
        request = {"version": 2, "operation": operation, "request_id": request_id}
        if operation == "collect":
            request.update(slot=slot, site=site)
        response_path = self.root / "responses" / f"{request_id}.json"
        request_path = self.root / "requests" / f"{request_id}.json"
        if not response_path.exists():
            temporary = request_path.with_name(f".{request_id}.{uuid.uuid4().hex}.tmp")
            with temporary.open("x") as handle:
                json.dump(request, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, request_path)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if response_path.exists():
                response = json.loads(response_path.read_text())
                if (response.get("version") != 2 or response.get("request_id") != request_id
                        or response.get("status") not in {"success", "skipped", "failed"}
                        or (operation == "collect" and (response.get("team") != site["slug"]
                                                       or response.get("slot") != slot))):
                    raise RuntimeError("Bridge returned an invalid or mismatched receipt")
                return response
            time.sleep(3)
        raise TimeoutError("No bridge receipt. The host collector may still be running; inspect bridge status before retrying.")


def save_receipt(receipt, run_id, site, artifacts_root="/opt/airflow/artifacts"):
    site = validate_site(site.get("slug"), site)
    if receipt.get("team") != site["slug"] or receipt.get("version") != 2:
        raise ValueError("Ticket receipt does not match its team")
    folder = Path(artifacts_root) / site["slug"] / "eventspy" / hashlib.sha256(run_id.encode()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    target, temporary = folder / "receipt.json", folder / "receipt.tmp"
    with temporary.open("w") as out:
        json.dump({"run_id": run_id, **receipt}, out, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, target)
    print(f"Saved {site['city']} {site['name']} ticket receipt: {target}")
    return str(target)
