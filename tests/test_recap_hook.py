import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))
from sfz_recap_hook import parse_receipt, validate_receipt


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.receipt = {
            "schema_version": 1, "runId": "manual__test", "season": 2026,
            "updatedAt": "2026-09-10T08:45:00+00:00",
            "nflSourceUpdatedAt": "2026-09-10T08:15:00+00:00", "nflSourceRunId": "source_run",
            "model": "gpt-4o-mini", "generatedCount": 0, "generatedGameIds": [],
            "requestCount": 0, "openaiRequestCount": 0, "files": {"gameRecaps.json": "a" * 64},
        }

    def test_noop_is_successful_receipt(self):
        output = ("log line\nSFZ_RECAP_RECEIPT=" + json.dumps(self.receipt)).encode()
        self.assertEqual(parse_receipt(output, "manual__test"), self.receipt)

    def test_wrong_run_rejected(self):
        with self.assertRaises(ValueError):
            validate_receipt(self.receipt, "another_run")

    def test_multiple_receipts_rejected(self):
        line = ("SFZ_RECAP_RECEIPT=" + json.dumps(self.receipt) + "\n").encode()
        with self.assertRaises(ValueError):
            parse_receipt(line * 2, "manual__test")

    def test_inconsistent_generated_count_rejected(self):
        bad = copy.deepcopy(self.receipt)
        bad["generatedCount"] = 1
        with self.assertRaises(ValueError):
            validate_receipt(bad, "manual__test")


if __name__ == "__main__":
    unittest.main()
