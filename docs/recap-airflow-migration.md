# Recap generator through Airflow

Prepared September 10, 2026 for Laura's existing `wkr` installation.
This package has not been installed on the server yet. Follow the blocks in
order. The website support change is merged first; the build switches to
snapshot import only after the first Airflow run succeeds.

## What this adds

- Python DAG: `sfz_game_recaps`.
- Python hook: `dags/sfz_recap_hook.py`.
- Python host scripts: `deployment/recaps/{install,refresh_recaps,ssh_entrypoint}.py`.
- Airflow SSH connection: `sfz_recap_host`, with a separate restricted key.
- Shared one-slot pool: `balldontlie_api`, also used by the NFL DAG.
- Persistent output: `/var/lib/sfz-recaps/current`.
- Website importer: `scripts/import-recap-snapshot.mjs`.

The host runner reads `BALLDONTLIE_API_KEY` and `OPENAI_API_KEY` from
`/home/laurawkr/seahawksfanzone/.env`. The keys are passed to the temporary
Node container without putting their values in shell commands or Airflow logs.
There is no need to enter the keys again or create an OpenAI Airflow connection.

The existing JavaScript recap writer and `gpt-4o-mini` model are retained. It
writes the same short paragraph and highlights that existing game pages read.
Only the orchestration and storage move. Player biographies and news articles
are outside this change.

## Schedule and behavior

Ten Seattle times daily: **00:45, 03:45, 06:45, 09:45, 12:45, 14:45, 16:45,
18:45, 20:45, 22:45**, using `America/Los_Angeles`.

These are 30 minutes after the NFL refresh slots. The recap DAG reads the latest
successfully published NFL snapshot. The offset does not itself guarantee that
an NFL refresh succeeded. The manifest records which NFL snapshot was used.
The shared pool serializes these two DAGs' source tasks.

The writer handles finished regular-season and postseason Seahawks games.
Existing complete recaps are reused. If there is nothing to generate, the run
succeeds with zero OpenAI and BALLDONTLIE requests. Missing recaps require
per-game statistics and optional play-by-play; those requests use the same
15-second pacing as the NFL collector.

One active run, no catch-up, and no Airflow task retries. A completed run ID
reuses its existing snapshot. A failed run leaves the previous published
snapshot in place. Recap state is independent of the NFL snapshot directory.

The DAG publishes data, not HTML. The next successful website build imports
the latest recaps. Game-page layout and live-score/news features are separate
work; this package retains the existing recap data format.

## 1. Upload and extract

On your **laptop**, after downloading `airflow-recap-refresh.zip`:

```bash
scp ~/Downloads/airflow-recap-refresh.zip laurawkr@192.168.88.3:airflow-recap-refresh.zip
```

On **wkr**:

```bash
(
set -e
cd ~
python3 -m zipfile -e airflow-recap-refresh.zip .
cd ~/airflow-recap-refresh
sha256sum --check SHA256SUMS
)
```

## 2. Push the two website branches

The GitHub connection could read the website but could not create a branch
(HTTP 403). The private Airflow repository was also unavailable through that
connection. This package therefore includes the committed website changes in
`website-recaps.bundle` for your existing GitHub SSH access.

This block creates local rollout branches and pushes them. It does not switch
your production checkout away from `main` or touch the dev checkout.

```bash
(
set -e
cd ~/seahawksfanzone
test "$(git branch --show-current)" = main
git fetch origin main
git fetch "$HOME/airflow-recap-refresh/website-recaps.bundle" \
  refs/heads/codex/recap-airflow-support:refs/heads/codex/recap-airflow-support \
  refs/heads/codex/recap-airflow-build-cutover:refs/heads/codex/recap-airflow-build-cutover
git push origin codex/recap-airflow-support codex/recap-airflow-build-cutover
)
```

Open the support comparison, create its PR, and merge it into `main`:

[Create the recap support PR](https://github.com/caferacerstudios/seahawks-fan-zone/compare/main...codex/recap-airflow-support)

The support change adds the importer and Airflow reporting support. The recap
writer remains in `prebuild` until step 5. Leave the build-cutover branch
unmerged for now.

Then update production:

```bash
(
set -e
cd ~/seahawksfanzone
test "$(git branch --show-current)" = main
git pull --ff-only
node --check scripts/generate-game-recaps.mjs
node --check scripts/import-recap-snapshot.mjs
)
```

## 3. Install the DAG and host connection

Run as `laurawkr`, not with `sudo`. The installer requests sudo only when it
needs to create `/var/lib/sfz-recaps`.

```bash
(
set -e
cd ~/airflow-recap-refresh
cp -R dags deployment tests docs "$HOME/homelab-airflow/"
cd ~/homelab-airflow
docker image inspect node:22-bookworm >/dev/null 2>&1 || docker pull node:22-bookworm
python3 deployment/recaps/install.py
)
```

Expected ending: DAG import and source-free SSH check passed. Installation
makes no BALLDONTLIE or OpenAI requests and leaves the new DAG paused.

This installer already includes the Airflow 3.3.1 standalone connection-check
fix used during the NFL rollout. No Dockerfile or Compose change is needed.

## 4. Run one live recap refresh

This command executes the DAG once while its schedule remains paused. If there
are finished games without recaps, it calls BALLDONTLIE and OpenAI using your
existing keys. Allow the command to finish.

```bash
(
set -e
cd ~/homelab-airflow
docker compose exec -T airflow-scheduler airflow dags test sfz_game_recaps
python3 -m json.tool /var/lib/sfz-recaps/current/manifest.json
cd ~/seahawksfanzone
node scripts/import-recap-snapshot.mjs --check-only
)
```

The manifest reports `generatedCount`, `generatedGameIds`, `requestCount`, and
`openaiRequestCount`. Zero generated recaps is valid when all eligible games
already have recaps or the current NFL snapshot has no newly finished games.
The NFL source timestamp in the manifest explains which game status was used.

A successful run and importer check are the rollout check. No wider site test
suite is required here.

## 5. Switch the build to import recaps

After step 4 succeeds, create and merge this PR into `main`:

[Create the recap build-cutover PR](https://github.com/caferacerstudios/seahawks-fan-zone/compare/main...codex/recap-airflow-build-cutover)

Then run:

```bash
(
set -e
cd ~/seahawksfanzone
test "$(git branch --show-current)" = main
git pull --ff-only
node -e 'if (!JSON.parse(require("node:fs").readFileSync("package.json", "utf8")).scripts.prebuild.includes("import-recap-snapshot.mjs")) throw new Error("Merge the recap build-cutover PR first.")'
node scripts/import-recap-snapshot.mjs
)
```

`prebuild` now runs:

```text
refresh-team-roster -> import-nfl-snapshot -> import-recap-snapshot -> generate-player-profiles
```

`npm run generate-game-recaps` remains available as a manual command, but it
is no longer run by normal builds.

Production builds are run directly on the host from `~/seahawksfanzone` using
`npm run build`. No additional snapshot bind mount is needed for that host
build or its nginx container. The last known production standings build issue
was deferred; this rollout does not require repairing it to enable collection.
Public pages will use new recaps after a successful normal build.

The dev deployment is unchanged. If its isolated Node build later adopts this
cutover, add read-only access to `/var/lib/sfz-recaps:/var/lib/sfz-recaps:ro`
as well as its NFL snapshot mount. Do not change its branch during this rollout.

## 6. Enable the schedule

Run this separately from any website build so an unrelated build failure does
not prevent enabling the collector:

```bash
(
set -e
cd ~/homelab-airflow
docker compose exec -T airflow-scheduler airflow dags unpause sfz_game_recaps
)
```

## 7. Record and push the Airflow changes

This block records the real manifest and enables a Git checkpoint without
claiming that a scheduled run or a production build has already been observed.

```bash
(
set -e
cd ~/homelab-airflow
python3 - <<'PY'
import json
from pathlib import Path
m = json.loads(Path('/var/lib/sfz-recaps/current/manifest.json').read_text())
lines = [
    '# Recap Airflow live checkpoint',
    '',
    '- DAG: sfz_game_recaps; schedule enabled during this rollout.',
    '- Schedule: ten times daily in America/Los_Angeles, 30 minutes after NFL slots.',
    f"- First recorded run: {m['runId']}",
    f"- Snapshot timestamp: {m['updatedAt']}",
    f"- Season: {m['season']}",
    f"- New recaps: {m['generatedCount']}; game IDs: {m['generatedGameIds']}",
    f"- BALLDONTLIE requests: {m['requestCount']}",
    f"- OpenAI requests: {m['openaiRequestCount']}; model: {m['model']}",
    f"- Website source commit used: {m['sourceCommit']}",
    f"- NFL input run: {m['nflSourceRunId']}",
    f"- NFL input timestamp: {m['nflSourceUpdatedAt']}",
    '- Snapshot validation/import passed during rollout.',
    '- Normal prebuild imports recaps; the standalone writer remains available.',
    '- First scheduled run has not yet been recorded here.',
    '- Production HTML build/publication has not been verified in this checkpoint.',
    '- Dev deployment and ticket collector were unchanged.',
    '',
]
Path('docs/recap-live-status.md').write_text('\n'.join(lines))
PY
git add \
  dags/sfz_game_recaps.py \
  dags/sfz_recap_hook.py \
  deployment/recaps \
  tests/test_recap_dag.py \
  tests/test_recap_hook.py \
  tests/test_refresh_recaps.py \
  docs/recap-airflow-migration.md \
  docs/recap-live-status.md
git commit -m "Add Airflow game recap generation and snapshot import rollout"
git push
)
```

## Routine use

View runs:

```bash
cd ~/homelab-airflow
docker compose exec -T airflow-scheduler airflow dags list-runs sfz_game_recaps
```

Pause future runs when needed:

```bash
cd ~/homelab-airflow
docker compose exec -T airflow-scheduler airflow dags pause sfz_game_recaps
```

Pausing does not terminate a collection already running. Resume with the same
command using `unpause`.

| Item | Location |
| --- | --- |
| Current recap snapshot | `/var/lib/sfz-recaps/current` |
| Retained snapshots | `/var/lib/sfz-recaps/runs/<sha256-run-id>/snapshot/` |
| Generator log | `/var/lib/sfz-recaps/runs/<sha256-run-id>/work-*/collector.log` |
| Airflow receipt on host | `/var/lib/homelab-pipelines/seahawks/recaps/<sha256-run-id>/receipt.json` |
| Build input after import | `~/seahawksfanzone/src/data/nfl/gameRecaps.json` |
| Dedicated SSH key | `~/homelab-airflow/secrets/sfz_recap_ed25519` |

Snapshots and logs have no automatic purge. Back up the runtime data along
with the existing Airflow metadata/Fernet key and SSH credentials. The Git
repository contains source and docs, not generated runtime snapshots or keys.

To return recap generation to builds later, pause this DAG and revert only
the recap build-cutover change through your normal Git workflow. The support
files can remain installed.
