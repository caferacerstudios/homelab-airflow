/** Shared pure identity/status checks. No network requests or generated game IDs. */
const text = value => String(value ?? "").trim();
const abbr = team => {
  const value = text(team?.abbreviation ?? team?.abbr ?? team).toUpperCase();
  // Existing ticket coverage uses WAS; the live NFL snapshot uses WSH.
  // Normalize comparisons only, preserving provider data and saved coverage.
  return value === "WAS" ? "WSH" : value;
};
const gameId = game => text(game?.id ?? game?.gameId ?? game?.game_id);
const safeId = value => /^\d{1,16}$/.test(text(value));
const forbidden = /tailgate|parking|training|stadium[- ]?tour|club[- ]?seats|season[- ]?(?:ticket|pass)|ticket[- ]?package|vip[- :]/i;

export function localDay(value, timeZone = "America/Los_Angeles") {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(date).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

export function scheduleLocalDate(game, row) {
  if (!game || game.dateConfirmed === false || game.date_confirmed === false || game.date_tbd === true) return null;
  const value = game.startsAt ?? game.datetime ?? game.start_time ?? game.kickoff ?? game.localDate ?? game.date;
  if (/^\d{4}-\d{2}-\d{2}$/.test(text(value))) return text(value);
  return value ? localDay(value, row.timeZone) : null;
}

function confirmedKickoff(game) {
  if (!game || game.dateConfirmed === false || game.date_confirmed === false || game.date_tbd === true || game.dateTbd === true || game.timeConfirmed === false || game.time_confirmed === false || game.time_tbd === true || game.timeTbd === true) return null;
  const status = `${text(game.state)} ${text(game.status)} ${text(game.status_state)}`;
  if (/tbd|to[_ ]be[_ ]determined|unconfirmed/i.test(status)) return null;
  const raw = text(game.startsAt ?? game.datetime ?? game.start_time ?? game.kickoff ?? game.date);
  // Date-only and timezone-less values are not a confirmed kickoff instant.
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/i.test(raw)) return null;
  const instant = new Date(raw);
  if (!Number.isFinite(instant.getTime())) return null;
  // The schedule adapter also treats unconfirmed UTC midnight as a placeholder.
  if (instant.getUTCHours() === 0 && instant.getUTCMinutes() === 0 && instant.getUTCSeconds() === 0 && game.timeConfirmed !== true && game.time_confirmed !== true) return null;
  return instant.getTime();
}

export function validateCoverage(site, coverage) {
  if (!site || !/^[a-z][a-z0-9-]{0,31}$/.test(site.slug) || !text(site.city) || !text(site.name)) throw new Error("Invalid site identity");
  if (!Array.isArray(coverage) || !coverage.length || coverage.length > 64) throw new Error("Invalid coverage array");
  const ids = new Set(), events = new Set(), weeks = new Set();
  for (const row of coverage) {
    if (!row || !["authorized", "unavailable"].includes(row.state) || !["home", "away"].includes(row.homeAway) || !text(row.opponent)) throw new Error("Invalid coverage identity");
    if (row.gameId !== null && row.gameId !== undefined) {
      if (!safeId(row.gameId) || ids.has(text(row.gameId))) throw new Error("Unsafe or duplicate game ID");
      ids.add(text(row.gameId));
    }
    if (row.week !== undefined) {
      const key = `${row.season}:${row.week}`;
      if (!Number.isInteger(row.week) || row.week < 1 || row.week > 18 || weeks.has(key)) throw new Error("Invalid coverage week");
      weeks.add(key);
    }
    if (row.timeZone) localDay(Date.now(), row.timeZone);
    if (row.state === "unavailable") {
      if (row.sourceEventId !== null || row.sourceUrl !== null || row.reasonCode !== "SOURCE_PAGE_NOT_AVAILABLE") throw new Error("Invalid unavailable coverage");
      continue;
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(row.localDate ?? "") || !/^\d{6}$/.test(row.sourceEventId ?? "") || events.has(row.sourceEventId)) throw new Error("Invalid or duplicate source event");
    const url = new URL(row.sourceUrl);
    if (url.protocol !== "https:" || url.hostname !== "www.event-spy.com" || url.username || url.password || url.port || url.search || url.hash || forbidden.test(url.pathname) || !/^\/event\/[a-z0-9-]+\/\d{6}$/.test(url.pathname) || !url.pathname.endsWith(`/${row.sourceEventId}`)) throw new Error("Unsafe EventSpy source URL");
    events.add(row.sourceEventId);
  }
  return coverage;
}

