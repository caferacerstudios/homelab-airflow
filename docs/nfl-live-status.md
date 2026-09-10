# NFL Airflow status — 2026-09-10

- DAG: sfz_nfl_refresh; schedule enabled during this checkpoint.
- Schedule: ten runs daily, America/Los_Angeles.
- Successful collection: manual__2026-09-10T07:44:17.663826+00:00.
- Snapshot timestamp: 2026-09-10T07:47:08.367Z.
- API requests for that collection: 12.
- Snapshot import and validation passed.
- Website build-fetch replacement merged into main: 147f29b.
- Production uses npm run build directly on wkr.
- Production build currently fails on preseason standings validation.
  That repair is deferred; a successful production build is not yet recorded.
- First scheduled NFL run remains to be observed.
- Dev deployment was not changed.
