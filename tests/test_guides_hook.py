"""Run identity, team isolation and request/receipt boundaries; no SSH calls."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dags"))
import sfz_guides_hook as hook


class GuidesHookTests(unittest.TestCase):
    def setUp(self):
        self.sites = json.loads((ROOT / "config/active-sites.json").read_text())
        self.site = dict(self.sites["broncos"], slug="broncos")
        self.receipt = {"schema_version": 1, "pipeline": "guides", "runId": "manual__guides-test", "team": "broncos",
                        "updatedAt": "2026-09-01T12:00:00Z", "openaiRequestCount": 3, "season": 2026,
                        "files": {name: "a" * 64 for name in hook.FILES}}

    def test_request_roundtrip_preserves_configuration(self):
        token = hook.encode_request(self.receipt["runId"], self.site)
        value = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        self.assertEqual(value["site"]["news_snapshot_dir"], "/var/lib/boncosfz-news/current")
        self.assertEqual(value["site"]["prompts"], self.site["prompts"])
        self.assertEqual(value["runId"], self.receipt["runId"])
        self.assertNotIn("guides_snapshot_dir", value["site"])

    def test_invalid_requests_stop_before_ssh(self):
        for run in (None, "", "\n", "x" * 513, "run\x7f", "run\x85"):
            with self.assertRaises(ValueError):
                hook.encode_request(run, self.site)
        wrong = dict(self.site, news_snapshot_dir="/var/lib/sfz-news/current")
        with self.assertRaises(ValueError):
            hook.encode_request("valid", wrong)

    def test_receipts_reject_missing_mixed_foreign_and_invalid_files(self):
        valid = self.receipt
        self.assertIs(hook.validate_receipt(valid, valid["runId"], self.site), valid)
        variants = [dict(valid, pipeline="nfl"), dict(valid, season=True), dict(valid, season=0), dict(valid, team="seahawks"), dict(valid, runId="another"),
                    dict(valid, files={"game-day-guides.json": "a" * 64}),
                    dict(valid, files={**valid["files"], "players.json": "a" * 64}),
                    dict(valid, files={**valid["files"], "watch-guide.json": "not-a-hash"}),
                    dict(valid, openaiRequestCount=True), dict(valid, openaiRequestCount=-1),
                    dict(valid, updatedAt="2026-09-01"),
                    dict(valid, updatedAt=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())]
        for receipt in variants:
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                hook.validate_receipt(receipt, valid["runId"], self.site)

    def test_only_one_explicit_completed_record_is_accepted(self):
        line = hook.RECEIPT_PREFIX.encode() + json.dumps(self.receipt).encode() + b"\n"
        self.assertEqual(hook.parse_receipt(b"collector finished\n" + line,
                         self.receipt["runId"], self.site), self.receipt)
        for output in (b"runner failed", line + line):
            with self.assertRaises(ValueError):
                hook.parse_receipt(output, self.receipt["runId"], self.site)

    def test_artifacts_separate_teams_for_the_same_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for slug in ("seahawks", "broncos"):
                receipt = dict(self.receipt, team=slug)
                site = dict(self.sites[slug], slug=slug)
                path = Path(hook.save_receipt(receipt, receipt["runId"], site, Path(tmp)))
                paths.append(path)
                self.assertEqual(json.loads(path.read_text()), receipt)
                self.assertEqual(path.relative_to(tmp).parts[:2], (slug, "guides"))
            self.assertNotEqual(*paths)


if __name__ == "__main__":
    unittest.main()
