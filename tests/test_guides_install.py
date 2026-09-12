"""Restricted key installation preserves unrelated access and refuses conflicts."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('guides_install_test', ROOT / 'deployment/guides/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


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
