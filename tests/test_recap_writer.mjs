// Run with node --test tests/test_recap_writer.mjs. Fixtures make no live requests.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
import { test } from "node:test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const env = {
  TEAM: "broncos", NFL_TEAM_ABBR: "DEN", NFL_TEAM_ID: "7",
  TEAM_NAME: "Broncos", TEAM_CITY: "Denver", RECAP_PROMPT: "Focus on Denver's passing efficiency.",
  BALLDONTLIE_API_KEY: "fixture-bdl", OPENAI_API_KEY: "fixture-ai",
};
const game = (id, home = "DEN", away = "SEA", status = "Final") => ({
  id, season: 2026, phase: "regular", week: 1, date: "2026-09-13", status,
  home_team: { id: home === "DEN" ? 7 : 31, abbreviation: home },
  visitor_team: { id: away === "DEN" ? 7 : 31, abbreviation: away },
  home_team_score: 24, visitor_team_score: 17,
});
const complete = (text) => ({ segments: [{ t: "text", v: text }], bullets: ["Historical highlight"] });
const generated = {
  segments: [{ t: "text", v: "Denver won 24–17.\n\nOnly supplied game facts are used.", id: null, name: null }],
  bullets: ["Denver scored 24 points.", "The visitors scored 17 points.", "Denver won by seven points."],
};
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

async function fixture(t, schedule = {}) {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "airflow-recap-writer-"));
  t.after(() => fs.rmSync(work, { recursive: true, force: true }));
  fs.mkdirSync(path.join(work, "scripts"));
  for (const name of ["generate-game-recaps.mjs", "nfl-api-client.mjs", "recap-artifacts.mjs", "recap-schedule.mjs"]) {
    fs.copyFileSync(path.join(root, "deployment/recaps", name), path.join(work, "scripts", name));
  }
  const nfl = path.join(work, "src/data/nfl");
  fs.mkdirSync(nfl, { recursive: true });
  fs.writeFileSync(path.join(nfl, "seahawks.json"), JSON.stringify({
    season: 2026, team: { id: 7, abbreviation: "DEN" }, gamesRegular: [game(1001)], ...schedule,
  }));
  const writer = await import(pathToFileURL(path.join(work, "scripts/generate-game-recaps.mjs")));
  return { work, nfl, generate: writer.generateGameRecaps };
}

function provider({ playsStatus = 200, modelStatus = 200 } = {}) {
  const calls = [];
  const inputs = [];
  return {
    calls, inputs,
    fetch: async (url, options) => {
      const target = new URL(url);
      calls.push(target);
      if (target.hostname === "api.balldontlie.io" && target.pathname.endsWith("/stats")) {
        return json({ data: [{ player: { id: 9, full_name: "Fixture Quarterback" }, passing_yards: 250 }], meta: { next_cursor: null } });
      }
      if (target.hostname === "api.balldontlie.io" && target.pathname.endsWith("/plays")) {
        return json({ data: [{ period: 2, clock_display: "03:00", text: "Fixture touchdown", scoring_play: true }], meta: {} }, playsStatus);
      }
      assert.equal(target.href, "https://api.openai.com/v1/responses", "No unmocked network requests are permitted");
      inputs.push(JSON.parse(options.body));
      return json({ status: "completed", output_text: JSON.stringify(generated) }, modelStatus);
    },
  };
}

