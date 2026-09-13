#!/usr/bin/env python3
"""Stage the sixth site, then register only its empty host paths and coverage.

This helper does not contact Airflow, providers or Docker, change the live
Variable, install runner code, write schedules, or start any services/tasks.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[2]
SLUG = 'patriots'
VARIABLE = 'fan_zone_active_sites'
spec = importlib.util.spec_from_file_location('patriots_config', PROJECT / 'dags/fan_zone_config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


def unpack(value):
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and set(value) == {VARIABLE}:
        value = value[VARIABLE]
        if isinstance(value, str):
            value = json.loads(value)
    config.validate_sites(value)
    return value


def dump(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


def candidates(current, proposed):
    """Preserve every existing site value, including custom prompts and flags."""
    current = unpack(current)
    proposed = config.validate_site(SLUG, proposed)
    if proposed['enabled'] is not False:
        raise ValueError('The checked-in Patriots entry must remain disabled during onboarding')
    if SLUG in current and config.validate_site(SLUG, current[SLUG]) != proposed:
        raise ValueError('An existing Patriots entry differs or is enabled; review it before continuing')
    disabled = copy.deepcopy(current)
    disabled.setdefault(SLUG, proposed)
    enabled = copy.deepcopy(disabled)
    enabled[SLUG]['enabled'] = True
    config.validate_sites(disabled)
    config.validate_sites(enabled)
    return disabled, enabled


def stage(current_file, destination):
    current = unpack(json.loads(current_file.read_text()))
    proposed = unpack(json.loads((PROJECT / 'config/active-sites.json').read_text()))[SLUG]
    disabled, enabled = candidates(current, proposed)
    # A new folder prevents accidentally reusing an earlier Variable snapshot.
    destination.mkdir(mode=0o700)
    for name, value in (('current-sites.json', current), ('disabled-sites.json', disabled),
                        ('enabled-sites.json', enabled)):
        path = destination / name
        path.write_bytes(dump(value))
        path.chmod(0o600)
    print(f'Staged {destination}; only the Patriots entry is added, initially disabled.')
    print('No live Variable or host configuration was changed.')


def read_plan(plan):
    current = unpack(json.loads((plan / 'current-sites.json').read_text()))
    disabled = unpack(json.loads((plan / 'disabled-sites.json').read_text()))
    enabled = unpack(json.loads((plan / 'enabled-sites.json').read_text()))
    proposed = unpack(json.loads((PROJECT / 'config/active-sites.json').read_text()))[SLUG]
    expected_disabled, expected_enabled = candidates(current, proposed)
    if disabled != expected_disabled or enabled != expected_enabled:
        raise ValueError('Staged candidates differ from an additive merge; stage fresh files')
    return current, config.validate_sites(disabled)[SLUG]


def ordinary(path, owner, *, directory=False, allow_absent=False):
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f'Refusing a symlink in setup path: {component}')
    if not path.exists() and allow_absent:
        return
    info = path.stat()
    correct = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct or info.st_uid != owner or info.st_mode & 0o022:
        raise ValueError(f'Unexpected type/owner/permissions; left unchanged: {path}')
    if not directory and info.st_nlink != 1:
        raise ValueError(f'Refusing a multiply-linked setup file: {path}')


def new_directory(path, uid, gid, mode=0o755):
    ordinary(path, uid, directory=True, allow_absent=True)
    if not path.exists():
        path.mkdir(mode=mode)
        os.chown(path, uid, gid)
        path.chmod(mode)


def atomic(path, payload, uid=0, gid=0, mode=0o644):
    fd, temporary = tempfile.mkstemp(prefix='.patriots-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def host_plan(plan):
    if os.geteuid() != 0:
        raise ValueError('--check-host and --prepare-host require sudo')
    account = pwd.getpwnam('laurawkr')
    if account.pw_uid != 1000:
        raise ValueError('The reviewed EventSpy container writes as UID 1000; inspect this host before onboarding')
    current, site = read_plan(plan)
    shared = Path('/opt/fanzone-shared')
    eventspy = Path('/opt/fanzone-eventspy')
    for root in (shared, eventspy, eventspy / 'coverage'):
        ordinary(root, 0, directory=True)
    registry = shared / 'settings.json'
    ordinary(registry, 0)
    before = registry.read_bytes()
    document = json.loads(before)
    if document.get('version') != 1 or not isinstance(document.get('sites'), dict):
        raise ValueError('Expected the existing version-1 shared host registry')
    registered = config.validate_sites(document['sites'])
    # Identity/path drift must be reviewed; prompts and enabled flags intentionally
    # remain controlled by the Variable, independent of this host registry.
    identity = ('slug', 'name', 'city', 'abbreviation', 'website_root',
                'nfl_snapshot_dir', 'recap_snapshot_dir', 'news_snapshot_dir', 'balldontlie_team_id')
    for slug, value in config.validate_sites(current).items():
        if slug == SLUG:
            continue
        if slug not in registered or any(value.get(key) != registered[slug].get(key) for key in identity):
            raise ValueError(f'{slug}: live configuration and installed registry identities/paths differ')
    if SLUG in registered and registered[SLUG] != site:
        raise ValueError('The installed Patriots entry differs; nothing was overwritten')
    for field, expected in {
        'news_snapshot_dir': '/var/lib/patriotsfz-news/current',
        'news_photos_dir': '/var/lib/patriotsfz-news/photos',
        'nfl_snapshot_dir': '/var/lib/patriotsfz-nfl/current',
        'recap_snapshot_dir': '/var/lib/patriotsfz-recaps/current',
    }.items():
        if site.get(field) != expected:
            raise ValueError(f'Unexpected Patriots {field}; review the onboarding helper')
    if site['eventspy'] != {
        'coverage_file': 'patriots.json',
        'schedule_file': '/var/lib/fanzone-eventspy/schedules/patriots.json',
        'output_dir': '/var/lib/patriotsfz-eventspy-mirror/dev/public',
    }:
        raise ValueError('Unexpected Patriots EventSpy destinations')
    news = Path('/var/lib/patriotsfz-news')
    directories = [news, *(news / name for name in ('photos', 'assets', 'days', 'releases')),
                   *(Path('/var/lib/patriotsfz-' + kind) for kind in ('nfl', 'recaps', 'roster', 'guides')),
                   Path('/var/lib/patriotsfz-eventspy-mirror'), Path('/var/lib/patriotsfz-eventspy-mirror/dev'),
                   Path('/var/lib/patriotsfz-eventspy-mirror/dev/public')]
    for path in directories:
        ordinary(path, account.pw_uid, directory=True, allow_absent=True)
    coverage = PROJECT / 'deployment/eventspy/coverage/patriots.json'
    ordinary(coverage, account.pw_uid)
    rows = json.loads(coverage.read_bytes())
    if not isinstance(rows, list) or len(rows) != 17 or len({row.get('week') for row in rows}) != 17:
        raise ValueError('Expected 17 reviewed Patriots coverage rows')
    target = eventspy / 'coverage/patriots.json'
    ordinary(target, 0, allow_absent=True)
    if target.exists() and target.read_bytes() != coverage.read_bytes():
        raise ValueError('Installed Patriots coverage differs; review it before replacing anything')
    model = news / 'config.json'
    ordinary(model, account.pw_uid, allow_absent=True)
    if model.exists():
        model_bytes = model.read_bytes()
    else:
        original = Path('/var/lib/sfz-news/config.json')
        ordinary(original, account.pw_uid)
        model_bytes = original.read_bytes()
    if not isinstance(json.loads(model_bytes), dict):
        raise ValueError('Expected an ordinary news model configuration object')
    return account, site, registry, before, document, directories, coverage, target, model, model_bytes


def prepare_host(plan, apply=False):
    account, site, registry, before, document, directories, coverage, target, model, model_bytes = host_plan(plan)
    if not apply:
        print('Additive Patriots path/registry/coverage checks passed; no files or outputs changed.')
        print('This does not verify SSH connections, provider availability, schedules or the preview container.')
        return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backups = registry.parent / 'backups'
    new_directory(backups, 0, 0, 0o700)
    backup = backups / ('patriots-onboarding-' + stamp)
    new_directory(backup, 0, 0, 0o700)
    atomic(backup / 'settings.json', before, mode=0o600)
    for path in directories:
        new_directory(path, account.pw_uid, account.pw_gid)
    if not model.exists():
        atomic(model, model_bytes, account.pw_uid, account.pw_gid)
    if not target.exists():
        atomic(target, coverage.read_bytes())
    # Never overwrite another registry edit made during this preparation.
    if registry.read_bytes() != before:
        raise ValueError('Host registry changed during preparation; new empty paths retained, registry left untouched')
    if SLUG not in document['sites']:
        document['sites'][SLUG] = site
        atomic(registry, dump(document))
    print(f'Added Patriots host registration and prepared only its paths/coverage. Registry backup: {backup}')
    print('The live Variable is unchanged. No runner, schedule, current snapshot, service, connection or DAG state changed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--stage', action='store_true')
    mode.add_argument('--verify-current', action='store_true')
    mode.add_argument('--check-host', action='store_true')
    mode.add_argument('--prepare-host', action='store_true')
    parser.add_argument('--current', type=Path, help='Export of only fan_zone_active_sites, or its fallback JSON')
    parser.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.stage or args.verify_current:
        if args.current is None:
            parser.error('--stage/--verify-current requires --current')
        if args.stage:
            stage(args.current, args.plan)
        else:
            expected, _ = read_plan(args.plan)
            actual = unpack(json.loads(args.current.read_text()))
            if expected != actual:
                raise ValueError('The live configuration changed since staging; export again and stage a fresh plan')
            print('Current Variable values match the staged baseline; existing site values are preserved.')
    else:
        prepare_host(args.plan, args.prepare_host)


if __name__ == '__main__':
    try:
        main()
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(f'PATRIOTS PREPARATION STOPPED: {error}', file=sys.stderr)
        raise SystemExit(1)
