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
SHARED = Path("/opt/fanzone-shared")
DIVISIONS = {
    "AFC East": ["BUF", "MIA", "NE", "NYJ"], "AFC North": ["BAL", "CIN", "CLE", "PIT"],
    "AFC South": ["HOU", "IND", "JAX", "TEN"], "AFC West": ["DEN", "KC", "LAC", "LV"],
    "NFC East": ["DAL", "NYG", "PHI", "WAS"], "NFC North": ["CHI", "DET", "GB", "MIN"],
    "NFC South": ["ATL", "CAR", "NO", "TB"], "NFC West": ["ARI", "LAR", "SF", "SEA"],
}


def site_settings(site=None):
    if site is None:
        return {"slug": "seahawks", "name": "Seahawks", "city": "Seattle", "abbreviation": "SEA",
                "balldontlie_team_id": 31, "nfl_snapshot_dir": str(RUNTIME / "current")}
    if str(SHARED) not in sys.path:
        sys.path.insert(0, str(SHARED))
    from fan_zone_host import authorize_site
    return authorize_site(site, "nfl")


def primary_files(site=None):
    return (f"{(site or {}).get('slug', 'seahawks')}.json", "players.json", "standings.json")


def snapshot_files(site=None):
    return (*primary_files(site), "gameRecaps.json")


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


def validate_outputs(work: Path, started: datetime, site=None) -> tuple[dict, Path]:
    selected = site or site_settings()
    combined_name = primary_files(selected)[0]
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
    payloads = {name: load_object(data / name) for name in primary_files(selected)}
    for name, payload in payloads.items():
        if payload.get("updatedAt") != report["updatedAt"] or payload.get("season") != season:
            raise ValueError(f"Freshness/season mismatch in {name}")
        if payload.get("fixture") is True:
            raise ValueError(f"Refusing fixture data in {name}")
    combined = payloads[combined_name]
    players = payloads["players.json"]
    team_id = combined.get("team", {}).get("id")
    if type(team_id) is not int or team_id < 1:
        raise ValueError("Combined snapshot has an invalid team ID")
    for name, payload in ((combined_name, combined), ("players.json", players)):
        if payload.get("team", {}).get("abbreviation") != selected["abbreviation"] or payload["team"].get("id") != team_id:
            raise ValueError(f"Wrong team in {name}")
        if payload.get("playerStatsSeason") != report.get("playerStatsSeason"):
            raise ValueError(f"Player-stat season mismatch in {name}")
        for key in ("currentRoster", "playerDirectory", "playerSeasonStats"):
            if not isinstance(payload.get(key), list):
                raise ValueError(f"Missing {key} array in {name}")
        if not payload["playerSeasonStats"]:
            raise ValueError(f"Empty player statistics in {name}")
    expected_id = selected.get("balldontlie_team_id")
    if expected_id is not None and team_id != expected_id:
        raise ValueError("NFL snapshot team ID does not match configured team")
    for key in ("games", "gamesPreseason", "gamesRegular", "gamesPostseason"):
        if not isinstance(combined.get(key), list):
            raise ValueError(f"Missing {key} array in {combined_name}")
    if not combined["gamesRegular"]:
        raise ValueError("Regular-season schedule is empty")
    for phase in ("preseason", "regular", "postseason"):
        bucket = payloads["standings.json"].get("phases", {}).get(phase, {})
        if bucket.get("phase") != phase or not isinstance(bucket.get("rows"), list):
            raise ValueError(f"Missing {phase} standings rows")
    if (data / "gameRecaps.json").exists():
        load_object(data / "gameRecaps.json")
    return report, data


def verify_snapshot(snapshot: Path, run_id: str | None = None, site=None) -> dict:
    manifest = load_object(snapshot / "manifest.json")
    if manifest.get("schema_version") != 1 or (run_id is not None and manifest.get("runId") != run_id):
        raise ValueError("Snapshot manifest identity is invalid")
    slug = (site or {}).get("slug", "seahawks")
    if manifest.get("team") not in ([None, "seahawks"] if slug == "seahawks" else [slug]):
        raise ValueError("Snapshot manifest belongs to a different team")
    files = manifest.get("files", {})
    if not isinstance(files, dict) or not set(primary_files(site)) <= set(files) or not set(files) <= set(snapshot_files(site)):
        raise ValueError("Snapshot manifest file list is invalid")
    for name, digest in files.items():
        if file_hash(snapshot / name) != digest:
            raise ValueError(f"Snapshot checksum mismatch: {name}")
    if site is not None:
        for name in primary_files(site)[:2]:
            team = load_object(snapshot / name).get("team", {})
            if team.get("abbreviation") != site["abbreviation"]:
                raise ValueError("Cached snapshot team abbreviation mismatch")
            if site.get("balldontlie_team_id") is not None and team.get("id") != site["balldontlie_team_id"]:
                raise ValueError("Cached snapshot team ID mismatch")
    return manifest


