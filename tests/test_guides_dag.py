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
    def load(self, sites, enabled=("seahawks",)):
        with patch("airflow.sdk.Variable.get", return_value=sites), patch(
                "fan_zone_guide_config.load_policy", return_value={"enabled_sites": list(enabled)}):
            bag = BundleDagBag(dag_folder=str(ROOT / "dags/sfz_game_guides.py"), bundle_path=ROOT / "dags")
        self.assertEqual(bag.import_errors, {})
        return bag.dags["sfz_game_guides"]

    def test_new_pipeline_is_paused_serialized_and_independently_enabled(self):
        sites = json.loads((ROOT / "config/active-sites.json").read_text())
        dag = self.load(sites)
        self.assertEqual(set(dag.task_ids), {"refresh_guides_seahawks", "save_run_receipt_seahawks"})
        self.assertTrue(dag.is_paused_upon_creation)
        self.assertFalse(dag.catchup)
        self.assertEqual(dag.max_active_tasks, 1)
        self.assertEqual(dag.max_active_runs, 1)
        task = dag.get_task("refresh_guides_seahawks")
        self.assertEqual(task.downstream_task_ids, {"save_run_receipt_seahawks"})
        self.assertEqual(task.pool, "default_pool")
        self.assertEqual(task.retries, 0)
        self.assertEqual(task.execution_timeout.total_seconds(), 35 * 60)
        disabled = deepcopy(sites)
        disabled["seahawks"]["enabled"] = False
        self.assertEqual(self.load(disabled).task_ids, [])
        self.assertEqual(set(self.load(sites, ("seahawks", "broncos")).task_ids),
                         {prefix + slug for prefix in ("refresh_guides_", "save_run_receipt_")
                          for slug in ("seahawks", "broncos")})

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
