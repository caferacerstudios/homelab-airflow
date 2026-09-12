"""Offline owner-supplied credit conversion and durable accepted-news backfill."""
from copy import deepcopy
import csv
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deployment/news'))
sys.path.insert(0, str(ROOT / 'dags'))
import news_core as news
import apply_photo_credits as apply
from convert_photo_credits import convert
from fan_zone_photo_credits import format_caption, load_catalog


DAY = '2026-09-12'
ASSET_ID = '2291753684'


def png():
    return b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\rIHDR' + struct.pack('>II', 1200, 800) + b'\x08\x02\x00\x00\x00' + b'\x00' * 4 + b'\x00\x00\x00\x00IEND' + b'\x00' * 4


def article(day, hero):
    sentence = ('This offline example describes a verified team development and its significance using supplied '
                'reporting without adding unsupported events or changing the original account for the reader. ')
    draft = dict(headline='An offline test of the Seattle editorial photo credits',
                 dek='An offline test description that retains the same reporting, publication dates and source links.',
                 slug='editorial-photo-credit', category='Analysis', tags=['seahawks'],
                 sections=[{'heading': 'Official reporting', 'paragraphs': [{'text': sentence * 8, 'sourceIds': ['S1']}]},
                           {'heading': 'Further evidence', 'paragraphs': [{'text': sentence * 8, 'sourceIds': ['S2']}]}])
    sources = [{'id': 'S1', 'label': 'Official source', 'url': 'https://www.seahawks.com/news/official?a=1&b=2'},
               {'id': 'S2', 'label': 'Second official source', 'url': 'https://www.seahawks.com/news/second'}]
    result = news.make_article(draft, sources, day, datetime(2026, 9, 12, 15, tzinfo=timezone.utc), 'fixture', [])
    result['hero'] = deepcopy(hero)
    return result


