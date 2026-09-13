"""Run the real NFL fetcher from raw modular-template sources, without network.

Set SFZ_TEMPLATE_SOURCE when template-fan-zone is not a sibling checkout. These
tests intentionally use unrendered source files: a rendered website fixture
cannot reproduce the template-token syntax error seen by the host collector.
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


PROJECT = Path(__file__).resolve().parents[1]
TEMPLATE = Path(os.environ.get("SFZ_TEMPLATE_SOURCE", PROJECT.parent / "template-fan-zone")).resolve()
SPEC = importlib.util.spec_from_file_location("nfl_template_runner", PROJECT / "deployment/nfl/refresh_nfl.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

SEATTLE = {"id": 31, "abbreviation": "SEA", "full_name": "Seattle Seahawks"}
OPPONENT = {"id": 9001, "abbreviation": "NYG", "full_name": "New York Giants"}
OLD_GAME = {
    "id": 3100, "season": 2025, "season_type": "regular", "week": 1,
    "date": "2025-09-07T20:25:00Z", "status": "Final", "home_team": SEATTLE,
    "visitor_team": OPPONENT, "home_team_score": 24, "visitor_team_score": 21,
}
ROSTER_SENTINEL = {"id": "seattle-editorial-player", "name": "Seattle roster sentinel", "status": "Active"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def file_contents(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@unittest.skipUnless(shutil.which("node") and (TEMPLATE / "config/active-sites.json").is_file(),
                     "Needs Node and the raw template checkout; set SFZ_TEMPLATE_SOURCE")
class NflRawTemplateStagingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nfl-raw-template-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        shutil.copytree(TEMPLATE / "scripts", self.source / "scripts")
        shutil.copytree(TEMPLATE / "src/lib", self.source / "src/lib")
        self.assertIn("const {team}Score", (self.source / "src/lib/schedule.mjs").read_text(),
                      "The regression fixture must be the raw template, not a rendered team build")
        write_json(self.source / "src/data/team/roster.json", {"players": [ROSTER_SENTINEL]})
        raw_game = {**OLD_GAME, "home_team": {**SEATTLE, "full_name": "Seattle {Team}"}}
        write_json(self.source / "src/data/nfl/{team}.json", {"season": 2025, "games": [raw_game]})
        write_json(self.source / "src/data/nfl/gameRecaps.json", {
            "recaps": {"3100": {"title": "Seattle-only edited recap", "body": "Preserve this editorial text"}},
        })
        write_json(self.source / "src/data/nfl/watch-guide-2026.json", {
            "season": 2026, "games": [], "editor": "Seattle viewing sentinel",
        })
        (self.source / "dist").mkdir()
        (self.source / "dist/index.html").write_text("Already served production output")
        document = json.loads((TEMPLATE / "config/active-sites.json").read_text())
        sites = document.get("fan_zone_active_sites", document)
        self.sites = json.loads(sites) if isinstance(sites, str) else sites
        self.before = file_contents(self.source)

    def stage(self, slug, suffix="", use_current=True):
        site = {**self.sites[slug], "slug": slug}
        work = self.root / (slug + suffix)
        current = self.root / "no-current-snapshot"
        if slug == "seahawks" and use_current:
            current = self.root / ("current" + suffix)
            write_json(current / "seahawks.json", {"season": 2025, "team": SEATTLE, "games": [OLD_GAME]})
            write_json(current / "players.json", {"season": 2025, "team": SEATTLE})
            write_json(current / "standings.json", {"season": 2025, "phases": {}})
            write_json(current / "manifest.json", {
                "schema_version": 1, "team": "seahawks", "runId": "last-good-collection",
                "files": {name: runner.file_hash(current / name)
                          for name in ("seahawks.json", "players.json", "standings.json")},
            })
        runner.stage_source(self.source, work, current, site)
        return site, work

    def fetch(self, work, site, api_team=None, identity_only=False):
        team = api_team or {
            "id": site["balldontlie_team_id"], "abbreviation": site["abbreviation"],
            "full_name": site["city"] + " " + site["name"],
        }
        games = [
            {"id": team["id"] * 100 + 1, "season": 2026, "season_type": "regular", "week": 1,
             "date": "2026-09-13T20:25:00Z", "status": "Final", "home_team": team,
             "visitor_team": OPPONENT, "home_team_score": 24, "visitor_team_score": 21},
            {"id": team["id"] * 100 + 2, "season": 2026, "season_type": "regular", "week": 2,
             "date": "2026-09-20T20:25:00Z", "status": "Final", "home_team": OPPONENT,
             "visitor_team": team, "home_team_score": 28, "visitor_team_score": 17},
        ]
        preload = work / "mock-http.mjs"
        preload.write_text("const team = " + json.dumps(team) + ";\nconst games = " + json.dumps(games) + ";\n"
                           + "const identityOnly = " + json.dumps(identity_only) + ";\n" + """
