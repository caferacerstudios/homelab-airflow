"""Offline checks of publication, input isolation, and credentials."""

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "refresh_recaps", Path(__file__).parents[1] / "deployment/recaps/refresh_recaps.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def complete(text):
    return {"segments": [{"t": "text", "v": text}], "bullets": [text]}


class RefreshRecapsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "src/lib").mkdir(parents=True)
        (self.source / "scripts/generate-game-recaps.mjs").write_text("// RECAP_GENERATION_REPORT\n")
        (self.source / "scripts/import-recap-snapshot.mjs").write_text("// importer\n")
        (self.source / ".env").write_text("BALLDONTLIE_API_KEY=bdl-secret\nOPENAI_API_KEY=ai-secret\n")
        self.keys = {"BALLDONTLIE_API_KEY": "bdl-secret", "OPENAI_API_KEY": "ai-secret"}
        self.nfl_snapshot = self.root / "nfl/runs/source-one/snapshot"
        self.nfl_current = self.root / "nfl/current"
        self.make_nfl(self.nfl_snapshot, "source-one")
        self.nfl_current.symlink_to(self.nfl_snapshot)
        self.commit_patch = patch.object(runner, "source_commit", return_value="a" * 40)
        self.commit_patch.start()
        self.addCleanup(self.commit_patch.stop)
        self.command_patch = patch.object(runner, "command", return_value="")
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    def make_nfl(self, path, run_id):
        updated = "2026-09-10T07:47:08.367Z"
        write_json(path / "seahawks.json", {
            "season": 2026, "updatedAt": updated, "team": {"id": 31, "abbreviation": "SEA"},
            "gamesRegular": [], "gamesPostseason": [],
        })
        write_json(path / "manifest.json", {
            "schema_version": 1, "runId": run_id, "season": 2026, "updatedAt": updated,
            "files": {"seahawks.json": runner.file_hash(path / "seahawks.json")},
        })

    def node_success(self, work, _keys, _run_key):
        data_path = work / "src/data/nfl/gameRecaps.json"
        data = json.loads(data_path.read_text())
        data["updatedAt"] = datetime.now(timezone.utc).isoformat()
        write_json(data_path, data)
        write_json(work / "recap-report.json", {
            "schema_version": 1, "status": "success", "season": 2026,
            "updatedAt": data["updatedAt"], "generatedCount": 0, "generatedGameIds": [],
            "requestCount": 0, "openaiRequestCount": 0, "model": "gpt-4o-mini",
        })

    def collect(self, run_id):
        return runner.collect(run_id, self.source, self.runtime, self.nfl_current)

    def test_noop_publishes_snapshot_then_replay_avoids_calls_and_rollback(self):
        with patch.object(runner, "run_node", side_effect=self.node_success) as node:
            first = self.collect("first")
            second = self.collect("second")
        self.assertEqual(node.call_count, 2)
        self.assertEqual(first["generatedCount"], 0)
        self.assertEqual(first["nflSourceRunId"], "source-one")
        self.assertEqual(first["files"], {"gameRecaps.json": runner.file_hash(Path(first["snapshotPath"]) / "gameRecaps.json")})
        with patch.object(runner, "preflight", side_effect=AssertionError("replay must not preflight")), \
                patch.object(runner, "run_node", side_effect=AssertionError("replay must not call APIs")):
            replay = self.collect("first")
        self.assertEqual(replay, first)
        self.assertEqual((self.runtime / "current").resolve(), Path(second["snapshotPath"]))

    def test_failed_generator_preserves_previous_publication(self):
        with patch.object(runner, "run_node", side_effect=self.node_success):
            first = self.collect("first")
        with patch.object(runner, "run_node", side_effect=RuntimeError("API request failed")):
            with self.assertRaisesRegex(RuntimeError, "API request failed"):
                self.collect("failed")
        self.assertEqual((self.runtime / "current").resolve(), Path(first["snapshotPath"]))
        failed_dir = self.runtime / "runs" / hashlib.sha256(b"failed").hexdigest()
        self.assertFalse((failed_dir / "snapshot").exists())

    def test_pins_nfl_input_and_preserves_historical_and_authored_recaps(self):
        write_json(self.source / "src/data/nfl/gameRecaps.json", {
            "season": 2025, "recaps": {"historic": complete("History"), "edited": complete("Original")},
        })
        with patch.object(runner, "run_node", side_effect=self.node_success):
            self.collect("first")
        write_json(self.source / "src/data/nfl/gameRecaps.json", {
            "season": 2026, "recaps": {"edited": complete("Authored edit"), "new": complete("New authored")},
        })
        selected, manifest = runner.select_nfl_snapshot(self.nfl_current)
        other = self.root / "nfl/runs/source-two/snapshot"
        self.make_nfl(other, "source-two")
        self.nfl_current.unlink()
        self.nfl_current.symlink_to(other)
        work = self.root / "staging"
        seed = runner.stage_source(self.source, work, self.runtime / "current", selected, manifest["season"])
        self.assertEqual(set(seed["recaps"]), {"historic", "edited", "new"})
        self.assertEqual(seed["recaps"]["edited"], complete("Authored edit"))
        self.assertEqual(selected, self.nfl_snapshot)
        self.assertEqual(manifest["runId"], "source-one")
        self.assertEqual((work / "src/data/nfl/seahawks.json").read_bytes(), (selected / "seahawks.json").read_bytes())

    def test_output_cannot_drop_historical_recaps(self):
        write_json(self.source / "src/data/nfl/gameRecaps.json", {
            "season": 2025, "recaps": {"historic": complete("History")},
        })
        def dropping_node(work, keys, run_key):
            self.node_success(work, keys, run_key)
            path = work / "src/data/nfl/gameRecaps.json"
            data = json.loads(path.read_text())
            data["recaps"] = {}
            write_json(path, data)
        with patch.object(runner, "run_node", side_effect=dropping_node):
            with self.assertRaisesRegex(ValueError, "removed historical"):
                self.collect("bad-output")
        self.assertFalse((self.runtime / "current").exists())

    def test_nfl_checksum_and_identity_are_checked_before_generation(self):
        path = self.nfl_snapshot / "seahawks.json"
        data = json.loads(path.read_text())
        data["team"]["abbreviation"] = "ARI"
        write_json(path, data)
        with patch.object(runner, "run_node") as node:
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                self.collect("bad-checksum")
            node.assert_not_called()
        manifest_path = self.nfl_snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["seahawks.json"] = runner.file_hash(path)
        write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "identify the Seahawks"):
            runner.select_nfl_snapshot(self.nfl_current)

    def test_both_keys_are_literal_and_errors_do_not_expose_values(self):
        path = self.source / ".env"
        path.write_text('\ufeffexport BALLDONTLIE_API_KEY="bdl-secret" # key\nOPENAI_API_KEY=ai-secret # key\n')
        self.assertEqual(runner.read_api_keys(path), self.keys)
        for contents in (
            "BALLDONTLIE_API_KEY=bdl-secret\n",
            "BALLDONTLIE_API_KEY=bdl-secret\nOPENAI_API_KEY=ai-secret\nOPENAI_API_KEY=duplicate-secret\n",
            "BALLDONTLIE_API_KEY=bdl-secret\nOPENAI_API_KEY=$(secret-command)\n",
        ):
            path.write_text(contents)
            with self.assertRaises(ValueError) as caught:
                runner.read_api_keys(path)
            self.assertNotIn("secret", str(caught.exception))

    def test_docker_passes_key_names_and_redacts_logged_output(self):
        write_json(self.root / "src/data/nfl/seahawks.json", {"team": {"id": 31}})
        process = Mock(returncode=0)
        process.communicate.return_value = ("bdl-secret and ai-secret were passed", None)
        with patch.object(runner.subprocess, "Popen", return_value=process) as popen:
            runner.run_node(self.root, self.keys, "run-hash")
        args = popen.call_args.args[0]
        self.assertNotIn("bdl-secret", " ".join(args))
        self.assertNotIn("ai-secret", " ".join(args))
        self.assertIn("OPENAI_API_KEY", args)
        self.assertIn("NFL_REQUEST_INTERVAL_MS=15000", args)
        self.assertEqual(popen.call_args.kwargs["env"]["OPENAI_API_KEY"], "ai-secret")
        log = (self.root / "collector.log").read_text()
        self.assertEqual(log, "[REDACTED] and [REDACTED] were passed")



