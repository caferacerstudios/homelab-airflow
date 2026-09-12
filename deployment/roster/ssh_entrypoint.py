#!/usr/bin/env python3
"""Only explicit, encoded site checks and roster refreshes are allowed."""
import base64
import binascii
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

HERE = Path(__file__).resolve().parent


def command_arguments(command: str) -> list[str]:
    match = re.fullmatch(r'(refresh|check) ([A-Za-z0-9_-]{1,32768}={0,2})', command)
    if not match:
        raise ValueError('Only check or refresh with an encoded site request is allowed')
    mode, token = match.groups()
    try:
        raw = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
        if len(raw) > 24576:
            raise ValueError('Request is too large')
        canonical = base64.urlsafe_b64encode(raw).decode()
        if token not in (canonical, canonical.rstrip('=')):
            raise ValueError('Noncanonical encoded request')
        request = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError('Invalid encoded roster request') from exc
    expected = {'site'} if mode == 'check' else {'site', 'runId'}
    if not isinstance(request, dict) or set(request) != expected or not isinstance(request['site'], dict):
        raise ValueError('Unexpected roster request fields')
    site = '--site-json=' + json.dumps(request['site'], separators=(',', ':'))
    if mode == 'check':
        return ['--check', site]
    run_id = request['runId']
    if (not isinstance(run_id, str) or not run_id.strip() or len(run_id.encode()) > 512
            or any(unicodedata.category(char) == 'Cc' for char in run_id)):
        raise ValueError('Invalid run ID')
    return ['--run-id=' + run_id, site]


def main():
    try:
        args = command_arguments(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return subprocess.run([sys.executable, '-B', str(HERE / 'refresh_roster.py'), *args], check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
