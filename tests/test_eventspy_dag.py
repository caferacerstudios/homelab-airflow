"""Real Airflow checks for separate team nodes, schedule, and manual-run suppression."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import pendulum
from airflow.dag_processing.dagbag import BundleDagBag
from airflow.sdk.exceptions import AirflowSkipException
from airflow.sdk import Variable
from airflow.serialization.serialized_objects import DagSerialization

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dags"))
sys.path.insert(0, str(ROOT / "tests"))
from test_eventspy_bridge import sites_fixture


class DagTests(unittest.TestCase):
    def load(self, sites=None):
        with patch.object(Variable, "get", return_value=sites_fixture() if sites is None else sites) as read:
            bag = BundleDagBag(dag_folder=str(ROOT / "dags/sfz_eventspy_collect.py"), bundle_path=ROOT / "dags")
        self.assertEqual(bag.import_errors, {})
        read.assert_called_once_with("fan_zone_active_sites", default=None, deserialize_json=True)
        return bag.dags["sfz_eventspy_collect"]

    def test_visible_unmapped_team_nodes_and_serialized_titles(self):
        dag = self.load()
        sites = sites_fixture()
        self.assertEqual(dag.task_ids, [task_id for slug in sorted(sites)
                                       for task_id in (f"collect_ticket_prices_{slug}", f"save_run_receipt_{slug}")])
        self.assertEqual(len(dag.task_ids), 10)
        encoded = DagSerialization.to_dict(dag)
        restored = DagSerialization.deserialize_dag(encoded["dag"], encoded.get("client_defaults"))
        for slug, site in sites.items():
            name = f"{site['city']} {site['name']}"
            task = dag.get_task(f"collect_ticket_prices_{slug}")
            self.assertFalse(task.is_mapped)
            self.assertEqual(task.op_args[0]["slug"], slug)
            self.assertEqual(task.downstream_task_ids, {f"save_run_receipt_{slug}"})
            self.assertEqual(restored.get_task(task.task_id).task_display_name, f"Collect tickets: {name}")
            self.assertEqual(task.pool, "balldontlie_api")
            self.assertEqual(task.retries, 0)

    def test_only_enabled_configured_teams_get_nodes_and_order_is_stable(self):
        sites = sites_fixture()
        expected = self.load().task_ids
        self.assertEqual(self.load(dict(reversed(list(sites.items())))).task_ids, expected)
        sites["broncos"]["enabled"] = False
        self.assertEqual(self.load(sites).task_ids, [task_id for task_id in expected if not task_id.endswith("_broncos")])
        sites["seahawks"].pop("eventspy")
        self.assertEqual(self.load(sites).task_ids, [task_id for task_id in expected if not task_id.endswith(("_broncos", "_seahawks"))])
        for slug in ("packers", "vikings", "chiefs"):
            sites[slug].pop("eventspy")
        self.assertEqual(self.load(sites).task_ids, [])

    def test_preserved_schedule_across_dst_and_no_catchup(self):
        dag = self.load()
        self.assertFalse(dag.catchup)
        self.assertEqual((dag.max_active_runs, dag.max_active_tasks), (1, 1))
        for date in ((2026, 9, 12), (2026, 11, 1), (2026, 3, 8)):
            current = pendulum.datetime(*date, tz="America/Los_Angeles")
            hours = []
            for _ in range(7):
                current = dag.timetable._get_next(current)
                hours.append(current.in_timezone("America/Los_Angeles").hour)
            self.assertEqual(hours, [3, 6, 9, 12, 15, 18, 21])

    def test_manual_and_backfill_skip_while_scheduled_uses_correct_team(self):
        task = self.load().get_task("collect_ticket_prices_broncos")
        context = {"dag_run": SimpleNamespace(run_type="manual"),
                   "data_interval_end": pendulum.datetime(2026, 9, 12, 13, tz="UTC")}
        with patch("airflow.sdk.get_current_context", return_value=context), patch("sfz_ticket_bridge.TicketCollectorHook.request") as request:
            for run_type in ("manual", "backfill"):
                context["dag_run"].run_type = run_type
                with self.assertRaises(AirflowSkipException):
                    task.python_callable(*task.op_args)
            request.assert_not_called()
            context["dag_run"].run_type = "scheduled"
            request.return_value = {"status": "success", "team": "broncos"}
            self.assertEqual(task.python_callable(*task.op_args)["team"], "broncos")
            request.assert_called_once_with("collect", slot="2026-09-12T13:00:00+00:00", site=task.op_args[0])


if __name__ == "__main__":
    unittest.main()
