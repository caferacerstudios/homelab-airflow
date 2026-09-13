import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile, mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { validateCoverage, bindCoverageToSchedule, collectionDecision } from '../collector-coverage.mjs';
import { runCollector } from '../collector.mjs';

const NE = { slug: 'patriots', name: 'Patriots', city: 'New England', abbreviation: 'NE', balldontlie_team_id: 1 };
const readCoverage = async slug => JSON.parse(await readFile(new URL(`../coverage/${slug}.json`, import.meta.url)));
const coverage = await readCoverage('patriots');
const expected = [
  [1, 'SEA', 'away'], [2, 'PIT', 'home'], [3, 'JAX', 'away'], [4, 'BUF', 'away'],
  [5, 'LV', 'home'], [6, 'NYJ', 'home'], [7, 'CHI', 'away'], [8, 'MIA', 'away'],
  [9, 'GB', 'home'], [10, 'DET', 'away'], [12, 'LAC', 'away'], [13, 'BUF', 'home'],
  [14, 'MIN', 'home'], [15, 'KC', 'away'], [16, 'NYJ', 'away'], [17, 'DEN', 'home'], [18, 'MIA', 'home'],
];

// These synthetic IDs exist only in test memory. Production IDs are supplied by the NFL snapshot.
const gameFor = row => ({ id: 990000 + row.week, week: row.week, season: 2026, phase: 'regular',
  homeTeam: { abbreviation: row.homeTeamAbbreviation }, awayTeam: { abbreviation: row.awayTeamAbbreviation },
  date: row.localDate, status: row.week === 1 ? 'Final' : 'Scheduled',
});
const scheduleFor = rows => ({ fixture: false, team: { abbreviation: 'NE', id: 1 }, season: 2026,
  gamesRegular: rows.map(gameFor) });

test('Patriots coverage includes the actual seventeen regular matchups, no bye or invented source IDs', () => {
  validateCoverage(NE, coverage);
  assert.deepEqual(coverage.map(row => [row.week, row.opponentAbbreviation, row.homeAway]), expected);
  assert.equal(coverage.filter(row => row.homeAway === 'home').length, 8);
  assert.equal(coverage.filter(row => row.state === 'authorized').length, 4);
  assert.equal(coverage.filter(row => row.state === 'unavailable').length, 13);
  assert.ok(coverage.every(row => row.gameId === null));
  for (const row of coverage.filter(row => row.state === 'unavailable')) {
    assert.equal(row.sourceUrl, null);
    assert.equal(row.sourceEventId, null);
    assert.equal(row.reasonCode, 'SOURCE_PAGE_NOT_AVAILABLE');
  }
  for (const week of [17, 18]) assert.equal(coverage.find(row => row.week === week).localDate, null);
});

test('Patriots shared games reuse the exact previously reviewed event and both team identities', async () => {
  for (const [week, slug] of [[1, 'seahawks'], [9, 'packers'], [14, 'vikings'], [15, 'chiefs']]) {
    const original = (await readCoverage(slug)).find(row => row.week === week);
    const selected = coverage.find(row => row.week === week);
    for (const field of ['sourceUrl', 'sourceEventId', 'localDate', 'timeZone', 'homeTeamAbbreviation', 'awayTeamAbbreviation']) {
      assert.equal(selected[field], original[field], `${slug} ${field}`);
    }
    assert.notEqual(selected.homeAway, original.homeAway);
  }
});

test('All Patriots rows bind independently by week and both teams, including repeated division opponents', () => {
  const schedule = scheduleFor(coverage);
  const bindings = bindCoverageToSchedule(NE, coverage, schedule);
  assert.equal(bindings.length, 17);
  assert.ok(bindings.every(binding => binding.reason === null && binding.game));
  assert.equal(new Set(bindings.map(binding => binding.row.gameId)).size, 17);
  const changed = structuredClone(schedule);
  const week16 = changed.gamesRegular.find(game => game.week === 16);
  [week16.homeTeam, week16.awayTeam] = [week16.awayTeam, week16.homeTeam];
  assert.equal(bindCoverageToSchedule(NE, coverage, changed).find(binding => binding.row.week === 16).reason, 'SCHEDULE_GAME_MISSING');
});

test('Munich remains a Detroit home game with Berlin-local date and confirmed kickoff skip', () => {
  const row = coverage.find(row => row.week === 10);
  assert.equal(row.timeZone, 'Europe/Berlin');
  assert.equal(row.homeAway, 'away');
  assert.equal(row.homeTeamAbbreviation, 'DET');
  assert.equal(row.awayTeamAbbreviation, 'NE');
  const game = { ...gameFor(row), startsAt: '2026-11-15T14:30:00Z', timeConfirmed: true };
  const binding = bindCoverageToSchedule(NE, [row], { ...scheduleFor([]), gamesRegular: [game] })[0];
  assert.deepEqual(collectionDecision(binding, Date.parse('2026-11-15T14:00:00Z')), { kind: 'unavailable', reason: 'SOURCE_PAGE_NOT_AVAILABLE' });
  assert.deepEqual(collectionDecision(binding, Date.parse('2026-11-15T14:31:00Z')), { kind: 'skipped', reason: 'GAME_STARTED' });
  const inverted = { ...game, homeTeam: game.awayTeam, awayTeam: game.homeTeam };
  assert.equal(bindCoverageToSchedule(NE, [row], { ...scheduleFor([]), gamesRegular: [inverted] })[0].reason, 'SCHEDULE_GAME_MISSING');
});

test('Completed opener and unavailable Patriots game make no browser requests and retain existing history', async t => {
  const root = await mkdtemp(join(tmpdir(), 'patriots-eventspy-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const outputRoot = join(root, 'patriots'), seattle = join(root, 'seahawks');
  await mkdir(outputRoot); await mkdir(seattle);
  const previous = '{"history":"retained"}\n';
  await writeFile(join(outputRoot, '990001.json'), previous);
  await writeFile(join(seattle, '1392216.json'), previous);
  const rows = coverage.filter(row => [1, 2].includes(row.week));
  const result = await runCollector({ site: NE, coverage: rows, schedule: scheduleFor(rows),
    slot: '2026-09-13T10:00:00.000Z', outputRoot, cacheRoot: join(root, 'cache') }, {
    now: Date.parse('2026-09-13T10:00:00.000Z'), log: () => {},
    browserFactory: () => { throw new Error('Browser must not start'); },
    fetchEvent: () => { throw new Error('Source must not be requested'); },
  });
  assert.equal(result.skipped, 1);
  assert.equal(result.unavailable, 1);
  assert.equal(result.failed, 0);
  assert.equal(await readFile(join(outputRoot, '990001.json'), 'utf8'), previous);
  assert.equal(await readFile(join(seattle, '1392216.json'), 'utf8'), previous);
  const unavailable = JSON.parse(await readFile(join(outputRoot, '990002.json')));
  assert.equal(unavailable.gameId, '990002');
  assert.equal(unavailable.state, 'unavailable');
  assert.equal(unavailable.source, 'eventspy');
});
