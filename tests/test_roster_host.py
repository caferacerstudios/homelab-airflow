"""Roster publication, input isolation, and installation checks use temp files only."""
import copy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
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

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


runner = module('roster_host_test', ROOT / 'deployment/roster/refresh_roster.py')
installer = module('roster_install_test', ROOT / 'deployment/roster/install.py')
config = module('roster_config_test', ROOT / 'dags/fan_zone_config.py')
with patch.dict(sys.modules, {'fan_zone_config': config}):
    registry = module('roster_registry_test', ROOT / 'deployment/shared/fan_zone_host.py')


def sites():
    return json.loads((ROOT / 'config/active-sites.json').read_text())


def candidate(work, run_key, slug):
    request = json.loads((work / 'request.json').read_text())
    output = work / 'candidate'
    output.mkdir()
    now = request['now']
    stores = {
        'roster': {'schemaVersion': 1, 'team': slug, 'asOf': now, 'sourceCheckedAt': now,
                   'players': [{'id': 'test-player', 'name': 'Test Player', 'position': 'QB', 'status': 'Active'}]},
        'injuries': {'schemaVersion': 2, 'team': slug, 'asOf': None, 'sourceCheckedAt': now,
                     'availability': 'unavailable', 'records': []},
        'transactions': {'schemaVersion': 1, 'team': slug, 'asOf': now, 'sourceCheckedAt': now, 'records': []},
        'report': {'status': 'success', 'team': slug, 'runId': request['runId'], 'updatedAt': now, 'requestCount': 3},
    }
    for name, value in stores.items():
        (output / (name + '.json')).write_text(json.dumps(value))


class HostTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / 'broncos-roster'
        self.runtime.mkdir()
        self.site = sites()['broncos']
        patches = [patch.object(runner, 'site_settings', side_effect=lambda site: copy.deepcopy(site)),
                   patch.object(runner, 'preflight', return_value='a' * 40),
                   patch.object(runner, 'source_commit', return_value='a' * 40),
                   patch.object(runner, 'nfl_payloads', return_value=None),
                   patch.object(runner, 'run_node', side_effect=candidate)]
        self.mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)

    def collect(self, run='manual__one'):
        return runner.collect(run, self.site, self.runtime)

    def test_atomic_snapshot_manifest_and_same_run_reuse(self):
        result = self.collect()
        self.assertEqual(result['team'], 'broncos')
        self.assertEqual(set(result['files']), set(runner.FILES))
        current = runner.current_snapshot(self.runtime)
        self.assertEqual(current, Path(result['snapshotPath']))
        self.assertFalse(result['reused'])
        again = self.collect()
        self.assertTrue(again['reused'])
        self.mocks[-1].assert_called_once()
        self.assertEqual(runner.verify_snapshot(current, self.site)['requestCount'], 3)

    def test_replay_never_rolls_back_newer_current(self):
        old = self.collect('old')
        new = self.collect('new')
        self.assertNotEqual(old['snapshotPath'], new['snapshotPath'])
        self.collect('old')
        self.assertEqual(runner.current_snapshot(self.runtime), Path(new['snapshotPath']))
        self.assertEqual(self.mocks[-1].call_count, 2)

    def test_failed_collection_and_invalid_candidate_preserve_current(self):
        original = self.collect()
        self.mocks[-1].side_effect = RuntimeError('official page changed')
        with self.assertRaisesRegex(RuntimeError, 'official page'):
            self.collect('failing')
        self.assertEqual(runner.current_snapshot(self.runtime), Path(original['snapshotPath']))
        def wrong_team(work, key, slug):
            candidate(work, key, 'seahawks')
        self.mocks[-1].side_effect = wrong_team
        with self.assertRaisesRegex(ValueError, 'Wrong team'):
            self.collect('wrong-team')
        self.assertEqual(runner.current_snapshot(self.runtime), Path(original['snapshotPath']))

    def test_publication_interruption_can_resume_without_recollection(self):
        with patch.object(runner, 'publish_link', side_effect=OSError('atomic replace failed')):
            with self.assertRaisesRegex(OSError, 'atomic replace'):
                self.collect()
        self.assertIsNone(runner.current_snapshot(self.runtime))
        self.assertTrue(self.collect()['reused'])
        self.mocks[-1].assert_called_once()

    def test_corrupt_previous_stops_before_next_source_call(self):
        result = self.collect()
        (Path(result['snapshotPath']) / 'roster.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.collect('later')
        self.mocks[-1].assert_called_once()

    def test_invalid_current_destination_is_not_overwritten(self):
        external = self.root / 'external'
        external.mkdir()
        (self.runtime / 'current').symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.collect()
        self.mocks[-1].assert_not_called()
        self.assertEqual((self.runtime / 'current').resolve(), external)

    def test_disabled_site_and_invalid_run_do_not_collect(self):
        self.site['enabled'] = False
        with self.assertRaisesRegex(ValueError, 'disabled'):
            self.collect()
        for value in ('', 'x\n', 'é' * 257, None):
            with self.assertRaises(ValueError):
                self.collect(value)
        self.mocks[-1].assert_not_called()

    def test_only_seattle_seeds_manual_checked_in_history(self):
        source = self.root / 'website'
        data = source / 'src/data/team'
        data.mkdir(parents=True)
        for name in runner.FILES:
            (data / name).write_text(json.dumps({'legacy': name}))
        self.site['website_root'] = str(source)
        self.assertEqual(runner.previous_payloads(self.site, self.runtime), {'roster': None, 'injuries': None, 'transactions': None})
        self.site['slug'] = 'seahawks'
        self.assertEqual(runner.previous_payloads(self.site, self.runtime)['transactions'], {'legacy': 'transactions.json'})

    def test_ssh_umask_does_not_make_state_shared_or_snapshots_private(self):
        for mask in (0o002, 0o077):
            with self.subTest(mask=oct(mask)):
                previous = os.umask(mask)
                try:
                    result = self.collect('umask-' + str(mask))
                    actual = os.umask(mask)
                    self.assertEqual(actual, mask)
                    snapshot = Path(result['snapshotPath'])
                    self.assertEqual((self.runtime / 'collector.lock').stat().st_mode & 0o777, 0o600)
                    for directory in (self.runtime / 'runs', snapshot.parent, snapshot):
                        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
                    for filename in (*runner.FILES, 'manifest.json'):
                        self.assertEqual((snapshot / filename).stat().st_mode & 0o777, 0o644)
                    self.assertTrue(self.collect('umask-' + str(mask))['reused'])
                    self.assertEqual(os.umask(mask), mask)
                finally:
                    os.umask(previous)

    def test_previous_group_writable_state_is_repaired_without_replacing_lock(self):
        lock = self.runtime / 'collector.lock'
        lock.write_text('')
        lock.chmod(0o664)
        inode = lock.stat().st_ino
        runs = self.runtime / 'runs'
        run = runs / hashlib.sha256(b'manual__one').hexdigest()
        run.mkdir(parents=True)
        runs.chmod(0o775)
        run.chmod(0o775)
        unrelated = runs / 'unrelated-history'
        unrelated.mkdir()
        unrelated.chmod(0o775)
        result = self.collect()
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        self.assertEqual(runs.stat().st_mode & 0o777, 0o755)
        self.assertEqual(run.stat().st_mode & 0o777, 0o755)
        self.assertEqual(unrelated.stat().st_mode & 0o777, 0o775)
        self.assertEqual(runner.current_snapshot(self.runtime), Path(result['snapshotPath']))

    def test_live_lock_is_not_replaced_or_chmodded(self):
        lock = self.runtime / 'collector.lock'
        lock.write_text('')
        lock.chmod(0o664)
        inode = lock.stat().st_ino
        with lock.open('a') as held:
            runner.fcntl.flock(held, runner.fcntl.LOCK_EX | runner.fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'Another roster collection'):
                self.collect()
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o664)
        self.mocks[-1].assert_not_called()

    def test_unsafe_locks_are_not_changed(self):
        target = self.root / 'unrelated-file'
        target.write_text('untouched')
        target.chmod(0o664)
        lock = self.runtime / 'collector.lock'
        lock.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.collect()
        lock.unlink()
        os.link(target, lock)
        with self.assertRaisesRegex(ValueError, 'links or permissions'):
            self.collect()
        self.assertEqual(target.stat().st_mode & 0o777, 0o664)
        self.assertEqual(target.read_text(), 'untouched')
        self.mocks[-1].assert_not_called()

    def test_umask_restored_after_collection_failure(self):
        self.mocks[-1].side_effect = RuntimeError('fixture collector failure')
        previous = os.umask(0o002)
        try:
            with self.assertRaisesRegex(RuntimeError, 'fixture collector failure'):
                self.collect()
            self.assertEqual(os.umask(0o002), 0o002)
        finally:
            os.umask(previous)

    def test_duplicate_roster_ids_reject_candidate(self):
        work = self.root / 'check'
        work.mkdir()
        (work / 'request.json').write_text(json.dumps({'now': '2026-09-12T00:00:00Z', 'runId': 'test'}))
        candidate(work, 'unused', 'broncos')
        file = work / 'candidate/roster.json'
        value = json.loads(file.read_text())
        value['players'] *= 2
        file.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'duplicated'):
            runner.verify_payloads(work / 'candidate', self.site)


