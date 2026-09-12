"""Bounded fixture directory lookup; no real credentials, requests, or host writes."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('team_ids_installer', REPO / 'deployment/shared/install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def fixture_sites():
    return json.loads((REPO / 'config/active-sites.json').read_text())


class TeamIdentityLookupTests(unittest.TestCase):
    def setUp(self):
        self.sites = fixture_sites()
        # Deliberate fixture IDs: these are never written to deployable config.
        self.directory = [dict(id=site.get('balldontlie_team_id') or index + 1000,
                               abbreviation=site['abbreviation'], full_name=site['city'] + ' ' + site['name'])
                          for index, site in enumerate(self.sites.values())]
        self.read_key = Mock(return_value='fixture-key-do-not-print')
        self.request = Mock(return_value={'data': self.directory, 'meta': {'next_cursor': None}})
        original_spec = importlib.util.spec_from_file_location

        def instrument(name, filename, *args, **kwargs):
            spec = original_spec(name, filename, *args, **kwargs)
            if name == 'team_id_lookup':
                original_exec = spec.loader.exec_module
                def execute(module):
                    original_exec(module)
                    # Exercise the actual module exports/path; replace only IO.
                    module.read_api_key = self.read_key
                    module.request_json = self.request
                spec.loader.exec_module = execute
            return spec
        loader = patch.object(installer.importlib.util, 'spec_from_file_location', side_effect=instrument)
        loader.start()
        self.addCleanup(loader.stop)

    def resolve(self):
        return installer.resolve_ids(copy.deepcopy(self.sites))

    def test_one_bounded_directory_request_resolves_null_ids_only(self):
        before = copy.deepcopy(self.sites)
        result = self.resolve()
        self.read_key.assert_called_once_with(Path('/home/laurawkr/seahawksfanzone/.env'))
        self.request.assert_called_once_with('https://api.balldontlie.io/nfl/v1/teams?per_page=100', 'fixture-key-do-not-print')
        for site, original, team in zip(result.values(), before.values(), self.directory):
            self.assertEqual(site['balldontlie_team_id'], team['id'])
            self.assertEqual({k: v for k, v in site.items() if k != 'balldontlie_team_id'},
                             {k: v for k, v in original.items() if k != 'balldontlie_team_id'})
        self.assertEqual(result['seahawks']['balldontlie_team_id'], 31)

    def test_incomplete_directory_missing_team_or_next_page_is_rejected(self):
        cases = [{}, {'data': None}, {'data': self.directory[:-1]},
                 {'data': self.directory, 'meta': {'next_cursor': 100}}]
        for response in cases:
            with self.subTest(response_keys=list(response)), self.assertRaises(ValueError):
                self.request.return_value = response
                self.resolve()

    def test_duplicate_abbreviation_invalid_id_and_wrong_name_are_rejected(self):
        for mutate in (
            lambda rows: rows.append(copy.deepcopy(rows[0])),
            lambda rows: rows[0].update(id=True),
            lambda rows: rows[0].update(id='31'),
            lambda rows: rows[0].update(id=0),
            lambda rows: rows[0].update(full_name='Denver Broncos'),
        ):
            rows = copy.deepcopy(self.directory)
            mutate(rows)
            self.request.return_value = {'data': rows}
            with self.subTest(rows=rows[:1]), self.assertRaises(ValueError):
                self.resolve()

    def test_missing_empty_and_nonstring_api_full_names_are_rejected(self):
        for invalid in (None, '', '   ', 123, True, [], {}):
            rows = copy.deepcopy(self.directory)
            rows[1]['full_name'] = invalid
            self.request.return_value = {'data': rows}
            with self.subTest(full_name=invalid), self.assertRaisesRegex(ValueError, 'full name'):
                self.resolve()
        rows = copy.deepcopy(self.directory)
        rows[1].pop('full_name')
        self.request.return_value = {'data': rows}
        with self.assertRaisesRegex(ValueError, 'full name'):
            self.resolve()

    def test_wrong_preconfigured_identifier_is_rejected(self):
        self.sites['broncos']['balldontlie_team_id'] = 900000
        with self.assertRaisesRegex(ValueError, 'Configured balldontlie ID'):
            self.resolve()

    def test_resolve_cli_outputs_verified_sites_and_does_not_install_or_write_input(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'active-sites.json'
            original = json.dumps(self.sites).encode()
            filename.write_bytes(original)
            output = io.StringIO()
            with patch.object(sys, 'argv', ['install.py', '--resolve-ids', '--sites', str(filename)]), \
                    patch.object(installer.os, 'geteuid', return_value=0), \
                    patch.object(installer.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1000, pw_gid=1000)), \
                    patch.object(installer, 'preflight') as preflight, patch.object(installer, 'install') as install, \
                    redirect_stdout(output):
                installer.main()
            preflight.assert_not_called()
            install.assert_not_called()
            self.assertEqual(filename.read_bytes(), original)
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertTrue(lines[0].startswith('TEAM_SITES='))
            resolved = json.loads(lines[0].removeprefix('TEAM_SITES='))
            self.assertEqual(set(resolved), set(self.sites))
            self.assertTrue(all(type(site['balldontlie_team_id']) is int for site in resolved.values()))
            self.assertNotIn('fixture-key-do-not-print', output.getvalue())
            self.request.assert_called_once()


if __name__ == '__main__':
    unittest.main()
