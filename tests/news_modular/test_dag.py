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
from airflow.timetables.base import TimeRestriction
from airflow.timetables.trigger import CronTriggerTimetable

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

    def test_reviewed_patriots_activation_adds_only_its_two_named_tasks(self):
        sites = json.loads((ROOT / "config/active-sites.json").read_text())
        original = self.load(sites)
        self.assertNotIn("generate_article_patriots", original.task_ids)
        sites["patriots"]["enabled"] = True
        expanded = self.load(sites)
        self.assertEqual(set(expanded.task_ids) - set(original.task_ids),
                         {"generate_article_patriots", "save_run_receipt_patriots"})
        self.assertEqual(set(original.task_ids) - set(expanded.task_ids), set())
        self.assertEqual(expanded.get_task("generate_article_patriots").task_display_name,
                         "Generate article: New England Patriots")

    def test_readable_names_survive_airflow_serialization(self):
        encoded = DagSerialization.to_dict(self.load())
        restored = DagSerialization.deserialize_dag(encoded["dag"], encoded.get("client_defaults"))
        self.assertEqual(restored.get_task("generate_article_broncos").task_display_name,
                         "Generate article: Denver Broncos")
        self.assertEqual(restored.get_task("save_run_receipt_seahawks").task_display_name,
                         "Save receipt: Seattle Seahawks")
        self.assertEqual(restored.get_task("save_run_receipt_broncos").upstream_task_ids,
                         {"generate_article_broncos"})

    def scheduled_runs(self, dag, start, count):
        # Traverse a fixed date range without depending on the test machine's clock.
        restriction = TimeRestriction(earliest=start, latest=None, catchup=True)
        previous = None
        for _ in range(count):
            info = dag.timetable.next_dagrun_info(
                last_automated_data_interval=previous, restriction=restriction,
            )
            self.assertIsNotNone(info)
            yield info
            previous = info.data_interval

    def test_schedule_runs_only_tuesday_and_friday_at_eight_pacific(self):
        dag = self.load()
        self.assertIsInstance(dag.timetable, CronTriggerTimetable)
        self.assertFalse(dag.catchup)
        self.assertEqual(dag.max_active_runs, 1)
        self.assertEqual(dag.max_active_tasks, 1)
        runs = list(self.scheduled_runs(
            dag, pendulum.datetime(2026, 9, 17, tz="America/Los_Angeles"), 6,
        ))
        self.assertEqual(
            [run.run_after.in_timezone("America/Los_Angeles").strftime("%Y-%m-%d %a %H:%M") for run in runs],
            ["2026-09-18 Fri 08:00", "2026-09-22 Tue 08:00", "2026-09-25 Fri 08:00",
             "2026-09-29 Tue 08:00", "2026-10-02 Fri 08:00", "2026-10-06 Tue 08:00"],
        )

    def test_schedule_keeps_eight_pacific_across_both_dst_changes(self):
        dag = self.load()
        cases = [
            ((2026, 3, 5), ["2026-03-06 16:00", "2026-03-10 15:00", "2026-03-13 15:00"]),
            ((2026, 10, 29), ["2026-10-30 15:00", "2026-11-03 16:00", "2026-11-06 16:00"]),
        ]
        for start, expected_utc in cases:
            with self.subTest(start=start):
                runs = list(self.scheduled_runs(
                    dag, pendulum.datetime(*start, tz="America/Los_Angeles"), 3,
                ))
                self.assertEqual(
                    [run.run_after.in_timezone("UTC").strftime("%Y-%m-%d %H:%M") for run in runs],
                    expected_utc,
                )
                self.assertEqual(
                    [run.run_after.in_timezone("America/Los_Angeles").strftime("%a %H:%M") for run in runs],
                    ["Fri 08:00", "Tue 08:00", "Fri 08:00"],
                )

    def test_trigger_logical_date_preserves_the_publication_day_when_delayed(self):
        from sfz_news_hook import publication_day

        dag = self.load()
        runs = self.scheduled_runs(
            dag, pendulum.datetime(2026, 9, 17, tz="America/Los_Angeles"), 2,
        )
        for info, expected_day in zip(runs, ("2026-09-18", "2026-09-22")):
            with self.subTest(publication_day=expected_day):
                self.assertEqual(info.logical_date, info.run_after)
                self.assertEqual(info.data_interval.start, info.data_interval.end)
                delayed_start = info.run_after.add(days=1)
                context = {"logical_date": info.logical_date,
                           "dag_run": type("Run", (), {"start_date": delayed_start})()}
                for site in self.sites.values():
                    self.assertEqual(publication_day(context, site["timezone"]), expected_day)

    def test_each_task_uses_its_own_site_and_existing_news_hook(self):
        dag = self.load()
        context = {"run_id": "manual", "logical_date": pendulum.datetime(2026, 9, 12, 16, tz="UTC")}
        with patch("airflow.sdk.get_current_context", return_value=context), \
                patch("fan_zone_photo_credits.read_task_catalog", return_value={"schema_version": 1, "assets": {}, "sha256": {}}) as credits, \
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
                news.assert_called_with("manual", "2026-09-12", site, photo_credits=credits.return_value)
                expected = f"/opt/airflow/artifacts/{slug}/news/receipt.json"
                save_receipt.return_value = expected
                saver = dag.get_task(f"save_run_receipt_{slug}")
                self.assertEqual(saver.python_callable(result), expected)
                validate_receipt.assert_called_with(receipt, "manual", "2026-09-12", site)
                save_receipt.assert_called_with(receipt, "manual", site)
            self.assertEqual(news.call_count, 2)
            self.assertEqual(credits.call_count, 2)
            self.assertEqual(save_receipt.call_count, 2)


if __name__ == "__main__":
    unittest.main()
