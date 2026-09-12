"""The new SSH key accepts only explicit team data, never a shell command."""
import base64
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('guides_ssh_test', ROOT / 'deployment/guides/ssh_entrypoint.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def encode(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


class RestrictedCommands(unittest.TestCase):
    def test_explicit_site_check_and_refresh(self):
        site = {'slug': 'broncos'}
        self.assertEqual(entry.command_arguments('check ' + encode({'site': site})),
                         ['--check', '--site-json={"slug":"broncos"}'])
        for run_id in ('scheduled__2026-09-12T12:30:00+00:00', '--help; $(id) `whoami`', 'manual__é'):
            for token in (encode({'site': site, 'runId': run_id}), encode({'site': site, 'runId': run_id}).rstrip('=')):
                self.assertEqual(entry.command_arguments('refresh ' + token),
                                 ['--run-id=' + run_id, '--site-json={"slug":"broncos"}'])

    def test_rejects_implicit_team_arbitrary_commands_and_runtime_override(self):
        for value in ('check', '', 'sh', 'refresh', 'refresh abc def', 'check; id', 'refresh _w==', 'refresh YR=='):
            with self.subTest(value=value), self.assertRaises(ValueError):
                entry.command_arguments(value)
        for data in ('manual__legacy', [], {'site': {}, 'runId': 'a', 'runtime': '/etc'},
                     {'site': [], 'runId': 'a'}, {'site': {}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                entry.command_arguments('refresh ' + encode(data))

    def test_request_and_utf8_run_id_bounds(self):
        for value in ('', ' ', 'a\n', 'a\x85b', 'a\x7fb', 'é' * 257, ['a']):
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError):
                entry.command_arguments('refresh ' + encode({'site': {}, 'runId': value}))
        with self.assertRaises(ValueError):
            entry.command_arguments('check ' + encode({'site': {'prompt': 'x' * 25000}}))
        self.assertEqual(entry.command_arguments('refresh ' + encode({'site': {}, 'runId': 'x' * 512}))[0], '--run-id=' + 'x' * 512)


if __name__ == '__main__':
    unittest.main()
