"""Offline proof of owner-supplied credits from task Variable to immutable hero."""
import base64
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_daily_news import news, hook, ssh, png, article
import fan_zone_photo_credits as credits

EMPTY = {'schema_version': 1, 'assets': {}, 'sha256': {}}
CATALOG = {'schema_version': 1, 'assets': {
    '2260614489': {'caption': 'A player celebrates in February.', 'credit': 'Photo by Thearon W. Henderson/Getty Images', 'eventDate': '2026-02-08'},
    '2291753684': {'caption': 'A preseason handoff.', 'credit': 'Photo by Justin Ford/Getty Images', 'eventDate': '2026-08-23'},
}, 'sha256': {}}


class PhotoCreditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        self.source = self.runtime / 'source'
        self.source.mkdir()
        for folder in ('photos', 'assets', 'days', 'releases'):
            (self.runtime / folder).mkdir()

    def test_exact_id_hash_and_metadata_identification_agree(self):
        checksum = news.digest(png(1))
        catalog = copy.deepcopy(CATALOG)
        catalog['sha256'][checksum] = '2260614489'
        resolved = credits.resolve_credit(catalog, filename='gettyimages-2260614489.jpg', sha256=checksum,
                                          metadata={'gettyAssetId': '2260614489'})
        self.assertEqual(resolved['gettyAssetId'], '2260614489')
        self.assertEqual(resolved['credit'], CATALOG['assets']['2260614489']['credit'])
        self.assertIsNone(credits.resolve_credit(CATALOG, filename='Jaxon-Smith-Njigba.jpg'))
        self.assertIsNone(credits.resolve_credit(CATALOG, filename='122606144899.jpg'))
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            credits.resolve_credit(catalog, filename='gettyimages-2291753684.jpg', sha256=checksum)
        with self.assertRaises(ValueError):
            credits.resolve_credit(CATALOG, metadata={'gettyAssetId': '9999999999'})

    def test_caption_keeps_exact_credit_and_marks_older_event_as_file_photo(self):
        meta = credits.resolve_credit(CATALOG, filename='2260614489.jpg')
        caption = credits.format_caption(meta, '2026-09-12')
        self.assertEqual(caption, 'File photo. A player celebrates in February. Photo by Thearon W. Henderson/Getty Images')
        self.assertNotIn('File photo', credits.format_caption(meta, '2026-02-08'))
        self.assertNotIn('Photo: Photo', caption)

    def test_selected_hero_and_publication_preserve_provenance_without_source_calls(self):
        data = png(1)
        photo = self.runtime / 'photos/gettyimages-2260614489.png'
        photo.write_bytes(data)
        (self.source / '.env').write_text('OPENAI_API_KEY=offline-fixture-only\n')
        with patch.object(news, 'run', return_value='a' * 40), patch.object(news, 'call_openai', side_effect=AssertionError('No API call')):
            receipt = news.collect('fixture', '2026-09-12', self.source, self.runtime,
                catalog_fn=lambda source: ('a' * 40, {'authored': [], 'visible': []}),
                generate_fn=lambda *args: (article('2026-09-12'), {'requestsThisAttempt': 0}),
                current_time=datetime(2026, 9, 12, 15, tzinfo=timezone.utc), photo_credits=CATALOG)
        final = news.read_json(self.runtime / 'current/articles.json')['articles'][0]
        hero = final['hero']
        self.assertEqual(hero['gettyAssetId'], '2260614489')
        self.assertEqual(hero['sha256'], news.digest(data))
        self.assertEqual(hero['credit'], CATALOG['assets']['2260614489']['credit'])
        self.assertTrue(hero['caption'].startswith('File photo.'))
        self.assertEqual((self.runtime / 'current/images' / Path(hero['src']).name).read_bytes(), data)
        self.assertEqual(receipt['openaiRequestCount'], 0)
        self.assertEqual(news.read_json(self.runtime / 'photo-credits.json'), credits.validate_catalog(CATALOG))
        prior = (self.runtime / 'current').resolve()
        with patch.object(news, 'generate', side_effect=AssertionError('No regeneration')):
            again = news.collect('repeat', '2026-09-12', self.source, self.runtime,
                current_time=datetime(2026, 9, 12, 15, tzinfo=timezone.utc), photo_credits=EMPTY)
        self.assertEqual(again['openaiRequestCount'], 0)
        self.assertEqual((self.runtime / 'current').resolve(), prior)
        self.assertEqual(news.read_json(self.runtime / 'current/articles.json')['articles'][0], final)
        self.assertEqual(news.photo_credit_catalog(self.runtime), EMPTY)

    def test_unknown_photo_uses_neutral_fallback_and_empty_catalog_is_respected(self):
        (self.runtime / 'photos/gettyimages-2260614489.png').write_bytes(png(1))
        for catalog in (EMPTY, {'schema_version': 1, 'assets': {}, 'sha256': {}}):
            hero, notes = news.select_photo(self.runtime, self.source, [], photo_credits=catalog)
            self.assertEqual(hero, news.FALLBACK)
            self.assertIn('no approved asset match', notes[0])
        (self.runtime / 'photos/gettyimages-2260614489.png').rename(self.runtime / 'photos/unknown.png')
        hero, _ = news.select_photo(self.runtime, self.source, [], photo_credits=CATALOG)
        self.assertEqual(hero, news.FALLBACK)

    def test_duplicates_with_conflicting_ids_are_not_eligible(self):
        for asset_id in CATALOG['assets']:
            (self.runtime / f'photos/{asset_id}.png').write_bytes(png(1))
        pool, notes = news.photo_pool(self.runtime, photo_credits=CATALOG)
        self.assertFalse(pool)
        self.assertIn('Conflicting credits', notes[0])

    def test_explicit_manual_credit_remains_usable_without_double_prefix(self):
        (self.runtime / 'photos/manual.png').write_bytes(png(1))
        news.atomic_json(self.runtime / 'photos/metadata.json', {'manual.png': {
            'caption': 'Owner supplied caption.', 'credit': 'Photo by Another Photographer', 'alt': 'Owner supplied alternative text'}})
        hero, _ = news.select_photo(self.runtime, self.source, [], photo_credits=EMPTY)
        self.assertEqual(hero['caption'], 'Owner supplied caption. Photo by Another Photographer')
        self.assertEqual(hero['alt'], 'Owner supplied alternative text')
        self.assertNotIn('gettyAssetId', hero)

    def test_unknown_fields_invalid_dates_hashes_and_markup_are_rejected(self):
        mutations = [lambda c: c.update(extra=True),
            lambda c: c['assets']['2260614489'].update(extra=True),
            lambda c: c['assets']['2260614489'].update(eventDate='2026-02-30'),
            lambda c: c['assets']['2260614489'].update(credit='<script>bad</script>'),
            lambda c: c['sha256'].update({'a' * 64: '9999999999'}),
            lambda c: c['assets']['2260614489'].update(caption='x' * 2501)]
        for mutate in mutations:
            value = copy.deepcopy(CATALOG)
            mutate(value)
            with self.assertRaises(ValueError):
                credits.validate_catalog(value)

    def test_task_reads_variable_each_execution_with_checked_in_missing_fallback(self):
        sdk = types.ModuleType('airflow.sdk')
        sdk.Variable = MagicMock()
        sdk.Variable.get.side_effect = [CATALOG, EMPTY, None]
        with patch.dict(sys.modules, {'airflow.sdk': sdk}), patch.object(credits, 'load_catalog', return_value=CATALOG) as fallback:
            self.assertEqual(credits.read_task_catalog(), credits.validate_catalog(CATALOG))
            self.assertEqual(credits.read_task_catalog(), EMPTY)
            self.assertEqual(credits.read_task_catalog(), CATALOG)
        fallback.assert_called_once_with()
        self.assertEqual(sdk.Variable.get.call_count, 3)
        sdk.Variable.get.assert_called_with('fan_zone_photo_credits', default=None, deserialize_json=True)

    def test_hook_transport_to_real_host_keeps_existing_size_and_field_guards(self):
        token = hook.encode_news_request('manual', '2026-09-12', photo_credits=CATALOG)
        args = ssh.command_arguments('refresh ' + token)
        self.assertEqual(args[:2], ['--run-id=manual', '--publication-day=2026-09-12'])
        self.assertEqual(json.loads(args[2].split('=', 1)[1]), credits.validate_catalog(CATALOG))
        request = json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))
        request['unexpected'] = True
        corrupt = base64.urlsafe_b64encode(json.dumps(request).encode()).decode().rstrip('=')
        with self.assertRaises(ValueError):
            ssh.command_arguments('refresh ' + corrupt)
        with patch('fan_zone_tasks.MAX_REQUEST_BYTES', 100):
            with self.assertRaises(ValueError):
                hook.encode_news_request('manual', '2026-09-12', photo_credits=CATALOG)
        request.pop('unexpected')
        request['photoCredits']['bad'] = True
        corrupt = base64.urlsafe_b64encode(json.dumps(request).encode()).decode().rstrip('=')
        with self.assertRaises(ValueError):
            ssh.command_arguments('refresh ' + corrupt)


if __name__ == '__main__':
    unittest.main()
