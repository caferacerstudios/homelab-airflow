"""Verify the real Airflow timetable, including Seattle clock changes."""
from pathlib import Path
import unittest

import pendulum
from airflow.dag_processing.dagbag import BundleDagBag


class DagScheduleTests(unittest.TestCase):
    def test_import_and_ten_slots_on_clock_change_days(self):
        folder = Path(__file__).resolve().parents[1] / "dags"
        bag = BundleDagBag(dag_folder=str(folder / "sfz_game_recaps.py"), bundle_path=folder)
        self.assertEqual(bag.import_errors, {})
        dag = bag.dags["sfz_game_recaps"]
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
            self.assertEqual(slots, ["00:45", "03:45", "06:45", "09:45", "12:45",
                                     "14:45", "16:45", "18:45", "20:45", "22:45"])


if __name__ == "__main__":
    unittest.main()
