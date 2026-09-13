"""Shared team registry and host setup contracts, using temporary files only."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config = load_module('team_host_test_config', REPO / 'dags/fan_zone_config.py')
with patch.dict(sys.modules, {'fan_zone_config': config}):
    host = load_module('team_host_test_registry', REPO / 'deployment/shared/fan_zone_host.py')
installer = load_module('team_host_test_installer', REPO / 'deployment/shared/install.py')


def configured_sites():
    return json.loads((REPO / 'config/active-sites.json').read_text())


class ConfigurationTests(unittest.TestCase):
    def test_existing_enabled_teams_and_disabled_patriots_have_distinct_destinations(self):
        sites = config.validate_sites(configured_sites())
        self.assertEqual(set(sites), {'seahawks', 'broncos', 'packers', 'vikings', 'chiefs', 'patriots'})
        self.assertTrue(all(site['enabled'] for slug, site in sites.items() if slug != 'patriots'))
        self.assertFalse(sites['patriots']['enabled'])
        for field in ('news_snapshot_dir', 'nfl_snapshot_dir', 'recap_snapshot_dir'):
            self.assertEqual(len({site[field] for site in sites.values()}), len(sites))
        self.assertEqual(sites['seahawks']['news_snapshot_dir'], '/var/lib/sfz-news/current')
        self.assertEqual(sites['seahawks']['nfl_snapshot_dir'], '/var/lib/sfz-nfl/current')
        self.assertEqual(sites['seahawks']['recap_snapshot_dir'], '/var/lib/sfz-recaps/current')
        self.assertEqual(sites['broncos']['news_snapshot_dir'], '/var/lib/boncosfz-news/current')

    def test_seattle_outputs_cannot_be_reassigned_or_renamed(self):
        for field, suffix in [('nfl_snapshot_dir', 'nfl'), ('recap_snapshot_dir', 'recaps')]:
            with self.subTest(field=field):
                site = configured_sites()['packers']
                site[field] = f'/var/lib/sfz-{suffix}/current'
                with self.assertRaisesRegex(ValueError, 'Seattle'):
                    config.validate_site('packers', site)
                site = configured_sites()['seahawks']
                site[field] = f'/var/lib/newseattle-{suffix}/current'
                with self.assertRaisesRegex(ValueError, 'Seattle'):
                    config.validate_site('seahawks', site)

    def test_duplicate_non_seattle_outputs_and_traversal_are_rejected(self):
        for field in ('nfl_snapshot_dir', 'recap_snapshot_dir'):
            sites = configured_sites()
            sites['vikings'][field] = sites['packers'][field]
            with self.assertRaisesRegex(ValueError, 'share'):
                config.validate_sites(sites)
            sites = configured_sites()
            sites['packers'][field] = '/var/lib/packersfz-nfl/../../etc/current'
            with self.assertRaisesRegex(ValueError, 'invalid'):
                config.validate_sites(sites)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.registry = self.root / 'settings.json'
        self.sites = configured_sites()
        self.sites['packers']['balldontlie_team_id'] = 100  # Deliberate fixture ID.
        self.registry.write_text(json.dumps({'version': 1, 'sites': self.sites}))
        self.registry.chmod(0o644)
        self.owner = 0
        actual_stat = Path.stat

        def fake_registry_owner(path, *args, **kwargs):
            result = actual_stat(path, *args, **kwargs)
            if path == self.registry:
                fields = list(result)
                fields[4] = self.owner
                return os.stat_result(fields)
            return result

        stat_patch = patch.object(Path, 'stat', fake_registry_owner)
        stat_patch.start()
        self.addCleanup(stat_patch.stop)

    def authorize(self, site, pipeline='nfl'):
        return host.authorize_site(site, pipeline, self.registry)

    def test_variable_prompts_and_enabled_flag_remain_task_configurable(self):
        site = copy.deepcopy(self.sites['packers'])
        site['prompts']['recap'] = 'Use verified special-teams evidence.\nNever invent plays.'
        site['prompts']['article'] = 'Cover Green Bay roster development.'
        site['enabled'] = False
        site['balldontlie_team_id'] = None
        for pipeline in ('nfl', 'recaps'):
            selected = self.authorize(site, pipeline)
            self.assertEqual(selected['prompts'], site['prompts'])
            self.assertFalse(selected['enabled'])
            self.assertEqual(selected['balldontlie_team_id'], 100)
        self.assertEqual(json.loads(self.registry.read_text())['sites'], self.sites)

    def test_valid_but_wrong_team_identity_paths_and_ids_are_rejected(self):
        alternatives = {
            'name': 'Vikings', 'city': 'Minnesota', 'abbreviation': 'MIN',
            'nfl_snapshot_dir': self.sites['vikings']['nfl_snapshot_dir'],
            'recap_snapshot_dir': self.sites['vikings']['recap_snapshot_dir'],
            'balldontlie_team_id': 101,
        }
        for field, value in alternatives.items():
            with self.subTest(field=field):
                site = copy.deepcopy(self.sites['packers'])
                site[field] = value
                with self.assertRaisesRegex(ValueError, 'differs'):
                    self.authorize(site)

    def test_unregistered_team_and_unknown_pipeline_are_rejected(self):
        sites = {key: value for key, value in self.sites.items() if key != 'packers'}
        self.registry.write_text(json.dumps({'sites': sites}))
        with self.assertRaisesRegex(ValueError, 'Install this team'):
            self.authorize(self.sites['packers'])
        with self.assertRaisesRegex(ValueError, 'Unknown host pipeline'):
            self.authorize(self.sites['seahawks'], 'news')

    def test_registry_owner_writable_mode_and_symlink_are_rejected(self):
        self.owner = 1000
        with self.assertRaisesRegex(ValueError, 'root-owned'):
            self.authorize(self.sites['seahawks'])
        self.owner = 0
        self.registry.chmod(0o664)
        with self.assertRaisesRegex(ValueError, 'root-owned'):
            self.authorize(self.sites['seahawks'])
        self.registry.chmod(0o644)
        link = self.root / 'settings-link.json'
        link.symlink_to(self.registry.name)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            host.authorize_site(self.sites['seahawks'], 'nfl', link)


class InstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.varlib = self.root / 'var/lib'
        self.varlib.mkdir(parents=True)
        self.target = self.root / 'opt/fanzone-shared'
        self.target.parent.mkdir()
        self.stage = self.root / 'staged-repo'
        shared = self.stage / 'deployment/shared'
        shared.mkdir(parents=True)
        (self.stage / 'dags').mkdir()
        for filename in ('fan_zone_host.py',):
            (shared / filename).write_bytes((REPO / 'deployment/shared' / filename).read_bytes())
        (self.stage / 'dags/fan_zone_config.py').write_bytes((REPO / 'dags/fan_zone_config.py').read_bytes())
        self.site_file = self.stage / 'active-sites.json'
        self.site_file.write_text(json.dumps(configured_sites()))
        self.uid, self.gid = os.getuid(), os.getgid()
        actual_stat = Path.stat

        def fixture_path(value):
            value = str(value)
            return self.varlib / value.removeprefix('/var/lib/') if value.startswith('/var/lib/') else Path(value)

        def registry_root_owner(path, *args, **kwargs):
            result = actual_stat(path, *args, **kwargs)
            if path == self.target or self.target in path.parents:
                fields = list(result)
                fields[4] = 0
                return os.stat_result(fields)
            return result

        # Model privileged ownership only inside the disposable fixture registry.
        # All writes, replacements, modes and current symlinks remain real files.
        patches = [
            patch.object(installer, 'ROOT', self.target),
            patch.object(installer, 'HERE', shared),
            patch.object(installer, 'Path', fixture_path),
            patch.object(Path, 'stat', registry_root_owner),
            patch.object(installer.os, 'chown'), patch.object(installer.os, 'fchown'),
            patch.object(installer.os, 'geteuid', return_value=0),
            patch.object(installer.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=self.uid, pw_gid=self.gid)),
            patch.object(installer, 'print', create=True),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        docker = patch.object(installer.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', ''))
        self.docker = docker.start()
        self.addCleanup(docker.stop)

    def test_staged_layout_resolves_its_own_config_and_loads_all_teams(self):
        sites, source = installer.load_sites(self.site_file)
        self.assertEqual(source, self.stage / 'dags/fan_zone_config.py')
        self.assertEqual(set(sites), set(configured_sites()))

    def test_check_mode_does_not_create_registry_or_pipeline_directories(self):
        with patch.object(sys, 'argv', ['install.py', '--check', '--sites', str(self.site_file)]):
            installer.main()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.varlib.iterdir()), [])
        self.assertEqual(self.docker.call_count, 2)
        for call in self.docker.call_args_list:
            self.assertEqual(call.args[0][:3], ['docker', 'ps', '-q'])

    def test_repeated_install_retains_current_snapshots_and_custom_news_settings(self):
        sites, source = installer.load_sites(self.site_file)
        snapshots = {}
        for slug, site in sites.items():
            for field in ('news_snapshot_dir', 'nfl_snapshot_dir', 'recap_snapshot_dir'):
                current = installer.Path(site[field])
                previous = current.parent / 'releases/previous'
                previous.mkdir(parents=True)
                artifact = previous / 'keep.json'
                artifact.write_text(json.dumps({'team': slug, 'kind': field, 'keep': True}))
                current.symlink_to('releases/previous')
                snapshots[current] = (current.readlink(), artifact.read_bytes(), artifact.stat().st_mtime_ns)
        news = installer.Path(sites['seahawks']['news_snapshot_dir']).parent
        (news / 'config.json').write_text('{"model":"existing-custom-model","custom":"keep"}\n')
        denver = installer.Path(sites['broncos']['news_snapshot_dir']).parent
        (denver / 'config.json').write_text('{"model":"denver-custom-model"}\n')
        for _ in range(2):
            installer.preflight(sites, self.uid)
            installer.install(sites, source, self.uid, self.gid)
        for current, expected in snapshots.items():
            artifact = current / 'keep.json'
            self.assertTrue(current.is_symlink())
            self.assertEqual((current.readlink(), artifact.read_bytes(), artifact.stat().st_mtime_ns), expected)
        self.assertEqual(json.loads((denver / 'config.json').read_text()), {'model': 'denver-custom-model'})
        for slug in ('packers', 'vikings', 'chiefs', 'patriots'):
            runtime = installer.Path(sites[slug]['news_snapshot_dir']).parent
            self.assertEqual(json.loads((runtime / 'config.json').read_text())['model'], 'existing-custom-model')
            self.assertTrue((runtime / 'photos').is_dir())
        self.assertEqual(json.loads((self.target / 'settings.json').read_text())['sites'], sites)
        self.assertTrue(any((backup / 'settings.json').is_file() for backup in (self.target / 'backups').iterdir()))
        self.assertEqual((self.target / 'settings.json').stat().st_mode & 0o777, 0o644)

    def test_active_docker_job_stops_preflight_without_creating_paths(self):
        sites, _ = installer.load_sites(self.site_file)
        self.docker.return_value = subprocess.CompletedProcess([], 0, 'running-fixture-container\n', '')
        with self.assertRaisesRegex(ValueError, 'running container'):
            installer.preflight(sites, self.uid)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.varlib.iterdir()), [])

    def test_existing_writable_news_root_and_symlink_photo_bucket_are_rejected(self):
        sites, _ = installer.load_sites(self.site_file)
        news = installer.Path(sites['seahawks']['news_snapshot_dir']).parent
        news.mkdir(mode=0o775)
        news.chmod(0o775)
        with self.assertRaisesRegex(ValueError, 'permissions'):
            installer.preflight(sites, self.uid)
        news.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'ownership'):
            installer.preflight(sites, self.uid + 1)
        (news / 'photos').symlink_to(self.varlib)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            installer.preflight(sites, self.uid)
        self.assertTrue((news / 'photos').is_symlink())
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
