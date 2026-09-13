# New England Patriots EventSpy coverage review — 2026

Reviewed 2026-09-13. This adds the Patriots to the existing shared collector and selected-team website build. All 17 regular-season matchups are represented; the Week 11 bye and preseason are excluded. Numeric NFL game IDs are not invented: `gameId` is null until an authenticated NFL snapshot supplies the real ID through the existing identity binder.

**Coverage is incomplete: four already-reviewed shared EventSpy pages are configured, and thirteen games explicitly report `SOURCE_PAGE_NOT_AVAILABLE` until their exact game pages can be reviewed.** One configured page is the completed Week 1 Seattle game; it is retained for history and skipped by collection. As of this review, only three future games have reviewed source URLs. Unavailable here means no verified URL is configured, not a claim that the provider definitely has no listing.

## Official schedule and neutral venue

- [Patriots schedule](https://www.patriots.com/schedule)
- [Patriots 2026 schedule announcement](https://www.patriots.com/news/patriots-announce-2026-schedule)
- [NFL Patriots schedule](https://www.nfl.com/schedules/2026/by-team/new-england-patriots)
- [Lions announcement: hosting New England in Munich](https://www.detroitlions.com/news/lions-patriots-scheduled-for-week-10-in-munich)

The Week 10 matchup is **New England at Detroit**, November 15 in Munich, Germany, with Detroit the designated home team and venue timezone `Europe/Berlin`. The Patriots schedule page labels that fixture “VS,” but the NFL schedule, Detroit hosting announcement, and Patriots schedule announcement explicitly identify the correct designated home team. The kickoff is 9:30 a.m. Eastern / 3:30 p.m. Munich time. The integration keeps the API's real teams and venue rather than relabeling Munich as Foxborough.

Weeks 17 (Denver) and 18 (Miami) remain date/time TBD in the official Patriots schedule. No local date or event ID is guessed for them.

## Reviewed coverage

| Week | Matchup | Venue-local date | EventSpy source | Provenance |
| --- | --- | --- | --- | --- |
| 1 | NE at SEA | 2026-09-09 | [374440](https://www.event-spy.com/event/seattle-seahawks-seattle-sep-09-2026/374440) | Existing Seahawks coverage |
| 2 | PIT at NE | 2026-09-20 | Unavailable; URL not verified | Official schedule only |
| 3 | NE at JAX | 2026-09-27 | Unavailable; URL not verified | Official schedule only |
| 4 | NE at BUF | 2026-10-04 | Unavailable; URL not verified | Official schedule only |
| 5 | LV at NE | 2026-10-11 | Unavailable; URL not verified | Official schedule only |
| 6 | NYJ at NE | 2026-10-18 | Unavailable; URL not verified | Official schedule only |
| 7 | NE at CHI | 2026-10-22 | Unavailable; URL not verified | Official schedule only |
| 8 | NE at MIA | 2026-11-01 | Unavailable; URL not verified | Official schedule only |
| 9 | GB at NE | 2026-11-08 | [374818](https://www.event-spy.com/event/new-england-patriots-foxborough-nov-08-2026/374818) | Existing Packers coverage |
| 10 | NE at DET (Munich) | 2026-11-15 | Unavailable; URL not verified | Official schedule only |
| 12 | NE at LAC | 2026-11-29 | Unavailable; URL not verified | Official schedule only |
| 13 | BUF at NE | 2026-12-06 | Unavailable; URL not verified | Official schedule only |
| 14 | MIN at NE | 2026-12-10 | [375029](https://www.event-spy.com/event/new-england-patriots-foxborough-dec-10-2026/375029) | Existing Vikings coverage |
| 15 | NE at KC | 2026-12-21 | [375110](https://www.event-spy.com/event/kansas-city-chiefs-kansas-city-dec-21-2026/375110) | Existing Chiefs coverage |
| 16 | NE at NYJ | 2026-12-27 | Unavailable; URL not verified | Official schedule only |
| 17 | DEN at NE | TBD | Unavailable; URL not verified | Official schedule only |
| 18 | MIA at NE | TBD | Unavailable; URL not verified | Official schedule only |

The shared events reuse the exact date, source ID, URL and both team identities from their existing reviewed coverage. No ticket prices or market history are hardcoded. The same collector checks returned event titles/dates before publication and skips completed/started games before opening a browser or making source requests.

Parallel Search queries checked the Patriots name, performer/event URL prefixes, Foxborough/Foxboro/Gillette variants and away-opponent listings. They did not expose further Patriots EventSpy URLs. The EventSpy homepage could be extracted, but its public navigation did not yield a Patriots performer URL. Earlier direct EventSpy game-page extraction returned HTTP 429. These limitations are not proof that the remaining games lack listings.

## Paths and runtime behavior

- Producer coverage: `deployment/eventspy/coverage/patriots.json`
- Website matching copy: `config/eventspy/patriots.json`
- Host installed coverage: `/opt/fanzone-eventspy/coverage/patriots.json`
- Authenticated schedule cache: `/var/lib/fanzone-eventspy/schedules/patriots.json`
- Producer output: `/var/lib/patriotsfz-eventspy-mirror/dev/public`
- Container read-only mirror mount: `/srv/fanzone-eventspy/patriots`
- Browser URL: `/data/eventspy-mirror/patriots/<realGameId>.json`
- Build: `bash template-tools/build-team.sh patriots`

The shared Airflow DAG retains its seven Pacific collection slots and creates named Patriots tasks from `fan_zone_active_sites`. Manual and backfill runs skip. Adding the JSON coverage alone does not install it on the host or seed live snapshots. The additive team rollout must register the Patriots paths and mount/alias; the current collector, existing teams, timer states and output files do not need broad replacement.

Prices continue to load through the team feed at runtime. The website build binds all 17 games to a real Patriots schedule and keeps unavailable games explicit. Wrong-team, ambiguous, or changed-date schedules fail validation instead of publishing misleading ticket links.

## Offline verification

`node --test deployment/eventspy/tests/collector.test.mjs deployment/eventspy/tests/patriots.test.mjs`

At implementation time, 29 tests passed: 24 existing collector tests plus five Patriots tests. The new checks cover exact regular-season identities, opposite-perspective shared events, independent repeated division-game binding, Munich designated home/away and kickoff behavior, no browser requests for completed/unavailable games, and retained Seattle output/history. Synthetic IDs exist only in test memory; they are not production schedule data.

## Completing the missing pages

For each unavailable game, identify a genuine general-admission EventSpy event page and verify both teams, venue-local date and event ID. Exclude parking, tours, club-seat products, ticket packages and unrelated dates. Update the matching coverage row in both repositories together, keeping actual NFL IDs bound from the schedule. Rerun coverage/binding tests and deploy only the reviewed coverage addition. Do not change an unavailable row to authorized merely to eliminate a missing-link display.

Live collection and public serving remain operator-verification steps; this source review did not run either.
