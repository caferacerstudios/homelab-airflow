import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_patriots', HERE / 'prepare_patriots.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
SITES = json.loads((HERE.parents[1] / 'config/active-sites.json').read_text())
PROPOSED = SITES['patriots']


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.current = {slug: copy.deepcopy(site) for slug, site in SITES.items() if slug != 'patriots'}
        self.current['seahawks']['prompts']['article'] = 'Keep this locally customized prompt.\nSecond line.'
        self.current['vikings']['enabled'] = False

    def test_preserves_all_original_sites_and_input(self):
        original = copy.deepcopy(self.current)
        disabled, enabled = helper.candidates(self.current, PROPOSED)
        self.assertEqual(self.current, original)
        for slug, site in original.items():
            self.assertEqual(disabled[slug], site)
            self.assertEqual(enabled[slug], site)
        self.assertFalse(disabled['patriots']['enabled'])
        self.assertTrue(enabled['patriots']['enabled'])
        enabled['patriots']['enabled'] = False
        self.assertEqual(enabled, disabled)

    def test_variable_export_wrapper(self):
        wrapped = {'fan_zone_active_sites': json.dumps(self.current)}
        self.assertEqual(helper.unpack(wrapped), self.current)

    def test_existing_enabled_or_custom_patriots_is_not_replaced(self):
        for key, value in [('enabled', True), ('city', 'Unexpected city')]:
            current = copy.deepcopy(self.current)
            current['patriots'] = dict(PROPOSED, **{key: value})
            with self.assertRaisesRegex(ValueError, 'existing Patriots entry differs'):
                helper.candidates(current, PROPOSED)

    def test_rejects_new_path_collision(self):
        proposed = dict(PROPOSED, news_snapshot_dir=self.current['broncos']['news_snapshot_dir'],
                        news_photos_dir=self.current['broncos']['news_photos_dir'])
        with self.assertRaisesRegex(ValueError, 'share news output'):
            helper.candidates(self.current, proposed)

    def test_staged_candidates_are_checked_for_other_team_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / 'current.json'
            current.write_text(json.dumps(self.current))
            plan = root / 'plan'
            helper.stage(current, plan)
            helper.read_plan(plan)
            candidate = json.loads((plan / 'enabled-sites.json').read_text())
            candidate['seahawks']['enabled'] = False
            (plan / 'enabled-sites.json').write_text(json.dumps(candidate))
            with self.assertRaisesRegex(ValueError, 'additive merge'):
                helper.read_plan(plan)
            with self.assertRaises(FileExistsError):
                helper.stage(current, plan)


class HostWriteTests(unittest.TestCase):
    def test_preparation_preserves_registry_values_and_existing_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = root / 'settings.json'
            original = {'version': 1, 'sites': {'seahawks': {'custom': 'preserved'}, 'other': {'enabled': False}}}
            before = json.dumps(original).encode()
            registry.write_bytes(before)
            coverage = root / 'coverage-source.json'
            coverage.write_text('[{"week": 1}]')
            installed = root / 'patriots.json'
            news = root / 'news'
            news.mkdir()
            existing = news / 'keep.json'
            existing.write_text('unchanged snapshot sentinel')
            model = news / 'config.json'
            arguments = (SimpleNamespace(pw_uid=0, pw_gid=0), {'slug': 'patriots', 'enabled': False},
                         registry, before, copy.deepcopy(original), [news], coverage, installed,
                         model, b'{"model":"existing-model"}\n')
            with patch.object(helper, 'host_plan', return_value=arguments), \
                 patch.object(helper, 'new_directory', side_effect=lambda path, *a: path.mkdir(exist_ok=True)), \
                 patch.object(helper, 'atomic', side_effect=lambda path, content, *a, **kw: path.write_bytes(content)):
                helper.prepare_host(root, apply=False)
                self.assertEqual(registry.read_bytes(), before)
                self.assertFalse(installed.exists())
                helper.prepare_host(root, apply=True)
            updated = json.loads(registry.read_text())
            self.assertEqual(updated['sites']['seahawks'], original['sites']['seahawks'])
            self.assertEqual(updated['sites']['other'], original['sites']['other'])
            self.assertFalse(updated['sites']['patriots']['enabled'])
            self.assertEqual(existing.read_text(), 'unchanged snapshot sentinel')
            self.assertEqual(installed.read_bytes(), coverage.read_bytes())
            self.assertEqual(next((root / 'backups').glob('*/settings.json')).read_bytes(), before)

    def test_registry_concurrent_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = root / 'settings.json'
            before = b'{"version":1,"sites":{}}'
            registry.write_text('{"concurrent":"edit"}')
            coverage = root / 'source.json'
            coverage.write_text('[]')
            model = root / 'config.json'
            model.write_text('{}')
            arguments = (SimpleNamespace(pw_uid=0, pw_gid=0), {'slug': 'patriots'}, registry, before,
                         {'version': 1, 'sites': {}}, [], coverage, root / 'patriots.json', model, b'{}')
            with patch.object(helper, 'host_plan', return_value=arguments), \
                 patch.object(helper, 'new_directory', side_effect=lambda path, *a: path.mkdir(exist_ok=True)), \
                 patch.object(helper, 'atomic', side_effect=lambda path, content, *a, **kw: path.write_bytes(content)):
                with self.assertRaisesRegex(ValueError, 'changed during preparation'):
                    helper.prepare_host(root, apply=True)
            self.assertEqual(json.loads(registry.read_text()), {'concurrent': 'edit'})


if __name__ == '__main__':
    unittest.main()
