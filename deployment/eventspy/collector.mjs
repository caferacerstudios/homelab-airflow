/** Modular Playwright wrapper: v1.1.0 permits explicitly missing provider links. */
import { mkdir, open, readFile, rename } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { bindCoverageToSchedule, collectionDecision, localDay, validateEventIdentity } from "./collector-coverage.mjs";
const MARKETS = [
  "ticketmaster",
  "stubhub",
  "vividseats",
  "seatgeek"
];
const UA =
  "Mozilla/5.0 (X11; Linux x86_64) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) " +
  "Chrome/140.0.0.0 Safari/537.36";
function safeProviderUrl(value) {
  try {
    const url = new URL(value);
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.port
    ) return null;
    if (
      /^(?:localhost|127\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)/
        .test(url.hostname)
    ) return null;
    if (
      [...url.searchParams.keys()].some(key =>
        /(?:token|secret|password|credential|api[_-]?key|auth|session|cookie)/i
          .test(key)
      )
    ) return null;
    return url.href;
  } catch {
    return null;
  }
}
function cents(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) {
    throw new Error("invalid price");
  }
  return Math.round(n * 100);
}
function pacificToday(now = Date.now(), timeZone = "America/Los_Angeles") {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    })
      .formatToParts(new Date(now))
      .map(part => [part.type, part.value])
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}
function daysUntil(localDate, now = Date.now(), timeZone = "America/Los_Angeles") {
  const [ey, em, ed] = localDate.split("-").map(Number);
  const [ty, tm, td] = pacificToday(now, timeZone).split("-").map(Number);
  return Math.max(
    0,
    Math.round(
      (
        Date.UTC(ey, em - 1, ed) -
        Date.UTC(ty, tm - 1, td)
      ) / 86400000
    )
  );
}
function ageLabel(iso, now = Date.now()) {
  const ms = Math.max(0, now - Date.parse(iso));
  const minutes = Math.floor(ms / 60000);
  if (minutes < 2) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours} hr${hours === 1 ? "" : "s"} ago`;
  }
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}
function timeLabel(event) {
  return new Intl.DateTimeFormat("en-US", {
    timeZone:
      event.eventTimezone ||
      "America/Los_Angeles",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZoneName: "short"
  }).format(new Date(event.eventDateTimeUTC));
}
function mergeHistory(priceHistory) {
  const points = new Map();
  for (const market of MARKETS) {
    const rows =
      Array.isArray(priceHistory?.[market])
        ? priceHistory[market]
        : [];
    for (const row of rows) {
      if (typeof row?.seenAt !== "string") continue;
      let value;
      try {
        value = cents(row.lowestPrice);
      } catch {
        continue;
      }
      const point =
        points.get(row.seenAt) || {
          observedAt: row.seenAt,
          ticketmasterCents: null,
          stubhubCents: null,
          vividseatsCents: null,
          seatgeekCents: null
        };
      point[`${market}Cents`] = value;
      points.set(row.seenAt, point);
    }
  }
  return [...points.values()]
    .sort(
      (a, b) =>
        Date.parse(a.observedAt) -
        Date.parse(b.observedAt)
    );
}
function providerLinks(event) {
  const links = {};
  for (const market of MARKETS) {
    const raw = event[`${market}Url`];
    if (raw == null || raw === "") {
      links[market] = null;
      continue;
    }
    links[market] = typeof raw === "string" ? safeProviderUrl(raw) : null;
    if (!links[market]) throw new Error(`invalid ${market} provider URL`);
  }
  if (!Object.values(links).some(Boolean)) throw new Error("no usable provider URLs");
  return links;
}
async function atomicWrite(file, value) {
  await mkdir(dirname(file), {
    recursive: true
  });
  const temp =
    `${file}.tmp-${process.pid}-${Date.now()}`;
  const handle =
    await open(temp, "wx", 0o644);
  try {
    await handle.writeFile(
      `${JSON.stringify(value)}\n`
    );
    await handle.sync();
  } finally {
    await handle.close();
  }
  await rename(temp, file);
}
export async function fetchEvent(page, sourceEventId) {
  return page.evaluate(
    async eventId => {
      const base =
        `https://api.event-spy.com/api/app/event/${eventId}`;
      const [
        eventResponse,
        historyResponse
      ] = await Promise.all([
        fetch(base, {
          method: "GET",
          credentials: "include"
        }),
        fetch(`${base}/history`, {
          method: "POST",
          credentials: "include",
          headers: {
            "content-type":
              "application/json"
          },
          body: "{}"
        })
      ]);
      if (!eventResponse.ok) {
        throw new Error(
          `event api ${eventResponse.status}`
        );
      }
      if (!historyResponse.ok) {
        throw new Error(
          `history api ${historyResponse.status}`
        );
      }
      return {
        event:
          await eventResponse.json(),
        history:
          await historyResponse.json()
      };
    },
    sourceEventId
  );
}
export function buildSnapshot(row, payload, now, site) {
  const event = payload.event;
  const title = validateEventIdentity(site, row, event);
  const links = providerLinks(event);
  const history =
    mergeHistory(
      payload.history?.priceHistory
    );
  if (history.length < 2) {
    throw new Error("insufficient history");
  }
  const currentMarketplace =
    String(
      event.currentPriceVendor || ""
    ).toLowerCase();
  const current =
    cents(event.currentPrice);
  const seven =
    cents(event.sevenDayLowest);
  return {
    schemaVersion: Object.values(links).includes(null) ? "1.1.0" : "1.0.0",
    source: "eventspy",
    currency: "USD",
    gameId:
      row.gameId,
    sourceEventId:
      row.sourceEventId,
    sourceUrl:
      row.sourceUrl,
    trackingUrl:
      row.sourceUrl,
    collectedAt:
      new Date(now).toISOString(),
    event: {
      title,
      venue:
        String(event.venueName || ""),
      location:
        [
          event.venueCity,
          event.venueState
        ]
          .filter(Boolean)
          .join(", "),
      localDate:
        event.eventDateLocal,
      localTimeLabel:
        timeLabel(event)
    },
    summary: {
      daysUntilEvent:
        daysUntil(
          event.eventDateLocal,
          now,
          site.timezone || "America/Los_Angeles"
        ),
      currentLowestCents:
        current,
      currentLowestMarketplace:
        MARKETS.includes(
          currentMarketplace
        )
          ? currentMarketplace
          : null,
      currentLowestObservedAt:
        event.currentPriceSeenAt,
      currentLowestAgeLabel:
        ageLabel(
          event.currentPriceSeenAt,
          now
        ),
      sevenDayLowestCents:
        seven,
      sevenDayLowestObservedAt:
        event.sevenDayLowestSeenAt,
      sevenDayLowestAgeLabel:
        ageLabel(
          event.sevenDayLowestSeenAt,
          now
        ),
      atSevenDayLow:
        current === seven
    },
    providerLinks: links,
    history
  };
}

