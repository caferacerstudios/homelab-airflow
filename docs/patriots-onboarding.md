# Patriots additive rollout

This adds New England as the sixth site in the existing Fan Zone system. It uses
`patriots`, `NE`, BALLDONTLIE team ID `1`, and the `patriotsfz` data prefix. The
checked-in entry starts with `enabled: false`. The five current teams keep their
existing configuration and output locations.

The code review establishes support; it does not establish that these changes
or any Patriots snapshots are installed on `wkr`. Confluence remains unchanged
until Laura approves the reviewed additions.

## What belongs to this rollout

- Both repositories' active-site configuration, selected-team style/favicon,
  selected-team history/banner, official personnel-source registry, reviewed
  EventSpy coverage, and `build-team.sh patriots` support.
- The existing six DAGs produce separate named Patriots tasks after activation:
  NFL refresh, roster/injuries/transactions, daily articles, game recaps,
  EventSpy ticket collection, and Game Day Guides/Where to Watch.
- NFL refresh continues to provide player statistics. Daily articles and final
  game recaps remain separate pipelines and separate snapshots.
- No new Airflow service, pool, SSH connection, DAG definition or scheduler is
  needed. Existing schedules and enabled-team selection are reused.

| Data | Patriots location |
| --- | --- |
| Daily articles | `/var/lib/patriotsfz-news/current` |
| Approved photo bucket | `/var/lib/patriotsfz-news/photos` |
| NFL schedule, player statistics, standings | `/var/lib/patriotsfz-nfl/current` |
| Game recaps | `/var/lib/patriotsfz-recaps/current` |
| Roster, injuries, transactions | `/var/lib/patriotsfz-roster/current` |
| Game guides and broadcast guidance | `/var/lib/patriotsfz-guides/current` |
| EventSpy output | `/var/lib/patriotsfz-eventspy-mirror/dev/public` |
| EventSpy schedule cache | `/var/lib/fanzone-eventspy/schedules/patriots.json` |

## 1. Bring the reviewed code to the two existing checkouts

Apply the reviewed commits to `/home/laurawkr/homelab-airflow` and
`/home/laurawkr/templatefanzone` using the normal Git workflow. Inspect `git status`
first; preserve local files and changes. Do not reset either checkout or rerun
the old all-team/ownership-handoff installers. Roster and guide source preflight
requires their reviewed source to be committed and clean.

Keep Patriots disabled in both checked-in active-site files at this stage.
`fan_zone_active_sites` is the runtime authority: a missing Variable falls back
to the checked-in JSON, whereas a present Variable is independent of that file.
A source update alone must not schedule the new team before its host is ready.

## 2. Export only the current site Variable and stage an additive change

Run as `laurawkr`. The following reads the Variable, falling back to the mounted
configuration only when it is absent. It does not export credentials or any
other Airflow Variables. Use a new plan directory for each attempt.

```bash
cd /home/laurawkr/homelab-airflow
python3 -B - <<'PY'
import json
import os
from pathlib import Path
import subprocess

code = '''
import json
from pathlib import Path
from airflow.models.variable import Variable
from fan_zone_config import validate_sites
raw = Variable.get('fan_zone_active_sites', default_var=None)
if raw is None:
    raw = Path('/opt/airflow/config/active-sites.json').read_text()
validate_sites(raw)
print('ACTIVE_SITES_EXPORT=' + json.dumps(raw))
'''
result = subprocess.run([
    'docker', 'compose', 'exec', '-T',
    '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONPATH=/opt/airflow/dags',
    '-e', '_AIRFLOW_PROCESS_CONTEXT=server',
    'airflow-scheduler', 'python', '-B', '-c', code,
], check=True, text=True, capture_output=True)
rows = [line[len('ACTIVE_SITES_EXPORT='):] for line in result.stdout.splitlines()
        if line.startswith('ACTIVE_SITES_EXPORT=')]
if len(rows) != 1:
    raise SystemExit('Could not read exactly one active-site export; nothing saved')
path = Path('/home/laurawkr/patriots-current-sites.json')
# Exclusive creation protects an earlier export from accidental replacement.
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    json.dump(json.loads(rows[0]), stream, indent=2)
    stream.write('\n')
print('Saved only the active-site configuration:', path)
PY

python3 -B deployment/shared/prepare_patriots.py --stage \
  --current /home/laurawkr/patriots-current-sites.json \
  --plan /home/laurawkr/patriots-onboarding
```

