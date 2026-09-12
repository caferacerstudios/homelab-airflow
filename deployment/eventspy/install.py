#!/usr/bin/env python3
"""Install the team ticket bridge without running a collection.

Run --check first. The outer repository installer pauses the Airflow DAG and
checks running tasks; this host installer independently checks the host workers,
seeds genuine schedules, and transfers the existing queue watcher only when idle.
The original collector image, runner, service, and Seattle JSON are never edited.
"""
import argparse
import base64
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
IMAGE = "seahawksfanzone-eventspy-season:1"
IMAGE_ID = "sha256:041d5d6b79e9e99faeabecaf8dbe72b342a450dc9dcb8328c55c479721daa019"
RUNNER_SHA = "3678afd5ea86d87f643488585d216999983246023cdf400e092045c7851d3973"
TIMER = "sfz-eventspy-season.timer"
COLLECTOR_SERVICE = "sfz-eventspy-season.service"
WATCHER = "sfz-airflow-ticket-test.path"
BRIDGE_SERVICE = "sfz-airflow-ticket-test.service"
RESTORE_TIMER = "sfz-airflow-ticket-test-restore.timer"
RESTORE_SERVICE = "sfz-airflow-ticket-test-restore.service"
IDLE = {"inactive", "failed"}
SCHEDULE_CHECK = """
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
const [digest, rawSite, coveragePath, schedulePath, helperPath] = process.argv.slice(1);
const source = readFileSync(schedulePath);
if (createHash('sha256').update(source).digest('hex') !== digest) throw new Error('Seattle snapshot changed during validation; retry');
const { bindCoverageToSchedule } = await import(pathToFileURL(helperPath));
const bindings = bindCoverageToSchedule(JSON.parse(rawSite), JSON.parse(readFileSync(coveragePath, 'utf8')), JSON.parse(source));
if (bindings.length !== 17 || bindings.some(binding => !binding.game || binding.reason)) throw new Error('Seattle schedule does not bind all 17 reviewed games');
console.log('Seattle schedule matches all 17 reviewed game identities');
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def no_symlinks(path):
    path = Path(path)
    for part in (path, *path.parents):
        require(not part.is_symlink(), f"Refusing symlink: {part}")


def read_file(path, limit=2_000_000):
    no_symlinks(path)
    require(path.is_file(), f"Missing regular file: {path}")
    require(path.stat().st_size <= limit, f"File is too large: {path}")
    return path.read_bytes()


def atomic_write(path, content, mode=0o644):
    no_symlinks(path)
    fd, temporary = tempfile.mkstemp(prefix=".eventspy-install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def checked_directory(path, mode=0o755, uid=0):
    """Create owned directories; never repair or recursively chown existing data."""
    no_symlinks(path)
    if path.exists():
        details = path.stat()
        require(stat.S_ISDIR(details.st_mode), f"Not a directory: {path}")
        require(details.st_uid == uid, f"Unexpected owner for {path}; kept unchanged")
        require(not details.st_mode & 0o022, f"Directory is writable by another account: {path}")
        return
    path.mkdir(mode=mode)
    os.chown(path, uid, 0 if uid == 0 else uid)
    path.chmod(mode)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Host:
    def run(self, args):
        result = subprocess.run(args, text=True, capture_output=True, timeout=180)
        if result.returncode:
            # Docker/env diagnostics can contain credentials. Do not echo stderr.
            raise RuntimeError(f"Command failed (exit {result.returncode}): {args[0]} {args[1]}")
        return result.stdout

    def unit(self, name):
        output = self.run(["systemctl", "show", name,
                           "--property=LoadState,ActiveState,UnitFileState,FragmentPath,DropInPaths"])
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    def command(self, *args):
        return self.run(["systemctl", *args])


class Installer:
    def __init__(self, *, host=None, here=HERE, prefix=Path("/")):
        self.host = host or Host()
        self.here = Path(here)
        self.prefix = Path(prefix)
        self.code = self.path("/opt/fanzone-eventspy")
        self.state = self.path("/var/lib/fanzone-eventspy")
        self.queue = self.path("/var/lib/sfz-airflow-ticket-test")
        self.unit_file = self.path("/etc/systemd/system/" + BRIDGE_SERVICE)
        self.settings = self.code / "settings.json"
        self.backup = self.state / "install-backups" / "original.json"

    def path(self, value):
        return self.prefix / value.lstrip("/")

    def busy_checks(self):
        for name in (COLLECTOR_SERVICE, BRIDGE_SERVICE, RESTORE_SERVICE):
            props = self.host.unit(name)
            if name == RESTORE_SERVICE and props.get("LoadState") == "not-found":
                continue
            require(props.get("LoadState") == "loaded", f"Expected loaded unit: {name}")
            require(props.get("ActiveState") in IDLE,
                    f"{name} is busy; let it finish, then rerun. It was not stopped.")
        requests = self.queue / "requests"
        no_symlinks(requests)
        require(requests.is_dir(), "The existing Airflow request queue is missing")
        require(not any(requests.iterdir()), "Airflow requests are queued; let them finish before installing")

    def payload(self):
        files = {}
        for name in ("bridge.py", "schedules.py", "collector.mjs", "collector-coverage.mjs"):
            files[self.code / name] = read_file(self.here / name)
        files[self.code / "fan_zone_config.py"] = read_file(self.here.parent.parent / "dags/fan_zone_config.py")
        for path in sorted((self.here / "coverage").glob("*.json")):
            content = read_file(path)
            require(isinstance(json.loads(content), list), f"Coverage must be an array: {path.name}")
            files[self.code / "coverage" / path.name] = content
        for path, content in files.items():
            if path.suffix == ".py":
                compile(content, str(path), "exec")
        return files

    def sites_from(self, filename):
        module = load_module("fan_zone_config", self.here.parent.parent / "dags/fan_zone_config.py")
        sites = module.validate_sites(json.loads(read_file(Path(filename))))
        active = [site for site in sites.values() if site["enabled"] and "eventspy" in site]
        require(active, "No enabled site has EventSpy configuration")
        for site in active:
            filename = site["eventspy"]["coverage_file"]
            coverage = json.loads(read_file(self.here / "coverage" / filename))
            require(isinstance(coverage, list) and coverage, f"Missing coverage for {site['slug']}")
        return active

    def original_settings(self):
        if not self.settings.exists():
            return None
        value = json.loads(read_file(self.settings))
        require(value.get("version") == 2 and value.get("image_id") == IMAGE_ID,
                "Existing modular bridge settings are incompatible; kept unchanged")
        require(self.settings.stat().st_uid == 0 and not self.settings.stat().st_mode & 0o077,
                "Existing bridge settings must be root-owned and private")
        activation = datetime.fromisoformat(value["activated_at"].replace("Z", "+00:00"))
        require(activation.tzinfo is not None, "Existing activation must include a timezone")
        require(self.backup.is_file(), "Existing bridge has no original handoff backup")
        return value

    def preflight(self, sites_file):
        self.busy_checks()
        for unit in (TIMER, WATCHER):
            props = self.host.unit(unit)
            require(props.get("LoadState") == "loaded", f"Expected loaded unit: {unit}")
            require(props.get("UnitFileState") in {"enabled", "disabled", "enabled-runtime"},
                    f"Unexpected enable state for {unit}; kept unchanged")
        restore = self.host.unit(RESTORE_TIMER)
        require(restore.get("LoadState") == "not-found" or (restore.get("LoadState") == "loaded"
                and restore.get("UnitFileState") in {"enabled", "disabled", "enabled-runtime"}),
                "Unexpected legacy restore timer configuration; kept unchanged")
        props = self.host.unit(BRIDGE_SERVICE)
        require(props.get("FragmentPath") == str(self.unit_file), "Queue worker unit is at an unexpected path")
        require(not props.get("DropInPaths"), "Queue worker has local overrides; review them before changing its command")
        content = read_file(self.unit_file)
        current = self.original_settings()
        self.updated_unit(content, already_installed=current is not None)
        runner = read_file(self.path("/usr/local/sbin/sfz-eventspy-season-collect"))
        require(hashlib.sha256(runner).hexdigest() == RUNNER_SHA,
                "Original Seattle runner differs from the reviewed deployment; kept unchanged")
        actual = self.host.run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"])
        require(actual.strip() == IMAGE_ID, "Collector image differs from the reviewed image; no image was pulled")
        if current is None:
            previous = self.path("/opt/sfz-airflow-ticket-test/bridge.py")
            read_file(previous)
            status = json.loads(self.host.run(["/usr/bin/python3", str(previous), "status"]))
            require(status.get("armed") is False and status.get("unresolved_attempts") == 0,
                    "The temporary Airflow trial is armed or unresolved; resolve it before handoff")
        else:
            self.modular_idle()
            for name in (TIMER, RESTORE_TIMER):
                props = self.host.unit(name)
                if props.get("LoadState") != "not-found":
                    require(props.get("ActiveState") in IDLE and props.get("UnitFileState") == "disabled",
                            "A legacy timer was reactivated after handoff; review ownership before reinstalling")
        seattle = self.path("/var/lib/sfz-eventspy-mirror/dev/public")
        no_symlinks(seattle)
        require(seattle.is_dir(), "Existing Seattle ticket output is missing")
        details = seattle.stat()
        require(details.st_uid == 1000 and details.st_mode & stat.S_IWUSR,
                "Existing Seattle output must remain writable by collector UID 1000")
        files = self.payload()
        active = self.sites_from(sites_file)
        for site in active:
            output = self.path(site["eventspy"]["output_dir"])
            no_symlinks(output)
            if output.exists():
                require(output.is_dir() and output.stat().st_uid == 1000 and output.stat().st_mode & stat.S_IWUSR,
                        f"Unexpected owner or access for {output}; kept unchanged")
        for site in active:
            if site["slug"] == "seahawks":
                self.check_seattle_schedule(site)
        for name in ("collector.mjs", "collector-coverage.mjs"):
            self.host.run(["docker", "run", "--rm", "--pull=never", "--network", "none",
                           "--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges:true",
                           "--entrypoint", "node", "--mount",
                           f"type=bind,src={self.here},dst=/review,readonly",
                           IMAGE_ID, "--check", f"/review/{name}"])
        return active, files, current

    def check_seattle_schedule(self, site):
        # Resolve current once: the NFL publisher switches a symlink atomically.
        root = self.path("/var/lib/sfz-nfl").resolve(strict=True)
        schedule = self.path(site["eventspy"]["schedule_file"]).resolve(strict=True)
        require(schedule.is_relative_to(root) and schedule.is_file(),
                "Seattle schedule must resolve to a regular file inside /var/lib/sfz-nfl")
        source = read_file(schedule, limit=50_000_000)
        digest = hashlib.sha256(source).hexdigest()
        payload = json.loads(source)
        require(isinstance(payload, dict) and payload.get("fixture") is False,
                "Seattle schedule must be a non-fixture NFL snapshot")
        require(isinstance(payload.get("team"), dict) and payload["team"].get("abbreviation") == "SEA",
                "Seattle NFL snapshot identifies a different team")
        coverage_file = site["eventspy"]["coverage_file"]
        coverage = json.loads(read_file(self.here / "coverage" / coverage_file))
        games = payload.get("gamesRegular") or payload.get("games")
        require(isinstance(games, list) and all(isinstance(row, dict) for row in games),
                "Seattle schedule must contain a regular-season game array")
        # The existing NFL normalizer includes a synthetic bye in gamesRegular.
        # Count actual games here; the unchanged binder below verifies all 17 IDs.
        regular_games = [row for row in games if row.get("bye") is not True and row.get("state") != "bye"]
        require(len(regular_games) == 17 and len(coverage) == 17,
                f"Seattle schedule has {len(regular_games)} game rows and {len(games) - len(regular_games)} bye rows; "
                f"expected 17 games matching {len(coverage)} reviewed coverage rows")
        require({row.get("season") for row in coverage} == {payload.get("season")},
                "Seattle snapshot season differs from reviewed coverage")
        manifest_path = schedule.parent / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(read_file(manifest_path))
            require(isinstance(manifest, dict) and manifest.get("schema_version") == 1
                    and isinstance(manifest.get("files"), dict)
                    and manifest["files"].get("seahawks.json") == digest,
                    "Seattle NFL snapshot manifest checksum does not match seahawks.json")
        # Reuse the collector's real game-ID/team/year/week binder, without a browser,
        # API request or data write. The digest also detects an in-place mutation.
        self.host.run(["docker", "run", "--rm", "--pull=never", "--network", "none",
                       "--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges:true",
                       "--entrypoint", "node", "--mount",
                       f"type=bind,src={self.here},dst=/review,readonly", "--mount",
                       f"type=bind,src={schedule},dst=/run/eventspy/schedule.json,readonly",
                       IMAGE_ID, "--input-type=module", "-e", SCHEDULE_CHECK, "--",
                       digest, json.dumps(site), f"/review/coverage/{coverage_file}",
                       "/run/eventspy/schedule.json", "/review/collector-coverage.mjs"])

    @staticmethod
    def updated_unit(content, already_installed=False):
        text = content.decode()
        old = "ExecStart=/usr/bin/python3 /opt/sfz-airflow-ticket-test/bridge.py process"
        new = "ExecStart=/usr/bin/python3 /opt/fanzone-eventspy/bridge.py process"
        expected = new if already_installed else old
        require(text.count(expected) == 1 and sum(line.strip().startswith("ExecStart=") for line in text.splitlines()) == 1,
                "Queue service ExecStart differs from the reviewed command; kept unchanged")
        require("TimeoutStartSec=70min" in text and "NoNewPrivileges=true" in text,
                "Queue service is missing existing timeout/hardening; kept unchanged")
        expected_home = "ProtectHome=read-only" if already_installed else "ProtectHome=true"
        require(text.count(expected_home) == 1, "Queue service ProtectHome differs; kept unchanged")
        return text.replace(expected, new).replace(expected_home, "ProtectHome=read-only").encode()

    def modular_idle(self):
        read_file(self.state / "state/ledger.sqlite3", limit=50_000_000)
        status = json.loads(self.host.run(["/usr/bin/python3", str(self.code / "bridge.py"), "status"]))
        require(status.get("version") == 2 and status.get("unresolved_attempts") == 0,
                "A modular collection is unresolved; inspect its ledger/container before changing ownership")

    def seed_schedules(self, sites):
        # Schedule retrieval uses the existing NFL credential and never contacts EventSpy.
        module = load_module("schedules", self.here / "schedules.py")
        for site in sites:
            coverage = json.loads(read_file(self.here / "coverage" / site["eventspy"]["coverage_file"]))
            module.ensure_schedule(site, coverage, force_refresh=True, allow_stale=False)

    def prepare_directories(self, sites):
        checked_directory(self.state)
        for name, mode, uid in (("state", 0o700, 0), ("cache", 0o755, 1000),
                                ("jobs", 0o755, 0), ("schedules", 0o755, 0),
                                ("install-backups", 0o700, 0)):
            checked_directory(self.state / name, mode, uid)
        checked_directory(self.code)
        checked_directory(self.code / "coverage")
        for site in sites:
            output = self.path(site["eventspy"]["output_dir"])
            if site["slug"] == "seahawks":
                continue
            for directory in (output.parent.parent, output.parent, output):
                checked_directory(directory, 0o755, 1000)

    def snapshot(self, paths):
        files = {}
        for path in paths:
            no_symlinks(path)
            files[str(path)] = ({"content": base64.b64encode(read_file(path)).decode(),
                                 "mode": stat.S_IMODE(path.stat().st_mode)} if path.exists() else None)
        return {"version": 1, "files": files,
                "units": {name: self.host.unit(name) for name in (TIMER, WATCHER, RESTORE_TIMER)}}

    def restore_unit_state(self, name, props):
        if props.get("LoadState") == "not-found":
            return
        state = props.get("UnitFileState")
        if state == "enabled":
            self.host.command("enable", name)
        elif state == "enabled-runtime":
            self.host.command("enable", "--runtime", name)
        elif state == "disabled":
            self.host.command("disable", name)
        else:
            raise RuntimeError(f"Cannot restore unknown enable state for {name}: {state}")
        self.host.command("start" if props.get("ActiveState") == "active" else "stop", name)

    def restore_snapshot(self, snapshot):
        for raw, saved in snapshot["files"].items():
            path = Path(raw)
            no_symlinks(path)
            if saved is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, base64.b64decode(saved["content"]), saved["mode"])
        self.host.command("daemon-reload")
        for name in (RESTORE_TIMER, TIMER, WATCHER):
            self.restore_unit_state(name, snapshot["units"][name])

    def install(self, sites_file):
        sites, files, current = self.preflight(sites_file)
        self.prepare_directories(sites)
        # All fetching/validation finishes while the original timer still owns collection.
        self.seed_schedules(sites)
        before = self.snapshot([self.unit_file, self.settings, *files])
        original = self.snapshot([self.unit_file, self.settings])
        previous_backup = read_file(self.backup) if self.backup.exists() else None
        try:
            self.host.command("stop", WATCHER)
            self.busy_checks()  # A request may have arrived immediately before the watch stopped.
            restore = self.host.unit(RESTORE_TIMER)
            if restore.get("LoadState") != "not-found":
                require(restore.get("UnitFileState") in {"enabled", "disabled", "enabled-runtime"},
                        "Unknown legacy restore timer state; kept unchanged")
                self.host.command("disable", "--now", RESTORE_TIMER)
            self.host.command("disable", "--now", TIMER)
            self.busy_checks()  # Never kill a collection that won the race with timer shutdown.
            for path, content in files.items():
                atomic_write(path, content)
            activation = current["activated_at"] if current else datetime.now(timezone.utc).isoformat()
            settings = {"version": 2, "image_id": IMAGE_ID, "activated_at": activation,
                        "coverage_dir": "/opt/fanzone-eventspy/coverage"}
            atomic_write(self.settings, (json.dumps(settings, indent=2) + "\n").encode(), 0o600)
            atomic_write(self.unit_file, self.updated_unit(read_file(self.unit_file), current is not None))
            if previous_backup is None:
                atomic_write(self.backup, (json.dumps(original, indent=2) + "\n").encode(), 0o600)
            self.host.run(["systemd-analyze", "verify", str(self.unit_file)])
            self.host.run(["/usr/bin/python3", str(self.code / "bridge.py"), "init"])
            self.host.command("daemon-reload")
            self.host.command("enable", "--now", WATCHER)
            require(self.host.unit(WATCHER).get("ActiveState") == "active", "The queue watcher did not start")
            require(self.host.unit(TIMER).get("ActiveState") == "inactive", "The original timer did not stop")
        except BaseException:
            # The watcher is quiescent until the final enable; restore all replaced code as well.
            self.host.command("stop", WATCHER)
            self.restore_snapshot(before)
            if previous_backup is None:
                self.backup.unlink(missing_ok=True)
            else:
                atomic_write(self.backup, previous_backup, 0o600)
            raise
        print("Host handoff complete. Airflow owns future slots; no ticket collection was run.")
        print("Seattle output preserved: /var/lib/sfz-eventspy-mirror/dev/public")

    def rollback(self):
        self.busy_checks()
        if self.settings.exists():
            self.modular_idle()
        original = json.loads(read_file(self.backup))
        require(original.get("version") == 1, "Unsupported handoff backup")
        # The root-owned backup is deliberately limited to the two installation files.
        require(set(original.get("files", {})) == {str(self.unit_file), str(self.settings)}, "Invalid rollback file list")
        require(set(original.get("units", {})) == {TIMER, WATCHER, RESTORE_TIMER}, "Invalid rollback unit list")
        before = self.snapshot([self.unit_file, self.settings])
        self.host.command("stop", WATCHER)
        try:
            self.busy_checks()
            self.restore_snapshot(original)
        except BaseException:
            self.restore_snapshot(before)
            raise
        print("Original queue worker and timer states restored. Ticket files/history were retained.")


@contextlib.contextmanager
def install_lock(installer):
    checked_directory(installer.state)
    path = installer.state / "install.lock"
    no_symlinks(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--install", action="store_true")
    actions.add_argument("--rollback", action="store_true")
    parser.add_argument("--sites", type=Path, help="Validated JSON exported from fan_zone_active_sites")
    args = parser.parse_args()
    require(os.geteuid() == 0, "Run this host installer with sudo")
    require(args.rollback or args.sites is not None, "--check/--install requires --sites JSON")
    sys.dont_write_bytecode = True
    installer = Installer()
    if args.check:
        sites, _, _ = installer.preflight(args.sites)
        print("Host checks passed for: " + ", ".join(site["slug"] for site in sites))
        print("No schedules, services, outputs, or system files were changed; no collection was run.")
        return
    with install_lock(installer):
        if args.install:
            installer.install(args.sites)
        else:
            installer.rollback()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"EVENTSPY INSTALL STOPPED: {error}", file=sys.stderr)
        sys.exit(1)
