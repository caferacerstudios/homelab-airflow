"""Optional official links cannot discard useful guides or weaken publication checks."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'guide_official_link_fixtures', Path(__file__).with_name('test_guides_core.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
core = fixtures.core


class OfficialLinkEligibilityTests(unittest.TestCase):
    def test_optional_ineligible_source_is_omitted_without_changing_facts(self):
        # The opponent's directions remain useful evidence for this away game.
        draft = fixtures.draft_fixture()
        draft['officialGameSourceId'] = 'S2'
        original = deepcopy(draft)
        sources = deepcopy(fixtures.SOURCES)
        core.validate_schema(draft, core.draft_schema_for_sources(sources))
        with self.assertRaisesRegex(ValueError, 'registered team or NFL'):
            core.make_records(draft, sources, fixtures.GAME, fixtures.SITE, fixtures.NOW)

        candidate, omitted = core.supported_draft(draft, sources, fixtures.GAME, fixtures.SITE)
        self.assertEqual(candidate, {**original, 'officialGameSourceId': None})
        self.assertEqual(draft, original)
        self.assertEqual(sources, fixtures.SOURCES)
        self.assertEqual(len(omitted), 1)
        self.assertEqual(omitted[0]['field'], 'officialGameSourceId')
        self.assertEqual(omitted[0]['sourceIds'], ['S2'])
        self.assertTrue(omitted[0]['reason'])
        guide, watch, _ = core.make_records(candidate, sources, fixtures.GAME,
                                           fixtures.SITE, fixtures.NOW)
        self.assertIsNone(watch['officialGameUrl'])
        self.assertEqual(guide['transportation'][0]['sourceUrl'], sources[1]['url'])
        self.assertIn(sources[1]['url'], {source['url'] for source in watch['sources']})
        core.validate_record_pair(guide, watch, fixtures.GAME, fixtures.SITE)

    def test_registered_team_and_nfl_domain_boundaries(self):
        site = {**fixtures.SITE, 'slug': 'broncos', 'city': 'Denver', 'name': 'Broncos',
                'source_domains': ['denverbroncos.com', 'nfl.com']}
        game = {**fixtures.GAME, 'team': 'broncos'}
        cases = {
            'https://denverbroncos.com/game-day': True,
            'https://www.denverbroncos.com/game-day': True,
            'https://nfl.com/games/details': True,
            'https://www.nfl.com/games/details': True,
            'https://seahawks.com/game-day': False,
            'https://www.empowerfieldatmilehigh.com/events/detail': False,
            'https://denverbroncos.com.example.org/game-day': False,
            'https://notdenverbroncos.com/game-day': False,
            'https://nfl.com.example.org/game-day': False,
        }
        for url, allowed in cases.items():
            with self.subTest(url=url):
                draft = fixtures.draft_fixture()
                draft['officialGameSourceId'] = 'S3'
                sources = [*deepcopy(fixtures.SOURCES), {'id': 'S3', 'name': 'Event page', 'url': url}]
                self.assertEqual(core.official_game_url_allowed(url, site), allowed)
                candidate, omitted = core.supported_draft(draft, sources, game, site)
                self.assertEqual(candidate['officialGameSourceId'], 'S3' if allowed else None)
                self.assertEqual(len(omitted), 0 if allowed else 1)
                guide, watch, _ = core.make_records(candidate, sources, game, site, fixtures.NOW)
                self.assertEqual(watch['officialGameUrl'], url if allowed else None)
                core.validate_record_pair(guide, watch, game, site)

    def test_missing_optional_link_needs_no_omission_or_replacement(self):
        draft = fixtures.draft_fixture()
        candidate, omitted = core.supported_draft(draft, fixtures.SOURCES, fixtures.GAME, fixtures.SITE)
        self.assertEqual(candidate, draft)
        self.assertEqual(omitted, [])

    def test_unknown_ids_and_malformed_drafts_remain_hard_failures(self):
        for value in ('S99', 'https://www.nfl.com/game', '', 0, False, ['S2']):
            with self.subTest(value=value):
                draft = fixtures.draft_fixture()
                draft['officialGameSourceId'] = value
                with self.assertRaises(ValueError):
                    core.supported_draft(draft, fixtures.SOURCES, fixtures.GAME, fixtures.SITE)
        draft = fixtures.draft_fixture()
        draft['officialGameSourceId'] = 'S2'
        draft['stadiumTips'][0]['details'] = '<b>Untrusted markup</b>'
        with self.assertRaises(ValueError):
            core.supported_draft(draft, fixtures.SOURCES, fixtures.GAME, fixtures.SITE)

    def test_unsafe_urls_and_duplicate_source_ids_are_not_harmless_omissions(self):
        invalid_urls = ('http://www.nfl.com/game', 'https://user:password@www.nfl.com/game',
                        'https://www.nfl.com:8443/game', 'https://127.0.0.1/game',
                        'https://www.nfl.com/unsafe path', 'https://www.nfl.com\\@example.org/game')
        draft = fixtures.draft_fixture()
        draft['officialGameSourceId'] = 'S3'
        for url in invalid_urls:
            with self.subTest(url=url):
                sources = [*deepcopy(fixtures.SOURCES), {'id': 'S3', 'name': 'Event page', 'url': url}]
                self.assertFalse(core.official_game_url_allowed(url, fixtures.SITE))
                with self.assertRaises(ValueError):
                    core.supported_draft(draft, sources, fixtures.GAME, fixtures.SITE)
        sources = [*deepcopy(fixtures.SOURCES), deepcopy(fixtures.SOURCES[0])]
        draft['officialGameSourceId'] = 'S1'
        with self.assertRaises(ValueError):
            core.supported_draft(draft, sources, fixtures.GAME, fixtures.SITE)

    def test_published_record_rejects_retrieved_but_ineligible_official_link(self):
        guide, watch, _ = core.make_records(fixtures.draft_fixture(), fixtures.SOURCES,
                                           fixtures.GAME, fixtures.SITE, fixtures.NOW)
        watch['officialGameUrl'] = fixtures.SOURCES[1]['url']
        with self.assertRaisesRegex(ValueError, 'registered team or NFL'):
            core.validate_record_pair(guide, watch, fixtures.GAME, fixtures.SITE)


class OfficialLinkCacheTests(unittest.TestCase):
    def test_failed_cached_candidate_recovers_without_api_calls_or_evidence_rewrite(self):
        draft = fixtures.draft_fixture()
        draft['officialGameSourceId'] = 'S2'
        calls = []

        def provider(payload, key):
            calls.append(payload)
            return (fixtures.response_fixture('Cited stadium and transport guidance.', True)
                    if 'tools' in payload else fixtures.response_fixture(json.dumps(draft)))

        def generate(directory, call):
            return core.generate(directory, 'test-secret', core.load_config(), fixtures.GAME,
                                 fixtures.SITE, fixtures.NOW, call)

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            # Earlier validation accepted the candidate until strict record construction.
            with patch.object(core, 'supported_draft', side_effect=lambda d, *_: (deepcopy(d), [])):
                with self.assertRaisesRegex(ValueError, 'registered team or NFL'):
                    generate(directory, provider)
            self.assertEqual(len(calls), 2)
            originals = {path.name: path.read_bytes() for path in directory.iterdir()
                         if path.name.endswith(('-request.json', '-response.json'))}
            self.assertEqual(len(originals), 4)

            def no_api(*_):
                self.fail('This validation-only recovery must reuse both saved responses')

            guide, watch, evidence = generate(directory, no_api)
            self.assertIsNone(watch['officialGameUrl'])
            self.assertEqual(evidence['openaiRequestCount'], 0)
            self.assertEqual(evidence['promptVersion'], 'fan-zone-guides-v3-practical')
            self.assertEqual(evidence['writingVersion'], 'guide-citations-v2')
            self.assertEqual(evidence['validationVersion'], 'guide-practical-listings-v1')
            self.assertEqual(evidence['omittedFacts'][0]['field'], 'officialGameSourceId')
            self.assertEqual(evidence['omittedFacts'][0]['sourceIds'], ['S2'])
            validation = json.loads((directory / 'validation.json').read_text())
            self.assertEqual(validation['omittedFacts'], evidence['omittedFacts'])
            self.assertEqual(validation['validationVersion'], evidence['validationVersion'])
            self.assertEqual(validation['event'], fixtures.GAME)
            repeated = generate(directory, no_api)
            self.assertEqual(repeated[:2], (guide, watch))
            self.assertEqual(repeated[2]['openaiRequestCount'], 0)
            for name, data in originals.items():
                self.assertEqual((directory / name).read_bytes(), data)


class OfficialLinkPublicationTests(unittest.TestCase):
    setUp = fixtures.PublicationTests.setUp
    collect = fixtures.PublicationTests.collect

    def test_insufficient_content_retains_previous_publication_and_omission_audit(self):
        self.collect()
        before = (self.runtime / 'current').resolve()
        original_files = {path.name: path.read_bytes() for path in before.iterdir()}
        draft = fixtures.draft_fixture()
        draft['officialGameSourceId'] = 'S2'
        draft['transportation'] = []
        draft['stadiumTips'] = []

        def provider(payload, key):
            return (fixtures.response_fixture('Cited stadium and transport guidance.', True)
                    if 'tools' in payload else fixtures.response_fixture(json.dumps(draft)))

        def generator(directory, key, config, game, site, now):
            return core.generate(directory, key, config, game, site, now, provider)

        with self.assertRaisesRegex(ValueError, 'at least two useful'):
            self.collect('insufficient-official-link-guide', fixtures.NOW + timedelta(days=2), generator)
        self.assertEqual((self.runtime / 'current').resolve(), before)
        self.assertEqual({path.name: path.read_bytes() for path in before.iterdir()}, original_files)
        audit_paths = list((self.runtime / 'evidence').glob('*/validation.json'))
        self.assertEqual(len(audit_paths), 1)
        audit = json.loads(audit_paths[0].read_text())
        self.assertEqual([item['field'] for item in audit['omittedFacts']], ['officialGameSourceId'])
        self.assertFalse((audit_paths[0].parent / 'evidence.json').exists())
        self.assertTrue((audit_paths[0].parent / f'{core.WRITING_STAGE}-response.json').exists())

    def test_snapshot_verification_checks_official_domain_beyond_checksums(self):
        self.collect()
        snapshot = (self.runtime / 'current').resolve()
        watch_path = snapshot / 'watch-guide.json'
        watch = json.loads(watch_path.read_text())
        watch['games'][0]['officialGameUrl'] = fixtures.SOURCES[1]['url']
        core.atomic_json(watch_path, watch)
        manifest_path = snapshot / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['watch-guide.json'] = core.file_hash(watch_path)
        core.atomic_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, 'registered team or NFL'):
            core.verify_snapshot(snapshot, fixtures.SITE)


if __name__ == '__main__':
    unittest.main()
