#!/usr/bin/env python3
"""Pause the existing timer, arm today's Airflow trial, and arrange restoration."""

from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


TZ = ZoneInfo("America/Los_Angeles")
HOURS = (3, 6, 9, 12, 15, 18, 21)
BRIDGE = "/opt/sfz-airflow-ticket-test/bridge.py"
PROJECT = "/home/laurawkr/homelab-airflow"
SOURCE_TIMER = "sfz-eventspy-season.timer"
SOURCE_SERVICE = "sfz-eventspy-season.service"
RESTORE_SERVICE = "sfz-airflow-ticket-test-restore.service"
RESTORE_TIMER = "sfz-airflow-ticket-test-restore.timer"
PATH_UNIT = "sfz-airflow-ticket-test.path"
DAG_ID = "sfz_eventspy_collect"
LEGACY_TIMERS = (
    "sfz-eventspy-collector@dev.timer",
    "sfz-eventspy-mirror-dev.timer",
    "sfz-eventspy-mirror.timer",
)


def run(args, *, capture=False, check=True, timeout=120, cwd=None):
    return subprocess.run(
        args, check=check, text=True, capture_output=capture,
        timeout=timeout, cwd=cwd,
    )


def bridge(*args):
    result = run(["/usr/bin/python3", BRIDGE, *args], capture=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Bridge did not return valid JSON; activation stopped.") from exc


def unit_state(name):
    result = run([
        "systemctl", "show", name,
        "--property=LoadState,ActiveState,UnitFileState",
    ], capture=True, check=False)
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and state.get("LoadState") != "not-found":
        raise RuntimeError(f"Cannot inspect {name}: {result.stderr.strip()}")
    if "LoadState" not in state:
        raise RuntimeError(f"systemctl returned no unit state for {name}.")
    return state


def airflow(*args, capture=False):
    return run([
        "runuser", "-u", "laurawkr", "--", "docker", "compose", "exec", "-T",
        "airflow-scheduler", "airflow", *args,
    ], capture=capture, cwd=PROJECT)


def next_slot(now):
    for hour in HOURS:
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > now:
            return slot
    return None


def require_slot(day):
    now = datetime.now(TZ)
    if now.date() != day:
        raise RuntimeError("The Seattle date changed. Start activation again for the new day.")
    slot = next_slot(now)
    if slot is None:
        raise RuntimeError(
            "No scheduled slot remains today. "
            "The collector will not be run outside its approved schedule."
        )
    if slot < now + timedelta(minutes=2):
        raise RuntimeError(
            f"The next slot ({slot:%H:%M %Z}) is less than two minutes away. "
            "Wait until that slot passes, then run activation again."
        )
    return slot


def parse_dag_list(output):
    # Airflow/plugin warnings can precede its requested JSON output.
    for match in re.finditer(r"(?m)^\s*\[", output):
        try:
            value, _ = json.JSONDecoder().raw_decode(output[match.start():].lstrip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
    raise RuntimeError("Could not read the JSON DAG list from Airflow.")


def preflight():
    if os.geteuid() != 0:
        raise RuntimeError("Run this command with sudo.")
    if not sys.stdin.isatty():
        raise RuntimeError("Run interactively: today's prior attempt count must be entered.")
    if not Path(BRIDGE).is_file() or not Path(PROJECT).is_dir():
        raise RuntimeError("Install the trial package first.")
    status = bridge("status")
    if status.get("armed"):
        raise RuntimeError("The trial is already armed. Use the status command; do not reseed it.")
    if status.get("unresolved_attempts", 0):
        raise RuntimeError("An unresolved bridge attempt exists; reconcile it before activation.")
    current = unit_state(SOURCE_TIMER)
    if current.get("LoadState") != "loaded":
        raise RuntimeError(f"{SOURCE_TIMER} is not installed.")
    if current.get("ActiveState") != "active" or current.get("UnitFileState") != "enabled":
        raise RuntimeError(
            f"Expected {SOURCE_TIMER} to be enabled and active before this first cutover. "
            "Inspect its state before proceeding."
        )
    if unit_state(PATH_UNIT).get("ActiveState") != "active":
        raise RuntimeError(f"{PATH_UNIT} must be active before activation.")
    restore_state = unit_state(RESTORE_SERVICE).get("ActiveState")
    if restore_state in {"active", "activating", "deactivating", "reloading"}:
        raise RuntimeError("The restoration service is busy. Wait for it to finish.")
    for name in LEGACY_TIMERS:
        state = unit_state(name)
        if state.get("LoadState") == "not-found":
            continue
        if state.get("ActiveState") not in {"inactive", "failed"} or state.get(
            "UnitFileState", ""
        ).startswith("enabled"):
            raise RuntimeError(f"Legacy timer {name} is active or enabled; inspect it first.")
    result = airflow("dags", "list", "--output", "json", capture=True)
    dags = parse_dag_list(result.stdout)
    matches = [item for item in dags if item.get("dag_id") == DAG_ID]
    if not matches:
        raise RuntimeError(f"{DAG_ID} is not visible to Airflow yet. Check DAG import errors.")
    paused = matches[0].get("is_paused")
    if paused not in (True, "True", "true"):
        raise RuntimeError(f"Pause {DAG_ID} before activation, then run this command again.")
    return status


def install_restore_timer(midnight):
    calendar = midnight.strftime("%Y-%m-%d 00:00:00 America/Los_Angeles")
    run(["systemd-analyze", "calendar", calendar], capture=True)
    service = f"""[Unit]
Description=Disarm the Airflow ticket trial and restore the existing collector timer
After=local-fs.target
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {BRIDGE} restore
TimeoutStartSec=75min
Restart=on-failure
RestartSec=60s
"""
    timer = f"""[Unit]
Description=End today's Airflow ticket trial at Seattle midnight

[Timer]
OnCalendar={calendar}
AccuracySec=1s
Persistent=true
Unit={RESTORE_SERVICE}

[Install]
WantedBy=timers.target
"""
    for name, content in ((RESTORE_SERVICE, service), (RESTORE_TIMER, timer)):
        target = Path("/etc/systemd/system") / name
        # The parent and these files are root-owned; no caller-supplied paths are used.
        if target.is_symlink():
            raise RuntimeError(f"Refusing to replace symlink: {target}")
        target.write_text(content, encoding="utf-8")
        target.chmod(0o644)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "reset-failed", RESTORE_SERVICE], check=False)
    run(["systemctl", "enable", RESTORE_TIMER])
    run(["systemctl", "restart", RESTORE_TIMER])
    if unit_state(RESTORE_TIMER).get("ActiveState") != "active":
        raise RuntimeError("The automatic restoration timer did not become active.")


