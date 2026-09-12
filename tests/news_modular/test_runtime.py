import base64
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dags"))
from fan_zone_config import validate_sites
import fan_zone_tasks as tasks
import sfz_news_hook as hook

FIXTURE = Path(__file__).parent / "fixtures/active-sites.json"


class NewsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.value = json.loads(FIXTURE.read_text())
        self.sites = validate_sites(self.value)

    def test_variable_controls_both_seattle_and_denver_on_each_run(self):
        sdk = types.ModuleType("airflow.sdk")
        sdk.Variable = MagicMock()
        sdk.Variable.get.return_value = self.value
        with patch.dict(sys.modules, {"airflow.sdk": sdk}):
            self.assertEqual([s["slug"] for s in tasks.active_sites()], ["broncos", "seahawks"])
            self.value["seahawks"]["enabled"] = False
            self.assertEqual([s["slug"] for s in tasks.active_sites()], ["broncos"])
            self.value["broncos"]["enabled"] = False
            self.assertEqual(tasks.active_sites(), [])
            sdk.Variable.get.assert_called_with("fan_zone_active_sites", default=None, deserialize_json=True)

    def test_missing_variable_uses_checked_in_configuration(self):
        sdk = types.ModuleType("airflow.sdk")
        sdk.Variable = MagicMock()
        sdk.Variable.get.return_value = None
        with patch.dict(sys.modules, {"airflow.sdk": sdk}), patch.object(tasks, "DEFAULT_SITES", FIXTURE):
            self.assertEqual(tasks.active_sites(), [self.sites["broncos"], self.sites["seahawks"]])

    def test_receipts_keep_existing_seattle_location_and_separate_denver(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tasks, "ARTIFACTS_ROOT", Path(folder)):
            paths = []
            for slug in ("seahawks", "broncos"):
                receipt = {"team": slug, "runId": "same_run"}
                path = Path(tasks.save_receipt(receipt, "same_run", self.sites[slug]))
                self.assertEqual(json.loads(path.read_text()), receipt)
                self.assertEqual(path.relative_to(folder).parts[:2], (slug, "news"))
                paths.append(path)
            self.assertNotEqual(*paths)
            with self.assertRaises(ValueError):
                tasks.save_receipt({"team": "seahawks", "runId": "same_run"}, "same_run", self.sites["broncos"])

    def test_same_ssh_connection_carries_team_and_legacy_requests(self):
        receipt = {"schema_version": 1, "runId": "manual", "publicationDay": "2026-09-12",
                   "team": "broncos", "updatedAt": "2026-09-12T08:00:00Z", "articleCount": 1,
                   "generatedCount": 0, "openaiRequestCount": 0, "files": {"articles.json": "a" * 64}}
        ssh = MagicMock()
        shim = types.ModuleType("airflow.providers.ssh.hooks.ssh")
        shim.SSHHook = MagicMock(return_value=ssh)
        ssh.exec_ssh_client_command.return_value = (0, ("SFZ_NEWS_RECEIPT=" + json.dumps(receipt)).encode(), b"")
        with patch.dict(sys.modules, {"airflow.providers.ssh.hooks.ssh": shim}):
            self.assertEqual(hook.NewsRefreshHook().refresh("manual", "2026-09-12", self.sites["broncos"]), receipt)
            self.assertEqual(shim.SSHHook.call_args.kwargs["ssh_conn_id"], "sfz_news_host")
            token = ssh.exec_ssh_client_command.call_args.args[1].removeprefix("refresh ")
            payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            self.assertEqual(payload["site"], self.sites["broncos"])
            self.assertEqual(set(payload), {"runId", "publicationDay", "site"})
            with self.assertRaises(ValueError):
                hook.NewsRefreshHook().refresh("manual", "2026-09-12", self.sites["seahawks"])
            hook.NewsRefreshHook().refresh("manual", "2026-09-12")
            token = ssh.exec_ssh_client_command.call_args.args[1].removeprefix("refresh ")
            self.assertEqual(json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))),
                             {"runId": "manual", "publicationDay": "2026-09-12"})

    def test_encoded_hook_request_is_accepted_by_real_host_entrypoint(self):
        path = ROOT / "deployment/news/ssh_entrypoint.py"
        spec = importlib.util.spec_from_file_location("news_modular_entrypoint", path)
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(path.parent), *sys.path]):
            spec.loader.exec_module(module)
            for slug in ("seahawks", "broncos"):
                args = module.command_arguments("refresh " + tasks.encode_request("manual", self.sites[slug], "2026-09-12"))
                self.assertEqual(args[:2], ["--run-id=manual", "--publication-day=2026-09-12"])
                self.assertEqual(json.loads(args[2].split("=", 1)[1]), self.sites[slug])

    def test_publication_day_and_request_limits(self):
        context = {"logical_date": datetime(2026, 9, 12, 6, 30, tzinfo=timezone.utc)}
        self.assertEqual(hook.publication_day(context), "2026-09-11")
        self.assertEqual(hook.publication_day(context, "America/Denver"), "2026-09-12")
        for run_id in ("", "x" * 513, "line\nbreak"):
            with self.assertRaises(ValueError):
                tasks.encode_request(run_id, self.sites["broncos"], "2026-09-12")
        with patch.object(tasks, "MAX_REQUEST_BYTES", 10), self.assertRaises(ValueError):
            tasks.encode_request("manual", self.sites["broncos"], "2026-09-12")


if __name__ == "__main__":
    unittest.main()