globalThis.fetch = async (url) => {
  const endpoint = url.pathname.split('/').at(-1);
  if (endpoint === 'teams') return new Response(JSON.stringify({data:[team, games[0].visitor_team]}));
  if (identityOnly) throw new Error('SHOULD_NOT_FETCH_AFTER_TEAMS');
  let data;
  if (endpoint === 'games') {
    if (url.searchParams.getAll('season_types[]').join(',') !== '1,2,3') throw new Error('Wrong game filters');
    data = games;
  } else if (endpoint === 'players') {
    data = [{id:1,first_name:'Test',last_name:'Player'}];
  } else if (endpoint === 'season_stats') {
    const phase = url.searchParams.get('season_types[]');
    if (!['2','3'].includes(phase)) throw new Error('Wrong stats filter');
    data = phase === '2' ? [{player_id:1,team_id:team.id,season:2026}] : [];
  } else throw new Error('Unexpected HTTP request: ' + endpoint);
  return new Response(JSON.stringify({data,meta:{next_cursor:null}}));
};
""")
        return subprocess.run(["node", "--import", str(preload), "scripts/fetch-nfl.mjs"], cwd=work,
            env={**os.environ, "BALLDONTLIE_API_KEY": "offline-test-key", "NFL_FETCH_STRICT": "1",
                 "NFL_REQUEST_INTERVAL_MS": "0", "NFL_SEASON": "2026", "NFL_TEAM_ABBR": site["abbreviation"],
                 "NFL_FETCH_REPORT": str(work / "fetch-report.json")},
            capture_output=True, text=True, timeout=30)

    def test_all_six_teams_execute_raw_template_with_independent_identity_and_data(self):
        for slug in ("seahawks", "broncos", "packers", "vikings", "chiefs", "patriots"):
            with self.subTest(team=slug):
                site, work = self.stage(slug)
                result = self.fetch(work, site)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                data = work / "src/data/nfl"
                combined = json.loads((data / (slug + ".json")).read_text())
                self.assertEqual(combined["team"]["id"], site["balldontlie_team_id"])
                self.assertEqual(combined["team"]["abbreviation"], site["abbreviation"])
                self.assertEqual(combined["team"]["full_name"], site["city"] + " " + site["name"])
                self.assertNotIn("{Team}", json.dumps(combined))
                home, away = combined["gamesRegular"]
                self.assertIs(home["isHome"], True)
                self.assertIs(away["isHome"], False)
                self.assertEqual(home["opponent"], OPPONENT)
                self.assertEqual(away["opponent"], OPPONENT)
                self.assertEqual(home[slug + "RecordAfter"], "1-0")
                self.assertEqual(away[slug + "RecordAfter"], "1-1")
                standings = json.loads((data / "standings.json").read_text())
                rows = standings["phases"]["regular"]["rows"]
                self.assertEqual([row["abbreviation"] for row in rows], [site["abbreviation"]])
                self.assertEqual((rows[0]["wins"], rows[0]["losses"]), (1, 1))
                self.assertFalse((data / "{team}.json").exists())
                report = json.loads((work / "fetch-report.json").read_text())
                self.assertEqual(report["status"], "success")
                if slug == "seahawks":
                    self.assertEqual(combined["currentRoster"], [ROSTER_SENTINEL])
                    self.assertIn(2025, [record["season"] for record in combined["seasons"]])
                    recap = json.loads((data / "gameRecaps.json").read_text())["recaps"]["3100"]
                    self.assertEqual(recap["body"], "Preserve this editorial text")
                    self.assertEqual(recap["game"], OLD_GAME)
                else:
                    self.assertEqual(combined["currentRoster"], [])
                    self.assertEqual([record["season"] for record in combined["seasons"]], [2026])
                    self.assertFalse((data / "seahawks.json").exists())
                    self.assertFalse((data / "gameRecaps.json").exists())
                    self.assertFalse((data / "watch-guide-2026.json").exists())
                self.assertEqual(file_contents(self.source), self.before)

    def test_first_seattle_collection_does_not_import_raw_template_seed_history(self):
        site, work = self.stage("seahawks", "-first-collection", use_current=False)
        self.assertFalse((work / "src/data/nfl/seahawks.json").exists())
        result = self.fetch(work, site)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = work / "src/data/nfl"
        combined = json.loads((data / "seahawks.json").read_text())
        self.assertEqual([record["season"] for record in combined["seasons"]], [2026])
        self.assertNotIn("{Team}", json.dumps(combined))
        recap = json.loads((data / "gameRecaps.json").read_text())["recaps"]["3100"]
        self.assertEqual(recap["body"], "Preserve this editorial text")
        self.assertNotIn("game", recap)
        self.assertEqual(file_contents(self.source), self.before)

    def test_seattle_watch_identity_reconciles_without_rewriting_editorial_content(self):
        guide_path = self.source / "src/data/nfl/watch-guide-2026.json"
        guide = {
            "season": 2026, "editor": "Seattle viewing sentinel",
            "notes": {"text": "Keep Seattle {Team} quotation literal"},
            "games": [{
                "phase": "preseason", "week": 1, "status": "completed",
                "matchup": "San Francisco 49ers at Seattle {Team}",
                "result": "{Team} 24, 49ers 21", "dateLabel": "Sat. Aug 15",
                "kickoffLabel": "7:00 PM PT", "venue": "Lumen Field",
                "officialGameUrl": "https://www.{team}.com/game-day/2026/pre/reg-1",
                "note": "Editorial text mentioning Seattle {Team} stays literal",
            }],
        }
        write_json(guide_path, guide)
        before = file_contents(self.source)
        _, work = self.stage("seahawks", "-watch-guide")
        staged_guide = json.loads((work / "src/data/nfl/watch-guide-2026.json").read_text())
        self.assertEqual(staged_guide["notes"], guide["notes"])
        self.assertEqual(staged_guide["editor"], guide["editor"])
        self.assertEqual(staged_guide["games"][0]["note"], guide["games"][0]["note"])
        result = subprocess.run(["node", "--input-type=module", "-e", """