The plan contains the original values, a disabled candidate, and an enabled
candidate. Every existing site's value, custom prompt and enabled flag is
preserved. An existing conflicting/enabled Patriots entry stops preparation.
Review the difference before applying anything. Keep the original export.

## 3. Add only the new team's host registration and empty paths

```bash
cd /home/laurawkr/homelab-airflow
sudo python3 -B deployment/shared/prepare_patriots.py --check-host \
  --plan /home/laurawkr/patriots-onboarding

sudo python3 -B deployment/shared/prepare_patriots.py --prepare-host \
  --plan /home/laurawkr/patriots-onboarding
```

`--check-host` is source-free and read-only. Preparation adds only Patriots to
`/opt/fanzone-shared/settings.json`, backs up the prior registry, creates the
new team's data/photo directories, copies the existing news model configuration
only if the new one is missing, and installs only `coverage/patriots.json` under
`/opt/fanzone-eventspy`. Existing registry entries are retained verbatim as JSON
values. Conflicting Patriots paths/coverage/registration stop the helper.

This does not create any `current` snapshot, write a schedule, refresh provider
data, install runner code, modify a live Variable, alter services or connections,
or change a DAG pause state. A failed partial preparation may leave new empty
Patriots directories or its coverage; keep them and inspect the reported issue.
Rerunning the same reviewed plan reuses matching paths; it does not delete them.

The existing EventSpy settings contain the image ID, activation time and coverage
directory; they have no per-team registry to replace. The normal generic bridge
reads the new team's coverage and destinations from the task request. Do not
rerun the original EventSpy handoff installer to add this team.

## 4. Check the actual preview's ticket mount before activation

The shared preview serves `/home/laurawkr/templatefanzone/dist` through
`templatefanzone-web` on port `4326`. A team build changes that one preview.
It does not create a separately hosted public Patriots site.

```bash
docker inspect templatefanzone-web \
  --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
docker exec templatefanzone-web nginx -T
```

The new ticket feed needs a read-only bind from
`/var/lib/patriotsfz-eventspy-mirror/dev/public` to
`/srv/fanzone-eventspy/patriots`, and the active nginx server needs this alias
using the same response headers as the other team feeds:

```nginx
location ^~ /data/eventspy-mirror/patriots/ {
    alias /srv/fanzone-eventspy/patriots/;
    add_header Cache-Control "no-store" always;
    add_header X-Robots-Tag "noindex, nofollow" always;
    add_header X-Content-Type-Options "nosniff" always;
}
```

Inspect and extend the **actual** container creation/Compose configuration and
its mounted nginx file. Docker cannot add a bind to a running container. Reuse
all existing mounts, port mappings and security settings; validate the candidate
nginx config and retain the previous container for rollback before replacing the
preview. This repository does not establish the current host's exact container
creation command, so there is deliberately no blind replacement command here.
Do not recreate the Seattle production container to add a preview team.

Also compare the installed EventSpy collector with the reviewed source and
confirm the queue worker is healthy. If a reader/collector format upgrade is
still pending, complete that separate reviewed rollout in reader-first order;
Patriots does not require a new collector protocol.

## 5. Verify, then activate only the added entry

Use the existing source-free SSH `check` operations for `sfz_nfl_host`,
`sfz_news_host`, and `sfz_roster_host` with the staged Patriots object. Guides and
recaps also have `check` operations, but they require an initial valid Patriots
NFL snapshot; a missing-first-snapshot error is not a successful full check.
Guide preflight additionally needs a fresh NFL snapshot (72-hour policy) and an
enabled site object. An in-memory enabled object used for a `check` does not
change the Variable or schedule work. Check each connection with the actual
Airflow environment; a local fixture test does not prove SSH or credentials.

For the basic three connection checks, run this from the Airflow checkout. After
the first NFL snapshot succeeds, change `include_dependents = False` to `True`
and run it again to include recap and guide preflight. All operations remain
source-free; expected missing NFL inputs are not silently treated as success.