class CollectorProcessTests(unittest.TestCase):
    def test_failure_surfaces_bounded_readable_tail_and_retains_full_log(self):
        output = ('Earlier collector output\n' * 500
                  + '\x1b[31mRoster collection failed:\x1b[0m official roster is empty\x00\u202e\n')
        process = Mock(returncode=1)
        process.communicate.return_value = (output, None)
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            with patch.object(runner, 'command', return_value=''), \
                    patch.object(runner.subprocess, 'Popen', return_value=process):
                with self.assertRaises(RuntimeError) as error:
                    runner.run_node(work, 'a' * 64, 'seahawks')
            message = str(error.exception)
            self.assertIn('Roster collector exited 1:', message)
            self.assertIn('Roster collection failed: official roster is empty', message)
            self.assertIn(str(work / 'collector.log'), message)
            self.assertTrue(message.isprintable())
            self.assertNotIn('[31m', message)
            self.assertLessEqual(len(message), runner.MAX_COLLECTOR_ERROR_CHARS + len(str(work / 'collector.log')) + 50)
            self.assertEqual((work / 'collector.log').read_text(), output)
            process.communicate.assert_called_once_with(timeout=480)

    def test_failure_without_output_still_reports_exit_and_log_path(self):
        process = Mock(returncode=125)
        process.communicate.return_value = ('', None)
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            with patch.object(runner, 'command', return_value=''), \
                    patch.object(runner.subprocess, 'Popen', return_value=process):
                with self.assertRaisesRegex(RuntimeError, 'exited 125: no diagnostic output'):
                    runner.run_node(work, 'a' * 64, 'seahawks')
            self.assertEqual((work / 'collector.log').read_text(), '')


