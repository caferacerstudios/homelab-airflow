"""Team isolation, configurable prompts and restricted transport without API calls."""
import base64
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from test_daily_news import news, ssh, png, draft


def configured_site(slug='broncos'):
    sites = json.loads((ROOT / 'tests/news_modular/fixtures/active-sites.json').read_text())
    return news.site_settings({**sites[slug], 'slug': slug})


def research_response(site):
    urls = [f"https://www.{site['source_domains'][0]}/news/fixture-one", 'https://www.nfl.com/news/fixture-two']
    return {'status': 'completed', 'output': [
        {'type': 'web_search_call', 'status': 'completed'},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Offline fixture research only.',
          'annotations': [{'type': 'url_citation', 'title': 'Official test source', 'url': url} for url in urls]}]},
    ]}


class ModularNewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.site = configured_site()
        self.now = datetime(2026, 9, 12, 15, tzinfo=timezone.utc)

    def article(self, site=None, day='2026-09-12'):
        site = site or self.site
        value = draft()
        value['headline'] = f"An offline {site['name']} reporting test fixture"
        value['tags'] = [site['name'], 'Test']
        value['category'] = 'AFC West' if site['slug'] == 'broncos' else 'NFC West'
        return news.make_article(value, news.research_sources(research_response(site), site),
                                 day, self.now, 'fixture', [], site=site)

    def test_team_identity_and_division_in_article(self):
        article = self.article()
        self.assertEqual(article['team'], 'broncos')
        self.assertEqual(article['author'], 'Broncos Fan Zone')
        self.assertEqual(article['tags'], ['broncos', 'Test'])
        self.assertEqual(article['category'], 'AFC West')
        self.assertTrue(article['slug'].startswith('daily-broncos-2026-09-12-'))
        self.assertIn('Broncos', article['hero']['alt'])
        self.assertNotIn('Seahawks', json.dumps(article))

    def test_research_and_writing_use_configured_city_team_prompt_and_domains(self):
        directory = self.root / 'attempt'
        directory.mkdir()
        site = {**self.site, 'prompts': {**self.site['prompts'], 'article': 'Focus on Denver special teams.'}}
        calls = []
        value = draft()
        value['headline'] = 'An offline Denver Broncos special teams fixture'
        value['tags'] = ['Broncos']
        def call(payload, key):
            calls.append(payload)
            if len(calls) == 1:
                return research_response(site)
            return {'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': json.dumps(value)}]}]}
        article, usage = news.generate(directory, 'offline-no-key', 'fixture', '2026-09-12', self.now, [], call=call, site=site)
        self.assertEqual(usage['requestsThisAttempt'], 2)
        self.assertEqual(article['team'], 'broncos')
        for call in calls:
            self.assertIn('Denver Broncos', call['input'])
            self.assertIn('Focus on Denver special teams.', call['input'])
            self.assertNotIn('Seattle', call['input'])
        self.assertEqual(calls[0]['tools'][0]['filters']['allowed_domains'], site['source_domains'])
        self.assertIn('AFC West', calls[1]['text']['format']['schema']['properties']['category']['enum'])
        self.assertEqual(news.generate(directory, 'offline-no-key', 'fixture', '2026-09-12', self.now, [], call=call, site=site)[1]['requestsThisAttempt'], 0)
        self.assertEqual(len(calls), 2)
        changed = {**site, 'prompts': {**site['prompts'], 'article': 'A different editorial request'}}
        with self.assertRaisesRegex(ValueError, 'cached response'):
            news.generate(directory, 'offline-no-key', 'fixture', '2026-09-12', self.now, [], call=call, site=changed)
        self.assertEqual(len(calls), 2)

    def test_legacy_and_mapped_seattle_keep_original_category_contract(self):
        expected = ['News', 'Analysis', 'Contract Strategy', 'Roster', 'Injuries', 'Game Week', 'Hard Knocks', 'NFC West']
        seattle = configured_site('seahawks')
        for index, site in enumerate((None, seattle)):
            directory = self.root / f'seattle-attempt-{index}'
            directory.mkdir()
            calls = []
            def call(payload, key):
                calls.append(payload)
                if len(calls) == 1:
                    return research_response(seattle)
                return {'status': 'completed', 'output': [{'type': 'message', 'content': [
                    {'type': 'output_text', 'text': json.dumps(draft())}]}]}
            news.generate(directory, 'offline-no-key', 'fixture', '2026-09-12', self.now, [], call=call, site=site)
            self.assertEqual(calls[1]['text']['format']['schema']['properties']['category']['enum'], expected)
            invalid = draft()
            invalid['category'] = 'AFC West'
            with self.assertRaisesRegex(ValueError, 'Invalid article category'):
                news.make_article(invalid, news.research_sources(research_response(seattle), site),
                    '2026-09-12', self.now, 'fixture', [], site=site)

    def test_citations_from_other_team_or_spoofed_domains_are_rejected(self):
        for domain in ('seahawks.com', 'denverbroncos.com.evil.example'):
            response = research_response({**self.site, 'source_domains': [domain]})
            with self.assertRaises(ValueError):
                news.research_sources(response, self.site)

    def test_same_day_team_runs_keep_separate_history_images_and_receipts(self):
        source = self.root / 'source'
        source.mkdir()
        (source / '.env').write_text('OPENAI_API_KEY=offline-fixture\n')
        sites = [configured_site('seahawks'), self.site]
        calls = []
        def settings(site=None):
            return site or sites[0]
        def catalog(source, site):
            return 'a' * 40, {'authored': [], 'visible': []}
        def generate(directory, key, model, day, now, previous, site):
            calls.append(site['slug'])
            return self.article(site, day), {'requestsThisAttempt': 2}
        with patch.object(news, 'site_settings', side_effect=settings), patch.object(news, 'SOURCE', source):
            for i, site in enumerate(sites):
                runtime = self.root / site['slug']
                runtime.mkdir()
                for folder in ('photos', 'assets', 'days', 'releases'):
                    (runtime / folder).mkdir()
                (runtime / 'photos/photo.png').write_bytes(png(i + 1))
                news.atomic_json(runtime / 'photos/metadata.json', {'photo.png': {'caption': 'Offline team photo fixture.', 'credit': 'Fixture photographer'}})
                site.update(website_root=str(source), news_snapshot_dir=str(runtime / 'current'), news_photos_dir=str(runtime / 'photos'))
                first = news.collect('same-run', '2026-09-12', current_time=self.now, site=site, catalog_fn=catalog, generate_fn=generate)
                second = news.collect('retry', '2026-09-12', current_time=self.now, site=site, catalog_fn=catalog, generate_fn=generate)
                self.assertEqual(first['team'], site['slug'])
                self.assertEqual(second['generatedCount'], 0)
                document = news.read_json(runtime / 'current/articles.json')
                self.assertEqual(document['team'], site['slug'])
                self.assertEqual([a['team'] for a in document['articles']], [site['slug']])
                self.assertEqual(document['articles'][0]['hero']['sha256'], news.digest(png(i + 1)))
                self.assertEqual(news.verify_snapshot(runtime / 'current', site)['team'], site['slug'])
        self.assertEqual(calls, ['seahawks', 'broncos'])

    def test_wrong_team_accepted_state_cannot_be_republished(self):
        runtime = self.root / 'runtime'
        directory = runtime / 'days/2026-09-12'
        directory.mkdir(parents=True)
        article = self.article(configured_site('seahawks'))
        news.atomic_json(directory / 'article.json', article)
        with self.assertRaisesRegex(ValueError, 'expected broncos'):
            news.accepted_articles(runtime, self.site)
        self.assertEqual(news.read_json(directory / 'article.json'), article)

    def test_old_and_mapped_seattle_requests_share_daily_history_and_existing_model(self):
        site = configured_site('seahawks')
        self.assertEqual(site['website_root'], str(news.SOURCE))
        self.assertEqual(site['news_snapshot_dir'], str(news.RUNTIME / 'current'))
        self.assertEqual(site['news_photos_dir'], str(news.RUNTIME / 'photos'))
        source, runtime = self.root / 'source', self.root / 'seattle-runtime'
        source.mkdir()
        runtime.mkdir()
        for folder in ('photos', 'assets', 'days', 'releases'):
            (runtime / folder).mkdir()
        (source / '.env').write_text('OPENAI_API_KEY=offline-fixture\n')
        news.atomic_json(runtime / 'config.json', {'model': 'existing-custom-model'})
        calls = []
        def generate(directory, key, model, day, now, previous):
            calls.append(model)
            return self.article(site), {'requestsThisAttempt': 2}
        first = news.collect('legacy-two-field-request', '2026-09-12', source=source, runtime=runtime,
            catalog_fn=lambda source: ('a' * 40, {'authored': [], 'visible': []}),
            generate_fn=generate, current_time=self.now)
        original_snapshot = (runtime / 'current').resolve()
        original_settings = news.site_settings
        mapped_site = {**site, 'website_root': str(source), 'news_snapshot_dir': str(runtime / 'current'),
                       'news_photos_dir': str(runtime / 'photos')}
        with patch.object(news, 'site_settings', side_effect=lambda selected=None: mapped_site if selected is not None else original_settings()):
            second = news.collect('mapped-seahawks-request', '2026-09-12', site=mapped_site,
                generate_fn=lambda *a, **kw: self.fail('Mapped Seattle must reuse the accepted article'), current_time=self.now)
        self.assertEqual(calls, ['existing-custom-model'])
        self.assertEqual(first['team'], 'seahawks')
        self.assertEqual(second['generatedCount'], 0)
        self.assertEqual(second['openaiRequestCount'], 0)
        self.assertEqual(second['team'], 'seahawks')
        self.assertEqual((runtime / 'current').resolve(), original_snapshot)
        self.assertEqual(len(news.read_json(runtime / 'current/articles.json')['articles']), 1)
        self.assertEqual(news.read_json(runtime / 'config.json'), {'model': 'existing-custom-model'})

    def test_legacy_seahawks_history_gets_tags_without_rewriting_prose(self):
        runtime = self.root / 'runtime'
        directory = runtime / 'days/2026-09-12'
        directory.mkdir(parents=True)
        article = self.article(configured_site('seahawks'))
        article.pop('team')
        article['tags'] = ['Historical']
        news.atomic_json(directory / 'article.json', article)
        accepted = news.accepted_articles(runtime)[0]
        self.assertEqual(accepted['team'], 'seahawks')
        self.assertEqual(accepted['body'], article['body'])
        self.assertEqual(news.read_json(directory / 'article.json'), article)
        with self.assertRaises(ValueError):
            news.accepted_articles(runtime, self.site)

    def test_restricted_ssh_validates_configuration_and_keeps_json_in_one_argument(self):
        request = {'runId': 'manual; $(false)', 'publicationDay': '2026-09-12', 'site': self.site}
        token = base64.urlsafe_b64encode(json.dumps(request).encode()).decode().rstrip('=')
        args = ssh.command_arguments('refresh ' + token)
        self.assertEqual(len(args), 3)
        self.assertEqual(args[0], '--run-id=manual; $(false)')
        self.assertEqual(json.loads(args[2].split('=', 1)[1])['slug'], 'broncos')
        bad = copy.deepcopy(request)
        bad['site']['news_snapshot_dir'] = '/etc/current'
        token = base64.urlsafe_b64encode(json.dumps(bad).encode()).decode().rstrip('=')
        with self.assertRaises(ValueError):
            ssh.command_arguments('refresh ' + token)
        check = base64.urlsafe_b64encode(json.dumps({'site': self.site}).encode()).decode().rstrip('=')
        self.assertEqual(ssh.command_arguments('check ' + check)[0], '--check')

    def test_template_catalog_is_isolated_and_excludes_other_team_preview(self):
        source = self.root / 'template'
        (source / 'template-tools').mkdir(parents=True)
        (source / 'template-tools/render.mjs').touch()
        for name in news.CODE_FILES:
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).touch()
        (source / 'dist/data').mkdir(parents=True)
        news.atomic_json(source / 'dist/data/news-front-page.json', [
            {'team': 'seahawks', 'slug': 'seattle-story'}, {'team': 'broncos', 'slug': 'denver-story'}])
        commands = []
        def run(args, **kwargs):
            commands.append(args)
            if args[0] == 'docker':
                return json.dumps({'authored': [], 'visible': []})
            if args[-2:] == ['branch', '--show-current']:
                return 'main'
            if 'rev-parse' in args:
                return 'a' * 40
            return ''
        with patch.object(news, 'run', side_effect=run):
            _, catalog = news.source_catalog(source, self.site)
        self.assertEqual(catalog['visible'], [{'team': 'broncos', 'slug': 'denver-story'}])
        docker = next(args for args in commands if args[0] == 'docker')
        self.assertIn('--network=none', docker)
        self.assertIn('--read-only', docker)
        self.assertIn('--tmpfs', docker)
        self.assertIn(f'type=bind,src={source},dst=/app,readonly', docker)
        self.assertIn('linkDependencies:false', docker[-1])


if __name__ == '__main__':
    unittest.main()
