"""Offline tests: no Docker daemon or API credential is used."""

import hashlib
import fcntl
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
from datetime import datetime, timezone, timedelta


MODULE_PATH = Path(__file__).resolve().parents[1] / "deployment/nfl/refresh_nfl.py"
spec = importlib.util.spec_from_file_location("refresh_nfl", MODULE_PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        for folder in ("scripts", "src/lib", "src/data/team", "src/data/nfl"):
            (self.source / folder).mkdir(parents=True, exist_ok=True)
        (self.source / "scripts/fetch-nfl.mjs").write_text("// NFL_FETCH_STRICT NFL_FETCH_REPORT\n")
        (self.source / "scripts/nfl-http.mjs").write_text("// helper\n")
        (self.source / "src/lib/schedule.mjs").write_text("// helper\n")
        (self.source / "src/data/team/roster.json").write_text('{"players": []}')
        self.write(self.source / "src/data/nfl/gameRecaps.json", {"recaps": {"game": {"title": "Keep this article"}}})
        self.patch = patch.object(runner, "preflight", return_value=("a" * 40, "test-key"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.commit_patch = patch.object(runner, "source_commit", return_value="a" * 40)
        self.commit_patch.start()
        self.addCleanup(self.commit_patch.stop)

    @staticmethod
    def write(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    def fetched(self, work, _key, _run_key):
        now = datetime.now(timezone.utc).isoformat()
        common = {"season": 2026, "playerStatsSeason": 2026, "updatedAt": now,
                  "team": {"abbreviation": "SEA", "id": 31}, "fixture": False,
                  "currentRoster": [], "playerDirectory": [], "playerSeasonStats": [{"player_id": 1}]}
        data = work / "src/data/nfl"
        self.write(data / "seahawks.json", {**common, "games": [{"id": 1}], "gamesRegular": [{"id": 1}], "gamesPreseason": [], "gamesPostseason": []})
        self.write(data / "players.json", common)
        self.write(data / "standings.json", {"season": 2026, "updatedAt": now, "phases": {phase: {"phase": phase, "rows": []} for phase in ("preseason", "regular", "postseason")}})
        self.write(work / "fetch-report.json", {"status": "success", "updatedAt": now, "season": 2026, "playerStatsSeason": 2026, "requestCount": 8})

    def collect(self, run_id):
        return runner.collect(run_id, self.source, self.runtime)

    def test_success_publishes_coherent_files_without_touching_source(self):
        before = {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        with patch.object(runner, "run_node", side_effect=self.fetched):
            receipt = self.collect("scheduled__first")
        current = (self.runtime / "current").resolve()
        self.assertEqual(current, Path(receipt["snapshotPath"]))
        for name, digest in receipt["files"].items():
            self.assertEqual(hashlib.sha256((current / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(receipt["requestCount"], 8)
        self.assertIn("gameRecaps.json", receipt["files"])
        self.assertEqual(before, {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()})

    def test_successful_same_run_never_refetches_or_rolls_back_newer_current(self):
        with patch.object(runner, "run_node", side_effect=self.fetched) as node:
            first = self.collect("first")
            self.collect("second")
            newer = (self.runtime / "current").resolve()
            repeated = self.collect("first")
        self.assertEqual(node.call_count, 2)
        self.assertEqual(repeated, first)
        self.assertEqual((self.runtime / "current").resolve(), newer)

    def test_failed_fetch_preserves_current_and_retains_work(self):
        with patch.object(runner, "run_node", side_effect=self.fetched):
            self.collect("good")
        previous = (self.runtime / "current").resolve()
        with patch.object(runner, "run_node", side_effect=RuntimeError("HTTP 429")):
            with self.assertRaisesRegex(RuntimeError, "429"):
                self.collect("bad")
        self.assertEqual((self.runtime / "current").resolve(), previous)
        failed = self.runtime / "runs" / hashlib.sha256(b"bad").hexdigest()
        self.assertTrue(list(failed.glob("work-*")))
        self.assertFalse((failed / "snapshot").exists())

    def test_partial_or_stale_outputs_do_not_publish(self):
        with patch.object(runner, "run_node", side_effect=self.fetched):
            self.collect("good")
        previous = (self.runtime / "current").resolve()
        def stale(work, key, run_key):
            self.fetched(work, key, run_key)
            players = runner.load_object(work / "src/data/nfl/players.json")
            players["updatedAt"] = "2001-01-01T00:00:00Z"
            self.write(work / "src/data/nfl/players.json", players)
        with patch.object(runner, "run_node", side_effect=stale):
            with self.assertRaisesRegex(ValueError, "Freshness"):
                self.collect("stale")
        self.assertEqual((self.runtime / "current").resolve(), previous)

    def test_corrupt_cached_file_fails_without_an_api_call(self):
        with patch.object(runner, "run_node", side_effect=self.fetched):
            receipt = self.collect("good")
        (Path(receipt["snapshotPath"]) / "players.json").write_text("{}")
        with patch.object(runner, "run_node") as node:
            with self.assertRaisesRegex(ValueError, "checksum"):
                self.collect("good")
            node.assert_not_called()

    def test_concurrent_run_is_refused_without_an_api_call(self):
        with (self.runtime / "collector.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(runner, "run_node") as node:
                with self.assertRaisesRegex(RuntimeError, "Another NFL collection"):
                    self.collect("overlapping")
                node.assert_not_called()

    def test_interrupted_publication_reuses_snapshot_without_refetching(self):
        with patch.object(runner, "run_node", side_effect=self.fetched):
            with patch.object(runner, "publish_link", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    self.collect("publish-on-retry")
        with patch.object(runner, "run_node") as node:
            receipt = self.collect("publish-on-retry")
            node.assert_not_called()
        self.assertEqual((self.runtime / "current").resolve(), Path(receipt["snapshotPath"]))

    def test_dotenv_reads_only_literal_key_and_never_executes_shell(self):
        env = self.source / ".env"
        marker = self.root / "executed"
        env.write_text(f'UNRELATED=$(touch {marker})\nexport BALLDONTLIE_API_KEY="test-key" # comment\n')
        self.assertEqual(runner.read_api_key(env), "test-key")
        self.assertFalse(marker.exists())
        env.write_text(f'BALLDONTLIE_API_KEY=$(touch {marker})\n')
        with self.assertRaises(ValueError):
            runner.read_api_key(env)
        self.assertFalse(marker.exists())
        env.write_text("BALLDONTLIE_API_KEY=first\nBALLDONTLIE_API_KEY=second\n")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            runner.read_api_key(env)

    def test_docker_uses_env_name_and_removes_only_owned_container_on_timeout(self):
        process = Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired("docker", 3600), ("", None)]
        process.poll.return_value = None
        with patch.object(runner, "command", return_value=""), patch.object(runner.subprocess, "Popen", return_value=process) as popen, patch.object(runner.subprocess, "run") as cleanup:
            with self.assertRaises(TimeoutError):
                runner.run_node(self.root, "secret-value", "a" * 64)
        args = popen.call_args.args[0]
        self.assertNotIn("secret-value", args)
        self.assertIn("BALLDONTLIE_API_KEY", args)
        self.assertEqual(popen.call_args.kwargs["env"]["BALLDONTLIE_API_KEY"], "secret-value")
        name = args[args.index("--name") + 1]
        self.assertEqual(cleanup.call_args.args[0], ["docker", "rm", "--force", name])
        process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
