#!/usr/bin/env python3
"""Bounded, one-day Airflow bridge to the existing EventSpy service.

Only this root-owned program can admit source work. Queue input is untrusted.
The installed CLI always uses ROOT; tests inject a temporary root and fake host.
"""
import argparse
import contextlib
from datetime import datetime, time, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import time as clock_time
import uuid
from zoneinfo import ZoneInfo

ROOT = Path('/var/lib/sfz-airflow-ticket-test')
LA = ZoneInfo('America/Los_Angeles')
UTC = timezone.utc
HOURS = (3, 6, 9, 12, 15, 18, 21)
SERVICE = 'sfz-eventspy-season.service'
TIMER = 'sfz-eventspy-season.timer'
RUNNER = Path('/usr/local/sbin/sfz-eventspy-season-collect')
RUNNER_SHA = '3678afd5ea86d87f643488585d216999983246023cdf400e092045c7851d3973'
IMAGE = 'seahawksfanzone-eventspy-season:1'
IMAGE_ID = 'sha256:041d5d6b79e9e99faeabecaf8dbe72b342a450dc9dcb8328c55c479721daa019'
NAME = re.compile(r'^[0-9a-f]{64}\.json$')
BUSY = {'active', 'activating', 'deactivating', 'reloading'}


def iso(value):
    return value.astimezone(UTC).isoformat()


