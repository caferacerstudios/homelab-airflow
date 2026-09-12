"""Host-free tests: locks, local edits and failed validation cannot overwrite the collector."""
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "deployment/eventspy/update_collector.py"
spec = importlib.util.spec_from_file_location("update_eventspy_collector", SOURCE)
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)
OLD, NEW = b"// reviewed old collector\n", b"// reviewed fixed collector\n"
SUMMARY = {"status": "ready", "gameId": "1392253", "schemaVersion": "1.1.0",
           "providers": ["ticketmaster", "stubhub", "vividseats"],
           "missingProviders": ["seatgeek"], "historyPoints": 300}


class UpdateCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.code, self.state, self.here = (self.root / name for name in ("install", "state", "source"))
        for directory in (self.code / "coverage", self.state / "state", self.state / "install-backups",
                          self.state / "schedules", self.state / "cache/2026-09-12", self.here):
            directory.mkdir(parents=True, exist_ok=True)
        self.target = self.code / "collector.mjs"
        self.target.write_bytes(OLD)
        self.target.chmod(0o644)
        (self.here / "collector.mjs").write_bytes(NEW)
        (self.code / "collector-coverage.mjs").write_text("// installed helper\n")
        (self.code / "coverage/chiefs.json").write_text("[]")
        (self.code / "settings.json").write_text(json.dumps({"version": 2, "image_id": update.IMAGE_ID}))
        (self.code / "settings.json").chmod(0o600)
        (self.state / "schedules/chiefs.json").write_text("{}")
        (self.state / update.CACHE_RELATIVE).write_text("{}")
        (self.state / "install.lock").touch(mode=0o600)
        self.bridge_lock = self.state / "state/bridge.lock"
        self.bridge_lock.touch(mode=0o600)
        self.ledger = self.state / "state/ledger.sqlite3"
        with contextlib.closing(sqlite3.connect(self.ledger)) as db:
            db.execute("CREATE TABLE attempts (state TEXT)")
            db.execute("INSERT INTO attempts VALUES ('finished')")
            db.commit()
        for name, value in {"INSTALL": self.code, "STATE": self.state, "HERE": self.here,
                            "BASELINE_SHA256": update.digest(OLD), "OWNER_UID": os.getuid()}.items():
            context = patch.object(update, name, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(update.os, "geteuid", return_value=0)
        context.start()
        self.addCleanup(context.stop)
        context = patch.object(update.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(SUMMARY), ""))
        self.docker = context.start()
        self.addCleanup(context.stop)

    def contents(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_check_is_read_only_and_docker_has_no_network_or_writable_mount(self):
        before = self.contents()
        update.update()
        self.assertEqual(self.contents(), before)
        args = self.docker.call_args.args[0]
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--read-only", args)
        self.assertIn("--pull=never", args)
        self.assertNotIn("--env", args)
        self.assertNotIn("--env-file", args)
        mounts = [args[index + 1] for index, part in enumerate(args) if part == "--mount"]
        self.assertEqual(len(mounts), 5)
        self.assertTrue(all(value.endswith(",readonly") for value in mounts))
        self.assertIn(update.IMAGE_ID, args)

    def test_busy_bridge_or_installer_stops_without_docker_or_changes(self):
        for lock in (self.bridge_lock, self.state / "install.lock"):
            with self.subTest(lock=lock.name), lock.open("r+") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                before = self.contents()
                with self.assertRaisesRegex(RuntimeError, "running"):
                    update.update(install=True)
                self.assertEqual(self.contents(), before)
        self.docker.assert_not_called()

    def test_pending_ledger_stops_without_modifying_receipts_or_reservations(self):
        with contextlib.closing(sqlite3.connect(self.ledger)) as db:
            db.execute("INSERT INTO attempts VALUES ('pending')")
            db.commit()
        before = self.contents()
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            update.update(install=True)
        self.assertEqual(self.contents(), before)
        self.docker.assert_not_called()

    def test_unrecognized_local_collector_is_not_overwritten(self):
        self.target.write_bytes(b"// user edit\n")
        before = self.contents()
        with self.assertRaisesRegex(RuntimeError, "local changes"):
            update.update(install=True)
        self.assertEqual(self.contents(), before)
        self.docker.assert_not_called()

    def test_failed_real_payload_validation_does_not_write_backup_or_collector(self):
        self.docker.return_value = subprocess.CompletedProcess([], 1, "", "private provider value")
        before = self.contents()
        with self.assertRaisesRegex(RuntimeError, "Offline cached Chiefs validation failed"):
            update.update(install=True)
        self.assertEqual(self.contents(), before)

    def test_install_atomic_replace_keeps_open_reader_old_inode_and_all_other_inputs(self):
        before = self.contents()
        with self.target.open("rb") as old_reader:
            update.update(install=True)
            self.assertEqual(old_reader.read(), OLD)
        self.assertEqual(self.target.read_bytes(), NEW)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)
        backup = self.state / "install-backups" / f"collector-before-missing-links-{update.digest(OLD)}.mjs"
        self.assertEqual(backup.read_bytes(), OLD)
        after = self.contents()
        after.pop(str(backup.relative_to(self.root)))
        after[str(self.target.relative_to(self.root))] = OLD
        self.assertEqual(after, before)
        already_installed = self.contents()
        update.update(install=True)
        self.assertEqual(self.contents(), already_installed)

    def test_failed_atomic_replace_preserves_previous_collector_and_cleans_temporary(self):
        with patch.object(update.os, "replace", side_effect=OSError("injected failure")):
            with self.assertRaisesRegex(OSError, "injected failure"):
                update.update(install=True)
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertFalse(list(self.code.glob(".collector-update-*")))

    def test_changed_candidate_during_validation_is_not_installed(self):
        def changed(*args, **kwargs):
            (self.here / "collector.mjs").write_bytes(b"// concurrent edit\n")
            return subprocess.CompletedProcess([], 0, json.dumps(SUMMARY), "")
        self.docker.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, "Candidate changed"):
            update.update(install=True)
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertFalse(list((self.state / "install-backups").iterdir()))


if __name__ == "__main__":
    unittest.main()