function identityMatches(site, row, game) {
  const home = game.homeTeam ?? game.home_team;
  const away = game.awayTeam ?? game.visitor_team ?? game.away_team;
  const homeAbbr = abbr(home), awayAbbr = abbr(away), siteAbbr = abbr(site.abbreviation);
  // Never trust a foreign snapshot's isHome/opponent fields without its real teams.
  if (!homeAbbr || !awayAbbr || !siteAbbr) return false;
  if (homeAbbr !== abbr(row.homeTeamAbbreviation ?? (row.homeAway === "home" ? siteAbbr : row.opponentAbbreviation)) || awayAbbr !== abbr(row.awayTeamAbbreviation ?? (row.homeAway === "away" ? siteAbbr : row.opponentAbbreviation))) return false;
  if (row.week != null && Number(game.week) !== row.week) return false;
  if (row.season != null && Number(game.season) !== row.season) return false;
  const phase = text(game.phase ?? game.season_type ?? game.seasonType).toLowerCase();
  if (phase && !["regular", "regular season", "regular_season"].includes(phase)) return false;
  return safeId(gameId(game));
}

/** Returns one binding per reviewed row. A missing/ambiguous ID is never invented. */
export function bindCoverageToSchedule(site, coverage, schedule = null) {
  validateCoverage(site, coverage);
  if (schedule !== null && (schedule.fixture !== false || abbr(schedule.team) !== abbr(site.abbreviation))) throw new Error("Schedule must be a non-fixture snapshot for the selected team");
  const games = schedule?.gamesRegular?.length ? schedule.gamesRegular : schedule?.games ?? schedule?.gamesRegular ?? null;
  if (schedule && !Array.isArray(games)) throw new Error("Schedule has no regular games");
  return coverage.map(original => {
    const row = { ...original, gameId: original.gameId == null ? null : text(original.gameId) };
    if (!games) return { row, game: null, reason: row.gameId ? null : "GAME_ID_UNRESOLVED" };
    const matches = games.filter(game => identityMatches(site, row, game) && (!row.gameId || gameId(game) === row.gameId));
    if (matches.length !== 1) return { row, game: null, reason: matches.length ? "SCHEDULE_GAME_AMBIGUOUS" : "SCHEDULE_GAME_MISSING" };
    const game = matches[0];
    row.gameId = gameId(game);
    return { row, game, reason: null };
  });
}

/** Completed decisions precede browser startup and every event request. */
export function collectionDecision(binding, now = Date.now()) {
  const { row, game, reason } = binding;
  if (reason) return { kind: "unresolved", reason };
  const status = `${text(game?.state)} ${text(game?.status)} ${text(game?.status_state)}`.toLowerCase();
  if (/postpon|reschedul|suspend|cancel|abandon|delay/.test(status)) return { kind: "unresolved", reason: "SCHEDULE_CHANGED" };
  if (/\bfinal\b|finished|completed?|closed/.test(status)) return { kind: "skipped", reason: "GAME_COMPLETED" };
  const currentDate = scheduleLocalDate(game, row);
  if (row.state === "authorized" && game && (!currentDate || currentDate !== row.localDate)) return { kind: "unresolved", reason: "SCHEDULE_DATE_CHANGED" };
  const kickoff = confirmedKickoff(game);
  if (kickoff !== null && kickoff <= now) return { kind: "skipped", reason: "GAME_STARTED" };
  const date = currentDate ?? row.localDate;
  if (date && date < localDay(now, row.timeZone)) return { kind: "skipped", reason: "PAST_GAME_DATE" };
  if (row.state === "unavailable") return { kind: "unavailable", reason: row.reasonCode };
  return { kind: "collect", reason: null };
}

export function validateEventIdentity(site, row, event) {
  if (!event || event.eventDateLocal !== row.localDate) throw new Error("event date mismatch");
  const title = String(event.eventName || "");
  if (forbidden.test(title) || !title.includes(`${site.city} ${site.name}`) || !title.includes(row.opponent)) throw new Error("event matchup mismatch");
  if (event.id != null && String(event.id) !== row.sourceEventId) throw new Error("event ID mismatch");
  return title;
}
