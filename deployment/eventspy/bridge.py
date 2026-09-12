#!/usr/bin/env python3
"""Restricted same-host bridge for team EventSpy tasks; no arbitrary shell commands."""
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo

# This module is copied beside the installed bridge. The repo uses dags/ instead.
try:
    from fan_zone_config import eventspy_request_id, eventspy_slot, validate_site
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dags"))
    from fan_zone_config import eventspy_request_id, eventspy_slot, validate_site

QUEUE = Path("/var/lib/sfz-airflow-ticket-test")
ROOT = Path("/var/lib/fanzone-eventspy")
INSTALL = Path("/opt/fanzone-eventspy")
LA, UTC = ZoneInfo("America/Los_Angeles"), timezone.utc
HOURS = (3, 6, 9, 12, 15, 18, 21)
NAME = re.compile(r"^[0-9a-f]{64}\.json$")
TIMER = "sfz-eventspy-season.timer"
RESTORE_TIMER = "sfz-airflow-ticket-test-restore.timer"
SERVICE = "sfz-eventspy-season.service"


def parse_utc(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("slot must be a UTC timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("slot must include a UTC offset")
    return parsed


def read_json(path, max_bytes=32768):
    """Queue requests must be regular files, never symlinks or devices."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r") as src:
        meta = os.fstat(src.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > max_bytes:
            raise ValueError("JSON input must be a bounded regular file")
        return json.load(src)


def write_json(path, value, mode=0o644):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x") as out:
            json.dump(value, out, indent=2, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_summary(summary, site, slot, coverage):
    """Skipped completed games replace successes; the 17-row contract stays accounted for."""
    if (not isinstance(summary, dict) or summary.get("team") != site["slug"]
            or summary.get("slot") != slot or summary.get("outcome") not in {
                "EVENTSPY_SEASON_SUCCESS", "EVENTSPY_SEASON_PARTIAL", "EVENTSPY_SEASON_FAILED"}):
        raise ValueError("Collector returned no matching team/slot summary")
    total = len(coverage)
    counts = ("succeeded", "failed", "unavailable", "skipped", "unresolved")
    if any(type(summary.get(key)) is not int or summary[key] < 0 for key in counts):
        raise ValueError("Invalid collector summary counters")
    if sum(summary[key] for key in counts) != total:
        raise ValueError("Collector summary does not account for every reviewed game")
    if not isinstance(summary.get("results"), list) or len(summary["results"]) != total:
        raise ValueError("Collector must report one result per reviewed game")
    counters = {key: 0 for key in counts}
    outcomes = {"EVENTSPY_COLLECTION_SUCCESS": "succeeded", "EVENTSPY_COLLECTION_FAILED": "failed",
                "EVENTSPY_SOURCE_UNAVAILABLE": "unavailable", "EVENTSPY_COLLECTION_SKIPPED": "skipped",
                "EVENTSPY_GAME_UNRESOLVED": "unresolved"}
    seen_weeks, seen_games = set(), set()
    by_week = {row["week"]: row for row in coverage}
    if len(by_week) != total:
        raise ValueError("Reviewed coverage contains duplicate weeks")
    for item in summary["results"]:
        if not isinstance(item, dict) or item.get("team") != site["slug"] or item.get("outcome") not in outcomes:
            raise ValueError("Invalid per-game collector result")
        week = item.get("week")
        if type(week) is not int or week in seen_weeks or week not in by_week:
            raise ValueError("Missing, duplicate or unknown reviewed game in summary")
        seen_weeks.add(week)
        row = by_week[week]
        if item.get("sourceEventId") != row.get("sourceEventId"):
            raise ValueError("Summary source event does not match reviewed game")
        identifier = item.get("gameId")
        if row.get("gameId") is not None and str(identifier) != str(row["gameId"]):
            raise ValueError("Summary game ID differs from reviewed game")
        if identifier is not None:
            if not re.fullmatch(r"[0-9]{1,16}", str(identifier)) or str(identifier) in seen_games:
                raise ValueError("Invalid or duplicate summary game ID")
            seen_games.add(str(identifier))
        if identifier is None and item["outcome"] != "EVENTSPY_GAME_UNRESOLVED":
            raise ValueError("A resolved result requires a genuine game ID")
        counters[outcomes[item["outcome"]]] += 1
    if any(counters[key] != summary[key] for key in counts):
        raise ValueError("Summary counters do not match individual game outcomes")
    if summary.get("authorized") != sum(row.get("state") == "authorized" for row in coverage):
        raise ValueError("Summary authorized count differs from reviewed coverage")
    return summary


class Host:
    def command(self, *args, check=True, timeout=30):
        return subprocess.run(args, text=True, capture_output=True, check=check, timeout=timeout)

    def ensure_ownership(self):
        for unit in (TIMER, RESTORE_TIMER):
            loaded = self.command("systemctl", "show", unit, "--property=LoadState", "--value", check=False).stdout.strip()
            if unit == RESTORE_TIMER and loaded == "not-found":
                continue
            if loaded != "loaded":
                raise ValueError(f"Cannot verify timer {unit}")
            enabled = self.command("systemctl", "is-enabled", unit, check=False).stdout.strip()
            active = self.command("systemctl", "is-active", unit, check=False).stdout.strip()
            if enabled not in {"disabled", "masked", "not-found"} or active not in {"inactive", "failed", "unknown"}:
                raise ValueError(f"{unit} must be disabled and inactive before Airflow collection")
        active = self.command("systemctl", "is-active", SERVICE, check=False).stdout.strip()
        if active not in {"inactive", "failed"}:
            raise ValueError("The original collector is still running; let it finish")

    def ensure_schedule(self, site, coverage):
        if site["slug"] != "seahawks":
            from schedules import ensure_schedule
            ensure_schedule(site, coverage)

    def run(self, args):
        # A timed-out Docker client may leave a running container; the durable pending
        # reservation blocks further launches until an operator resolves that state.
        return self.command(*args, check=False, timeout=3600)


class Bridge:
    def __init__(self, root=ROOT, queue=QUEUE, install=INSTALL, host=None, clock=None):
        self.root, self.queue, self.install = Path(root), Path(queue), Path(install)
        self.host, self.clock = host or Host(), clock or (lambda: datetime.now(UTC))
        self.state = self.root / "state"

    def init(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock(), self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (team TEXT, slot TEXT, request_id TEXT, state TEXT, receipt TEXT, PRIMARY KEY(team,slot))")
            db.commit()
        return self.status()

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.state / "bridge.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.state / "ledger.sqlite3")
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def settings(self):
        settings = read_json(self.install / "settings.json")
        if not isinstance(settings, dict) or settings.get("version") != 2 or not re.fullmatch(r"sha256:[0-9a-f]{64}", settings.get("image_id", "")):
            raise ValueError("Invalid installed settings or pinned Docker image")
        parse_utc(settings.get("activated_at"))
        return settings

    def status(self):
        with self.db() as db:
            return {"version": 2, "attempts": db.execute("SELECT count(*) FROM attempts").fetchone()[0],
                    "unresolved_attempts": db.execute("SELECT count(*) FROM attempts WHERE state='pending'").fetchone()[0]}

    def receipt(self, request, status, message, **extra):
        site = request.get("site", {})
        return {"version": 2, "request_id": request.get("request_id"), "status": status,
                "message": message, "team": site.get("slug") if isinstance(site, dict) else None,
                "slot": request.get("slot"), "finished_at": self.clock().isoformat(), **extra}

    def validate_request(self, request):
        if not isinstance(request, dict) or type(request.get("version")) is not int or request["version"] != 2:
            raise ValueError("Only modular bridge protocol version 2 is accepted")
        if not isinstance(request.get("request_id"), str) or not NAME.fullmatch(request["request_id"] + ".json"):
            raise ValueError("Invalid request identity")
        base = {"version", "operation", "request_id"}
        if request.get("operation") == "health" and set(request) == base:
            return None
        if request.get("operation") != "collect" or set(request) != base | {"slot", "site"}:
            raise ValueError("Only health or configured team collection is supported")
        raw = request["site"]
        if not isinstance(raw, dict):
            raise ValueError("Invalid site")
        site = validate_site(raw.get("slug"), raw)
        if not site["enabled"] or "eventspy" not in site:
            raise ValueError("Site is disabled or has no EventSpy configuration")
        if eventspy_slot(request["slot"]) != request["slot"]:
            raise ValueError("Request slot must use canonical UTC ISO format")
        if eventspy_request_id(request["slot"], site) != request["request_id"]:
            raise ValueError("Request identity does not match its site, slot and configuration")
        return site

    def eligible(self, slot, settings):
        when, now = parse_utc(slot), self.clock()
        local = when.astimezone(LA)
        if local.hour not in HOURS or any((local.minute, local.second, local.microsecond)):
            return "Not one of the seven original Pacific collection slots"
        if when <= parse_utc(settings["activated_at"]):
            return "This slot predates the Airflow scheduling handoff"
        # Teams execute sequentially; allow a later team to start after earlier teams.
        if now < when or now - when > timedelta(minutes=90) or local.date() != now.astimezone(LA).date():
            return "Future, stale, or backfill slots cannot collect"
        return None

    def job(self, request, site, settings):
        options = site["eventspy"]
        coverage_path = self.install / "coverage" / options["coverage_file"]
        coverage = read_json(coverage_path, 262144)
        rows = coverage.get("events") if isinstance(coverage, dict) else coverage
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
            raise ValueError("Reviewed coverage is missing or has an invalid event count")
        self.host.ensure_schedule(site, coverage)
        schedule = Path(options["schedule_file"]).resolve(strict=True)
        # SEA's current is intentionally a symlink to the persistent atomic snapshot.
        allowed = Path("/var/lib/sfz-nfl") if site["slug"] == "seahawks" else self.root / "schedules"
        if not schedule.is_relative_to(allowed.resolve()) or not schedule.is_file():
            raise ValueError("Schedule resolved outside its team snapshot directory")
        output = Path(options["output_dir"])
        if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
            raise ValueError("EventSpy output may not contain symlinks")
        output.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.chown(output, 1000, 1000)
        folder = self.root / "jobs" / request["request_id"]
        folder.mkdir(parents=True, exist_ok=True, mode=0o755)
        config = {"version": 2, "slot": request["slot"], "site": {key: site[key] for key in
                  ("slug", "name", "city", "abbreviation", "balldontlie_team_id", "timezone") if key in site},
                  "output_dir": "/output", "cache_dir": "/cache", "coverage_file": "/run/eventspy/coverage.json",
                  "schedule_file": "/run/eventspy/schedule.json"}
        config_path = folder / "request.json"
        write_json(config_path, config)
        args = ["docker", "run", "--rm", "--pull=never", "--init", "--read-only", "--shm-size=256m", "--pids-limit=256",
                "--cap-drop=ALL", "--security-opt", "no-new-privileges:true", "--user", "1000:1000",
                "--name", "fanzone-eventspy-" + request["request_id"][:32],
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m,mode=1777"]
        mounts = [(self.install / "collector.mjs", "/app/modular-collector.mjs", True),
                  (self.install / "collector-coverage.mjs", "/app/collector-coverage.mjs", True),
                  (config_path, "/run/eventspy/request.json", True),
                  (coverage_path, "/run/eventspy/coverage.json", True), (schedule, "/run/eventspy/schedule.json", True),
                  (output, "/output", False), (self.root / "cache", "/cache", False)]
        for source, target, readonly in mounts:
            args += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
        args += ["--entrypoint", "node", settings["image_id"], "/app/modular-collector.mjs", "--config", "/run/eventspy/request.json"]
        return args, rows

    def execute(self, request, db):
        site = self.validate_request(request)
        settings = self.settings()
        if site is None:
            return self.receipt(request, "success", "Modular EventSpy queue is responding; no source requests made")
        reason = self.eligible(request["slot"], settings)
        if reason:
            return self.receipt(request, "skipped", reason)
        previous = db.execute("SELECT * FROM attempts WHERE team=? AND slot=?", (site["slug"], request["slot"])).fetchone()
        if previous:
            if previous["request_id"] == request["request_id"] and previous["receipt"]:
                return json.loads(previous["receipt"])
            return self.receipt(request, "skipped", "This team/slot was already reserved; configuration changes do not authorize another attempt")
        if db.execute("SELECT 1 FROM attempts WHERE state='pending'").fetchone():
            return self.receipt(request, "failed", "An unresolved collector invocation requires inspection before more collection")
        self.host.ensure_ownership()
        args, coverage = self.job(request, site, settings)
        with db:
            db.execute("INSERT INTO attempts VALUES (?,?,?,'pending',NULL)", (site["slug"], request["slot"], request["request_id"]))
        try:
            result = self.host.run(args)
        except Exception as exc:
            # Keep pending on interruption: a Docker invocation may still be running.
            return self.receipt(request, "failed", f"Collector wait interrupted; reservation remains unresolved ({type(exc).__name__})")
        summary = None
        for line in reversed(result.stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except ValueError:
                continue
            if isinstance(candidate, dict) and str(candidate.get("outcome", "")).startswith("EVENTSPY_SEASON_"):
                summary = candidate
                break
        try:
            validate_summary(summary, site, request["slot"], coverage)
            success = result.returncode == 0 and summary["failed"] == 0 and summary["unresolved"] == 0
            receipt = self.receipt(request, "success" if success else "failed",
                                   f"{site['city']} {site['name']}: {summary['succeeded']} published, {summary['skipped']} skipped, "
                                   f"{summary['unavailable']} unavailable, {summary['unresolved']} unresolved, {summary['failed']} failed",
                                   summary=summary, exit_code=result.returncode)
        except ValueError as exc:
            receipt = self.receipt(request, "failed", str(exc), exit_code=result.returncode)
        with db:
            db.execute("UPDATE attempts SET state='finished',receipt=? WHERE team=? AND slot=?",
                       (json.dumps(receipt), site["slug"], request["slot"]))
        return receipt

    def process(self):
        count = 0
        with self.lock(), self.db() as db:
            for path in sorted((self.queue / "requests").glob("*.json"))[:256]:
                if not NAME.fullmatch(path.name):
                    continue
                response = self.queue / "responses" / path.name
                if response.exists():
                    path.unlink(missing_ok=True)
                    continue
                request = {"request_id": path.stem}
                try:
                    candidate = read_json(path)
                    if not isinstance(candidate, dict) or candidate.get("request_id") != path.stem:
                        raise ValueError("Request filename and identity differ")
                    request = candidate
                    receipt = self.execute(request, db)
                except Exception as exc:
                    receipt = self.receipt(request, "failed", f"Request rejected: {type(exc).__name__}: {exc}")
                write_json(response, receipt)
                path.unlink(missing_ok=True)
                count += 1
        return {"processed": count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "process", "status"))
    args = parser.parse_args()
    print(json.dumps(getattr(Bridge(), args.action)(), indent=2))


if __name__ == "__main__":
    main()