test("Denver selects only its completed games and sends its configured grounded prompt", async (t) => {
  const f = await fixture(t, { gamesRegular: [game(1001), game(1002, "SEA", "ARI"), game(1003, "DEN", "SEA", "Scheduled")] });
  const p = provider();
  const sleeps = [];
  const report = await f.generate({ projectRoot: f.work, env: { ...env, RECAP_GENERATION_REPORT: path.join(f.work, "report.json") }, fetchImpl: p.fetch, sleep: async (ms) => sleeps.push(ms), now: () => Date.parse("2026-09-15T12:00:00Z") });
  assert.deepEqual(report.generatedGameIds, ["1001"]);
  assert.equal(report.team, "broncos");
  assert.equal(report.requestCount, 2);
  assert.equal(report.openaiRequestCount, 1);
  assert.equal(report.model, "gpt-4o-mini");
  assert.deepEqual(sleeps, [15000]);
  assert.equal(p.calls[0].searchParams.get("game_ids[]"), "1001");
  const request = p.inputs[0];
  assert.equal(request.max_output_tokens, 6000, "Preserve the existing weekly writer's output budget");
  assert.match(request.input[0].content, /Denver Broncos/);
  assert.match(request.input[0].content, /600–900/);
  assert.match(request.input[0].content, /7–10/);
  assert.match(request.input[0].content, /Passing interceptions are interceptions thrown/);
  assert.match(request.input[0].content, /Missing or null values are unknown, not zero/);
  assert.match(request.input[0].content, /avoid today, tonight, yesterday and last night/);
  assert.match(request.input[0].content, /Never invent drives/);
  assert.doesNotMatch(request.input[0].content, /Seattle/);
  const facts = JSON.parse(request.input[1].content);
  assert.equal(facts.game.team, "broncos");
  assert.equal(facts.game.team_is_home, true);
  assert.equal(facts.instructions.editorial_brief, env.RECAP_PROMPT);
  assert.match(facts.instructions.coverage.join(" "), /Denver's passing game/);
  assert.doesNotMatch(facts.instructions.coverage.join(" "), /Seattle/);
  assert.match(facts.instructions.segments_rule, /first mention/);
  assert.match(facts.instructions.segments_rule, /Never put object notation in a v string/);
  assert.match(facts.instructions.highlights_rule, /plain-text strings/);
  assert.equal(facts.candidate_players[0].name, "Fixture Quarterback");
  const output = JSON.parse(fs.readFileSync(path.join(f.nfl, "gameRecaps.json")));
  assert.equal(output.team, "broncos");
  assert.equal(output.recaps[1001].team, "broncos");
  assert.deepEqual(output.recaps[1001].segments, generated.segments);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.work, "report.json"))), report);
});

test("legacy Seattle complete recaps and historical prose are retained without API requests", async (t) => {
  const f = await fixture(t, { team: { id: 31, abbreviation: "SEA" }, gamesRegular: [game(1001, "SEA", "DEN")] });
  const previous = { season: 2026, recaps: { 1001: complete("Edited recap stays unchanged."), historical: complete("Historical prose stays unchanged.") } };
  fs.writeFileSync(path.join(f.nfl, "gameRecaps.json"), JSON.stringify(previous));
  const report = await f.generate({ projectRoot: f.work, env: {}, fetchImpl: async () => { throw new Error("No API call expected"); } });
  assert.equal(report.generatedCount, 0);
  assert.equal(report.requestCount, 0);
  assert.equal(report.openaiRequestCount, 0);
  const output = JSON.parse(fs.readFileSync(path.join(f.nfl, "gameRecaps.json")));
  for (const [id, recap] of Object.entries(previous.recaps)) {
    assert.deepEqual(output.recaps[id].segments, recap.segments);
    assert.deepEqual(output.recaps[id].bullets, recap.bullets);
    assert.equal(output.recaps[id].team, "seahawks");
  }
});

test("configurable source filename and away-team perspective work for Patriots", async (t) => {
  const patriots = game(1001, "DEN", "NE");
  patriots.visitor_team.id = 17;
  const f = await fixture(t, { team: { id: 17, abbreviation: "NE" }, gamesRegular: [patriots] });
  fs.renameSync(path.join(f.nfl, "seahawks.json"), path.join(f.nfl, "patriots.json"));
  const p = provider();
  await f.generate({ projectRoot: f.work, env: { ...env, TEAM: "patriots", NFL_TEAM_ABBR: "NE", NFL_TEAM_ID: "17", TEAM_NAME: "Patriots", TEAM_CITY: "New England", NFL_DATA_FILE: "patriots.json", NFL_REQUEST_INTERVAL_MS: "0" }, fetchImpl: p.fetch });
  const facts = JSON.parse(p.inputs[0].input[1].content);
  assert.equal(facts.game.team_name, "New England Patriots");
  assert.equal(facts.game.team_is_home, false);
  assert.equal(facts.game.opponent, "DEN");
});

test("a 401 from play-by-play preserves the stats-only fallback", async (t) => {
  const f = await fixture(t);
  const p = provider({ playsStatus: 401 });
  await f.generate({ projectRoot: f.work, env: { ...env, NFL_REQUEST_INTERVAL_MS: "0" }, fetchImpl: p.fetch });
  const facts = JSON.parse(p.inputs[0].input[1].content);
  assert.deepEqual(facts.key_plays, []);
  assert.match(p.inputs[0].input[0].content, /If key_plays is empty, do not describe specific plays/);
});

