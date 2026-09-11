#!/usr/bin/env python3
"""Only accept a source-free check or a validated encoded news-run request."""
import base64
import binascii
from datetime import date
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata


def command_arguments(command):
    if command == 'check':
        return ['--check']
    match = re.fullmatch(r'refresh ([A-Za-z0-9_-]{1,1600})', command)
    if not match:
        raise ValueError('Only check or refresh with an encoded news request is allowed')
    token = match[1]
    try:
        raw = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip('=') != token:
            raise ValueError('Noncanonical encoding')
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {'runId', 'publicationDay'}:
            raise ValueError('Unexpected request fields')
        run_id, day = request['runId'], request['publicationDay']
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id.encode()) > 512 or any(unicodedata.category(c) == 'Cc' for c in run_id):
            raise ValueError('Invalid run ID')
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError('Invalid publication day')
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
        raise ValueError('Invalid encoded news request') from exc
    return ['--run-id=' + run_id, '--publication-day=' + day]


def main():
    try:
        args = command_arguments(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return subprocess.run([sys.executable, str(Path(__file__).with_name('refresh_news.py')), *args], check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