async function readJson(file, fallback) {
  try { return JSON.parse(await readFile(file, "utf8")); }
  catch (error) { if (error.code === "ENOENT" && fallback !== undefined) return fallback; throw error; }
}

/** The root bridge holds one global lock across all sites while this executes. */
async function reserveSource(config, row, now) {
  const day = localDay(config.slot, "America/Los_Angeles");
  const file = join(config.cacheRoot, day, `${row.sourceEventId}.json`);
  const record = await readJson(file, { sourceEventId: row.sourceEventId, day, attempts: {} });
  if (record.sourceEventId !== row.sourceEventId || record.day !== day || !record.attempts || typeof record.attempts !== "object") throw new Error("Invalid event attempt ledger");
  const previous = record.attempts[config.slot];
  if (previous?.payload) return { record, file, cached: previous };
  if (previous) throw new Error("Source event already attempted in this slot; retry suppressed");
  if (Object.keys(record.attempts).length >= 7) throw new Error("Source event reached seven attempts for this Pacific day");
  record.attempts[config.slot] = { reservedAt: new Date(now).toISOString() };
  // Durably reserve before browser bootstrap; a crash never silently grants a retry.
  await atomicWrite(file, record);
  return { record, file, cached: null };
}

async function defaultBrowserFactory() {
  const { chromium } = await import("playwright-core");
  return chromium.launch({ headless: true });
}

