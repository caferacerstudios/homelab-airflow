#!/usr/bin/env node
// Official club roster, injury observations and complete transaction-log rows.
// This module performs no statistical aggregation and makes no provider/LLM calls.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

const teams = JSON.parse(fs.readFileSync(new URL('./teams.json', import.meta.url), 'utf8'));
const digest = value => createHash('sha256').update(String(value)).digest('hex');
const dayMs = 86_400_000;
export const ROSTER_STATUSES = ['Active', 'Practice Squad', 'Reserve/Injured', 'PUP', 'Commissioner Exempt', 'Suspended', 'Reserve/Non-Football Injury', 'Released', 'Waived', 'Historical'];
const departed = new Set(['Released', 'Waived', 'Historical']);
const absent = /^(?:\(?\s*[-–—]+\s*\)?|(?:un)?specified|not\s+(?:listed|specified)|n\/?a|none)?$/i;

export function clean(value) {
  return String(value ?? '').replace(/<[^>]*>/g, ' ').replace(/&#(?:x([0-9a-f]+)|(\d+));/gi, (entity, hex, dec) => {
    const n = Number.parseInt(hex ?? dec, hex ? 16 : 10);
    return Number.isInteger(n) && n >= 0 && n <= 0x10ffff ? String.fromCodePoint(n) : entity;
  }).replace(/&nbsp;/gi, ' ').replace(/&amp;/gi, '&').replace(/&apos;/gi, "'").replace(/&quot;/gi, '"').replace(/\s+/g, ' ').trim();
}
export const identityKey = value => clean(value).normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/\b(jr|sr|ii|iii|iv)\b\.?/g, '').replace(/[^a-z0-9]+/g, '');
const slug = value => clean(value).normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/['’]/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
const tableRows = table => [...table.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)].map(row => ({ html: row[1], cells: [...row[1].matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/gi)].map(cell => clean(cell[1])) }));
function links(html) {
  return [...html.matchAll(/<a\b[^>]*href=["'](?:https:\/\/[^/"']+)?\/team\/players-roster\/([^/"'?]+)\/?["'][^>]*>([\s\S]*?)<\/a>/gi)]
    .map(row => ({ sourceId: row[1], name: clean(row[2]) })).filter(row => row.name);
}
function rosterStatus(source) {
  const status = clean(source).toLowerCase();
  if (status === 'active') return 'Active';
  if (/^practice squad(?:\/international)?$/.test(status)) return 'Practice Squad';
  if (/^reserve\/(?:injured(?:; designated for return)?|designated to return)$/.test(status)) return 'Reserve/Injured';
  if (/^(?:reserve\/)?(?:physically unable to perform|pup)$/.test(status)) return 'PUP';
  if (/^(?:commissioner exempt|exempt\/commissioner permission)$/.test(status)) return 'Commissioner Exempt';
  if (/^reserve\/suspended(?: by commissioner)?$/.test(status)) return 'Suspended';
  if (/^reserve\/non-football (?:injury|illness)$/.test(status)) return 'Reserve/Non-Football Injury';
  throw new Error(`Unrecognized official roster section: ${source}`);
}
function verifyClub(html, team) {
  const title = clean(html.match(/<title\b[^>]*>([\s\S]*?)<\/title>/i)?.[1]);
  if (!title || !title.toLowerCase().includes(team.name.toLowerCase())) throw new Error(`Official page did not identify ${team.name}`);
}
export function parseRoster(html, team) {
  verifyClub(html, team);
  const players = [];
  for (const match of html.matchAll(/<div\b[^>]*class=["'][^"']*\bnfl-o-roster(?:\s|["'])[^>]*>([\s\S]*?)(?=<div\b[^>]*class=["'][^"']*\bnfl-o-roster(?:\s|["'])|$)/gi)) {
    const block = match[1];
    const sourceStatus = clean(block.match(/nfl-o-roster__title-status[^>]*>([\s\S]*?)<\/span>/i)?.[1] ?? block.match(/<caption[^>]*>([\s\S]*?)<\/caption>/i)?.[1]);
    const rows = tableRows(block).filter(row => row.cells.length >= 3 && links(row.html).length);
    if (!rows.length) continue;
    const status = rosterStatus(sourceStatus);
    for (const row of rows) {
      const player = links(row.html)[0];
      if (!row.cells[2]) throw new Error('Official roster player has no position');
      players.push({ ...player, position: row.cells[2], number: /^\d+$/.test(row.cells[1]) ? Number(row.cells[1]) : null, status, sourceStatus });
    }
  }
  if (!players.length) throw new Error('Official roster response contains no recognizable roster records');
  const names = players.map(row => `${identityKey(row.name)}:${row.position}`);
  if (new Set(names).size !== names.length || new Set(players.map(row => row.sourceId)).size !== players.length) throw new Error('Duplicate official roster identities');
  return players;
}
function uniqueMatch(rows, predicate) { const found = rows.filter(predicate); return found.length === 1 ? found[0] : null; }
function directory(nfl) {
  return nfl?.schedule?.playerDirectory ?? nfl?.players?.playerDirectory ?? [];
}
export function reconcileRoster(previous, rows, { team, site, now, season, nfl }) {
  const old = previous?.players ?? [];
  const used = new Set();
  const players = rows.map(row => {
    const prior = uniqueMatch(old, x => x.sourceId === row.sourceId) ?? uniqueMatch(old, x => identityKey(x.name) === identityKey(row.name));
    const id = prior?.id ?? slug(row.name);
    if (used.has(String(id))) throw new Error(`Roster produced duplicate ID: ${id}`);
    used.add(String(id));
    const match = uniqueMatch(directory(nfl), x => identityKey(x.full_name || `${x.first_name ?? ''} ${x.last_name ?? ''}`) === identityKey(row.name) && clean(x.position_abbreviation ?? x.position).toUpperCase() === row.position.toUpperCase());
    const balldontlieId = Number(match?.id);
    return { ...(prior ?? {}), ...row, id, ...(Number.isSafeInteger(balldontlieId) && balldontlieId > 0 ? { balldontlieId } : {}) };
  });
  const identities = new Set(players.map(row => identityKey(row.name)));
  for (const row of old) if (!used.has(String(row.id)) && !identities.has(identityKey(row.name))) players.push({ ...row, status: departed.has(row.status) ? row.status : 'Historical' });
  const current = players.filter(row => !departed.has(row.status));
  if (current.length < 40 || current.filter(row => row.status === 'Active').length > 90) throw new Error(`Implausible roster count: ${current.length}`);
  const priorCount = old.filter(row => !departed.has(row.status)).length;
  if (priorCount >= 20 && Math.abs(current.length - priorCount) > Math.max(8, Math.ceil(priorCount * .15))) throw new Error(`Large roster count change (${priorCount} -> ${current.length}); inspect the official source before replacing the prior snapshot`);
  return { ...previous, schemaVersion: 1, team: site.slug, season, asOf: now, sourceCheckedAt: now, sourceUrl: team.rosterUrl, sourcePublisher: team.name, sourceNote: 'Official club roster; original club status labels retained. Departed players remain historical entries.', players };
}
const monthNames = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
function classifyTransaction(text) {
  const value = text.toLowerCase();
  // Classify the complete source event without claiming a roster-status transition.
  if (/\btrad(?:e|ed|ing)\b/.test(value)) return 'Trade';
  if (/\b(?:extend(?:ed)?|extension)\b/.test(value)) return 'Extension';
  if (/\b(?:elevat(?:ed|ion)|standard elevation)\b/.test(value)) return 'Elevated';
  if (/\b(?:injured reserve|reserve\/injured)\b/.test(value)) return 'Injured Reserve';
  if (/physically unable to perform|\bpup\b/.test(value)) return 'PUP';
  if (/\b(?:waived|waiver)\b/.test(value)) return 'Waived';
  if (/\bclaim(?:ed)?\b/.test(value)) return 'Claimed';
  if (/practice squad/.test(value)) return 'Practice Squad';
  if (/\b(?:released|terminated)\b/.test(value)) return 'Released';
  if (/\bsigned\b/.test(value)) return 'Signed';
  return 'Other';
}
export function parseTransactions(html, { team, season, transactionYear = season, roster, now }) {
  verifyClub(html, team);
  const selected = [...html.matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/gi)].filter(match => /\bselected(?:\s|=|$)/i.test(match[1]));
  const selectedSeasons = selected.map(match => match[1].match(/\/team\/transactions\/(\d{4})\b/)?.[1]).filter(Boolean);
  if (selectedSeasons.length !== 1 || Number(selectedSeasons[0]) !== transactionYear) throw new Error('Official transaction page did not confirm the requested season');
  const records = [];
  let tables = 0;
  for (const match of html.matchAll(/<div\b[^>]*\bnfl-c-transactions-report\b[^>]*>([\s\S]*?)<\/table>/gi)) {
    const month = monthNames.indexOf(clean(match[1].match(/nfl-c-transactions-report__month[^>]*>([\s\S]*?)<\/th>/i)?.[1])) + 1;
    if (!month) throw new Error('Unrecognized transaction month heading');
    tables++;
    for (const row of tableRows(match[1])) {
      if (row.cells.length !== 2) continue;
      const date = row.cells[0].match(/^(\d{2})\/(\d{2})$/);
      if (!date || Number(date[1]) !== month || !row.cells[1]) throw new Error('Unrecognized official transaction row');
      const day = `${transactionYear}-${date[1]}-${date[2]}`;
      if (new Date(`${day}T12:00:00Z`).toISOString().slice(0, 10) !== day) throw new Error('Invalid transaction calendar date');
      if (now && day > new Intl.DateTimeFormat('en-CA', { timeZone: team.timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(now))) throw new Error('Official transaction row is dated in the future');
      const description = row.cells[1];
      const eventId = digest(`${team.abbreviation}:${day}:${description}`).slice(0, 24);
      const normalized = ` ${description.toLowerCase().replace(/[^a-z0-9'’]+/g, ' ')} `;
      const matches = (roster.players ?? []).filter(player => normalized.includes(` ${player.name.toLowerCase().replace(/[^a-z0-9'’]+/g, ' ').trim()} `));
      const identities = [...new Map(matches.map(player => [String(player.id), player])).values()];
      // A source row can include many players/moves. Keep one complete event rather
      // than inventing a per-player interpretation or dropping unfamiliar grammar.
      records.push({ timestamp: `${day}T12:00:00Z`, datePrecision: 'day', eventId,
        playerId: `transaction-${eventId}`, playerName: 'Team transaction', entityType: 'transaction',
        relatedPlayerIds: identities.map(player => player.id), transactionType: classifyTransaction(description), previousStatus: null, newStatus: null,
        description, sourcePublisher: team.name, sourceUrl: team.transactionsUrl, updateStatus: 'Official' });
    }
  }
  if (!tables) {
    if (/no transactions (?:available|found|to display)/i.test(clean(html))) return [];
    throw new Error('Official transactions response contains no recognizable transaction tables');
  }
  return records;
}
export function reconcileTransactions(previous, fetched, { site, team, now }) {
  const records = [...(previous?.records ?? [])];
  const ids = new Set(records.map(row => row.eventId ?? digest(`${row.timestamp}:${row.playerId}:${row.description}`)));
  for (const row of fetched) if (!ids.has(row.eventId)) { records.push(row); ids.add(row.eventId); }
  return { ...previous, schemaVersion: 1, team: site.slug, asOf: now, sourceCheckedAt: now, sourcePublisher: team.name, sourceUrl: team.transactionsUrl,
    sourceNote: 'Complete dated entries from the official club transaction log. Multi-player entries retain source wording; earlier curated records are retained.', records };
}
function clubInjuryTable(html, team) {
  verifyClub(html, team);
  const blocks = [...html.matchAll(/<span\b[^>]*class=["'][^"']*\bnfl-o-injury-report__club-name\b[^"']*["'][^>]*>([\s\S]*?)<\/span>([\s\S]*?)(?=<span\b[^>]*class=["'][^"']*\bnfl-o-injury-report__club-name\b|$)/gi)];
  const own = blocks.filter(block => clean(block[1]).toLowerCase() === team.name.toLowerCase());
  if (own.length !== 1) throw new Error('Official injury report did not contain exactly one selected club table');
  const table = own[0][2].match(/<table\b[^>]*>[\s\S]*?<\/table>/i)?.[0];
  if (!table || !/Table - Injury report/i.test(clean(table))) throw new Error('Official club injury table markup is unrecognized');
  return table;
}
function selectedWeek(html) {
  const options = [...html.matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/gi)].filter(match => /\bselected(?:\s|=|$)/i.test(match[1]));
  return options.map(match => match[1].match(/\/injury-report\/week\/(REG|PRE|POST|WC|DIV|CONF|SB)-(\d+)/i)).find(Boolean);
}
function gameDate(game, timeZone) {
  if (game.dateConfirmed === false || game.date_confirmed === false || game.date_tbd === true || game.dateTbd === true) return null;
  const value = game.startsAt ?? game.date;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value ?? '')) {
    const parsed = Date.parse(`${value}T12:00:00Z`);
    return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 10) === value ? value : null;
  }
  if (!value || !Number.isFinite(Date.parse(value))) return null;
  return new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(value));
}
export function parseInjuries(html, { team, roster, nfl, now, season, previousInjuries = null }) {
  const table = clubInjuryTable(html, team);
  const rows = tableRows(table).filter(row => row.cells.length);
  const headings = [...table.matchAll(/<th\b[^>]*>([\s\S]*?)<\/th>/gi)].map(x => clean(x[1]));
  if (headings[0] !== 'Player' || headings[2] !== 'Injury' || headings.at(-1) !== 'Game Status') throw new Error('Unrecognized injury report columns');
  const selected = selectedWeek(html);
  const schedule = nfl?.schedule;
  if (!selected || selected[1].toUpperCase() !== 'REG' || Number(schedule?.season) !== season) return { records: [], available: false, reason: 'No verified regular-season schedule for the selected injury-report week', period: null };
  const sourceReportFingerprint = digest(`${selected[1].toUpperCase()}-${selected[2]}:${clean(table)}`);
  if (previousInjuries?.sourceReportSeason != null && previousInjuries.sourceReportSeason !== season && previousInjuries.sourceReportFingerprint === sourceReportFingerprint) {
    return { records: [], available: false, reason: 'Unchanged source report was previously observed in another NFL season; its new-season date is unverified', period: null };
  }
  const week = Number(selected[2]);
  const games = schedule.gamesRegular ?? schedule.games ?? [];
  const matches = games.filter(game => Number(game.week) === week && !['preseason', 'postseason'].includes(game.phase) && ![1, 3].includes(Number(game.season_type)) && !game.bye);
  if (matches.length !== 1) return { records: [], available: false, reason: 'The selected injury-report week cannot be joined uniquely to the verified schedule', period: null };
  const game = matches[0], date = gameDate(game, team.timeZone);
  if (!date) return { records: [], available: false, reason: 'Verified kickoff date is unavailable', period: null };
  const gameAt = Date.parse(`${date}T12:00:00Z`), nowAt = Date.parse(now);
  // Prevent a previous season's stale dropdown from being dated in a new season.
  if (gameAt < nowAt - 14 * dayMs || gameAt > nowAt + 10 * dayMs) return { records: [], available: false, reason: 'Selected report is outside the verified current game window', period: null };
  const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const columns = headings.map((label, index) => {
    const wanted = days.indexOf(label);
    if (wanted < 0) return null;
    const gap = (new Date(gameAt).getUTCDay() - wanted + 7) % 7 || 7;
    return { index, date: new Date(gameAt - gap * dayMs).toISOString().slice(0, 10) };
  }).filter(Boolean);
  if (!columns.length) throw new Error('Official injury report has no recognizable practice-day columns');
  const records = [], participation = new Map([['DNP', 'DNP'], ['LP', 'Limited'], ['FP', 'Full']]);
  const period = { season, week, gameId: game.id, gameDate: date, dateBasis: 'Observation dates inferred from the official selected report week and verified NFL schedule; the club table does not publish explicit report dates' };
  if (!rows.length) return { records: [], available: true, reason: 'Official club table contains no report rows for the verified game period', period, sourceReportFingerprint };
  for (const row of rows) {
    if (row.cells.length !== headings.length) throw new Error('Incomplete official injury row');
    const linked = links(row.html)[0];
    if (!linked) throw new Error('Official club injury player link missing');
    const player = uniqueMatch(roster.players, x => x.sourceId === linked.sourceId) ?? uniqueMatch(roster.players, x => identityKey(x.name) === identityKey(linked.name));
    const playerId = player?.id ?? slug(linked.name), injury = row.cells[2];
    const common = { playerId, playerName: linked.name, injury, season, week, gameId: game.id, sourcePublisher: team.name, sourceUrl: team.injuriesUrl, updateStatus: 'Official', datePrecision: 'day' };
    for (const column of columns) {
      const raw = row.cells[column.index];
      if (absent.test(raw)) continue;
      const status = participation.get(raw.toUpperCase());
      if (!status) throw new Error(`Unrecognized official practice status: ${raw}`);
      if (Date.parse(`${column.date}T00:00:00Z`) > nowAt) throw new Error('Official practice observation resolves to a future date');
      records.push({ ...common, date: `${column.date}T12:00:00Z`, reportType: 'Practice Participation', status, description: `Practice participation: ${status} (${injury}).` });
    }
    const raw = row.cells.at(-1);
    if (!absent.test(raw)) {
      if (!/^(out|doubtful|questionable)$/i.test(raw)) throw new Error(`Unrecognized official game designation: ${raw}`);
      const dated = columns.filter(column => !absent.test(row.cells[column.index]));
      const last = dated.map(column => column.date).sort().at(-1);
      if (!last) throw new Error('Game designation has no dated practice observation');
      const status = raw[0].toUpperCase() + raw.slice(1).toLowerCase();
      records.push({ ...common, date: `${last}T12:00:00Z`, reportType: 'Game Status', status, description: `Game designation: ${status} (${injury}).` });
    }
  }
  return { records, available: true, reason: null, period, sourceReportFingerprint };
}
export function reconcileInjuries(previous, parsed, { site, team, now }) {
  const records = [...(previous?.records ?? [])];
  const key = row => `${row.date?.slice(0, 10)}:${row.playerId}:${row.reportType ?? 'Roster Status'}`;
  const indices = new Map(records.map((row, index) => [key(row), index]));
  for (const row of parsed.records) {
    const index = indices.get(key(row));
    if (index === undefined) { indices.set(key(row), records.length); records.push(row); }
    else if (records[index].sourceUrl === team.injuriesUrl) records[index] = row;
  }
  return { ...previous, schemaVersion: 2, team: site.slug,
    asOf: parsed.available ? now : previous?.asOf ?? null, sourceCheckedAt: now,
    sourcePublisher: team.name, sourceUrl: team.injuriesUrl,
    sourceNote: 'Dated official practice and game-designation observations; an absent report is not evidence that all players are healthy.',
    availability: parsed.available ? 'available' : 'unavailable', availabilityReason: parsed.reason, reportPeriod: parsed.period,
    sourceReportFingerprint: parsed.available ? parsed.sourceReportFingerprint : previous?.sourceReportFingerprint ?? null,
    sourceReportSeason: parsed.available ? parsed.period?.season ?? null : previous?.sourceReportSeason ?? null,
    currentReportKeys: parsed.records.map(key), records };
}
async function fetchPage(url, team, fetchImpl) {
  for (let redirects = 0; redirects <= 3; redirects++) {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' || parsed.host !== team.domain || parsed.username || parsed.password) throw new Error('Official-source redirect left the configured club domain');
    const response = await fetchImpl(url, { headers: { Accept: 'text/html', 'User-Agent': 'FanZone official roster collector' }, redirect: 'manual', signal: AbortSignal.timeout(20_000) });
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      const location = response.headers?.get?.('location');
      if (!location) throw new Error('Official source redirect has no location');
      url = new URL(location, url).href; continue;
    }
    if (!response.ok) throw new Error(`Official source failed: ${parsed.pathname} HTTP ${response.status}`);
    const body = await response.text();
    if (Buffer.byteLength(body) > 5_000_000) throw new Error('Official source exceeded the 5 MB response limit');
    return body;
  }
  throw new Error('Official source redirected too many times');
}
function validatePrevious(previous, site) {
  for (const name of ['roster', 'injuries', 'transactions']) {
    const store = previous?.[name];
    if (!store) continue;
    if (store.team !== site.slug && !(store.team == null && site.slug === 'seahawks')) throw new Error(`Refusing another team's prior ${name} archive`);
    if (!Array.isArray(store[name === 'roster' ? 'players' : 'records'])) throw new Error(`Invalid prior ${name} archive`);
  }
}
export async function collectTeam(request, { fetchImpl = globalThis.fetch } = {}) {
  const site = request?.site, now = request?.now;
  if (!site || !Object.hasOwn(teams, site.slug) || !Number.isFinite(Date.parse(now))) throw new Error('Collector requires a supported team and an explicit valid clock');
  const configured = teams[site.slug];
  if (site.abbreviation !== configured.abbreviation || `${site.city} ${site.name}` !== configured.name) throw new Error('Active-site identity does not match official-source registry');
  const clock = new Date(now), season = clock.getUTCMonth() < 2 ? clock.getUTCFullYear() - 1 : clock.getUTCFullYear();
  // Transactions are published by calendar year, including January moves during
  // the prior NFL season. Existing archives retain earlier calendar years.
  const transactionYear = clock.getUTCFullYear();
  validatePrevious(request.previous, site);
  const team = { ...configured, rosterUrl: `https://${configured.domain}/team/players-roster/`, injuriesUrl: `https://${configured.domain}/team/injury-report/`, transactionsUrl: `https://${configured.domain}/team/transactions/${transactionYear}` };
  let requestCount = 0;
  const countedFetch = (...args) => { requestCount++; return fetchImpl(...args); };
  // Injury reports are optional: clubs can remove or replace the report between
  // game weeks. Keep that failure separate from current roster/transaction data.
  const [rosterHtml, injurySource, transactionHtml] = await Promise.all([
    fetchPage(team.rosterUrl, team, countedFetch),
    fetchPage(team.injuriesUrl, team, countedFetch).then(html => ({ html }), error => ({ error })),
    fetchPage(team.transactionsUrl, team, countedFetch),
  ]);
  const context = { site, team, now: clock.toISOString(), season, transactionYear, nfl: request.nfl };
  const roster = reconcileRoster(request.previous?.roster, parseRoster(rosterHtml, team), context);
  const transactions = reconcileTransactions(request.previous?.transactions, parseTransactions(transactionHtml, { ...context, roster }), context);
  let parsed;
  try {
    if (injurySource.error) throw injurySource.error;
    parsed = parseInjuries(injurySource.html, { ...context, roster, previousInjuries: request.previous?.injuries });
  } catch (error) {
    parsed = { records: [], available: false, period: null,
      reason: `Official injury report unavailable: ${clean(error.message).slice(0, 500)}` };
  }
  const injuries = reconcileInjuries(request.previous?.injuries, parsed, context);
  const report = { status: 'success', team: site.slug, runId: request.runId, updatedAt: context.now, season, transactionYear, requestCount,
    counts: { currentRoster: roster.players.filter(row => !departed.has(row.status)).length, rosterHistory: roster.players.length, transactions: transactions.records.length, injuryObservations: injuries.records.length },
    injuryAvailability: injuries.availability, injuryReason: injuries.availabilityReason,
    sources: { roster: team.rosterUrl, injuries: team.injuriesUrl, transactions: team.transactionsUrl } };
  return { roster, injuries, transactions, report };
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.length !== 4) throw new Error('Usage: node collector.mjs request.json candidate-directory');
    const result = await collectTeam(JSON.parse(fs.readFileSync(process.argv[2], 'utf8')));
    fs.mkdirSync(process.argv[3], { recursive: true });
    for (const name of ['roster', 'injuries', 'transactions', 'report']) fs.writeFileSync(path.join(process.argv[3], `${name}.json`), JSON.stringify(result[name], null, 2) + '\n');
    console.log(JSON.stringify(result.report));
  } catch (error) { console.error(`Roster collection failed: ${error.message}`); process.exitCode = 1; }
}
