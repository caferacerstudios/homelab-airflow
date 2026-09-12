"""Verify the real Airflow timetable, including Seattle clock changes."""
from collections import Counter
from pathlib import Path
import unittest
import sys
from unittest.mock import patch

import pendulum
from airflow.dag_processing.dagbag import BundleDagBag


class DagScheduleTests(unittest.TestCase):
    def test_import_and_ten_slots_on_clock_change_days(self):
        folder = Path(__file__).resolve().parents[1] / "dags"
        sys.path.insert(0, str(folder))
        sites = [{"slug": slug, "city": city, "name": name} for slug, city, name in [
            ("seahawks", "Seattle", "Seahawks"), ("broncos", "Denver", "Broncos"),
            ("packers", "Green Bay", "Packers"), ("vikings", "Minnesota", "Vikings"),
            ("chiefs", "Kansas City", "Chiefs")]]
        with patch("fan_zone_tasks.active_sites", return_value=sites):
            bag = BundleDagBag(dag_folder=str(folder / "sfz_nfl_refresh.py"), bundle_path=folder)
        self.assertEqual(bag.import_errors, {})
        dag = bag.dags["sfz_nfl_refresh"]
        self.assertEqual(len(dag.tasks), 10)
        for site in sites:
            slug = site["slug"]
            task = dag.get_task("refresh_nfl_snapshot_" + slug)
            self.assertEqual(task.pool, "balldontlie_api")
            self.assertEqual(task.op_args[0], site)
            self.assertEqual(task.task_display_name, "Refresh NFL: " + site["city"] + " " + site["name"])
            self.assertEqual(task.downstream_task_ids, {"save_run_receipt_" + slug})
        for month, day in [(9, 10), (3, 8), (11, 1)]:
            start = pendulum.datetime(2026, month, day, tz="America/Los_Angeles")
            stop = start.add(days=1)
            current = start.subtract(seconds=1)
            slots = []
            while True:
                current = dag.timetable._get_next(current)
                if current >= stop:
                    break
                slots.append(current.in_timezone("America/Los_Angeles").strftime("%H:%M"))
            self.assertEqual(slots, ["00:15", "03:15", "06:15", "09:15", "12:15",
                                     "14:15", "16:15", "18:15", "20:15", "22:15"])


if __name__ == "__main__":
    unittest.main()
