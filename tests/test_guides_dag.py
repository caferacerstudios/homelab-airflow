"""Validate the real Airflow graph and DST timetable without starting research."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dags"))
HAS_AIRFLOW = importlib.util.find_spec("airflow") is not None
if HAS_AIRFLOW:
    import pendulum
    from airflow.dag_processing.dagbag import BundleDagBag


@unittest.skipUnless(HAS_AIRFLOW, "Requires deployed Airflow 3.3.1 runtime")
class GuidesDagTests(unittest.TestCase):
    def load(self, sites):
        with patch("airflow.sdk.Variable.get", return_value=sites):
            bag = BundleDagBag(dag_folder=str(ROOT / "dags/sfz_game_guides.py"), bundle_path=ROOT / "dags")
        self.assertEqual(bag.import_errors, {})
        return bag.dags["sfz_game_guides"]

    def test_every_active_team_has_its_named_refresh_and_receipt_pair(self):
        sites = json.loads((ROOT / "config/active-sites.json").read_text())
        dag = self.load(sites)
        self.assertEqual(set(sites), {"seahawks", "broncos", "chiefs", "packers", "vikings"})
        self.assertEqual(set(dag.task_ids),
                         {prefix + slug for prefix in ("refresh_guides_", "save_run_receipt_")
                          for slug in sites})
        self.assertTrue(dag.is_paused_upon_creation)
        self.assertFalse(dag.catchup)
        self.assertEqual(dag.max_active_tasks, 1)
        self.assertEqual(dag.max_active_runs, 1)
        for slug, site in sites.items():
            with self.subTest(team=slug):
                task = dag.get_task("refresh_guides_" + slug)
                receipt = dag.get_task("save_run_receipt_" + slug)
                title = site["city"] + " " + site["name"]
                self.assertEqual(task.task_display_name, "Research game-day and viewing guides: " + title)
                self.assertEqual(receipt.task_display_name, "Save guide receipt: " + title)
                self.assertEqual(task.op_args[0]["slug"], slug)
                self.assertEqual(task.op_args[0]["news_snapshot_dir"], site["news_snapshot_dir"])
                self.assertEqual(task.downstream_task_ids, {receipt.task_id})
                self.assertEqual(task.upstream_task_ids, set())
                self.assertEqual(receipt.upstream_task_ids, {task.task_id})
                self.assertEqual(receipt.downstream_task_ids, set())
                self.assertEqual(task.pool, "default_pool")
                self.assertEqual(task.retries, 0)
                self.assertEqual(receipt.retries, 0)
                self.assertEqual(task.execution_timeout.total_seconds(), 35 * 60)
                self.assertEqual(receipt.execution_timeout.total_seconds(), 2 * 60)

    def test_disabling_one_team_preserves_all_other_team_tasks(self):
        sites = json.loads((ROOT / "config/active-sites.json").read_text())
        disabled = deepcopy(sites)
        disabled["seahawks"]["enabled"] = False
        self.assertEqual(set(self.load(disabled).task_ids),
                         {prefix + slug for prefix in ("refresh_guides_", "save_run_receipt_")
                          for slug in sites if slug != "seahawks"})

    def test_active_sites_alone_adds_and_removes_teams(self):
        sites = json.loads((ROOT / "config/active-sites.json").read_text())
        policy = (ROOT / "config/game-guides.json").read_bytes()
        subset = {"seahawks": sites["seahawks"]}
        self.assertEqual(set(self.load(subset).task_ids),
                         {"refresh_guides_seahawks", "save_run_receipt_seahawks"})
        subset["chiefs"] = sites["chiefs"]
        self.assertEqual(set(self.load(subset).task_ids),
                         {prefix + slug for prefix in ("refresh_guides_", "save_run_receipt_")
                          for slug in subset})
        del subset["seahawks"]
        self.assertEqual(set(self.load(subset).task_ids),
                         {"refresh_guides_chiefs", "save_run_receipt_chiefs"})
        self.assertEqual((ROOT / "config/game-guides.json").read_bytes(), policy)

    def test_no_active_sites_produces_no_tasks(self):
        self.assertEqual(self.load({}).task_ids, [])
        sites = json.loads((ROOT / "config/active-sites.json").read_text())
        for site in sites.values():
            site["enabled"] = False
        self.assertEqual(self.load(sites).task_ids, [])

    def test_daily_pacific_slot_through_dst_boundaries(self):
        dag = self.load({})
        for month, day in ((9, 12), (3, 8), (11, 1)):
            start = pendulum.datetime(2026, month, day, tz="America/Los_Angeles")
            end = start.add(days=1)
            instant = start.subtract(seconds=1)
            slots = []
            while True:
                instant = dag.timetable._get_next(instant)
                if instant >= end:
                    break
                slots.append(instant.in_timezone("America/Los_Angeles").strftime("%H:%M"))
            self.assertEqual(slots, ["04:45"])


if __name__ == "__main__":
    unittest.main()