def stage_source(source: Path, work: Path, current: Path, site=None) -> None:
    shutil.copytree(source / "scripts", work / "scripts")
    shutil.copytree(source / "src/lib", work / "src/lib")
    roster = work / "src/data/team/roster.json"
    roster.parent.mkdir(parents=True)
    selected = site or site_settings()
    combined_name = primary_files(selected)[0]
    if selected["slug"] == "seahawks":
        shutil.copy2(source / "src/data/team/roster.json", roster)
    else:
        # The BDL player directory is not an authoritative active roster.
        roster.write_text(json.dumps({"players": []}) + "\n")
        adapt_staged_source(work, selected)
    data = work / "src/data/nfl"
    data.mkdir(parents=True)
    source_data = source / "src/data/nfl"
    for path in (source_data.glob("watch-guide-*.json") if selected["slug"] == "seahawks" else []):
        shutil.copy2(path, data / path.name)
    for name in (("seahawks.json", "gameRecaps.json") if selected["slug"] == "seahawks" else []):
        if (source_data / name).is_file():
            shutil.copy2(source_data / name, data / name)
    if current.exists():
        previous = current.resolve(strict=True)
        verify_snapshot(previous, site=selected)
        shutil.copy2(previous / combined_name, data / combined_name)


def check_staging_contract(source: Path) -> None:
    fetch = (source / "scripts/fetch-nfl.mjs").read_text()
    normalizer = (source / "src/lib/schedule.mjs").read_text()
    required = {
        '"seahawks.json"': 2,
        '  console.log(`Using ${TEAM_ABBR} team id: ${team.id}`);': 1,
    }
    if any(fetch.count(marker) != count for marker, count in required.items()):
        raise ValueError("Production NFL fetcher staging contract changed; review before collecting another team")
    standings = (source / "src/lib/standings.mjs").read_text()
    if standings.count('const WEST = new Set(["ARI", "LAR", "SF", "SEA"]);') != 1:
        raise ValueError("Production standings division binding changed; review before collecting another team")
    if normalizer.count('const TEAM = "SEA";') != 1:
        raise ValueError("Production schedule normalizer team binding changed; review before collecting another team")


def adapt_staged_source(work: Path, site: dict) -> None:
    """Bind the production normalizers to one team only in an isolated work copy."""
    check_staging_contract(work)
    fetch_path = work / "scripts/fetch-nfl.mjs"
    fetch = fetch_path.read_text()
    fetch = fetch.replace('"seahawks.json"', json.dumps(site["slug"] + ".json"))
    marker = '  console.log(`Using ${TEAM_ABBR} team id: ${team.id}`);'
    expected = json.dumps({"full_name": site["city"] + " " + site["name"],
                           "id": site.get("balldontlie_team_id")})
    guard = '''  const configuredTeam = __EXPECTED__;
  const matchingTeams = teams.filter((candidate) => (candidate.abbreviation || "").toUpperCase() === TEAM_ABBR);
  if (matchingTeams.length !== 1 || !Number.isSafeInteger(team.id) || team.id < 1
      || String(team.full_name || "").toLowerCase() !== configuredTeam.full_name.toLowerCase()
      || (configuredTeam.id !== null && team.id !== configuredTeam.id)) {
    throw new Error("API team identity does not match the configured active site");
  }
'''.replace("__EXPECTED__", expected)
    fetch_path.write_text(fetch.replace(marker, guard + marker))
    normalizer = work / "src/lib/schedule.mjs"
    body = normalizer.read_text().replace('const TEAM = "SEA";', 'const TEAM = ' + json.dumps(site["abbreviation"]) + ';')
    body = body.replace('game.seahawksRecordAfter', 'game.' + site["slug"].replace('-', '_') + 'RecordAfter')
    normalizer.write_text(body)
    division = DIVISIONS.get(site.get("division"))
    if not division or site["abbreviation"] not in division:
        raise ValueError("Configured NFL division does not contain the selected team")
    standings = work / "src/lib/standings.mjs"
    standings.write_text(standings.read_text().replace(
        'const WEST = new Set(["ARI", "LAR", "SF", "SEA"]);',
        'const WEST = new Set(' + json.dumps(division) + ');'))


