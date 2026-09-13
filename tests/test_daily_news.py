"""Source-free tests; no Airflow installation or live API key required."""
import base64
import copy
from datetime import datetime, timezone
import html
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deployment/news'))
sys.path.insert(0, str(ROOT / 'dags'))
import news_core as news
import sfz_news_hook as hook
ssh_spec = importlib.util.spec_from_file_location('sfz_news_ssh_entrypoint', ROOT / 'deployment/news/ssh_entrypoint.py')
ssh = importlib.util.module_from_spec(ssh_spec)
ssh_spec.loader.exec_module(ssh)


def png(n):
    def chunk(tag, data):
        return struct.pack('>I',len(data)) + tag + data + struct.pack('>I',zlib.crc32(tag+data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0)) + chunk(b'IDAT',zlib.compress(bytes([0,n,0,0]))) + chunk(b'IEND',b'')


SOURCES = [{'id':'S1','label':'Team reporting','url':'https://www.seahawks.com/news/fixture-one'},
           {'id':'S2','label':'League reporting','url':'https://www.nfl.com/news/fixture-two'}]


def draft():
    sentence = 'This test fixture provides source material for exercising the article publication contract and checking that accepted stories stay available across later builds. '
    return {'headline':'An example Seahawks article used only in offline testing', 'dek':'A test description for a generated story, never used as live website content.',
            'slug':'an-example-story','category':'Analysis','tags':['Seahawks'],
            'sections':[{'heading':'First fixture section','paragraphs':[{'text':sentence*8,'sourceIds':['S1']}]},
                        {'heading':'Second fixture section','paragraphs':[{'text':sentence*8,'sourceIds':['S2']}]}]}


def article(day='2026-09-11'):
    return news.make_article(draft(),SOURCES,day,datetime.fromisoformat(day+'T15:00:00+00:00'),'fixture',[])


class DailyNewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source, self.runtime = root/'source',root/'runtime'
        self.source.mkdir(); self.runtime.mkdir()
        for folder in ('photos','assets','days','releases'):
            (self.runtime/folder).mkdir()
        (self.source/'.env').write_text('OPENAI_API_KEY=sk-test-fixture-not-a-real-key\n')
        self.calls = 0

    def generate(self, directory,key,model,day,now,previous):
        self.calls += 1
        return article(day), {'requestsThisAttempt':2}

    def collect(self, run_id='manual-a', day='2026-09-11', generator=None):
        with patch.object(news,'run',return_value='a'*40):
            return news.collect(run_id,day,self.source,self.runtime,
                catalog_fn=lambda source: ('a'*40,{'authored':[],'visible':[]}),
                generate_fn=generator or self.generate,current_time=datetime.fromisoformat(day+'T15:00:00+00:00'))

    def test_same_day_different_run_ids_generate_once(self):
        first=self.collect(); second=self.collect('manual-b')
        self.assertEqual(self.calls,1)
        self.assertEqual(first['generatedCount'],1)
        self.assertEqual(second['generatedCount'],0)
        self.assertEqual(second['openaiRequestCount'],0)
        self.assertEqual(second['runId'],'manual-b')
        hook.validate_receipt(second,'manual-b','2026-09-11')

    def test_crash_after_acceptance_recovers_without_writing(self):
        with patch.object(news,'publish',side_effect=RuntimeError('simulated interruption')):
            with self.assertRaises(RuntimeError): self.collect()
        self.assertTrue((self.runtime/'days/2026-09-11/article.json').exists())
        recovered=self.collect('retry')
        self.assertEqual(self.calls,1)
        self.assertEqual(recovered['articleCount'],1)
        self.assertEqual(recovered['openaiRequestCount'],0)

    def test_generation_failure_preserves_current(self):
        self.collect()
        prior=(self.runtime/'current').resolve()
        def fail(*args): raise RuntimeError('simulated source failure')
        with self.assertRaises(RuntimeError): self.collect(day='2026-09-12',generator=fail)
        self.assertEqual((self.runtime/'current').resolve(),prior)
        self.assertFalse((self.runtime/'days/2026-09-12/article.json').exists())

    def test_next_day_retains_all_history(self):
        for day in range(11,20): self.collect(day=f'2026-09-{day}')
        doc=news.read_json((self.runtime/'current').resolve()/'articles.json')
        self.assertEqual(len(doc['articles']),9)
        self.assertEqual(self.calls,9)

    def test_eight_photos_exclude_seven_and_renamed_duplicates(self):
        hashes=[]
        for i in range(8):
            data=png(i); (self.runtime/f'photos/{i}.png').write_bytes(data)
            hashes.append(news.digest(data))
        (self.runtime/'photos/copy.png').write_bytes(png(0))
        credits = {f'{i}.png': {'caption': f'Offline photo fixture {i}.', 'credit': 'Fixture photographer'} for i in range(8)}
        credits['copy.png'] = credits['0.png']
        news.atomic_json(self.runtime/'photos/metadata.json', credits)
        pool,_=news.photo_pool(self.runtime)
        self.assertEqual(len(pool),8)
        blocked=[{'hero':{'src':f'/images/news/generated/{h}.png'}} for h in hashes[:7]]
        hero,_=news.select_photo(self.runtime,self.source,blocked)
        self.assertEqual(hero['sha256'],hashes[7])
        self.assertTrue((self.runtime/f'assets/{hashes[7]}.png').exists())
        blocked.append({'hero':hero})
        fallback,notes=news.select_photo(self.runtime,self.source,blocked)
        self.assertEqual(fallback,news.FALLBACK)
        self.assertTrue(notes)

    def test_deleted_input_photo_does_not_remove_historical_asset(self):
        photo=self.runtime/'photos/a.png'; photo.write_bytes(png(7))
        news.atomic_json(self.runtime/'photos/metadata.json', {'a.png': {'caption': 'Offline photo fixture.', 'credit': 'Fixture photographer'}})
        receipt=self.collect()
        first=news.read_json((self.runtime/'current').resolve()/'articles.json')['articles'][0]
        filename=first['hero']['src'].split('/')[-1]
        photo.unlink()
        self.collect(day='2026-09-12')
        self.assertTrue(((self.runtime/'current').resolve()/'images'/filename).is_file())
        self.assertEqual(news.read_json((self.runtime/'current').resolve()/'articles.json')['articles'][0]['hero'],first['hero'])

    def test_corrupt_and_empty_pool_fall_back(self):
        (self.runtime/'photos/broken.jpg').write_bytes(b'invalid')
        hero,notes=news.select_photo(self.runtime,self.source,[])
        self.assertEqual(hero,news.FALLBACK)
        self.assertEqual(len(notes),2)

    def test_authored_stories_and_utc_dates_affect_visible_set(self):
        a=article(); a['slug']='an-authored-story'
        a['hero']['src']='/images/authored.png'
        generated=article('2026-09-10')
        visible=news.visible_articles([a],[generated],datetime(2026,9,12,tzinfo=timezone.utc))
        self.assertEqual(visible[0]['slug'],'an-authored-story')
        self.assertEqual(hook.publication_day({'logical_date':datetime(2026,9,12,0,1,tzinfo=timezone.utc)}),'2026-09-11')
        self.assertEqual(hook.publication_day({'logical_date':datetime(2026,3,8,15,tzinfo=timezone.utc)}),'2026-03-08')
        self.assertEqual(hook.publication_day({'logical_date':datetime(2026,11,1,16,tzinfo=timezone.utc)}),'2026-11-01')

    def test_manual_run_uses_stable_start_date(self):
        class Run: start_date=datetime(2026,9,12,1,tzinfo=timezone.utc)
        self.assertEqual(hook.publication_day({'logical_date':None,'dag_run':Run()}),'2026-09-11')
        with self.assertRaises(ValueError): hook.publication_day({})

    def test_cached_successful_responses_make_no_more_requests(self):
        calls=[]
        def call(payload,key): calls.append(payload); return {'status':'completed','output':[]}
        directory=self.runtime/'days/2026-09-11'; directory.mkdir()
        news.cached_response(directory,'research',{'input':'fixture'},'fake',call)
        _,count=news.cached_response(directory,'research',{'input':'fixture'},'fake',call)
        self.assertEqual(len(calls),1); self.assertEqual(count,0)
        with self.assertRaises(ValueError):
            news.cached_response(directory,'research',{'model':'different-model','input':'fixture'},'fake',call)
        self.assertEqual(len(calls),1)

    def test_research_requires_real_search_and_official_citations(self):
        response={'status':'completed','output':[{'type':'web_search_call','status':'completed'}, {'type':'message','content':[
            {'type':'output_text','text':'Fixture research', 'annotations':[{'type':'url_citation','title':s['label'],'url':s['url']} for s in SOURCES]}]}]}
        self.assertEqual(len(news.research_sources(response)),2)
        response['output'][0]['status']='failed'
        with self.assertRaises(ValueError): news.research_sources(response)

    def test_unknown_source_and_html_are_rejected_before_acceptance(self):
        d=draft(); d['sections'][0]['paragraphs'][0]['sourceIds']=['S999']
        with self.assertRaises(ValueError): news.make_article(d,SOURCES,'2026-09-11',news.now_utc(),'fixture',[])
        d=draft(); d['sections'][0]['paragraphs'][0]['text']='<script>bad</script>'
        with self.assertRaises(ValueError): news.make_article(d,SOURCES,'2026-09-11',news.now_utc(),'fixture',[])

    def test_research_markers_do_not_control_first_use_citation_numbers(self):
        d = draft()
        first, second = [section['paragraphs'][0] for section in d['sections']]
        first['sourceIds'], second['sourceIds'] = ['S2'], ['S1']
        first['text'] += ' [S2]'
        second['text'] += ' [S1]'
        result = news.make_article(d, SOURCES, '2026-09-12', news.now_utc(), 'fixture', [])
        self.assertEqual(result['sources'], [{'label': s['label'], 'url': s['url']} for s in reversed(SOURCES)])
        self.assertTrue(result['body'][1]['html'].endswith(f'<a href="{SOURCES[1]["url"]}">[1]</a>'))
        self.assertTrue(result['body'][3]['html'].endswith(f'<a href="{SOURCES[0]["url"]}">[2]</a>'))
        self.assertNotRegex(json.dumps(result['body']), r'\[S[0-9]+\]')

    def test_marker_order_repetition_and_omitted_markers_preserve_declared_sources(self):
        d = draft()
        p = d['sections'][0]['paragraphs'][0]
        p['sourceIds'] = ['S2', 'S1']
        p['text'] += ' [S1] [S2] [S1]'
        result = news.make_article(d, SOURCES, '2026-09-12', news.now_utc(), 'fixture', [])
        self.assertEqual([s['url'] for s in result['sources']], [SOURCES[1]['url'], SOURCES[0]['url']])
        self.assertTrue(result['body'][1]['html'].endswith(
            f'<a href="{SOURCES[1]["url"]}">[1]</a> <a href="{SOURCES[0]["url"]}">[2]</a>'))
        self.assertTrue(result['body'][3]['html'].endswith(f'<a href="{SOURCES[1]["url"]}">[1]</a>'))
        p['text'] = p['text'].replace('[S2]', '')  # IDs do not require prose markers.
        again = news.make_article(d, SOURCES, '2026-09-12', news.now_utc(), 'fixture', [])
        self.assertEqual(again['sources'], result['sources'])

    def test_unknown_or_unmatched_markers_are_rejected_before_acceptance(self):
        for marker in ('[S2]', '[S999]', '[S0]', '[S01]'):
            with self.subTest(marker=marker):
                d = draft()
                d['sections'][0]['paragraphs'][0]['text'] += ' ' + marker
                with self.assertRaisesRegex(ValueError, 'absent from its sourceIds'):
                    news.make_article(d, SOURCES, '2026-09-12', news.now_utc(), 'fixture', [])
        for ids in ([], ['S999'], ['S1', 'S1'], [['S1']]):
            with self.subTest(ids=ids):
                d = draft()
                d['sections'][0]['paragraphs'][0]['sourceIds'] = ids
                with self.assertRaisesRegex(ValueError, 'known retrieved source IDs'):
                    news.make_article(d, SOURCES, '2026-09-12', news.now_utc(), 'fixture', [])

    def test_markers_outside_paragraphs_are_rejected(self):
        for field in ('headline', 'dek', 'heading', 'tags'):
            with self.subTest(field=field):
                d = draft()
                if field == 'heading':
                    d['sections'][0]['heading'] += ' [S1]'
                elif field == 'tags':
                    d['tags'].append('[S1]')
                else:
                    d[field] += ' [S1]'
                with self.assertRaisesRegex(ValueError, 'belong only in paragraph sourceIds'):
                    news.make_article(d, SOURCES, '2026-09-12', news.now_utc(), 'fixture', [])

    def test_normalization_changes_only_markers_and_is_idempotent(self):
        value = 'These  source-backed words & punctuation, remain. [S2] Next sentence [S1].'
        expected = value.replace('[S2]', '').replace('[S1]', '')
        clean = news.normalize_paragraph_text(value, ['S1', 'S2'])
        self.assertEqual(clean, expected)
        self.assertEqual(news.normalize_paragraph_text(clean, ['S1', 'S2']), clean)
        without_markers = 'These  source-backed words & punctuation, remain. Next sentence.'
        self.assertEqual(news.normalize_paragraph_text(without_markers, ['S1']), without_markers)
        with self.assertRaises(ValueError):
            news.normalize_paragraph_text('[S1] ' * 8, ['S1'])

    def test_html_escaping_and_urls_are_preserved_with_marker_normalization(self):
        sources = copy.deepcopy(SOURCES)
        sources[0]['url'] += '?first=1&second=2'
        d = draft()
        p = d['sections'][0]['paragraphs'][0]
        p['text'] += ' A & B remain cited. [S1]'
        result = news.make_article(d, sources, '2026-09-12', news.now_utc(), 'fixture', [])
        self.assertEqual(result['sources'][0]['url'], sources[0]['url'])
        self.assertIn('A &amp; B remain cited.', result['body'][1]['html'])
        self.assertTrue(result['body'][1]['html'].endswith(
            f'<a href="{html.escape(sources[0]["url"], quote=True)}">[1]</a>'))

    def test_articles_without_markers_keep_exact_previous_html(self):
        d = draft()
        result = news.make_article(d, SOURCES, '2026-09-12', news.now_utc(), 'fixture', [])
        for index, section in enumerate(d['sections']):
            expected = html.escape(section['paragraphs'][0]['text'].strip())
            expected += f' <a href="{SOURCES[index]["url"]}">[{index + 1}]</a>'
            self.assertEqual(result['body'][index * 2 + 1]['html'], expected)

    def test_source_registry_does_not_silently_rebind_ids(self):
        variants = [
            [SOURCES[0], {**SOURCES[1], 'id': 'S1'}],
            [{**SOURCES[0], 'id': 'invalid'}, SOURCES[1]],
            [SOURCES[0], {**SOURCES[1], 'url': SOURCES[0]['url']}],
            [{**SOURCES[0], 'url': 'javascript:alert(1)'}, SOURCES[1]],
            [{**SOURCES[0], 'url': 'https://user:password@www.seahawks.com/article'}, SOURCES[1]],
            [{**SOURCES[0], 'url': None}, SOURCES[1]],
        ]
        for sources in variants:
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                news.make_article(draft(), sources, '2026-09-12', news.now_utc(), 'fixture', [])

    def test_marker_normalization_is_shared_by_all_six_teams(self):
        sites = json.loads((ROOT / 'config/active-sites.json').read_text())
        self.assertEqual(set(sites), {'seahawks', 'broncos', 'packers', 'vikings', 'chiefs', 'patriots'})
        for slug, site in sites.items():
            with self.subTest(team=slug):
                d = draft()
                first = d['sections'][0]['paragraphs'][0]
                first['sourceIds'] = ['S2', 'S1']
                first['text'] += ' [S1] [S2]'
                sources = [
                    {'id': 'S1', 'label': 'Team fixture', 'url': f'https://{site["source_domains"][0]}/fixture'},
                    SOURCES[1],
                ]
                result = news.make_article(d, sources, '2026-09-12', news.now_utc(), 'fixture', [], site=site)
                self.assertEqual(result['team'], slug)
                self.assertTrue(result['slug'].startswith(f'daily-{slug}-'))
                self.assertEqual(result['sources'][0]['url'], SOURCES[1]['url'])
                self.assertNotRegex(json.dumps(result['body']), r'\[S[0-9]+\]')

    def test_restricted_command_passes_arguments_without_shell_interpretation(self):
        request={'runId':'manual__with spaces; $(false)','publicationDay':'2026-09-11'}
        token=base64.urlsafe_b64encode(json.dumps(request).encode()).decode().rstrip('=')
        self.assertEqual(ssh.command_arguments('refresh '+token),['--run-id='+request['runId'],'--publication-day=2026-09-11'])
        for command in ('bash','check; whoami','refresh invalid','refresh '+token+' extra'):
            with self.assertRaises(ValueError): ssh.command_arguments(command)


if __name__ == '__main__':
    unittest.main()
