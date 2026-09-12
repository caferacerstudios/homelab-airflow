#!/usr/bin/env python3
"""Check/install only the missing-provider-link collector fix on the existing host."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
INSTALL = Path("/opt/fanzone-eventspy")
STATE = Path("/var/lib/fanzone-eventspy")
BASELINE_SHA256 = "4adee6828b111ee5b8255aace9f883126797002075a77477486b2c99e90491e8"
IMAGE_ID = "sha256:041d5d6b79e9e99faeabecaf8dbe72b342a450dc9dcb8328c55c479721daa019"
CACHE_RELATIVE = "cache/2026-09-12/374562.json"
OWNER_UID = 0

OFFLINE_CHECK = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildSnapshot } from '/review/collector.mjs';
import { bindCoverageToSchedule, collectionDecision } from '/review/collector-coverage.mjs';
const read = path => JSON.parse(readFileSync(path, 'utf8'));
const slot = '2026-09-12T16:00:00.000Z';
const site = { slug: 'chiefs', city: 'Kansas City', name: 'Chiefs', abbreviation: 'KC', timezone: 'America/Los_Angeles' };
try {
  const bindings = bindCoverageToSchedule(site, read('/inputs/coverage.json'), read('/inputs/schedule.json'));
  const matches = bindings.filter(item => item.row.sourceEventId === '374562');
  assert.equal(matches.length, 1);
  const binding = matches[0];
  assert.equal(binding.reason, null);
  assert.ok(binding.game && binding.row.gameId);
  assert.equal(collectionDecision(binding, Date.parse(slot)).kind, 'collect');
  const payload = read('/inputs/cache.json').attempts[slot].payload;
  const snapshot = buildSnapshot(binding.row, payload, Date.parse(slot), site);
  assert.equal(snapshot.schemaVersion, '1.1.0');
  assert.equal(snapshot.providerLinks.seatgeek, null);
  const providers = ['ticketmaster', 'stubhub', 'vividseats'];
  for (const market of providers) assert.ok(snapshot.providerLinks[market]);
  assert.equal(snapshot.summary.currentLowestCents, Math.round(Number(payload.event.currentPrice) * 100));
  assert.equal(snapshot.summary.sevenDayLowestCents, Math.round(Number(payload.event.sevenDayLowest) * 100));
  assert.ok(snapshot.history.length >= 2);
  console.log(JSON.stringify({ status: 'ready', gameId: snapshot.gameId, schemaVersion: snapshot.schemaVersion,
    providers, missingProviders: ['seatgeek'], historyPoints: snapshot.history.length }));
} catch {
  console.error('Cached Chiefs payload did not pass identity, provider-link, price/history validation; no update applied.');
  process.exitCode = 1;
}
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def checked(path, *, directory=False, root_owned=True, limit=50_000_000):
    """Reject redirected/private-state inputs; never repair permissions in place."""
    for part in (path, *path.parents):
        require(not part.is_symlink(), f"Symlink is not supported: {part}")
    details = path.stat()
    require(stat.S_ISDIR(details.st_mode) if directory else stat.S_ISREG(details.st_mode),
            f"Unexpected file type: {path}")
    if root_owned:
        require(details.st_uid == OWNER_UID and not details.st_mode & 0o022,
                f"Expected root-owned, protected installed file/directory: {path}")
    if not directory:
        require(details.st_size <= limit, f"Input is too large: {path}")
    return details


@contextlib.contextmanager
def idle_locks():
    descriptors = []
    try:
        # Reuse existing locks, so --check does not even create a lock file.
        for path in (STATE / "install.lock", STATE / "state/bridge.lock"):
            checked(path)
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            descriptors.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("An installer or ticket collection is running; let it finish and rerun.") from exc
        ledger = STATE / "state/ledger.sqlite3"
        checked(ledger)
        with contextlib.closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as db:
            require(db.execute("SELECT count(*) FROM attempts WHERE state='pending'").fetchone()[0] == 0,
                    "An unresolved ticket collection remains in the ledger; inspect it before updating.")
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def validate_offline():
    mounts = [(HERE / "collector.mjs", "/review/collector.mjs"),
              (INSTALL / "collector-coverage.mjs", "/review/collector-coverage.mjs"),
              (INSTALL / "coverage/chiefs.json", "/inputs/coverage.json"),
              (STATE / "schedules/chiefs.json", "/inputs/schedule.json"),
              (STATE / CACHE_RELATIVE, "/inputs/cache.json")]
    for source, _ in mounts:
        checked(source, root_owned=source not in {HERE / "collector.mjs", STATE / CACHE_RELATIVE})
    args = ["docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only",
            "--cap-drop=ALL", "--security-opt", "no-new-privileges:true", "--entrypoint", "node"]
    for source, target in mounts:
        args.extend(["--mount", f"type=bind,src={source},dst={target},readonly"])
    result = subprocess.run(args + [IMAGE_ID, "--input-type=module", "-e", OFFLINE_CHECK],
                            text=True, capture_output=True, timeout=45)
    require(result.returncode == 0,
            "Offline cached Chiefs validation failed. No collector was installed; no source request was made.")
    # Only allow the explicitly safe summary to reach the terminal.
    summary = json.loads(result.stdout)
    require(summary.get("status") == "ready" and summary.get("schemaVersion") == "1.1.0",
            "Offline checker did not return its expected result")
    print(json.dumps({key: summary[key] for key in
                     ("status", "gameId", "schemaVersion", "providers", "missingProviders", "historyPoints")}))


def update(*, install=False):
    require(os.geteuid() == 0, "Run with sudo python3 -B deployment/eventspy/update_collector.py --check or --install")
    for path in (INSTALL, STATE, STATE / "state", STATE / "install-backups"):
        checked(path, directory=True)
    checked(INSTALL / "settings.json")
    settings = json.loads((INSTALL / "settings.json").read_bytes())
    require(settings.get("version") == 2 and settings.get("image_id") == IMAGE_ID,
            "The installed bridge/image differs from the reviewed deployment; kept unchanged")
    target = INSTALL / "collector.mjs"
    candidate = HERE / "collector.mjs"
    checked(candidate, root_owned=False)
    content = candidate.read_bytes()
    candidate_hash = digest(content)
    with idle_locks():
        meta = checked(target)
        previous = target.read_bytes()
        require(digest(previous) in {BASELINE_SHA256, candidate_hash},
                "Installed collector has unrecognized local changes; it was not overwritten")
        validate_offline()
        require(candidate.read_bytes() == content, "Candidate changed during validation; rerun")
        require(target.read_bytes() == previous, "Installed collector changed during validation; rerun")
        if not install:
            print("Check passed. No installed files, ticket data, queue, ledger, or services changed.")
            return
        if previous == content:
            print("Collector fix is already installed. No files changed.")
            return
        backup = STATE / "install-backups" / f"collector-before-missing-links-{digest(previous)}.mjs"
        if backup.exists():
            checked(backup)
            require(backup.read_bytes() == previous, "Existing collector backup differs; kept unchanged")
        else:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(previous)
                stream.flush()
                os.fsync(stream.fileno())
        fd, temporary = tempfile.mkstemp(prefix=".collector-update-", dir=INSTALL)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                os.fchown(stream.fileno(), meta.st_uid, meta.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(meta.st_mode))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print(f"Installed collector fix. Backup: {backup}")
        print("The next eligible scheduled slot uses the fix. Existing receipts and snapshots are preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--install", action="store_true")
    args = parser.parse_args()
    try:
        update(install=args.install)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Collector update stopped: {exc}\n")


if __name__ == "__main__":
    main()
