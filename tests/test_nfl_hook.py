import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("sfz_nfl_hook", Path(__file__).resolve().parents[1] / "dags/sfz_nfl_hook.py")
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


class ReceiptTests(unittest.TestCase):
    def receipt(self):
        return {"schema_version": 1, "runId": "manual__test", "updatedAt": "2026-09-10T18:00:00Z",
                "requestCount": 9, "season": 2026, "files": {name: "a" * 64 for name in hook.REQUIRED_FILES}}

    def test_only_explicit_completed_receipt_is_accepted(self):
        value = self.receipt()
        output = b"Some ordinary runner log\n" + hook.RECEIPT_PREFIX.encode() + json.dumps(value).encode() + b"\n"
        self.assertEqual(hook.parse_receipt(output, "manual__test"), value)
        with self.assertRaises(ValueError):
            hook.parse_receipt(b"Refresh failed; retaining old schedule\n", "manual__test")

    def test_wrong_run_or_missing_snapshot_is_rejected(self):
        value = self.receipt()
        with self.assertRaises(ValueError):
            hook.validate_receipt(value, "manual__another")
        value["files"].pop("players.json")
        with self.assertRaises(ValueError):
            hook.validate_receipt(value, "manual__test")

    def test_invalid_digests_and_no_api_requests_are_rejected(self):
        for bad in (0, -1, True, "9"):
            value = self.receipt(); value["requestCount"] = bad
            with self.assertRaises(ValueError):
                hook.validate_receipt(value, "manual__test")
        value = self.receipt(); value["files"]["players.json"] = "no-hash"
        with self.assertRaises(ValueError):
            hook.validate_receipt(value, "manual__test")


if __name__ == "__main__":
    unittest.main()