def drain_collector():
    deadline = time.monotonic() + 60 * 60
    announced = False
    while True:
        state = unit_state(SOURCE_SERVICE)
        if state.get("LoadState") != "loaded":
            raise RuntimeError(f"{SOURCE_SERVICE} is not installed.")
        if state.get("ActiveState") in {"inactive", "failed"}:
            return
        if state.get("ActiveState") not in {"active", "activating", "deactivating", "reloading"}:
            raise RuntimeError(f"Unexpected collector state: {state}")
        if not announced:
            print("The existing collector is running; waiting for it to finish naturally.", flush=True)
            announced = True
        if time.monotonic() >= deadline:
            raise RuntimeError("The collector is still running after one hour; activation cancelled.")
        time.sleep(5)


def show_journal(day):
    midnight = datetime(day.year, day.month, day.day, tzinfo=TZ)
    since = midnight.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    result = run([
        "journalctl", "--unit", SOURCE_SERVICE, "--since", since,
        "--output=json", "--no-pager",
    ], capture=True)
    invocations = {}
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
            key = row.get("_SYSTEMD_INVOCATION_ID")
            if not isinstance(key, str):
                continue
            timestamp = datetime.fromtimestamp(int(row["__REALTIME_TIMESTAMP"]) / 1e6, TZ)
        except (ValueError, TypeError, KeyError):
            continue
        invocations[key] = min(invocations.get(key, timestamp), timestamp)
    print(f"\nSeattle date: {day.isoformat()}")
    print("Journal-visible service invocations today:")
    for key, timestamp in sorted(invocations.items(), key=lambda item: item[1]):
        print(f"  {timestamp:%H:%M:%S %Z}  {key}")
    print(f"Visible invocation count: {len(invocations)}")
    print(
        "This is evidence only: rotated/missing logs and direct manual Docker runs may be absent.\n"
        "Count every full-season collector attempt today, including failures, manual runs,\n"
        "and the run that just finished. Each attempt consumes one of the seven daily slots.\n"
        "If you cannot establish the count, enter unknown to cancel safely."
    )
    return len(invocations)