class ModularRefreshRecapsTests(unittest.TestCase):
    setUp = RefreshRecapsTests.setUp
    make_nfl = RefreshRecapsTests.make_nfl
    def sites(self):
        return json.loads((Path(__file__).parents[1] / 'config/active-sites.json').read_text())

    def make_team_nfl(self, site, path):
        updated = '2026-09-12T04:00:00Z'
        data = {'season': 2026, 'updatedAt': updated,
                'team': {'id': site['balldontlie_team_id'] or 99, 'abbreviation': site['abbreviation']},
                'gamesRegular': [], 'gamesPostseason': []}
        filename = site['slug'] + '.json'
        write_json(path / filename, data)
        write_json(path / 'manifest.json', {'schema_version': 1, 'team': site['slug'], 'runId': 'nfl-fixture',
                   'season': 2026, 'updatedAt': updated, 'files': {filename: runner.file_hash(path / filename)}})

    @unittest.skipUnless(shutil.which("node"), "Real writer integration runs separately in Node; Airflow images need no Node runtime")
    def test_team_snapshots_and_same_run_receipts_are_isolated(self):
        from subprocess import run
        outputs = {}
        for slug, raw in self.sites().items():
            site = runner.site_settings(raw)
            runtime = self.root / slug / 'recaps'
            runtime.mkdir(parents=True)
            nfl = self.root / slug / 'nfl'
            self.make_team_nfl(site, nfl)
            def offline_node(work, _keys, _key, selected):
                # Real writer, empty fixture schedule: networking is replaced with a throwing fetch.
                env = {'TEAM': selected['slug'], 'NFL_TEAM_ABBR': selected['abbreviation'],
                       'NFL_TEAM_ID': str(selected['balldontlie_team_id'] or 99),
                       'TEAM_NAME': selected['name'], 'TEAM_CITY': selected['city'],
                       'NFL_DATA_FILE': selected['slug'] + '.json',
                       'RECAP_GENERATION_REPORT': str(work / 'recap-report.json')}
                script = 'import {generateGameRecaps} from "./scripts/generate-game-recaps.mjs"; await generateGameRecaps({env:' + json.dumps(env) + ',fetchImpl:async()=>{throw Error("Unexpected network request")}});'
                run(['node', '--input-type=module', '-e', script], cwd=work, check=True, capture_output=True)
            with patch.object(runner, 'SOURCE', self.source), patch.object(runner, 'run_node', side_effect=offline_node):
                receipt = runner.collect('same-airflow-run', self.source, runtime, nfl, site)
            self.assertEqual(receipt['team'], slug)
            self.assertEqual(receipt['generatedCount'], 0)
            self.assertEqual(receipt['openaiRequestCount'], 0)
            self.assertEqual(receipt['requestCount'], 0)
            outputs[slug] = receipt['snapshotPath']
            with patch.object(runner, 'run_node', side_effect=AssertionError('Replay must not run writer')):
                replay = runner.collect('same-airflow-run', self.source, runtime, nfl, site)
            self.assertEqual(replay, receipt)
            changed = dict(site, prompts={**site['prompts'], 'recap': 'Changed prompt'})
            with self.assertRaisesRegex(ValueError, 'different site settings'):
                runner.collect('same-airflow-run', self.source, runtime, nfl, changed)
        self.assertEqual(len(set(outputs.values())), len(self.sites()))

    def test_other_teams_never_seed_from_seattle_authored_content(self):
        site = runner.site_settings(self.sites()['broncos'])
        nfl = self.root / 'denver-nfl'
        self.make_team_nfl(site, nfl)
        write_json(self.source / 'src/data/nfl/gameRecaps.json', {
            'season': 2026, 'recaps': {'seattle-game': complete('Seattle keeps its own history.')},
        })
        work = self.root / 'denver-work'
        seed = runner.stage_source(self.source, work, self.runtime / 'current', nfl, 2026, site)
        self.assertEqual(seed['recaps'], {})
        self.assertEqual(seed['team'], 'broncos')
        self.assertFalse((work / 'src/data/nfl/seahawks.json').exists())
        self.assertTrue((work / 'src/data/nfl/broncos.json').is_file())

    def test_matching_filename_cannot_mask_wrong_nfl_team(self):
        site = runner.site_settings(self.sites()['broncos'])
        nfl = self.root / 'denver-nfl'
        self.make_team_nfl(site, nfl)
        filename = nfl / 'broncos.json'
        data = runner.load_object(filename)
        data['team']['abbreviation'] = 'SEA'
        write_json(filename, data)
        manifest = runner.load_object(nfl / 'manifest.json')
        manifest['files']['broncos.json'] = runner.file_hash(filename)
        write_json(nfl / 'manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'identify the Broncos'):
            runner.select_nfl_snapshot(nfl, site)

    def test_past_team_snapshot_rejected_before_prose_is_staged(self):
        site = runner.site_settings(self.sites()['broncos'])
        snapshot = self.root / 'wrong-team'
        updated = '2026-09-12T04:00:00Z'
        write_json(snapshot / 'gameRecaps.json', {'season': 2026, 'updatedAt': updated,
                   'team': 'seahawks', 'recaps': {}})
        write_json(snapshot / 'manifest.json', {'schema_version': 1, 'runId': 'old', 'season': 2026,
                   'updatedAt': updated, 'team': 'seahawks',
                   'files': {'gameRecaps.json': runner.file_hash(snapshot / 'gameRecaps.json')}})
        with self.assertRaisesRegex(ValueError, 'different team'):
            runner.verify_snapshot(snapshot, site=site)


if __name__ == "__main__":
    unittest.main()
