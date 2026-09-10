#!/usr/bin/env python3
"""Install the dedicated host SSH connection; never start a source refresh."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
EXPECTED_PROJECT = Path("/home/laurawkr/homelab-airflow")
STATE = Path("/var/lib/sfz-recaps")
CONNECTION_ID = "sfz_recap_host"


def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(arguments, check=True, text=True, **kwargs)


def check_owned(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Expected an ordinary, user-owned {'directory' if directory else 'file'}: {path}")


def ensure_user_directory(path: Path, mode: int) -> None:
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=mode)
    check_owned(path, directory=True)
    path.chmod(mode)


def ensure_state_directory() -> None:
    if not STATE.exists() and not STATE.is_symlink():
        run([
            "sudo", "install", "-d", "-o", str(os.getuid()), "-g",
            str(os.getgid()), "-m", "0755", str(STATE),
        ])
    check_owned(STATE, directory=True)
    if STATE.stat().st_gid != os.getgid():
        raise RuntimeError(f"Unexpected group on {STATE}; inspect it before continuing")


def install_key() -> str:
    secrets = PROJECT / "secrets"
    ensure_user_directory(secrets, 0o700)
    private = secrets / "sfz_recap_ed25519"
    public = private.with_suffix(".pub")
    if not private.exists() and not private.is_symlink():
        if public.exists() or public.is_symlink():
            raise RuntimeError(f"Public key exists without its private key: {public}")
        run([
            "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
            "sfz-recaps-airflow", "-f", str(private),
        ], stdin=subprocess.DEVNULL)
    check_owned(private)
    private.chmod(0o600)
    derived = run(
        ["ssh-keygen", "-y", "-P", "", "-f", str(private)],
        stdin=subprocess.DEVNULL, capture_output=True,
    ).stdout.strip()
    fields = derived.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise RuntimeError("The dedicated Airflow key must be an unencrypted Ed25519 key")
    public_key = " ".join(fields[:2])
    if public.exists() or public.is_symlink():
        check_owned(public)
        if " ".join(public.read_text().split()[:2]) != public_key:
            raise RuntimeError("The dedicated private and public SSH keys do not match")
    else:
        public.write_text(public_key + " sfz-recaps-airflow\n")
    public.chmod(0o644)

    ssh_directory = Path.home() / ".ssh"
    ensure_user_directory(ssh_directory, 0o700)
    authorized = ssh_directory / "authorized_keys"
    original = ""
    if authorized.exists() or authorized.is_symlink():
        check_owned(authorized)
        original = authorized.read_text()
    line = (
        f'restrict,command="/usr/bin/python3 {HERE / "ssh_entrypoint.py"}" '
        f"{public_key} sfz-recaps-airflow"
    )
    matching = [entry for entry in original.splitlines() if fields[1] in entry.split()]
    if matching and matching != [line]:
        raise RuntimeError("This dedicated key already has different authorized_keys options; inspect that entry")
    if not matching:
        updated = original + ("\n" if original and not original.endswith("\n") else "") + line + "\n"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=ssh_directory, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            temporary.replace(authorized)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    authorized.chmod(0o600)
    return private.read_text()


def configure_connection(private_key: str) -> None:
    host_fields = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()
    if len(host_fields) < 2 or host_fields[0] != "ssh-ed25519":
        raise RuntimeError("Could not read the host's Ed25519 public key")
    payload = {
        "conn_id": CONNECTION_ID,
        "conn_type": "ssh",
        "host": "192.168.88.3",
        "login": "laurawkr",
        "port": 22,
        "extra": json.dumps({
            "private_key": private_key,
            "host_key": " ".join(host_fields[:2]),
            "no_host_key_check": False,
            "allow_host_key_change": False,
            "look_for_keys": False,
            "conn_timeout": 15,
        }),
    }
    code = r'''
import json
import logging
import sys

try:
    from airflow import settings
    from airflow.models.connection import Connection
    from airflow.utils.session import create_session
    logging.getLogger("sqlalchemy.engine").setLevel(logging.CRITICAL)
    if settings.engine is not None:
        settings.engine.hide_parameters = True
    payload = json.load(sys.stdin)
    if payload["conn_id"] != "sfz_recap_host":
        raise ValueError("Unexpected connection ID")
    with create_session() as session:
        current = session.query(Connection).filter(Connection.conn_id == payload["conn_id"]).one_or_none()
        if current is None:
            session.add(Connection(**payload))
        else:
            for name in ("conn_type", "host", "login", "port", "extra"):
                setattr(current, name, payload[name])
            current.password = None
            current.schema = None
    print("Airflow SSH connection configured.")
except Exception as exc:
    print("Connection update failed (" + type(exc).__name__ + "); details suppressed to protect the private key.", file=sys.stderr)
    raise SystemExit(1)
'''
    run(
        ["docker", "compose", "exec", "-T", "airflow-scheduler", "python", "-c", code],
        cwd=PROJECT, input=json.dumps(payload),
    )


def verify_airflow() -> None:
    run([
        "docker", "compose", "exec", "-T", "airflow-scheduler", "airflow",
        "pools", "set", "balldontlie_api", "1", "Serialize Seahawks NFL and recap source requests",
    ], cwd=PROJECT)
    code = r'''
from pathlib import Path
from airflow.dag_processing.dagbag import BundleDagBag
from airflow.providers.ssh.hooks.ssh import SSHHook

bag = BundleDagBag(dag_folder="/opt/airflow/dags/sfz_game_recaps.py", bundle_path=Path("/opt/airflow/dags"))
assert not bag.import_errors, bag.import_errors
assert "sfz_game_recaps" in bag.dags, "Recap refresh DAG was not found"
hook = SSHHook(ssh_conn_id="sfz_recap_host", cmd_timeout=120)
with hook.get_conn() as client:
    status, output, error = hook.exec_ssh_client_command(client, "check", get_pty=False, environment=None, timeout=120)
    if status:
        print(error.decode("utf-8", errors="replace"))
        raise SystemExit("Source-free SSH host check failed")
    print(output.decode("utf-8", errors="replace").strip())
print("DAG import and source-free SSH check passed. No API collection was started.")
'''
    run(["docker", "compose", "exec", "-T", "-e", "_AIRFLOW_PROCESS_CONTEXT=server", "airflow-scheduler", "python", "-c", code], cwd=PROJECT)


def main() -> int:
    account = pwd.getpwuid(os.getuid())
    if os.getuid() == 0 or account.pw_name != "laurawkr" or PROJECT != EXPECTED_PROJECT:
        raise RuntimeError("Run this as laurawkr from /home/laurawkr/homelab-airflow/deployment/recaps/install.py; do not run the installer with sudo")
    check_owned(PROJECT, directory=True)
    for name in ("refresh_recaps.py", "ssh_entrypoint.py"):
        check_owned(HERE / name)
    check_owned(PROJECT / "dags", directory=True)
    (PROJECT / "dags").chmod(0o755)
    for name in ("sfz_game_recaps.py", "sfz_recap_hook.py"):
        check_owned(PROJECT / "dags" / name)
        (PROJECT / "dags" / name).chmod(0o644)
    ensure_state_directory()
    run([sys.executable, str(HERE / "refresh_recaps.py"), "--check"])
    private_key = install_key()
    configure_connection(private_key)
    verify_airflow()
    print("Installation complete. Leave the DAG paused until you are ready for the first live refresh.")
    print("Next: follow the rollout instructions to run the first refresh and enable the schedule.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            print(f"INSTALL STOPPED: a setup command exited {exc.returncode}.", file=sys.stderr)
        else:
            print(f"INSTALL STOPPED: {exc}", file=sys.stderr)
        raise SystemExit(1)
