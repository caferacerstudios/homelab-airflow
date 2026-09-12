"""Exercise source admission, durable receipts, and completed-game accounting offline."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dags"))
from fan_zone_config import eventspy_request_id, eventspy_slot, validate_sites
from sfz_ticket_bridge import TicketCollectorHook
spec = importlib.util.spec_from_file_location("modular_eventspy_bridge", ROOT / "deployment/eventspy/bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def sites_fixture():
    # Exercise the checked-in five-team configuration, including each team's
    # distinct output root. Do not remap additional teams to Denver's directory.
    sites = json.loads((ROOT / "config/active-sites.json").read_text())
    return validate_sites(sites)


def coverage_fixture(slug):
    return json.loads((ROOT / "deployment/eventspy/coverage" / f"{slug}.json").read_text())


class FakeHost:
    def __init__(self):
        self.calls = []
        self.timer_active = False
        self.interrupted = False
        self.summary_delta = 0
        self.ensure_schedule_calls = []

    def ensure_ownership(self):
        if self.timer_active:
            raise ValueError("Original timer active")

    def ensure_schedule(self, site, coverage):
        self.ensure_schedule_calls.append(site["slug"])

    def run(self, args):
        self.calls.append(args)
        if self.interrupted:
            raise TimeoutError("Docker client interrupted")
        team, slot = args
        rows = coverage_fixture(team)
        results = []
        for index, row in enumerate(rows):
            outcome = ("EVENTSPY_COLLECTION_SKIPPED" if index == 0 else "EVENTSPY_SOURCE_UNAVAILABLE"
                       if row["state"] == "unavailable" else "EVENTSPY_COLLECTION_SUCCESS")
            results.append(dict(team=team, week=row["week"], gameId=row.get("gameId") or str(90000 + index),
                                sourceEventId=row.get("sourceEventId"), outcome=outcome))
        summary = dict(outcome="EVENTSPY_SEASON_SUCCESS", team=team, slot=slot,
                       authorized=sum(row["state"] == "authorized" for row in rows),
                       succeeded=sum(row["outcome"] == "EVENTSPY_COLLECTION_SUCCESS" for row in results), failed=0,
                       unavailable=sum(row["outcome"] == "EVENTSPY_SOURCE_UNAVAILABLE" for row in results),
                       skipped=1 + self.summary_delta, unresolved=0, results=results)
        return subprocess.CompletedProcess(args, 0, json.dumps(summary), "")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue, self.install = self.root / "queue", self.root / "install"
        for folder in (self.queue / "requests", self.queue / "responses", self.install):
            folder.mkdir(parents=True)
        self.now = datetime(2026, 9, 12, 13, 0, 5, tzinfo=timezone.utc)  # 06:00 Pacific
        (self.install / "settings.json").write_text(json.dumps({"version": 2, "image_id": "sha256:" + "a" * 64,
                                                               "activated_at": "2026-09-12T12:30:00+00:00"}))
        self.host = FakeHost()
        self.app = bridge.Bridge(self.root / "runtime", self.queue, self.install, self.host, lambda: self.now)
        self.app.init()
        self.sites = sites_fixture()
        self.job_patch = patch.object(self.app, "job", side_effect=lambda request, site, settings: ([site["slug"], request["slot"]], coverage_fixture(site["slug"])))
        self.job_patch.start()
        self.addCleanup(self.job_patch.stop)

    def request(self, slug="seahawks", slot="2026-09-12T13:00:00+00:00", site=None, **extra):
        site = deepcopy(site or self.sites[slug])
        slot = eventspy_slot(slot)
        request = {"version": 2, "operation": "collect", "slot": slot, "site": site,
                   "request_id": eventspy_request_id(slot, site), **extra}
        path = self.queue / "requests" / (request["request_id"] + ".json")
        path.write_text(json.dumps(request))
        self.app.process()
        return json.loads((self.queue / "responses" / path.name).read_text())

    def test_completed_game_skip_is_success_and_outputs_remain_team_specific(self):
        for slug in self.sites:
            receipt = self.request(slug)
            self.assertEqual(receipt["status"], "success")
            self.assertEqual(receipt["summary"]["skipped"], 1)
            self.assertEqual(receipt["team"], slug)
        self.assertEqual([args[0] for args in self.host.calls], list(self.sites))
        self.assertEqual(self.app.status()["attempts"], len(self.sites))
        self.assertEqual(len({site["eventspy"]["output_dir"] for site in self.sites.values()}), len(self.sites))

    def test_same_request_and_changed_configuration_cannot_repeat_team_slot(self):
        first = self.request()
        self.assertEqual(self.request(), first)
        changed = deepcopy(self.sites["seahawks"])
        changed["prompts"]["article"] += " New text."
        self.assertEqual(self.request(site=changed)["status"], "skipped")
        self.assertEqual(len(self.host.calls), 1)

    def test_legacy_timer_and_wrong_accounting_fail_without_automatic_retry(self):
        self.host.timer_active = True
        self.assertEqual(self.request()["status"], "failed")
        self.assertFalse(self.host.calls)
        self.host.timer_active = False
        self.host.summary_delta = 1
        response = self.request("broncos")
        self.assertEqual(response["status"], "failed")
        self.assertIn("every reviewed game", response["message"])
        self.assertEqual(self.request("broncos"), response)
        self.assertEqual(len(self.host.calls), 1)

    def test_future_stale_backfill_pre_activation_and_off_schedule_do_not_run(self):
        for slot in ("2026-09-12T16:00:00+00:00", "2026-09-11T13:00:00+00:00",
                     "2026-09-12T10:00:00+00:00", "2026-09-12T13:01:00+00:00"):
            with self.subTest(slot=slot):
                self.assertEqual(self.request(slot=slot)["status"], "skipped")
        self.assertFalse(self.host.calls)
        self.now = datetime(2026, 9, 12, 14, 31, tzinfo=timezone.utc)
        self.assertEqual(self.request()["status"], "skipped")

    def test_interrupted_docker_blocks_both_teams_and_survives_bridge_restart(self):
        self.host.interrupted = True
        self.assertEqual(self.request()["status"], "failed")
        self.assertEqual(self.app.status()["unresolved_attempts"], 1)
        self.host.interrupted = False
        self.assertEqual(self.request("broncos")["status"], "failed")
        self.assertEqual(len(self.host.calls), 1)
        reopened = bridge.Bridge(self.app.root, self.queue, self.install)
        self.assertEqual(reopened.status()["unresolved_attempts"], 1)

    def test_v1_arbitrary_fields_and_symlink_inputs_never_launch(self):
        self.assertEqual(self.request(version=1)["status"], "failed")
        self.assertEqual(self.request("broncos", command="touch /tmp/bad")["status"], "failed")
        target = self.root / "target.json"
        target.write_text("{}")
        name = "c" * 64 + ".json"
        (self.queue / "requests" / name).symlink_to(target)
        self.app.process()
        self.assertTrue(target.exists())
        self.assertEqual(json.loads((self.queue / "responses" / name).read_text())["status"], "failed")
        self.assertFalse(self.host.calls)

    def test_client_roundtrip_keeps_config_and_team_and_checks_receipt_identity(self):
        hook = TicketCollectorHook(self.queue)
        with patch("sfz_ticket_bridge.time.sleep", side_effect=lambda seconds: self.app.process()):
            result = hook.request("collect", site=self.sites["broncos"], slot="2026-09-12T13:00:00+00:00", timeout=2)
        self.assertEqual(result["team"], "broncos")
        path = self.queue / "responses" / (result["request_id"] + ".json")
        path.write_text(json.dumps(dict(result, team="seahawks")))
        with self.assertRaisesRegex(RuntimeError, "mismatched"):
            hook.request("collect", site=self.sites["broncos"], slot=result["slot"], timeout=2)

    def test_equivalent_timestamps_have_one_canonical_request_and_cache_key(self):
        hook = TicketCollectorHook(self.queue)
        with patch("sfz_ticket_bridge.time.sleep", side_effect=lambda seconds: self.app.process()):
            a = hook.request("collect", site=self.sites["broncos"], slot="2026-09-12T13:00:00+00:00", timeout=2)
            b = hook.request("collect", site=self.sites["broncos"], slot="2026-09-12T13:00:00.000Z", timeout=2)
        self.assertEqual(a, b)
        self.assertEqual(a["slot"], "2026-09-12T13:00:00.000Z")
        self.assertEqual(a["summary"]["slot"], a["slot"])
        self.assertEqual(len(self.host.calls), 1)

    def test_docker_job_mounts_only_selected_team_output_with_pinned_image_and_helper(self):
        self.job_patch.stop()
        (self.install / "coverage").mkdir()
        (self.install / "coverage/broncos.json").write_text(json.dumps(coverage_fixture("broncos")))
        schedule = self.app.root / "schedules/broncos.json"
        schedule.parent.mkdir(parents=True)
        schedule.write_text("{}")
        site = deepcopy(self.sites["broncos"])
        site["eventspy"].update(schedule_file=str(schedule), output_dir=str(self.root / "broncos-output"))
        request = {"slot": "2026-09-12T13:00:00.000Z", "request_id": "d" * 64}
        with patch.object(bridge.os, "chown"):
            args, coverage = self.app.job(request, site, self.app.settings())
        self.assertEqual(len(coverage), 17)
        self.assertIn("sha256:" + "a" * 64, args)
        self.assertIn("--pull=never", args)
        self.assertIn("type=bind,src=" + str(self.install / "collector-coverage.mjs") + ",dst=/app/collector-coverage.mjs,readonly", args)
        mounts = [args[index + 1] for index, arg in enumerate(args[:-1]) if arg == "--mount"]
        self.assertEqual(sum(",dst=/output" in mount for mount in mounts), 1)
        self.assertTrue(any(str(self.root / "broncos-output") in mount and ",dst=/output" in mount for mount in mounts))
        self.assertEqual(self.host.ensure_schedule_calls, ["broncos"])
        config = json.loads((self.app.root / "jobs" / request["request_id"] / "request.json").read_text())
        self.assertEqual(config["site"]["slug"], "broncos")
        self.assertNotIn("prompts", config["site"])
        self.assertEqual(config["schedule_file"], "/run/eventspy/schedule.json")

    def test_health_uses_unique_receipts_and_never_starts_collector(self):
        with patch("sfz_ticket_bridge.time.sleep", side_effect=lambda seconds: self.app.process()):
            a = TicketCollectorHook(self.queue).request("health", timeout=2)
            b = TicketCollectorHook(self.queue).request("health", timeout=2)
        self.assertNotEqual(a["request_id"], b["request_id"])
        self.assertEqual(a["status"], "success")
        self.assertFalse(self.host.calls)


class ConfigTests(unittest.TestCase):
    def test_paths_cannot_cross_teams_and_original_news_only_variable_is_valid(self):
        sites = sites_fixture()
        self.assertEqual(sites["seahawks"]["eventspy"]["output_dir"], "/var/lib/sfz-eventspy-mirror/dev/public")
        # This assertion deliberately covers the original two-team Variable.
        news_only = {slug: deepcopy(sites[slug]) for slug in ("seahawks", "broncos")}
        for site in news_only.values():
            site.pop("eventspy")
        self.assertEqual(set(validate_sites(news_only)), {"seahawks", "broncos"})
        for field, bad in (("output_dir", sites["seahawks"]["eventspy"]["output_dir"]),
                           ("schedule_file", "/etc/passwd"), ("coverage_file", "../seahawks.json")):
            changed = deepcopy(sites)
            changed["broncos"]["eventspy"][field] = bad
            with self.assertRaises(ValueError):
                validate_sites(changed)

    def test_completed_summary_requires_correct_team_slot_and_complete_accounting(self):
        rows = coverage_fixture("seahawks")
        summary = json.loads(FakeHost().run(["seahawks", "slot"]).stdout)
        self.assertIs(bridge.validate_summary(summary, {"slug": "seahawks"}, "slot", rows), summary)
        with self.assertRaises(ValueError):
            bridge.validate_summary(summary, {"slug": "broncos"}, "slot", rows)
        for change in ("duplicate", "wrong_event", "wrong_counter"):
            invalid = deepcopy(summary)
            if change == "duplicate":
                invalid["results"][1] = deepcopy(invalid["results"][0])
            elif change == "wrong_event":
                invalid["results"][0]["sourceEventId"] = "999999"
            else:
                invalid["results"][1]["outcome"] = "EVENTSPY_COLLECTION_SKIPPED"
            with self.assertRaises(ValueError):
                bridge.validate_summary(invalid, {"slug": "seahawks"}, "slot", rows)



if __name__ == "__main__":
    unittest.main()
