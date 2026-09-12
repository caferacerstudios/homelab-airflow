"""Exercise visible team tasks with real Airflow and an isolated Variable fixture."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import pendulum
from airflow.dag_processing.dagbag import BundleDagBag
from airflow.sdk import Variable
from airflow.serialization.serialized_objects import DagSerialization

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dags"))


class DailyNewsDagTests(unittest.TestCase):
    def setUp(self):
        self.sites = json.loads((Path(__file__).parent / "fixtures/active-sites.json").read_text())

    def load(self, sites=None):
        with patch.object(Variable, "get", return_value=deepcopy(self.sites if sites is None else sites)) as get_variable:
            bag = BundleDagBag(dag_folder=str(ROOT / "dags/sfz_daily_article.py"), bundle_path=ROOT / "dags")
        self.assertEqual(bag.import_errors, {})
        self.assertEqual(set(bag.dags), {"sfz_daily_article"})
        get_variable.assert_called_once_with("fan_zone_active_sites", default=None, deserialize_json=True)
        return bag.dags["sfz_daily_article"]

    def test_each_enabled_team_has_separate_named_unmapped_tasks(self):
        dag = self.load()
        self.assertEqual(dag.task_ids, ["generate_article_broncos", "save_run_receipt_broncos",
                                        "generate_article_seahawks", "save_run_receipt_seahawks"])
        for slug, title in (("seahawks", "Seattle Seahawks"), ("broncos", "Denver Broncos")):
            generator = dag.get_task(f"generate_article_{slug}")
            saver = dag.get_task(f"save_run_receipt_{slug}")
            self.assertFalse(generator.is_mapped)
            self.assertFalse(saver.is_mapped)
            self.assertEqual(generator.task_display_name, f"Generate article: {title}")
            self.assertEqual(saver.task_display_name, f"Save receipt: {title}")
            self.assertEqual(generator.upstream_task_ids, set())
            self.assertEqual(generator.downstream_task_ids, {saver.task_id})
            self.assertEqual(saver.upstream_task_ids, {generator.task_id})
            self.assertEqual(saver.op_args[0].operator.task_id, generator.task_id)
            self.assertEqual(generator.op_args[0]["slug"], slug)

    def test_variable_changes_rebuild_nodes_and_keep_stable_ids(self):
        original = self.load()
        reversed_sites = dict(reversed(list(self.sites.items())))
        self.assertEqual(self.load(reversed_sites).task_ids, original.task_ids)
        self.sites["broncos"]["enabled"] = False
        self.assertEqual(self.load().task_ids, ["generate_article_seahawks", "save_run_receipt_seahawks"])
        patriots = deepcopy(self.sites["broncos"])
        patriots.update(enabled=True, name="Patriots", city="New England", abbreviation="NE",
                        division="AFC East", news_snapshot_dir="/var/lib/patriotsfz-news/current",
                        news_photos_dir="/var/lib/patriotsfz-news/photos",
                        source_domains=["patriots.com", "nfl.com"],
                        prompts={"article": "Write for New England Patriots fans."})
        self.sites["patriots"] = patriots
        expanded = self.load()
        self.assertEqual(expanded.task_ids, ["generate_article_patriots", "save_run_receipt_patriots",
                                             "generate_article_seahawks", "save_run_receipt_seahawks"])
        self.assertEqual(expanded.get_task("generate_article_patriots").task_display_name,
                         "Generate article: New England Patriots")
        self.sites["seahawks"]["city"] = "Seattle updated"
        renamed = self.load()
        self.assertEqual(renamed.task_ids, expanded.task_ids)
        self.assertEqual(renamed.get_task("generate_article_seahawks").task_display_name,
                         "Generate article: Seattle updated Seahawks")

    def test_readable_names_survive_airflow_serialization(self):
        encoded = DagSerialization.to_dict(self.load())
        restored = DagSerialization.deserialize_dag(encoded["dag"], encoded.get("client_defaults"))
        self.assertEqual(restored.get_task("generate_article_broncos").task_display_name,
                         "Generate article: Denver Broncos")
        self.assertEqual(restored.get_task("save_run_receipt_seahawks").task_display_name,
                         "Save receipt: Seattle Seahawks")
        self.assertEqual(restored.get_task("save_run_receipt_broncos").upstream_task_ids,
                         {"generate_article_broncos"})

    def test_schedule_remains_daily_eight_pacific_across_dst(self):
        dag = self.load()
        self.assertFalse(dag.catchup)
        self.assertEqual(dag.max_active_runs, 1)
        self.assertEqual(dag.max_active_tasks, 1)
        for month, day in [(9, 10), (3, 8), (11, 1)]:
            start = pendulum.datetime(2026, month, day, tz="America/Los_Angeles")
            next_run = dag.timetable._get_next(start)
            self.assertEqual(next_run.in_timezone("America/Los_Angeles").strftime("%H:%M"), "08:00")

    def test_each_task_uses_its_own_site_and_existing_news_hook(self):
        dag = self.load()
        context = {"run_id": "manual", "logical_date": pendulum.datetime(2026, 9, 12, 16, tz="UTC")}
        with patch("airflow.sdk.get_current_context", return_value=context), \
                patch("sfz_news_hook.NewsRefreshHook.refresh") as news, \
                patch("sfz_news_hook.validate_receipt") as validate_receipt, \
                patch("fan_zone_tasks.save_receipt") as save_receipt:
            for slug in ("seahawks", "broncos"):
                generator = dag.get_task(f"generate_article_{slug}")
                site = generator.op_args[0]
                receipt = {"team": slug, "runId": "manual", "generatedCount": 1,
                           "articleCount": 1, "openaiRequestCount": 2}
                news.return_value = receipt
                result = generator.python_callable(*generator.op_args, **generator.op_kwargs)
                self.assertEqual(result, {"site": site, "receipt": receipt})
                news.assert_called_with("manual", "2026-09-12", site)
                expected = f"/opt/airflow/artifacts/{slug}/news/receipt.json"
                save_receipt.return_value = expected
                saver = dag.get_task(f"save_run_receipt_{slug}")
                self.assertEqual(saver.python_callable(result), expected)
                validate_receipt.assert_called_with(receipt, "manual", "2026-09-12", site)
                save_receipt.assert_called_with(receipt, "manual", site)
            self.assertEqual(news.call_count, 2)
            self.assertEqual(save_receipt.call_count, 2)


if __name__ == "__main__":
    unittest.main()
