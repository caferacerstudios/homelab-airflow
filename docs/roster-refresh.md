# Fan Zone roster, injuries and transactions

Status: implementation prepared against the checked-in five-team deployment on
2026-09-12. This document describes the new pipeline, not evidence that it has
been installed or run on `wkr`.

## Purpose and existing behavior

Seattle currently combines checked-in `src/data/team` JSON with
`scripts/refresh-team-roster.mjs` in the production build. The stored records
contain curated historical updates. The old collector reads official team pages,
but its transaction parser recognizes only practice-squad signings/releases.
Its injury parser can attach the retrieval date to an older report. Non-Seattle
NFL collections intentionally do not infer current membership from the provider's
player directory or last season's statistics.

`sfz_roster_refresh` moves routine personnel collection into Airflow and publishes
an independent, team-tagged snapshot. Website builds import that snapshot. The
existing daily article, NFL refresh, recap and EventSpy DAGs retain their own
schedules, connections, output formats and paths. No OpenAI generation or additional
balldontlie requests are added by the roster collector.

## Scheduling and configuration

| Setting | Value |
| --- | --- |
| DAG | `sfz_roster_refresh` |
| Schedule | `30 5,17 * * *`, `America/Los_Angeles` (05:30 / 17:30 Pacific) |
| Active sites | Existing `fan_zone_active_sites` Airflow Variable |
| Work task | `refresh_roster_<slug>` |
| Receipt task | `save_run_receipt_<slug>` |
| Display name | Includes the configured city and team name |
| Concurrency | One active run and one active task in this DAG |
| Pool | `default_pool`; existing provider pool is unchanged |
| Retries / catchup | One refresh retry after two minutes; receipt task 0 / false |
| Initial state | Paused on first creation |
| SSH connection | New, dedicated `sfz_roster_host` |
| Source process | Cached `node:22-bookworm`; eight-minute collector limit |

Changing a site's existing `enabled` flag controls this DAG as it controls the
other Fan Zone DAGs. Tasks are ordinary named tasks constructed at parse time;
allow the DAG processor to refresh before expecting the graph to change. The
checked-in config is only the existing fallback when the Variable is absent.
An invalid configuration fails visibly rather than silently selecting Seattle.

No new fields are inserted into the Variable. This matters because the deployed
EventSpy worker uses a strict copy of the shared validator. Roster paths derive
from each site's registered `news_snapshot_dir`: replace `-news/current` with
`-roster/current`. Host authorization validates identity and the registered path.

| Team | Publication | Official source host |
| --- | --- | --- |
| Seattle Seahawks | `/var/lib/sfz-roster/current` | `www.seahawks.com` |
| Denver Broncos | `/var/lib/boncosfz-roster/current` | `www.denverbroncos.com` |
| Green Bay Packers | `/var/lib/packersfz-roster/current` | `www.packers.com` |
| Minnesota Vikings | `/var/lib/vikingsfz-roster/current` | `www.vikings.com` |
| Kansas City Chiefs | `/var/lib/chiefsfz-roster/current` | `www.chiefs.com` |

`boncosfz` retains the existing deployed spelling. The source registry in
`deployment/roster/teams.json` contains reviewed domains, names, abbreviations and
local time zones; it is not a second active-team configuration. A future team
needs its official source shape reviewed and registered before enabling this
pipeline, along with the existing shared host registration and website support.

## Sources and interpretation

Each run reads three official pages: `/team/players-roster/`,
`/team/injury-report/`, and `/team/transactions/<calendar-year>`. Transaction
calendar year is independent of the NFL season during January and February.
Pages must stay on the selected club's HTTPS host. No arbitrary source URL or
secret is accepted from the Variable.

**Roster.** Read all observed active, practice-squad and reserve sections; retain
the official wording alongside normalized membership categories. Keep stable
player IDs and historical entries. A player disappearing from the roster becomes
historical; absence alone does not invent a release transaction. Unexpected empty,
small or substantially changed rosters stop publication for inspection.