def parse_utc(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('slot must be a UTC timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError('slot must include a UTC offset')
    return result


class Host:
    def command(self, *args, check=True, timeout=30):
        return subprocess.run(args, text=True, capture_output=True,
                              check=check, timeout=timeout)

    def service(self):
        result = self.command('systemctl', 'show', SERVICE,
                              '--property=LoadState,ActiveState,Result,ExecMainStatus,InvocationID')
        return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)

    def timer(self):
        enabled = self.command('systemctl', 'is-enabled', TIMER, check=False).stdout.strip()
        active = self.command('systemctl', 'is-active', TIMER, check=False).stdout.strip()
        return enabled, active

    def fingerprints(self):
        self.verify_configuration()
        runner_sha = hashlib.sha256(RUNNER.read_bytes()).hexdigest()
        image_id = self.command('docker', 'image', 'inspect', IMAGE,
                                '--format', '{{.Id}}').stdout.strip()
        return runner_sha, image_id

    @staticmethod
    def validate_unit_properties(props, persistent):
        hooks = ('ExecStartPre', 'ExecStartPost', 'ExecCondition', 'ExecStop', 'ExecStopPost')
        required = {'ExecStart', 'Type', 'Restart', 'RemainAfterExit'}
        if not required.issubset(props):
            raise ValueError('collector unit configuration could not be verified')
        if props['Type'] != 'oneshot' or props['Restart'] != 'no' or props['RemainAfterExit'] != 'no':
            raise ValueError('collector unit must remain a oneshot without automatic retries')
        pattern = (r'^\{\s*path=' + re.escape(str(RUNNER)) +
                   r'\s*;\s*argv\[\]=' + re.escape(str(RUNNER)) +
                   r'\s*;\s*ignore_errors=no\s*;[^{}]*\}$')
        if not re.fullmatch(pattern, props['ExecStart']) or any(props.get(key, '') for key in hooks):
            raise ValueError('collector unit no longer executes only the fixed preserved runner')
        if persistent != 'no':
            raise ValueError('legacy timer must retain Persistent=false before restoring ownership')

    def verify_configuration(self):
        result = self.command('systemctl', 'show', SERVICE, '--all',
                              '--property=ExecStart,ExecStartPre,ExecStartPost,ExecCondition,ExecStop,ExecStopPost,Type,Restart,RemainAfterExit')
        props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        persistent = self.command('systemctl', 'show', TIMER,
                                  '--property=Persistent', '--value').stdout.strip()
        self.validate_unit_properties(props, persistent)

    def start(self, previous_invocation):
        """Observe a fresh invocation while systemctl waits for the oneshot unit.

        A timeout terminates the waiting systemctl CLIENT, never the host unit.
        The bridge leaves its ledger reservation unresolved on any interruption.
        """
        proc = subprocess.Popen(['systemctl', 'start', SERVICE],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = clock_time.monotonic() + 3600
        invocation = None
        try:
            while True:
                props = self.service()
                candidate = props.get('InvocationID', '')
                if re.fullmatch(r'[0-9a-f]{32}', candidate) and candidate != previous_invocation:
                    invocation = candidate
                code = proc.poll()
                if code is not None:
                    return code, invocation
                if clock_time.monotonic() >= deadline:
                    raise TimeoutError('collector wait exceeded one hour')
                clock_time.sleep(0.2)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    def summary(self, invocation):
        if not invocation:
            return None
        self.command('journalctl', '--sync')
        result = self.command('journalctl', '--no-pager', '-o', 'cat',
                              '-u', SERVICE, f'_SYSTEMD_INVOCATION_ID={invocation}',
                              '-n', '1000')
        for line in reversed(result.stdout.splitlines()):
            # The preserved collector writes one JSON object per line.
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and value.get('outcome') == 'EVENTSPY_SEASON_SUCCESS':
                return value
        return None

    def restore(self):
        self.command('systemctl', 'enable', '--now', TIMER)


class Bridge:
    def __init__(self, root=ROOT, host=None, clock=None):
        self.root = Path(root)
        self.host = host or Host()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.state = self.root / 'state'
        self.db_path = self.state / 'ledger.sqlite3'

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.state / 'bridge.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def db(self, create=False):
        mode = 'rwc' if create else 'rw'
        conn = sqlite3.connect(f'{self.db_path.as_uri()}?mode={mode}', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def init(self):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        with self.lock(), self.db(create=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS control (
                  id INTEGER PRIMARY KEY CHECK(id=1), armed INTEGER NOT NULL DEFAULT 0,
                  test_date TEXT, armed_at TEXT, expires_at TEXT);
                INSERT OR IGNORE INTO control(id, armed) VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS days (
                  day TEXT PRIMARY KEY, prior_attempts INTEGER NOT NULL CHECK(prior_attempts>=0));
                CREATE TABLE IF NOT EXISTS attempts (
                  slot TEXT PRIMARY KEY, day TEXT NOT NULL, state TEXT NOT NULL,
                  reserved_at TEXT NOT NULL, invocation_id TEXT, message TEXT);
            ''')
            db.commit()
        os.chmod(self.db_path, 0o600)
        return self.status()

    def view(self, db):
        control = dict(db.execute('SELECT * FROM control WHERE id=1').fetchone())
        control.pop('id')
        control['armed'] = bool(control['armed'])
        day = control['test_date'] or self.clock().astimezone(LA).date().isoformat()
        row = db.execute('SELECT prior_attempts FROM days WHERE day=?', (day,)).fetchone()
        prior = row[0] if row else 0
        count = db.execute('SELECT count(*) FROM attempts WHERE day=?', (day,)).fetchone()[0]
        pending = db.execute("SELECT count(*) FROM attempts WHERE state='pending'").fetchone()[0]
        now = self.clock()
        next_slot = self.next_slot(now) if day == now.astimezone(LA).date().isoformat() else None
        return dict(control, prior_attempts=prior, bridge_attempts=count,
                    total_attempts=prior + count, unresolved_attempts=pending,
                    next_slot=iso(next_slot) if next_slot else None)

    def status(self):
        with self.db() as db:
            return self.view(db)

    @staticmethod
    def next_slot(now):
        local = now.astimezone(LA)
        candidates = [datetime.combine(local.date(), time(hour=h), LA) for h in HOURS]
        return next((value for value in candidates if value > local), None)

    def ensure_idle(self):
        props = self.host.service()
        if props.get('LoadState') != 'loaded':
            raise ValueError('collector service is not loaded')
        if props.get('ActiveState') not in {'inactive', 'failed'}:
            raise ValueError('collector service is busy or its state is unknown; let it finish')
        return props

    def ensure_ownership(self):
        if self.host.timer() != ('disabled', 'inactive'):
            raise ValueError('legacy collector timer must be disabled and inactive')
        self.ensure_idle()
        if self.host.fingerprints() != (RUNNER_SHA, IMAGE_ID):
            raise ValueError('working collector fingerprint differs from the preserved runbook')

    def arm(self, prior):
        if type(prior) is not int or not 0 <= prior <= 6:
            raise ValueError('prior attempts must be an integer from 0 through 6')
        with self.lock(), self.db() as db:
            now = self.clock()
            local = now.astimezone(LA)
            if self.next_slot(now) is None:
                raise ValueError('no original collection slots remain today in Seattle')
            self.ensure_ownership()
            if db.execute("SELECT 1 FROM attempts WHERE state='pending'").fetchone():
                raise ValueError('an unresolved source attempt requires investigation')
            day = local.date().isoformat()
            old = db.execute('SELECT prior_attempts FROM days WHERE day=?', (day,)).fetchone()
            prior = max(prior, old[0] if old else 0)
            used = db.execute('SELECT count(*) FROM attempts WHERE day=?', (day,)).fetchone()[0]
            if prior + used >= 7:
                raise ValueError('the seven-attempt allowance has already been consumed')
            expiry = datetime.combine(local.date() + timedelta(days=1), time.min, LA)
            with db:
                db.execute('INSERT INTO days(day,prior_attempts) VALUES (?,?) '
                           'ON CONFLICT(day) DO UPDATE SET prior_attempts=excluded.prior_attempts', (day, prior))
                db.execute('UPDATE control SET armed=1,test_date=?,armed_at=?,expires_at=? WHERE id=1',
                           (day, iso(now), iso(expiry)))
            return self.view(db)

    def restore(self):
        with self.lock(), self.db() as db:
            with db:
                db.execute('UPDATE control SET armed=0 WHERE id=1')
            self.ensure_idle()  # Never interrupt a running collector.
            self.host.verify_configuration()
            self.host.restore()
            return dict(self.view(db), status='success', message='Legacy timer restored; trial gateway disarmed.')

    def handle(self, body, db):
        operation = body['operation']
        if operation == 'health':
            return dict(self.view(db), status='success', message='Bridge queue and state are readable; no collection requested.')
        slot = parse_utc(body.get('slot'))
        slot_key = iso(slot)
        prior = db.execute('SELECT * FROM attempts WHERE slot=?', (slot_key,)).fetchone()
        if prior:
            return dict(status='skipped', message='This slot was already reserved; no source rerun.',
                        slot=slot_key, previous_state=prior['state'], invocation_id=prior['invocation_id'])
        state = self.view(db)
        now = self.clock()
        local_slot = slot.astimezone(LA)
        if not state['armed'] or state['test_date'] != now.astimezone(LA).date().isoformat():
            return dict(status='skipped', message='The one-day collector trial is not armed for today.')
        if local_slot.date().isoformat() != state['test_date']:
            return dict(status='skipped', message='Slot is outside the armed Seattle date.')
        if local_slot.hour not in HOURS or local_slot.minute or local_slot.second or local_slot.microsecond:
            return dict(status='skipped', message='Slot is not one of the seven original schedule slots.')
        if slot < parse_utc(state['armed_at']) or not 0 <= (now - slot).total_seconds() <= 600:
            return dict(status='skipped', message='Slot is before activation, in the future, or more than ten minutes late.')
        if state['unresolved_attempts']:
            return dict(status='failed', message='An earlier source attempt is unresolved; no new collection admitted.')
        if state['total_attempts'] >= 7:
            return dict(status='skipped', message='Seven daily attempts are already reserved or consumed.')
        self.ensure_ownership()
        before = self.host.service().get('InvocationID', '')
        with db:
            db.execute("INSERT INTO attempts(slot,day,state,reserved_at) VALUES (?,?,'pending',?)",
                       (slot_key, state['test_date'], iso(now)))
        # The committed reservation survives process death. Never refund it.
        try:
            returncode, invocation = self.host.start(before)
        except Exception:
            return dict(status='failed', message='Collector wait was interrupted; reservation remains unresolved. Do not retry the source.', slot=slot_key)
        props = self.host.service()
        summary = self.host.summary(invocation)
        expected = {'outcome': 'EVENTSPY_SEASON_SUCCESS', 'authorized': 16,
                    'succeeded': 16, 'failed': 0, 'unavailable': 1}
        success = (returncode == 0 and invocation and invocation != before and
                   props.get('ActiveState') == 'inactive' and props.get('Result') == 'success' and
                   props.get('ExecMainStatus') == '0' and isinstance(summary, dict) and
                   all(summary.get(key) == value for key, value in expected.items()))
        if props.get('ActiveState') in BUSY:
            return dict(status='failed', message='Collector is still active; reservation remains unresolved.', slot=slot_key)
        result = 'success' if success else 'failed'
        message = ('Verified fresh collector invocation and 16 successful authorized games.' if success else
                   'Collector completion or expected 16-game summary did not verify; attempt remains consumed.')
        with db:
            db.execute('UPDATE attempts SET state=?,invocation_id=?,message=? WHERE slot=?',
                       (result, invocation, message, slot_key))
        return dict(status=result, message=message, slot=slot_key, invocation_id=invocation,
                    attempt_count=state['total_attempts'] + 1, summary=expected if success else None)

    @staticmethod
    def validate(body, filename):
        if not isinstance(body, dict) or type(body.get('version')) is not int or body['version'] != 1:
            raise ValueError('invalid request version')
        if body.get('request_id') != filename[:-5] or body.get('operation') not in {'health', 'collect'}:
            raise ValueError('invalid request identity or operation')
        if set(body) - {'version', 'operation', 'request_id', 'slot'}:
            raise ValueError('unsupported request fields')
        if body['operation'] == 'collect':
            parse_utc(body.get('slot'))

    def respond(self, name, value):
        destination = self.root / 'responses' / name
        tmp = destination.parent / ('.response-' + uuid.uuid4().hex)
        data = json.dumps(dict(value, request_id=name[:-5]), sort_keys=True).encode() + b'\n'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o644)
            # Preserve a durable receipt even if an exact request is resubmitted.
            try:
                os.link(tmp, destination, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            tmp.unlink(missing_ok=True)

    def process(self):
        with self.lock(), self.db() as db:
            directory = self.root / 'requests'
            quarantine = self.state / 'quarantine'
            quarantine.mkdir(mode=0o700, exist_ok=True)
            # DirectoryNotEmpty ignores dotfiles. Claim all other names, including
            # malformed names and directories, so poison entries cannot retrigger
            # the path unit forever. No recursive traversal is performed.
            with os.scandir(directory) as entries:
                names = []
                for entry in entries:
                    if not entry.name.startswith('.'):
                        names.append(entry.name)
                    if len(names) >= 100:
                        break
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            quarantine_fd = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for name in names:
                    claimed = uuid.uuid4().hex + '.entry'
                    try:
                        os.rename(name, claimed, src_dir_fd=directory_fd, dst_dir_fd=quarantine_fd)
                    except FileNotFoundError:
                        continue
                    if not NAME.fullmatch(name):
                        continue  # Retain invalid names privately for inspection.
                    if os.path.lexists(self.root / 'responses' / name):
                        # Do not change a completed result for an exact replay.
                        if not stat.S_ISDIR(os.stat(claimed, dir_fd=quarantine_fd, follow_symlinks=False).st_mode):
                            os.unlink(claimed, dir_fd=quarantine_fd)
                        continue
                    retain = True
                    try:
                        fd = os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=quarantine_fd)
                        with os.fdopen(fd, 'rb') as stream:
                            meta = os.fstat(stream.fileno())
                            if not stat.S_ISREG(meta.st_mode) or meta.st_size > 8192:
                                raise ValueError('request must be a small regular file')
                            raw = stream.read(8193)
                            if len(raw) > 8192:
                                raise ValueError('request exceeds size limit')
                        body = json.loads(raw)
                        self.validate(body, name)
                        value = self.handle(body, db)
                        retain = False
                    except Exception as error:
                        # Avoid returning request data or command output to the queue owner.
                        value = dict(status='failed', message=f'Bridge request rejected or failed ({type(error).__name__}). Inspect host service/state before retrying.')
                    self.respond(name, value)
                    if not retain:
                        os.unlink(claimed, dir_fd=quarantine_fd)
            finally:
                os.close(directory_fd)
                os.close(quarantine_fd)
        return {'status': 'success', 'processed': len(names)}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'status', 'process', 'arm', 'restore'))
    parser.add_argument('--prior-attempts', type=int)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('this installed host command requires sudo/root')
    bridge = Bridge()
    try:
        result = bridge.arm(args.prior_attempts) if args.action == 'arm' else getattr(bridge, args.action)()
    except Exception as error:
        print(json.dumps({'status': 'failed', 'message': str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
