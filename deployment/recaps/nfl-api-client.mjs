// Shared pacing applies to every endpoint, page and retry within this process.
// Other applications using the same API key still share its account allowance.
export function createNflApiClient({
  apiKey,
  intervalMs = 15000,
  timeoutMs = 30000,
  maxRateLimitRetries = 2,
  maxPages = 100,
  fetchImpl = globalThis.fetch,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now = () => Date.now(),
} = {}) {
  if (!Number.isFinite(intervalMs) || intervalMs < 0) throw new Error("Invalid NFL_REQUEST_INTERVAL_MS");
  let lastRequestAt = null;
  let requestCount = 0;

  async function get(endpoint, params = {}) {
    if (!apiKey) throw new Error("Missing BALLDONTLIE_API_KEY env var.");
    const url = new URL(`https://api.balldontlie.io/nfl/v1${endpoint}`);
    for (const [key, value] of Object.entries(params)) {
      if (value == null) continue;
      for (const item of [].concat(value)) url.searchParams.append(key, String(item));
    }
    for (let retry = 0; ; retry++) {
      if (lastRequestAt !== null) await sleep(Math.max(0, lastRequestAt + intervalMs - now()));
      lastRequestAt = now();
      requestCount++;
      let response;
      try {
        response = await fetchImpl(url, {
          headers: { Authorization: apiKey },
          signal: AbortSignal.timeout(timeoutMs),
        });
      } catch {
        // Provider bodies and arbitrary fetch errors can contain credentials.
        throw new Error(`NFL request failed or timed out: ${endpoint}`);
      }
      if (!response.ok) {
        await response.body?.cancel().catch(() => {});
        if (response.status === 429 && retry < maxRateLimitRetries) {
          const header = response.headers.get("retry-after");
          const seconds = header?.trim() && /^\d+(?:\.\d+)?$/.test(header.trim()) ? Number(header) : NaN;
          const until = header ? Date.parse(header) : NaN;
          const requested = Number.isFinite(seconds) ? seconds * 1000 : Number.isFinite(until) ? until - now() : 60000;
          const delay = Math.max(intervalMs, 60000, requested);
          // Never retry earlier than a long provider delay; fail this run instead.
          if (delay > 300000) throw new Error(`NFL HTTP 429: retry deferred beyond this run's limit (${endpoint})`);
          await sleep(delay);
          continue;
        }
        throw new Error(`NFL HTTP ${response.status}: ${endpoint}`);
      }
      try { return await response.json(); }
      catch { throw new Error(`NFL response is not valid JSON: ${endpoint}`); }
    }
  }

  async function pagedGet(endpoint, params = {}) {
    const rows = [];
    const seen = new Set();
    let cursor = null;
    for (let page = 0; page < maxPages; page++) {
      const data = await get(endpoint, { ...params, per_page: 100, ...(cursor === null ? {} : { cursor }) });
      if (!data || !Array.isArray(data.data) || (data.meta !== undefined && (!data.meta || typeof data.meta !== "object" || Array.isArray(data.meta)))) {
        throw new Error(`NFL invalid paginated response: ${endpoint}`);
      }
      rows.push(...data.data);
      // /teams is unpaginated; terminal pages may omit next_cursor.
      const next = data.meta?.next_cursor;
      if (next === null || next === undefined) return rows;
      const valid = typeof next === "number" ? Number.isSafeInteger(next) && next >= 0 : typeof next === "string" && /^\d+$/.test(next);
      if (!valid || !data.data.length || seen.has(String(next))) throw new Error(`NFL invalid or repeated pagination cursor: ${endpoint}`);
      seen.add(String(next));
      cursor = next;
    }
    throw new Error(`NFL pagination exceeded ${maxPages} pages: ${endpoint}`);
  }
  return { pagedGet, get requestCount() { return requestCount; } };
}