**Transactions.** Preserve each dated official log entry and source link, including
multi-player and multi-action descriptions. Generic events have stable event IDs
and related player IDs where recognized; the website does not create fake player
links. Retain existing curated history instead of replacing it. Exact duplicates
of new official events are deduplicated, but overlapping legacy curated and
source-log descriptions may both remain. Source dates have day precision; the
stored noon-UTC timestamp is not a claimed announcement time.

**Injuries.** Select the report table for the correct team. An optional verified
NFL snapshot supplies the report week's game context. Weekday dates inferred
from that schedule are explicitly identified as inferred, not source publication
timestamps. Missing, stale, unsupported or unconfirmed report context yields an
unavailable state. Historical observations remain stored; `currentReportKeys`
selects only the current report. Empty current reports clear this selection.
No missing row proves that a player is healthy. Current reserve membership is
separate from a current practice/game-status report.

`sourceCheckedAt` records when a page was checked; observation dates retain their
own meaning. These pages are not a permanent feed contract: a club changing its
HTML may require a parser adjustment. Failed roster or transaction validation
preserves the previous snapshot. An unavailable injury report can publish with
an explicit reason and the earlier archive. This includes an injury-page HTTP or
network failure, missing club table, or an unrecognized report format/status.
The earlier injury `asOf` stays unchanged, current report keys are cleared, and
the receipt records `injuryAvailability: unavailable` and its reason. A verified
NFL snapshot from an older season likewise makes injury context unavailable;
it does not block the current official roster or transaction log. The DAG supplies
official reports, not medical conclusions.

## Publication and website contract

The restricted SSH entrypoint accepts an encoded team/run request. The host checks
that team against `/opt/fanzone-shared` registration, then collects in a temporary
run directory as `laurawkr`. It does not edit the website checkout. Docker has a
read-only root filesystem, read-only collector source, a writable work mount,
resource limits and no mounted credentials or Docker socket.

Each immutable snapshot contains `roster.json` (schema 1), `injuries.json`
(schema 2), `transactions.json` (schema 1), and `manifest.json`. The manifest
contains `pipeline: roster`, schema version, team slug, Airflow run ID, source
commit, timestamp, source request count and SHA-256 hashes of the three files.
After validation, an atomic `current` symlink selects the completed snapshot.
Per-team locking avoids overlapping publishers. A completed run can be replayed
without recollecting or rolling back a newer publication. Snapshots are retained;
no new retention/deletion policy is introduced.

On Seattle's first run only, existing production `src/data/team` files seed the
historical archive and stable IDs. Other teams never inherit Seattle content.
Existing NFL input is checked for team, season, checksums, timestamps and fixture
markers. If none exists, roster/transactions can still publish, but injury period
and provider-ID enrichment may be unavailable. Present invalid NFL input fails
instead of silently using an unverified source.

The downstream receipt task stores the manifest at
`/opt/airflow/artifacts/<slug>/roster/<sha256(run-id)>/receipt.json`. Collector
logs and run material stay under each roster runtime's `runs` directory.

The template imports the selected roster after its NFL/EventSpy data steps, so
those steps cannot overwrite current membership. Performance rows and their
statistics year remain unchanged. Numeric provider IDs are joined only when
verified; name similarity does not attach another player's stats. Seattle's
production companion change places this importer after the NFL import and removes
the old collector from routine prebuild. Its explicit manual refresh command
remains available.

A missing snapshot permits the staged cutover: Seattle retains its prior data;
other teams show unavailable/empty personnel sections. A present invalid snapshot
stops the build. Data older than 72 hours is warned about; a stricter
`ROSTER_SNAPSHOT_MAX_AGE_HOURS` is optional. A website build is still required to
publish changed static HTML. This DAG does not build, deploy or switch the preview.

## Install and first run on wkr

Use the reviewed Airflow changes in `/home/laurawkr/homelab-airflow`, and the
companion template/production changes in their respective repositories. Commit
roster runner source before installing: the host records and verifies its source
commit. Do not copy these source files over unknown local edits.

```bash
cd /home/laurawkr/homelab-airflow
python3 deployment/roster/install.py
```

Run as `laurawkr`, not with `sudo`. The installer requests sudo only to create the
new roster runtime directories. It reads current active sites, verifies all selected
host paths, creates the dedicated restricted SSH key/connection, and checks the
new DAG and source-free SSH request using the existing Airflow containers. It does
not modify the Variable, other connections, pools, old DAG pause states, preview,
existing snapshots or service timers. It does not collect source pages. If the
cached Node image is missing, the error supplies the required `docker pull` command.

