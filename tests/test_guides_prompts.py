"""Prompt rollout keeps evidence caches and accepted publications safe."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'guide_prompt_fixtures', Path(__file__).with_name('test_guides_core.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
core = fixtures.core
LEGACY_PROMPT_VERSION = 'fan-zone-guides-v2-detailed'


def file_bytes(directory):
    return {path.relative_to(directory): path.read_bytes()
            for path in directory.rglob('*') if path.is_file()}


class PromptRolloutTests(unittest.TestCase):
    setUp = fixtures.PublicationTests.setUp
    collect = fixtures.PublicationTests.collect

    def test_failed_old_prompt_gets_new_evidence_without_overwriting_saved_responses(self):
        self.assertNotEqual(core.PROMPT_VERSION, LEGACY_PROMPT_VERSION)
        with patch.object(core, 'PROMPT_VERSION', LEGACY_PROMPT_VERSION):
            self.collect('previous-good-run', fixtures.NOW - timedelta(days=2))
        previous = (self.runtime / 'current').resolve()
        previous_bytes = file_bytes(previous)
        calls = []
        draft = fixtures.draft_fixture()
        draft.update(summary=None, transportation=[], stadiumTips=[])

        def provider(payload, key):
            self.assertEqual(key, 'test-secret')
            calls.append(deepcopy(payload))
            return (fixtures.response_fixture('Cited stadium entry and transportation research.', True)
                    if 'tools' in payload else fixtures.response_fixture(json.dumps(draft)))

        def generate(directory, key, config, game, site, now):
            return core.generate(directory, key, config, game, site, now, provider)

        # Simulate the saved namespace from the previous prompt version. The
        # failed draft must still fail the unchanged publication quality gate.
        before_directories = set((self.runtime / 'evidence').iterdir())
        with patch.object(core, 'PROMPT_VERSION', LEGACY_PROMPT_VERSION):
            with self.assertRaisesRegex(ValueError, 'grounded summary and at least two useful'):
                self.collect('retry-after-prompt-update', generator=generate)
        self.assertEqual(len(calls), 2)
        self.assertEqual((self.runtime / 'current').resolve(), previous)
        self.assertEqual(file_bytes(previous), previous_bytes)
        old_directories = set((self.runtime / 'evidence').iterdir()) - before_directories
        self.assertEqual(len(old_directories), 1)
        old_cache = old_directories.pop()
        old_bytes = file_bytes(old_cache)
        self.assertEqual(sum(path.name.endswith(('-request.json', '-response.json'))
                             for path in old_bytes), 4)
        self.assertIn(Path('validation.json'), old_bytes)
        old_request = json.loads((old_cache / 'request.json').read_text())
        self.assertEqual(old_request['cacheRequest']['promptVersion'], LEGACY_PROMPT_VERSION)

        draft = fixtures.draft_fixture()
        recovered = self.collect('retry-after-prompt-update', generator=generate)
        self.assertEqual(len(calls), 4, 'The revised prompt gets only one research and one writing call')
        self.assertEqual(recovered['openaiRequestCount'], 2)
        self.assertEqual(recovered['generatedGameIds'], [fixtures.GAME['gameId']])
        new_cache = self.runtime / 'evidence' / recovered['evidenceKeys'][fixtures.GAME['gameId']]
        self.assertNotEqual(new_cache, old_cache)
        new_request = json.loads((new_cache / 'request.json').read_text())
        self.assertEqual(new_request['cacheRequest']['promptVersion'], core.PROMPT_VERSION)
        self.assertEqual({k: v for k, v in new_request['cacheRequest'].items() if k != 'promptVersion'},
                         {k: v for k, v in old_request['cacheRequest'].items() if k != 'promptVersion'})
        self.assertEqual(file_bytes(old_cache), old_bytes)
        self.assertEqual(file_bytes(previous), previous_bytes)
        published_bytes = file_bytes(Path(recovered['snapshotPath']))

        def no_generation(*_):
            self.fail('An already accepted run must not research or write again')

        repeated = self.collect('retry-after-prompt-update', generator=no_generation)
        self.assertTrue(repeated['reused'])
        self.assertEqual(len(calls), 4)
        self.assertEqual(file_bytes(Path(recovered['snapshotPath'])), published_bytes)
        self.assertEqual(file_bytes(old_cache), old_bytes)

    def test_fresh_accepted_old_prompt_guides_are_not_forced_to_regenerate(self):
        self.assertNotEqual(core.PROMPT_VERSION, LEGACY_PROMPT_VERSION)
        with patch.object(core, 'PROMPT_VERSION', LEGACY_PROMPT_VERSION):
            initial = self.collect('accepted-old-prompt-run')
        original_path = Path(initial['snapshotPath'])
        original_bytes = file_bytes(original_path)
        original_records = core.verify_snapshot(original_path, fixtures.SITE)[1:]

        def no_generation(*_):
            self.fail('A prompt update must not force a fresh accepted guide to regenerate')

        with patch.object(core, 'credential', side_effect=no_generation):
            result = self.collect('next-prompt-run', fixtures.NOW + timedelta(hours=1), no_generation)
        self.assertFalse(result['reused'])
        self.assertEqual(result['openaiRequestCount'], 0)
        self.assertEqual(result['generatedGameIds'], [])
        self.assertEqual(core.verify_snapshot(Path(result['snapshotPath']), fixtures.SITE)[1:], original_records)
        self.assertEqual(file_bytes(original_path), original_bytes)


class PromptRequestTests(unittest.TestCase):
    def test_home_and_away_requests_keep_canonical_context_sources_and_call_limits(self):
        cases = [
            (fixtures.SITE, fixtures.GAME),
            ({**fixtures.SITE, 'slug': 'broncos', 'name': 'Broncos', 'city': 'Denver',
              'abbreviation': 'DEN', 'source_domains': ['denverbroncos.com', 'nfl.com']},
             {**fixtures.GAME, 'team': 'broncos', 'homeAway': 'home',
              'opponent': 'Kansas City Chiefs', 'venue': 'Empower Field at Mile High'}),
        ]
        for site, game in cases:
            with self.subTest(team=site['slug'], homeAway=game['homeAway']):
                calls = []
                research_text = 'Current official stadium entry and transportation guidance with citations.'
                draft = fixtures.draft_fixture()
                draft['summary']['text'] = ('Use the stadium entry policy when packing and the official '
                                            'transportation guidance to plan arrival.')

                def provider(payload, key):
                    self.assertEqual(key, 'test-secret')
                    calls.append(payload)
                    return (fixtures.response_fixture(research_text, True) if 'tools' in payload
                            else fixtures.response_fixture(json.dumps(draft)))

                config = core.load_config()
                with tempfile.TemporaryDirectory() as folder:
                    guide, watch, evidence = core.generate(Path(folder), 'test-secret', config,
                                                           game, site, fixtures.NOW, provider)
                self.assertEqual(len(calls), 2)
                research, writing = calls
                for payload in calls:
                    self.assertEqual(payload['model'], config['model'])
                    self.assertIs(payload['store'], False)
                    self.assertIn(json.dumps(game, sort_keys=True), payload['input'])
                    self.assertIn(site['city'], payload['input'])
                self.assertEqual(research['tool_choice'], 'required')
                self.assertEqual(research['tools'], [{'type': 'web_search'}])
                self.assertEqual(research['max_tool_calls'], config['max_search_calls_per_game'])
                self.assertEqual(research['max_output_tokens'], 6500)
                self.assertNotIn('tools', writing)
                self.assertEqual(writing['max_output_tokens'], 8500)
                self.assertIn(research_text, writing['input'])
                self.assertIn(json.dumps(fixtures.SOURCES), writing['input'])
                self.assertEqual(writing['text']['format']['schema'],
                                 core.draft_schema_for_sources(fixtures.SOURCES))
                self.assertEqual(evidence['openaiRequestCount'], 2)
                self.assertEqual(evidence['promptVersion'], core.PROMPT_VERSION)
                self.assertEqual(guide['game']['homeAway'], game['homeAway'])
                self.assertEqual(watch['team'], site['slug'])


if __name__ == '__main__':
    unittest.main()
