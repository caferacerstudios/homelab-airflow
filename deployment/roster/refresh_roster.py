#!/usr/bin/env python3
"""Collect official team personnel data in isolation; publish a verified snapshot.

No website files, NFL snapshots, article outputs, or existing service settings
are changed. The only writable publication root is derived from the registered
team's news root, replacing the final '-news' suffix with '-roster'.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import unicodedata
import uuid

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
SHARED = Path('/opt/fanzone-shared')
IMAGE = 'node:22-bookworm'
FILES = ('roster.json', 'injuries.json', 'transactions.json')
SOURCE_FILES = ('collector.mjs', 'teams.json', 'refresh_roster.py', 'ssh_entrypoint.py')
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_COLLECTOR_ERROR_CHARS = 2000


def load_object(path: Path) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
        raise ValueError(f'Expected an ordinary bounded JSON file: {path}')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f'Expected a JSON object: {path.name}')
    return value


def ordinary_path(path: Path, *, directory=False, writable=False) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f'Refusing symlink in an ordinary path: {part}')
    info = path.stat()
    correct = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct:
        raise ValueError(f'Unexpected path type: {path}')
    if writable and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError(f'Expected a user-owned, non-shared writable path: {path}')


def ensure_run_directory(path: Path) -> None:
    """Create/repair only this runner's exact state directory, never recursively."""
    path.mkdir(mode=0o755, exist_ok=True)
    ordinary_path(path, directory=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o7002:
            raise ValueError(f'Unexpected owner or permissions on roster run directory: {path}')
        # Earlier versions inherited umask 0002 and left these directories 0775.
        os.fchmod(descriptor, 0o755)
    finally:
        os.close(descriptor)
    ordinary_path(path, directory=True, writable=True)


@contextmanager
def collection_lock(path: Path):
    ordinary_path(path.parent, directory=True, writable=True)
    if path.exists() or path.is_symlink():
        ordinary_path(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o7002):
            raise ValueError(f'Unexpected owner, links or permissions on roster lock: {path}')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another roster collection is running for this team') from None
        # Keep the inode: deleting a lock can allow two simultaneous publishers.
        os.fchmod(lock.fileno(), 0o600)
        yield lock


def runtime_for(site: dict) -> Path:
    value = site.get('news_snapshot_dir')
    if not isinstance(value, str) or not re.fullmatch(r'/var/lib/[a-z][a-z0-9-]{0,59}-news/current', value):
        raise ValueError('Roster output requires a valid registered news snapshot path')
    return Path(value.removesuffix('-news/current') + '-roster')


def site_settings(site: dict, settings_path: Path | None = None) -> dict:
    if not isinstance(site, dict):
        raise ValueError('An explicit active-site request is required')
    if str(SHARED) not in sys.path:
        sys.path.insert(0, str(SHARED))
    from fan_zone_host import authorize_site
    path = settings_path or SHARED / 'settings.json'
    selected = authorize_site(site, 'nfl', path)
    # The existing NFL registry validator pins NFL/recap identities but does not
    # pin news destinations; roster derives a path from news and pins that too.
    registered = load_object(path)['sites'][selected['slug']]
    if selected['news_snapshot_dir'] != registered['news_snapshot_dir']:
        raise ValueError('Task news output differs from the installed host registry')
    if runtime_for(selected) != runtime_for(registered):
        raise ValueError('Roster output differs from the installed host registry')
    return selected


def command(args: list[str], **kwargs) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def source_commit(project: Path = PROJECT) -> str:
    if Path(command(['git', '-C', str(project), 'rev-parse', '--show-toplevel'])).resolve() != project.resolve():
        raise ValueError('Airflow source must be its own Git checkout')
    relative = ['deployment/roster/' + name for name in SOURCE_FILES]
    if command(['git', '-C', str(project), 'status', '--porcelain', '--', *relative]):
        raise ValueError('Commit or resolve roster runner changes before collecting; no source was changed')
    commit = command(['git', '-C', str(project), 'rev-parse', 'HEAD'])
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Expected a full Airflow source commit')
    return commit


def preflight(site: dict, runtime: Path, *, require_runtime=True) -> str:
    commit = source_commit()
    for name in SOURCE_FILES:
        ordinary_path(HERE / name)
    registry = load_object(HERE / 'teams.json')
    if site['slug'] not in registry:
        raise ValueError('Review official personnel sources for this team before enabling its roster task')
    known = registry[site['slug']]
    if known.get('name') != site['city'] + ' ' + site['name'] or known.get('abbreviation') != site['abbreviation']:
        raise ValueError('Active-site identity differs from the reviewed official-source registry')
    if require_runtime:
        ordinary_path(runtime, directory=True, writable=True)
        if not os.access(runtime, os.W_OK | os.X_OK):
            raise ValueError(f'Roster runtime is not writable: {runtime}')
    command(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=30)
    try:
        command(['docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}'], timeout=30)
    except subprocess.CalledProcessError:
        raise RuntimeError(f'Required collector image is not cached. Run: docker pull {IMAGE}') from None
    return commit


def timestamp(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError('Snapshot timestamp is missing')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Snapshot timestamps must include a timezone')
    return parsed


def file_hash(path: Path) -> str:
    ordinary_path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_snapshot(runtime: Path) -> Path | None:
    ordinary_path(runtime, directory=True)
    current = runtime / 'current'
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise ValueError(f'Expected a current symlink; existing output left untouched: {current}')
    target = current.resolve(strict=True)
    if not target.is_relative_to(runtime.resolve() / 'runs'):
        raise ValueError('Current snapshot escapes its pipeline runtime')
    ordinary_path(target, directory=True)
    return target


def verify_payloads(folder: Path, site: dict) -> dict:
    payloads = {name: load_object(folder / name) for name in FILES}
    for name, payload in payloads.items():
        if payload.get('team') != site['slug']:
            raise ValueError(f'Wrong team in roster output: {name}')
        if payload.get('schemaVersion') != (2 if name == 'injuries.json' else 1):
            raise ValueError(f'Unsupported personnel schema: {name}')
        timestamp(payload.get('sourceCheckedAt'))
        if payload.get('asOf') is not None:
            timestamp(payload['asOf'])
        elif name != 'injuries.json' or payload.get('availability') != 'unavailable':
            raise ValueError(f'Missing personnel observation timestamp: {name}')
    for name, field in (('roster.json', 'players'), ('injuries.json', 'records'), ('transactions.json', 'records')):
        rows = payloads[name].get(field)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError(f'Missing {field} array in {name}')
    if not payloads['roster.json']['players']:
        raise ValueError('Refusing an empty roster publication')
    identities = []
    for player in payloads['roster.json']['players']:
        if any(not isinstance(player.get(key), str) or not player[key].strip() for key in ('id', 'name', 'position', 'status')):
            raise ValueError('Roster player identity or status is missing')
        identities.append(player['id'])
    if len(set(identities)) != len(identities):
        raise ValueError('Roster player IDs are duplicated')
    if payloads['injuries.json'].get('availability') not in ('available', 'unavailable'):
        raise ValueError('Injury availability must be explicit')
    return payloads


def verify_snapshot(snapshot: Path, site: dict, run_id: str | None = None) -> dict:
    ordinary_path(snapshot, directory=True)
    manifest = load_object(snapshot / 'manifest.json')
    if (manifest.get('schema_version') != 1 or manifest.get('pipeline') != 'roster'
            or manifest.get('team') != site['slug'] or (run_id is not None and manifest.get('runId') != run_id)):
        raise ValueError('Roster snapshot manifest identity is invalid')
    timestamp(manifest.get('updatedAt'))
    validate_run_id(manifest.get('runId'))
    if (not isinstance(manifest.get('sourceCommit'), str) or not re.fullmatch(r'[0-9a-f]{40}', manifest['sourceCommit'])
            or type(manifest.get('requestCount')) is not int or not 0 <= manifest['requestCount'] <= 30):
        raise ValueError('Roster snapshot provenance is invalid')
    files = manifest.get('files')
    if not isinstance(files, dict) or set(files) != set(FILES):
        raise ValueError('Roster snapshot file list is invalid')
    for name, digest in files.items():
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or file_hash(snapshot / name) != digest:
            raise ValueError(f'Roster snapshot checksum mismatch: {name}')
    payloads = verify_payloads(snapshot, site)
    if any(timestamp(payload['sourceCheckedAt']) != timestamp(manifest['updatedAt']) for payload in payloads.values()):
        raise ValueError('Roster snapshot timestamps differ')
    return manifest


def previous_payloads(site: dict, runtime: Path) -> dict:
    previous = current_snapshot(runtime)
    if previous is not None:
        verify_snapshot(previous, site)
        return {name.removesuffix('.json'): load_object(previous / name) for name in FILES}
    result = {name.removesuffix('.json'): None for name in FILES}
    if site['slug'] == 'seahawks':
        # Retain Seattle's manually collected history on the first run only.
        seed = Path(site['website_root']) / 'src/data/team'
        for name in FILES:
            path = seed / name
            if path.exists() or path.is_symlink():
                ordinary_path(path)
                result[name.removesuffix('.json')] = load_object(path)
    return result


def nfl_payloads(site: dict) -> dict | None:
    runtime = Path(site['nfl_snapshot_dir']).parent
    if not runtime.exists() and not runtime.is_symlink():
        return None
    snapshot = current_snapshot(runtime)
    if snapshot is None:
        return None
    manifest = load_object(snapshot / 'manifest.json')
    slug = site['slug']
    if (manifest.get('schema_version') != 1 or manifest.get('team') not in ([None, slug] if slug == 'seahawks' else [slug])):
        raise ValueError('NFL snapshot manifest belongs to another team')
    validate_run_id(manifest.get('runId'))
    updated = timestamp(manifest.get('updatedAt'))
    if updated > datetime.now(timezone.utc):
        raise ValueError('NFL snapshot publication timestamp is in the future')
    names = (slug + '.json', 'players.json', 'standings.json')
    files = manifest.get('files')
    if not isinstance(files, dict) or not set(names) <= set(files) or not set(files) <= {*names, 'gameRecaps.json'}:
        raise ValueError('NFL snapshot manifest file list is invalid')
    for name, digest in files.items():
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or file_hash(snapshot / name) != digest:
            raise ValueError(f'NFL snapshot checksum mismatch: {name}')
    schedule, players = (load_object(snapshot / name) for name in names[:2])
    season = manifest.get('season')
    if type(season) is not int or not 2002 <= season <= 2200:
        raise ValueError('NFL snapshot season is invalid')
    for value in (schedule, players):
        team = value.get('team', {})
        if (not isinstance(team, dict) or team.get('abbreviation') != site['abbreviation'] or value.get('season') != season
                or (site.get('balldontlie_team_id') is not None and team.get('id') != site['balldontlie_team_id'])):
            raise ValueError('NFL input team or season differs from the selected site')
        if value.get('fixture') is True or timestamp(value.get('updatedAt')) != updated:
            raise ValueError('NFL input is a fixture or differs from the publication timestamp')
    if not isinstance(schedule.get('gamesRegular'), list) or not isinstance(players.get('playerDirectory'), list):
        raise ValueError('NFL input lacks its schedule or player-directory arrays')
    return {'schedule': schedule, 'players': players}


def collector_error_summary(output: str) -> str:
    """Keep the useful log tail visible in Airflow without flooding its log."""
    prefix = '... ' if len(output) > MAX_COLLECTOR_ERROR_CHARS else ''
    tail = output[-(MAX_COLLECTOR_ERROR_CHARS - len(prefix)):]
    tail = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', tail)
    tail = ''.join(char if char.isprintable() else ' ' for char in tail)
    return prefix + ' '.join(tail.split()) if tail.strip() else 'no diagnostic output'


def run_node(work: Path, run_key: str, slug: str) -> None:
    label = 'com.caferacerstudios.pipeline=sfz-roster-' + slug
    if command(['docker', 'ps', '-q', '--filter', 'label=' + label], timeout=30):
        raise RuntimeError('A previous roster container is running; no duplicate collection started')
    name = f'sfz-roster-{slug}-{run_key[:16]}-{uuid.uuid4().hex[:8]}'
    args = ['docker', 'run', '--rm', '--pull=never', '--name', name, '--label', label,
            '--read-only', '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--memory', '512m', '--pids-limit', '128',
            '--mount', f'type=bind,src={HERE},dst=/app,readonly',
            '--mount', f'type=bind,src={work},dst=/work', '--workdir', '/app',
            IMAGE, 'node', '/app/collector.mjs', '/work/request.json', '/work/candidate']
    process = subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        output, _ = process.communicate(timeout=480)
    except BaseException as exc:
        try:
            subprocess.run(['docker', 'rm', '--force', name], capture_output=True, timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
            partial, _ = process.communicate(timeout=15)
            (work / 'collector.log').write_text(partial or str(exc))
        if isinstance(exc, subprocess.TimeoutExpired):
            raise TimeoutError('Roster collection exceeded its eight-minute timeout') from None
        raise
    (work / 'collector.log').write_text(output)
    if process.returncode:
        raise RuntimeError(f'Roster collector exited {process.returncode}: {collector_error_summary(output)}; '
                           f'full log: {work / "collector.log"}')


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_link(snapshot: Path, runtime: Path) -> None:
    link = runtime / ('.current-' + uuid.uuid4().hex)
    try:
        link.symlink_to(snapshot.relative_to(runtime))
        os.replace(link, runtime / 'current')
        sync_directory(runtime)
    finally:
        link.unlink(missing_ok=True)


def publish_if_newer(snapshot: Path, runtime: Path, site: dict, manifest: dict) -> None:
    previous = current_snapshot(runtime)
    # Equal timestamps do not establish that an older run should replace the
    # current one; this also makes rapid same-millisecond replays harmless.
    if previous is None or timestamp(verify_snapshot(previous, site)['updatedAt']) < timestamp(manifest['updatedAt']):
        publish_link(snapshot, runtime)


def validate_run_id(value: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value.encode()) > 512
            or any(unicodedata.category(char) == 'Cc' for char in value)):
        raise ValueError('run-id must contain 1-512 UTF-8 bytes without control characters')
    return value


def _collect(run_id: str, site: dict, runtime: Path | None = None) -> dict:
    validate_run_id(run_id)
    selected = site_settings(site)
    if not selected['enabled']:
        raise ValueError('Roster refresh is disabled for this site')
    runtime = runtime_for(selected) if runtime is None else Path(runtime)
    ordinary_path(runtime, directory=True, writable=True)
    run_key = hashlib.sha256(run_id.encode()).hexdigest()
    lock_path = runtime / 'collector.lock'
    with collection_lock(lock_path):
        runs = runtime / 'runs'
        ensure_run_directory(runs)
        run_dir = runs / run_key
        ensure_run_directory(run_dir)
        snapshot = run_dir / 'snapshot'
        if snapshot.exists() or snapshot.is_symlink():
            manifest = verify_snapshot(snapshot, selected, run_id)
            publish_if_newer(snapshot, runtime, selected, manifest)
            return {**manifest, 'snapshotPath': str(snapshot), 'reused': True}
        commit = preflight(selected, runtime)
        clock = datetime.now(timezone.utc)
        started = clock.replace(microsecond=(clock.microsecond // 1000) * 1000)
        now = started.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        request = {'site': selected, 'runId': run_id, 'now': now,
                   'previous': previous_payloads(selected, runtime), 'nfl': nfl_payloads(selected)}
        work = run_dir / f'work-{time.time_ns()}'
        work.mkdir()
        (work / 'request.json').write_text(json.dumps(request) + '\n')
        if source_commit() != commit:
            raise RuntimeError('Airflow source changed before collection')
        run_node(work, run_key, selected['slug'])
        if source_commit() != commit:
            raise RuntimeError('Airflow source changed during collection; previous output retained')
        candidate = work / 'candidate'
        ordinary_path(candidate, directory=True)
        if {path.name for path in candidate.iterdir()} != {*FILES, 'report.json'}:
            raise ValueError('Collector output must contain exactly the three personnel files and report')
        payloads = verify_payloads(candidate, selected)
        report = load_object(candidate / 'report.json')
        if (report.get('status') != 'success' or report.get('team') != selected['slug']
                or report.get('runId') != run_id):
            raise ValueError('Collector did not report success for the selected team')
        updated = report.get('updatedAt')
        if not started <= timestamp(updated) <= datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError('Collector report has an invalid freshness timestamp')
        if any(timestamp(value['sourceCheckedAt']) != timestamp(updated) for value in payloads.values()):
            raise ValueError('Collector payload and report timestamps differ')
        if type(report.get('requestCount')) is not int or not 0 <= report['requestCount'] <= 30:
            raise ValueError('Collector request count is invalid')
        pending = run_dir / ('snapshot-' + uuid.uuid4().hex + '.tmp')
        pending.mkdir()
        for name in FILES:
            shutil.copyfile(candidate / name, pending / name)
            with (pending / name).open('rb') as stream:
                os.fsync(stream.fileno())
        manifest = {'schema_version': 1, 'pipeline': 'roster', 'team': selected['slug'],
                    'runId': run_id, 'updatedAt': updated, 'sourceCommit': commit, 'requestCount': report['requestCount'],
                    'files': {name: file_hash(pending / name) for name in FILES}, 'report': report}
        with (pending / 'manifest.json').open('w') as stream:
            stream.write(json.dumps(manifest, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        verify_snapshot(pending, selected, run_id)
        sync_directory(pending)
        pending.rename(snapshot)
        sync_directory(run_dir)
        publish_if_newer(snapshot, runtime, selected, manifest)
        return {**manifest, 'snapshotPath': str(snapshot), 'reused': False}


def collect(run_id: str, site: dict, runtime: Path | None = None) -> dict:
    # SSH can inherit 0002 (group writable) or 0077 (unreadable by builders).
    # This host runner is a dedicated process; leave its caller's mask unchanged.
    previous_mask = os.umask(0o022)
    try:
        return _collect(run_id, site, runtime)
    finally:
        os.umask(previous_mask)


def interrupted(_signum, _frame):
    raise InterruptedError('Roster collector interrupted; its owned container is being stopped')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--run-id')
    parser.add_argument('--site-json', required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        site = json.loads(args.site_json)
        selected = site_settings(site)
        if args.check:
            commit = preflight(selected, runtime_for(selected))
            print(json.dumps({'status': 'ready', 'team': selected['slug'], 'sourceCommit': commit,
                              'snapshotPath': str(runtime_for(selected) / 'current'), 'sourceRequests': 0}))
        else:
            print('SFZ_ROSTER_RECEIPT=' + json.dumps(collect(args.run_id, site), separators=(',', ':')))
        return 0
    except BrokenPipeError:
        return 0
    except Exception as exc:
        print(f'Roster refresh failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
