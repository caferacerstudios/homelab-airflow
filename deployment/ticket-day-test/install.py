#!/usr/bin/env python3
"""Install the day-test integration; never pause or start the source collector."""
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PROJECT = Path("/home/laurawkr/homelab-airflow")
HOST = Path("/opt/sfz-airflow-ticket-test")
STATE = Path("/var/lib/sfz-airflow-ticket-test")
BRIDGE_UNIT = "sfz-airflow-ticket-test"
FILES = ["bridge.py", "activate.py", "sfz_ticket_bridge.py", "sfz_eventspy_collect.py"]
COMPOSE = ["docker", "compose"]


def run(args, *, capture=False, cwd=None):
    return subprocess.run(args, check=True, text=True, cwd=cwd,
                          stdout=subprocess.PIPE if capture else None).stdout


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def compatible(target, content):
    require(not target.is_symlink(), f"Refusing symlink: {target}")
    require(not target.exists() or target.read_bytes() == content,
            f"Existing file differs; kept unchanged: {target}. Share this error before merging it.")


def write_fixed(target, content, mode=0o644):
    compatible(target, content)
    target.write_bytes(content)
    target.chmod(mode)


def root_install():
    require(os.geteuid() == 0, "Host installation needs sudo.")
    # Airflow owns only the request leaf, never either root parent or responses.
    for folder, mode, uid in [(HOST, 0o755, 0), (STATE, 0o755, 0),
                              (STATE / "requests", 0o750, 50000),
                              (STATE / "responses", 0o755, 0),
                              (STATE / "state", 0o700, 0)]:
        require(not folder.is_symlink(), f"Refusing symlink: {folder}")
        folder.mkdir(exist_ok=True)
        folder.chmod(mode)
        os.chown(folder, uid, 0)
    for filename in ("bridge.py", "activate.py"):
        write_fixed(HOST / filename, (HERE / filename).read_bytes())
    service = b"""[Unit]
Description=Process fixed Airflow ticket collector requests
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/sfz-airflow-ticket-test/bridge.py process
TimeoutStartSec=70min
UMask=0022
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
"""
    path = b"""[Unit]
Description=Watch Airflow ticket collector request queue

[Path]
DirectoryNotEmpty=/var/lib/sfz-airflow-ticket-test/requests
Unit=sfz-airflow-ticket-test.service

[Install]
WantedBy=multi-user.target
"""
    for suffix, content in [("service", service), ("path", path)]:
        write_fixed(Path(f"/etc/systemd/system/{BRIDGE_UNIT}.{suffix}"), content)
    run(["/usr/bin/python3", str(HOST / "bridge.py"), "init"])
    run(["systemd-analyze", "verify", f"/etc/systemd/system/{BRIDGE_UNIT}.service",
         f"/etc/systemd/system/{BRIDGE_UNIT}.path"])
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", f"{BRIDGE_UNIT}.path"])


