"""Explicit full refresh regenerates future guides without losing accepted data."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'guide_force_fixtures', Path(__file__).with_name('test_guides_core.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
core = fixtures.core


def file_bytes(directory):
    return {path.relative_to(directory): path.read_bytes()
            for path in directory.rglob('*') if path.is_file()}


def future_game(number):
    day = fixtures.NOW.date() + timedelta(days=8 + number * 7)
    return {**fixtures.GAME, 'gameId': str(2000000 + number), 'week': 2 + number,
            'date': day.isoformat(), 'startsAt': day.isoformat() + 'T20:25:00.000Z'}


class ForcedSelectionTests(unittest.TestCase):
    def test_force_includes_fresh_and_distant_guides_beyond_batch_limit(self):
        upcoming = [future_game(number) for number in range(6)]
        excluded = [
            {**future_game(7), 'state': 'completed'},
            {**future_game(8), 'state': 'in_progress'},
            {**future_game(9), 'state': 'postponed'},
            {**future_game(10), 'date': '2026-09-11', 'startsAt': '2026-09-11T20:25:00.000Z'},
            {**future_game(11), 'date': '2026-09-12', 'startsAt': core.iso(fixtures.NOW)},
        ]
        games = {game['gameId']: game for game in upcoming + excluded}
        previous = {game['gameId']: core.make_records(fixtures.draft_fixture(), fixtures.SOURCES,
                                                     game, fixtures.SITE, fixtures.NOW)[0]
                    for game in upcoming[:2]}
        config = core.load_config()
        normal = core.select_games(games, previous, config, fixtures.NOW)
        forced = core.select_games(games, previous, config, fixtures.NOW, force_all=True)
        self.assertEqual([game['gameId'] for game in normal],
                         [game['gameId'] for game in upcoming[2:5]])
        self.assertEqual([game['gameId'] for game in forced],
                         [game['gameId'] for game in upcoming])


class ForcedPublicationTests(unittest.TestCase):
    setUp = fixtures.PublicationTests.setUp

    def collect(self, run, *, force=False, now=fixtures.NOW, generator=fixtures.accepted_fixture):
        return core.collect(run, fixtures.SITE, now=now, generate_fn=generator, force_all=force)

    def provider_generator(self, calls):
        def provider(payload, key):
            self.assertEqual(key, 'test-secret')
            calls.append(deepcopy(payload))
            return (fixtures.response_fixture('Current official entry and transportation guidance.', True)
                    if 'tools' in payload else fixtures.response_fixture(json.dumps(fixtures.draft_fixture())))

        def generate(directory, key, config, game, site, now):
            return core.generate(directory, key, config, game, site, now, provider)

        return generate

    def test_forced_run_gets_fresh_research_and_retries_do_not_spend_again(self):
        calls = []
        generate = self.provider_generator(calls)
        initial = self.collect('ordinary-run', generator=generate)
        original = Path(initial['snapshotPath'])
        original_bytes = file_bytes(original)
        original_cache = self.runtime / 'evidence' / initial['evidenceKeys'][fixtures.GAME['gameId']]
        original_evidence = file_bytes(original_cache)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('forceAll', initial)

        forced = self.collect('force-one', force=True, generator=generate)
        self.assertEqual(len(calls), 4)
        self.assertEqual(forced['openaiRequestCount'], 2)
        self.assertTrue(forced['forceAll'])
        self.assertEqual(forced['generatedGameIds'], [fixtures.GAME['gameId']])
        self.assertNotEqual(forced['evidenceKeys'], initial['evidenceKeys'])
        self.assertEqual(file_bytes(original_cache), original_evidence)
        self.assertEqual(file_bytes(original), original_bytes)

        repeated = self.collect('force-one', force=True, now=fixtures.NOW + timedelta(days=1), generator=generate)
        self.assertTrue(repeated['reused'])
        self.assertEqual(len(calls), 4)
        normal = self.collect('ordinary-after-force', generator=generate)
        self.assertEqual(normal['generatedGameIds'], [])
        self.assertEqual(normal['openaiRequestCount'], 0)
        self.assertEqual(len(calls), 4)
        with self.assertRaises(ValueError):
            self.collect('ordinary-run', force=True, generator=generate)
        with self.assertRaises(ValueError):
            self.collect('force-one', generator=generate)
        self.assertEqual(len(calls), 4)

    def test_failed_publication_retains_current_and_forced_cache_survives_midnight(self):
        initial = self.collect('previous-good-run')
        previous = Path(initial['snapshotPath'])
        previous_bytes = file_bytes(previous)
        old_evidence = file_bytes(self.runtime / 'evidence')
        calls = []
        generate = self.provider_generator(calls)

        # The final source check fails after both Responses have been saved.
        # Retrying the same explicit refresh the next day must reuse those
        # responses and their actual check time rather than spend again.
        with patch.object(core, 'source_commit', return_value='b' * 40):
            with self.assertRaisesRegex(RuntimeError, 'source changed during collection'):
                self.collect('interrupted-force', force=True, generator=generate)
        self.assertEqual(len(calls), 2)
        self.assertEqual((self.runtime / 'current').resolve(), previous)
        self.assertEqual(file_bytes(previous), previous_bytes)
        for name, value in old_evidence.items():
            self.assertEqual((self.runtime / 'evidence' / name).read_bytes(), value)

        result = self.collect('interrupted-force', force=True,
                              now=fixtures.NOW + timedelta(days=1), generator=generate)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['openaiRequestCount'], 0)
        self.assertTrue(result['forceAll'])
        _, guides, watch = core.verify_snapshot(Path(result['snapshotPath']), fixtures.SITE)
        self.assertEqual(guides['games'][fixtures.GAME['gameId']]['lastUpdated'], core.iso(fixtures.NOW))
        self.assertEqual(watch['games'][0]['lastUpdated'], core.iso(fixtures.NOW))
        self.assertEqual(file_bytes(previous), previous_bytes)

    def test_full_publication_preserves_completed_history_and_regenerates_all_future(self):
        initial = self.collect('accepted-history')
        original_guide = core.verify_snapshot(Path(initial['snapshotPath']), fixtures.SITE)[1]['games'][fixtures.GAME['gameId']]
        self.schedule['games'][fixtures.GAME['gameId']]['state'] = 'completed'
        upcoming = [future_game(number) for number in range(1, 6)]
        self.schedule['games'].update({game['gameId']: game for game in upcoming})
        forced = self.collect('all-future', force=True)
        self.assertEqual(forced['generatedGameIds'], [game['gameId'] for game in upcoming])
        self.assertEqual(forced['openaiRequestCount'], 10)
        _, guides, watch = core.verify_snapshot(Path(forced['snapshotPath']), fixtures.SITE)
        self.assertEqual(guides['games'][fixtures.GAME['gameId']], original_guide)
        self.assertEqual(set(guides['games']), {fixtures.GAME['gameId'], *[game['gameId'] for game in upcoming]})
        self.assertEqual(len(watch['games']), 6)


class ForcedCommandTests(unittest.TestCase):
    def test_all_active_reads_enabled_sites_and_passes_explicit_force(self):
        spec = importlib.util.spec_from_file_location(
            'guides_force_command', fixtures.ROOT / 'deployment/guides/refresh_guides.py')
        command = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'guides_core': core}):
            spec.loader.exec_module(command)
        active = {
            'seahawks': deepcopy(fixtures.SITE),
            'broncos': {**fixtures.SITE, 'slug': 'broncos', 'city': 'Denver', 'name': 'Broncos'},
            'patriots': {**fixtures.SITE, 'slug': 'patriots', 'enabled': False},
        }
        original = deepcopy(active)
        with (patch.object(command, 'load_active_sites', return_value=active) as load_sites,
              patch.object(command, 'site_settings', side_effect=lambda site: site),
              patch.object(command, 'collect', return_value={'reused': False}) as collect,
              patch.object(command.signal, 'signal'),
              patch('sys.stdout', new_callable=io.StringIO),
              patch('sys.stderr', new_callable=io.StringIO),
              patch('sys.argv', ['refresh_guides.py', '--run-id', 'operator-force', '--all-active', '--force-all'])):
            self.assertEqual(command.main(), 0)
            load_sites.assert_called_once_with()
            self.assertEqual([call.args for call in collect.call_args_list],
                             [('operator-force', active['broncos']), ('operator-force', active['seahawks'])])
            self.assertEqual([call.kwargs for call in collect.call_args_list], [{'force_all': True}] * 2)
            self.assertEqual(active, original)

            load_sites.reset_mock()
            collect.reset_mock()
            with patch('sys.argv', ['refresh_guides.py', '--check', '--all-active', '--force-all']):
                with self.assertRaises(SystemExit) as error:
                    command.main()
            self.assertEqual(error.exception.code, 2)
            load_sites.assert_not_called()
            collect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
