"""Offline regression for MIN at SF, November 22, 2026, in Mexico City.

Designated home team independently confirmed by both clubs:
https://www.vikings.com/news/mexico-city-49ers-nfl-international-game-week-11-2026
https://www.49ers.com/news/49ers-to-face-minnesota-vikings-in-mexico-city-in-week-11-snf
All IDs and kickoff times used below are synthetic fixtures, not published data.
"""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mexico_schedules", ROOT / "deployment/eventspy/schedules.py")
schedules = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schedules)


class MexicoScheduleTests(unittest.TestCase):
    def setUp(self):
        self.coverage = json.loads((ROOT / "deployment/eventspy/coverage/vikings.json").read_text())
        self.site = {"slug": "vikings", "abbreviation": "MIN", "balldontlie_team_id": 23}
        self.team = {"id": 23, "abbreviation": "MIN", "full_name": "Minnesota Vikings"}
        self.games = []
        for row in self.coverage:
            opponent = {"id": 100 + row["week"], "abbreviation": row["opponentAbbreviation"]}
            date = None
            if row["localDate"]:
                date = datetime.fromisoformat(row["localDate"] + "T14:00:00").replace(
                    tzinfo=ZoneInfo(row["timeZone"])).astimezone(timezone.utc).isoformat()
            self.games.append({"id": 900000 + row["week"], "week": row["week"], "season": 2026,
                "date": date, "status": "Scheduled", "postseason": False,
                "home_team": deepcopy(self.team if row["homeAway"] == "home" else opponent),
                "visitor_team": deepcopy(opponent if row["homeAway"] == "home" else self.team)})
        # Do not derive this ordering from the coverage being tested.
        self.mexico = next(game for game in self.games if game["week"] == 11)
        self.mexico.update(home_team={"id": 111, "abbreviation": "SF"},
                          visitor_team=deepcopy(self.team), date="2026-11-23T01:20:00Z")

    def ensure(self, root, coverage=None):
        key = root / "test.env"
        key.write_text("BALLDONTLIE_API_KEY=offline-fixture\n")
        def request(url, token):
            return {"data": [self.team]} if "/teams" in url else {"data": self.games, "meta": {"next_cursor": None}}
        return schedules.ensure_schedule(self.site, coverage or self.coverage,
            now=datetime(2026, 9, 12, 8, tzinfo=timezone.utc), request=request, sleep=lambda _: None,
            cache_root=root / "schedules", api_key_path=key, force_refresh=True, allow_stale=False)

    def test_mexico_game_binds_with_san_francisco_home_and_venue_local_date(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.ensure(Path(directory))
            game = next(game for game in json.loads(path.read_text())["gamesRegular"] if game["week"] == 11)
            self.assertEqual(game["home_team"]["abbreviation"], "SF")
            self.assertEqual(game["visitor_team"]["abbreviation"], "MIN")
        row = next(row for row in self.coverage if row["week"] == 11)
        self.assertEqual(row["homeAway"], "away")
        self.assertEqual(row["localDate"], "2026-11-22")
        self.assertEqual(row["timeZone"], "America/Mexico_City")
        self.assertEqual(row["state"], "unavailable")
        self.assertIsNone(row["sourceUrl"])
        self.assertIsNone(row["gameId"])

    def test_reversed_matchup_still_fails_and_keeps_previous_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.ensure(root)
            original = path.read_bytes()
            self.mexico["home_team"], self.mexico["visitor_team"] = self.mexico["visitor_team"], self.mexico["home_team"]
            with self.assertRaisesRegex(ValueError, r"vikings Week 11:.*API SF at MIN; reviewed MIN at SF"):
                self.ensure(root)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
