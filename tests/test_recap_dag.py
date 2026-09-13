"""Verify the real Airflow graph and Tuesday schedule using an isolated Variable."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dags'))


class DagScheduleTests(unittest.TestCase):
    def setUp(self):
        self.sites = json.loads((ROOT / 'config/active-sites.json').read_text())
        self.sites['patriots']['enabled'] = True  # Isolated activation fixture.

    def load(self):
        with patch.object(Variable, 'get', return_value=deepcopy(self.sites)) as variable:
            bag = BundleDagBag(dag_folder=str(ROOT / 'dags/sfz_game_recaps.py'), bundle_path=ROOT / 'dags')
        self.assertEqual(bag.import_errors, {})
        variable.assert_called_once_with('fan_zone_active_sites', default=None, deserialize_json=True)
        return bag.dags['sfz_game_recaps']

    def test_six_separate_named_tasks_and_dependencies(self):
        dag = self.load()
        self.assertEqual(set(self.sites), {'seahawks', 'broncos', 'packers', 'vikings', 'chiefs', 'patriots'})
        self.assertEqual(set(dag.task_ids), {f'{prefix}_{slug}' for slug in self.sites
                         for prefix in ('generate_recaps', 'save_run_receipt')})
        for slug, site in self.sites.items():
            task = dag.get_task(f'generate_recaps_{slug}')
            self.assertFalse(task.is_mapped)
            self.assertEqual(task.pool, 'balldontlie_api')
            self.assertEqual(task.task_display_name, f"Generate game recaps: {site['city']} {site['name']}")
            self.assertEqual(task.downstream_task_ids, {f'save_run_receipt_{slug}'})
            self.assertEqual(task.op_args[0]['slug'], slug)
        encoded = DagSerialization.to_dict(dag)
        restored = DagSerialization.deserialize_dag(encoded['dag'], encoded.get('client_defaults'))
        self.assertEqual(restored.get_task('generate_recaps_chiefs').task_display_name,
                         'Generate game recaps: Kansas City Chiefs')

    def test_variable_changes_remove_only_disabled_team(self):
        self.sites['vikings']['enabled'] = False
        dag = self.load()
        self.assertNotIn('generate_recaps_vikings', dag.task_ids)
        self.assertIn('generate_recaps_seahawks', dag.task_ids)
        self.assertIn('generate_recaps_chiefs', dag.task_ids)

    def test_tuesday_0645_pacific_across_clock_changes(self):
        dag = self.load()
        self.assertFalse(dag.catchup)
        self.assertEqual(dag.max_active_runs, 1)
        self.assertEqual(dag.max_active_tasks, 1)
        for month, day in [(9, 10), (3, 8), (11, 1)]:
            start = pendulum.datetime(2026, month, day, tz='America/Los_Angeles')
            next_run = dag.timetable._get_next(start).in_timezone('America/Los_Angeles')
            self.assertEqual(next_run.strftime('%A %H:%M'), 'Tuesday 06:45')
            following = dag.timetable._get_next(next_run).in_timezone('America/Los_Angeles')
            self.assertEqual(following.strftime('%A %H:%M'), 'Tuesday 06:45')
            self.assertEqual((following.date() - next_run.date()).days, 7)

    def test_callable_passes_selected_site_to_its_recap_hook(self):
        dag = self.load()
        with patch('airflow.sdk.get_current_context', return_value={'run_id': 'weekly-fixture'}), \
                patch('sfz_recap_hook.RecapRefreshHook.refresh') as refresh, \
                patch('sfz_recap_hook.save_receipt') as save:
            for slug in self.sites:
                generator = dag.get_task(f'generate_recaps_{slug}')
                site = generator.op_args[0]
                receipt = {'runId': 'weekly-fixture', 'team': slug}
                refresh.return_value = receipt
                result = generator.python_callable(*generator.op_args)
                refresh.assert_called_with('weekly-fixture', site)
                self.assertEqual(result, {'site': site, 'receipt': receipt})
                dag.get_task(f'save_run_receipt_{slug}').python_callable(result)
                save.assert_called_with(receipt, 'weekly-fixture', site)


if __name__ == '__main__':
    unittest.main()