def run_node(work: Path, api_key: str, run_key: str, site=None) -> None:
    if command(["docker", "ps", "--filter", f"label={DOCKER_LABEL}", "--format", "{{.ID}}"]):
        raise ValueError("A previous NFL collector container is still running; no second collection was started")
    name = f"sfz-nfl-{run_key[:24]}-{uuid.uuid4().hex[:8]}"
    selected = site or site_settings()
    env = os.environ.copy()
    env["BALLDONTLIE_API_KEY"] = api_key
    args = [
        "docker", "run", "--rm", "--pull=never", "--name", name,
        "--label", DOCKER_LABEL, "--read-only", "--user", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,src={work},dst=/app", "--workdir", "/app",
        "--env", "BALLDONTLIE_API_KEY", "--env", "NFL_TEAM_ABBR=" + selected["abbreviation"],
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


def collect(run_id: str, source: Path = SOURCE, runtime: Path | None = None, site=None) -> dict:
    if not run_id or len(run_id) > 512:
        raise ValueError("run-id must contain between 1 and 512 characters")
    selected = site_settings(site)
    if selected.get("enabled") is False:
        raise ValueError("NFL refresh is disabled for this site")
    explicit_runtime = runtime is not None
    runtime = Path(runtime) if explicit_runtime else Path(selected["nfl_snapshot_dir"]).parent
    # Retain the existing global host lock as well as the single Airflow API pool.
    lock_runtime = runtime if explicit_runtime else RUNTIME
    run_key = hashlib.sha256(run_id.encode()).hexdigest()
    run_dir = runtime / "runs" / run_key
    snapshot = run_dir / "snapshot"
    with (lock_runtime / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another NFL collection is running; no new collection was started") from None
        if snapshot.exists():
            manifest = verify_snapshot(snapshot, run_id, selected)
            # Finish an interrupted publication, but never roll back a newer run.
            current = runtime / "current"
            if not current.exists() or timestamp(verify_snapshot(current.resolve(), site=selected)["updatedAt"]) <= timestamp(manifest["updatedAt"]):
                publish_link(snapshot, runtime)
            return {**manifest, "snapshotPath": str(snapshot)}
        commit, api_key = preflight(source, runtime)
        run_dir.mkdir(parents=True, exist_ok=True)
        work = run_dir / f"work-{time.time_ns()}"
        if site is None:
            stage_source(source, work, runtime / "current")
        else:
            stage_source(source, work, runtime / "current", selected)
        if source_commit(source) != commit:
            raise RuntimeError("Source commit changed during staging; no API collection was started")
        started = datetime.now(timezone.utc)
        if site is None:
            run_node(work, api_key, run_key)
        else:
            run_node(work, api_key, run_key, selected)
        report, data = validate_outputs(work, started, selected)
        pending = run_dir / f"snapshot-{uuid.uuid4().hex}.tmp"
        pending.mkdir()
        for name in snapshot_files(selected):
            if (data / name).is_file():
                shutil.copy2(data / name, pending / name)
                with (pending / name).open("rb") as copied:
                    os.fsync(copied.fileno())
        manifest = {
            "schema_version": 1, "runId": run_id, "updatedAt": report["updatedAt"],
            "season": report["season"], "sourceCommit": commit, "requestCount": report["requestCount"],
            "files": {name: file_hash(pending / name) for name in snapshot_files(selected) if (pending / name).is_file()},
        }
        if site is not None:
            manifest["team"] = selected["slug"]
        with (pending / "manifest.json").open("w") as output:
            output.write(json.dumps(manifest, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        verify_snapshot(pending, run_id, selected)
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
    parser.add_argument("--site-json", help="Validated site request from the dedicated SSH command")
    args = parser.parse_args()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        request_site = json.loads(args.site_json) if args.site_json else None
        selected = site_settings(request_site)
        if args.check:
            commit, _ = preflight(SOURCE, Path(selected["nfl_snapshot_dir"]).parent)
            if selected["slug"] != "seahawks":
                check_staging_contract(SOURCE)
            print(json.dumps({"status": "ready", "sourceCommit": commit, "image": IMAGE, "apiRequests": 0}))
        else:
            result = collect(args.run_id, site=request_site)
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
