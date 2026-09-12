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
    match = re.fullmatch(r'(refresh|check) ([A-Za-z0-9_-]{1,32768})', command)
    if not match:
        raise ValueError('Only check or refresh with an encoded news request is allowed')
    mode, token = match.groups()
    try:
        raw = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
        if len(raw) > 24576:
            raise ValueError('Request exceeds the size limit')
        if base64.urlsafe_b64encode(raw).decode().rstrip('=') != token:
            raise ValueError('Noncanonical encoding')
        request = json.loads(raw)
        allowed = ({'site'}, {'site', 'photoCredits'}, {'photoCredits'}) if mode == 'check' else (
            {'runId', 'publicationDay'}, {'runId', 'publicationDay', 'site'},
            {'runId', 'publicationDay', 'photoCredits'}, {'runId', 'publicationDay', 'site', 'photoCredits'})
        if not isinstance(request, dict) or set(request) not in allowed:
            raise ValueError('Unexpected request fields')
        extra = []
        if 'site' in request:
            if not isinstance(request['site'], dict):
                raise ValueError('Site configuration must be an object')
            from news_core import site_settings
            site = site_settings(request['site'])
            extra = ['--site-json=' + json.dumps(site, separators=(',', ':'))]
        if 'photoCredits' in request:
            from news_core import validate_catalog
            credits = validate_catalog(request['photoCredits'])
            extra.append('--photo-credits-json=' + json.dumps(credits, separators=(',', ':'), ensure_ascii=False))
        if mode == 'check':
            return ['--check', *extra]
        run_id, day = request['runId'], request['publicationDay']
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id.encode()) > 512 or any(unicodedata.category(c) == 'Cc' for c in run_id):
            raise ValueError('Invalid run ID')
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError('Invalid publication day')
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
        raise ValueError('Invalid encoded news request') from exc
    return ['--run-id=' + run_id, '--publication-day=' + day, *extra]


def main():
    try:
        args = command_arguments(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return subprocess.run([sys.executable, str(Path(__file__).with_name('refresh_news.py')), *args], check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
