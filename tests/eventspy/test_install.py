"""Offline tests for service ownership and failure recovery; no Docker/systemd calls."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess

SOURCE = Path(__file__).resolve().parents[2] / "deployment/eventspy/install.py"
spec = importlib.util.spec_from_file_location("eventspy_install", SOURCE)
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

ORIGINAL = b"""[Unit]
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


class FakeHost:
    def __init__(self):
        self.commands = []
        self.fail_verify = False
        self.race_after_stop = None
        self.units = {name: {"LoadState": "loaded", "ActiveState": "inactive", "UnitFileState": "disabled"}
                      for name in (install.TIMER, install.WATCHER, install.RESTORE_TIMER,
                                   install.COLLECTOR_SERVICE, install.BRIDGE_SERVICE, install.RESTORE_SERVICE)}
        for name in (install.TIMER, install.WATCHER, install.RESTORE_TIMER):
            self.units[name].update(ActiveState="active", UnitFileState="enabled")

    def unit(self, name):
        return dict(self.units[name])

    def run(self, args):
        self.commands.append(list(args))
        if args[0] == "systemd-analyze" and self.fail_verify:
            self.fail_verify = False
            raise RuntimeError("injected unit validation failure")
        if args[-1] == "status":
            return '{"version":2,"unresolved_attempts":0}'
        return ""

    def command(self, *args):
        self.commands.append(["systemctl", *args])
        if args[0] == "daemon-reload":
            return
        name = args[-1]
        state = self.units[name]
        if args[0] in {"disable", "enable"}:
            state["UnitFileState"] = "disabled" if args[0] == "disable" else "enabled"
            if "--now" in args:
                state["ActiveState"] = "inactive" if args[0] == "disable" else "active"
        elif args[0] in {"stop", "start"}:
            state["ActiveState"] = "inactive" if args[0] == "stop" else "active"
        if self.race_after_stop and args == ("stop", install.WATCHER):
            self.units[self.race_after_stop]["ActiveState"] = "active"
            self.race_after_stop = None


