import copy
import base64
import importlib.util
import tempfile
from unittest.mock import patch
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))
from sfz_recap_hook import parse_receipt, validate_receipt, encode_request, save_receipt
import sfz_recap_hook as hook
from fan_zone_config import validate_site

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deployment/recaps"))
import refresh_recaps as host
SPEC = importlib.util.spec_from_file_location("recap_ssh_entrypoint", ROOT / "deployment/recaps/ssh_entrypoint.py")
ssh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ssh)


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



class ModularReceiptTests(unittest.TestCase):
    def setUp(self):
        ReceiptTests.setUp(self)
        self.sites = json.loads((ROOT / 'config/active-sites.json').read_text())

    def test_one_shared_airflow_run_has_independent_team_receipts(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(hook, 'ARTIFACTS_ROOT', Path(tmp)):
            paths = []
            for slug, raw in self.sites.items():
                site = validate_site(slug, raw)
                receipt = dict(self.receipt, team=slug)
                target = save_receipt(receipt, 'manual__test', site)
                self.assertEqual(json.loads(Path(target).read_text()), receipt)
                self.assertIn(f'/{slug}/recaps/', target)
                paths.append(target)
            self.assertEqual(len(paths), len(set(paths)))

    def test_receipt_from_shared_game_for_wrong_team_is_rejected(self):
        receipt = dict(self.receipt, team='seahawks')
        with self.assertRaisesRegex(ValueError, 'different team'):
            validate_receipt(receipt, 'manual__test', self.sites['broncos'])

    def test_legacy_command_still_selects_default_seattle(self):
        self.assertEqual(ssh.command_arguments('check'), ['--check'])
        self.assertEqual(ssh.command_arguments('refresh ' + encode_request('manual__test')), ['--run-id=manual__test'])

    def test_encoded_request_roundtrip_authorizes_selected_site(self):
        site = validate_site('broncos', self.sites['broncos'])
        def authorize(value, *, authorize=False):
            self.assertTrue(authorize)
            return validate_site(value['slug'], value)
        with patch.object(host, 'site_settings', side_effect=authorize) as authorize_site:
            args = ssh.command_arguments('refresh ' + encode_request('manual__test', site))
            self.assertEqual(args[0], '--run-id=manual__test')
            self.assertEqual(json.loads(args[1].split('=', 1)[1]), site)
            authorize_site.assert_called_once_with(site, authorize=True)
            token = base64.urlsafe_b64encode(json.dumps({'site': site}).encode()).decode().rstrip('=')
            self.assertEqual(ssh.command_arguments('check ' + token)[0], '--check')

    def test_path_authorization_failure_never_dispatches(self):
        site = self.sites['broncos']
        with patch.object(host, 'site_settings', side_effect=ValueError('Unregistered path')):
            with self.assertRaisesRegex(ValueError, 'Invalid encoded recap request'):
                ssh.command_arguments('refresh ' + encode_request('run', site))

    def test_malformed_requests_cannot_add_cli_arguments(self):
        for request in ({'runId': 'run', 'site': self.sites['broncos'], 'command': 'delete'},
                        {'runId': 'bad\nrun', 'site': self.sites['broncos']}):
            token = base64.urlsafe_b64encode(json.dumps(request).encode()).decode().rstrip('=')
            with patch.object(host, 'site_settings', return_value=self.sites['broncos']):
                with self.assertRaises(ValueError):
                    ssh.command_arguments('refresh ' + token)
        for command in ('refresh abc; echo hi', 'check && echo hi', 'refresh ' + 'a'*32769):
            with self.assertRaises(ValueError):
                ssh.command_arguments(command)


if __name__ == '__main__':
    unittest.main()