class RegistryAndInstallerTests(unittest.TestCase):
    def test_derived_destinations_keep_existing_spelling_and_no_new_config_fields(self):
        configured = sites()
        self.assertEqual(str(runner.runtime_for(configured['seahawks'])), '/var/lib/sfz-roster')
        self.assertEqual(str(runner.runtime_for(configured['broncos'])), '/var/lib/boncosfz-roster')
        self.assertEqual(len({runner.runtime_for(site) for site in configured.values()}), len(configured))
        configured['broncos']['news_snapshot_dir'] = '/var/lib/boncosfz-news/../../etc'
        with self.assertRaises(ValueError):
            runner.runtime_for(configured['broncos'])

    def test_installed_registry_pins_news_path_and_authorizes_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text(json.dumps({'sites': sites()}))
            selected = sites()['broncos']
            selected['news_snapshot_dir'] = '/var/lib/other-news/current'
            selected['news_photos_dir'] = '/var/lib/other-news/photos'
            fake_host = Mock()
            fake_host.authorize_site.side_effect = lambda site, pipeline, path: site
            with patch.dict(sys.modules, {'fan_zone_host': fake_host}):
                with self.assertRaisesRegex(ValueError, 'news output differs'):
                    runner.site_settings(selected, path)
                self.assertEqual(runner.site_settings(sites()['broncos'], path)['slug'], 'broncos')
                self.assertEqual(fake_host.authorize_site.call_args.args[1], 'nfl')

    def test_installer_reads_variable_without_writes_and_uses_dedicated_connection(self):
        reply = Mock(stdout='log\nROSTER_ACTIVE_SITES=' + json.dumps(sites()) + '\n')
        with patch.object(installer, 'run', return_value=reply) as call:
            self.assertEqual(set(installer.load_active_sites()), set(sites()))
            code = call.call_args.args[0][-1]
            self.assertIn("Variable.get('fan_zone_active_sites'", code)
            self.assertNotIn('Variable.set', code)
        with patch.object(installer, 'run') as call:
            installer.verify_airflow([sites()['broncos']])
            args = call.call_args.args[0]
            self.assertIn('PYTHONDONTWRITEBYTECODE=1', args)
            self.assertIn("ssh_conn_id='sfz_roster_host'", args[-1])
            self.assertIn("'check ' + token", args[-1])
            self.assertNotIn('pools', args)
            self.assertNotIn('include_examples', args[-1])


class OptionalNflInputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.site = sites()['broncos']
        self.site['nfl_snapshot_dir'] = str(self.root / 'current')
        self.snapshot = self.root / 'runs' / ('a' * 64) / 'snapshot'
        self.snapshot.mkdir(parents=True)
        (self.root / 'current').symlink_to(self.snapshot.relative_to(self.root))
        now = datetime.now(timezone.utc).isoformat()
        base = {'team': {'abbreviation': 'DEN', 'id': 15}, 'season': 2026, 'updatedAt': now, 'fixture': False}
        self.payloads = {'broncos.json': {**base, 'gamesRegular': []},
                         'players.json': {**base, 'playerDirectory': []}, 'standings.json': {}}
        self.manifest = {'schema_version': 1, 'team': 'broncos', 'runId': 'fixture-run', 'season': 2026, 'updatedAt': now}
        self.write()

    def write(self):
        for name, value in self.payloads.items():
            (self.snapshot / name).write_text(json.dumps(value))
        self.manifest['files'] = {name: runner.file_hash(self.snapshot / name) for name in self.payloads}
        (self.snapshot / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_missing_nfl_is_optional_and_valid_input_is_read_only(self):
        self.assertEqual(runner.nfl_payloads(self.site)['schedule']['team']['id'], 15)
        original = (self.snapshot / 'manifest.json').read_bytes()
        runner.nfl_payloads(self.site)
        self.assertEqual((self.snapshot / 'manifest.json').read_bytes(), original)
        (self.root / 'current').unlink()
        self.assertIsNone(runner.nfl_payloads(self.site))

    def test_checksum_identity_fixture_and_freshness_must_validate(self):
        for field, value in [('team', []), ('team', {'id': 15, 'abbreviation': 'SEA'}), ('fixture', True),
                             ('updatedAt', '2026-01-01T00:00:00Z'), ('updatedAt', 'invalid'), ('season', 2025)]:
            with self.subTest(field=field):
                original = copy.deepcopy(self.payloads)
                self.payloads['players.json'][field] = value
                self.write()
                with self.assertRaises(ValueError):
                    runner.nfl_payloads(self.site)
                self.payloads = original
                self.write()
        (self.snapshot / 'players.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            runner.nfl_payloads(self.site)

    def test_current_cannot_escape_and_future_manifest_is_rejected(self):
        self.manifest['updatedAt'] = '2200-01-01T00:00:00Z'
        self.write()
        with self.assertRaisesRegex(ValueError, 'future'):
            runner.nfl_payloads(self.site)
        (self.root / 'current').unlink()
        (self.root / 'current').symlink_to('/tmp')
        with self.assertRaisesRegex(ValueError, 'escapes'):
            runner.nfl_payloads(self.site)


@unittest.skipUnless(importlib.util.find_spec('airflow'), 'Run in the Airflow image or matching test environment')
class ActualAirflowInstallerTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'dags'))
        self.addCleanup(lambda: sys.path.remove(str(ROOT / 'dags')))

    def test_installer_variable_payload_matches_actual_331_api(self):
        from airflow.models.variable import Variable
        with patch.object(installer, 'run', return_value=SimpleNamespace(stdout='ROSTER_ACTIVE_SITES={}')) as call:
            installer.load_active_sites()
            code = call.call_args.args[0][-1]
        for value in (json.dumps(sites()), None):
            stdout = io.StringIO()
            with patch.object(Variable, 'get', return_value=value) as get, patch.object(Path, 'read_text', return_value=json.dumps(sites())), redirect_stdout(stdout):
                exec(code, {})
            get.assert_called_once_with('fan_zone_active_sites', default_var=None)
            result = json.loads(stdout.getvalue().split('ROSTER_ACTIVE_SITES=', 1)[1])
            self.assertEqual(set(result), set(sites()))
        with patch.object(Variable, 'get', return_value='{bad json'), patch.object(Path, 'read_text') as fallback:
            with self.assertRaises(ValueError):
                exec(code, {})
            fallback.assert_not_called()

    def test_installer_actual_dagbag_and_restricted_site_checks(self):
        from airflow.dag_processing.dagbag import BundleDagBag
        from airflow.sdk import Variable
        entry = module('roster_installer_protocol_test', ROOT / 'deployment/roster/ssh_entrypoint.py')
        enabled_sites = [site for site in sites().values() if site['enabled']]
        with patch.object(installer, 'run') as call:
            installer.verify_airflow(enabled_sites)
            code = call.call_args.args[0][-1]
        collected = []
        hook = Mock()
        hook.get_conn.return_value.__enter__ = Mock(return_value=Mock())
        hook.get_conn.return_value.__exit__ = Mock(return_value=False)
        def check(client, command, **kwargs):
            args = entry.command_arguments(command)
            self.assertEqual(args[0], '--check')
            selected = json.loads(args[1].removeprefix('--site-json='))
            collected.append(selected['slug'])
            return 0, b'{"status":"ready"}', b''
        hook.exec_ssh_client_command.side_effect = check
        def local_bag(*, dag_folder, bundle_path):
            self.assertEqual(dag_folder, '/opt/airflow/dags/sfz_roster_refresh.py')
            self.assertEqual(bundle_path, Path('/opt/airflow/dags'))
            return BundleDagBag(dag_folder=str(ROOT / 'dags/sfz_roster_refresh.py'), bundle_path=ROOT / 'dags')
        with patch('airflow.dag_processing.dagbag.BundleDagBag', side_effect=local_bag), \
                patch('airflow.providers.ssh.hooks.ssh.SSHHook', return_value=hook) as hook_type, \
                patch.object(Variable, 'get', return_value=sites()), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(enabled_sites))), redirect_stdout(io.StringIO()):
            exec(code, {})
        hook_type.assert_called_once_with(ssh_conn_id='sfz_roster_host', cmd_timeout=120)
        self.assertEqual(set(collected), {site['slug'] for site in enabled_sites})

    def test_connection_failure_suppresses_private_key(self):
        sensitive = 'PRIVATE-KEY-TEST-CONTENT-DO-NOT-PRINT'
        with patch.object(Path, 'read_text', return_value='ssh-ed25519 HOST-PUBLIC-KEY'), patch.object(installer, 'run') as call:
            installer.configure_connection(sensitive)
            code = call.call_args.args[0][-1]
            payload = call.call_args.kwargs['input']
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('airflow.utils.session.create_session', side_effect=RuntimeError(sensitive)), \
                patch.object(sys, 'stdin', io.StringIO(payload)), redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                exec(code, {})
        self.assertEqual(stopped.exception.code, 1)
        self.assertNotIn(sensitive, stdout.getvalue() + stderr.getvalue())
        self.assertIn('RuntimeError', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