class Harness(install.Installer):
    def __init__(self, directory, host):
        super().__init__(host=host, prefix=directory)
        self.seed_failure = False
        self.seed_timer_states = []
        self.sites = [{"slug": "seahawks", "eventspy": {"output_dir": "/var/lib/sfz-eventspy-mirror/dev/public"}}]
        for path in (self.unit_file.parent, self.settings.parent, self.state / "install-backups",
                     self.queue / "requests", self.code / "coverage", self.state / "state"):
            path.mkdir(parents=True, exist_ok=True)
        self.unit_file.write_bytes(ORIGINAL)
        self.source_files = {self.code / "collector.mjs": b"// new collector\n",
                             self.code / "bridge.py": b"# new bridge\n"}

    def preflight(self, filename):
        self.busy_checks()
        current = json.loads(self.settings.read_text()) if self.settings.exists() else None
        return self.sites, self.source_files, current

    def prepare_directories(self, sites):
        pass

    def seed_schedules(self, sites):
        self.seed_timer_states.append(self.host.unit(install.TIMER)["ActiveState"])
        if self.seed_failure:
            raise RuntimeError("injected API failure")

    def modular_idle(self):
        pass


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = FakeHost()
        self.installer = Harness(Path(self.temp.name), self.host)

    def test_handoff_changes_only_two_existing_unit_settings_and_keeps_activation(self):
        self.installer.install("sites.json")
        expected = ORIGINAL.replace(b"/opt/sfz-airflow-ticket-test/bridge.py", b"/opt/fanzone-eventspy/bridge.py").replace(b"ProtectHome=true", b"ProtectHome=read-only")
        self.assertEqual(self.installer.unit_file.read_bytes(), expected)
        self.assertEqual(self.installer.seed_timer_states, ["active"])
        self.assertEqual(self.host.unit(install.TIMER)["ActiveState"], "inactive")
        self.assertEqual(self.host.unit(install.RESTORE_TIMER)["UnitFileState"], "disabled")
        self.assertEqual(self.host.unit(install.WATCHER)["ActiveState"], "active")
        activation = json.loads(self.installer.settings.read_text())["activated_at"]
        original = self.installer.backup.read_bytes()
        self.installer.install("sites.json")
        self.assertEqual(json.loads(self.installer.settings.read_text())["activated_at"], activation)
        self.assertEqual(self.installer.backup.read_bytes(), original)
        self.assertFalse(any(command[-1] == install.COLLECTOR_SERVICE for command in self.host.commands))

    def test_schedule_failure_never_stops_original_timer_or_watcher(self):
        self.installer.seed_failure = True
        with self.assertRaisesRegex(RuntimeError, "API failure"):
            self.installer.install("sites.json")
        self.assertEqual(self.host.commands, [])
        self.assertEqual(self.installer.unit_file.read_bytes(), ORIGINAL)
        self.assertFalse(self.installer.settings.exists())

    def test_busy_collector_never_stops_existing_service_or_timer(self):
        self.host.units[install.COLLECTOR_SERVICE]["ActiveState"] = "active"
        with self.assertRaisesRegex(RuntimeError, "busy"):
            self.installer.install("sites.json")
        self.assertEqual(self.host.commands, [])

    def test_queue_watcher_race_restores_watcher_without_killing_worker(self):
        self.host.race_after_stop = install.BRIDGE_SERVICE
        with self.assertRaisesRegex(RuntimeError, "busy"):
            self.installer.install("sites.json")
        self.assertEqual(self.installer.unit_file.read_bytes(), ORIGINAL)
        self.assertEqual(self.host.unit(install.TIMER)["ActiveState"], "active")
        self.assertEqual(self.host.unit(install.WATCHER)["ActiveState"], "active")
        self.assertEqual(self.host.unit(install.BRIDGE_SERVICE)["ActiveState"], "active")
        self.assertNotIn(["systemctl", "disable", "--now", install.TIMER], self.host.commands)

    def test_unit_validation_failure_restores_timer_files_and_previous_code(self):
        old_code = b"# previous code retained\n"
        (self.installer.code / "bridge.py").write_bytes(old_code)
        self.host.fail_verify = True
        with self.assertRaisesRegex(RuntimeError, "unit validation"):
            self.installer.install("sites.json")
        self.assertEqual(self.installer.unit_file.read_bytes(), ORIGINAL)
        self.assertEqual((self.installer.code / "bridge.py").read_bytes(), old_code)
        self.assertFalse((self.installer.code / "collector.mjs").exists())
        self.assertFalse(self.installer.settings.exists())
        self.assertFalse(self.installer.backup.exists())
        for name in (install.TIMER, install.RESTORE_TIMER, install.WATCHER):
            self.assertEqual(self.host.unit(name)["ActiveState"], "active")
            self.assertEqual(self.host.unit(name)["UnitFileState"], "enabled")

    def test_rollback_restores_original_states_and_leaves_new_ticket_history(self):
        self.installer.install("sites.json")
        history = self.installer.state / "saved-price-history.json"
        history.write_text('{"retained":true}')
        self.installer.rollback()
        self.assertEqual(self.installer.unit_file.read_bytes(), ORIGINAL)
        self.assertFalse(self.installer.settings.exists())
        self.assertEqual(json.loads(history.read_text()), {"retained": True})
        self.assertTrue((self.installer.code / "collector.mjs").exists())
        self.assertEqual(self.host.unit(install.TIMER)["ActiveState"], "active")
        self.assertEqual(self.host.unit(install.WATCHER)["ActiveState"], "active")

    def test_queued_request_refuses_handoff(self):
        (self.installer.queue / "requests/job.json").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "queued"):
            self.installer.install("sites.json")
        self.assertEqual(self.host.commands, [])

    def test_unknown_execstart_cannot_be_overwritten(self):
        content = ORIGINAL.replace(b"/opt/sfz-airflow-ticket-test/bridge.py", b"/opt/custom/bridge.py")
        with self.assertRaisesRegex(RuntimeError, "ExecStart"):
            install.Installer.updated_unit(content)


