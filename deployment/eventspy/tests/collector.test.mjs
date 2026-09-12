import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, mkdir, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { buildSnapshot, runCollector, fetchEvent } from "../collector.mjs";
import { bindCoverageToSchedule, collectionDecision, validateCoverage } from "../collector-coverage.mjs";

const SEA = { slug: "seahawks", city: "Seattle", name: "Seahawks", abbreviation: "SEA" };
const DEN = { slug: "broncos", city: "Denver", name: "Broncos", abbreviation: "DEN", timezone: "America/Denver" };
const seahawks = JSON.parse(await readFile(new URL("../coverage/seahawks.json", import.meta.url)));
const broncos = JSON.parse(await readFile(new URL("../coverage/broncos.json", import.meta.url)));
const now = Date.parse("2026-09-12T10:00:00.000Z");
const slot = new Date(now).toISOString();
const row = seahawks[1];

function payloadFor(row, title = `Seattle Seahawks vs ${row.opponent}`) {
  return {
    event: {
      id: Number(row.sourceEventId), eventName: title, eventDateLocal: row.localDate,
      eventDateTimeUTC: `${row.localDate}T20:00:00Z`, eventTimezone: row.timeZone,
      venueName: "Example Stadium", venueCity: "Glendale", venueState: "AZ",
      currentPrice: 40.10, currentPriceVendor: "Ticketmaster", currentPriceSeenAt: "2026-09-12T09:00:00.000Z",
      sevenDayLowest: 35.01, sevenDayLowestSeenAt: "2026-09-11T10:00:00.000Z",
      ticketmasterUrl: "https://www.ticketmaster.com/event/123", stubhubUrl: "https://www.stubhub.com/event/123",
      vividseatsUrl: "https://www.vividseats.com/event/123", seatgeekUrl: "https://seatgeek.com/event/123",
    },
    history: { priceHistory: {
      ticketmaster: [{ seenAt: "2026-09-11T10:00:00.000Z", lowestPrice: 35.01 }, { seenAt: "2026-09-12T09:00:00.000Z", lowestPrice: 40.10 }],
      stubhub: [{ seenAt: "2026-09-12T09:00:00.000Z", lowestPrice: 45.02 }], vividseats: [], seatgeek: [],
    } },
  };
}

