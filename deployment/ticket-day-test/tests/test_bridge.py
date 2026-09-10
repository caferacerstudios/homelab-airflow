import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

MODULE = Path(__file__).resolve().parents[1] / 'bridge.py'
spec = importlib.util.spec_from_file_location('ticket_bridge', MODULE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class FakeHost:
    def __init__(self):
        self.starts = 0
        self.restores = 0
        self.active = 'inactive'
        self.timer_state = ('disabled', 'inactive')
        self.fail_start = False
        self.good_summary = True
        self.good_fingerprints = True

    def timer(self):
        return self.timer_state

    def service(self):
        return dict(LoadState='loaded', ActiveState=self.active, Result='success',
                    ExecMainStatus='0', InvocationID=f'{self.starts:032x}')

    def fingerprints(self):
        self.verify_configuration()
        return (bridge.RUNNER_SHA, bridge.IMAGE_ID) if self.good_fingerprints else ('wrong', 'wrong')

    def verify_configuration(self):
        return None

    def start(self, previous):
        self.starts += 1
        if self.fail_start:
            raise TimeoutError('simulated disconnected wait')
        return 0, f'{self.starts:032x}'

    def summary(self, invocation):
        if not self.good_summary:
            return None
        return dict(outcome='EVENTSPY_SEASON_SUCCESS', authorized=16, succeeded=16, failed=0, unavailable=1)

    def restore(self):
        self.restores += 1


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'requests').mkdir()
        (self.root / 'responses').mkdir()
        self.host = FakeHost()
        self.now = datetime(2026, 9, 9, 20, 0, tzinfo=timezone.utc)  # 13:00 Seattle
        self.app = bridge.Bridge(self.root, self.host, lambda: self.now)
        self.app.init()

    def collect(self, slot='2026-09-09T22:00:00+00:00', nonce=''):
        request_id = hashlib.sha256((slot + nonce).encode()).hexdigest()
        name = request_id + '.json'
        body = dict(version=1, operation='collect', request_id=request_id, slot=slot)
        (self.root / 'requests' / name).write_text(json.dumps(body))
        self.app.process()
        return json.loads((self.root / 'responses' / name).read_text())

    def at_first_slot(self):
        self.now = datetime(2026, 9, 9, 22, 0, 5, tzinfo=timezone.utc)

    def test_budget_consumes_failed_or_successful_attempt_and_blocks_eighth(self):
        self.app.arm(6)
        self.at_first_slot()
        self.assertEqual(self.collect()['status'], 'success')
        self.now = datetime(2026, 9, 10, 1, 0, 5, tzinfo=timezone.utc)
        self.assertEqual(self.collect('2026-09-10T01:00:00+00:00')['status'], 'skipped')
        self.assertEqual(self.host.starts, 1)
        self.assertEqual(self.app.status()['total_attempts'], 7)

    def test_duplicate_request_id_and_alternate_id_do_not_repeat_source(self):
        self.app.arm(4)
        self.at_first_slot()
        self.assertEqual(self.collect()['status'], 'success')
        receipt = next((self.root / 'responses').iterdir()).read_bytes()
        self.assertEqual(self.collect()['status'], 'success')
        self.assertEqual(next((self.root / 'responses').iterdir()).read_bytes(), receipt)
        self.assertEqual(self.collect(nonce='different-id')['status'], 'skipped')
        self.assertEqual(self.host.starts, 1)

    def test_future_late_wrong_day_and_off_schedule_do_not_debit(self):
        self.app.arm(4)
        self.assertEqual(self.collect()['status'], 'skipped')
        self.now = datetime(2026, 9, 9, 22, 11, tzinfo=timezone.utc)
        self.assertEqual(self.collect()['status'], 'skipped')
        self.assertEqual(self.collect('2026-09-09T22:10:00+00:00')['status'], 'skipped')
        self.assertEqual(self.collect('2026-09-10T22:00:00+00:00')['status'], 'skipped')
        self.assertEqual(self.host.starts, 0)
        self.assertEqual(self.app.status()['total_attempts'], 4)

    def test_before_activation_slot_does_not_run_even_if_fresh(self):
        self.now = datetime(2026, 9, 9, 22, 0, 2, tzinfo=timezone.utc)
        self.app.arm(4)
        self.assertEqual(self.collect()['status'], 'skipped')
        self.assertEqual(self.host.starts, 0)

    def test_midnight_disarms_by_date_even_without_restore_timer(self):
        self.app.arm(4)
        self.now = datetime(2026, 9, 10, 10, 0, 2, tzinfo=timezone.utc)
        self.assertEqual(self.collect('2026-09-10T10:00:00+00:00')['status'], 'skipped')
        self.assertEqual(self.host.starts, 0)

    def test_interrupted_attempt_remains_pending_and_blocks_new_slot(self):
        self.app.arm(4)
        self.at_first_slot()
        self.host.fail_start = True
        self.assertEqual(self.collect()['status'], 'failed')
        self.now = datetime(2026, 9, 10, 1, 0, 5, tzinfo=timezone.utc)
        self.host.fail_start = False
        self.assertEqual(self.collect('2026-09-10T01:00:00+00:00')['status'], 'failed')
        self.assertEqual(self.host.starts, 1)
        self.assertEqual(self.app.status()['unresolved_attempts'], 1)

    def test_bad_summary_is_failure_and_consumes_attempt(self):
        self.app.arm(4)
        self.at_first_slot()
        self.host.good_summary = False
        self.assertEqual(self.collect()['status'], 'failed')
        self.assertEqual(self.app.status()['total_attempts'], 5)

    def test_arm_rejects_busy_timer_fingerprint_and_exhausted_day(self):
        for active in ('active', 'activating', 'deactivating'):
            self.host.active = active
            with self.assertRaises(ValueError):
                self.app.arm(4)
        self.host.active = 'inactive'
        self.host.timer_state = ('enabled', 'active')
        with self.assertRaises(ValueError):
            self.app.arm(4)
        self.host.timer_state = ('disabled', 'inactive')
        self.host.good_fingerprints = False
        with self.assertRaises(ValueError):
            self.app.arm(4)
        self.host.good_fingerprints = True
        with self.assertRaises(ValueError):
            self.app.arm(7)

    def test_rearm_does_not_erase_prior_or_source_debits(self):
        self.app.arm(4)
        self.at_first_slot()
        self.collect()
        self.app.arm(0)
        state = self.app.status()
        self.assertEqual(state['prior_attempts'], 4)
        self.assertEqual(state['total_attempts'], 5)

    def test_restore_disarms_before_busy_refusal_then_restores_when_idle(self):
        self.app.arm(4)
        self.host.active = 'activating'
        with self.assertRaises(ValueError):
            self.app.restore()
        self.assertFalse(self.app.status()['armed'])
        self.assertEqual(self.host.restores, 0)
        self.host.active = 'inactive'
        self.app.restore()
        self.assertEqual(self.host.restores, 1)

    def test_queue_rejects_symlink_and_invalid_identity_without_source(self):
        self.app.arm(4)
        self.at_first_slot()
        name = 'a' * 64 + '.json'
        secret = self.root / 'secret'
        secret.write_text('private text must not be returned')
        (self.root / 'requests' / name).symlink_to(secret)
        self.app.process()
        output = (self.root / 'responses' / name).read_text()
        self.assertNotIn('private text', output)
        self.assertEqual(json.loads(output)['status'], 'failed')
        self.assertEqual(self.host.starts, 0)

    def test_health_is_source_free(self):
        name = 'b' * 64 + '.json'
        (self.root / 'requests' / name).write_text(json.dumps(dict(version=1, operation='health', request_id='b' * 64)))
        self.app.process()
        output = json.loads((self.root / 'responses' / name).read_text())
        self.assertEqual(output['status'], 'success')
        self.assertFalse(output['armed'])
        self.assertEqual(self.host.starts, 0)

    def test_poison_queue_entries_are_quarantined_without_recursion(self):
        requests = self.root / 'requests'
        (requests / 'not-a-request.json').write_text('invalid')
        (requests / ('c' * 64 + '.json')).mkdir()
        (requests / ('c' * 64 + '.json') / 'nested').write_text('retained')
        (requests / 'odd-directory').mkdir()
        self.app.process()
        self.assertEqual(list(requests.iterdir()), [])
        quarantined = list((self.root / 'state' / 'quarantine').iterdir())
        self.assertEqual(len(quarantined), 3)
        self.assertTrue(any(item.is_dir() and (item / 'nested').exists() for item in quarantined))
        self.assertEqual(self.host.starts, 0)

    def test_queue_work_is_bounded_to_one_hundred_entries(self):
        for index in range(105):
            (self.root / 'requests' / f'invalid-{index}').write_text('invalid')
        self.assertEqual(self.app.process()['processed'], 100)
        self.assertEqual(len(list((self.root / 'requests').iterdir())), 5)
        self.assertEqual(self.app.process()['processed'], 5)

    def test_unit_validation_rejects_extra_commands_and_persistent_timer(self):
        props = dict(ExecStart=f'{{ path={bridge.RUNNER} ; argv[]={bridge.RUNNER} ; ignore_errors=no ; pid=0 ; status=0/0 }}',
                     ExecStartPre='', ExecStartPost='', ExecCondition='', ExecStop='', ExecStopPost='',
                     Type='oneshot', Restart='no', RemainAfterExit='no')
        bridge.Host.validate_unit_properties(props, 'no')
        for changed in (
            dict(props, ExecStart=props['ExecStart'].replace(' ; ignore_errors', ' extra-argument ; ignore_errors')),
            dict(props, ExecStart=props['ExecStart'] + ' ' + props['ExecStart']),
            dict(props, ExecStartPost='another collector'),
            dict(props, Restart='on-failure'),
        ):
            with self.assertRaises(ValueError):
                bridge.Host.validate_unit_properties(changed, 'no')
        with self.assertRaises(ValueError):
            bridge.Host.validate_unit_properties(props, 'yes')


if __name__ == '__main__':
    unittest.main()
