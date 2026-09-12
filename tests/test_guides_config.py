"""Research policy keeps strict budgets without a second team activation list."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('guides_config_test', ROOT / 'deployment/guides/guides_core.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class GuidePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((ROOT / 'config/game-guides.json').read_text())
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'policy.json'

    def load(self, value):
        self.path.write_text(json.dumps(value))
        return core.load_config(self.path)

    def test_reviewed_policy_has_only_model_and_research_budgets(self):
        self.assertEqual(set(self.policy), {'model', 'horizon_days', 'max_games_per_run',
                         'refresh_days', 'max_nfl_age_hours', 'max_search_calls_per_game'})
        self.assertEqual(self.load(self.policy), self.policy)
        self.assertEqual(self.policy['model'], 'gpt-5.4-mini')
        self.assertEqual(self.policy['max_games_per_run'], 3)

    def test_extra_or_missing_fields_and_nonobject_policy_are_rejected(self):
        missing = dict(self.policy)
        missing.pop('model')
        for value in ([], {}, missing, {**self.policy, 'enabled_sites': ['seahawks']},
                      {**self.policy, 'unexpected': True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(value)

    def test_each_budget_requires_a_bounded_integer(self):
        limits = {'horizon_days': (1, 30), 'max_games_per_run': (1, 6),
                  'refresh_days': (1, 7), 'max_nfl_age_hours': (1, 168),
                  'max_search_calls_per_game': (1, 8)}
        for field, (low, high) in limits.items():
            for value in (low, high):
                with self.subTest(field=field, accepted=value):
                    self.assertEqual(self.load({**self.policy, field: value})[field], value)
            for value in (low - 1, high + 1, True, None, str(low), float(low)):
                with self.subTest(field=field, rejected=value), self.assertRaisesRegex(ValueError, field):
                    self.load({**self.policy, field: value})

    def test_model_identifier_remains_strict(self):
        for model in ('', None, True, 'gpt-5.4-mini\n', 'gpt-5.4-mini;echo', 'other-model'):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, 'model'):
                self.load({**self.policy, 'model': model})

    def test_preflight_rejects_disabled_team_before_credentials_or_schedule(self):
        with patch.object(core, 'source_commit', return_value='a' * 40), \
                patch.object(core, 'credential') as credential, patch.object(core, 'read_schedule') as schedule:
            with self.assertRaisesRegex(ValueError, 'enabled'):
                core.preflight({'slug': 'chiefs', 'enabled': False}, Path('/var/lib/chiefsfz-guides'),
                               require_runtime=False)
            credential.assert_not_called()
            schedule.assert_not_called()


if __name__ == '__main__':
    unittest.main()
