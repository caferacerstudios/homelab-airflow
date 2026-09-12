import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dags"))
from fan_zone_config import validate_sites

FIXTURE = Path(__file__).parent / "fixtures/active-sites.json"


class NewsConfigTests(unittest.TestCase):
    def setUp(self):
        self.sites = json.loads(FIXTURE.read_text())

    def test_previous_website_shape_is_valid_without_nfl_or_recap_settings(self):
        for site in self.sites.values():
            for name in ("website_root", "timezone", "division"):
                site.pop(name, None)
            site["prompts"]["recap"] = "Existing metadata; not used by daily articles."
        result = validate_sites(self.sites)
        self.assertEqual(result["seahawks"]["website_root"], "/home/laurawkr/seahawksfanzone")
        self.assertEqual(result["seahawks"]["news_snapshot_dir"], "/var/lib/sfz-news/current")
        self.assertEqual(result["broncos"]["website_root"], "/home/laurawkr/templatefanzone")
        self.assertIsNone(result["broncos"]["balldontlie_team_id"])
        self.assertEqual(result["broncos"]["timezone"], "America/Los_Angeles")

    def test_plain_json_and_variable_export_share_the_same_schema(self):
        expected = validate_sites(self.sites)
        self.assertEqual(validate_sites(json.dumps(self.sites)), expected)
        self.assertEqual(validate_sites({"fan_zone_active_sites": json.dumps(self.sites)}), expected)
        self.assertEqual(validate_sites({}), {})

    def test_team_outputs_cannot_collide_or_reuse_seattle_directory(self):
        duplicate = copy.deepcopy(self.sites)
        duplicate["patriots"] = copy.deepcopy(duplicate["broncos"])
        with self.assertRaisesRegex(ValueError, "share"):
            validate_sites(duplicate)
        for field in ("news_snapshot_dir", "website_root"):
            sites = copy.deepcopy(self.sites)
            sites["broncos"][field] = sites["seahawks"][field]
            if field == "news_snapshot_dir":
                sites["broncos"]["news_photos_dir"] = sites["seahawks"]["news_photos_dir"]
            with self.assertRaises(ValueError):
                validate_sites(sites)

    def test_invalid_flags_ids_paths_domains_and_prompts_stop(self):
        for field, value in [("enabled", "true"), ("balldontlie_team_id", True),
                             ("news_snapshot_dir", "/var/lib/../tmp/current"),
                             ("news_photos_dir", "/var/lib/sfz-news/photos"),
                             ("source_domains", ["https://nfl.com"]),
                             ("prompts", {"article": "x" * 8001})]:
            with self.subTest(field=field):
                sites = copy.deepcopy(self.sites)
                sites["broncos"][field] = value
                with self.assertRaises(ValueError):
                    validate_sites(sites)


if __name__ == "__main__":
    unittest.main()
