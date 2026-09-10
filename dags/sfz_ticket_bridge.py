"""Python client for the fixed-operation, same-host ticket collector bridge."""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path


class TicketCollectorHook:
    """Exchange small JSON requests; this client has no host execution privileges."""

    def __init__(self, root="/opt/airflow/ticket-bridge"):
        self.root = Path(root)

    def request(self, operation, *, slot=None, timeout=3900):
        identity = f"collect:{slot}" if operation == "collect" else f"health:{uuid.uuid4()}"
        request_id = hashlib.sha256(identity.encode()).hexdigest()
        request = {"version": 1, "operation": operation, "request_id": request_id}
        if slot is not None:
            request["slot"] = slot
        response_path = self.root / "responses" / f"{request_id}.json"
        request_path = self.root / "requests" / f"{request_id}.json"
        if not response_path.exists():
            # Hidden temporary files are not watched by systemd.path.
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
                if response.get("status") not in {"success", "skipped", "failed"}:
                    raise RuntimeError("Bridge returned an invalid receipt")
                return response
            time.sleep(3)
        raise TimeoutError(
            "No bridge receipt. The host collector may still be running. "
            "Inspect its service and bridge status before taking action."
        )