export async function runCollector(config, dependencies = {}) {
  const now = dependencies.now ?? Date.now();
  const log = dependencies.log ?? (value => console.log(JSON.stringify(value)));
  const getBrowser = dependencies.browserFactory ?? defaultBrowserFactory;
  const getEvent = dependencies.fetchEvent ?? fetchEvent;
  const instant = new Date(config.slot);
  if (!Number.isFinite(instant.getTime()) || instant.toISOString() !== config.slot) throw new Error("slot must be canonical UTC ISO timestamp");
  if (!config.outputRoot || !config.cacheRoot || resolve(config.outputRoot) === resolve(config.cacheRoot)) throw new Error("Separate output and cache directories required");
  const bindings = bindCoverageToSchedule(config.site, config.coverage, config.schedule ?? null);
  const summary = {
    outcome: "EVENTSPY_SEASON_SUCCESS", team: config.site.slug, slot: config.slot,
    authorized: config.coverage.filter(row => row.state === "authorized").length,
    succeeded: 0, failed: 0, unavailable: 0, skipped: 0, unresolved: 0, results: [],
  };
  let browser, context, page, browserError;
  async function getPage(row) {
    if (browserError) throw browserError;
    if (page) return page;
    try {
      browser = await getBrowser();
      context = await browser.newContext({ userAgent: UA, acceptDownloads: false });
      const candidate = await context.newPage();
      // This is the first eligible future game, never the fixed Week 1 event.
      const response = await candidate.goto(row.sourceUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
      if (!response || response.status() < 200 || response.status() >= 400) throw new Error("EventSpy bootstrap page failed");
      page = candidate;
      return page;
    } catch (error) { browserError = error; throw error; }
  }
  function result(row, kind, outcome, extra = {}) {
    summary[kind]++;
    const item = { team: config.site.slug, gameId: row.gameId, sourceEventId: row.sourceEventId, week: row.week ?? null, outcome, ...extra };
    summary.results.push(item);
    log(item);
  }
  try {
    for (const binding of bindings) {
      const { row } = binding;
      const decision = collectionDecision(binding, now);
      if (decision.kind === "skipped" || decision.kind === "unresolved") {
        result(row, decision.kind, decision.kind === "skipped" ? "EVENTSPY_COLLECTION_SKIPPED" : "EVENTSPY_GAME_UNRESOLVED", { reason: decision.reason });
        continue;
      }
      if (decision.kind === "unavailable") {
        try {
          // Keep the deployed unavailable document exactly, including collectedAt.
          await atomicWrite(join(config.outputRoot, `${row.gameId}.json`), {
            schemaVersion: "1.0.0", source: "eventspy", gameId: row.gameId,
            state: "unavailable", reasonCode: row.reasonCode, collectedAt: new Date(now).toISOString(),
          });
          result(row, "unavailable", "EVENTSPY_SOURCE_UNAVAILABLE");
        } catch (error) { result(row, "failed", "EVENTSPY_COLLECTION_FAILED", { error: String(error?.message || error).slice(0, 180) }); }
        continue;
      }
      try {
        const reservation = await reserveSource(config, row, now);
        let collected = reservation.cached;
        if (!collected) {
          const activePage = await getPage(row);
          const payload = await getEvent(activePage, row.sourceEventId);
          collected = { ...reservation.record.attempts[config.slot], collectedAt: new Date(now).toISOString(), payload };
          reservation.record.attempts[config.slot] = collected;
          // Cache raw EventSpy identity/history once; each team validates its own matchup.
          await atomicWrite(reservation.file, reservation.record);
        }
        const snapshot = buildSnapshot(row, collected.payload, Date.parse(collected.collectedAt), config.site);
        await atomicWrite(join(config.outputRoot, `${row.gameId}.json`), snapshot);
        result(row, "succeeded", "EVENTSPY_COLLECTION_SUCCESS", {
          currentLowestCents: snapshot.summary.currentLowestCents,
          historyPoints: snapshot.history.length, cached: Boolean(reservation.cached),
          ...(snapshot.schemaVersion === "1.1.0" ? {
            missingProviders: MARKETS.filter(market => snapshot.providerLinks[market] === null),
          } : {}),
        });
      } catch (error) { result(row, "failed", "EVENTSPY_COLLECTION_FAILED", { error: String(error?.message || error).slice(0, 180) }); }
      if (page) await page.waitForTimeout(250).catch(() => {});
    }
  } finally {
    await page?.close().catch(() => {});
    await context?.close().catch(() => {});
    await browser?.close().catch(() => {});
  }
  summary.outcome = summary.failed ? (summary.succeeded ? "EVENTSPY_SEASON_PARTIAL" : "EVENTSPY_SEASON_FAILED") : summary.unresolved ? "EVENTSPY_SEASON_PARTIAL" : "EVENTSPY_SEASON_SUCCESS";
  log(summary);
  return summary;
}

async function main() {
  if (process.argv.length !== 4 || process.argv[2] !== "--config") throw new Error("Usage: node collector.mjs --config request.json");
  const request = await readJson(process.argv[3]);
  if (request.version !== 2) throw new Error("Unsupported collector request version");
  const result = await runCollector({
    site: request.site, slot: new Date(request.slot).toISOString(),
    outputRoot: request.output_dir, cacheRoot: request.cache_dir,
    coverage: await readJson(request.coverage_file),
    schedule: request.schedule_file ? await readJson(request.schedule_file) : null,
  });
  if (result.failed) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => {
    console.error(JSON.stringify({ outcome: "EVENTSPY_SEASON_FAILED", error: String(error?.message || error).slice(0, 180) }));
    process.exitCode = 1;
  });
}