def read_prior(minimum):
    value = input("Total prior collector attempts today (0-6, or unknown): ").strip().lower()
    if value == "unknown" or not value:
        raise RuntimeError("Prior attempts are unknown; the trial was not armed.")
    if not value.isdecimal() or not 0 <= int(value) <= 6:
        raise RuntimeError("Enter a count from 0 through 6; seven attempts leaves no test allowance.")
    count = int(value)
    if count < minimum:
        raise RuntimeError("That count is below the journal-visible invocation count; activation stopped.")
    return count


def request_immediate_restore():
    # Retry under systemd if a source run is still finishing. Never stop that run.
    run(["systemctl", "start", "--no-block", RESTORE_SERVICE])
    print(
        "Restoration has been requested. It disarms the trial and re-enables the original\n"
        "timer after any collector run finishes; systemd retries every 60 seconds if needed.\n"
        f"Check: systemctl status {RESTORE_SERVICE} {SOURCE_TIMER} --no-pager",
        flush=True,
    )


def main():
    pause_attempted = False
    success = False
    activation_lock = None
    try:
        if os.geteuid() != 0:
            raise RuntimeError("Run this command with sudo.")
        activation_lock = os.open(
            "/var/lib/sfz-airflow-ticket-test/state/activation.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(activation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another activation is already running; wait for it to finish.") from exc
        initial = preflight()
        now = datetime.now(TZ)
        day = now.date()
        slot = require_slot(day)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        print(f"Next eligible slot: {slot:%Y-%m-%d %H:%M %Z}")
        print(f"Automatic return to the original timer: {midnight:%Y-%m-%d %H:%M %Z}")
        install_restore_timer(midnight)
        pause_attempted = True  # A partially successful systemctl call still needs rollback.
        run(["systemctl", "disable", "--now", SOURCE_TIMER])
        drain_collector()
        require_slot(day)
        visible = show_journal(day)
        prior = read_prior(visible)
        slot = require_slot(day)
        if initial.get("test_date") == day.isoformat() and initial.get("bridge_attempts", 0):
            raise RuntimeError("A trial already made attempts today; do not reseed its quota ledger.")
        armed = bridge("arm", "--prior-attempts", str(prior))
        if not armed.get("armed") or armed.get("test_date") != day.isoformat():
            raise RuntimeError("Bridge did not confirm today's armed state.")
        if armed.get("next_slot"):
            slot = datetime.fromisoformat(armed["next_slot"]).astimezone(TZ)
        airflow("dags", "unpause", DAG_ID)
        # If the activation crossed midnight, do not leave an apparently successful trial.
        if datetime.now(TZ).date() != day:
            raise RuntimeError("Seattle midnight occurred during activation; restoring the original timer.")
        success = True
        print(
            f"\nAirflow ticket collection is armed for {day.isoformat()} only.\n"
            f"Prior attempts: {prior}; remaining daily allowance: {7 - prior}.\n"
            f"Next scheduled slot: {slot:%Y-%m-%d %H:%M %Z}.\n"
            "No source collection was triggered by this activation command.\n"
            f"The original timer returns at {midnight:%Y-%m-%d %H:%M %Z}.",
            flush=True,
        )
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
        else:
            detail = str(exc) or "Interrupted."
        print(f"\nActivation stopped: {detail}", file=sys.stderr, flush=True)
        return 1
    finally:
        if pause_attempted and not success:
            try:
                request_immediate_restore()
            except Exception as exc:
                print(
                    f"Immediate restoration could not be requested: {exc}\n"
                    f"Run: sudo python3 {BRIDGE} restore\n"
                    "The previously armed midnight restoration timer remains in place.",
                    file=sys.stderr, flush=True,
                )
        if activation_lock is not None:
            os.close(activation_lock)


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise InterruptedError(f"Received signal {signum}; activation interrupted.")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    raise SystemExit(main())
