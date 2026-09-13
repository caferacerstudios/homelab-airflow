#!/usr/bin/env python3
"""Add the Patriots read-only feed to the existing template preview only."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.parse import quote
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler
from uuid import uuid4

NAME = 'templatefanzone-web'
ROOT = Path('/home/laurawkr/templatefanzone')
CONF = '/etc/nginx/conf.d/default.conf'
FEED = '/srv/fanzone-eventspy/patriots'
SOURCE = Path('/var/lib/patriotsfz-eventspy-mirror/dev/public')
EXISTING = {'seahawks': '/var/lib/sfz-eventspy-mirror/dev/public',
            'broncos': '/var/lib/boncosfz-eventspy-mirror/dev/public',
            'packers': '/var/lib/packersfz-eventspy-mirror/dev/public',
            'vikings': '/var/lib/vikingsfz-eventspy-mirror/dev/public',
            'chiefs': '/var/lib/chiefsfz-eventspy-mirror/dev/public'}


def run(*args):
    result = subprocess.run(args, check=True, text=True, capture_output=True, timeout=45)
    return result.stdout


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, pathname):
        super().__init__('localhost', timeout=45)
        self.pathname = pathname

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.pathname)


class EngineError(RuntimeError):
    def __init__(self, status, method, path, message):
        super().__init__(f'Docker {method} {path}: {message}')
        self.status = status


class Engine:
    def __init__(self):
        endpoint = os.environ.get('DOCKER_HOST') if not os.environ.get('DOCKER_CONTEXT') else None
        endpoint = endpoint or run('docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}').strip()
        if not endpoint.startswith('unix://'):
            raise ValueError('Run on wkr using its local Docker socket; remote contexts are not supported')
        self.socket = endpoint[len('unix://'):]
        self.prefix = ''
        version = self.call('GET', '/version')['ApiVersion']
        if not re.fullmatch(r'1\.\d+', version):
            raise ValueError('Unexpected Docker API version')
        self.prefix = '/v' + version

    def call(self, method, path, value=None):
        connection = UnixConnection(self.socket)
        try:
            body = None if value is None else json.dumps(value).encode()
            connection.request(method, self.prefix + path, body=body,
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read()
            if response.status >= 400:
                try:
                    message = json.loads(raw).get('message', 'Docker request failed')
                except (ValueError, AttributeError):
                    message = 'Docker request failed'
                raise EngineError(response.status, method, path, message)
            return json.loads(raw) if raw else None
        finally:
            connection.close()


def block_at(text, start):
    opening = text.index('{', start)
    depth, quoted, escaped, comment = 0, None, False, False
    for index in range(opening, len(text)):
        char = text[index]
        if comment:
            comment = char != '\n'
        elif quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quoted:
                quoted = None
        elif char in "\"'":
            quoted = char
        elif char == '#':
            comment = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ValueError('Unclosed nginx block')


def adapt(config):
    if re.findall(r'^\s*root\s+([^;]+);', config, re.M) != ['/site/dist']:
        raise ValueError('Expected the existing /site/dist preview')
    if len(re.findall(r'^\s*server\s*\{', config, re.M)) != 1:
        raise ValueError('Expected one preview server block')
    if config.count('# END FANZONE EVENTSPY FEEDS') != 1:
        raise ValueError('Expected the existing EventSpy feed block marker')
    sample = None
    for team in EXISTING:
        match = re.search(r'location\s+\^~\s+/data/eventspy-mirror/' + team + r'/\s*\{', config)
        if not match:
            raise ValueError(f'Existing {team} nginx feed is missing')
        block = block_at(config, match.start())
        if not re.search(r'\balias\s+/srv/fanzone-eventspy/' + team + r'/\s*;', block):
            raise ValueError(f'Unexpected {team} nginx alias')
        if team == 'seahawks':
            sample = block
    match = re.search(r'location\s+\^~\s+/data/eventspy-mirror/patriots/\s*\{', config)
    if match:
        block = block_at(config, match.start())
        if not re.search(r'\balias\s+' + re.escape(FEED) + r'/\s*;', block):
            raise ValueError('A conflicting Patriots alias already exists')
        return config
    added = sample.replace('/seahawks/', '/patriots/')
    return config.replace('# END FANZONE EVENTSPY FEEDS', added + '\n# END FANZONE EVENTSPY FEEDS', 1)


def inspect_layout(old, root=ROOT, source=SOURCE):
    if old.get('Name') != '/' + NAME or not old.get('State', {}).get('Running') or old['State'].get('Paused'):
        raise ValueError('Expected the running template preview')
    host = old['HostConfig']
    if host.get('AutoRemove') or host.get('ContainerIDFile'):
        raise ValueError('Unexpected automatic-removal/CID-file configuration; original left intact')
    if host.get('NetworkMode') not in ('default', 'bridge'):
        raise ValueError('A custom preview network needs its own reviewed migration; original left intact')
    if len(old.get('NetworkSettings', {}).get('Networks', {})) != 1:
        raise ValueError('Expected the single existing bridge network')
    if host.get('Privileged') or old['Config'].get('Labels', {}).get('com.docker.compose.project'):
        raise ValueError('Unexpected privileged or Compose-managed preview')
    ports = host.get('PortBindings') or {}
    if set(ports) != {'80/tcp'} or not ports['80/tcp'] or any(str(p.get('HostPort')) != '4326' for p in ports['80/tcp']):
        raise ValueError('Expected only the preview port 4326 -> 80')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', old.get('Image', '')):
        raise ValueError('Expected the immutable running image ID')
    mounts = old['Mounts']
    by_dest = {m['Destination']: m for m in mounts}
    if len(by_dest) != len(mounts) or any(m.get('Type') != 'bind' or m.get('RW') for m in mounts):
        raise ValueError('Expected unique read-only bind mounts')
    expected = {'/site': str(root), CONF: None, **{'/srv/fanzone-eventspy/' + k: v for k, v in EXISTING.items()}}
    if FEED in by_dest:
        expected[FEED] = str(source)
    if set(by_dest) != set(expected):
        raise ValueError('Preview mounts changed from the reviewed layout; original left intact')
    for target, path in expected.items():
        if path is not None and by_dest[target]['Source'] != path:
            raise ValueError(f'Unexpected mount at {target}')
    if any(m.get('Propagation', 'rprivate') != 'rprivate' for m in mounts):
        raise ValueError('Unexpected bind propagation; original left intact')
    conf = Path(by_dest[CONF]['Source'])
    if conf.is_symlink() or not conf.is_file() or root not in conf.parents:
        raise ValueError('Expected the preview config file within the template checkout')
    if not source.is_dir() or source.is_symlink():
        raise ValueError('Prepare the Patriots host paths first: /var/lib/patriotsfz-eventspy-mirror/dev/public is missing or not an ordinary directory. Follow docs/patriots-onboarding.md in homelab-airflow.')
    return conf, FEED in by_dest


def clone_body(old, conf, source=SOURCE):
    body = copy.deepcopy(old['Config'])
    body['Image'] = old['Image']
    host = copy.deepcopy(old['HostConfig'])
    # Preserve every other create-time setting, including ports, environment,
    # command, healthcheck, log driver, resources, capabilities and restart policy.
    # Preserve the original representation and bind options, including recursive
    # settings. Docker accepts both Mounts objects and legacy Binds strings.
    represented = []
    for mount in host.get('Mounts') or []:
        represented.append(mount['Target'])
        if mount['Target'] == CONF:
            mount['Source'] = str(conf)
    binds = []
    for binding in host.get('Binds') or []:
        matches = [m for m in old['Mounts'] if binding == m['Source'] + ':' + m['Destination']
                   or binding.startswith(m['Source'] + ':' + m['Destination'] + ':')]
        if len(matches) != 1:
            raise ValueError('Unrecognized legacy bind representation')
        mount = matches[0]
        represented.append(mount['Destination'])
        binds.append(str(conf) + binding[len(mount['Source']):] if mount['Destination'] == CONF else binding)
    if host.get('Binds') is not None:
        host['Binds'] = binds
    if sorted(represented) != sorted(m['Destination'] for m in old['Mounts']):
        raise ValueError('Create-time mounts do not match the inspected mounts')
    if FEED not in represented:
        host['Mounts'] = (host.get('Mounts') or []) + [
            {'Type': 'bind', 'Source': str(source), 'Target': FEED,
             'ReadOnly': True, 'BindOptions': {'Propagation': 'rprivate'}}]
    body['HostConfig'] = host
    return body


def create(engine, name, body):
    return engine.call('POST', '/containers/create?name=' + quote(name, safe=''), body)['Id']


def remove_candidate(engine, identifier):
    """Only an already-absent candidate is harmless during cleanup."""
    try:
        engine.call('DELETE', f'/containers/{identifier}?force=true')
    except EngineError as error:
        if error.status != 404:
            raise


def verify_original_unchanged(old, current, conf, active):
    # Docker update can change restart policy/resources without replacing the ID.
    # Runtime mounts are an unordered set; Docker may return a different order.
    # Compare every mount field after ordering by its unique destination.
    keys = ('Id', 'Name', 'Image', 'Config', 'HostConfig')
    changes = [key for key in keys if current.get(key) != old.get(key)]
    mounts = lambda value: sorted(value['Mounts'], key=lambda mount: mount['Destination'])
    networks = lambda value: sorted(value.get('NetworkSettings', {}).get('Networks', {}))
    if mounts(current) != mounts(old):
        changes.append('Mounts')
    if networks(current) != networks(old):
        changes.append('NetworkSettings.Networks')
    if not current.get('State', {}).get('Running'):
        changes.append('State.Running')
    if current.get('State', {}).get('Paused'):
        changes.append('State.Paused')
    if conf.read_text() != active:
        changes.append('nginx file contents')
    if changes:
        # Name changed fields without exposing environment/configuration values.
        raise ValueError('Preview changed during checks (' + ', '.join(changes)
                         + '); original left untouched')


def validate_candidate(engine, body):
    test = copy.deepcopy(body)
    test['Entrypoint'], test['Cmd'] = ['nginx'], ['-t']
    test['HostConfig'].update(NetworkMode='none', PortBindings={}, PublishAllPorts=False,
                              RestartPolicy={'Name': 'no', 'MaximumRetryCount': 0}, AutoRemove=False)
    identifier = create(engine, NAME + '-patriots-check-' + uuid4().hex[:8], test)
    try:
        engine.call('POST', f'/containers/{identifier}/start')
        result = engine.call('POST', f'/containers/{identifier}/wait?condition=not-running')
        if result.get('StatusCode') != 0:
            raise ValueError('Candidate nginx -t failed. The current preview was left running.')
    finally:
        remove_candidate(engine, identifier)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def homepage():
    with build_opener(ProxyHandler({}), NoRedirect()).open('http://127.0.0.1:4326/', timeout=5) as response:
        if response.status != 200 or 'noindex' not in response.headers.get('X-Robots-Tag', '').lower():
            raise ValueError('Expected the existing successful noindex preview homepage')
        return hashlib.sha256(response.read()).hexdigest()


def verify_homepage(expected):
    error = None
    for _ in range(10):
        try:
            if homepage() != expected:
                raise ValueError('Preview homepage changed during the switch')
            return
        except (OSError, ValueError) as exc:
            error = exc
            time.sleep(0.5)
    raise RuntimeError(f'Preview verification failed: {error}')


def switch(engine, old, body, expected, folder, conf=None, active=None):
    original = old['Id']
    backup_name = NAME + '-before-patriots-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    candidate = create(engine, NAME + '-patriots-candidate-' + uuid4().hex[:8], body)
    receipt = {'container': NAME, 'original_id': original, 'retained_container': backup_name,
               'original_restart_policy': old['HostConfig']['RestartPolicy'], 'new_id': candidate,
               'config': str(folder / 'default.conf'), 'status': 'prepared'}
    receipt_path = folder / 'receipt.json'
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    stopping = False
    try:
        if conf is not None:
            # Check again after creation, immediately before touching the original.
            latest = engine.call('GET', '/containers/' + NAME + '/json')
            verify_original_unchanged(old, latest, conf, active)
        stopping = True
        engine.call('POST', f'/containers/{original}/stop?t=10')
        engine.call('POST', f'/containers/{original}/rename?name=' + backup_name)
        engine.call('POST', f'/containers/{original}/update', {'RestartPolicy': {'Name': 'no', 'MaximumRetryCount': 0}})
        engine.call('POST', f'/containers/{candidate}/rename?name=' + NAME)
        engine.call('POST', f'/containers/{candidate}/start')
        verify_homepage(expected)
        actual = engine.call('GET', f'/containers/{candidate}/json')
        inspect_layout(actual)
        if not any(m['Destination'] == FEED and m['Source'] == str(SOURCE) and not m['RW'] for m in actual['Mounts']):
            raise ValueError('Patriots mount did not match the reviewed configuration')
    except BaseException:
        # A failed DELETE response does not establish whether Docker removed it.
        # Always try to restore the original by immutable ID independently.
        recovery_errors = []
        try:
            remove_candidate(engine, candidate)
        except BaseException as cleanup_error:
            receipt['cleanup_error'] = str(cleanup_error)
            print('Candidate cleanup response failed; checking actual state:', cleanup_error)
        try:
            if stopping:
                # Inspect actual state: a timed-out stop/rename can still have
                # succeeded in Docker even if its response was not received.
                state = engine.call('GET', f'/containers/{original}/json')
                if state['Name'] != '/' + NAME:
                    engine.call('POST', f'/containers/{original}/rename?name=' + NAME)
                engine.call('POST', f'/containers/{original}/update', {'RestartPolicy': old['HostConfig']['RestartPolicy']})
                if not state['State']['Running']:
                    engine.call('POST', f'/containers/{original}/start')
                verify_homepage(expected)
                print('The original preview container was restored.')
        except BaseException as recovery_error:
            recovery_errors.append(str(recovery_error))
        # Do not report complete rollback while a remaining candidate could
        # retain a name, bind a port, or restart later with the cloned policy.
        try:
            remaining = engine.call('GET', f'/containers/{candidate}/json')
            recovery_errors.append('Candidate still exists: ' + candidate
                                   + '; running=' + str(remaining.get('State', {}).get('Running')))
        except EngineError as inspect_error:
            if inspect_error.status != 404:
                recovery_errors.append('Could not verify candidate removal: ' + str(inspect_error))
        except BaseException as inspect_error:
            recovery_errors.append('Could not verify candidate removal: ' + str(inspect_error))
        receipt['status'] = 'needs-recovery' if recovery_errors else 'rolled-back'
        if recovery_errors:
            receipt['recovery_errors'] = recovery_errors
            print('Automatic recovery needs attention; retain both containers and inspect:', receipt_path)
            for recovery_error in recovery_errors:
                print('Recovery error:', recovery_error)
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        raise
    receipt['status'] = 'installed'
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    print('Patriots preview mount and alias installed. Existing homepage verified unchanged.')
    print('Retained stopped rollback container:', backup_name)
    print('Keep this active configuration directory:', folder)
    print('Actual Patriots prices require a successful scheduled EventSpy collection.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    sites = json.loads((ROOT / 'config/active-sites.json').read_text())
    site = sites.get('patriots', {})
    if site.get('abbreviation') != 'NE' or site.get('balldontlie_team_id') != 1 or site.get('eventspy', {}).get('output_dir') != str(SOURCE):
        raise ValueError('Apply the reviewed Patriots source update first')
    engine = Engine()
    old = engine.call('GET', '/containers/' + NAME + '/json')
    if run('docker', 'inspect', '--format', '{{.Id}}', NAME).strip() != old['Id']:
        raise ValueError('Docker CLI and socket refer to different containers')
    conf, mounted = inspect_layout(old)
    active = run('docker', 'exec', old['Id'], 'cat', CONF)
    if conf.read_text() != active:
        raise ValueError('Mounted nginx bytes differ from the host file; original left untouched')
    proposed = adapt(active)
    expected = homepage()
    if mounted and proposed == active:
        print('Patriots preview mount and alias are already installed; no change needed.')
        return
    runtime = ROOT / '.sites-runtime'
    if runtime.is_symlink():
        raise ValueError('Unexpected runtime symlink')
    runtime.mkdir(exist_ok=True)
    folder = runtime / ('patriots-preview-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:8])
    folder.mkdir(mode=0o700)
    new_conf = folder / 'default.conf'
    new_conf.write_text(proposed)
    new_conf.chmod(0o644)
    body = clone_body(old, new_conf)
    validate_candidate(engine, body)
    print('Candidate nginx configuration passed using the running image and all six read-only feeds.')
    if args.check:
        new_conf.unlink()
        folder.rmdir()
        print('Current preview kept running. No Airflow, Variable, collection or build changes.')
        return
    # Refuse a concurrent replacement/config edit before changing the live name.
    latest = engine.call('GET', '/containers/' + NAME + '/json')
    verify_original_unchanged(old, latest, conf, active)
    switch(engine, old, body, expected, folder, conf, active)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'PREVIEW UPDATE STOPPED: {exc}')