import fs from 'node:fs';
import {reconcileOfficialSchedule} from './src/lib/schedule-guide.mjs';
const guide = JSON.parse(fs.readFileSync('src/data/nfl/watch-guide-2026.json', 'utf8'));
console.log(JSON.stringify(reconcileOfficialSchedule([], guide)));
"""], cwd=work, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        games = json.loads(result.stdout)
        self.assertEqual(len(games), 1)
        game = games[0]
        self.assertEqual(game["home_team"], {"abbreviation": "SEA", "full_name": "Seattle Seahawks"})
        self.assertEqual(game["visitor_team"], {"abbreviation": "SF", "full_name": "San Francisco 49ers"})
        self.assertEqual((game["home_team_score"], game["visitor_team_score"]), (24, 21))
        self.assertEqual(game["canonical_url"], "https://www.seahawks.com/game-day/2026/pre/reg-1")
        self.assertEqual(game["venue"], "Lumen Field")
        self.assertEqual(file_contents(self.source), before)

    def test_wrong_api_team_identity_is_rejected_before_fetching_games(self):
        for slug in ("seahawks", "broncos", "packers", "vikings", "chiefs", "patriots"):
            for mismatch in ("id", "name"):
                with self.subTest(team=slug, mismatch=mismatch):
                    site, work = self.stage(slug, "-wrong-" + mismatch)
                    api_team = {"id": site["balldontlie_team_id"], "abbreviation": site["abbreviation"],
                                "full_name": site["city"] + " " + site["name"]}
                    if mismatch == "id":
                        api_team["id"] += 10000
                    else:
                        api_team["full_name"] = "Unrelated Football Team"
                    result = self.fetch(work, site, api_team, identity_only=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("API team identity does not match", result.stderr)
                    self.assertNotIn("SHOULD_NOT_FETCH_AFTER_TEAMS", result.stderr)
                    self.assertFalse((work / "src/data/nfl/players.json").exists())
                    self.assertEqual(file_contents(self.source), self.before)


if __name__ == "__main__":
    unittest.main()
