#!/usr/bin/env python3
"""Dispatch the two commands allowed by the dedicated Airflow SSH key."""

from __future__ import annotations

import base64
import binascii
import os
import json
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

HERE = Path(__file__).resolve().parent
MAX_RUN_ID_BYTES = 512


def command_arguments(command: str) -> list[str]:
    """Return runner arguments without interpreting any input as shell code."""
    if command == "check":
        return ["--check"]
    match = re.fullmatch(r"(refresh|check) ([A-Za-z0-9_-]{1,32768}={0,2})", command)
    if match is None:
        raise ValueError("Only check or refresh with an encoded run ID is allowed")
    mode, token = match.groups()
    try:
        decoded = base64.b64decode(
            token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
        )
        run_id = decoded.decode("utf-8")
        if len(decoded) > 24576:
            raise ValueError("Request is too large")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Invalid encoded run ID") from exc
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii")
    if token not in (canonical, canonical.rstrip("=")):
        raise ValueError("Invalid encoded run ID")
    extra = []
    if run_id.startswith("{"):
        request = json.loads(run_id)
        expected = {"site"} if mode == "check" else {"runId", "site"}
        if not isinstance(request, dict) or set(request) != expected or not isinstance(request["site"], dict):
            raise ValueError("Unexpected NFL request fields")
        # Host authorization happens again in the runner before choosing paths.
        extra = ["--site-json=" + json.dumps(request["site"], separators=(",", ":"))]
        if mode == "check":
            return ["--check", *extra]
        run_id = request["runId"]
        if not isinstance(run_id, str):
            raise ValueError("Invalid run ID")
        decoded = run_id.encode("utf-8")
    elif mode != "refresh":
        raise ValueError("A site check requires an encoded site request")
    if (
        not run_id.strip()
        or len(decoded) > MAX_RUN_ID_BYTES
        or any(unicodedata.category(char) == "Cc" for char in run_id)
    ):
        raise ValueError("Invalid run ID")
    return ["--run-id=" + run_id, *extra]


def main() -> int:
    try:
        arguments = command_arguments(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return subprocess.run(
        [sys.executable, str(HERE / "refresh_nfl.py"), *arguments],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
