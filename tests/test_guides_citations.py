"""Citation-contract regressions; provider responses are mocked and cached locally."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    'guide_citation_fixtures', Path(__file__).with_name('test_guides_core.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
core = fixtures.core


def evidence_schemas(schema):
    """Visit every fact, including nullable summary and national broadcast."""
    if isinstance(schema, dict):
        if 'sourceIds' in schema.get('properties', {}):
            yield schema['properties']['sourceIds']
        for value in schema.values():
            yield from evidence_schemas(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from evidence_schemas(value)


class CitationSchemaTests(unittest.TestCase):
    def test_every_fact_is_bound_to_the_actual_retrieved_registry(self):
        original = deepcopy(core.DRAFT_SCHEMA)
        sources = deepcopy(fixtures.SOURCES)
        schema = core.draft_schema_for_sources(sources)
        facts = list(evidence_schemas(schema))
        self.assertEqual(len(facts), 11)
        for fact_schema in facts:
            with self.subTest(schema=fact_schema):
                self.assertEqual(set(fact_schema['items']['enum']), {'S1', 'S2'})
                self.assertEqual(fact_schema['minItems'], 1)
                self.assertEqual(fact_schema['maxItems'], 4)
                core.validate_schema(['S1'], fact_schema)
                for invalid in ([], ['S99'], ['https://example.com'], ['S1'] * 5):
                    with self.assertRaises(ValueError):
                        core.validate_schema(invalid, fact_schema)
        official = schema['properties']['officialGameSourceId']
        for valid in ('S1', 'S2', None):
            core.validate_schema(valid, official)
        for invalid in ('S99', 'https://example.com'):
            with self.assertRaises(ValueError):
                core.validate_schema(invalid, official)
        self.assertEqual(core.DRAFT_SCHEMA, original)
        self.assertEqual(sources, fixtures.SOURCES)
        facts[0]['items']['enum'].append('S999')
        self.assertEqual(core.DRAFT_SCHEMA, original)
        next_schema = core.draft_schema_for_sources(fixtures.SOURCES)
        self.assertTrue(all('S999' not in fact['items']['enum']
                            for fact in evidence_schemas(next_schema)))

    def test_local_schema_validator_enforces_array_minimum_and_maximum(self):
        schema = {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 4}
        core.validate_schema(['S1'], schema)
        core.validate_schema(['S1', 'S2', 'S3', 'S4'], schema)
        for invalid in ([], ['S1', 'S2', 'S3', 'S4', 'S5']):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                core.validate_schema(invalid, schema)

    def test_unsourced_and_duplicate_claims_still_fail_with_the_field(self):
        for ids in ([], ['S99'], ['S1', 'S1']):
            draft = fixtures.draft_fixture()
            draft['stadiumTips'][0]['sourceIds'] = ids
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, r'stadiumTips\[0\]'):
                core.make_records(draft, fixtures.SOURCES, fixtures.GAME, fixtures.SITE, fixtures.NOW)
        draft = fixtures.draft_fixture()
        draft['national'] = fixtures.fact({'text': ''}, event=True)
        draft['national']['sourceIds'] = []
        with self.assertRaisesRegex(ValueError, 'national'):
            core.make_records(draft, fixtures.SOURCES, fixtures.GAME, fixtures.SITE, fixtures.NOW)


class CitationCacheTests(unittest.TestCase):
    def generate(self, folder, provider):
        return core.generate(folder, 'test-secret', core.load_config(), fixtures.GAME,
                             fixtures.SITE, fixtures.NOW, provider)

    def test_existing_failed_writer_preserves_evidence_and_reuses_research(self):
        initial_calls = []
        invalid = fixtures.draft_fixture()
        invalid['stadiumTips'][0]['sourceIds'] = []

        def failing_provider(payload, key):
            initial_calls.append(payload)
            return (fixtures.response_fixture('Source-cited stadium and operator guidance.', True)
                    if 'tools' in payload else fixtures.response_fixture(json.dumps(invalid)))

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with self.assertRaises(ValueError):
                self.generate(directory, failing_provider)
            self.assertEqual(len(initial_calls), 2)
            # Simulate the previous release's cached failed writing attempt.
            for suffix in ('request', 'response'):
                (directory / f'{core.WRITING_STAGE}-{suffix}.json').rename(
                    directory / f'guide-{suffix}.json')
            preserved = {path.name: path.read_bytes() for path in directory.iterdir()}
            calls = []

            def corrected_provider(payload, key):
                self.assertNotIn('tools', payload, 'Cached research must not be purchased again')
                calls.append(payload)
                return fixtures.response_fixture(json.dumps(fixtures.draft_fixture()))

            guide, watch, evidence = self.generate(directory, corrected_provider)
            self.assertEqual(len(calls), 1)
            self.assertEqual(evidence['openaiRequestCount'], 1)
            self.assertEqual(watch['national'], 'TBD')
            self.assertTrue((directory / f'{core.WRITING_STAGE}-response.json').is_file())
            repeated = self.generate(directory, corrected_provider)
            self.assertEqual(len(calls), 1)
            self.assertEqual(repeated[2]['openaiRequestCount'], 0)
            self.assertEqual((guide, watch), repeated[:2])
            for filename, data in preserved.items():
                self.assertEqual((directory / filename).read_bytes(), data)

    def test_fresh_generation_uses_two_calls_with_the_constrained_schema(self):
        calls = []

        def provider(payload, key):
            calls.append(payload)
            if 'tools' in payload:
                return fixtures.response_fixture('Source-cited stadium and operator guidance.', True)
            self.assertEqual(payload['text']['format']['schema'],
                             core.draft_schema_for_sources(fixtures.SOURCES))
            return fixtures.response_fixture(json.dumps(fixtures.draft_fixture()))

        with tempfile.TemporaryDirectory() as folder:
            _, _, evidence = self.generate(Path(folder), provider)
            self.assertEqual(len(calls), 2)
            self.assertEqual(evidence['openaiRequestCount'], 2)

    def test_invalid_response_is_retained_without_an_automatic_repair_loop(self):
        calls = []
        invalid = fixtures.draft_fixture()
        invalid['stadiumTips'][0]['sourceIds'] = ['S99']

        def provider(payload, key):
            calls.append(payload)
            return (fixtures.response_fixture('Source-cited stadium and operator guidance.', True)
                    if 'tools' in payload else fixtures.response_fixture(json.dumps(invalid)))

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, r'stadiumTips\[0\]'):
                    self.generate(directory, provider)
                self.assertEqual(len(calls), 2)
                self.assertFalse((directory / 'evidence.json').exists())
                self.assertTrue((directory / f'{core.WRITING_STAGE}-response.json').is_file())


if __name__ == '__main__':
    unittest.main()
