# Player statistics audit for the roster DAG work

Reviewed 2026-09-12 against the pinned sources linked below. This describes the baseline before the roster update. The companion importer now uses verified provider IDs for current-roster joins; numeric statistics and season-phase selection are unchanged. No live provider values or host state were inspected by this audit.

## Recommendation

Keep player performance statistics in `sfz_nfl_refresh`. Give the new roster DAG current membership (active, practice squad, reserve), injuries, and transactions. A roster change must not rewrite the season or team attached to historical performance. The build can import the two snapshots independently and join identities. The existing NFL pipeline already fetches season statistics, so moving or duplicating that work in the roster DAG adds requests and publication coupling without solving the roster-source problem.

## Main player statistics path

1. `sfz_nfl_refresh` creates ordinary named `refresh_nfl_snapshot_<slug>` tasks from `fan_zone_active_sites`. It runs ten Pacific-time daily slots at :15 (00,03,06,09,12,14,16,18,20,22), max one task at a time, in `balldontlie_api`.
2. Host `deployment/nfl/refresh_nfl.py` stages a copy of the production Seattle scripts and libraries, then executes `scripts/fetch-nfl.mjs` under cached `node:22-bookworm`. Each non-Seattle staged copy is bound to that team's abbreviation/name/BDL ID and output filename. The production checkout is not changed.
3. The fetcher calls BDL NFL v1 `/teams`, season `/games`, `/players?team_ids[]=<id>`, and `/season_stats`. Statistics requests use `season=<year>`, `team_id=<id>`, and separately `season_types[]=2` / `season_types[]=3`.
4. The `/players` results enrich provider statistics with player name, position, jersey, height, weight, college and experience. They are explicitly treated as a directory, not an authoritative active roster.
5. The two season-stat response arrays are concatenated. Each row is spread unchanged, then supplied a `player` object, `player_id`, `team_id` and season fallback. **Main player yardage, touchdown, tackle, sack, attempts, percentage and average fields are read from provider rows. This path does not sum per-game box scores, calculate fantasy points, or recalculate completion percentage/passer rating.**
6. The combined `<slug>.json` and `players.json` contain `season`, `playerStatsSeason`, `updatedAt`, `currentRoster`, `playerDirectory` and `playerSeasonStats`. Immutable validated snapshots are published behind the team's `/var/lib/<prefix>-nfl/current`; checksums, team identity, season and nonempty-data checks protect publication.
7. The template's normal `npm run build` invokes `build:offline`. It imports the selected NFL snapshot before Astro renders pages; it does not refresh provider statistics. Existing static HTML changes only after a build.