function gameFor(row, status = "Scheduled") {
  return { id: row.gameId ?? "998877", season: row.season, week: row.week, phase: "regular", status,
    homeTeam: { abbreviation: row.homeTeamAbbreviation }, awayTeam: { abbreviation: row.awayTeamAbbreviation }, date: row.localDate };
}
function scheduleFor(site, games) { return { fixture: false, season: 2026, team: { abbreviation: site.abbreviation }, gamesRegular: games }; }
async function environment(t, site = SEA, coverage = [row]) {
  const root = await mkdtemp(join(tmpdir(), "eventspy-test-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  return { site, coverage, slot, outputRoot: join(root, "output"), cacheRoot: join(root, "cache") };
}
function mocks(payload = payloadFor(row)) {
  const calls = { launches: 0, fetches: [], bootstrap: [], closes: 0 };
  const page = { goto: async url => { calls.bootstrap.push(url); return { status: () => 200 }; }, waitForTimeout: async () => {}, close: async () => {} };
  const dependencies = {
    now, log: () => {},
    browserFactory: async () => { calls.launches++; return { newContext: async () => ({ newPage: async () => page, close: async () => {} }), close: async () => { calls.closes++; } }; },
    fetchEvent: async (_page, id) => { calls.fetches.push(id); return structuredClone(payload); },
  };
  return { calls, dependencies };
}

test("Seattle normalizer retains the deployed exact snapshot keys and values", () => {
  assert.deepEqual(buildSnapshot(row, payloadFor(row), now, SEA), {
    schemaVersion: "1.0.0", source: "eventspy", currency: "USD", gameId: "1392244", sourceEventId: "374512",
    sourceUrl: row.sourceUrl, trackingUrl: row.sourceUrl, collectedAt: slot,
    event: { title: "Seattle Seahawks vs Arizona Cardinals", venue: "Example Stadium", location: "Glendale, AZ", localDate: "2026-09-20", localTimeLabel: "1:00 PM MST" },
    summary: {
      daysUntilEvent: 8, currentLowestCents: 4010, currentLowestMarketplace: "ticketmaster", currentLowestObservedAt: "2026-09-12T09:00:00.000Z", currentLowestAgeLabel: "1 hr ago",
      sevenDayLowestCents: 3501, sevenDayLowestObservedAt: "2026-09-11T10:00:00.000Z", sevenDayLowestAgeLabel: "1 day ago", atSevenDayLow: false,
    },
    providerLinks: { ticketmaster: "https://www.ticketmaster.com/event/123", stubhub: "https://www.stubhub.com/event/123", vividseats: "https://www.vividseats.com/event/123", seatgeek: "https://seatgeek.com/event/123" },
    history: [
      { observedAt: "2026-09-11T10:00:00.000Z", ticketmasterCents: 3501, stubhubCents: null, vividseatsCents: null, seatgeekCents: null },
      { observedAt: "2026-09-12T09:00:00.000Z", ticketmasterCents: 4010, stubhubCents: 4502, vividseatsCents: null, seatgeekCents: null },
    ],
  });
});

test("deployed fetch API calls retain GET event and POST history with credentials", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => { calls.push([url, options]); return { ok: true, json: async () => ({ ok: true }) }; };
  try {
    await fetchEvent({ evaluate: async (fn, id) => fn(id) }, "374512");
    assert.deepEqual(calls, [
      ["https://api.event-spy.com/api/app/event/374512", { method: "GET", credentials: "include" }],
      ["https://api.event-spy.com/api/app/event/374512/history", { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: "{}" }],
    ]);
  } finally { globalThis.fetch = originalFetch; }
});

test("completed games preserve existing files and make no browser or source requests", async t => {
  const config = await environment(t);
  await mkdir(config.outputRoot);
  const old = '{"existing":"preserved byte for byte"}\n';
  await writeFile(join(config.outputRoot, `${row.gameId}.json`), old);
  config.schedule = scheduleFor(SEA, [gameFor(row, "Final")]);
  const { calls, dependencies } = mocks();
  const summary = await runCollector(config, dependencies);
  assert.equal(summary.skipped, 1); assert.equal(summary.failed, 0);
  assert.equal(summary.results[0].reason, "GAME_COMPLETED");
  assert.equal(calls.launches, 0); assert.deepEqual(calls.fetches, []);
  assert.equal(await readFile(join(config.outputRoot, `${row.gameId}.json`), "utf8"), old);
});

test("bootstrap selects first future game rather than played Week 1", async t => {
  const config = await environment(t, SEA, [seahawks[0], row]);
  const { calls, dependencies } = mocks();
  const summary = await runCollector(config, dependencies);
  assert.equal(summary.skipped, 1); assert.equal(summary.succeeded, 1);
  assert.deepEqual(calls.bootstrap, [row.sourceUrl]); assert.deepEqual(calls.fetches, [row.sourceEventId]);
});

test("completed statuses and past local calendar fallback; postponements are unresolved", () => {
  for (const status of ["Final", "Final/OT", "finished", "complete", "completed", "closed"]) {
    assert.equal(collectionDecision({ row, game: { status }, reason: null }, now).reason, "GAME_COMPLETED");
  }
  assert.equal(collectionDecision({ row, game: { status_state: "final" }, reason: null }, now).reason, "GAME_COMPLETED");
  for (const status of ["postponed", "rescheduled", "suspended", "canceled", "delayed"]) {
    assert.deepEqual(collectionDecision({ row: seahawks[0], game: { status }, reason: null }, now), { kind: "unresolved", reason: "SCHEDULE_CHANGED" });
  }
  assert.equal(collectionDecision({ row: seahawks[0], game: null, reason: null }, now).reason, "PAST_GAME_DATE");
  assert.equal(collectionDecision({ row: { ...row, localDate: "2026-09-12" }, game: null, reason: null }, now).kind, "collect");
  assert.equal(collectionDecision({ row, game: { date: "2026-09-21" }, reason: null }, now).reason, "SCHEDULE_DATE_CHANGED");
});

test("all unresolved Bronx IDs do not launch browser or publish null.json", async t => {
  const config = await environment(t, DEN, [broncos[0], broncos[16]]);
  const { calls, dependencies } = mocks();
  const summary = await runCollector(config, dependencies);
  assert.equal(summary.unresolved, 2); assert.equal(calls.launches, 0);
  await assert.rejects(readdir(config.outputRoot), { code: "ENOENT" });
});

test("cached pregame status cannot request tickets after a confirmed kickoff", async t => {
  const denRow = { ...broncos[1], gameId: "998877" };
  const config = await environment(t, DEN, [denRow]);
  const game = { ...gameFor(denRow), date: "2026-09-20T20:05:00Z", status_state: "scheduled" };
  config.schedule = scheduleFor(DEN, [game]);
  config.slot = "2026-09-21T01:00:00.000Z";
  const { calls, dependencies } = mocks();
  dependencies.now = Date.parse(config.slot);
  const summary = await runCollector(config, dependencies);
  assert.equal(summary.skipped, 1); assert.equal(summary.results[0].reason, "GAME_STARTED");
  assert.equal(calls.launches, 0); assert.deepEqual(calls.fetches, []);
});

test("TBD, unconfirmed, date-only, and placeholder midnight do not infer kickoff", () => {
  const denRow = broncos[1];
  const after = Date.parse("2026-09-21T01:00:00Z");
  const base = { date: "2026-09-20T20:05:00Z", status: "Scheduled" };
  for (const game of [
    { ...base, status: "TBD" }, { ...base, timeConfirmed: false }, { ...base, time_tbd: true },
    { ...base, date: "2026-09-20" }, { ...base, date: "2026-09-20T20:05:00" },
    { ...base, date: "2026-09-21T00:00:00Z" },
  ]) {
    assert.notEqual(collectionDecision({ row: denRow, game, reason: null }, after).reason, "GAME_STARTED");
  }
  assert.equal(collectionDecision({ row: denRow, game: { ...base, status: "Postponed" }, reason: null }, after).reason, "SCHEDULE_CHANGED");
  assert.equal(collectionDecision({ row: denRow, game: { ...base, date: "2026-09-19T20:05:00Z" }, reason: null }, after).reason, "SCHEDULE_DATE_CHANGED");
  assert.equal(collectionDecision({ row: denRow, game: { ...base, status: "Final" }, reason: null }, after).reason, "GAME_COMPLETED");
});

test("only unique genuine matching team/season/week/game identity binds a Broncos ID", () => {
  const match = gameFor(broncos[0]);
  assert.equal(bindCoverageToSchedule(DEN, [broncos[0]], scheduleFor(DEN, [match]))[0].row.gameId, "998877");
  assert.equal(bindCoverageToSchedule(DEN, [broncos[0]], scheduleFor(DEN, [match, { ...match, id: "998878" }]))[0].reason, "SCHEDULE_GAME_AMBIGUOUS");
  for (const wrong of [{ ...match, week: 2 }, { ...match, season: 2025 }, { ...match, phase: "preseason" }, { ...match, homeTeam: { abbreviation: "SEA" } }]) {
    assert.equal(bindCoverageToSchedule(DEN, [broncos[0]], scheduleFor(DEN, [wrong]))[0].reason, "SCHEDULE_GAME_MISSING");
  }
  assert.throws(() => bindCoverageToSchedule(DEN, [broncos[0]], { ...scheduleFor(DEN, [match]), fixture: true }), /non-fixture/);
  assert.throws(() => bindCoverageToSchedule(DEN, [broncos[0]], scheduleFor(SEA, [match])), /selected team/);
  const fallback = { ...scheduleFor(DEN, []), games: [match] };
  assert.equal(bindCoverageToSchedule(DEN, [broncos[0]], fallback)[0].row.gameId, "998877");
});

test("shared Seahawks-at-Broncos source is fetched once per slot and validated for both teams", async t => {
  const seaRow = seahawks[5], denRow = broncos[5];
  const config = await environment(t, SEA, [seaRow]);
  const { calls, dependencies } = mocks(payloadFor(seaRow, "Denver Broncos vs Seattle Seahawks"));
  assert.equal((await runCollector(config, dependencies)).succeeded, 1);
  const denOutput = `${config.outputRoot}-denver`;
  const second = await runCollector({ ...config, site: DEN, coverage: [denRow], outputRoot: denOutput }, dependencies);
  assert.equal(second.succeeded, 1); assert.equal(second.results[0].cached, true);
  assert.deepEqual(calls.fetches, ["374655"]); assert.equal(calls.launches, 1);
  const a = JSON.parse(await readFile(join(config.outputRoot, "1392295.json")));
  const b = JSON.parse(await readFile(join(denOutput, "1392295.json")));
  assert.deepEqual(a, b); assert.equal(a.team, undefined);
});

test("failed attempt is durable and cannot make a second source request in same slot", async t => {
  const config = await environment(t);
  const { calls, dependencies } = mocks();
  dependencies.fetchEvent = async () => { calls.fetches.push("failed"); throw new Error("source down"); };
  assert.equal((await runCollector(config, dependencies)).failed, 1);
  const repeat = await runCollector(config, dependencies);
  assert.equal(repeat.failed, 1); assert.match(repeat.results[0].error, /retry suppressed/);
  assert.deepEqual(calls.fetches, ["failed"]); assert.equal(calls.launches, 1);
});

test("seven source attempts cap is shared across team invocations for the Pacific day", async t => {
  const config = await environment(t);
  const { calls, dependencies } = mocks();
  for (let hour = 10; hour < 17; hour++) {
    assert.equal((await runCollector({ ...config, slot: `2026-09-12T${hour}:00:00.000Z` }, dependencies)).succeeded, 1);
  }
  const eighth = await runCollector({ ...config, slot: "2026-09-12T17:00:00.000Z" }, dependencies);
  assert.equal(eighth.failed, 1); assert.match(eighth.results[0].error, /seven attempts/);
  assert.equal(calls.fetches.length, 7); assert.equal(calls.launches, 7);
});

test("unavailable document preserves the actual deployed format without opponent fields", async t => {
  const unavailable = seahawks[16];
  const config = await environment(t, SEA, [unavailable]);
  const { calls, dependencies } = mocks();
  assert.equal((await runCollector(config, dependencies)).unavailable, 1);
  assert.equal(calls.launches, 0);
  assert.deepEqual(JSON.parse(await readFile(join(config.outputRoot, `${unavailable.gameId}.json`))), {
    schemaVersion: "1.0.0", source: "eventspy", gameId: "1392478", state: "unavailable", reasonCode: "SOURCE_PAGE_NOT_AVAILABLE", collectedAt: slot,
  });
});

test("tailgates, parking, wrong team/date, and provider credentials cannot become snapshots", () => {
  for (const title of ["VIP Tailgate: Seattle Seahawks vs Arizona Cardinals", "Parking Seattle Seahawks Arizona Cardinals", "Denver Broncos vs Arizona Cardinals"]) {
    assert.throws(() => buildSnapshot(row, payloadFor(row, title), now, SEA), /matchup/);
  }
  const wrongDate = payloadFor(row); wrongDate.event.eventDateLocal = "2026-09-21";
  assert.throws(() => buildSnapshot(row, wrongDate, now, SEA), /date mismatch/);
  const credentials = payloadFor(row); credentials.event.ticketmasterUrl += "?api_key=secret";
  assert.throws(() => buildSnapshot(row, credentials, now, SEA), /provider URL/);
  assert.throws(() => validateCoverage(SEA, [{ ...row, sourceUrl: "https://www.event-spy.com/event/parking-seattle/374512" }]), /Unsafe/);
});

test("checked coverage preserves SEA17 and DEN15 games plus two unresolved event pages", () => {
  assert.equal(validateCoverage(SEA, seahawks).length, 17);
  assert.equal(validateCoverage(DEN, broncos).length, 17);
  assert.equal(broncos.filter(row => row.state === "authorized").length, 15);
  assert.equal(broncos.filter(row => row.sourceEventId === "379022").length, 0);
  assert.equal(broncos.find(row => row.week === 8).sourceEventId, "374756");
});


const fixtureUrl = new URL("../../../tests/eventspy/fixtures/seattle-2026-recorded-identity.json", import.meta.url);
const fixtureBytes = await readFile(fixtureUrl);
const recorded = JSON.parse(fixtureBytes);
const site = { slug: "seahawks", city: "Seattle", name: "Seahawks", abbreviation: "SEA" };
const coverage = JSON.parse(await readFile(new URL("../coverage/seahawks.json", import.meta.url)));
const recordedIds = [
  "1392216", "1392244", "1392256", "1392277", "1392292", "1392295",
  "1392321", "1392336", "1392349", "1392361", "1392392", "1392408",
  "1392421", "1392425", "1392443", "1392467", "1392478",
];

function freeze(value) {
  if (value && typeof value === "object") {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

test("recorded Seattle snapshot binds all 17 actual game IDs, ignores the bye, and preserves input", async () => {
  const schedule = freeze(structuredClone(recorded));
  const rows = freeze(structuredClone(coverage));
  const beforeSchedule = JSON.stringify(schedule), beforeRows = JSON.stringify(rows);
  assert.equal(schedule.gamesRegular.length, 18);
  assert.equal(schedule.gamesRegular.filter(game => game.bye === true).length, 1);
  assert.equal(schedule.gamesRegular.find(game => game.week === 3).homeTeam.abbreviation, "WSH");
  assert.equal(rows.find(row => row.week === 3).homeTeamAbbreviation, "WAS");
  const bindings = bindCoverageToSchedule(site, rows, schedule);
  assert.equal(bindings.length, 17);
  assert.deepEqual(bindings.map(binding => binding.reason), Array(17).fill(null));
  assert.deepEqual(bindings.map(binding => binding.row.gameId), recordedIds);
  assert.deepEqual(bindings.map(binding => String(binding.game.id)), recordedIds);
  assert.equal(JSON.stringify(schedule), beforeSchedule);
  assert.equal(JSON.stringify(rows), beforeRows);
  assert.deepEqual(await readFile(fixtureUrl), fixtureBytes);
});

test("Washington aliases bind in either coverage/schedule direction without changing abbreviations", () => {
  for (const [expected, actual] of [["WAS", "WSH"], ["WSH", "WAS"]]) {
    const row = structuredClone(coverage.find(entry => entry.week === 3));
    row.homeTeamAbbreviation = expected;
    row.opponentAbbreviation = expected;
    const game = structuredClone(recorded.gamesRegular.find(entry => entry.week === 3));
    game.homeTeam.abbreviation = actual;
    game.home_team.abbreviation = actual;
    const schedule = { ...recorded, gamesRegular: [game] };
    const binding = bindCoverageToSchedule(site, freeze([row]), freeze(schedule))[0];
    assert.equal(binding.reason, null, `${expected} coverage / ${actual} schedule`);
    assert.equal(binding.row.gameId, "1392256");
    assert.equal(binding.row.homeTeamAbbreviation, expected);
    assert.equal(binding.game.homeTeam.abbreviation, actual);
  }
});

test("Washington alias acceptance still rejects wrong game ID, week, season, and teams", () => {
  const changes = [
    game => { game.id = "1392257"; },
    game => { game.week = 4; },
    game => { game.season = 2025; },
    game => { game.homeTeam.abbreviation = "DEN"; game.home_team.abbreviation = "DEN"; },
    game => { game.awayTeam.abbreviation = "DEN"; game.visitor_team.abbreviation = "DEN"; },
  ];
  for (const change of changes) {
    const schedule = structuredClone(recorded);
    change(schedule.gamesRegular.find(game => game.week === 3));
    const bindings = bindCoverageToSchedule(site, coverage, schedule);
    assert.equal(bindings.filter(binding => binding.reason === null).length, 16);
    assert.equal(bindings.find(binding => binding.row.week === 3).reason, "SCHEDULE_GAME_MISSING");
    assert.equal(bindings.find(binding => binding.row.week === 3).game, null);
  }
});
