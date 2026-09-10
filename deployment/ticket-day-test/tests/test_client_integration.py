"""Exercise the actual client/host JSON exchange with a fake collector."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sfz_ticket_bridge import TicketCollectorHook
import test_bridge


class ClientIntegrationTests(unittest.TestCase):
    setUp = test_bridge.BridgeTests.setUp
    at_first_slot = test_bridge.BridgeTests.at_first_slot
    def test_client_health_roundtrip_does_not_start_source(self):
        hook = TicketCollectorHook(self.root)
        with mock.patch("sfz_ticket_bridge.time.sleep", side_effect=lambda _: self.app.process()):
            receipt = hook.request("health", timeout=2)
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(self.host.starts, 0)
        self.assertFalse(list((self.root / "requests").iterdir()))

    def test_client_collection_replay_returns_original_receipt_without_new_request(self):
        self.app.arm(4)
        self.at_first_slot()
        hook = TicketCollectorHook(self.root)
        with mock.patch("sfz_ticket_bridge.time.sleep", side_effect=lambda _: self.app.process()):
            first = hook.request("collect", slot="2026-09-09T22:00:00+00:00", timeout=2)
            second = hook.request("collect", slot="2026-09-09T22:00:00+00:00", timeout=2)
        self.assertEqual(first["status"], "success")
        self.assertEqual(first, second)
        self.assertEqual(self.host.starts, 1)
