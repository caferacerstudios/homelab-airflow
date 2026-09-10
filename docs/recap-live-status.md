# Recap Airflow rollout — September 10, 2026

## Verified first run

- DAG: sfz_game_recaps
- Run: manual__2026-09-10T16:36:08.088460+00:00
- Result: success; snapshot importer check passed.
- Snapshot timestamp: 2026-09-10T16:36:13.647Z
- Season: 2026
- Website source: ffb26976c7361b120b0ef99f35e990c611782cca
- NFL input: scheduled__2026-09-10T16:15:00+00:00
- New recaps: 0
- BALLDONTLIE requests: 0
- OpenAI requests: 0
- Model: gpt-4o-mini
- OpenAI generation was not exercised by this first run.

## Operation after rollout

- Schedule enabled: 00:45, 03:45, 06:45, 09:45, 12:45,
  14:45, 16:45, 18:45, 20:45, 22:45 America/Los_Angeles.
- Current snapshot: /var/lib/sfz-recaps/current/gameRecaps.json
- Retained snapshots: /var/lib/sfz-recaps/runs/
- Python DAG/hook call the host runner over connection sfz_recap_host.
- The existing Node writer generates missing completed-game recaps.
- The website prebuild imports snapshots through import-recap-snapshot.mjs.
- Imported website data: src/data/nfl/gameRecaps.json.
- A website build is required to publish updated HTML.
- Keys come from the production .env and remain outside Git.
- Source checks cover recap dependencies; unrelated schedule-guide edits remain.
- The installer sets readable permissions on the recap DAG and hook.
- First scheduled execution and public HTML publication remain unverified here.
