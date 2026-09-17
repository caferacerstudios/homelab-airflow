"""Real Git coverage for catalog source guards; Docker/API calls are mocked."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_daily_news import news


class NewsSourceCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / 'template source'
        self.source.mkdir()
        sites = json.loads((ROOT / 'config/active-sites.json').read_text())
        self.site = {**sites['broncos'], 'slug': 'broncos'}
        for name in [*news.CODE_FILES, 'template-tools/render.mjs',
                     'src/lib/news-team.mjs', 'config/active-sites.json']:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}\n' if name.endswith('.json') else '// committed fixture\n')
        self.git('init', '-b', 'main')
        self.git('add', '.')
        self.git('-c', 'user.name=Offline Test', '-c', 'user.email=offline@example.invalid',
                 'commit', '-m', 'Catalog fixture')
        self.original_run = news.run
        self.docker_calls = []

    def git(self, *args):
        return news.run(['git', '-C', str(self.source), *args])

    def catalog_run(self, args, **kwargs):
        if args[0] == 'docker':
            self.docker_calls.append(args)
            return json.dumps({'authored': [], 'visible': []})
        return self.original_run(args, **kwargs)

    def test_team_config_edits_report_checkout_and_path_without_changing_files(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                path = self.source / 'config/active-sites.json'
                path.write_text('{"local": "review before committing"}\n')
                if staged:
                    self.git('add', 'config/active-sites.json')
                before = self.git('status', '--porcelain')
                with patch.object(news, 'run', side_effect=self.catalog_run):
                    with self.assertRaisesRegex(ValueError, 'team/news configuration') as error:
                        news.source_catalog(self.source, self.site)
                self.assertIn(str(self.source), str(error.exception))
                self.assertIn('config/active-sites.json', str(error.exception))
                self.assertEqual(self.git('status', '--porcelain'), before)
                self.assertEqual(path.read_text(), '{"local": "review before committing"}\n')
        self.assertEqual(self.docker_calls, [])

    def test_untracked_template_files_report_each_path(self):
        local = self.source / 'template-tools/local'
        local.mkdir()
        for name in ('first.mjs', 'second.mjs'):
            (local / name).write_text('// unreviewed local work\n')
        with patch.object(news, 'run', side_effect=self.catalog_run):
            with self.assertRaisesRegex(ValueError, 'team/news configuration') as error:
                news.source_catalog(self.source, self.site)
        for name in ('first.mjs', 'second.mjs'):
            self.assertIn('template-tools/local/' + name, str(error.exception))
        self.assertEqual(self.docker_calls, [])

    def test_authored_news_code_edit_stays_blocked_with_actionable_path(self):
        (self.source / 'src/lib/news.ts').write_text('// pending authored news\n')
        with patch.object(news, 'run', side_effect=self.catalog_run):
            with self.assertRaisesRegex(ValueError, 'news source files') as error:
                news.source_catalog(self.source, self.site)
        self.assertIn('src/lib/news.ts', str(error.exception))
        self.assertIn(str(self.source), str(error.exception))
        self.assertEqual(self.docker_calls, [])

    def test_normal_team_build_outputs_do_not_block_read_only_catalog(self):
        for name in ('.team-build/broncos/src/data/news-site.json', 'dist/data/other-output.json'):
            output = self.source / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{}\n')
        before = self.git('status', '--porcelain', '--untracked-files=all')
        with patch.object(news, 'run', side_effect=self.catalog_run):
            commit, catalog = news.source_catalog(self.source, self.site)
        self.assertEqual(commit, self.git('rev-parse', 'HEAD'))
        self.assertEqual(catalog, {'authored': [], 'visible': []})
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all'), before)
        self.assertEqual(len(self.docker_calls), 1)
        self.assertIn('--network=none', self.docker_calls[0])
        self.assertIn('--read-only', self.docker_calls[0])


if __name__ == '__main__':
    unittest.main()
