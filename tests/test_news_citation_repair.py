"""Offline accepted-news repair, immutable release, and recovery regression tests."""
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import html
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deployment/news'))
import news_core as news
import repair_citations as repair


DAY = '2026-09-12'
SOURCES = [
    {'id': 'S1', 'label': 'Next Gen official source', 'url': 'https://www.seahawks.com/news/next-gen?a=1&b=2'},
    {'id': 'S2', 'label': 'Comeback official source', 'url': 'https://www.seahawks.com/news/comeback'},
    {'id': 'S3', 'label': 'Roster official source', 'url': 'https://www.seahawks.com/news/roster'},
]


def writing_draft():
    sentence = ('This offline example describes a verified team development and its significance using the '
                'supplied reporting without inventing additional facts for the reader. ')
    return dict(headline='An offline test of the Seattle reporting source order',
                dek='An offline test description that retains the same verified reporting and source links.',
                slug='verified-source-order', category='Analysis', tags=['seahawks'],
                sections=[
                    {'heading': 'First official account', 'paragraphs': [
                        {'text': sentence * 7 + '[S2]', 'sourceIds': ['S2']}]},
                    {'heading': 'Supporting official evidence', 'paragraphs': [
                        {'text': sentence * 7 + '[S1]', 'sourceIds': ['S1']},
                        {'text': sentence * 2 + '[S3]', 'sourceIds': ['S3']}]},
                ])


class RepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = Path(temp.name) / 'runtime'
        self.runtime.mkdir()
        for folder in ('days', 'releases', 'assets', 'photos'):
            (self.runtime / folder).mkdir()
        self.directory = self.runtime / 'days' / DAY
        self.directory.mkdir()
        (self.runtime / 'generation.lock').touch()
        self.site = dict(news.site_settings(), news_snapshot_dir=str(self.runtime / 'current'),
                         news_photos_dir=str(self.runtime / 'photos'), website_root=str(Path(temp.name) / 'website'))
        self.addCleanup(patch.stopall)
        patch.object(news, 'site_settings', side_effect=lambda site=None: self.site).start()
        patch.object(news, 'run', return_value='a' * 40).start()
        self.no_api = patch.object(news, 'call_openai', side_effect=AssertionError('No network calls')).start()
        self.draft = writing_draft()
        self.article = news.make_article(self.draft, SOURCES, DAY,
                                        datetime(2026, 9, 12, 15, tzinfo=timezone.utc), 'fixture', [])
        # Reproduce the old producer's literal prose markers independently of
        # the repair reconstruction. First rendered citation belongs to S2.
        for index, marker in ((1, 'S2'), (3, 'S1'), (4, 'S3')):
            text, links = self.article['body'][index]['html'].split(' <a ', 1)
            self.article['body'][index]['html'] = text + f' [{marker}] <a ' + links
        data = b'retained-photo-fixture'
        self.photo_name = news.digest(data) + '.png'
        (self.runtime / 'assets' / self.photo_name).write_bytes(data)
        self.article['hero'] = dict(src='/images/news/generated/' + self.photo_name,
                                   alt='An existing editorial photograph', caption='Original credit.', width=1200, height=675)
        self.write_cache()
        news.atomic_json(self.directory / 'article.json', self.article)
        # An unrelated older story must be retained byte-for-byte in collection.
        self.older = deepcopy(self.article)
        self.older.update(slug='daily-seahawks-2026-09-11-prior-story', headline='A distinct older accepted news story')
        self.older['generation']['publicationDay'] = '2026-09-11'
        older_dir = self.runtime / 'days/2026-09-11'
        older_dir.mkdir()
        news.atomic_json(older_dir / 'article.json', self.older)
        news.publish(self.runtime, 'original-run', DAY, '0' * 40, 1, {'requestsThisAttempt': 2}, [], self.site)
        self.original_snapshot = (self.runtime / 'current').resolve()
        self.original_files = {p.relative_to(self.original_snapshot): p.read_bytes()
                               for p in self.original_snapshot.rglob('*') if p.is_file()}

    def write_cache(self):
        news.atomic_json(self.directory / 'article-request.json', {'input': 'Fixture research\nSOURCE IDS:\n' + json.dumps(SOURCES)})
        news.atomic_json(self.directory / 'article-response.json', {'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(self.draft)}]}]})

    def go(self, apply=False):
        return repair.repair(DAY, self.site, apply=apply)

    def test_check_is_read_only_then_remapped_sources_preserve_identity_and_history(self):
        before = {p.relative_to(self.runtime): (p.read_bytes(), p.stat().st_mode)
                  for p in self.runtime.rglob('*') if p.is_file() and not p.is_symlink()}
        lock_inode = (self.runtime / 'generation.lock').stat().st_ino
        checked = self.go()
        self.assertEqual(checked['paragraphsRepaired'], 3)
        self.assertEqual(checked['status'], 'ready')
        after = {p.relative_to(self.runtime): (p.read_bytes(), p.stat().st_mode)
                 for p in self.runtime.rglob('*') if p.is_file() and not p.is_symlink()}
        self.assertEqual(before, after)
        self.assertFalse((self.runtime / 'repairs').exists())
        result = self.go(True)
        self.assertEqual(result['status'], 'repaired')
        self.assertNotEqual((self.runtime / 'current').resolve(), self.original_snapshot)
        self.assertEqual((self.runtime / 'generation.lock').stat().st_ino, lock_inode)
        current = news.read_json(self.runtime / 'current/articles.json')['articles']
        self.assertEqual(current[0], self.older)
        corrected = current[1]
        self.assertEqual({k: v for k, v in corrected.items() if k != 'body'},
                         {k: v for k, v in self.article.items() if k != 'body'})
        self.assertEqual(corrected['sources'][0], {'label': SOURCES[1]['label'], 'url': SOURCES[1]['url']})
        self.assertIn('comeback">[1]</a>', corrected['body'][1]['html'])
        self.assertIn('next-gen?a=1&amp;b=2">[2]</a>', corrected['body'][3]['html'])
        self.assertNotIn('[S', json.dumps(corrected['body']))
        manifest = news.verify_snapshot(self.runtime / 'current', self.site)
        self.assertEqual(manifest['sourceCommit'], 'a' * 40)
        self.assertEqual(manifest['generatedCount'], 0)
        self.assertEqual(manifest['openaiRequestCount'], 0)
        self.assertEqual(manifest['articleCount'], 2)
        for relative, data in self.original_files.items():
            self.assertEqual((self.original_snapshot / relative).read_bytes(), data)
        self.assertEqual((self.runtime / 'current/images' / self.photo_name).read_bytes(), b'retained-photo-fixture')
        backup = Path(result['backup'])
        self.assertEqual(json.loads((backup / 'article.json').read_bytes()), self.article)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((backup / 'article.json').stat().st_mode), 0o600)
        self.no_api.assert_not_called()

    def test_repeat_does_not_create_another_release(self):
        self.go(True)
        current = (self.runtime / 'current').resolve()
        releases = sorted((self.runtime / 'releases').iterdir())
        result = self.go(True)
        self.assertEqual(result['status'], 'already-correct')
        self.assertEqual(current, (self.runtime / 'current').resolve())
        self.assertEqual(releases, sorted((self.runtime / 'releases').iterdir()))

    def test_interrupted_publication_recovers_without_recollection(self):
        with patch.object(news, 'publish', side_effect=RuntimeError('simulated interruption')):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.go(True)
        self.assertEqual((self.runtime / 'current').resolve(), self.original_snapshot)
        corrected = news.read_json(self.directory / 'article.json')
        self.assertNotIn('[S', json.dumps(corrected['body']))
        check = self.go()
        self.assertFalse(check['acceptedChange'])
        self.assertTrue(check['publicationChange'])
        self.go(True)
        self.assertEqual(news.read_json(self.runtime / 'current/articles.json')['articles'][1], corrected)
        self.no_api.assert_not_called()

    def test_unknown_or_mismatched_marker_is_not_guessed(self):
        for marker in ('S99', 'S1'):
            with self.subTest(marker=marker):
                self.draft['sections'][0]['paragraphs'][0]['text'] = writing_draft()['sections'][0]['paragraphs'][0]['text'].replace('[S2]', '[' + marker + ']')
                self.write_cache()
                with self.assertRaises(ValueError):
                    self.go(True)
                self.assertEqual((self.runtime / 'current').resolve(), self.original_snapshot)
                self.assertEqual(news.read_json(self.directory / 'article.json'), self.article)
                self.assertFalse((self.runtime / 'repairs').exists())

    def test_missing_cached_response_stops_without_changes(self):
        (self.directory / 'article-response.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.go(True)
        self.assertFalse((self.runtime / 'repairs').exists())
        self.assertEqual((self.runtime / 'current').resolve(), self.original_snapshot)

    def test_manual_prose_changes_require_review(self):
        modified = deepcopy(self.article)
        modified['body'][1]['html'] = 'A manual editorial addition. ' + modified['body'][1]['html']
        news.atomic_json(self.directory / 'article.json', modified)
        with self.assertRaisesRegex(ValueError, 'body differs'):
            self.go(True)
        self.assertFalse((self.runtime / 'repairs').exists())
        self.assertEqual(news.read_json(self.directory / 'article.json'), modified)

    def test_changed_source_order_or_url_requires_review(self):
        modified = deepcopy(self.article)
        modified['sources'][0]['url'] += '?different=1'
        news.atomic_json(self.directory / 'article.json', modified)
        with self.assertRaisesRegex(ValueError, 'source order or URLs'):
            self.go(True)
        self.assertFalse((self.runtime / 'repairs').exists())

    def test_corrupt_snapshot_fails_before_accepted_write(self):
        with (self.runtime / 'current/articles.json').open('a') as handle:
            handle.write(' ')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.go(True)
        self.assertEqual(news.read_json(self.directory / 'article.json'), self.article)
        self.assertFalse((self.runtime / 'repairs').exists())

    def test_missing_retained_photo_stops_before_acceptance(self):
        (self.runtime / 'assets' / self.photo_name).unlink()
        with self.assertRaises(FileNotFoundError):
            self.go(True)
        self.assertEqual(news.read_json(self.directory / 'article.json'), self.article)
        self.assertFalse((self.runtime / 'repairs').exists())

    def test_unrelated_unpublished_changes_are_not_silently_published(self):
        changed = deepcopy(self.older)
        changed['headline'] += ' edited'
        news.atomic_json(self.runtime / 'days/2026-09-11/article.json', changed)
        with self.assertRaisesRegex(ValueError, 'unrelated differences'):
            self.go(True)
        self.assertFalse((self.runtime / 'repairs').exists())

    def test_busy_generation_lock_is_not_replaced(self):
        lock = self.runtime / 'generation.lock'
        with lock.open('a') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'generation is running'):
                self.go(True)
        self.assertFalse((self.runtime / 'repairs').exists())

    def test_symlink_cache_and_missing_lock_are_not_followed_or_created(self):
        response = self.directory / 'article-response.json'
        saved = self.runtime / 'outside.json'
        response.rename(saved)
        response.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, 'ordinary path'):
            self.go(True)
        response.unlink()
        saved.rename(response)
        (self.runtime / 'generation.lock').unlink()
        with self.assertRaises(FileNotFoundError):
            self.go()
        self.assertFalse((self.runtime / 'generation.lock').exists())

    def test_restrictive_umask_restored_and_publication_readable(self):
        previous = os.umask(0o077)
        try:
            self.go(True)
            observed = os.umask(0o077)
            self.assertEqual(observed, 0o077)
            snapshot = (self.runtime / 'current').resolve()
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((snapshot / 'articles.json').stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE((snapshot / 'images').stat().st_mode), 0o755)
        finally:
            os.umask(previous)


if __name__ == '__main__':
    unittest.main()
