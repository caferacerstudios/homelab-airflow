# Game-day and where-to-watch research

This is an independent producer and an explicitly enabled website import. It gathers cited game-day logistics and viewing information without changing the NFL, roster, news, recap or ticket publications. The new DAG does not build or deploy a website.

Source added in this change is not evidence that the host installer has run, research has completed or production is using the output. Inspect those three stages separately.

## Flow and existing system boundaries

`sfz_game_guides` creates a named `refresh_guides_<slug>` task followed by `save_run_receipt_<slug>` for each team enabled in both `fan_zone_active_sites` and `config/game-guides.json`. The separate guide policy initially enables only `seahawks`; other active team pipelines keep their existing configuration.

The refresh task uses the dedicated `sfz_guides_host` SSH connection. Its Ed25519 key is restricted to `deployment/guides/ssh_entrypoint.py`. The entrypoint accepts only a bounded encoded `check` or `refresh` request; a request cannot select an arbitrary shell command, runtime destination or credential path.

The host reads the existing shared team registration in `/opt/fanzone-shared/settings.json` and the selected team's validated NFL snapshot. It derives its independent runtime from the registered news path. Existing article source domains and prompts remain untouched. Research uses the existing OpenAI Responses helper and host credential; no additional search-provider account or Airflow provider is required.

| Team | Guide publication |
| --- | --- |
| Seahawks | `/var/lib/sfz-guides/current` |
| Broncos | `/var/lib/boncosfz-guides/current` |
| Packers | `/var/lib/packersfz-guides/current` |
| Vikings | `/var/lib/vikingsfz-guides/current` |
| Chiefs | `/var/lib/chiefsfz-guides/current` |

Only enabled guide teams are installed and scheduled. Denver's established `boncosfz` spelling is preserved. The runtime is separate from website checkouts, served releases and all other producer roots.

## Schedule and workload

| Setting | Value |
| --- | --- |
| DAG | `sfz_game_guides` |
| Timetable | Daily at 04:45 `America/Los_Angeles` |
| Initial state | Paused |
| Catchup / retries | Disabled / zero |
| Active runs / tasks | One / one |
| Work task / SSH timeout | 35 minutes / 33 minutes |
| Receipt task timeout | Two minutes |
| Pool | Existing `default_pool` |
| Initial activated team | Seahawks |

Global Airflow parallelism still applies. This DAG may queue behind other work; its clock time is not a guaranteed completion time. It does not change other DAGs' schedules, pools or pause states.

The independent `config/game-guides.json` controls enabled teams, research horizon, refresh interval, per-run game budget and model. The initial policy looks ahead 14 days and limits each team run to three games. Near-term games receive priority; missing later regular-season games can be filled within the same bounded budget. Completed games and existing historical guide content are retained. A single run is not represented as researching every remaining game.

Only official schedule identities select games. Research must distinguish dated event announcements from general venue guidance. Sounder, event times, closures, watch parties, broadcast availability and regional streaming restrictions require appropriate evidence. Unconfirmed items remain unknown or absent. Source URLs and timestamps accompany accepted facts; retrieval time is not an announcement date.

## Install the new host handoff

First review and merge the Airflow and website changes through the normal repository workflow. On `wkr`, update the clean Airflow checkout to the reviewed revision without overwriting local edits. Review `config/game-guides.json` and leave the initial guide activation limited to Seattle.

Run as `laurawkr`, without running the whole installer under `sudo`:

```bash
cd /home/laurawkr/homelab-airflow
python3 -B deployment/guides/install.py
```

The installer checks registered teams, source and input prerequisites before preparing its guide runtime, generates or reuses only `secrets/sfz_guides_ed25519`, appends its restricted public-key entry when absent and configures only `sfz_guides_host`. It verifies the actual Airflow DAG graph and performs a source-free SSH check for each guide-enabled team.

The installer neither calls the research provider nor publishes a snapshot. It does not unpause or trigger any DAG. It does not change Docker Compose, nginx, Cloudflare, existing provider settings, other SSH connections, the active-site Variable or existing snapshots.

For a direct source-free host check:

```bash
cd /home/laurawkr/homelab-airflow
python3 - <<'PY'
import json
import subprocess
from pathlib import Path
site = json.loads(Path('config/active-sites.json').read_text())['seahawks']
subprocess.run([
    'python3', '-B', 'deployment/guides/refresh_guides.py',
    '--check', '--site-json=' + json.dumps(site),
], check=True)
PY
```

This example reads the checked-in fallback. Use the live Variable's selected site when its values differ. The installer itself reads the current Variable with fallback and preserves it.

## First research publication and website preview

