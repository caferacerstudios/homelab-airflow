# NFL collection from modular template sources

After the production checkout moved to `template-fan-zone`, the NFL collector
copied unrendered executable sources. Node stopped at `const {team}Score` in
`schedule.mjs`; `schedule-guide.mjs`, output filenames and division membership
also contain template tokens. SSH authentication was successful.

The host runner now recognizes the complete modular template staging contract
and resolves identity, location and division tokens in its private `scripts/`
and `src/lib/` copies. It then verifies the provider's team ID, abbreviation and
full name before fetching games. The existing legacy-source path remains
supported. In the isolated Seattle watch-guide copy, only the matchup, result
and official URL identity tokens are resolved so existing schedule reconciliation
still works. Literal opponent names, dates and other facts are preserved. A
verified current snapshot continues to supply canonical schedule history; raw
template seed history is not imported.

This change does not modify website checkouts, publish website builds, restart
containers, change DAG schedules or replace an existing snapshot on failure.

## Apply after merging the fix

Run as `laurawkr` on `wkr`:

```bash
cd /home/laurawkr/homelab-airflow &&
test "$(git branch --show-current)" = main &&
git pull --ff-only origin main &&
python3 -B deployment/nfl/refresh_nfl.py --check
```

If any command fails, stop and inspect that output. `--check` verifies local
prerequisites and the template staging contract without API requests. The
restricted SSH entrypoint loads this runner for every invocation, so no service
restart or NFL installer rerun is required.

In Airflow, trigger one new run of `sfz_nfl_refresh`. Keep this DAG unpaused while
it runs. Let the five refresh tasks and their receipt tasks finish. The existing
API pool and host lock still serialize collection. Old failed runs remain in
history; clearing them is unnecessary for a new run.

Once the fresh NFL run succeeds, continue installing and testing `sfz_game_guides`
using `docs/game-guides-airflow.md`. That DAG requires verified, fresh NFL inputs.
For a manual guide test, it must be unpaused while tasks run; pause it after the
test if preview review is still pending. Do not update the production website
checkout as part of this collector repair.

## Offline verification

```bash
python3 -B -m unittest discover -s tests -p test_refresh_nfl.py -v
SFZ_TEMPLATE_SOURCE=/path/to/template-fan-zone \
  python3 -B -m unittest discover -s tests -p test_nfl_template_staging.py -v
```

The second suite executes the real Node fetcher against mocked HTTP for each of
the five configured teams, using raw template sources. It checks identities,
filenames, home/away records, standings, historical data, source integrity and
rejection of incorrect provider identities before further requests. It needs
Node and the template checkout; it does not need Docker or API credentials.