```bash
cd /home/laurawkr/homelab-airflow
docker compose exec -T \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/opt/airflow/dags \
  -e _AIRFLOW_PROCESS_CONTEXT=server \
  airflow-scheduler python -B - <<'PY'
import base64
import json
from pathlib import Path
from airflow.models.variable import Variable
from airflow.providers.ssh.hooks.ssh import SSHHook
from fan_zone_config import validate_sites
from fan_zone_photo_credits import load_catalog, validate_catalog

include_dependents = False
fallback = validate_sites(Path('/opt/airflow/config/active-sites.json').read_text())
raw = Variable.get('fan_zone_active_sites', default_var=None)
live = validate_sites(raw) if raw is not None else fallback
site = dict(live.get('patriots', fallback['patriots']), enabled=True)
site = validate_sites({'patriots': site})['patriots']
raw_credits = Variable.get('fan_zone_photo_credits', default_var=None)
credits = load_catalog() if raw_credits is None else validate_catalog(json.loads(raw_credits))
connections = ['sfz_nfl_host', 'sfz_news_host', 'sfz_roster_host']
if include_dependents:
    connections += ['sfz_recap_host', 'sfz_guides_host']
failed = []
for connection in connections:
    request = {'site': site}
    if connection == 'sfz_news_host':
        request['photoCredits'] = credits
    payload = json.dumps(request, separators=(',', ':'), ensure_ascii=False).encode()
    if len(payload) > 24576:
        raise SystemExit('Check request exceeds the supported size limit')
    token = base64.urlsafe_b64encode(payload).decode().rstrip('=')
    print('Checking:', connection, flush=True)
    hook = SSHHook(ssh_conn_id=connection, conn_timeout=15,
                   cmd_timeout=120, conn_retry_attempts=1)
    try:
        with hook.get_conn() as client:
            status, output, error = hook.exec_ssh_client_command(
                client, 'check ' + token, get_pty=False, environment=None, timeout=120)
        print(output.decode('utf-8', 'replace'))
        if status:
            print(error.decode('utf-8', 'replace'))
            failed.append(connection)
    except Exception as exc:
        print(connection, 'check failed:', type(exc).__name__)
        failed.append(connection)
if failed:
    raise SystemExit('Checks needing attention: ' + ', '.join(failed))
print('Selected Patriots source-free checks passed; no refresh was started.')
PY
```

No additional roster installer is required when the existing connection is
working: its restricted SSH command executes the committed roster runner in the
Airflow checkout, and that runner reads `deployment/roster/teams.json` directly.
The new source registry entry therefore arrives with the source update; the
helper prepares only its new runtime. A missing existing connection needs a
separate reviewed repair, not a silent replacement of shared credentials.

Before editing the Variable, export its current value again to a **new** file
using step 2's export command with a new output filename, then run:

```bash
python3 -B deployment/shared/prepare_patriots.py --verify-current \
  --current /home/laurawkr/patriots-current-sites-fresh.json \
  --plan /home/laurawkr/patriots-onboarding
```

If the configuration changed, stage a fresh plan instead of overwriting those
changes. Once host paths, source, coverage and preview routing are ready, replace
only the value of `fan_zone_active_sites` in Airflow's Variables page with the
reviewed `enabled-sites.json` content. Do not import the raw candidate as an
all-Variables import file. The other site entries must stay unchanged.

Wait for DAG parsing and check these new named tasks beside the existing teams:

| Existing DAG | New work task |
| --- | --- |
| `sfz_nfl_refresh` | `refresh_nfl_snapshot_patriots` |
| `sfz_roster_refresh` | `refresh_roster_patriots` |
| `sfz_daily_article` | `generate_article_patriots` |
| `sfz_game_recaps` | `generate_recaps_patriots` |
| `sfz_eventspy_collect` | `collect_ticket_prices_patriots` |
| `sfz_game_guides` | `refresh_guides_patriots` |

Each keeps its corresponding `save_run_receipt_patriots` task. Preserve the
current pause state of every DAG. Adding the Variable entry does not unpause a
paused pipeline or create a successful publication.

Seed NFL first through the existing `sfz_nfl_refresh` DAG. A normal manual DAG
trigger refreshes **all enabled teams**, not only Patriots. It uses the existing
pool and normal output contracts. Do not invent a `team` run-conf filter: this
DAG does not implement one. Alternatively wait for its next normal scheduled
run. Until the first Patriots NFL snapshot succeeds, newly enabled guide/recap
work can report missing-input errors; those need a successful NFL input before
retry. Do not use direct host refreshes to bypass the Airflow pool.