class RegistryTests(unittest.TestCase):
    def test_checked_in_registry_matches_owner_csv_photographers_and_dates(self):
        catalog = load_catalog(ROOT / 'config/photo-credits.json')
        expected = {'2291753684': ('Justin Ford', '2026-08-23'),
                    '2237444728': ('Norm Hall', '2025-09-25'),
                    '2260601545': ('Kevin C. Cox', '2026-02-08'),
                    '2260614489': ('Thearon W. Henderson', '2026-02-08'),
                    '2258183135': ('Steph Chambers', '2026-01-25')}
        for asset_id, (photographer, day) in expected.items():
            with self.subTest(asset_id=asset_id):
                value = catalog['assets'][asset_id]
                self.assertEqual(value['credit'], f'Photo by {photographer}/Getty Images')
                self.assertEqual(value['eventDate'], day)
                self.assertNotIn('(Photo by ', value['caption'])
                self.assertIn('File photo.', format_caption(value, DAY))
        self.assertIn('Jacardia Wright #31', catalog['assets'][ASSET_ID]['alt'])
        self.assertNotIn('Damien Martinez', catalog['assets'][ASSET_ID]['alt'])
        self.assertEqual(len(catalog['sha256']), 5)
        wrapper = json.loads((ROOT / 'config/photo-credits-airflow-import.json').read_text())
        self.assertEqual(wrapper, {'fan_zone_photo_credits': catalog})
        serialized = json.dumps(catalog)
        for excluded in ('Laura Whicker', 'Download ID', 'Subscription', '4118678122'):
            self.assertNotIn(excluded, serialized)

    def test_converter_handles_bom_commas_duplicates_and_retains_approved_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'downloads.csv'
            row = {'Item #': ASSET_ID, 'Caption': 'NASHVILLE, TENNESSEE - AUGUST 23: Drew Lock hands off on August 23, 2026. (Photo by Justin Ford/Getty Images)',
                   'Media type': 'Photos', 'Download canceled': '', 'Contributor': 'Getty Images Sport', 'Downloaded by': 'PRIVATE OWNER'}
            with path.open('w', encoding='utf-8-sig', newline='') as output:
                writer = csv.DictWriter(output, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
                writer.writerow(row)
                writer.writerow({**row, 'Item #': '2237444728', 'Download canceled': 'true'})
            existing = load_catalog(ROOT / 'config/photo-credits.json')
            value = convert(path, existing)
            self.assertEqual(value['sha256'], existing['sha256'])
            self.assertEqual(value['assets'][ASSET_ID]['eventDate'], '2026-08-23')
            self.assertEqual(value['assets'][ASSET_ID]['credit'], 'Photo by Justin Ford/Getty Images')
            self.assertNotIn('PRIVATE OWNER', json.dumps(value))
            self.assertNotIn('Contributor', json.dumps(value))
            row['Caption'] = 'A photo with agency Contributor but no photographer credit.'
            with path.open('w', newline='') as output:
                writer = csv.DictWriter(output, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            with self.assertRaisesRegex(ValueError, 'explicit photographer'):
                convert(path)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name) / 'runtime'
        self.runtime.mkdir()
        for name in ('assets', 'photos', 'days', 'releases'):
            (self.runtime / name).mkdir()
        (self.runtime / 'generation.lock').touch()
        self.site = dict(news.site_settings(), news_snapshot_dir=str(self.runtime / 'current'),
                         news_photos_dir=str(self.runtime / 'photos'), website_root=str(Path(temporary.name) / 'website'))
        self.addCleanup(patch.stopall)
        patch.object(news, 'site_settings', side_effect=lambda value=None: self.site).start()
        patch.object(news, 'run', return_value='a' * 40).start()
        self.no_api = patch.object(news, 'call_openai', side_effect=AssertionError('No API calls')).start()
        self.data = png()
        self.checksum = news.digest(self.data)
        self.name = self.checksum + '.png'
        (self.runtime / 'assets' / self.name).write_bytes(self.data)
        hero = dict(src='/images/news/generated/' + self.name, width=1200, height=800,
                    caption='Photo selected from the Seahawks Fan Zone photo collection; illustrative image.',
                    alt='Photo from the Seahawks Fan Zone collection', sha256=self.checksum)
        self.catalog = load_catalog(ROOT / 'config/photo-credits.json')
        self.catalog['sha256'][self.checksum] = ASSET_ID
        self.original = [article(day, hero) for day in ('2026-09-11', DAY)]
        for value in self.original:
            news.atomic_json(self.runtime / 'days' / value['generation']['publicationDay'] / 'article.json', value)
        news.publish(self.runtime, 'original', DAY, '0' * 40, 1, {'requestsThisAttempt': 2}, [], self.site)
        self.snapshot = (self.runtime / 'current').resolve()
        self.original_files = {path.relative_to(self.snapshot): path.read_bytes() for path in self.snapshot.rglob('*') if path.is_file()}

    def go(self, execute=False):
        return apply.apply_credits(self.site, self.catalog, apply=execute)

    def test_check_no_changes_and_apply_only_changes_credit_metadata(self):
        before = {str(path.relative_to(self.runtime)): path.read_bytes() for path in self.runtime.rglob('*') if path.is_file() and not path.is_symlink()}
        checked = self.go()
        self.assertEqual(checked['articlesUpdated'], 2)
        self.assertEqual(checked['unmatchedPhotos'], [])
        self.assertEqual(before, {str(path.relative_to(self.runtime)): path.read_bytes() for path in self.runtime.rglob('*') if path.is_file() and not path.is_symlink()})
        lock_inode = (self.runtime / 'generation.lock').stat().st_ino
        result = self.go(True)
        self.assertEqual(result['status'], 'updated')
        current = news.read_json(self.runtime / 'current/articles.json')['articles']
        for old, new in zip(self.original, current):
            self.assertEqual({k: v for k, v in old.items() if k != 'hero'}, {k: v for k, v in new.items() if k != 'hero'})
            self.assertEqual({k: v for k, v in old['hero'].items() if k not in ('caption', 'alt', 'credit', 'eventDate', 'gettyAssetId')},
                             {k: v for k, v in new['hero'].items() if k not in ('caption', 'alt', 'credit', 'eventDate', 'gettyAssetId')})
            self.assertIn('Photo by Justin Ford/Getty Images', new['hero']['caption'])
            self.assertIn('File photo.', new['hero']['caption'])
            self.assertIn('Jacardia Wright #31', new['hero']['alt'])
            self.assertEqual(new['hero']['gettyAssetId'], ASSET_ID)
            backup = Path(result['backup']) / (old['generation']['publicationDay'] + '.article.json')
            self.assertEqual(json.loads(backup.read_bytes()), old)
        receipt = news.verify_snapshot(self.runtime / 'current', self.site)
        self.assertEqual(receipt['generatedCount'], 0)
        self.assertEqual(receipt['openaiRequestCount'], 0)
        self.assertEqual(lock_inode, (self.runtime / 'generation.lock').stat().st_ino)
        for relative, content in self.original_files.items():
            self.assertEqual(content, (self.snapshot / relative).read_bytes())
        self.assertEqual((self.runtime / 'current/images' / self.name).read_bytes(), self.data)
        self.no_api.assert_not_called()

    def test_repeat_creates_no_new_release(self):
        self.go(True)
        snapshot = (self.runtime / 'current').resolve()
        self.assertEqual(self.go(True)['status'], 'already-current')
        self.assertEqual(snapshot, (self.runtime / 'current').resolve())
        self.assertEqual(len(list((self.runtime / 'releases').iterdir())), 2)

    def test_corrected_catalog_updates_existing_credit_and_removes_stale_date(self):
        self.go(True)
        previous = news.read_json(self.runtime / 'current/articles.json')['articles']
        for value in previous:
            self.assertEqual(value['hero']['credit'], 'Photo by Justin Ford/Getty Images')
            self.assertEqual(value['hero']['eventDate'], '2026-08-23')
        self.catalog['assets'][ASSET_ID]['credit'] = 'Photo by Corrected Photographer/Getty Images'
        self.catalog['assets'][ASSET_ID].pop('eventDate')
        self.assertEqual(self.go(True)['status'], 'updated')
        corrected = news.read_json(self.runtime / 'current/articles.json')['articles']
        for old, new in zip(previous, corrected):
            self.assertEqual({k: v for k, v in old.items() if k != 'hero'},
                             {k: v for k, v in new.items() if k != 'hero'})
            self.assertEqual(new['hero']['credit'], 'Photo by Corrected Photographer/Getty Images')
            self.assertTrue(new['hero']['caption'].endswith(new['hero']['credit']))
            self.assertNotIn('eventDate', new['hero'])
            self.assertEqual(old['hero']['src'], new['hero']['src'])
        self.assertEqual(self.go(True)['status'], 'already-current')
        self.no_api.assert_not_called()

    def test_publication_failure_resumes_with_existing_backups(self):
        with patch.object(news, 'publish', side_effect=RuntimeError('interrupted publication')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.go(True)
        self.assertEqual(self.snapshot, (self.runtime / 'current').resolve())
        self.assertEqual(self.go()['status'], 'ready')
        self.assertEqual(self.go(True)['status'], 'updated')
        self.no_api.assert_not_called()

    def test_partial_accepted_write_resumes(self):
        original_atomic = news.atomic_json
        count = 0
        def interrupt(path, data):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('interrupted accepted write')
            return original_atomic(path, data)
        with patch.object(news, 'atomic_json', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, 'interrupted accepted'):
                self.go(True)
        self.assertEqual(self.snapshot, (self.runtime / 'current').resolve())
        self.assertEqual(self.go(True)['status'], 'updated')

    def test_unknown_photo_stays_unchanged_and_is_reported(self):
        del self.catalog['sha256'][self.checksum]
        result = self.go(True)
        self.assertEqual(result['status'], 'already-current')
        self.assertEqual(result['articlesUpdated'], 0)
        self.assertEqual(len(result['unmatchedPhotos']), 2)
        self.assertEqual(result['unmatchedPhotos'][0]['sha256'], self.checksum)
        self.assertEqual(self.snapshot, (self.runtime / 'current').resolve())
        self.assertFalse((self.runtime / 'repairs').exists())

    def test_pool_getty_filename_proves_renamed_image_bytes(self):
        del self.catalog['sha256'][self.checksum]
        (self.runtime / 'photos' / ('GettyImages-' + ASSET_ID + '.png')).write_bytes(self.data)
        result = self.go(True)
        self.assertEqual(result['articlesUpdated'], 2)
        self.assertEqual(result['unmatchedPhotos'], [])

    def test_unrelated_accepted_edits_and_corrupt_current_stop_before_writes(self):
        path = self.runtime / 'days' / DAY / 'article.json'
        changed = news.read_json(path)
        changed['headline'] += ' manual change'
        news.atomic_json(path, changed)
        with self.assertRaisesRegex(ValueError, 'unrelated unpublished'):
            self.go(True)
        self.assertFalse((self.runtime / 'repairs').exists())
        news.atomic_json(path, self.original[1])
        with (self.runtime / 'current/articles.json').open('a') as output:
            output.write(' ')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.go(True)
        self.assertFalse((self.runtime / 'repairs').exists())

    def test_missing_image_and_busy_lock_do_not_change_history(self):
        lock = self.runtime / 'generation.lock'
        with lock.open('a') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'generation is running'):
                self.go(True)
        (self.runtime / 'assets' / self.name).unlink()
        with self.assertRaises(FileNotFoundError):
            self.go(True)
        self.assertEqual(self.snapshot, (self.runtime / 'current').resolve())
        self.assertFalse((self.runtime / 'repairs').exists())


if __name__ == '__main__':
    unittest.main()
