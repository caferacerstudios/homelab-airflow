#!/usr/bin/env python3
"""Register team output paths for the existing Airflow host runners."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = Path('/opt/fanzone-shared')


def ordinary(path, uid, *, directory=False):
    path = Path(path)
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f'Refusing a symlink in a setup path: {part}')
    if not path.exists():
        return
    info = path.stat()
    correct = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct or info.st_uid != uid or info.st_mode & 0o022:
        raise ValueError(f'Unexpected ownership/type/permissions at {path}; left unchanged')


def load_sites(filename):
    config = HERE.parents[1] / 'dags/fan_zone_config.py'
    spec = importlib.util.spec_from_file_location('team_install_config', config)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sites = module.validate_sites(json.loads(Path(filename).read_text()))
    for slug, site in sites.items():
        for field in ('nfl_snapshot_dir', 'recap_snapshot_dir', 'abbreviation', 'division'):
            if field not in site:
                raise ValueError(f'Add {field} for {slug} before installing its pipelines')
    return sites, config


def preflight(sites, uid):
    ordinary(ROOT, 0, directory=True)
    for name in ('settings.json', 'fan_zone_host.py', 'fan_zone_config.py'):
        ordinary(ROOT / name, 0)
    for site in sites.values():
        for field in ('news_snapshot_dir', 'nfl_snapshot_dir', 'recap_snapshot_dir'):
            ordinary(Path(site[field]).parent, uid, directory=True)
        news = Path(site['news_snapshot_dir']).parent
        for folder in ('photos', 'assets', 'days', 'releases'):
            ordinary(news / folder, uid, directory=True)
        ordinary(news / 'config.json', uid)
    # A terminated Airflow task can leave a Docker job alive. Do not replace its
    # runner while that container continues publishing an existing snapshot.
    for pipeline in ('sfz-nfl', 'sfz-recaps'):
        result = subprocess.run(['docker', 'ps', '-q', '--filter', f'label=com.caferacerstudios.pipeline={pipeline}'],
                                check=True, text=True, capture_output=True, timeout=30)
        if result.stdout.strip():
            raise ValueError(f'{pipeline} has a running container; let it finish before updating')


def directory(path, uid, gid, mode=0o755):
    ordinary(path, uid, directory=True)
    if not path.exists():
        path.mkdir(mode=mode)
        os.chown(path, uid, gid)
        path.chmod(mode)


def atomic(path, data, uid=0, gid=0, mode=0o644):
    ordinary(path, uid)
    fd, name = tempfile.mkstemp(prefix='.team-install-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install(sites, config, uid, gid):
    directory(ROOT, 0, 0)
    directory(ROOT / 'backups', 0, 0, 0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = ROOT / 'backups' / stamp
    directory(backup, 0, 0, 0o700)
    payload = {'fan_zone_host.py': (HERE / 'fan_zone_host.py').read_bytes(),
               'fan_zone_config.py': config.read_bytes(),
               'settings.json': (json.dumps({'version': 1, 'sites': sites}, indent=2) + '\n').encode()}
    for name, content in payload.items():
        if name.endswith('.py'):
            compile(content, name, 'exec')
        existing = ROOT / name
        if existing.exists():
            atomic(backup / name, existing.read_bytes(), mode=0o600)
    model_config = Path('/var/lib/sfz-news/config.json')
    ordinary(model_config, uid)
    default_news = model_config.read_bytes() if model_config.is_file() else b'{"model":"gpt-5.4-mini"}\n'
    if not isinstance(json.loads(default_news), dict):
        raise ValueError('Existing Seattle news configuration must be a JSON object')
    for site in sites.values():
        for field in ('news_snapshot_dir', 'nfl_snapshot_dir', 'recap_snapshot_dir'):
            directory(Path(site[field]).parent, uid, gid)
        news = Path(site['news_snapshot_dir']).parent
        for folder in ('photos', 'assets', 'days', 'releases'):
            directory(news / folder, uid, gid)
        if not (news / 'config.json').exists():
            atomic(news / 'config.json', default_news, uid, gid)
    for name, content in payload.items():
        atomic(ROOT / name, content)
    print('Registered team NFL/recap paths and prepared news/photo directories: ' + ', '.join(sorted(sites)))
    print('Existing current snapshots and SSH connections were retained. No generation or collection was run.')


def resolve_ids(sites):
    spec = importlib.util.spec_from_file_location('team_id_lookup', HERE.parent / 'eventspy/schedules.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key = module.read_api_key(module.API_KEY_PATH)
    response = module.request_json(module.API_BASE + '/teams?per_page=100', key)
    if not isinstance(response.get('data'), list) or response.get('meta', {}).get('next_cursor') is not None:
        raise ValueError('Expected one complete NFL team directory from balldontlie')
    for slug, site in sites.items():
        matches = [team for team in response['data'] if team.get('abbreviation') == site['abbreviation']]
        if len(matches) != 1 or type(matches[0].get('id')) is not int or matches[0]['id'] < 1:
            raise ValueError(f'balldontlie did not uniquely identify {slug}')
        team = matches[0]
        if site.get('balldontlie_team_id') not in (None, team['id']):
            raise ValueError(f'Configured balldontlie ID does not match {slug}')
        full_name = team.get('full_name')
        if (not isinstance(full_name, str) or not full_name.strip()
                or full_name.casefold() != f"{site['city']} {site['name']}".casefold()):
            raise ValueError(f'balldontlie full name does not match {slug}')
        site['balldontlie_team_id'] = team['id']
    return sites


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--install', action='store_true')
    mode.add_argument('--resolve-ids', action='store_true')
    parser.add_argument('--sites', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('Run this host setup through the outer installer, which requests sudo')
    account = pwd.getpwnam('laurawkr')
    sites, config = load_sites(args.sites)
    if args.resolve_ids:
        print('TEAM_SITES=' + json.dumps(resolve_ids(sites)))
        return
    preflight(sites, account.pw_uid)
    if args.install:
        install(sites, config, account.pw_uid, account.pw_gid)
    else:
        for filename in (HERE / 'fan_zone_host.py', config):
            compile(filename.read_bytes(), str(filename), 'exec')
        print('Shared host path checks passed; no files or outputs changed.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'TEAM HOST SETUP STOPPED: {error}', file=sys.stderr)
        raise SystemExit(1)