Evidence: [DAG](https://github.com/caferacerstudios/homelab-airflow/blob/72693840b8bf61786230c2f20d05d36b9359a63a/dags/sfz_nfl_refresh.py), [host collector](https://github.com/caferacerstudios/homelab-airflow/blob/72693840b8bf61786230c2f20d05d36b9359a63a/deployment/nfl/refresh_nfl.py), [production fetcher](https://github.com/caferacerstudios/seahawks-fan-zone/blob/8360735e2824dd01c33d9cf71e637c528644896c/scripts/fetch-nfl.mjs), [build import](https://github.com/caferacerstudios/template-fan-zone/blob/79c8f99db95f291732105b26d5e84dd7c497fe77/template-tools/nfl.mjs).

## Season fallback and current roster

If **both** regular-season and postseason statistic arrays for the selected season are empty, the fetcher requests the previous year for both. Schedule `season` remains the selected year, while `playerStatsSeason` records the actual statistics year. If both current and previous year have no stats, the nonempty assertion fails and the Airflow runner retains the previous good published snapshot. This is a one-year fallback, not arbitrary historical search.

Example of behavior, not a report about current live values: a 2026 schedule can accompany 2025 player stats until the 2026 response supplies records. This should stay visibly labeled as 2025 production, even if a player's current roster status changes.

Seattle's `currentRoster` is derived from the separately maintained production `src/data/team/roster.json`; the NFL collector copies that checked-in file into staging and filters it through `isCurrentRosterPlayer`. For non-Seattle teams, the Airflow collector currently stages `{players: []}` on purpose. Therefore the new NFL snapshots alone cannot establish current non-Seattle rosters. The template uses these empty `currentRoster` arrays to show explicit unavailable states. The proposed roster snapshot should become the authoritative roster/build import rather than guessing membership from last season's stats.

Evidence: production `fetch-nfl.mjs` lines 153-222, host `refresh_nfl.py` `stage_source`, template `template-tools/nfl.mjs` `prepareNflSnapshot`.

## What the website calculates or selects

### Roster page /players

`src/pages/players.astro`:
- reads performance rows from `playerSeasonStats`;
- collapses repeated player IDs to one row by selecting the row with the **most numeric fields**, retaining the first on a tie;
- displays numeric fields directly, choosing known aliases where available (e.g. `rushing_attempts` or `carries`, `yards_per_rush` or `rushing_average`);
- passing/rushing/receiving leader = row with greatest corresponding yards;
- sack leader = highest sacks when any numeric sacks row exists, otherwise highest tackles;
- sorts category tables by the first available primary metric;
- does not calculate a combined season total or per-game player average.

### Player detail /players/<id>

`src/pages/players/[playerId].astro`:
- resolves roster/provider IDs and, when necessary, matches an exact lowercased current-roster name;
- among matching NFL rows chooses the row with greatest `passing_yards + rushing_yards + receiving_yards`; this sum is a **row-selection heuristic**, not a displayed official statistic;
- displays position-specific numeric values from that one row;
- hides values equal to zero (so absence on a card need not mean unavailable);
- if no NFL statistics row is found, it can display the latest regular-season career-facts record instead;
- sources profile prose separately from numeric stats.

Evidence: [roster/statistics page](https://github.com/caferacerstudios/template-fan-zone/blob/79c8f99db95f291732105b26d5e84dd7c497fe77/src/pages/players.astro), [player detail](https://github.com/caferacerstudios/template-fan-zone/blob/79c8f99db95f291732105b26d5e84dd7c497fe77/src/pages/players/%5BplayerId%5D.astro).

### Separate career-facts calculations

There is also `scripts/refresh-player-career-facts.mjs`, exposed as a separate npm command. It is **not called by `sfz_nfl_refresh` or the normal offline build**. It matches roster identities to BDL player IDs, requests a range of seasons (default from 2018 through the available stats season), and writes a persistent career artifact.

`src/lib/player-career-facts.mjs` does perform these calculations:
- completion percentage = completions × 100 / passing attempts, rounded to one decimal;
- yards per attempt = passing yards / passing attempts, rounded to one decimal;
- career totals = sum of each recorded regular-season field across stored season/team rows;
- career highs = maximum recorded regular-season passing yards, passing touchdowns, completion percentage and rushing yards;
- passer rating is still copied from `passer_rating` or `rating`, not calculated.

A concrete arithmetic illustration: 20 completions in 30 attempts gives 66.7%; 240 passing yards in 30 attempts gives 8.0 yards/attempt. These illustrate the helper's formulas, not actual player values.

The historical career refresh is an additional source of profile data; do not silently add its potentially many calls to the new roster DAG.

Evidence: [career refresh script](https://github.com/caferacerstudios/template-fan-zone/blob/79c8f99db95f291732105b26d5e84dd7c497fe77/scripts/refresh-player-career-facts.mjs), [career calculations](https://github.com/caferacerstudios/template-fan-zone/blob/79c8f99db95f291732105b26d5e84dd7c497fe77/src/lib/player-career-facts.mjs), [rendered npm scripts](https://github.com/caferacerstudios/template-fan-zone/blob/79c8f99db95f291732105b26d5e84dd7c497fe77/template-tools/upstream-package.json).

## Existing presentation issue to keep separate

The main fetcher concatenates regular and postseason rows, but the two player-page readers above do **not explicitly filter season type before selecting one row**, while their labels state Regular Season. The list and detail page also use different heuristics, which can select different rows. This is a verified code-path risk; live data was not inspected, so do not assert every currently displayed value is wrong.

A focused later fix should normalize/preserve phase at ingestion, select exactly the requested phase, and use the same selection rule for list/detail pages. It should not sum postseason into regular-season totals. Moving statistics to the roster DAG would not fix this reader issue.

The career helper has a separate edge case: if attempts is positive but completions/yards is null, arithmetic coerces null to zero. That calculation should eventually preserve missing values, but this is not necessary to deliver current roster/injury/transaction collection.

## Team statistics are different

`src/lib/team-stats.mjs` computes:
- team points for/against by summing unique, verified completed regular-season game scores;
- win-loss-tie record from those scores;
- points per game = points scored / counted completed games.

It intentionally does **not** reconstruct team passing/rushing totals by summing player rows. The main collector writes `teamSeasonStats: null` until an authoritative team-scoped endpoint is integrated. This is separate from both player season stats and roster state.

Evidence: [team stats](https://github.com/caferacerstudios/template-fan-zone/blob/79c8f99db95f291732105b26d5e84dd7c497fe77/src/lib/team-stats.mjs).

