"""Offline regression tests; the fake engine never contacts Docker or wkr."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

SPEC = importlib.util.spec_from_file_location('preview', Path(__file__).with_name('add-patriots-preview.py'))
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


class FakeEngine:
    def __init__(self, original):
        self.containers = {original['Id']: copy.deepcopy(original)}
        self.calls = []
        self.created_bodies = []
        self.fail_start = False
        self.disappear_on_start = False
        self.delete_timeout_after_success = False
        self.delete_refused = False
        self.test_exit = 0
        self.after_create = None

    def resolve(self, identifier):
        for item in self.containers.values():
            if item['Id'] == identifier or item['Name'] == '/' + identifier:
                return item
        raise p.EngineError(404, 'GET', identifier, 'No such container')

    def call(self, method, path, value=None):
        self.calls.append((method, path, copy.deepcopy(value)))
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if method == 'POST' and parsed.path == '/containers/create':
            body = copy.deepcopy(value)
            self.created_bodies.append(copy.deepcopy(body))
            identifier = 'candidate-' + str(len(self.created_bodies))
            mounts = []
            host = body.pop('HostConfig')
            for mount in host.get('Mounts') or []:
                mounts.append({'Type': mount['Type'], 'Source': mount['Source'],
                               'Destination': mount['Target'], 'RW': not mount.get('ReadOnly', False),
                               'Propagation': mount.get('BindOptions', {}).get('Propagation', 'rprivate')})
            for binding in host.get('Binds') or []:
                source, dest, options = binding.split(':', 2)
                mounts.append({'Type': 'bind', 'Source': source, 'Destination': dest,
                               'RW': 'ro' not in options.split(','), 'Propagation': 'rprivate'})
            self.containers[identifier] = {
                'Id': identifier, 'Name': '/' + query['name'][0], 'Image': body['Image'],
                'Config': body, 'HostConfig': host, 'Mounts': mounts,
                'State': {'Running': False, 'Paused': False},
                'NetworkSettings': {'Networks': {host['NetworkMode']: {}}},
            }
            if self.after_create:
                self.after_create()
            return {'Id': identifier}
        pieces = parsed.path.strip('/').split('/')
        identifier = pieces[1]
        item = self.resolve(identifier)
        operation = pieces[2] if len(pieces) > 2 else None
        if method == 'GET' and operation == 'json':
            return copy.deepcopy(item)
        if method == 'DELETE':
            if self.delete_refused:
                raise p.EngineError(500, method, path, 'simulated delete refusal')
            del self.containers[item['Id']]
            if self.delete_timeout_after_success:
                raise TimeoutError('simulated lost DELETE response')
            return None
        if method == 'POST' and operation == 'start':
            if identifier.startswith('candidate-') and self.fail_start:
                if self.disappear_on_start:
                    del self.containers[identifier]
                raise p.EngineError(500, method, path, 'simulated start failure')
            if item['HostConfig'].get('PortBindings') and any(
                    other['State']['Running'] and other['HostConfig'].get('PortBindings')
                    for key, other in self.containers.items() if key != item['Id']):
                raise p.EngineError(500, method, path, 'address already in use')
            item['State']['Running'] = True
            return None
        if method == 'POST' and operation == 'stop':
            item['State']['Running'] = False
            return None
        if method == 'POST' and operation == 'rename':
            name = '/' + query['name'][0]
            if any(other['Name'] == name for key, other in self.containers.items() if key != item['Id']):
                raise p.EngineError(409, method, path, 'name already in use')
            item['Name'] = name
            return None
        if method == 'POST' and operation == 'update':
            item['HostConfig'].update(copy.deepcopy(value))
            return {}
        if method == 'POST' and operation == 'wait':
            item['State']['Running'] = False
            return {'StatusCode': self.test_exit}
        raise AssertionError((method, path, value))


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'template'
        self.root.mkdir()
        self.source = Path(self.tmp.name) / 'patriots-public'
        self.source.mkdir()
        self.conf = self.root / 'existing.conf'
        feeds = '\n'.join(
            'location ^~ /data/eventspy-mirror/' + team + '/ {\n'
            '  alias /srv/fanzone-eventspy/' + team + '/;\n'
            '  autoindex off;\n  default_type application/json;\n'
            '  types { application/json json; }\n'
            '  add_header Cache-Control "no-store" always;\n'
            '  add_header X-Content-Type-Options "nosniff" always;\n'
            '  add_header X-Robots-Tag "noindex" always;\n}' for team in p.EXISTING)
        self.active = ('server {\n# BEGIN FANZONE EVENTSPY FEEDS\n' + feeds
                       + '\n# END FANZONE EVENTSPY FEEDS\nlisten 80;\nroot /site/dist;\n'
                       'add_header X-Robots-Tag "noindex, nofollow" always;\n'
                       'location / { try_files $uri $uri/ =404; }\n}\n')
        self.conf.write_text(self.active)
        self.folder = self.root / 'new'
        self.folder.mkdir()
        self.new_conf = self.folder / 'default.conf'
        self.new_conf.write_text(p.adapt(self.active))
        paths = {'/site': str(self.root), p.CONF: str(self.conf),
                 **{'/srv/fanzone-eventspy/' + team: path for team, path in p.EXISTING.items()}}
        self.old = {
            'Id': 'original', 'Name': '/' + p.NAME, 'Image': 'sha256:' + 'a' * 64,
            'State': {'Running': True, 'Paused': False},
            'Config': {'Image': 'nginx:latest', 'Entrypoint': ['/docker-entrypoint.sh'],
                       'Cmd': ['nginx', '-g', 'daemon off;'], 'Env': ['TZ=UTC'],
                       'Labels': {'maintainer': 'nginx'}, 'WorkingDir': '/',
                       'Healthcheck': {'Test': ['CMD', 'nginx', '-t'], 'Interval': 30000000000}},
            'HostConfig': {'NetworkMode': 'bridge', 'AutoRemove': False, 'Privileged': False,
                           'PortBindings': {'80/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '4326'}]},
                           'RestartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0},
                           'ReadonlyRootfs': True, 'SecurityOpt': ['no-new-privileges'],
                           'Memory': 268435456, 'LogConfig': {'Type': 'json-file', 'Config': {'max-size': '10m'}},
                           'Binds': [source + ':' + dest + ':ro,rprivate' for dest, source in paths.items()],
                           'Mounts': []},
            'Mounts': [{'Type': 'bind', 'Source': source, 'Destination': dest,
                        'RW': False, 'Propagation': 'rprivate'} for dest, source in paths.items()],
            'NetworkSettings': {'Networks': {'bridge': {'IPAddress': '172.17.0.4'}}},
        }
        self.engine = FakeEngine(self.old)
        self.body = p.clone_body(self.old, self.new_conf, self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def switch(self):
        original_layout = p.inspect_layout
        # Capture the unpatched function rather than recursing through the mock.
        check = lambda current: original_layout(current, self.root, self.source)
        with patch.object(p, 'SOURCE', self.source), patch.object(p, 'verify_homepage'), \
                patch.object(p, 'inspect_layout', side_effect=check):
            p.switch(self.engine, self.old, self.body, 'homepage-hash', self.folder,
                     self.conf, self.active)

    def test_config_extends_only_patriots_and_retains_headers(self):
        proposed = p.adapt(self.active)
        new = p.block_at(proposed, proposed.index('location ^~ /data/eventspy-mirror/patriots/'))
        self.assertIn('add_header Cache-Control "no-store" always;', new)
        self.assertIn('types { application/json json; }', new)
        self.assertEqual(proposed.replace(new + '\n', ''), self.active)
        self.assertEqual(p.adapt(proposed), proposed)

    def test_clone_preserves_original_settings_and_uses_immutable_image(self):
        expected = copy.deepcopy(self.old['Config'])
        expected['Image'] = self.old['Image']
        self.assertEqual({key: value for key, value in self.body.items() if key != 'HostConfig'}, expected)
        before = copy.deepcopy(self.old['HostConfig'])
        before['Binds'][1] = str(self.new_conf) + ':' + p.CONF + ':ro,rprivate'
        before['Mounts'] = [{'Type': 'bind', 'Source': str(self.source), 'Target': p.FEED,
                             'ReadOnly': True, 'BindOptions': {'Propagation': 'rprivate'}}]
        self.assertEqual(self.body['HostConfig'], before)
        self.assertEqual(self.engine.containers['original'], self.old)

    def test_candidate_check_uses_same_image_without_ports_or_network(self):
        p.validate_candidate(self.engine, self.body)
        candidate = self.engine.created_bodies[0]
        self.assertEqual(candidate['Image'], self.old['Image'])
        self.assertEqual(candidate['HostConfig']['NetworkMode'], 'none')
        self.assertEqual(candidate['HostConfig']['PortBindings'], {})
        self.assertTrue(candidate['HostConfig']['Mounts'][-1]['ReadOnly'])
        self.assertEqual(self.engine.containers, {'original': self.old})

    def test_structured_mount_options_are_retained(self):
        old = copy.deepcopy(self.old)
        old['HostConfig']['Binds'] = None
        old['HostConfig']['Mounts'] = [
            {'Type': 'bind', 'Source': mount['Source'], 'Target': mount['Destination'],
             'ReadOnly': True, 'BindOptions': {'Propagation': 'rprivate', 'NonRecursive': True}}
            for mount in old['Mounts']]
        body = p.clone_body(old, self.new_conf, self.source)
        mounts = body['HostConfig']['Mounts']
        for mount in mounts[:-1]:
            self.assertTrue(mount['ReadOnly'])
            self.assertTrue(mount['BindOptions']['NonRecursive'])
        self.assertEqual(mounts[1]['Source'], str(self.new_conf))
        self.assertEqual(mounts[-1]['Target'], p.FEED)
        self.assertTrue(mounts[-1]['ReadOnly'])

    def test_bad_nginx_candidate_never_stops_original(self):
        self.engine.test_exit = 1
        with self.assertRaisesRegex(ValueError, 'nginx -t failed'):
            p.validate_candidate(self.engine, self.body)
        self.assertEqual(self.engine.containers, {'original': self.old})
        self.assertFalse(any('/stop' in path for _, path, _ in self.engine.calls))

    def test_success_retains_original_stopped_and_replaces_only_preview(self):
        self.switch()
        original = self.engine.resolve('original')
        current = self.engine.resolve(p.NAME)
        self.assertFalse(original['State']['Running'])
        self.assertEqual(original['HostConfig']['RestartPolicy']['Name'], 'no')
        self.assertTrue(current['State']['Running'])
        self.assertEqual(current['HostConfig']['PortBindings'], self.old['HostConfig']['PortBindings'])
        self.assertEqual(current['Image'], self.old['Image'])
        p.inspect_layout(current, self.root, self.source)
        self.assertEqual(json.loads((self.folder / 'receipt.json').read_text())['status'], 'installed')

    def test_failed_start_restores_original_restart_policy_name_and_running_state(self):
        self.engine.fail_start = True
        with self.assertRaisesRegex(p.EngineError, 'simulated start failure'):
            self.switch()
        self.assertEqual(self.engine.containers, {'original': self.old})
        self.assertEqual(json.loads((self.folder / 'receipt.json').read_text())['status'], 'rolled-back')

    def test_candidate_already_gone_404_does_not_prevent_rollback(self):
        self.engine.fail_start = self.engine.disappear_on_start = True
        with self.assertRaisesRegex(p.EngineError, 'simulated start failure'):
            self.switch()
        self.assertEqual(self.engine.containers, {'original': self.old})
        self.assertEqual(json.loads((self.folder / 'receipt.json').read_text())['status'], 'rolled-back')

    def test_lost_successful_delete_response_still_restores_original(self):
        self.engine.fail_start = True
        self.engine.delete_timeout_after_success = True
        with self.assertRaisesRegex(p.EngineError, 'simulated start failure'):
            self.switch()
        self.assertEqual(self.engine.containers, {'original': self.old})
        receipt = json.loads((self.folder / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'rolled-back')
        self.assertIn('lost DELETE response', receipt['cleanup_error'])

    def test_remaining_candidate_reports_recovery_needed_and_attempts_original_restore(self):
        self.engine.fail_start = True
        self.engine.delete_refused = True
        with self.assertRaisesRegex(p.EngineError, 'simulated start failure'):
            self.switch()
        receipt = json.loads((self.folder / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'needs-recovery')
        self.assertIn('simulated delete refusal', receipt['cleanup_error'])
        self.assertTrue(any('Candidate still exists' in error for error in receipt['recovery_errors']))
        attempts = [path for method, path, _ in self.engine.calls
                    if method == 'POST' and path == '/containers/original/rename?name=' + p.NAME]
        self.assertEqual(len(attempts), 1)

    def test_concurrent_docker_update_is_preserved_without_stopping_original(self):
        def update():
            self.engine.containers['original']['HostConfig']['Memory'] = 123456789
        self.engine.after_create = update
        with self.assertRaisesRegex(ValueError, 'changed during checks'):
            self.switch()
        self.assertEqual(list(self.engine.containers), ['original'])
        self.assertEqual(self.engine.containers['original']['HostConfig']['Memory'], 123456789)
        self.assertTrue(self.engine.containers['original']['State']['Running'])
        self.assertFalse(any('/stop' in path for _, path, _ in self.engine.calls))

    def test_mutable_config_same_container_id_is_rejected(self):
        for key in ('Config', 'HostConfig'):
            with self.subTest(key=key):
                current = copy.deepcopy(self.old)
                current[key]['changed'] = 'local-edit'
                with self.assertRaisesRegex(ValueError, 'changed during checks'):
                    p.verify_original_unchanged(self.old, current, self.conf, self.active)

    def test_inspect_mount_order_is_not_a_configuration_change(self):
        current = copy.deepcopy(self.old)
        current['Mounts'].reverse()
        p.verify_original_unchanged(self.old, current, self.conf, self.active)

    def test_reordered_mounts_still_reject_changed_fields(self):
        for field, value in [('Source', '/different'), ('RW', True), ('Propagation', 'shared')]:
            with self.subTest(field=field):
                current = copy.deepcopy(self.old)
                current['Mounts'].reverse()
                current['Mounts'][0][field] = value
                with self.assertRaisesRegex(ValueError, r'changed during checks \(Mounts\)'):
                    p.verify_original_unchanged(self.old, current, self.conf, self.active)

    def test_added_or_removed_mount_is_rejected(self):
        for change in ('add', 'remove'):
            current = copy.deepcopy(self.old)
            if change == 'add':
                current['Mounts'].append({'Destination': '/extra', 'Source': '/extra', 'RW': False})
            else:
                current['Mounts'].pop()
            with self.assertRaisesRegex(ValueError, r'changed during checks \(Mounts\)'):
                p.verify_original_unchanged(self.old, current, self.conf, self.active)

    def test_real_config_change_reports_field_without_environment_values(self):
        current = copy.deepcopy(self.old)
        current['Config']['Env'].append('PRIVATE_VALUE=do-not-print')
        with self.assertRaises(ValueError) as error:
            p.verify_original_unchanged(self.old, current, self.conf, self.active)
        self.assertIn('(Config)', str(error.exception))
        self.assertNotIn('PRIVATE_VALUE', str(error.exception))
        self.assertNotIn('do-not-print', str(error.exception))

    def test_mount_reordering_during_creation_does_not_prevent_switch(self):
        self.engine.after_create = lambda: self.engine.containers['original']['Mounts'].reverse()
        self.switch()
        self.assertTrue(self.engine.resolve(p.NAME)['State']['Running'])
        self.assertFalse(self.engine.resolve('original')['State']['Running'])

    def test_writable_old_mount_or_production_port_refused(self):
        for field in ('mount', 'port'):
            current = copy.deepcopy(self.old)
            if field == 'mount':
                current['Mounts'][0]['RW'] = True
            else:
                current['HostConfig']['PortBindings']['80/tcp'][0]['HostPort'] = '4322'
            with self.subTest(field=field), self.assertRaises(ValueError):
                p.inspect_layout(current, self.root, self.source)


if __name__ == '__main__':
    unittest.main()
