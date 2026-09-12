"""Guide research and isolated publisher tests; no live provider/host writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('guides_under_test', ROOT / 'deployment/guides/guides_core.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

NOW = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
SITE = {'slug': 'seahawks', 'enabled': True, 'city': 'Seattle', 'name': 'Seahawks', 'abbreviation': 'SEA',
        'balldontlie_team_id': 31, 'timezone': 'America/Los_Angeles', 'source_domains': ['seahawks.com', 'nfl.com'],
        'website_root': '/home/laurawkr/seahawksfanzone', 'news_snapshot_dir': '/var/lib/sfz-news/current',
        'nfl_snapshot_dir': '/var/lib/sfz-nfl/current'}
GAME = {'gameId': '1392244', 'team': 'seahawks', 'season': 2026, 'phase': 'regular', 'week': 2,
        'date': '2026-09-20', 'startsAt': '2026-09-20T20:25:00.000Z', 'timeConfirmed': True,
        'homeAway': 'away', 'opponent': 'Arizona Cardinals', 'venue': 'State Farm Stadium', 'state': 'upcoming'}
SOURCES = [
    {'id': 'S1', 'name': 'Stadium guide', 'url': 'https://www.statefarmstadium.com/plan-your-visit/a-z-guide'},
    {'id': 'S2', 'name': 'Official transportation', 'url': 'https://www.azcardinals.com/stadium/directions'},
]


def fact(values, ids=None, *, event=False):
    return {**values, 'sourceIds': ids or ['S1'], 'scope': 'event-specific' if event else 'standing-policy',
            'eventDate': GAME['date'] if event else None}


def draft_fixture():
    return {'summary': fact({'text': 'Review the stadium entry policy and official transportation guidance before heading to the away venue.'}, ['S1', 'S2']),
            'alerts': [], 'transportation': [fact({'name': 'Arrival planning', 'recommendation': 'Review the official stadium directions.',
                                                  'details': 'This is general stadium access guidance; event-specific service remains unconfirmed.'}, ['S2'])],
            'parking': [], 'timeline': [], 'tailgates': [], 'watchParties': [],
            'stadiumTips': [fact({'title': 'Entry policy', 'details': 'Check the stadium entry policy before packing a bag.'})],
            'localTv': [], 'streams': [], 'national': None, 'officialGameSourceId': None,
            'unknowns': ['Local station, streams and dated watch-party announcements remain unconfirmed.']}


def schedule_fixture(games=None):
    return {'season': 2026, 'games': games or {GAME['gameId']: deepcopy(GAME)},
            'updatedAt': '2026-09-12T10:00:00.000Z', 'manifestSha256': 'b' * 64}


def normalized_row(game=None):
    game = deepcopy(game or GAME)
    own = {'id': 31, 'abbreviation': 'SEA', 'full_name': 'Seattle Seahawks'}
    opponent = {'id': 1, 'abbreviation': 'ARI', 'full_name': game['opponent']}
    return {'id': game['gameId'], 'season': game['season'], 'phase': game['phase'], 'week': game['week'],
            'isHome': game['homeAway'] == 'home', 'opponent': opponent,
            'homeTeam': own if game['homeAway'] == 'home' else opponent,
            'awayTeam': opponent if game['homeAway'] == 'home' else own,
            'date': game['date'], 'startsAt': game['startsAt'], 'dateConfirmed': game['date'] is not None,
            'timeConfirmed': game['timeConfirmed'], 'venue': game['venue'], 'state': game['state']}


def accepted_fixture(directory, key, config, game, site, now):
    guide, watch, evidence = core.make_records(draft_fixture(), SOURCES, game, site, now)
    return guide, watch, {**evidence, 'openaiRequestCount': 2}


def response_fixture(value, research=False):
    return {'status': 'completed', 'id': 'mock-research' if research else 'mock-draft', 'output': [
        *([{'type': 'web_search_call', 'status': 'completed'}] if research else []),
        {'type': 'message', 'content': [{'type': 'output_text', 'text': value,
          'annotations': [{'type': 'url_citation', 'url': source['url'], 'title': source['name']} for source in SOURCES] if research else []}]}]}


class ResearchTests(unittest.TestCase):
    def test_only_actual_https_annotations_are_accepted(self):
        response = response_fixture('Text may mention https://unretrieved.example/details but it is not evidence.', True)
        response['output'][-1]['content'][0]['annotations'].extend([
            {'type': 'url_citation', 'url': 'http://unsafe.example/a'},
            {'type': 'url_citation', 'url': 'https://127.0.0.1/private'},
            {'type': 'url_citation', 'url': 'https://name:password@example.com/private'}])
        self.assertEqual(core.sources_from_research(response), SOURCES)
        response['output'] = response['output'][1:]
        with self.assertRaisesRegex(ValueError, 'completed live web search'):
            core.sources_from_research(response)

    def test_claims_require_real_source_and_exact_event_date(self):
        draft = draft_fixture()
        draft['stadiumTips'][0]['sourceIds'] = ['S99']
        with self.assertRaisesRegex(ValueError, 'retrieved source IDs'):
            core.make_records(draft, SOURCES, GAME, SITE, NOW)
        draft = draft_fixture()
        draft['localTv'] = [fact({'name': 'Unverified channel', 'note': None}, event=True)]
        draft['localTv'][0]['eventDate'] = '2026-09-21'
        with self.assertRaisesRegex(ValueError, 'exact canonical game date'):
            core.make_records(draft, SOURCES, GAME, SITE, NOW)

    def test_generic_page_cannot_confirm_sounder_or_broadcast(self):
        draft = draft_fixture()
        draft['transportation'][0]['name'] = 'Sounder'
        with self.assertRaisesRegex(ValueError, 'date-specific'):
            core.make_records(draft, SOURCES, GAME, SITE, NOW)
        draft = draft_fixture()
        draft['localTv'] = [fact({'name': 'FOX 13', 'note': None})]
        with self.assertRaisesRegex(ValueError, 'date-specific'):
            core.make_records(draft, SOURCES, GAME, SITE, NOW)

    def test_unknown_only_draft_is_not_published(self):
        draft = draft_fixture()
        draft['transportation'], draft['stadiumTips'] = [], []
        draft['summary'] = None
        with self.assertRaisesRegex(ValueError, 'at least two useful'):
            core.make_records(draft, SOURCES, GAME, SITE, NOW)

    def test_unsupported_fields_and_fabricated_urls_are_rejected(self):
        draft = draft_fixture()
        draft['transportation'][0]['sourceUrl'] = 'https://invented.example/page'
        with self.assertRaisesRegex(ValueError, 'structured schema'):
            core.make_records(draft, SOURCES, GAME, SITE, NOW)
        draft = draft_fixture()
        draft['stadiumTips'][0]['details'] = 'Visit https://invented.example/page.'
        with self.assertRaisesRegex(ValueError, 'markup/links'):
            core.make_records(draft, SOURCES, GAME, SITE, NOW)

    def test_research_retry_reuses_actual_responses_without_new_calls(self):
        calls = []

        def provider(payload, key):
            self.assertEqual(key, 'test-secret')
            calls.append(payload)
            return (response_fixture('Current stadium policy and operator guidance with citations.', True)
                    if 'tools' in payload else response_fixture(json.dumps(draft_fixture())))

        with tempfile.TemporaryDirectory() as folder:
            guide, watch, evidence = core.generate(Path(folder), 'test-secret', core.load_config(), GAME, SITE, NOW, provider)
            again = core.generate(Path(folder), 'test-secret', core.load_config(), GAME, SITE, NOW, provider)
            self.assertEqual(len(calls), 2)
            self.assertEqual(evidence['openaiRequestCount'], 2)
            self.assertEqual(again[2]['openaiRequestCount'], 0)
            self.assertEqual(guide, again[0])
            self.assertEqual(guide['game']['startsAt'], GAME['startsAt'])
            self.assertEqual(watch['lastUpdated'], guide['lastUpdated'])
            self.assertEqual(watch['national'], 'TBD')
            self.assertNotIn('test-secret', ''.join(path.read_text() for path in Path(folder).iterdir()))


class ScheduleTests(unittest.TestCase):
    def test_real_game_identity_and_canonical_date_required(self):
        self.assertEqual(core.game_identity(normalized_row(), 2026, SITE), GAME)
        row = normalized_row()
        row['id'] = '2026-regular-2-upcoming'
        with self.assertRaisesRegex(ValueError, 'real upstream numeric'):
            core.game_identity(row, 2026, SITE)
        row = normalized_row()
        row['date'] = '2026-09-21'
        with self.assertRaisesRegex(ValueError, 'canonical Pacific'):
            core.game_identity(row, 2026, SITE)
        row = normalized_row()
        row['awayTeam']['id'] = 32
        with self.assertRaisesRegex(ValueError, 'registered team'):
            core.game_identity(row, 2026, SITE)

    def test_bye_and_canceled_games_are_excluded(self):
        self.assertIsNone(core.game_identity({'bye': True}, 2026, SITE))
        row = normalized_row()
        row['state'] = 'canceled'
        self.assertIsNone(core.game_identity(row, 2026, SITE))

    def test_bounded_horizon_and_missing_future_batch(self):
        games = {GAME['gameId']: GAME}
        past = {**GAME, 'gameId': '100', 'date': '2026-09-09', 'week': 1, 'state': 'completed'}
        games[past['gameId']] = past
        for week in range(3, 9):
            game = {**GAME, 'gameId': str(100 + week), 'week': week,
                    'date': (NOW.date() + timedelta(days=week * 7)).isoformat()}
            games[game['gameId']] = game
        selected = core.select_games(games, {}, core.load_config(), NOW)
        self.assertEqual([row['week'] for row in selected], [2, 3, 4])
        guide, _, _ = core.make_records(draft_fixture(), SOURCES, GAME, SITE, NOW)
        self.assertEqual([row['week'] for row in core.select_games(games, {GAME['gameId']: guide}, core.load_config(), NOW)], [3, 4, 5])

    def test_kickoff_change_invalidates_accepted_record(self):
        guide, _, _ = core.make_records(draft_fixture(), SOURCES, GAME, SITE, NOW)
        changed = {**GAME, 'startsAt': '2026-09-20T23:25:00Z'}
        self.assertFalse(core.same_identity(guide, changed))

    def test_runtime_can_only_be_derived_from_news_prefix(self):
        self.assertEqual(core.runtime_for(SITE), Path('/var/lib/sfz-guides'))
        for value in ['/var/lib/sfz-nfl/current', '/tmp/sfz-news/current', '/var/lib/../sfz-news/current']:
            with self.assertRaises(ValueError):
                core.runtime_for({**SITE, 'news_snapshot_dir': value})


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.schedule = schedule_fixture()
        for name, result in [('site_settings', SITE), ('runtime_for', self.runtime), ('preflight', 'a' * 40),
                             ('source_commit', 'a' * 40), ('credential', 'test-secret')]:
            mocker = patch.object(core, name, return_value=result)
            mocker.start()
            self.addCleanup(mocker.stop)
        mocker = patch.object(core, 'read_schedule', side_effect=lambda *_: deepcopy(self.schedule))
        mocker.start()
        self.addCleanup(mocker.stop)

    def collect(self, run='test-run', now=NOW, generator=accepted_fixture):
        return core.collect(run, SITE, now=now, generate_fn=generator)

    def test_idempotency_and_immutable_current_publication(self):
        initial = self.collect()
        selected = (self.runtime / 'current').resolve()
        files = {path.name: path.read_bytes() for path in selected.iterdir()}
        repeat = self.collect(generator=lambda *_: self.fail('An accepted run must not regenerate'))
        self.assertTrue(repeat['reused'])
        self.assertEqual(initial['files'], repeat['files'])
        self.assertEqual(files, {path.name: path.read_bytes() for path in selected.iterdir()})
        self.assertEqual({path.name for path in self.runtime.iterdir()}, {'collector.lock', 'releases', 'evidence', 'current'})
        self.assertEqual(core.verify_snapshot(selected, SITE)[0]['runId'], 'test-run')

    def test_failed_batch_retains_previous_current_and_evidence_timestamps(self):
        self.collect()
        before = (self.runtime / 'current').resolve()

        def failure(*_):
            raise ValueError('No completed research')

        with self.assertRaisesRegex(ValueError, 'completed research'):
            self.collect('failing-run', NOW + timedelta(days=2), failure)
        self.assertEqual((self.runtime / 'current').resolve(), before)
        _, guides, _ = core.verify_snapshot(before, SITE)
        self.assertEqual(guides['games'][GAME['gameId']]['lastUpdated'], core.iso(NOW))

    def test_nearby_run_retains_accepted_untouched_guides(self):
        self.collect()
        next_game = {**GAME, 'gameId': '1392266', 'week': 3, 'date': '2026-09-27', 'startsAt': '2026-09-27T20:25:00.000Z'}
        self.schedule['games'][next_game['gameId']] = next_game
        result = self.collect('next-run', NOW + timedelta(hours=1))
        _, guides, watch = core.verify_snapshot(Path(result['snapshotPath']), SITE)
        self.assertEqual(len(guides['games']), 2)
        self.assertEqual(guides['games'][GAME['gameId']]['lastUpdated'], core.iso(NOW))
        self.assertEqual(guides['games'][next_game['gameId']]['lastUpdated'], core.iso(NOW + timedelta(hours=1)))

    def test_past_accepted_game_survives_future_research(self):
        self.collect()
        self.schedule['games'][GAME['gameId']]['state'] = 'completed'
        next_game = {**GAME, 'gameId': '1392266', 'week': 3, 'date': '2026-09-27', 'startsAt': '2026-09-27T20:25:00.000Z'}
        self.schedule['games'][next_game['gameId']] = next_game
        result = self.collect('future-run', NOW + timedelta(days=9))
        _, guides, _ = core.verify_snapshot(Path(result['snapshotPath']), SITE)
        self.assertIn(GAME['gameId'], guides['games'])
        self.assertEqual(guides['games'][GAME['gameId']]['lastUpdated'], core.iso(NOW))

    def test_changed_identity_is_researched_again(self):
        self.collect()
        changed = {**GAME, 'startsAt': '2026-09-20T23:25:00.000Z'}
        self.schedule['games'][GAME['gameId']] = changed
        result = self.collect('time-change', NOW + timedelta(hours=1))
        _, guides, watch = core.verify_snapshot(Path(result['snapshotPath']), SITE)
        self.assertEqual(result['generatedGameIds'], [GAME['gameId']])
        self.assertEqual(guides['games'][GAME['gameId']]['game']['startsAt'], changed['startsAt'])
        self.assertEqual(watch['games'][0]['startsAt'], changed['startsAt'])

    def test_checksum_tamper_and_escaped_current_fail_without_publish(self):
        self.collect()
        current = self.runtime / 'current'
        selected = current.resolve()
        (selected / 'watch-guide.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.collect('after-tamper')
        self.assertEqual(current.resolve(), selected)
        current.unlink()
        current.symlink_to(self.runtime.parent)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.collect('escaped')

    def test_nonblocking_per_team_lock(self):
        with core.collection_lock(self.runtime / 'collector.lock'):
            with self.assertRaisesRegex(RuntimeError, 'Another guide collection'):
                self.collect()


if __name__ == '__main__':
    unittest.main()
