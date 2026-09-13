"""General fan options survive while mismatched event claims remain excluded."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'guide_event_evidence_fixtures', Path(__file__).with_name('test_guides_core.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
core = fixtures.core


def alert(*, event=False):
    return fixtures.fact({'title': 'Entry notice', 'text': 'Review the stadium entry notice.',
                          'severity': 'info'}, event=event)


def event_sections():
    return {
        'alerts': alert(),
        'timeline': fixtures.fact({'time': 'Before arrival', 'event': 'Entry planning',
                                   'details': 'Review the published entry arrangements.'}),
        'tailgates': fixtures.fact({'name': 'Pregame gathering', 'location': 'Stadium plaza',
                                    'startTime': 'Before the game', 'description': 'Organizer gathering.',
                                    'ageRestriction': None, 'official': True}),
        'watchParties': fixtures.fact({'name': 'Watch gathering', 'location': 'Home market',
                                      'startTime': 'Before the game', 'description': 'Organizer gathering.',
                                      'specials': [], 'ageRestriction': None}),
        'localTv': fixtures.fact({'name': 'Local broadcaster', 'note': None}),
        'streams': fixtures.fact({'name': 'Streaming provider', 'note': None}),
        'national': fixtures.fact({'text': 'National broadcaster'}),
    }


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class SupportedDraftTests(unittest.TestCase):
    def filter(self, draft, sources=fixtures.SOURCES, game=fixtures.GAME):
        return core.supported_draft(draft, sources, game, fixtures.SITE)

    def test_wrong_date_alert_is_omitted_and_supported_fact_is_unchanged(self):
        draft = fixtures.draft_fixture()
        draft['alerts'] = [alert(event=True), alert(event=True)]
        draft['alerts'][1]['eventDate'] = '2026-09-21'
        original = deepcopy(draft)
        with self.assertRaisesRegex(ValueError, r'alerts\[1\].*exact canonical game date'):
            core.make_records(draft, fixtures.SOURCES, fixtures.GAME, fixtures.SITE, fixtures.NOW)

        filtered, omitted = self.filter(draft)
        self.assertEqual(filtered, {**original, 'alerts': original['alerts'][:1]})
        self.assertEqual(draft, original)
        self.assertEqual(len(omitted), 1)
        self.assertEqual(omitted[0]['field'], 'alerts[1]')
        for field in ('sourceIds', 'scope', 'eventDate'):
            self.assertEqual(omitted[0][field], original['alerts'][1][field])
        self.assertTrue(omitted[0]['reason'])
        guide, _, _ = core.make_records(filtered, fixtures.SOURCES, fixtures.GAME, fixtures.SITE, fixtures.NOW)
        self.assertEqual(len(guide['alerts']), 1)
        self.assertEqual(guide['alerts'][0]['text'], original['alerts'][0]['text'])
        filtered['alerts'][0]['sourceIds'].append('S2')
        self.assertEqual(draft, original, 'Filtering must return independent nested objects')

    def test_generic_evidence_never_establishes_broadcasts(self):
        for field in ('localTv', 'streams', 'national'):
            row = event_sections()[field]
            with self.subTest(field=field):
                draft = fixtures.draft_fixture()
                draft[field] = row if field == 'national' else [row]
                filtered, omitted = self.filter(draft)
                self.assertEqual(filtered[field], None if field == 'national' else [])
                self.assertEqual([item['field'] for item in omitted],
                                 [field if field == 'national' else field + '[0]'])
                guide, watch, _ = core.make_records(filtered, fixtures.SOURCES, fixtures.GAME,
                                                    fixtures.SITE, fixtures.NOW)
                self.assertEqual(watch['national'], 'TBD')
                self.assertEqual(len(guide['stadiumTips']), 1)

    def test_general_options_keep_links_and_clear_labels_without_relabeling_evidence(self):
        draft = fixtures.draft_fixture()
        labels = {'alerts': ('title', 'General guidance: '),
                  'timeline': ('time', 'Typical: '),
                  'tailgates': ('name', 'Recurring option: '),
                  'watchParties': ('name', 'Viewing option: ')}
        for field in labels:
            draft[field] = [event_sections()[field]]
        draft['alerts'][0]['severity'] = 'warning'
        before = deepcopy(draft)
        filtered, omitted = self.filter(draft)
        self.assertEqual(omitted, [])
        self.assertEqual(filtered, before)
        guide, watch, evidence = core.make_records(filtered, fixtures.SOURCES, fixtures.GAME,
                                                   fixtures.SITE, fixtures.NOW)
        for field, (label, prefix) in labels.items():
            with self.subTest(field=field):
                self.assertEqual(len(guide[field]), 1)
                self.assertEqual(guide[field][0][label], prefix + before[field][0][label])
                self.assertEqual(guide[field][0]['sourceUrl'], fixtures.SOURCES[0]['url'])
                for key, value in before[field][0].items():
                    if key not in (*core.EVIDENCE, label):
                        self.assertEqual(guide[field][0][key],
                                         'info' if field == 'alerts' and key == 'severity' else value)
                claim = next(row for row in evidence['claims'] if row['field'] == field)
                self.assertEqual(claim['scope'], 'standing-policy')
                self.assertIsNone(claim['eventDate'])
        self.assertEqual(draft, before)
        core.validate_record_pair(guide, watch, fixtures.GAME, fixtures.SITE)

    def test_sounder_and_wrong_date_broadcasts_are_omitted_without_inference(self):
        draft = fixtures.draft_fixture()
        generic_sounder = fixtures.fact({'name': 'Sounder', 'recommendation': 'Check the operator calendar.',
                                         'details': 'A general operator page lists service information.'}, ['S2'])
        draft['transportation'].append(generic_sounder)
        draft['localTv'] = [fixtures.fact({'name': 'Unconfirmed local station', 'note': None}, event=True)]
        draft['localTv'][0]['eventDate'] = '2026-09-21'
        before = deepcopy(draft)
        filtered, omitted = self.filter(draft)
        self.assertEqual(filtered['transportation'], before['transportation'][:1])
        self.assertEqual(filtered['localTv'], [])
        self.assertEqual({item['field'] for item in omitted}, {'transportation[1]', 'localTv[0]'})
        self.assertEqual(draft, before)
        self.assertEqual(filtered['unknowns'], before['unknowns'])

    def test_wrong_date_events_and_dated_standing_policies_are_not_rewritten(self):
        for field in ('summary', 'parking', 'stadiumTips', 'alerts', 'timeline', 'tailgates', 'watchParties'):
            for scope, date in (('event-specific', '2026-09-21'),
                                ('standing-policy', fixtures.GAME['date'])):
                with self.subTest(field=field, scope=scope):
                    draft = fixtures.draft_fixture()
                    if field == 'parking':
                        draft[field] = [fixtures.fact({'name': 'Stadium parking', 'details': 'Published parking policy.'})]
                    elif field in ('alerts', 'timeline', 'tailgates', 'watchParties'):
                        draft[field] = [event_sections()[field]]
                    row = draft[field] if field == 'summary' else draft[field][0]
                    row.update(scope=scope, eventDate=date)
                    before = deepcopy(draft)
                    filtered, omitted = self.filter(draft)
                    self.assertEqual(filtered[field], None if field == 'summary' else [])
                    self.assertEqual(omitted[0]['scope'], scope)
                    self.assertEqual(omitted[0]['eventDate'], date)
                    self.assertEqual(draft, before)

    def test_fully_supported_draft_and_records_are_byte_identical(self):
        draft = fixtures.draft_fixture()
        for field, row in event_sections().items():
            row.update(scope='event-specific', eventDate=fixtures.GAME['date'])
            draft[field] = row if field == 'national' else [row]
        draft['alerts'][0]['severity'] = 'warning'
        before = encoded(draft)
        expected = core.make_records(draft, fixtures.SOURCES, fixtures.GAME, fixtures.SITE, fixtures.NOW)
        filtered, omitted = self.filter(draft)
        actual = core.make_records(filtered, fixtures.SOURCES, fixtures.GAME, fixtures.SITE, fixtures.NOW)
        self.assertEqual(omitted, [])
        self.assertEqual(encoded(filtered), before)
        self.assertEqual(encoded(draft), before)
        self.assertEqual(encoded(actual), encoded(expected))
        for field, label in (('alerts', 'title'), ('timeline', 'time'),
                             ('tailgates', 'name'), ('watchParties', 'name')):
            self.assertEqual(actual[0][field][0][label], draft[field][0][label])
        self.assertEqual(actual[0]['alerts'][0]['severity'], 'warning')

    def test_bad_citations_or_prose_still_fail_even_for_an_unsupported_event(self):
        mutations = [
            {'sourceIds': []}, {'sourceIds': ['S99']}, {'sourceIds': ['S1', 'S1']},
            {'text': 'Read https://invented.example/details'}, {'text': '<b>Entry policy</b>'},
            {'text': ''}, {'text': 123}, {'inventedField': 'Unrecognized'},
        ]
        for change in mutations:
            with self.subTest(change=change):
                draft = fixtures.draft_fixture()
                draft['alerts'] = [{**alert(), **change}]
                with self.assertRaises(ValueError):
                    self.filter(draft)
        sources = deepcopy(fixtures.SOURCES)
        sources[0]['url'] = 'https://127.0.0.1/private'
        draft = fixtures.draft_fixture()
        draft['alerts'] = [alert()]
        with self.assertRaises(ValueError):
            self.filter(draft, sources=sources)


class EvidenceCacheTests(unittest.TestCase):
    def test_cached_strict_failure_recovers_with_zero_calls_and_original_evidence(self):
        draft = fixtures.draft_fixture()
        draft['alerts'] = [alert(event=True), alert(event=True)]
        draft['alerts'][1]['eventDate'] = '2026-09-21'
        calls = []

        def provider(payload, key):
            calls.append(payload)
            return (fixtures.response_fixture('Cited entry and transportation guidance.', True)
                    if 'tools' in payload else fixtures.response_fixture(json.dumps(draft)))

        def generate(directory, call):
            return core.generate(directory, 'test-secret', core.load_config(), fixtures.GAME,
                                 fixtures.SITE, fixtures.NOW, call)

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            # The prior release sent exactly these requests but rejected the complete draft.
            with patch.object(core, 'supported_draft', side_effect=lambda d, *_: (deepcopy(d), [])):
                with self.assertRaisesRegex(ValueError, r'alerts\[1\].*exact canonical game date'):
                    generate(directory, provider)
            self.assertEqual(len(calls), 2)
            originals = {path.name: path.read_bytes() for path in directory.iterdir()
                         if path.name.endswith(('-request.json', '-response.json'))}
            self.assertEqual(len(originals), 4)

            def no_api(*_):
                self.fail('A validation-only retry must reuse both cached provider responses')

            guide, watch, evidence = generate(directory, no_api)
            self.assertEqual(len(guide['alerts']), 1)
            self.assertEqual(evidence['openaiRequestCount'], 0)
            self.assertEqual(evidence['validationVersion'], 'guide-practical-listings-v1')
            self.assertEqual(evidence['writingVersion'], 'guide-citations-v2')
            validation = json.loads((directory / 'validation.json').read_text())
            self.assertEqual(validation['validationVersion'], evidence['validationVersion'])
            self.assertEqual(validation['event'], fixtures.GAME)
            self.assertEqual(validation['omittedFacts'], evidence['omittedFacts'])
            self.assertEqual([row['field'] for row in validation['omittedFacts']], ['alerts[1]'])
            for name, data in originals.items():
                self.assertEqual((directory / name).read_bytes(), data)
            repeated = generate(directory, no_api)
            self.assertEqual((guide, watch), repeated[:2])
            self.assertEqual(repeated[2]['openaiRequestCount'], 0)


class EvidencePublicationTests(unittest.TestCase):
    setUp = fixtures.PublicationTests.setUp
    collect = fixtures.PublicationTests.collect

    def test_insufficient_supported_content_keeps_last_good_and_omission_audit(self):
        self.collect()
        before = (self.runtime / 'current').resolve()
        original_files = {path.name: path.read_bytes() for path in before.iterdir()}
        draft = fixtures.draft_fixture()
        draft['transportation'] = []
        draft['stadiumTips'] = []
        draft['alerts'] = [alert(event=True)]
        draft['alerts'][0]['eventDate'] = '2026-09-21'

        def provider(payload, key):
            return (fixtures.response_fixture('Cited entry and transportation guidance.', True)
                    if 'tools' in payload else fixtures.response_fixture(json.dumps(draft)))

        def generator(directory, key, config, game, site, now):
            return core.generate(directory, key, config, game, site, now, provider)

        with self.assertRaisesRegex(ValueError, 'at least two useful'):
            self.collect('insufficient-supported-facts', fixtures.NOW + timedelta(days=2), generator)
        self.assertEqual((self.runtime / 'current').resolve(), before)
        self.assertEqual({path.name: path.read_bytes() for path in before.iterdir()}, original_files)
        audit_paths = list((self.runtime / 'evidence').glob('*/validation.json'))
        self.assertEqual(len(audit_paths), 1)
        audit = json.loads(audit_paths[0].read_text())
        self.assertEqual([row['field'] for row in audit['omittedFacts']], ['alerts[0]'])
        self.assertFalse((audit_paths[0].parent / 'evidence.json').exists())
        self.assertTrue((audit_paths[0].parent / f'{core.WRITING_STAGE}-response.json').exists())


if __name__ == '__main__':
    unittest.main()