class CommandDiagnosticsTests(unittest.TestCase):
    def test_isolated_node_validator_errors_are_visible(self):
        args = ["docker", "run", "--rm", "--read-only", "--network", "none",
                "--entrypoint", "node", "collector-image", "--check", "/review/collector.mjs"]
        detail = "Seattle schedule does not bind all 17 reviewed games: week 3, game 1392256: SCHEDULE_GAME_MISSING"
        with patch.object(install.subprocess, "run", return_value=CompletedProcess(args, 1, "", detail)):
            with self.assertRaisesRegex(RuntimeError, "week 3, game 1392256"):
                install.Host().run(args)

    def test_unrelated_command_and_environment_output_remain_suppressed(self):
        for args in (["docker", "run", "--env-file", "/production/.env", "image", "env"],
                     ["systemctl", "status", "example.service"]):
            with self.subTest(command=args[:2]):
                with patch.object(install.subprocess, "run", return_value=CompletedProcess(
                        args, 1, "BALLDONTLIE_API_KEY=do-not-print", "secret stderr")):
                    with self.assertRaises(RuntimeError) as error:
                        install.Host().run(args)
                self.assertEqual(str(error.exception), f"Command failed (exit 1): {args[0]} {args[1]}")


class LocalNodeHost:
    """Execute the same pure Node validation using local fixture mount sources."""
    def run(self, args):
        import subprocess
        mounts = {}
        for index, value in enumerate(args):
            if value == "--mount":
                options = dict(part.split("=", 1) for part in args[index + 1].split(",") if "=" in part)
                mounts[options["dst"]] = options["src"]
        program = args.index("-e")
        translated = args[program + 3:]
        for index, value in enumerate(translated):
            for target, source in mounts.items():
                if value == target or value.startswith(target + "/"):
                    translated[index] = source + value[len(target):]
                    break
        result = subprocess.run(["node", "--input-type=module", "-e", args[program + 1], "--", *translated],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("Seattle schedule binding failed\n" + result.stderr)
        return result.stdout


class SeattleSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.installer = install.Installer(host=LocalNodeHost(), prefix=root)
        self.coverage = json.loads((SOURCE.parent / "coverage/seahawks.json").read_text())
        self.snapshot = root / "var/lib/sfz-nfl/runs/example/snapshot"
        self.snapshot.mkdir(parents=True)
        (root / "var/lib/sfz-nfl/current").symlink_to(self.snapshot, target_is_directory=True)
        self.file = self.snapshot / "seahawks.json"
        self.site = {"slug": "seahawks", "abbreviation": "SEA", "city": "Seattle", "name": "Seahawks",
                     "eventspy": {"schedule_file": "/var/lib/sfz-nfl/current/seahawks.json", "coverage_file": "seahawks.json"}}
        self.payload = {"season": 2026, "fixture": False, "team": {"abbreviation": "SEA"},
                        "gamesRegular": [{"id": int(row["gameId"]), "season": row["season"], "week": row["week"],
                                          "season_type": "regular", "home_team": {"abbreviation": row["homeTeamAbbreviation"]},
                                          "visitor_team": {"abbreviation": row["awayTeamAbbreviation"]}}
                                         for row in self.coverage]}
        # The existing NFL normalizer includes its synthetic bye in gamesRegular.
        self.bye = {"id": "2026-regular-11-bye", "season": 2026, "week": 11,
                    "phase": "regular", "season_type": "regular", "state": "bye",
                    "status": "bye", "bye": True, "homeTeam": None, "awayTeam": None}
        self.write()

    def write(self):
        import hashlib
        self.file.write_text(json.dumps(self.payload))
        (self.snapshot / "manifest.json").write_text(json.dumps({"schema_version": 1, "files": {
            "seahawks.json": hashlib.sha256(self.file.read_bytes()).hexdigest()}}))

    @unittest.skipUnless(shutil.which("node"), "Node fixture test; host preflight always validates bindings in the collector image")
    def test_symlink_snapshot_with_real_binder_is_read_only(self):
        before = {path.name: path.read_bytes() for path in self.snapshot.iterdir()}
        self.installer.check_seattle_schedule(self.site)
        self.assertEqual({path.name: path.read_bytes() for path in self.snapshot.iterdir()}, before)

    @unittest.skipUnless(shutil.which("node"), "Node fixture test; host preflight always validates bindings in the collector image")
    def test_normalized_snapshot_with_bye_binds_all_games_without_changing_files(self):
        self.payload["gamesRegular"].append(self.bye)
        self.write()
        self.assertEqual(len(self.payload["gamesRegular"]), 18)
        before = {path.name: path.read_bytes() for path in self.snapshot.iterdir()}
        self.installer.check_seattle_schedule(self.site)
        self.assertEqual({path.name: path.read_bytes() for path in self.snapshot.iterdir()}, before)

    @unittest.skipUnless(shutil.which("node"), "Node fixture test; host preflight always validates bindings in the collector image")
    def test_recorded_server_snapshot_with_wsh_and_bye_passes_real_schedule_check(self):
        # Recorded game IDs and abbreviations are independent of the coverage table.
        fixture = Path(__file__).parent / "fixtures/seattle-2026-recorded-identity.json"
        self.payload = json.loads(fixture.read_text())
        self.write()
        self.assertEqual(len(self.payload["gamesRegular"]), 18)
        self.assertEqual(sum(row.get("bye") is not True for row in self.payload["gamesRegular"]), 17)
        self.assertEqual(next(row for row in self.payload["gamesRegular"] if row["week"] == 3)["homeTeam"]["abbreviation"], "WSH")
        before = {path.name: path.read_bytes() for path in self.snapshot.iterdir()}
        self.installer.check_seattle_schedule(self.site)
        self.assertEqual({path.name: path.read_bytes() for path in self.snapshot.iterdir()}, before)

    @unittest.skipUnless(shutil.which("node"), "Node fixture test; host preflight always validates bindings in the collector image")
    def test_real_schedule_check_names_wrong_week_and_game_id(self):
        fixture = Path(__file__).parent / "fixtures/seattle-2026-recorded-identity.json"
        self.payload = json.loads(fixture.read_text())
        game = next(row for row in self.payload["gamesRegular"] if row["week"] == 3)
        game["id"] = "1392257"
        self.write()
        with self.assertRaisesRegex(RuntimeError, "week 3, game 1392256: SCHEDULE_GAME_MISSING"):
            self.installer.check_seattle_schedule(self.site)

    def test_bye_cannot_replace_a_missing_real_game(self):
        self.payload["gamesRegular"] = self.payload["gamesRegular"][1:] + [self.bye]
        self.write()
        self.assertEqual(len(self.payload["gamesRegular"]), 17)
        host = FakeHost()
        self.installer.host = host
        with self.assertRaises(RuntimeError):
            self.installer.check_seattle_schedule(self.site)
        self.assertEqual(host.commands, [])

    @unittest.skipUnless(shutil.which("node"), "Node fixture test; host preflight always validates bindings in the collector image")
    def test_wrong_game_id_year_week_and_opponent_are_rejected(self):
        import copy
        valid = copy.deepcopy(self.payload)
        for field, value in (("id", 999), ("season", 2025), ("week", 18),
                             ("home_team", {"abbreviation": "DEN"})):
            with self.subTest(field=field):
                self.payload = copy.deepcopy(valid)
                self.payload["gamesRegular"][0][field] = value
                self.write()
                with self.assertRaisesRegex(RuntimeError, "binding"):
                    self.installer.check_seattle_schedule(self.site)

    def test_foreign_fixture_and_incomplete_snapshot_are_rejected(self):
        import copy
        valid = copy.deepcopy(self.payload)
        for change in ({"team": {"abbreviation": "DEN"}}, {"fixture": True}, {"gamesRegular": []}):
            with self.subTest(change=change):
                self.payload = {**valid, **change}
                self.write()
                with self.assertRaises(RuntimeError):
                    self.installer.check_seattle_schedule(self.site)

    def test_manifest_mismatch_is_rejected(self):
        self.file.write_text(self.file.read_text() + "\n")
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            self.installer.check_seattle_schedule(self.site)

    def test_missing_and_escaping_snapshot_are_rejected(self):
        self.file.unlink()
        with self.assertRaises(FileNotFoundError):
            self.installer.check_seattle_schedule(self.site)
        outside = Path(self.temp.name) / "foreign.json"
        outside.write_text(json.dumps(self.payload))
        self.file.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "inside"):
            self.installer.check_seattle_schedule(self.site)


if __name__ == "__main__":
    unittest.main()
