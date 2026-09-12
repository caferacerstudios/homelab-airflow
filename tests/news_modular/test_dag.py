"""Parse the one updated DAG using real Airflow without reading the Variable."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import pendulum
from airflow.dag_processing.dagbag import BundleDagBag
from airflow.sdk import Variable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dags"))
from fan_zone_config import validate_sites


class DailyNewsDagTests(unittest.TestCase):
    def load(self):
        bag = BundleDagBag(dag_folder=str(ROOT / "dags/sfz_daily_article.py"), bundle_path=ROOT / "dags")
        self.assertEqual(bag.import_errors, {})
        self.assertEqual(set(bag.dags), {"sfz_daily_article"})
        return bag.dags["sfz_daily_article"]

    def test_existing_dag_maps_sites_without_parse_time_variable_access(self):
        with patch.object(Variable, "get", side_effect=AssertionError("parse-time Variable access")):
            dag = self.load()
        self.assertEqual(set(dag.task_ids), {"load_active_sites", "generate_article", "save_run_receipt"})
        generator, saver = dag.get_task("generate_article"), dag.get_task("save_run_receipt")
        self.assertTrue(generator.is_mapped)
        self.assertEqual(generator.upstream_task_ids, {"load_active_sites"})
        self.assertEqual(generator.map_index_template, "{{ site_slug }}")
        self.assertTrue(saver.is_mapped)
        self.assertEqual(saver.upstream_task_ids, {"generate_article"})

    def test_schedule_remains_daily_eight_pacific_across_dst(self):
        dag = self.load()
        for month, day in [(9, 10), (3, 8), (11, 1)]:
            start = pendulum.datetime(2026, month, day, tz="America/Los_Angeles")
            next_run = dag.timetable._get_next(start)
            self.assertEqual(next_run.in_timezone("America/Los_Angeles").strftime("%H:%M"), "08:00")

    def test_each_team_dispatches_through_existing_news_hook(self):
        sites = validate_sites(json.loads((Path(__file__).parent / "fixtures/active-sites.json").read_text()))
        context = {"run_id": "manual", "logical_date": pendulum.datetime(2026, 9, 12, 16, tz="UTC")}
        generator = self.load().get_task("generate_article").python_callable
        with patch("airflow.sdk.get_current_context", return_value=context), patch("sfz_news_hook.NewsRefreshHook.refresh") as news:
            for slug in ("seahawks", "broncos"):
                receipt = {"team": slug, "runId": "manual"}
                news.return_value = receipt
                self.assertEqual(generator(sites[slug]), {"site": sites[slug], "receipt": receipt})
                news.assert_called_with("manual", "2026-09-12", sites[slug])
            self.assertEqual(news.call_count, 2)


if __name__ == "__main__":
    unittest.main()