def main():
    if sys.argv[1:] == ["--host"]:
        root_install()
        return
    require(not sys.argv[1:], "Usage: python3 install.py")
    require(os.geteuid() != 0, "Run as laurawkr; installer requests sudo only for host files.")
    require(Path.home() == PROJECT.parent, "Run this on wkr as laurawkr.")
    require((PROJECT / "compose.yaml").is_file(), "Expected ~/homelab-airflow/compose.yaml.")
    require(not os.environ.get("COMPOSE_FILE"), "Unset COMPOSE_FILE before installing.")
    if (HOST / "bridge.py").exists():
        status = json.loads(run(["sudo", "/usr/bin/python3", str(HOST / "bridge.py"),
                                 "status"], capture=True))
        require(not status.get("armed") and not status.get("unresolved_attempts"),
                "A trial is armed or unresolved. Inspect status; do not reinstall during the test.")
    for filename in FILES:
        compile((HERE / filename).read_text(), filename, "exec")
    for filename in ("compose.override.yml", "docker-compose.override.yml", "docker-compose.override.yaml"):
        require(not (PROJECT / filename).exists(), f"Existing override needs a merge: {filename}")
    overlay = (HERE / "compose.override.yaml").read_bytes()
    compatible(PROJECT / "compose.override.yaml", overlay)
    for filename in ("sfz_ticket_bridge.py", "sfz_eventspy_collect.py"):
        compatible(PROJECT / "dags" / filename, (HERE / filename).read_bytes())
    # Never display expanded Compose configuration: it can contain credentials.
    config = json.loads(run(COMPOSE + ["config", "--format", "json"], capture=True, cwd=PROJECT))
    scheduler = config["services"]["airflow-scheduler"]
    require(str(scheduler.get("user", "")).split(":")[0] == "50000",
            "This package expects Airflow UID 50000. Share the mismatch before changing ownership.")
    mounts = {v["target"]: v.get("source") for v in scheduler.get("volumes", [])}
    require(mounts.get("/opt/airflow/dags") == str(PROJECT / "dags"),
            "Scheduler DAG mount differs from ~/homelab-airflow/dags.")
    require(mounts.get("/opt/airflow/artifacts") == "/var/lib/homelab-pipelines",
            "Expected shared artifacts mount /var/lib/homelab-pipelines.")
    executor = run(COMPOSE + ["exec", "-T", "airflow-scheduler", "airflow", "config",
                             "get-value", "core", "executor"], capture=True, cwd=PROJECT)
    require(executor.strip().splitlines()[-1] == "LocalExecutor", "This package targets LocalExecutor.")
    run(["sudo", "-v"])
    run(["sudo", "/usr/bin/python3", str(HERE / "install.py"), "--host"])
    write_fixed(PROJECT / "compose.override.yaml", overlay)
    (PROJECT / "dags").chmod(0o755)
    for filename in ("sfz_ticket_bridge.py", "sfz_eventspy_collect.py"):
        write_fixed(PROJECT / "dags" / filename, (HERE / filename).read_bytes())
    run(COMPOSE + ["config", "--quiet"], cwd=PROJECT)
    print("Adding queue mounts to the scheduler. The source collector timer is unchanged.", flush=True)
    run(COMPOSE + ["up", "-d", "--wait", "airflow-scheduler"], cwd=PROJECT)
    run(COMPOSE + ["exec", "-T", "airflow-scheduler", "airflow", "pools", "set",
                   "eventspy_source", "1", "One EventSpy season collection at a time"], cwd=PROJECT)
    validation = (
        "from pathlib import Path; from airflow.dag_processing.dagbag import BundleDagBag; "
        "b=BundleDagBag(dag_folder='/opt/airflow/dags/sfz_eventspy_collect.py',bundle_path=Path('/opt/airflow/dags')); "
        "assert not b.import_errors, b.import_errors; "
        "assert {'sfz_eventspy_collect','sfz_ticket_bridge_smoke'} <= set(b.dags); "
        "print('Both ticket DAGs import successfully')"
    )
    run(COMPOSE + ["exec", "-T", "airflow-scheduler", "python", "-c", validation], cwd=PROJECT)
    run(COMPOSE + ["restart", "airflow-dag-processor"], cwd=PROJECT)
    print("Running the source-free smoke DAG. This does not call EventSpy.", flush=True)
    run(COMPOSE + ["exec", "-T", "airflow-scheduler", "airflow", "dags", "test",
                   "sfz_ticket_bridge_smoke", "--dagfile-path",
                   "/opt/airflow/dags/sfz_eventspy_collect.py"], cwd=PROJECT)
    run(COMPOSE + ["exec", "-T", "airflow-scheduler", "airflow", "dags", "list"], cwd=PROJECT)
    print("INSTALL COMPLETE. The existing collector timer is still in charge.")
    print("Next: sudo python3 /opt/sfz-airflow-ticket-test/activate.py")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, KeyError) as exc:
        print(f"INSTALL STOPPED: {exc}", file=sys.stderr)
        print("The installer does not disable the original collector timer.", file=sys.stderr)
        sys.exit(1)
