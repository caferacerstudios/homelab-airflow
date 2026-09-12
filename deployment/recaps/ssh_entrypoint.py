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
    """Accept site requests and the original Seattle run-ID token; never shell code."""
    if command == "check":
        return ["--check"]
    match = re.fullmatch(r"(refresh|check) ([A-Za-z0-9_-]{1,32768}={0,2})", command)
    if match is None:
        raise ValueError("Only check or refresh with an encoded request is allowed")
    mode, token = match.groups()
    try:
        decoded = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        canonical = base64.urlsafe_b64encode(decoded).decode("ascii")
        if len(decoded) > 24576 or token not in (canonical, canonical.rstrip("=")):
            raise ValueError("Invalid encoding")
        text = decoded.decode("utf-8")
        extra = []
        if text.startswith("{"):
            request = json.loads(text)
            expected = {"site"} if mode == "check" else {"runId", "site"}
            if not isinstance(request, dict) or set(request) != expected or not isinstance(request["site"], dict):
                raise ValueError("Unexpected request fields")
            from refresh_recaps import site_settings
            site = site_settings(request["site"], authorize=True)
            extra = ["--site-json=" + json.dumps(site, separators=(",", ":"))]
            if mode == "check":
                return ["--check", *extra]
            run_id = request["runId"]
        else:
            if mode != "refresh":
                raise ValueError("A check requires a site object")
            run_id = text
        if (not isinstance(run_id, str) or not run_id.strip() or len(run_id.encode()) > MAX_RUN_ID_BYTES
                or any(unicodedata.category(char) == "Cc" for char in run_id)):
            raise ValueError("Invalid run ID")
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("Invalid encoded recap request") from exc
    return ["--run-id=" + run_id, *extra]


def main() -> int:
    try:
        arguments = command_arguments(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return subprocess.run(
        [sys.executable, str(HERE / "refresh_recaps.py"), *arguments],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
