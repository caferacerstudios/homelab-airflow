"""Offline schedule tests; all API replies below are synthetic test data only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
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
