"""Invoke the restricted news runner with the selected team's configuration."""
from datetime import date, datetime
import base64
import json
import re
from zoneinfo import ZoneInfo


def encode_news_request(run_id, day, site=None, photo_credits=None):
    from fan_zone_tasks import encode_request, MAX_REQUEST_BYTES, MAX_TOKEN_BYTES
    token = encode_request(run_id, site, day)
    if photo_credits is None:
        return token
    from fan_zone_photo_credits import validate_catalog
    request = json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))
    request['photoCredits'] = validate_catalog(photo_credits)
    raw = json.dumps(request, separators=(',', ':'), ensure_ascii=False).encode()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError('News run request with photo credits is too large')
    token = base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
    if len(token) > MAX_TOKEN_BYTES:
        raise ValueError('News run token with photo credits is too large')
    return token


def publication_day(context, timezone='America/Los_Angeles'):
    logical = context.get('logical_date')
    if logical is None:
        run = context.get('dag_run')
        logical = getattr(run, 'start_date', None) or getattr(run, 'run_after', None)
    if logical is None:
        raise ValueError('Cannot determine a stable publication day from this DAG run')
    return logical.astimezone(ZoneInfo(timezone)).date().isoformat()


def validate_receipt(receipt, run_id, day, site=None):
    if not isinstance(receipt, dict) or receipt.get('schema_version') != 1 or receipt.get('runId') != run_id or receipt.get('publicationDay') != day:
        raise ValueError('News receipt identity mismatch')
    if site is not None and receipt.get('team') != site['slug']:
        raise ValueError('News receipt belongs to a different team')
    for field in ('articleCount', 'generatedCount', 'openaiRequestCount'):
        if type(receipt.get(field)) is not int or receipt[field] < 0:
            raise ValueError('Invalid news receipt count')
    if receipt['generatedCount'] > 1 or receipt['openaiRequestCount'] > 2:
        raise ValueError('News generation exceeded its per-run limits')
    files = receipt.get('files')
    if not isinstance(files, dict) or 'articles.json' not in files:
        raise ValueError('News receipt is missing its article collection')
    for filename, checksum in files.items():
        if filename != 'articles.json' and not re.fullmatch(r'images/[a-f0-9]{64}\.(jpg|png|webp)', filename):
            raise ValueError('Invalid news receipt file')
        if not isinstance(checksum, str) or not re.fullmatch('[a-f0-9]{64}', checksum):
            raise ValueError('Invalid news receipt checksum')
    if datetime.fromisoformat(receipt.get('updatedAt', '').replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('News receipt timestamp needs a timezone')
    return receipt


class NewsRefreshHook:
    def __init__(self, ssh_conn_id='sfz_news_host'):
        self.ssh_conn_id = ssh_conn_id

    def refresh(self, run_id, day, site=None, photo_credits=None):
        from airflow.providers.ssh.hooks.ssh import SSHHook
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError('Invalid news run request')
        token = encode_news_request(run_id, day, site, photo_credits)
        hook = SSHHook(ssh_conn_id=self.ssh_conn_id, conn_timeout=15, cmd_timeout=720, keepalive_interval=30, conn_retry_attempts=1)
        with hook.get_conn() as client:
            status, stdout, _ = hook.exec_ssh_client_command(client, 'refresh ' + token, get_pty=False, environment=None, timeout=720)
        if status:
            raise RuntimeError(f'News host runner exited {status}; inspect the task log')
        prefix = 'SFZ_NEWS_RECEIPT='
        lines = [line[len(prefix):] for line in stdout.decode('utf-8', 'replace').splitlines() if line.startswith(prefix)]
        if len(lines) != 1:
            raise ValueError('Expected exactly one news receipt')
        return validate_receipt(json.loads(lines[0]), run_id, day, site)
