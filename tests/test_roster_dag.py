"""Verify actual Airflow 3.3.1 graph and local-time schedule without provider work."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import pendulum
from airflow.dag_processing.dagbag import BundleDagBag

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dags"))


class RosterDagTests(unittest.TestCase):
    def load(self, sites):
        with patch("airflow.sdk.Variable.get", return_value=sites):
            bag = BundleDagBag(dag_folder=str(ROOT / "dags/sfz_roster_refresh.py"),
                               bundle_path=ROOT / "dags")
        self.assertEqual(bag.import_errors, {})
        return bag.dags["sfz_roster_refresh"]

    def test_named_team_branches_and_existing_variable_flags(self):
        sites = json.loads((ROOT / "config/active-sites.json").read_text())
        sites["patriots"]["enabled"] = True  # Activate only in this isolated test.
        dag = self.load(sites)
        self.assertEqual(len(dag.tasks), 2 * len(sites))
        self.assertEqual(dag.max_active_tasks, 1)
        self.assertEqual(dag.max_active_runs, 1)
        self.assertFalse(dag.catchup)
        for slug, site in sites.items():
            task = dag.get_task("refresh_roster_" + slug)
            self.assertEqual(task.op_args[0]["slug"], slug)
            self.assertIn(site["city"] + " " + site["name"], task.task_display_name)
            self.assertEqual(task.downstream_task_ids, {"save_run_receipt_" + slug})
            self.assertEqual(task.pool, "default_pool")
            self.assertEqual(task.retries, 1)
            self.assertEqual(task.retry_delay.total_seconds(), 120)
            self.assertEqual(dag.get_task("save_run_receipt_" + slug).retries, 0)
        disabled = deepcopy(sites)
        disabled["broncos"]["enabled"] = False
        dag = self.load(disabled)
        self.assertNotIn("refresh_roster_broncos", dag.task_ids)
        self.assertEqual(len(dag.tasks), 2 * (len(sites) - 1))

    def test_two_pacific_slots_including_dst_boundaries(self):
        dag = self.load({})
        self.assertEqual(dag.task_ids, [])
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
            self.assertEqual(slots, ["05:30", "17:30"])


if __name__ == "__main__":
    unittest.main()
