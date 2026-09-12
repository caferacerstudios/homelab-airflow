"""Offline schedule tests; all API replies below are synthetic test data only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("eventspy_schedules", ROOT / "deployment/eventspy/schedules.py")
schedules = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schedules)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key_path = self.root / "production.env"
        self.key_path.write_text("BALLDONTLIE_API_KEY='offline-test-key'\n")
        self.coverage = json.loads((ROOT / "deployment/eventspy/coverage/broncos.json").read_text())
        self.site = {"slug": "broncos", "abbreviation": "DEN", "balldontlie_team_id": None}
        self.now = datetime(2026, 9, 12, 8, tzinfo=timezone.utc)
        self.team = {"id": 10, "abbreviation": "DEN", "full_name": "Denver Broncos", "name": "Broncos", "location": "Denver"}
        self.games = []
        for row in self.coverage:
            opponent = {"id": 100 + row["week"], "abbreviation": row["opponentAbbreviation"], "full_name": row["opponent"]}
            kickoff = None
            if row["localDate"]:
                kickoff = datetime.fromisoformat(row["localDate"] + "T14:00:00").replace(tzinfo=ZoneInfo(row["timeZone"])).astimezone(timezone.utc).isoformat()
            self.games.append({"id": int(row["gameId"] or 900000 + row["week"]), "season": 2026,
                               "week": row["week"], "date": kickoff, "status": "Scheduled", "status_state": "scheduled",
                               "home_team": deepcopy(self.team if row["homeAway"] == "home" else opponent),
                               "visitor_team": deepcopy(opponent if row["homeAway"] == "home" else self.team),
                               "postseason": False, "venue": "Synthetic test venue", "home_team_score": None})
        self.requests, self.delays = [], []

    def request(self, url, key):
        self.assertEqual(key, "offline-test-key")
        self.requests.append(url)
        if urlparse(url).path.endswith("/teams"):
            return {"data": [deepcopy(self.team)]}
        return {"data": deepcopy(self.games), "meta": {"next_cursor": None}}

    def ensure(self, **overrides):
        options = {"now": self.now, "request": self.request, "sleep": self.delays.append,
                   "cache_root": self.root / "schedules", "api_key_path": self.key_path}
        options.update(overrides)
        return schedules.ensure_schedule(self.site, self.coverage, **options)

    def publish_nfl(self, *, mutate=None, updated=None, run="one"):
        """Write a synthetic test run using the actual NFL producer manifest shape."""
        runtime = self.root / "broncos-nfl"
        self.site["nfl_snapshot_dir"] = str(runtime / "current")
        snapshot = runtime / "runs" / run / "snapshot"
        snapshot.mkdir(parents=True)
        stamp = (updated or self.now).isoformat()
        games = []
        for original in self.games:
            game = deepcopy(original)
            game["id"] = str(game["id"])
            game["homeTeam"] = game.pop("home_team")
            game["awayTeam"] = game.pop("visitor_team")
            game["phase"] = "regular"
            game["state"] = "upcoming"
            game["startsAt"] = game["date"]
            game["dateConfirmed"] = game["date"] is not None
            game["timeConfirmed"] = game["date"] is not None
            if game["date"]:
                game["date"] = datetime.fromisoformat(game["date"]).astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
            games.append(game)
        games.append({"id": "bye-2026-10", "week": 10, "season": 2026, "bye": True})
        common = {"fixture": False, "updatedAt": stamp, "season": 2026}
        documents = {
            "broncos.json": {**common, "team": deepcopy(self.team), "sourceSeason": 2026,
                "gamesRegular": games, "games": games,
                "currentRoster": [{"testRoster": True}], "playerSeasonStats": [{"testStats": True}],
                "seasons": [{"season": 2025, "games": [{"id": "not-current"}]}]},
            "players.json": {**common, "team": deepcopy(self.team), "playerSeasonStats": [{"testStats": True}]},
            "standings.json": {**common, "phases": {}},
        }
        if mutate:
            mutate(documents)
        files = {}
        for name, value in documents.items():
            encoded = (json.dumps(value) + "\n").encode()
            (snapshot / name).write_bytes(encoded)
            files[name] = hashlib.sha256(encoded).hexdigest()
        manifest = {"schema_version": 1, "team": "broncos", "season": 2026, "updatedAt": stamp,
                    "runId": run, "files": files}
        (snapshot / "manifest.json").write_text(json.dumps(manifest))
        current = runtime / "current"
        current.unlink(missing_ok=True)
        current.symlink_to(snapshot.relative_to(runtime))
        return snapshot

    def test_fresh_full_nfl_snapshot_replaces_old_cache_without_api_or_credentials(self):
        path = self.ensure()
        old_cache = json.loads(path.read_text())
        self.now += timedelta(hours=1)
        self.publish_nfl()
        self.key_path.unlink()
        self.assertEqual(self.ensure(request=Mock(side_effect=AssertionError("no API")), force_refresh=True), path)
        cached = json.loads(path.read_text())
        self.assertNotEqual(cached["updatedAt"], old_cache["updatedAt"])
        self.assertEqual(cached["team"], self.team)
        self.assertEqual(len(cached["gamesRegular"]), 17)
        self.assertEqual(cached["gamesRegular"][0]["id"], self.games[0]["id"])
        self.assertEqual(cached["gamesRegular"][0]["startsAt"], self.games[0]["date"])
        self.assertEqual(cached["gamesRegular"][0]["date"], self.coverage[0]["localDate"])
        self.assertEqual(cached["gamesRegular"][0]["home_team"], self.games[0]["home_team"])
        self.assertEqual(cached["gamesRegular"][0]["visitor_team"], self.games[0]["visitor_team"])
        self.assertNotIn("seasons", cached)
        self.assertNotIn("playerDirectory", cached)
        self.assertEqual(cached["currentRoster"], [])
        self.assertEqual(cached["playerSeasonStats"], [])
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    @unittest.skipUnless(shutil.which("node"), "Node is required for the collector cross-check")
    def test_full_nfl_completed_state_skips_collection_after_deriving_cache(self):
        def completed(documents):
            game = documents["broncos.json"]["gamesRegular"][0]
            game.pop("status")
            game.pop("status_state")
            game["state"] = "completed"
            game["home_team_score"] = 24
            game["visitor_team_score"] = 17
        self.publish_nfl(mutate=completed)
        path = self.ensure(request=Mock(side_effect=AssertionError("no API")))
        cached = json.loads(path.read_text())
        self.assertEqual(cached["gamesRegular"][0]["status_state"], "completed")
        self.assertEqual(cached["gamesRegular"][0]["home_team_score"], 24)
        module_url = (ROOT / "deployment/eventspy/collector-coverage.mjs").as_uri()
        script = f'''import {{bindCoverageToSchedule,collectionDecision}} from {json.dumps(module_url)};
            let input = ''; for await (const part of process.stdin) input += part;
            const {{site,coverage,schedule}} = JSON.parse(input);
            const binding = bindCoverageToSchedule(site,coverage,schedule)[0];
            console.log(collectionDecision(binding).reason);'''
        result = subprocess.run(["node", "--input-type=module", "-e", script],
            input=json.dumps({"site": {**self.site, "city": "Denver", "name": "Broncos"},
                              "coverage": self.coverage, "schedule": cached}),
            text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), "GAME_COMPLETED")

    def test_missing_or_six_hour_old_full_snapshot_uses_existing_api_fallback(self):
        self.site["nfl_snapshot_dir"] = str(self.root / "broncos-nfl/current")
        self.ensure()
        self.assertEqual(len(self.requests), 2)
        self.publish_nfl(updated=self.now - timedelta(hours=6))
        self.ensure(force_refresh=True)
        self.assertEqual(len(self.requests), 4)

    def test_full_nfl_checksum_failure_keeps_cache_and_never_uses_api_fallback(self):
        path = self.ensure()
        previous = path.read_bytes()
        snapshot = self.publish_nfl()
        (snapshot / "players.json").write_text('{}')
        with self.assertRaisesRegex(ValueError, "checksum mismatch"), patch.object(schedules, "read_api_key", side_effect=AssertionError):
            self.ensure()
        self.assertEqual(path.read_bytes(), previous)

    def test_invalid_full_nfl_identity_season_fixture_or_timestamp_stops_even_when_stale(self):
        mutations = [
            lambda docs: docs["broncos.json"]["team"].update(abbreviation="SEA"),
            lambda docs: docs["broncos.json"]["team"].update(id=31),
            lambda docs: docs["players.json"]["team"].update(abbreviation="SEA"),
            lambda docs: docs["broncos.json"].update(season=2025),
            lambda docs: docs["broncos.json"].update(sourceSeason=2025),
            lambda docs: docs["broncos.json"].update(fixture=True),
            lambda docs: docs["players.json"].update(fixture=True),
            lambda docs: docs["broncos.json"].update(updatedAt="2026-09-11T01:00:00+00:00"),
            lambda docs: docs["broncos.json"]["gamesRegular"][0]["awayTeam"].update(id=99),
        ]
        self.site["balldontlie_team_id"] = self.team["id"]
        for index, mutate in enumerate(mutations):
            self.publish_nfl(mutate=mutate, updated=self.now - timedelta(hours=7), run=str(index))
            with self.subTest(index=index), self.assertRaises(ValueError), patch.object(schedules, "read_api_key", side_effect=AssertionError):
                self.ensure()
        self.publish_nfl(updated=self.now + timedelta(minutes=1), run="future")
        with self.assertRaisesRegex(ValueError, "future"):
            self.ensure()

    def test_full_nfl_manifest_wrong_team_or_checksum_path_is_rejected(self):
        for index, change in enumerate((
            {"team": "seahawks"}, {"season": 2025},
            {"files": {"../broncos.json": "0" * 64}},
        )):
            snapshot = self.publish_nfl(run=str(index))
            path = snapshot / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest.update(change)
            path.write_text(json.dumps(manifest))
            with self.subTest(change=change), self.assertRaises(ValueError), patch.object(schedules, "read_api_key", side_effect=AssertionError):
                self.ensure()

    def test_full_nfl_current_cannot_escape_own_root_or_point_to_missing_run(self):
        self.publish_nfl()
        current = Path(self.site["nfl_snapshot_dir"])
        foreign = self.root / "seahawks-nfl/runs/other/snapshot"
        foreign.mkdir(parents=True)
        for target in (foreign, current.parent / "runs/missing/snapshot"):
            current.unlink()
            current.symlink_to(target)
            with self.assertRaises(ValueError), patch.object(schedules, "read_api_key", side_effect=AssertionError):
                self.ensure()

    def test_full_nfl_time_tbd_preserves_calendar_date_without_synthetic_kickoff(self):
        def unknown_time(documents):
            game = documents["broncos.json"]["gamesRegular"][0]
            game["startsAt"] = None
            game["timeConfirmed"] = False
        self.publish_nfl(mutate=unknown_time)
        path = self.ensure(request=Mock(side_effect=AssertionError("no API")))
        game = json.loads(path.read_text())["gamesRegular"][0]
        self.assertEqual(game["date"], self.coverage[0]["localDate"])
        self.assertIsNone(game["startsAt"])
        self.assertFalse(game["timeConfirmed"])

    def test_full_nfl_uses_confirmed_instant_for_venue_date_over_display_calendar(self):
        def differing_display_date(documents):
            game = documents["broncos.json"]["gamesRegular"][0]
            game["date"] = "2026-09-13"
            game["startsAt"] = "2026-09-14T05:30:00Z"
        self.publish_nfl(mutate=differing_display_date)
        path = self.ensure(request=Mock(side_effect=AssertionError("no API")))
        game = json.loads(path.read_text())["gamesRegular"][0]
        self.assertEqual(game["date"], "2026-09-13")
        self.assertEqual(game["startsAt"], "2026-09-14T05:30:00Z")

    def test_full_nfl_member_symlinks_are_rejected(self):
        snapshot = self.publish_nfl()
        member = snapshot / "broncos.json"
        foreign = self.root / "foreign.json"
        member.rename(foreign)
        member.symlink_to(foreign)
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.ensure()

    def test_python_coverage_alias_comparison_preserves_original_washington_abbreviations(self):
        for source_alias, coverage_alias in (("WSH", "WAS"), ("WAS", "WSH")):
            self.coverage[0]["homeTeamAbbreviation"] = coverage_alias
            self.games[0]["home_team"]["abbreviation"] = source_alias
            path = self.ensure(force_refresh=True)
            cached = json.loads(path.read_text())
            self.assertEqual(cached["gamesRegular"][0]["home_team"]["abbreviation"], source_alias)

    def test_seattle_does_not_touch_its_snapshot_or_credentials(self):
        with patch.object(schedules, "read_api_key", side_effect=AssertionError("must not read key")):
            self.assertIsNone(schedules.ensure_schedule({"slug": "seahawks"}, []))

    def test_fetches_authentic_team_and_regular_season_query_preserving_rows(self):
        self.games[0]["status"] = "Final/OT"
        self.games[0]["status_state"] = "final"
        self.games[0]["home_team_score"] = 24
        path = self.ensure()
        self.assertEqual(path, self.root / "schedules/broncos.json")
        query = parse_qs(urlparse(self.requests[-1]).query)
        self.assertEqual(query, {"seasons[]": ["2026"], "team_ids[]": ["10"], "season_types[]": ["2"], "per_page": ["100"]})
        self.assertEqual(self.delays, [15])
        payload = json.loads(path.read_text())
        self.assertEqual(payload["gamesRegular"], [dict(row, season_type="regular") for row in self.games])
        self.assertEqual(payload["games"], payload["gamesRegular"])
        self.assertEqual(payload["team"], self.team)
        self.assertEqual(payload["source"], "balldontlie")
        self.assertFalse(payload["fixture"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_fresh_cache_skips_api_and_credentials(self):
        path = self.ensure()
        initial = path.read_bytes()
        self.key_path.unlink()
        self.assertEqual(self.ensure(now=self.now + timedelta(hours=5), request=Mock(side_effect=AssertionError)), path)
        self.assertEqual(path.read_bytes(), initial)

    def test_refreshes_at_six_hours_and_day_change(self):
        self.ensure(now=self.now.replace(hour=23))
        self.ensure(now=self.now.replace(hour=23) + timedelta(hours=1))
        self.assertEqual(len(self.requests), 4)
        self.ensure(now=self.now.replace(hour=23) + timedelta(hours=7))
        self.assertEqual(len(self.requests), 6)

    def test_pagination_is_followed_without_dropping_games(self):
        def paginated(url, key):
            self.requests.append(url)
            if "/teams?" in url:
                return {"data": [self.team]}
            if "cursor=" not in url:
                return {"data": self.games[:8], "meta": {"next_cursor": 800}}
            self.assertEqual(parse_qs(urlparse(url).query)["cursor"], ["800"])
            return {"data": self.games[8:], "meta": {}}
        self.ensure(request=paginated)
        self.assertEqual(self.delays, [15, 15])
        self.assertEqual(len(self.requests), 3)

    def test_pagination_loop_stops(self):
        request = Mock(return_value={"data": [], "meta": {"next_cursor": 1}})
        with self.assertRaisesRegex(schedules.ScheduleFetchError, "repeated cursor"):
            self.ensure(request=request)
        self.assertEqual(request.call_count, 2)
        self.assertFalse((self.root / "schedules/broncos.json").exists())

    def test_transient_failure_retains_valid_cache_without_overwriting(self):
        path = self.ensure()
        initial = path.read_bytes()
        request = Mock(side_effect=schedules.ScheduleFetchError("failed"))
        with self.assertWarnsRegex(RuntimeWarning, "retaining validated schedule"):
            self.assertEqual(self.ensure(now=self.now + timedelta(days=1), request=request), path)
        self.assertEqual(path.read_bytes(), initial)
        self.assertEqual(request.call_count, 1)

    def test_installer_requires_fresh_schedule(self):
        self.ensure()
        with self.assertRaises(schedules.ScheduleFetchError):
            self.ensure(force_refresh=True, allow_stale=False,
                        request=Mock(side_effect=schedules.ScheduleFetchError("failed")))

    def test_team_id_mismatch_stops_before_game_request(self):
        self.site["balldontlie_team_id"] = 31
        with self.assertRaisesRegex(ValueError, "does not match its abbreviation"):
            self.ensure()
        self.assertEqual(len(self.requests), 1)

    def test_wrong_or_fixture_cache_is_not_used_as_failure_fallback(self):
        for mutation in ({"fixture": True}, {"team": {"id": 31, "abbreviation": "SEA"}}, {"season": 2025}):
            with self.subTest(mutation=mutation):
                path = self.ensure(force_refresh=True)
                value = json.loads(path.read_text())
                value.update(mutation)
                path.write_text(json.dumps(value))
                with self.assertRaises(schedules.ScheduleFetchError):
                    self.ensure(request=Mock(side_effect=schedules.ScheduleFetchError("failed")))

    def test_invalid_fresh_games_cannot_replace_existing_cache(self):
        path = self.ensure()
        before = path.read_bytes()
        original = deepcopy(self.games)
        mutations = [
            lambda rows: rows.pop(),
            lambda rows: rows[1].update(id=rows[0]["id"]),
            lambda rows: rows[0].update(season=2025),
            lambda rows: rows[0].update(season_type="preseason"),
            lambda rows: rows[0].update(postseason=True),
            lambda rows: rows[0].update(date="2026-09-15T22:00:00Z"),
            lambda rows: rows[0]["visitor_team"].update(id=99),
            lambda rows: rows[5].update(id=12345),
            lambda rows: rows[1].update(week=1),
        ]
        for mutation in mutations:
            self.games = deepcopy(original)
            mutation(self.games)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.ensure(force_refresh=True)
            self.assertEqual(path.read_bytes(), before)

    def test_unknown_date_only_allowed_for_reviewed_tbd_games(self):
        self.ensure()
        self.games[0]["date"] = None
        with self.assertRaisesRegex(ValueError, "kickoff is missing"):
            self.ensure(force_refresh=True)

    def test_request_429_is_bounded_and_never_includes_error_body(self):
        error = HTTPError(schedules.API_BASE + "/games", 429, "secret-message", {}, io.BytesIO(b"secret-response"))
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(schedules, "build_opener", return_value=opener):
            with self.assertRaises(schedules.ScheduleFetchError) as caught:
                schedules.request_json(schedules.API_BASE + "/games", "secret-key")
        self.assertEqual(str(caught.exception), "BALLDONTLIE HTTP 429; no retry was attempted")
        self.assertEqual(opener.open.call_count, 1)

    def test_api_key_parser_never_expands_shell_and_rejects_duplicates(self):
        self.key_path.write_text("export BALLDONTLIE_API_KEY='a b' # literal\n")
        self.assertEqual(schedules.read_api_key(self.key_path), "a b")
        for value in ("$(echo secret)", "`echo secret`", "", "bad\\value"):
            self.key_path.write_text(f"BALLDONTLIE_API_KEY='{value}'\n")
            with self.assertRaises(ValueError):
                schedules.read_api_key(self.key_path)
        self.key_path.write_text("BALLDONTLIE_API_KEY=a\nBALLDONTLIE_API_KEY=b\n")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            schedules.read_api_key(self.key_path)


if __name__ == "__main__":
    unittest.main()
