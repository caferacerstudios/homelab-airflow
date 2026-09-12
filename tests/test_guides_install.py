"""Restricted key installation preserves unrelated access and refuses conflicts."""
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('guides_install_test', ROOT / 'deployment/guides/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
sys.path.insert(0, str(ROOT / 'deployment/guides'))
import guides_core as runner


class ActiveTeamStateInstallationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.sites = json.loads((ROOT / 'config/active-sites.json').read_text())
        self.roots = {slug: self.base / (slug + '-guides') for slug in self.sites}
        self.roots['seahawks'].mkdir(mode=0o755)
        self.existing = self.roots['seahawks'] / 'existing-publication.json'
        self.existing.write_text('{"retained":true}\n')
        self.existing_stat = self.existing.stat()
        self.events = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.authorize = self.stack.enter_context(patch.object(runner, 'site_settings', side_effect=lambda site: site))
        self.stack.enter_context(patch.object(runner, 'runtime_for', side_effect=lambda site: self.roots[site['slug']]))
        self.preflight = self.stack.enter_context(patch.object(runner, 'preflight', side_effect=self.check_site))
        self.run = self.stack.enter_context(patch.object(installer, 'run', side_effect=self.create_root))

    def check_site(self, site, runtime, *, require_runtime):
        self.assertFalse(require_runtime)
        self.assertEqual(runtime, self.roots[site['slug']])
        self.events.append(('preflight', site['slug']))

    def create_root(self, arguments):
        self.assertEqual(arguments[:3], ['sudo', 'install', '-d'])
        destination = Path(arguments[-1])
        slug = next(slug for slug, root in self.roots.items() if root == destination)
        self.events.append(('mkdir', slug))
        destination.mkdir(mode=0o755)

    def assert_existing_publication_retained(self):
        self.assertEqual(self.existing.read_text(), '{"retained":true}\n')
        self.assertEqual(self.existing.stat().st_ino, self.existing_stat.st_ino)
        self.assertEqual(self.existing.stat().st_mtime_ns, self.existing_stat.st_mtime_ns)
        self.assertEqual(self.existing.stat().st_mode, self.existing_stat.st_mode)

    def test_all_active_team_roots_are_prepared_after_every_preflight(self):
        selected = installer.ensure_state_directories(self.sites)
        self.assertEqual([site['slug'] for site in selected], sorted(self.sites))
        self.assertEqual(self.events, [('preflight', slug) for slug in sorted(self.sites)] +
                         [('mkdir', slug) for slug in sorted(self.sites) if slug != 'seahawks'])
        self.assertTrue(all(root.is_dir() for root in self.roots.values()))
        self.assertEqual({call.args[0]['slug'] for call in self.authorize.call_args_list}, set(self.sites))
        self.assert_existing_publication_retained()

    def test_disabled_team_is_not_authorized_checked_or_created(self):
        self.sites['chiefs']['enabled'] = False
        selected = installer.ensure_state_directories(self.sites)
        self.assertEqual({site['slug'] for site in selected}, set(self.sites) - {'chiefs'})
        self.assertNotIn('chiefs', {call.args[0]['slug'] for call in self.authorize.call_args_list})
        self.assertNotIn('chiefs', {slug for _, slug in self.events})
        self.assertFalse(self.roots['chiefs'].exists())
        self.assert_existing_publication_retained()

    def test_last_team_preflight_failure_creates_no_roots(self):
        def check(site, runtime, *, require_runtime):
            self.check_site(site, runtime, require_runtime=require_runtime)
            if site['slug'] == 'vikings':
                raise ValueError('Vikings NFL snapshot is stale')

        self.preflight.side_effect = check
        with self.assertRaisesRegex(ValueError, 'snapshot is stale'):
            installer.ensure_state_directories(self.sites)
        self.assertEqual(self.events, [('preflight', slug) for slug in sorted(self.sites)])
        self.run.assert_not_called()
        self.assertEqual({slug for slug, root in self.roots.items() if root.exists()}, {'seahawks'})
        self.assert_existing_publication_retained()

    def test_no_active_teams_does_not_prepare_any_state(self):
        for site in self.sites.values():
            site['enabled'] = False
        with self.assertRaisesRegex(RuntimeError, 'No .*enabled'):
            installer.ensure_state_directories(self.sites)
        self.authorize.assert_not_called()
        self.preflight.assert_not_called()
        self.run.assert_not_called()
        self.assert_existing_publication_retained()


@unittest.skipUnless(shutil.which('ssh-keygen'), 'Requires the host SSH key tool')
class DedicatedKeyInstallationTests(unittest.TestCase):
    def test_reuses_one_restricted_key_and_preserves_unrelated_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / 'project'
            project.mkdir()
            home = base / 'account'
            ssh = home / '.ssh'
            ssh.mkdir(parents=True)
            authorized = ssh / 'authorized_keys'
            unrelated = '# retained access\nssh-ed25519 unrelated-existing-key laptop\n'
            authorized.write_text(unrelated)
            with patch.object(installer, 'PROJECT', project), patch.object(Path, 'home', return_value=home):
                first = installer.install_key()
                contents = authorized.read_text()
                second = installer.install_key()
            self.assertEqual(first, second)
            self.assertEqual(contents, authorized.read_text())
            self.assertTrue(contents.startswith(unrelated))
            additions = contents[len(unrelated):].splitlines()
            self.assertEqual(len(additions), 1)
            self.assertTrue(additions[0].startswith('restrict,command="/usr/bin/python3 '))
            self.assertIn('/deployment/guides/ssh_entrypoint.py" ssh-ed25519 ', additions[0])
            self.assertEqual((project / 'secrets/sfz_guides_ed25519').stat().st_mode & 0o777, 0o600)

    def test_conflicting_options_for_same_key_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / 'project'
            project.mkdir()
            home = base / 'account'
            home.mkdir()
            with patch.object(installer, 'PROJECT', project), patch.object(Path, 'home', return_value=home):
                installer.install_key()
                authorized = home / '.ssh/authorized_keys'
                original = authorized.read_text()
                conflict = original.replace('restrict,command=', 'no-pty,command=')
                authorized.write_text(conflict)
                with self.assertRaisesRegex(RuntimeError, 'different authorized_keys options'):
                    installer.install_key()
                self.assertEqual(authorized.read_text(), conflict)


if __name__ == '__main__':
    unittest.main()
