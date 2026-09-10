"""Offline contract: real JS fetch -> Python publish -> real JS import.

Set SFZ_WEBSITE_SOURCE to the website checkout when it is not a sibling named
seahawks-fan-zone. Only Docker/preflight and upstream HTTP are replaced here.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
WEBSITE = Path(os.environ.get("SFZ_WEBSITE_SOURCE", PROJECT.parent / "seahawks-fan-zone")).resolve()
SPEC = importlib.util.spec_from_file_location("nfl_contract_runner", PROJECT / "deployment/nfl/refresh_nfl.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

TEAM = {"id": 31, "abbreviation": "SEA", "full_name": "Seattle Seahawks"}
GAME = {
    "id": 1392216, "season": 2026, "season_type": "regular", "week": 1,
    "date": "2026-09-09T23:00:00Z", "status": "Scheduled", "home_team": TEAM,
    "visitor_team": {"id": 23, "abbreviation": "NE", "full_name": "New England Patriots"},
}
MOCK_HTTP = """
const team = __TEAM__, game = __GAME__;
globalThis.fetch = async (url) => {
  const endpoint = url.pathname.split('/').at(-1);
  let data;
  if (endpoint === 'teams') return new Response(JSON.stringify({data:[team]}));
  if (endpoint === 'games') {
    if (url.searchParams.getAll('season_types[]').join(',') !== '1,2,3') throw new Error('Wrong game filters');
    data = [game];
  } else if (endpoint === 'players') data = [{id:1,first_name:'Test',last_name:'Player'}];
  else if (endpoint === 'season_stats') {
    const phase = url.searchParams.get('season_types[]');
    if (!['2','3'].includes(phase)) throw new Error('Wrong stats filter');
    data = phase === '2' ? [{player_id:1,team_id:31,season:2026}] : [];
  } else throw new Error('Unexpected HTTP request');
  return new Response(JSON.stringify({data,meta:{next_cursor:null}}));
};
""".replace("__TEAM__", json.dumps(TEAM)).replace("__GAME__", json.dumps(GAME))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@unittest.skipUnless(shutil.which("node") and (WEBSITE / "scripts/nfl-api-client.mjs").is_file(),
                     "Needs Node and the migration website checkout; set SFZ_WEBSITE_SOURCE")
class NflCrossLanguageContractTest(unittest.TestCase):
    def test_generated_snapshot_imports_with_or_without_optional_recaps(self):
        for with_recaps in (False, True):
            with self.subTest(with_recaps=with_recaps), tempfile.TemporaryDirectory(prefix="sfz-contract-") as directory:
                folder = Path(directory)
                source = folder / "source"
                runtime = folder / "runtime"
                runtime.mkdir()
                shutil.copytree(WEBSITE / "scripts", source / "scripts")
                shutil.copytree(WEBSITE / "src/lib", source / "src/lib")
                write_json(source / "src/data/team/roster.json", {"players": []})
                write_json(source / "src/data/nfl/seahawks.json", {"season": 2026, "games": [GAME]})
                if with_recaps:
                    write_json(source / "src/data/nfl/gameRecaps.json", {
                        "recaps": {"1392216": {"title": "Old generated title", "body": "Old generated copy"}},
                    })
                before_source = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
                invocations = []

                def run_real_node(work: Path, api_key: str, run_key: str) -> None:
                    invocations.append(run_key)
                    preload = work / "mock-http.mjs"
                    preload.write_text(MOCK_HTTP)
                    env = {
                        **os.environ, "BALLDONTLIE_API_KEY": api_key, "NFL_FETCH_STRICT": "1",
                        "NFL_FETCH_REPORT": str(work / "fetch-report.json"), "NFL_REQUEST_INTERVAL_MS": "0",
                        "NFL_SEASON": "2026", "NFL_TEAM_ABBR": "SEA",
                    }
                    result = subprocess.run(
                        ["node", "--import", str(preload), "scripts/fetch-nfl.mjs"],
                        cwd=work, env=env, check=True, capture_output=True, text=True,
                    )
                    self.assertNotIn(api_key, result.stdout + result.stderr)

                commit = "f" * 40
                run_id = "manual__offline-contract-test"
                with patch.object(runner, "preflight", return_value=(commit, "mock-api-key")), patch.object(runner, "source_commit", return_value=commit), patch.object(runner, "run_node", side_effect=run_real_node):
                    receipt = runner.collect(run_id, source, runtime)
                    repeated = runner.collect(run_id, source, runtime)
                self.assertEqual(len(invocations), 1, "A successful retry must not fetch again")
                self.assertEqual(receipt, repeated)
                self.assertEqual(receipt["requestCount"], 5)
                self.assertEqual(receipt["season"], 2026)
                self.assertEqual(receipt["schema_version"], 1)
                self.assertEqual(runtime.joinpath("current").resolve(), Path(receipt["snapshotPath"]))
                self.assertEqual("gameRecaps.json" in receipt["files"], with_recaps)
                self.assertEqual(set(receipt["files"]), set(runner.PRIMARY_FILES) | ({"gameRecaps.json"} if with_recaps else set()))
                self.assertEqual(before_source, {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()})

                consumer = folder / "consumer"
                shutil.copytree(source, consumer)
                write_json(consumer / "src/data/nfl/gameRecaps.json", {
                    "editor": "Laura", "recaps": {"1392216": {"title": "Current title", "body": "Current edited text"}},
                })
                before_import = {path.name: path.read_bytes() for path in (consumer / "src/data/nfl").iterdir()}
                env = {**os.environ, "NFL_SNAPSHOT_DIR": str(runtime / "current")}
                env.pop("NFL_SNAPSHOT_MAX_AGE_HOURS", None)
                checked = subprocess.run(["node", "scripts/import-nfl-snapshot.mjs", "--check-only"], cwd=consumer, env=env, check=True, capture_output=True, text=True)
                check_receipt = json.loads(checked.stdout.splitlines()[-1])
                self.assertTrue(check_receipt["checkOnly"])
                self.assertEqual(check_receipt["updatedAt"], receipt["updatedAt"])
                self.assertEqual(before_import, {path.name: path.read_bytes() for path in (consumer / "src/data/nfl").iterdir()})
                subprocess.run(["node", "scripts/import-nfl-snapshot.mjs"], cwd=consumer, env=env, check=True, capture_output=True, text=True)
                for name in runner.PRIMARY_FILES:
                    payload = json.loads((consumer / "src/data/nfl" / name).read_text())
                    self.assertEqual(payload["updatedAt"], receipt["updatedAt"])
                    self.assertEqual(payload["season"], receipt["season"])
                    self.assertEqual(runner.file_hash(consumer / "src/data/nfl" / name), receipt["files"][name])
                recaps = json.loads((consumer / "src/data/nfl/gameRecaps.json").read_text())
                self.assertEqual(recaps["editor"], "Laura")
                self.assertEqual(recaps["recaps"]["1392216"]["title"], "Current title")
                self.assertEqual(recaps["recaps"]["1392216"]["body"], "Current edited text")
                self.assertEqual(recaps["recaps"]["1392216"]["game"]["id"], GAME["id"])


if __name__ == "__main__":
    unittest.main()
