#!/usr/bin/env python3
"""Run the existing NFL fetcher in isolation and publish a verified snapshot.

Run as laurawkr, not root. This never changes a website checkout or builds a site.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
import uuid


SOURCE = Path("/home/laurawkr/seahawksfanzone")
RUNTIME = Path("/var/lib/sfz-nfl")
IMAGE = "node:22-bookworm"
PRIMARY_FILES = ("seahawks.json", "players.json", "standings.json")
SNAPSHOT_FILES = (*PRIMARY_FILES, "gameRecaps.json")
DOCKER_LABEL = "com.caferacerstudios.pipeline=sfz-nfl"


def command(args: list[str], **kwargs) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)
    return result.stdout.strip()


def read_api_key(path: Path) -> str:
    """Read one literal dotenv value. Do not source or expand any shell input."""
    found = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*(?:export\s+)?BALLDONTLIE_API_KEY\s*=(.*)$", line)
        if not match:
            continue
        raw = match.group(1).strip()
        if raw.startswith(("'", '"')):
            literal = re.fullmatch(r"(['\"])(.*?)\1\s*(?:#.*)?", raw)
            if not literal:
                raise ValueError("BALLDONTLIE_API_KEY must be a single literal dotenv value")
            value = literal.group(2)
        else:
            value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
            if re.search(r"\s", value):
                raise ValueError("Quote a BALLDONTLIE_API_KEY value containing spaces")
        if not value or any(char in value for char in ("$", "`", "\\", "\x00")):
            raise ValueError("BALLDONTLIE_API_KEY must be nonempty and literal; shell expansion is unsupported")
        found.append(value)
    if len(found) != 1:
        raise ValueError("Expected exactly one BALLDONTLIE_API_KEY entry in the production .env")
    return found[0]


def source_commit(source: Path) -> str:
    root = command(["git", "-C", str(source), "rev-parse", "--show-toplevel"])
    if Path(root).resolve() != source.resolve():
        raise ValueError("The production source must be its own Git checkout")
    if command(["git", "-C", str(source), "branch", "--show-current"]) != "main":
        raise ValueError("Production source must already be on main; no branch changes were made")
    dirty = command(["git", "-C", str(source), "status", "--porcelain", "--", "scripts", "src/lib"])
    if dirty:
        raise ValueError("Commit or resolve local scripts/src/lib changes before collecting; source was left unchanged")
    return command(["git", "-C", str(source), "rev-parse", "HEAD"])


def preflight(source: Path, runtime: Path) -> tuple[str, str]:
    commit = source_commit(source)
    script = (source / "scripts/fetch-nfl.mjs").read_text()
    if "NFL_FETCH_STRICT" not in script or "NFL_FETCH_REPORT" not in script:
        raise ValueError("Production fetch-nfl.mjs lacks Airflow strict/report support; deploy the support commit first")
    if not (source / "src/lib").is_dir() or not (source / "src/data/team/roster.json").is_file():
        raise ValueError("Production source is missing src/lib or src/data/team/roster.json")
    key = read_api_key(source / ".env")
    if not runtime.is_dir() or not os.access(runtime, os.W_OK | os.X_OK):
        raise ValueError(f"Runtime directory must exist and be writable by the collector user: {runtime}")
    command(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
    try:
        command(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], timeout=30)
    except subprocess.CalledProcessError:
        raise RuntimeError(f"Required collector image is not cached. Run: docker pull {IMAGE}") from None
    return commit, key


def load_object(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return value


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Snapshot timestamps must include a timezone")
    return parsed


def validate_outputs(work: Path, started: datetime) -> tuple[dict, Path]:
    report = load_object(work / "fetch-report.json")
    if report.get("status") != "success":
        raise ValueError("Fetcher did not report a successful fresh collection")
    if timestamp(report["updatedAt"]) < started:
        raise ValueError("Fetcher returned a stale success timestamp")
    season = report.get("season")
    if type(season) is not int or not 2002 <= season <= 2200:
        raise ValueError("Fetcher report has an invalid season")
    if type(report.get("playerStatsSeason")) is not int or report["playerStatsSeason"] not in (season, season - 1):
        raise ValueError("Fetcher report has an invalid player-stat season")
    if type(report.get("requestCount")) is not int or report["requestCount"] < 1:
        raise ValueError("Fetcher report must include its positive request count")
    data = work / "src/data/nfl"
    payloads = {name: load_object(data / name) for name in PRIMARY_FILES}
    for name, payload in payloads.items():
        if payload.get("updatedAt") != report["updatedAt"] or payload.get("season") != season:
            raise ValueError(f"Freshness/season mismatch in {name}")
        if payload.get("fixture") is True:
            raise ValueError(f"Refusing fixture data in {name}")
    combined = payloads["seahawks.json"]
    players = payloads["players.json"]
    team_id = combined.get("team", {}).get("id")
    if type(team_id) is not int or team_id < 1:
        raise ValueError("Combined snapshot has an invalid team ID")
    for name, payload in (("seahawks.json", combined), ("players.json", players)):
        if payload.get("team", {}).get("abbreviation") != "SEA" or payload["team"].get("id") != team_id:
            raise ValueError(f"Wrong team in {name}")
        if payload.get("playerStatsSeason") != report.get("playerStatsSeason"):
            raise ValueError(f"Player-stat season mismatch in {name}")
        for key in ("currentRoster", "playerDirectory", "playerSeasonStats"):
            if not isinstance(payload.get(key), list):
                raise ValueError(f"Missing {key} array in {name}")
        if not payload["playerSeasonStats"]:
            raise ValueError(f"Empty player statistics in {name}")
    for key in ("games", "gamesPreseason", "gamesRegular", "gamesPostseason"):
        if not isinstance(combined.get(key), list):
            raise ValueError(f"Missing {key} array in seahawks.json")
    if not combined["gamesRegular"]:
        raise ValueError("Regular-season schedule is empty")
    for phase in ("preseason", "regular", "postseason"):
        bucket = payloads["standings.json"].get("phases", {}).get(phase, {})
        if bucket.get("phase") != phase or not isinstance(bucket.get("rows"), list):
            raise ValueError(f"Missing {phase} standings rows")
    if (data / "gameRecaps.json").exists():
        load_object(data / "gameRecaps.json")
    return report, data


def verify_snapshot(snapshot: Path, run_id: str | None = None) -> dict:
    manifest = load_object(snapshot / "manifest.json")
    if manifest.get("schema_version") != 1 or (run_id is not None and manifest.get("runId") != run_id):
        raise ValueError("Snapshot manifest identity is invalid")
    files = manifest.get("files", {})
    if not isinstance(files, dict) or not set(PRIMARY_FILES) <= set(files) or not set(files) <= set(SNAPSHOT_FILES):
        raise ValueError("Snapshot manifest file list is invalid")
    for name, digest in files.items():
        if file_hash(snapshot / name) != digest:
            raise ValueError(f"Snapshot checksum mismatch: {name}")
    return manifest


def stage_source(source: Path, work: Path, current: Path) -> None:
    shutil.copytree(source / "scripts", work / "scripts")
    shutil.copytree(source / "src/lib", work / "src/lib")
    roster = work / "src/data/team/roster.json"
    roster.parent.mkdir(parents=True)
    shutil.copy2(source / "src/data/team/roster.json", roster)
    data = work / "src/data/nfl"
    data.mkdir(parents=True)
    source_data = source / "src/data/nfl"
    for path in source_data.glob("watch-guide-*.json"):
        shutil.copy2(path, data / path.name)
    for name in ("seahawks.json", "gameRecaps.json"):
        if (source_data / name).is_file():
            shutil.copy2(source_data / name, data / name)
    if current.exists():
        previous = current.resolve(strict=True)
        verify_snapshot(previous)
        shutil.copy2(previous / "seahawks.json", data / "seahawks.json")


def run_node(work: Path, api_key: str, run_key: str) -> None:
    if command(["docker", "ps", "--filter", f"label={DOCKER_LABEL}", "--format", "{{.ID}}"]):
        raise ValueError("A previous NFL collector container is still running; no second collection was started")
    name = f"sfz-nfl-{run_key[:24]}-{uuid.uuid4().hex[:8]}"
    env = os.environ.copy()
    env["BALLDONTLIE_API_KEY"] = api_key
    args = [
        "docker", "run", "--rm", "--pull=never", "--name", name,
        "--label", DOCKER_LABEL, "--read-only", "--user", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,src={work},dst=/app", "--workdir", "/app",
        "--env", "BALLDONTLIE_API_KEY", "--env", "NFL_TEAM_ABBR=SEA",
        "--env", "NFL_FETCH_STRICT=1", "--env", "NFL_REQUEST_INTERVAL_MS=15000",
        "--env", "NFL_FETCH_REPORT=/app/fetch-report.json",
        IMAGE, "node", "scripts/fetch-nfl.mjs",
    ]
    process = subprocess.Popen(args, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        output, _ = process.communicate(timeout=3600)
    except BaseException as exc:
        # Stop/remove only this invocation's container before releasing the host lock.
        try:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=45)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=15)
        if isinstance(exc, subprocess.TimeoutExpired):
            raise TimeoutError("NFL collection exceeded its 60-minute timeout") from None
        raise
    safe_output = output.replace(api_key, "[REDACTED]")
    (work / "collector.log").write_text(safe_output)
    if process.returncode:
        print("\n".join(safe_output.splitlines()[-60:]), file=sys.stderr)
        raise RuntimeError(f"NFL fetcher exited {process.returncode}; inspect {work / 'collector.log'}")


def collect(run_id: str, source: Path = SOURCE, runtime: Path = RUNTIME) -> dict:
    if not run_id or len(run_id) > 512:
        raise ValueError("run-id must contain between 1 and 512 characters")
    run_key = hashlib.sha256(run_id.encode()).hexdigest()
    run_dir = runtime / "runs" / run_key
    snapshot = run_dir / "snapshot"
    with (runtime / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another NFL collection is running; no new collection was started") from None
        if snapshot.exists():
            manifest = verify_snapshot(snapshot, run_id)
            # Finish an interrupted publication, but never roll back a newer run.
            current = runtime / "current"
            if not current.exists() or timestamp(verify_snapshot(current.resolve())["updatedAt"]) <= timestamp(manifest["updatedAt"]):
                publish_link(snapshot, runtime)
            return {**manifest, "snapshotPath": str(snapshot)}
        commit, api_key = preflight(source, runtime)
        run_dir.mkdir(parents=True, exist_ok=True)
        work = run_dir / f"work-{time.time_ns()}"
        stage_source(source, work, runtime / "current")
        if source_commit(source) != commit:
            raise RuntimeError("Source commit changed during staging; no API collection was started")
        started = datetime.now(timezone.utc)
        run_node(work, api_key, run_key)
        report, data = validate_outputs(work, started)
        pending = run_dir / f"snapshot-{uuid.uuid4().hex}.tmp"
        pending.mkdir()
        for name in SNAPSHOT_FILES:
            if (data / name).is_file():
                shutil.copy2(data / name, pending / name)
                with (pending / name).open("rb") as copied:
                    os.fsync(copied.fileno())
        manifest = {
            "schema_version": 1, "runId": run_id, "updatedAt": report["updatedAt"],
            "season": report["season"], "sourceCommit": commit, "requestCount": report["requestCount"],
            "files": {name: file_hash(pending / name) for name in SNAPSHOT_FILES if (pending / name).is_file()},
        }
        with (pending / "manifest.json").open("w") as output:
            output.write(json.dumps(manifest, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        verify_snapshot(pending, run_id)
        sync_directory(pending)
        pending.rename(snapshot)
        sync_directory(run_dir)
        publish_link(snapshot, runtime)
        return {**manifest, "snapshotPath": str(snapshot)}


def publish_link(snapshot: Path, runtime: Path) -> None:
    next_link = runtime / f".current-{uuid.uuid4().hex}"
    next_link.symlink_to(snapshot.relative_to(runtime))
    os.replace(next_link, runtime / "current")
    sync_directory(runtime)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def interrupted(_signum, _frame) -> None:
    raise InterruptedError("Collector interrupted; its owned container is being stopped")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Read-only preflight; no API requests")
    mode.add_argument("--run-id", help="Airflow run ID; successful repeats reuse the verified receipt")
    args = parser.parse_args()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.check:
            commit, _ = preflight(SOURCE, RUNTIME)
            print(json.dumps({"status": "ready", "sourceCommit": commit, "image": IMAGE, "apiRequests": 0}))
        else:
            result = collect(args.run_id)
            print("SFZ_NFL_RECEIPT=" + json.dumps(result, separators=(",", ":")))
        return 0
    except BrokenPipeError:
        # A disconnected SSH reader does not undo a completed publication.
        return 0
    except Exception as exc:
        print(f"NFL refresh failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