After installation, unpause `sfz_game_guides`, trigger one manual run in the Airflow UI and inspect both Seattle tasks. Airflow also requires an unpaused DAG to execute manual tasks. Check for an already queued or running scheduled run before triggering another. If preview review is still pending, pause the DAG after the run finishes. This run makes paid OpenAI requests; the source-free check above does not.

The completed snapshot contains:

| File | Contract |
| --- | --- |
| `game-day-guides.json` | Team/season-tagged guide collection keyed by real regular-season game ID |
| `watch-guide.json` | Team/season-tagged viewing information for those games |
| `manifest.json` | Schema, pipeline, team, season, run ID, timestamp and SHA-256 for both files |

The hook requires a single `SFZ_GUIDES_RECEIPT=` record with the selected team/run, pipeline `guides`, supported season, exact file list and a bounded OpenAI request count. Receipts are saved under `/var/lib/homelab-pipelines/<slug>/guides/<run-hash>/receipt.json` on the host. Successful Airflow execution and snapshot publication do not mean the public site was rebuilt.

In `template-fan-zone`, the importer is disabled by default. Enable `FAN_ZONE_GUIDES_ENABLED=1` only for the selected preview/build using the existing team-build workflow. It reads the separate guide root, verifies manifest checksums and matches records to the selected team's actual NFL schedule before copying data into the isolated build workspace. The importer has no provider calls and no production deployment action.

Review the Game Day Guide and Where to Watch sections on `/games/<gameId>` in preview. Confirm source links, actual opponent/date/home-away matches, unknown fields and retained manually authored records. Only then include the setting in an explicitly chosen production build using the existing reviewed deployment process. The new DAG does not run that process.

## Failure handling and recovery

Failed research or validation retains the previous good guide publication. Do not replace existing JSON with an empty fallback after a provider error. Preserve failed run evidence and read its log before retrying; a provider timeout does not prove the request was unbilled. Completed-run reuse and cached responses reduce duplicate requests but are not an external billing guarantee.

### Source-ID validation failure

The initial writer schema allowed empty or arbitrary `sourceIds`, while the
publication validator required one to four distinct IDs from the actual cited
source registry. A response could satisfy the provider schema and still fail
with `Every guide fact must cite known retrieved source IDs`. The old writer
response was then reused for the same event, policy and research day.

The writer now receives a schema restricted to the retrieved IDs, with one to
four IDs per fact. Missing information must use null or empty item lists.
Duplicate IDs, unsupported claims and incorrect event dates remain rejected.
Validation errors identify the affected field without logging research bodies.

Writing uses the separate `guide-citations-v2` cache stage. The research prompt
and cache identity are unchanged, so an eligible cached research response is
reused and only the corrected writing step needs a new request. Original
`guide-request.json` and `guide-response.json` files are retained for inspection.
Fresh research still uses at most one research request and one writing request
per selected game; there is no automatic repair loop. A later research day or
changed event/policy can require new research as before.

After committing the fix in `/home/laurawkr/homelab-airflow`, clear the failed
`refresh_guides_seahawks` task and its downstream receipt task for the affected
run, or trigger one new run if none is active. Keep the DAG unpaused while the
tasks run. No installer rerun, container restart, cache deletion or website build
is needed for this correction. Review the first accepted snapshot before using
it in a website build.

A malformed activation policy stops this DAG instead of enabling every active team. Enabling a team for other pipelines does not enable its guide research. Adding guide teams requires the separate policy entry, approved existing host registration, usable team NFL input and guide installer checks.

If generated guides need to stop appearing, omit `FAN_ZONE_GUIDES_ENABLED=1` from the next reviewed build and use the existing deployment workflow. Pausing the DAG stops future scheduled research, but it does not rewrite already deployed static HTML. Retain snapshots and receipts for inspection rather than deleting a lock or recursively changing `/var/lib` permissions.

## Verification

Run the new tests with the deployed Airflow 3.3.1 / SSH-provider environment:

```bash
python3 -m pytest -q \
  tests/test_guides_config.py tests/test_guides_core.py tests/test_guides_dag.py \
  tests/test_guides_hook.py tests/test_guides_ssh.py tests/test_guides_install.py
```

Producer tests cover payload identity, evidence validation, bounded selection and publication behavior separately. The orchestration tests exercise the actual paused Airflow graph and Pacific DST schedule, activation intersection, receipt isolation and restricted SSH command grammar without live source requests. Host installation and rendered preview remain deployment checks to perform on `wkr`.

Local validation on September 12, 2026 passed 32 tests and 38 subtests under Airflow 3.3.1 with SSH provider 6.0.1. A separate integration check used mocked research responses with the real producer, verified NFL fixture files, immutable publication, same-run reuse, the website importer and a full 157-page Astro build. The generated game-page HTML contained the guide and viewing information. This was a fixture test, not a live research run or host deployment.
