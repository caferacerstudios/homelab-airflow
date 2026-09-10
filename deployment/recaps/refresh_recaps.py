#!/usr/bin/env python3
"""Generate game recaps in isolation and atomically publish a local snapshot.

Run as laurawkr. Credentials stay in the production .env; this runner never
changes the production checkout, builds a site, or refreshes the NFL snapshot.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
import uuid


SOURCE = Path("/home/laurawkr/seahawksfanzone")
RUNTIME = Path("/var/lib/sfz-recaps")
NFL_CURRENT = Path("/var/lib/sfz-nfl/current")
IMAGE = "node:22-bookworm"
DOCKER_LABEL = "com.caferacerstudios.pipeline=sfz-recaps"
KEY_NAMES = ("BALLDONTLIE_API_KEY", "OPENAI_API_KEY")


def command(args: list[str], **kwargs) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)
    return result.stdout.strip()


def read_api_keys(path: Path) -> dict[str, str]:
    """Read literal dotenv values without shell sourcing or interpolation."""
    found: dict[str, list[str]] = {name: [] for name in KEY_NAMES}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*(?:export\s+)?(BALLDONTLIE_API_KEY|OPENAI_API_KEY)\s*=(.*)$", line)
        if not match:
            continue
        name, raw = match.groups()
        raw = raw.strip()
        if raw.startswith(("'", '"')):
            literal = re.fullmatch(r"(['\"])(.*?)\1\s*(?:#.*)?", raw)
            if not literal:
                raise ValueError(f"{name} must be a single literal dotenv value")
            value = literal.group(2)
        else:
            value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
            if re.search(r"\s", value):
                raise ValueError(f"Quote a {name} value containing spaces")
        if not value or any(char in value for char in ("$", "`", "\\", "\x00")):
            raise ValueError(f"{name} must be nonempty and literal; shell expansion is unsupported")
        found[name].append(value)
    for name, values in found.items():
        if len(values) != 1:
            raise ValueError(f"Expected exactly one {name} entry in the production .env")
    return {name: values[0] for name, values in found.items()}


def redact(text: str, keys: dict[str, str]) -> str:
    for value in sorted(keys.values(), key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


def source_commit(source: Path) -> str:
    root = command(["git", "-C", str(source), "rev-parse", "--show-toplevel"])
    if Path(root).resolve() != source.resolve():
        raise ValueError("The production source must be its own Git checkout")
    if command(["git", "-C", str(source), "branch", "--show-current"]) != "main":
        raise ValueError("Production source must already be on main; no branch changes were made")
    if command(["git", "-C", str(source), "status", "--porcelain", "--", "scripts/generate-game-recaps.mjs", "scripts/nfl-api-client.mjs", "scripts/import-recap-snapshot.mjs", "src/lib/schedule.mjs", "src/lib/recap-artifacts.mjs"]):
        raise ValueError("Commit or resolve local recap source changes before generating; source was left unchanged")
    return command(["git", "-C", str(source), "rev-parse", "HEAD"])


def load_object(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return value


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Snapshot timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Snapshot timestamps must include a timezone")
    return parsed


def valid_season(value) -> bool:
    return type(value) is int and 2002 <= value <= 2200


def select_nfl_snapshot(current: Path) -> tuple[Path, dict]:
    # Resolve once so an NFL publication cannot mix files from two runs.
    snapshot = current.resolve(strict=True)
    manifest = load_object(snapshot / "manifest.json")
    files = manifest.get("files")
    if (manifest.get("schema_version") != 1 or not isinstance(manifest.get("runId"), str)
            or not manifest["runId"] or not valid_season(manifest.get("season"))
            or not isinstance(files, dict) or "seahawks.json" not in files):
        raise ValueError("NFL snapshot manifest identity is invalid")
    timestamp(manifest.get("updatedAt"))
    if file_hash(snapshot / "seahawks.json") != files["seahawks.json"]:
        raise ValueError("NFL snapshot checksum mismatch: seahawks.json")
    data = load_object(snapshot / "seahawks.json")
    team = data.get("team", {})
    if (not isinstance(team, dict) or team.get("abbreviation") != "SEA"
            or type(team.get("id")) is not int or team["id"] < 1):
        raise ValueError("NFL snapshot must identify the Seahawks")
    if data.get("fixture") is True or data.get("season") != manifest["season"] or data.get("updatedAt") != manifest["updatedAt"]:
        raise ValueError("NFL snapshot freshness/season mismatch or fixture data")
    if not isinstance(data.get("gamesRegular"), list) or not isinstance(data.get("gamesPostseason"), list):
        raise ValueError("NFL snapshot is missing regular/postseason game arrays")
    return snapshot, manifest


def preflight(source: Path, runtime: Path, nfl_current: Path) -> tuple[str, dict, Path, dict]:
    commit = source_commit(source)
    script = (source / "scripts/generate-game-recaps.mjs").read_text()
    if "RECAP_GENERATION_REPORT" not in script or not (source / "scripts/import-recap-snapshot.mjs").is_file():
        raise ValueError("Deploy the recap generator/report and importer support to main first")
    if not (source / "src/lib").is_dir():
        raise ValueError("Production source is missing src/lib")
    keys = read_api_keys(source / ".env")
    if not runtime.is_dir() or not os.access(runtime, os.W_OK | os.X_OK):
        raise ValueError(f"Runtime directory must exist and be writable by the collector user: {runtime}")
    snapshot, manifest = select_nfl_snapshot(nfl_current)
    command(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
    try:
        command(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], timeout=30)
    except subprocess.CalledProcessError:
        raise RuntimeError(f"Required generator image is not cached. Run: docker pull {IMAGE}") from None
    return commit, keys, snapshot, manifest


def load_recaps(path: Path) -> dict:
    data = load_object(path)
    if not isinstance(data.get("recaps"), dict) or not all(isinstance(item, dict) for item in data["recaps"].values()):
        raise ValueError("gameRecaps.json must contain a recaps object of game entries")
    return data


def complete_recap(entry: dict) -> bool:
    return bool(isinstance(entry.get("segments"), list) and entry["segments"]
                and isinstance(entry.get("bullets"), list) and entry["bullets"])


def verify_snapshot(snapshot: Path, run_id: str | None = None) -> dict:
    manifest = load_object(snapshot / "manifest.json")
    if (manifest.get("schema_version") != 1 or not isinstance(manifest.get("runId"), str)
            or not manifest["runId"] or (run_id is not None and manifest["runId"] != run_id)
            or not valid_season(manifest.get("season"))):
        raise ValueError("Recap snapshot manifest identity is invalid")
    timestamp(manifest.get("updatedAt"))
    files = manifest.get("files", {})
    if not isinstance(files, dict) or set(files) != {"gameRecaps.json"}:
        raise ValueError("Recap snapshot manifest file list is invalid")
    if file_hash(snapshot / "gameRecaps.json") != files["gameRecaps.json"]:
        raise ValueError("Recap snapshot checksum mismatch: gameRecaps.json")
    data = load_recaps(snapshot / "gameRecaps.json")
    if data.get("season") != manifest["season"] or data.get("updatedAt") != manifest["updatedAt"]:
        raise ValueError("Recap snapshot freshness/season mismatch")
    return manifest


def stage_source(source: Path, work: Path, current: Path, nfl_snapshot: Path, season: int) -> dict:
    shutil.copytree(source / "scripts", work / "scripts")
    shutil.copytree(source / "src/lib", work / "src/lib")
    data = work / "src/data/nfl"
    data.mkdir(parents=True)
    shutil.copy2(nfl_snapshot / "seahawks.json", data / "seahawks.json")
    seed: dict = {"season": season, "updatedAt": None, "recaps": {}}
    if current.exists() or current.is_symlink():
        previous = current.resolve(strict=True)
        verify_snapshot(previous)
        seed = load_recaps(previous / "gameRecaps.json")
    source_recaps = source / "src/data/nfl/gameRecaps.json"
    if source_recaps.is_file():
        authored = load_recaps(source_recaps)
        for game_id, entry in authored["recaps"].items():
            # Preserve all historical entries, adding authored edits when complete.
            if game_id not in seed["recaps"] or complete_recap(entry):
                seed["recaps"][game_id] = entry
    seed["season"] = season
    (data / "gameRecaps.json").write_text(json.dumps(seed, indent=2) + "\n")
    return seed


def run_node(work: Path, keys: dict[str, str], run_key: str) -> None:
    if command(["docker", "ps", "--filter", f"label={DOCKER_LABEL}", "--format", "{{.ID}}"]):
        raise ValueError("A previous recap generator container is still running; no second run was started")
    name = f"sfz-recaps-{run_key[:24]}-{uuid.uuid4().hex[:8]}"
    env = os.environ.copy()
    env.update(keys)
    args = [
        "docker", "run", "--rm", "--pull=never", "--name", name,
        "--label", DOCKER_LABEL, "--read-only", "--user", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,src={work},dst=/app", "--workdir", "/app",
        "--env", "BALLDONTLIE_API_KEY", "--env", "OPENAI_API_KEY",
        "--env", "NFL_REQUEST_INTERVAL_MS=15000",
        "--env", "RECAP_GENERATION_REPORT=/app/recap-report.json",
        IMAGE, "node", "scripts/generate-game-recaps.mjs",
    ]
    process = subprocess.Popen(args, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        output, _ = process.communicate(timeout=3000)
    except BaseException as exc:
        try:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=45)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=15)
        if isinstance(exc, subprocess.TimeoutExpired):
            raise TimeoutError("Recap generation exceeded its 50-minute timeout") from None
        raise
    safe_output = redact(output, keys)
    (work / "collector.log").write_text(safe_output)
    if process.returncode:
        print("\n".join(safe_output.splitlines()[-60:]), file=sys.stderr)
        raise RuntimeError(f"Recap generator exited {process.returncode}; inspect {work / 'collector.log'}")


def validate_outputs(work: Path, started: datetime, season: int, seed: dict) -> tuple[dict, Path]:
    report = load_object(work / "recap-report.json")
    if report.get("schema_version") != 1 or report.get("status") != "success":
        raise ValueError("Generator did not report successful recap generation")
    if timestamp(report.get("updatedAt")) < started or report.get("season") != season:
        raise ValueError("Generator report has a stale timestamp or wrong season")
    for name in ("generatedCount", "requestCount", "openaiRequestCount"):
        if type(report.get(name)) is not int or report[name] < 0:
            raise ValueError(f"Generator report must include nonnegative {name}")
    ids = report.get("generatedGameIds")
    if (not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids)
            or len(set(ids)) != len(ids) or len(ids) != report["generatedCount"]):
        raise ValueError("Generator report has inconsistent generated game IDs")
    if not isinstance(report.get("model"), str) or not report["model"]:
        raise ValueError("Generator report must identify its model")
    data_path = work / "src/data/nfl/gameRecaps.json"
    data = load_recaps(data_path)
    if data.get("updatedAt") != report["updatedAt"] or data.get("season") != season:
        raise ValueError("Generated recap freshness/season mismatch")
    if not set(seed["recaps"]) <= set(data["recaps"]):
        raise ValueError("Generator removed historical recap entries")
    for game_id, entry in seed["recaps"].items():
        if complete_recap(entry) and (data["recaps"][game_id].get("segments") != entry["segments"]
                                      or data["recaps"][game_id].get("bullets") != entry["bullets"]):
            raise ValueError("Generator replaced existing complete recap prose")
    if not all(game_id in data["recaps"] and complete_recap(data["recaps"][game_id]) for game_id in ids):
        raise ValueError("Generated game IDs lack complete recap output")
    return report, data_path


def collect(run_id: str, source: Path = SOURCE, runtime: Path = RUNTIME, nfl_current: Path = NFL_CURRENT) -> dict:
    if not run_id or len(run_id) > 512:
        raise ValueError("run-id must contain between 1 and 512 characters")
    run_key = hashlib.sha256(run_id.encode()).hexdigest()
    run_dir = runtime / "runs" / run_key
    snapshot = run_dir / "snapshot"
    with (runtime / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another recap generation is running; no new run was started") from None
        if snapshot.exists():
            manifest = verify_snapshot(snapshot, run_id)
            current = runtime / "current"
            # Recover interrupted publication without rolling back a newer snapshot.
            if not current.exists() or timestamp(verify_snapshot(current.resolve())["updatedAt"]) <= timestamp(manifest["updatedAt"]):
                publish_link(snapshot, runtime)
            return {**manifest, "snapshotPath": str(snapshot)}
        commit, keys, nfl_snapshot, nfl_manifest = preflight(source, runtime, nfl_current)
        run_dir.mkdir(parents=True, exist_ok=True)
        work = run_dir / f"work-{time.time_ns()}"
        seed = stage_source(source, work, runtime / "current", nfl_snapshot, nfl_manifest["season"])
        if source_commit(source) != commit:
            raise RuntimeError("Source commit changed during staging; no API calls were started")
        if file_hash(work / "src/data/nfl/seahawks.json") != nfl_manifest["files"]["seahawks.json"]:
            raise ValueError("NFL input changed during staging; no API calls were started")
        started = datetime.now(timezone.utc)
        run_node(work, keys, run_key)
        report, data_path = validate_outputs(work, started, nfl_manifest["season"], seed)
        pending = run_dir / f"snapshot-{uuid.uuid4().hex}.tmp"
        pending.mkdir()
        shutil.copy2(data_path, pending / "gameRecaps.json")
        with (pending / "gameRecaps.json").open("rb") as copied:
            os.fsync(copied.fileno())
        manifest = {
            "schema_version": 1, "runId": run_id, "updatedAt": report["updatedAt"],
            "season": report["season"], "sourceCommit": commit,
            "nflSourceRunId": nfl_manifest["runId"], "nflSourceUpdatedAt": nfl_manifest["updatedAt"],
            **{name: report[name] for name in ("generatedCount", "generatedGameIds", "requestCount", "openaiRequestCount", "model")},
            "files": {"gameRecaps.json": file_hash(pending / "gameRecaps.json")},
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
    raise InterruptedError("Recap generator interrupted; its owned container is being stopped")


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
            commit, _, _, nfl_manifest = preflight(SOURCE, RUNTIME, NFL_CURRENT)
            print(json.dumps({"status": "ready", "sourceCommit": commit, "image": IMAGE,
                              "nflSourceRunId": nfl_manifest["runId"], "apiRequests": 0}))
        else:
            print("SFZ_RECAP_RECEIPT=" + json.dumps(collect(args.run_id), separators=(",", ":")))
        return 0
    except BrokenPipeError:
        return 0
    except Exception as exc:
        print(f"Recap refresh failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
