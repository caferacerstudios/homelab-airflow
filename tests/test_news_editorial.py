"""Exercise the edition briefs through both model requests without network calls."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

from test_daily_news import news, draft, article

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests/news_modular'))
from test_news_host import research_response


class ArticleEditorialTests(unittest.TestCase):
    def test_each_team_sends_publication_day_brief_to_research_and_writer(self):
        sites = json.loads((ROOT / 'config/active-sites.json').read_text())
        editions = [('2026-09-15', 'TUESDAY — TEAM PERFORMANCE'),
                    ('2026-09-18', 'FRIDAY — UPCOMING MATCHUP')]
        # Execution date is deliberately different: edition follows publication day.
        now = datetime(2026, 9, 19, 15, tzinfo=timezone.utc)
        for raw_site in [None, *sites.values()]:
            for day, expected in editions:
                selected = news.site_settings(raw_site)
                selected['prompts'] = {'article': 'Prefer a daily injury roundup.'}
                site = selected if raw_site is not None else None
                with self.subTest(team=selected['slug'], day=day), tempfile.TemporaryDirectory() as tmp:
                    calls = []
                    def call(payload, key):
                        calls.append(payload)
                        if len(calls) == 1:
                            return research_response(selected)
                        return {'status': 'completed', 'output': [{'type': 'message', 'content': [
                            {'type': 'output_text', 'text': json.dumps(draft())}]}]}
                    previous = article('2026-09-11')
                    previous['headline'] = 'An older fixture headline to avoid repeating'
                    directory = Path(tmp)
                    generated, usage = news.generate(directory, 'offline', 'fixture', day, now,
                                                     [previous], call=call, site=site)
                    self.assertEqual(usage['requestsThisAttempt'], 2)
                    self.assertEqual(len(calls), 2)
                    for payload in calls:
                        self.assertIn(expected, payload['input'])
                        self.assertIn('brief takes precedence', payload['input'])
                        self.assertIn('TEAM PREFERENCES:', payload['input'])
                        self.assertIn(day, payload['input'])
                        if site:
                            self.assertIn('Prefer a daily injury roundup.', payload['input'])
                    self.assertIn(previous['headline'], calls[0]['input'])
                    self.assertEqual(calls[0]['tools'][0]['filters']['allowed_domains'], selected['source_domains'])
                    self.assertEqual(generated['generation']['publicationDay'], day)
                    self.assertEqual(generated['generation']['promptVersion'], 'fan-zone-articles-v3')
                    self.assertEqual(generated['team'], selected['slug'])
                    # Identical requests reuse both successful stages without extra calls.
                    _, reused = news.generate(directory, 'offline', 'fixture', day, now,
                                              [previous], call=call, site=site)
                    self.assertEqual(reused['requestsThisAttempt'], 0)
                    self.assertEqual(len(calls), 2)

    def test_briefs_cover_schedule_and_reporting_edge_cases(self):
        friday = news.editorial_brief('2026-09-18')
        for constraint in ('next confirmed', 'completed Thursday game', 'bye week', 'offseason',
                           'do not invent an opponent', 'uncertainty'):
            self.assertIn(constraint, friday)
        tuesday = news.editorial_brief('2026-09-15')
        for constraint in ('Choose ONE', 'game completion', 'no recent game', 'major current team headline',
                           'specific strategy breakdown', 'prior Friday'):
            self.assertIn(constraint, tuesday)

    def test_manual_non_schedule_day_remains_available(self):
        self.assertIn('MANUAL EDITION', news.editorial_brief('2026-09-17'))
        self.assertNotIn('FRIDAY —', news.editorial_brief('2026-09-17'))
        with self.assertRaises(ValueError):
            news.editorial_brief('invalid-date')


if __name__ == '__main__':
    unittest.main()
