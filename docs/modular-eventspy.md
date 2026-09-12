# EventSpy for active fan sites

`sfz_eventspy_collect` now uses the existing `fan_zone_active_sites` Airflow Variable. Each enabled site with an `eventspy` block creates its own **Collect tickets: City Team** and **Save ticket receipt: City Team** nodes. This uses ordinary named tasks generated when Airflow parses the DAG, so each team is visible separately. Changing `enabled` takes effect after the next parse. Existing daily-news settings and output directories are preserved.

The checked-in `config/active-sites.json` contains Seattle and Denver examples. The installer merges only the new EventSpy block into the current Variable, preserving existing enabled flags, names, IDs, article prompts and news locations. It checks in the resulting configuration in both repositories. The template uses this checked-in configuration when `TEAM` selects a build.

| Site | Ticket output | Schedule input |
| --- | --- | --- |
| Seahawks | `/var/lib/sfz-eventspy-mirror/dev/public` | `/var/lib/sfz-nfl/current/seahawks.json` |
| Broncos | `/var/lib/boncosfz-eventspy-mirror/dev/public` | `/var/lib/fanzone-eventspy/schedules/broncos.json` |

The `boncosfz` spelling follows the existing Broncos news directory convention. EventSpy and daily-news outputs are separate. This update does not change the recap DAG, daily-article DAG or Seattle NFL refresh DAG.

## Collector behavior

The collector is based on the deployed `/app/season-collector.mjs` exported from `seahawksfanzone-eventspy-season:1`. The existing browser image is reused by its reviewed image ID; no image is rebuilt or pulled. The new source files are mounted read-only into that image. The old image, runner and collector service remain available for rollback.

Snapshots with all four marketplace links retain the deployed `1.0.0` JSON fields, formatting and public path. Snapshots with a genuinely missing marketplace link use `1.1.0` and represent that link as `null`; nonempty unsafe links still fail. See [the scoped missing-link repair](eventspy-missing-provider-links.md) for the consumer-first rollout. No `team` field is added to public ticket snapshots. Team identity lives in the configuration, output directory and run receipts.

Before opening a browser or fetching an event, the collector binds reviewed URLs to genuine schedule game IDs. It skips completed games and games whose confirmed kickoff has passed, retaining their existing JSON and price history unchanged. A stale cached `Scheduled` status therefore cannot keep collecting after a confirmed kickoff. Unknown kickoff times are not treated as midnight starts. Postponed/rescheduled games and mismatched dates are reported for review. Failed or unresolved games keep previous published files.

Denver has 15 verified EventSpy game pages and two unavailable pages for Weeks 17 and 18. The November 1 Chiefs game is event `374756`; `379022` is a VIP tailgate and is excluded. Reviewed URLs are in `deployment/eventspy/coverage/broncos.json`. API game IDs are fetched on the server, never fabricated. The schedule helper resolves a null team ID by abbreviation using the existing production BALLDONTLIE credential. It validates all 17 matchups before replacing the cache. Seattle continues reading its existing NFL snapshots.

Sources checked September 12, 2026:

- https://www.denverbroncos.com/schedule/
- https://www.event-spy.com/performer/denver-broncos-ticket-prices/4626
- https://nfl.balldontlie.io/

## Scheduling handoff

The seven original America/Los_Angeles slots remain: 03:00, 06:00, 09:00, 12:00, 15:00, 18:00 and 21:00. Manual/backfill runs skip. The host admits only slots after successful installation; it never makes an extra collection to test installation. Receipts and reservations survive restarts. Shared events, including Seahawks at Broncos, reuse one raw EventSpy response per slot across both sites. Per-event source reservations prevent same-slot retries and cap daily attempts at seven.

The existing `balldontlie_api` pool serializes these tasks with the Seattle NFL refresh while schedule lookups run. The EventSpy DAG and host also serialize ticket work. Denver's validated schedule cache is reused for six hours; temporary API failures can retain valid cached data. API/date/identity mismatches fail validation rather than relabeling another team's games.

The host installer validates code, current workers and schedules before disabling `sfz-eventspy-season.timer` and its old trial restore timer. It reuses `sfz-airflow-ticket-test.path` and the existing request/response mounts. Only that watcher's worker command changes to `/opt/fanzone-eventspy/bridge.py`. A failed handoff restores previous code and timer states. No running collector is killed; if a job wins the race with installation, let it finish and rerun.

## Checks after installation

```bash
cd /home/laurawkr/homelab-airflow
docker compose exec -T airflow-scheduler airflow dags list-import-errors
systemctl show sfz-eventspy-season.timer sfz-airflow-ticket-test.path --property=Id,ActiveState,UnitFileState
sudo python3 /opt/fanzone-eventspy/bridge.py status
```

The old timer should be disabled/inactive, and the queue watcher enabled/active. The DAG graph should show one named collection task per enabled team. Ticket files appear after the next scheduled slot, not during installation. Review per-team receipts under `/opt/airflow/artifacts/<team>/eventspy` and the collection task logs.

Do not clear/retry an unresolved source attempt to force collection. A pending host reservation can mean the Docker collector is still running. Inspect its container and worker status first.

## Rollback

Pause the EventSpy DAG and let any running/queued task finish before rolling back. Other DAGs remain running.

```bash
cd /home/laurawkr/homelab-airflow
docker compose exec -T airflow-scheduler airflow dags pause sfz_eventspy_collect
sudo python3 deployment/eventspy/install.py --rollback
```

This restores the original queue worker and timer states. It retains all ticket files and price history. Leave the modular DAG paused after rollback. The original handoff backup is root-owned at `/var/lib/fanzone-eventspy/install-backups/original.json`.

For another team, add a reviewed coverage file, an active-site entry with its own ticket output and schedule path, and the matching template location/theme. The same DAG and collector create the new task. Never reuse Seattle's directory or relabel Seattle game IDs.
