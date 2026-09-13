#!/usr/bin/env python3
"""Finish Patriots setup for the existing shared preview; never create a production site."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

BASE = Path('/home/laurawkr')
AIRFLOW = BASE / 'homelab-airflow'
TEMPLATE = BASE / 'templatefanzone'
PACKAGE = BASE / 'fanzone-patriots-update'
MANIFEST_SHA = '5ebdafafb7fe69b8cf3d654ad755e06dd72fcf2a2344fbcdc6388abc9e11ccdf'
MARKER = 'PATRIOTS_RESULT='

# Data travels over stdin, never by shell interpolation or command arguments.
SERVER = r'''
import base64
import json
import os
from pathlib import Path
import sys
from airflow.models.variable import Variable
from fan_zone_config import validate_sites
request = json.load(sys.stdin)
key = 'fan_zone_active_sites'
if 'AIRFLOW_VAR_FAN_ZONE_ACTIVE_SITES' in os.environ:
    raise SystemExit('An environment override controls active sites; the database Variable was left unchanged.')
def unpack(raw):
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict) and set(raw) == {key}:
        raw = raw[key]
        if isinstance(raw, str):
            raw = json.loads(raw)
    validate_sites(raw)
    return raw
def current():
    raw = Variable.get(key, default_var=None)
    return {'present': raw is not None,
            'sites': unpack(raw if raw is not None else Path('/opt/airflow/config/active-sites.json').read_text())}
action = request['action']
if action == 'export':
    result = current()
elif action == 'activate':
    before, candidate = request['before'], request['candidate']
    if current() != before:
        raise SystemExit('The live active-site Variable changed during setup; nothing was overwritten. Rerun this script.')
    validate_sites(candidate)
    old = before['sites']
    if {k:v for k,v in candidate.items() if k != 'patriots'} != {k:v for k,v in old.items() if k != 'patriots'}:
        raise SystemExit('Refusing to alter another team.')
    if candidate['patriots']['enabled'] is not True:
        raise SystemExit('Expected the enabled Patriots candidate.')
    Variable.set(key, candidate, serialize_json=True)
    actual = current()
    if actual['sites'] != candidate:
        raise SystemExit('Variable write did not change its effective value. Inspect the configured secrets backend before continuing.')
    result = {'enabled': True}
elif action == 'checks':
    from airflow.providers.ssh.hooks.ssh import SSHHook
    from fan_zone_photo_credits import load_catalog, validate_catalog
    from airflow.dag_processing.dagbag import BundleDagBag
    from unittest.mock import patch
    sites = validate_sites(request['candidate'])
    site = sites['patriots']
    # Validate the actual installed DAG code before enabling the new task graph.
    tasks = {'sfz_nfl_refresh':'refresh_nfl_snapshot', 'sfz_roster_refresh':'refresh_roster',
             'sfz_daily_article':'generate_article', 'sfz_game_recaps':'generate_recaps',
             'sfz_eventspy_collect':'collect_ticket_prices', 'sfz_game_guides':'refresh_guides'}
    with patch('fan_zone_tasks.active_sites', return_value=[sites[s] for s in sorted(sites) if sites[s]['enabled']]):
        for dag_id, prefix in tasks.items():
            bag = BundleDagBag(dag_folder='/opt/airflow/dags/' + dag_id + '.py', bundle_path=Path('/opt/airflow/dags'))
            if bag.import_errors or dag_id not in bag.dags:
                raise SystemExit('DAG import failed: ' + dag_id + ': ' + str(bag.import_errors))
            dag = bag.dags[dag_id]
            for slug in (s for s in sites if sites[s]['enabled']):
                if prefix + '_' + slug not in dag.task_ids or 'save_run_receipt_' + slug not in dag.task_ids:
                    raise SystemExit('Missing named tasks: ' + dag_id + '/' + slug)
            print('Validated:', dag_id, flush=True)
    raw_credits = Variable.get('fan_zone_photo_credits', default_var=None)
    credits = load_catalog() if raw_credits is None else validate_catalog(json.loads(raw_credits))
    connections = ['sfz_nfl_host', 'sfz_news_host', 'sfz_roster_host']
    if request.get('dependents'):
        connections += ['sfz_recap_host', 'sfz_guides_host']
    for connection in connections:
        message = {'site': site}
        if connection == 'sfz_news_host':
            message['photoCredits'] = credits
        payload = json.dumps(message, separators=(',', ':'), ensure_ascii=False).encode()
        if len(payload) > 24576:
            raise SystemExit('Source-free check payload is too large.')
        token = base64.urlsafe_b64encode(payload).decode().rstrip('=')
        print('Checking:', connection, flush=True)
        hook = SSHHook(ssh_conn_id=connection, conn_timeout=15, cmd_timeout=120, conn_retry_attempts=1)
        with hook.get_conn() as client:
            status, output, error = hook.exec_ssh_client_command(client, 'check ' + token,
                get_pty=False, environment=None, timeout=120)
        print(output.decode('utf-8', 'replace'), flush=True)
        if status:
            print(error.decode('utf-8', 'replace'), flush=True)
            raise SystemExit('Source-free check failed: ' + connection)
    result = {'checks': 'passed'}
else:
    raise SystemExit('Unknown operation.')
print('PATRIOTS_RESULT=' + json.dumps(result, ensure_ascii=False))
'''

SCHEDULE = r'''
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
root = Path('/home/laurawkr/homelab-airflow')
spec = importlib.util.spec_from_file_location('patriots_schedule', root / 'deployment/eventspy/schedules.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
site = json.loads(Path('/opt/fanzone-shared/settings.json').read_text())['sites']['patriots']
coverage = json.loads(Path('/opt/fanzone-eventspy/coverage/patriots.json').read_text())
payload = module.schedule_from_nfl_snapshot(site, coverage, datetime.now(timezone.utc))
if payload is None:
    raise SystemExit('A fresh, successful Patriots NFL snapshot is required. No provider request or cache write was made.')
module.validate_schedule(payload, site, coverage)
path = Path('/var/lib/fanzone-eventspy/schedules/patriots.json')
module._atomic_write(path, payload)
print('Prepared Patriots EventSpy schedule from its verified NFL snapshot:', path)
'''


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args, *, cwd=AIRFLOW, capture=False, input=None):
    result = subprocess.run([str(a) for a in args], cwd=cwd, text=True,
                            input=input, capture_output=capture, check=False)
    if result.returncode:
        if capture:
            print(result.stdout, end='')
            print(result.stderr, end='', file=sys.stderr)
        raise RuntimeError(f'{args[0]} failed (exit {result.returncode}); retain completed commits and rerun after resolving the reported error.')
    return result.stdout if capture else None


def server(action, **values):
    # Inherit output for checks so long SSH operations remain visible.
    output = run(['docker', 'compose', 'exec', '-T', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                  '-e', 'PYTHONPATH=/opt/airflow/dags', '-e', '_AIRFLOW_PROCESS_CONTEXT=server',
                  'airflow-scheduler', 'python', '-B', '-c', SERVER],
                 input=json.dumps({'action': action, **values}), capture=action != 'checks')
    if action == 'checks':
        return
    rows = [line[len(MARKER):] for line in output.splitlines() if line.startswith(MARKER)]
    if len(rows) != 1:
        raise ValueError('Could not read a unique Airflow result; no next step was attempted.')
    return json.loads(rows[0])


def check_source():
    manifest_bytes = (PACKAGE / 'manifest.json').read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA:
        raise ValueError('Expected the reviewed fanzone-patriots-update package manifest.')
    applier = load_module('patriots_source_check', PACKAGE / 'apply_update.py')
    roots = {'airflow': AIRFLOW, 'template': TEMPLATE}
    if applier.prepare(roots):
        raise ValueError('Source changes are not fully applied; run the existing apply_update.py --apply first.')
    return roots, json.loads(manifest_bytes)


def commit_source(roots, manifest):
    # Preflight both indexes before staging anything. Never absorb partial staging.
    for root in roots.values():
        branch = run(['git', 'branch', '--show-current'], cwd=root, capture=True).strip()
        if branch != 'main':
            raise ValueError(f'Expected main in {root}; no branch was changed.')
        if run(['git', 'diff', '--cached', '--name-only'], cwd=root, capture=True).strip():
            raise ValueError(f'{root} has staged changes. Commit or unstage those deliberately before rerunning; this script left the index alone.')
    for name, root in roots.items():
        paths = [entry['path'] for entry in manifest['files'] if entry['repo'] == name]
        if not run(['git', 'status', '--porcelain', '--', *paths], cwd=root, capture=True).strip():
            print('Reviewed source already committed:', root.name, flush=True)
            continue
        # --only limits the commit even if an unrelated file gets staged concurrently.
        run(['git', 'add', '--', *paths], cwd=root)
        run(['git', 'commit', '--only', '-m', 'Add Patriots to shared Fan Zone preview pipelines', '--', *paths], cwd=root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--after-nfl', action='store_true', help='After the first NFL success, verify dependent inputs and prepare only Patriots schedule cache.')
    args = parser.parse_args()
    if os.geteuid() == 0 or Path.home() != BASE:
        raise ValueError('Run as laurawkr, without sudo; only host preparation requests sudo itself.')
    os.umask(0o077)
    roots, manifest = check_source()
    helper = load_module('patriots_host_prepare', AIRFLOW / 'deployment/shared/prepare_patriots.py')
    before = server('export')
    sites = helper.unpack(before['sites'])
    proposed = helper.config.validate_site('patriots', helper.unpack(json.loads((AIRFLOW / 'config/active-sites.json').read_text()))['patriots'])
    present = sites.get('patriots')
    already_enabled = present is not None and present.get('enabled') is True
    if already_enabled:
        if helper.config.validate_site('patriots', present) != dict(proposed, enabled=True):
            raise ValueError('Live Patriots settings differ from this reviewed addition; existing values were retained.')
        candidate = sites
    else:
        _, candidate = helper.candidates(sites, proposed)
    if args.after_nfl:
        if not already_enabled:
            raise ValueError('Patriots is not enabled yet. Run this script without --after-nfl first.')
        server('checks', candidate=candidate, dependents=True)
        run(['sudo', 'python3', '-B', '-c', SCHEDULE])
        run(['python3', '-B', TEMPLATE / 'template-tools/build-team.py', 'patriots', '--dry-run'], cwd=TEMPLATE)
        print('NFL/dependent checks, schedule cache and required build input paths are ready. Run bash template-tools/build-team.sh patriots from templatefanzone.')
        return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    work = BASE / ('patriots-activation-' + stamp)
    work.mkdir(mode=0o700)
    (work / 'variable-before.json').write_text(json.dumps(before, indent=2, ensure_ascii=False) + '\n')
    (work / 'current-sites.json').write_text(json.dumps(sites, indent=2, ensure_ascii=False) + '\n')
    print('Saved current active-site values:', work, flush=True)
    commit_source(roots, manifest)
    if not already_enabled:
        helper.stage(work / 'current-sites.json', work / 'plan')
        helper_script = AIRFLOW / 'deployment/shared/prepare_patriots.py'
        run(['sudo', 'python3', '-B', helper_script, '--check-host', '--plan', work / 'plan'])
        run(['sudo', 'python3', '-B', helper_script, '--prepare-host', '--plan', work / 'plan'])
        candidate = json.loads((work / 'plan/enabled-sites.json').read_text())
    server('checks', candidate=candidate, dependents=False)
    preview = Path(__file__).resolve().with_name('add-patriots-preview.py')
    if not preview.is_file():
        raise ValueError('Keep add-patriots-preview.py beside this script; extract the complete activation ZIP.')
    run(['python3', '-B', preview, '--apply'])
    if not already_enabled:
        server('activate', before=before, candidate=candidate)
        print('Patriots enabled; all other live site values were preserved.', flush=True)
    else:
        print('Patriots was already enabled; source-free checks passed.', flush=True)
    print('In Airflow, wait for refresh_nfl_snapshot_patriots to appear in sfz_nfl_refresh, then trigger that DAG or wait for its next scheduled run.')
    print('A normal manual run refreshes all enabled teams. This script did not trigger a run or change DAG pause states.')
    print('After Patriots NFL task and receipt succeed: python3 ' + str(Path(__file__).resolve()) + ' --after-nfl')
    print('Reviewed source was committed locally. Push each checkout with git push origin main when ready.')
    print('The existing preview now has the Patriots ticket mount/alias. Prices arrive with a successful eligible scheduled EventSpy run.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit('PATRIOTS SETUP STOPPED: ' + str(exc))