The existing DAG-processor bind mount discovers the new files; no image rebuild
or stack recreation is required. After successful installation and DAG discovery,
unpause only the new DAG and trigger its first collection:

```bash
cd /home/laurawkr/homelab-airflow
docker compose exec -T airflow-scheduler airflow dags unpause sfz_roster_refresh
docker compose exec -T airflow-scheduler airflow dags trigger sfz_roster_refresh
```

Inspect the five work tasks and receipts in Airflow. Keep existing team NFL
snapshots current so injury-week and identity enrichment can be validated. The
installer itself does not prove a live source refresh succeeded. If the first run
fails, inspect its task/collector log; do not replace good snapshots manually.

Once the selected team succeeds, rebuild using the updated template helper:

```bash
cd /home/laurawkr/templatefanzone
bash template-tools/build-team.sh seahawks
# Select broncos, packers, vikings, or chiefs for the other builds.
```

Run builds one at a time: each replaces the same template `dist` preview. The
helper mounts the selected roster parent read-only, which lets `current` resolve
to its immutable run. Custom Docker builders also need the whole parent mounted,
not just the symlink. Production Seattle follows its separate reviewed build and
publish process; installing this DAG alone does not deploy it.

## Operations and rollback boundary

- Source/parse error: inspect the selected team's `collector.log` and official
  page; the Airflow task also shows a bounded tail of the collector error. A failed
  refresh retries once after two minutes; if it still fails, the last good
  publication remains selected. Do not label old data fresh.
- Injury unavailable: examine `availabilityReason`, report period and NFL schedule
  context. Roster and dated transactions may still be valid.
- Wrong path/team/manifest: correct the configuration or source inconsistency;
  never bypass identity/checksum validation to force a build.
- Need to suspend updates: pause only `sfz_roster_refresh`. Existing snapshots
  remain usable and the other four pipelines continue normally.
- Revert code through Git if necessary. Reverting the importer/DAG does not require
  deleting roster snapshots, credentials or history. Do not use old EventSpy or
  five-team handoff installers to undo this additive pipeline.

### Apply the roster resilience update

After merging the fix, update the existing host checkout and trigger a new run:

```bash
cd /home/laurawkr/homelab-airflow
git pull --ff-only origin main
docker compose exec -T airflow-scheduler airflow dags trigger sfz_roster_refresh
```

Allow the DAG processor to refresh the mounted DAG before triggering if the new
retry setting is not visible yet. The host collector runs directly from this
checkout, so no installer, image rebuild, or stack restart is required. Keep local
Compose/LAN access settings as they are. The website still imports the completed
snapshot on its next normal build.

The September 16 task log confirms successful SSH authentication and a collector
exit, but hides the collector's underlying error. A September 17 source review
reproduced an injury-only failure for Minnesota: its official injury page returned
HTTP 200 with only the opponent's club table. Seattle's current sources parsed
successfully at review time; its original host log is needed to identify that
specific failure. Regression tests cover these unavailable-report cases without
weakening required roster/transaction or publication checks.

## Validation and reference files

Focused checks use fixtures and recorded official HTML, not live publication.
They cover all five teams, task naming/enable flags, Pacific DST slots, malformed
inputs, immutable publication, replay and team isolation, injury archive lifecycle,
provider-ID joins, unchanged statistics, and read-only build mounts. Actual Airflow
3.3.1 is used for DAG import checks.

```bash
python3 -m unittest discover -s tests -p 'test_roster_*.py'
node --test deployment/roster/tests/collector.test.mjs
```

Run Python DAG tests inside the configured Airflow environment. Website tests and
build details are in its `docs/roster-airflow.md`. Implementation entry points:
`dags/sfz_roster_refresh.py`, `dags/sfz_roster_hook.py`, and
`deployment/roster/{install.py,ssh_entrypoint.py,refresh_roster.py,collector.mjs,teams.json}`.
See [player statistics audit](player-statistics.md) for the separate numerical
statistics pipeline and its current presentation limitations.