test("mismatched schedule identity or recap team fails before generation", async (t) => {
  const f = await fixture(t);
  let calls = 0;
  const fetchImpl = async () => { calls++; throw new Error("No request expected"); };
  await assert.rejects(f.generate({ projectRoot: f.work, env: { ...env, NFL_TEAM_ID: "31" }, fetchImpl }), /does not identify/);
  fs.writeFileSync(path.join(f.nfl, "gameRecaps.json"), JSON.stringify({ season: 2026, team: "seahawks", recaps: {} }));
  await assert.rejects(f.generate({ projectRoot: f.work, env, fetchImpl }), /another team/);
  assert.equal(calls, 0);
});

test("a failed model response never overwrites the last complete recap snapshot", async (t) => {
  const f = await fixture(t);
  const previous = JSON.stringify({ season: 2026, team: "broncos", recaps: { old: { team: "broncos", ...complete("Keep this older recap.") } } });
  const outputPath = path.join(f.nfl, "gameRecaps.json");
  fs.writeFileSync(outputPath, previous);
  const p = provider({ modelStatus: 503 });
  await assert.rejects(f.generate({ projectRoot: f.work, env: { ...env, NFL_REQUEST_INTERVAL_MS: "0" }, fetchImpl: p.fetch }), /OpenAI recap HTTP 503/);
  assert.equal(fs.readFileSync(outputPath, "utf8"), previous);
});

for (const [slug, abbreviation, city, name, id] of [
  ["packers", "GB", "Green Bay", "Packers", 99],
  ["vikings", "MIN", "Minnesota", "Vikings", 98],
  ["chiefs", "KC", "Kansas City", "Chiefs", 97],
]) {
  test(`${city} ${name} gets only its own completed-game prompt and preserves opponent names`, async (t) => {
    // Deliberately synthetic provider IDs: no fixture ID is used as configuration.
    const played = game(1001, abbreviation, "SEA");
    played.home_team.id = id;
    played.visitor_team.full_name = "Seattle Seahawks";
    const unplayed = { ...played, id: 1002, status: "In Progress" };
    const preseason = { ...played, id: 1003, phase: "preseason" };
    const f = await fixture(t, { team: { id, abbreviation }, gamesRegular: [played, unplayed, preseason] });
    fs.renameSync(path.join(f.nfl, "seahawks.json"), path.join(f.nfl, `${slug}.json`));
    const p = provider();
    const report = await f.generate({ projectRoot: f.work,
      env: { ...env, TEAM: slug, NFL_TEAM_ABBR: abbreviation, NFL_TEAM_ID: String(id),
        TEAM_NAME: name, TEAM_CITY: city, NFL_DATA_FILE: `${slug}.json`, NFL_REQUEST_INTERVAL_MS: "0",
        RECAP_PROMPT: `Write for ${city} ${name}.` }, fetchImpl: p.fetch });
    assert.deepEqual(report.generatedGameIds, ["1001"]);
    assert.equal(report.team, slug);
    assert.ok(p.inputs[0].input[0].content.includes(`${city} ${name}`));
    const output = JSON.parse(fs.readFileSync(path.join(f.nfl, "gameRecaps.json")));
    assert.equal(output.recaps[1001].game.visitor_team.full_name, "Seattle Seahawks");
    assert.equal(output.recaps[1001].team, slug);
  });
}

test("a purported final game without verified scores fails before source or model requests", async (t) => {
  const played = { ...game(1001), home_team_score: null };
  const f = await fixture(t, { gamesRegular: [played] });
  await assert.rejects(f.generate({ projectRoot: f.work, env,
    fetchImpl: async () => { throw new Error("No API calls expected"); } }), /lacks verified final scores/);
});


test("a shared game ID cannot relabel untagged Seattle prose for Denver", async (t) => {
  const f = await fixture(t);
  fs.writeFileSync(path.join(f.nfl, "gameRecaps.json"), JSON.stringify({
    season: 2026, recaps: { 1001: complete("Seattle perspective stays Seattle.") },
  }));
  await assert.rejects(f.generate({ projectRoot: f.work, env,
    fetchImpl: async () => { throw new Error("No API call expected"); } }), /Cannot identify the team/);
});


test("ambiguous incomplete statuses do not count as final games", async (t) => {
  const f = await fixture(t, { gamesRegular: [game(1001, "DEN", "SEA", "Incomplete"), game(1002, "DEN", "SEA", "Not Final")] });
  const report = await f.generate({ projectRoot: f.work, env,
    fetchImpl: async () => { throw new Error("No API call expected"); } });
  assert.equal(report.generatedCount, 0);
  assert.equal(report.requestCount, 0);
});
