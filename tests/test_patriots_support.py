"""Patriots activation uses the existing producers; no sources or services run."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dags'))
sys.path.insert(0, str(ROOT / 'deployment/news'))
from fan_zone_config import validate_sites
from fan_zone_tasks import active_sites
import news_core


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


nfl = module('patriots_nfl_contract', 'deployment/nfl/refresh_nfl.py')
roster = module('patriots_roster_contract', 'deployment/roster/refresh_roster.py')
guides = module('patriots_guides_contract', 'deployment/guides/guides_core.py')


class PatriotsSupportTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((ROOT / 'config/active-sites.json').read_text())
        self.sites = validate_sites(self.raw)
        self.patriots = self.sites['patriots']

    def test_disabled_default_preserves_existing_tasks_and_variable_activation_adds_only_patriots(self):
        sdk = ModuleType('airflow.sdk')
        sdk.Variable = Mock()
        sdk.Variable.get.return_value = deepcopy(self.raw)
        with patch.dict(sys.modules, {'airflow.sdk': sdk}):
            existing = active_sites()
            self.assertEqual({site['slug'] for site in existing},
                             {'seahawks', 'broncos', 'packers', 'vikings', 'chiefs'})
            activated = deepcopy(self.raw)
            activated['patriots']['enabled'] = True
            sdk.Variable.get.return_value = activated
            expanded = active_sites()
        self.assertEqual([site for site in expanded if site['slug'] != 'patriots'], existing)
        self.assertEqual(next(site for site in expanded if site['slug'] == 'patriots'),
                         {**self.patriots, 'enabled': True})
        self.assertFalse(self.patriots['enabled'])

    def test_patriots_identity_and_snapshot_contracts_do_not_reuse_an_existing_team(self):
        # ID 1 / NE / New England Patriots: https://nfl.balldontlie.io/.
        self.assertEqual((self.patriots['balldontlie_team_id'], self.patriots['abbreviation'],
                          self.patriots['city'], self.patriots['name']),
                         (1, 'NE', 'New England', 'Patriots'))
        self.assertEqual(self.patriots['division'], 'AFC East')
        self.assertEqual(nfl.primary_files(self.patriots),
                         ('patriots.json', 'players.json', 'standings.json'))
        self.assertEqual(str(roster.runtime_for(self.patriots)), '/var/lib/patriotsfz-roster')
        self.assertEqual(str(guides.runtime_for(self.patriots)), '/var/lib/patriotsfz-guides')
        registered = json.loads((ROOT / 'deployment/roster/teams.json').read_text())['patriots']
        self.assertEqual(registered, {'name': 'New England Patriots', 'abbreviation': 'NE',
            'domain': 'www.patriots.com', 'timeZone': 'America/New_York'})
        for other_slug, other in self.sites.items():
            if other_slug != 'patriots':
                for field in ('news_snapshot_dir', 'news_photos_dir',
                              'nfl_snapshot_dir', 'recap_snapshot_dir'):
                    self.assertNotEqual(self.patriots[field], other[field])

    def test_daily_article_uses_patriots_identity_and_preserves_source_citations(self):
        sentence = ('This offline fixture checks the independent publication contract '
                    'and its verified source references without creating a live story. ')
        draft = {'headline': 'A Patriots publication fixture for offline checks',
                 'dek': 'This fixture verifies team identity and source citations without provider calls.',
                 'slug': 'publication-fixture', 'category': 'Analysis', 'tags': ['Patriots'],
                 'sections': [
                     {'heading': 'First fixture section', 'paragraphs': [
                         {'text': sentence * 10, 'sourceIds': ['S2']}]},
                     {'heading': 'Second fixture section', 'paragraphs': [
                         {'text': sentence * 10, 'sourceIds': ['S1']}]}]}
        sources = [
            {'id': 'S1', 'label': 'League fixture', 'url': 'https://www.nfl.com/news/fixture'},
            {'id': 'S2', 'label': 'Club fixture', 'url': 'https://www.patriots.com/news/fixture'}]
        article = news_core.make_article(draft, sources, '2026-09-13',
            datetime(2026, 9, 13, 15, tzinfo=timezone.utc), 'offline-fixture', [], self.patriots)
        self.assertEqual(article['team'], 'patriots')
        self.assertTrue(article['slug'].startswith('daily-patriots-'))
        self.assertEqual(article['tags'], ['patriots'])
        self.assertEqual(article['author'], 'Patriots Fan Zone')
        self.assertEqual(article['hero']['caption'], 'Patriots Fan Zone illustration.')
        self.assertEqual(article['sources'][0]['url'], sources[1]['url'])
        self.assertIn('https://www.patriots.com/news/fixture">[1]</a>', article['body'][1]['html'])
        self.assertNotIn('Seahawks', json.dumps(article))


if __name__ == '__main__':
    unittest.main()
