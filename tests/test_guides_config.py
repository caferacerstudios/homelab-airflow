"""Activation is independent, bounded and cannot enable an inactive team."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dags"))
from fan_zone_guide_config import guide_sites, load_policy


class GuideActivationTests(unittest.TestCase):
    def test_intersection_preserves_site_configuration(self):
        sites = [{"slug": "seahawks", "enabled": True, "custom": "retained"},
                 {"slug": "broncos", "enabled": False}, {"slug": "chiefs", "enabled": True}]
        selected = guide_sites(sites, {"enabled_sites": ["seahawks", "broncos"]})
        self.assertEqual(selected, [sites[0]])
        self.assertIs(selected[0], sites[0])
        self.assertEqual(len(sites), 3)

    def test_malformed_policy_does_not_fall_back_to_all_sites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            for value in ([], {}, {"enabled_sites": "seahawks"}, {"enabled_sites": ["seahawks", "seahawks"]},
                          {"enabled_sites": ["../seahawks"]}, {"enabled_sites": [True]}):
                path.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(ValueError):
                    load_policy(path)
            path.write_text(json.dumps({"enabled_sites": []}))
            self.assertEqual(load_policy(path)["enabled_sites"], [])
            path.write_text(" " * 65537)
            with self.assertRaises(ValueError):
                load_policy(path)


if __name__ == "__main__":
    unittest.main()