Then verify the Patriots NFL receipt and repeat the dependent recap/guide
source-free checks. Let daily articles, roster, game recaps and guides run under
their existing schedules or deliberately trigger their existing DAGs knowing
that all enabled teams are selected. The roster pipeline handles roster,
injuries and transactions; it does not recalculate player statistics.

EventSpy manual/backfill runs deliberately skip collection. Validate the first
eligible future scheduled slot; preserve the existing slot ledger/history. Do
not clear a stored failed slot as a way to force repeated provider collection.
Already-started/completed games are skipped before visiting EventSpy.

## 6. Prepare the Patriots schedule cache from its successful NFL snapshot

The ordinary EventSpy bridge builds this cache from a valid fresh NFL publication.
For the first website build, it can be prepared without any provider request:

```bash
cd /home/laurawkr/homelab-airflow
sudo python3 -B - <<'PY'
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path

root = Path('/home/laurawkr/homelab-airflow')
spec = importlib.util.spec_from_file_location('patriots_schedule', root / 'deployment/eventspy/schedules.py')
schedules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schedules)
site = json.loads(Path('/opt/fanzone-shared/settings.json').read_text())['sites']['patriots']
coverage = json.loads(Path('/opt/fanzone-eventspy/coverage/patriots.json').read_text())
payload = schedules.schedule_from_nfl_snapshot(site, coverage, datetime.now(timezone.utc))
if payload is None:
    raise SystemExit('A fresh, verified Patriots NFL snapshot is required; no API request or cache write was made')
schedules.validate_schedule(payload, site, coverage)
path = Path('/var/lib/fanzone-eventspy/schedules/patriots.json')
# Use the existing validated atomic cache writer, never fabricate NFL game IDs.
schedules._atomic_write(path, payload)
print('Prepared only the Patriots schedule cache from its verified NFL snapshot:', path)
PY
```

Coverage entries with no verified EventSpy page are intentionally unavailable.
They must not be turned into guessed links, another team's prices, or zero-dollar
offers. Consult the reviewed `coverage/patriots.json` and research notes for the
current coverage count and outstanding links. The Patriots schedule must still
contain all 17 real regular-season games, including games with unavailable prices.

## 7. Build and test the selected team

```bash
cd /home/laurawkr/templatefanzone
bash template-tools/build-team.sh patriots --dry-run
bash template-tools/build-team.sh patriots --stage-only
bash template-tools/build-team.sh patriots
```

The dry run validates required input directories and prints the build command.
Stage-only builds without replacing the served `dist`. The ordinary successful
build replaces the shared preview's `dist`, so it will now display Patriots.
If Game Day Guides and Where to Watch are enabled for this test, first obtain a
valid Patriots guide snapshot and set the existing build flag explicitly:

```bash
FAN_ZONE_GUIDES_ENABLED=1 bash template-tools/build-team.sh patriots --stage-only
FAN_ZONE_GUIDES_ENABLED=1 bash template-tools/build-team.sh patriots
```

Verify homepage/title/favicon, history and history banner, roster and player
pages, articles and image credits, final-game recaps, guide/watch pages when
enabled, and ticket requests. Tickets must use `/data/eventspy-mirror/patriots/`.
Check the generated output and sitemap contain selected-team content; correct
opponents and source citations are not cross-team leakage.

Populate the Patriots photo bucket with appropriate licensed images and add
matching entries to the existing photo-credit Variable/catalog. An empty bucket
uses the existing neutral illustration fallback; it does not borrow Seattle
photos. A passing DAG does not build or deploy the static website automatically.

For a separate public Patriots site, confirm the intended owned domain, canonical
URL, checkout/output location, container/port, tunnel/DNS mapping, indexing policy
and advertising configuration. The template's default rendered domain is not
proof that the domain is owned or routed. The existing shared preview remains
`noindex`; remove that policy only in a reviewed public deployment configuration.

## Evidence and scope

Baseline reviewed: template `620fdf2e6c85fa9e21d9b1c3dfd6463b5926c69a` and
Airflow `6cc3f23e945651415935fb5a8c30536483869e00`, plus the current Confluence
production deployment, Airflow operations and modular design pages. Patriots
changes build on those sources. Current host Variable, registry, mounts,
connections and successful data publications require the host checks above.
The documentation update in Confluence follows the user's approval and should
record actual observed installation/build results, not imply that code review
or source-free preflight already deployed the site.
