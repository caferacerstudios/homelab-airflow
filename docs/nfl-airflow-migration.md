# NFL data refresh through Airflow

Prepared September 10, 2026. This is a staged migration: the support code and
Airflow DAG are prepared and tested locally; installation and a real API run on
`wkr` still need verification. The separate build-cutover change must wait for
that live run and the snapshot mount in every build environment.

## What changes

`sfz_nfl_refresh` is a Python Airflow DAG with two tasks:

1. `refresh_nfl_snapshot`: a Python SSH hook invokes the fixed Python host runner.
2. `save_run_receipt`: saves the verified manifest as an Airflow artifact.

The host runner stages `fetch-nfl.mjs`, its existing normalizers, the canonical
roster, watch guides, previous schedule, and recap metadata. It runs the fetcher
in a temporary Node 22 container, validates fresh outputs, then atomically
publishes `/var/lib/sfz-nfl/current`. It never writes to a website checkout,
changes a website branch, or starts a website build.

JavaScript remains the existing collection/normalization engine. The DAG,
Airflow hook, installer, SSH entrypoint, and host orchestration are Python.
No new systemd bridge, timer, Docker-socket mount, or Airflow image change is
needed. The installed SSH provider is used with one dedicated restricted key.

After cutover, `prebuild` becomes:

```text
refresh-team-roster -> import-nfl-snapshot -> generate-game-recaps -> generate-player-profiles
```

The importer reads local files only. It verifies the complete snapshot before
writing the three generated NFL files, and preserves existing recap prose while
adding only missing game/season metadata. The recap and profile generators
remain in the build until their own migrations. Keep `BALLDONTLIE_API_KEY` in
the existing build environment: recap generation still uses it.

**Collection is not live HTML publication.** Astro embeds these JSON files at
build time. The next normal site build imports the newest snapshot and renders
it; an Airflow collection by itself does not update public static pages. This
DAG does not run content generators ten times a day.

## Schedule and API allowance

Ten Seattle times daily: **00:15, 03:15, 06:15, 09:15, 12:15, 14:15, 16:15,
18:15, 20:15, 22:15**. The timetable uses `America/Los_Angeles`, including DST.
There is no catch-up; one active run and the one-slot `balldontlie_api` pool
serialize Airflow execution. Task retries are zero; successful repeated run IDs
reuse a checksummed receipt without fetching again.

The official NFL API documentation publishes 5 requests/minute for Free and
paid trials, 60/minute for ALL-STAR, and 600/minute for GOAT. Season statistics
require ALL-STAR/GOAT endpoint access. No separate daily/monthly request quota
is stated there; the account's actual subscription remains unverified.

This collector spaces every request by 15 seconds, including pagination and
fallback requests. It honors bounded HTTP 429 retries and records actual
request counts. Other jobs sharing the key also consume its rate allowance.
Ten runs is not ten requests; page counts determine usage. A 401 can mean an
invalid key or missing endpoint entitlement. EventSpy's seven-attempt daily
limit does not apply to BALLDONTLIE.

Official reference: https://nfl.balldontlie.io/#account-tiers

## 0. Copy the package and publish the prepared commits

The GitHub connection could read the website repository but returned HTTP 403
when creating a branch. These changes are committed locally and supplied in
`website-nfl.bundle`; they have not been pushed or merged remotely. The private
Airflow repository was also unavailable to this connection.

Download `airflow-nfl-refresh.zip`. On your **laptop**:

```bash
scp ~/Downloads/airflow-nfl-refresh.zip laurawkr@192.168.88.3:airflow-nfl-refresh.zip
```

On **wkr**, extract the package, import both prepared branches, and push them
using your existing GitHub SSH access. These commands keep the production
checkout on `main`; they do not deploy either change.

```bash
(
set -e
cd ~
python3 -m zipfile -e airflow-nfl-refresh.zip .
cd ~/seahawksfanzone
test "$(git branch --show-current)" = main
git fetch origin main
git bundle verify ~/airflow-nfl-refresh/website-nfl.bundle
git fetch ~/airflow-nfl-refresh/website-nfl.bundle refs/heads/codex/nfl-airflow-support:refs/heads/codex/nfl-airflow-support refs/heads/codex/nfl-airflow-build-cutover:refs/heads/codex/nfl-airflow-build-cutover
git push origin codex/nfl-airflow-support codex/nfl-airflow-build-cutover
)
```

If an existing branch prevents the fetch/push, stop and inspect it; do not force
an update. Create and review the support pull request first:

https://github.com/caferacerstudios/seahawks-fan-zone/compare/main...codex/nfl-airflow-support?expand=1

The cutover branch depends on that support change. Leave it unmerged until
steps 3 and 4 pass. Create its pull request at step 5:

https://github.com/caferacerstudios/seahawks-fan-zone/compare/main...codex/nfl-airflow-build-cutover?expand=1

## 1. Install the website support change first

Merge the `codex/nfl-airflow-support` pull request before installing the DAG.
That change leaves `fetch-nfl.mjs` in `prebuild` while adding strict refresh
mode, pacing, a completion report, and the snapshot importer.

On **wkr**, update production source without changing its branch:

```bash
(
set -e
cd ~/seahawksfanzone
test "$(git branch --show-current)" = main
git pull --ff-only
node --check scripts/fetch-nfl.mjs
node --check scripts/import-nfl-snapshot.mjs
)
```

If Git reports conflicting local changes, keep them and resolve that specific
conflict; do not reset the checkout. `~/seahawksfanzone` stays on `main` and
`~/seahawksfanzone-dev` stays on `dev` throughout this rollout.

## 2. Install the Airflow additions

The Airflow files belong in `caferacerstudios/homelab-airflow`. Step 0 has
already extracted the package. On **wkr**:

```bash
(
set -e
cd ~/airflow-nfl-refresh
cp -R dags deployment tests docs "$HOME/homelab-airflow/"
cd ~/homelab-airflow
docker image inspect node:22-bookworm >/dev/null 2>&1 || docker pull node:22-bookworm
python3 deployment/nfl/install.py
)
```

The installer requests sudo only to create `/var/lib/sfz-nfl` if it is absent.
It reads the existing production `.env` for the single literal
`BALLDONTLIE_API_KEY` value, creates a dedicated SSH key under `secrets/`, and
adds a restricted `authorized_keys` entry. Existing Git/SSH keys and SSH config
are preserved. The host's SSH public key is pinned in Airflow connection
`sfz_nfl_host`; the private connection key is sent through stdin, not command
arguments or logs. The provider API key stays on the host.

The source-free check validates the source, credentials' presence, Docker
access, DAG import, and SSH connection. It does not test provider entitlement or
start a collection. No existing ticket timers or Airflow services are restarted.

## 3. Verify one real run before removing the build fetch

With the new DAG still paused, run its two tasks in Airflow's test mode. This
performs one complete BALLDONTLIE collection, including all required pages,
and writes real data:

```bash
(
set -e
cd ~/homelab-airflow
docker compose exec -T airflow-scheduler airflow dags test sfz_nfl_refresh --dagfile-path /opt/airflow/dags/sfz_nfl_refresh.py
python3 -m json.tool /var/lib/sfz-nfl/current/manifest.json
cd ~/seahawksfanzone
node scripts/import-nfl-snapshot.mjs --check-only
)
```

Require both tasks to succeed, a recent `updatedAt`, a positive `requestCount`,
and a successful importer check. Any API failure in strict mode fails the task;
the previous published snapshot remains selected. Source logs are retained in
the run's `work-*/collector.log`; a failure also prints sanitized log lines in
the Airflow task log.

The real host/API test was not run while preparing this change. Record its run
ID, timestamp, request count, and successful importer output before cutover.

## 4. Make the snapshot visible in build containers

On-host builds can read `/var/lib/sfz-nfl/current` directly. Every isolated
production/dev/agent build container must receive the **parent directory**
read-only, so both `current` and its `runs/` symlink targets are available.

Add this argument to the existing build wrapper's `docker run` command;
it is an argument, not a standalone shell command:

```text
--mount type=bind,src=/var/lib/sfz-nfl,dst=/var/lib/sfz-nfl,readonly
```

Equivalent Compose entry on an existing build service:

```yaml
volumes:
  - /var/lib/sfz-nfl:/var/lib/sfz-nfl:ro
```

`NFL_SNAPSHOT_DIR` defaults to `/var/lib/sfz-nfl/current`, so no environment
change is needed when using that mount. The host-owned `sfz-dev` build wrapper
is outside these repositories; its actual Docker command must be updated and
checked before merging the cutover. Existing profile-artifact mounts remain.
Setting an environment variable alone does not make the snapshot accessible.

Check the mount from a separate Node container without changing source files:

```bash
docker run --rm --user "$(id -u):$(id -g)" --mount "type=bind,src=$HOME/seahawksfanzone,dst=/work,readonly" --mount type=bind,src=/var/lib/sfz-nfl,dst=/var/lib/sfz-nfl,readonly --workdir /work node:22-bookworm node scripts/import-nfl-snapshot.mjs --check-only
```

Also run the same importer check through the actual build wrapper. The sample
container proves the mount layout, not that an unseen wrapper has been changed.

## 5. Enable the DAG and apply the build cutover

Once the live test and actual build mounts pass, enable the ten scheduled runs:

```bash
cd ~/homelab-airflow
docker compose exec -T airflow-scheduler airflow dags unpause sfz_nfl_refresh
docker compose exec -T airflow-scheduler airflow dags list
```

Then merge `codex/nfl-airflow-build-cutover` after its support prerequisite. The
cutover replaces only the NFL fetch step with the local snapshot importer and
updates the README and standings freshness copy. It retains the manual
`npm run fetch-nfl` command for deliberate maintenance.

Update the existing main/dev deployments through their normal workflow, and
perform one normal site build. Confirm that its log contains
`Imported NFL snapshot ...` and does not run `node scripts/fetch-nfl.mjs` as part
of `prebuild`. Confirm the resulting schedule/standings page uses that snapshot's
timestamp. Other remaining build scripts may still make API calls.

## 6. Check in the Airflow source and record acceptance

The website changes are separate support and cutover commits, pushed in step 0. When the Airflow
additions were prepared, the private Airflow repository was not available to
the GitHub connection; use this host-side commit if it has not already been
checked in through that repository.

```bash
(
set -e
cd ~/homelab-airflow
git status --short --branch
git add dags/sfz_nfl_refresh.py dags/sfz_nfl_hook.py
git add deployment/nfl/install.py deployment/nfl/refresh_nfl.py deployment/nfl/ssh_entrypoint.py
git add tests/test_nfl_dag.py tests/test_nfl_hook.py tests/test_nfl_ssh_entrypoint.py tests/test_refresh_nfl.py
git add tests/test_nfl_contract.py
git add docs/nfl-airflow-migration.md
git diff --cached --check
git diff --cached --stat
)
```

Review the staged diff locally, then commit and push:

```bash
cd ~/homelab-airflow
git diff --cached
git commit -m "Add scheduled NFL snapshot refresh through Airflow"
git push
```

Do not stage `secrets/`, real `.env` values, runtime snapshots, or logs. Record
actual successful host deployment and cutover in `docs/verified-baseline.md`;
the historical EventSpy test is a separate pipeline.

## Routine checks, retention, and recovery

```bash
cd ~/homelab-airflow
docker compose exec -T airflow-scheduler airflow dags list-runs sfz_nfl_refresh
python3 -m json.tool /var/lib/sfz-nfl/current/manifest.json
du -sh /var/lib/sfz-nfl
```

- Host snapshots: `/var/lib/sfz-nfl/runs/<sha256-run-id>/snapshot/`.
- Current snapshot: `/var/lib/sfz-nfl/current` (atomic symlink).
- Host work/logs: the run's `work-*` directory. Retained for diagnosis; no
  automatic deletion or retention job is installed in this change.
- Airflow receipts: `/var/lib/homelab-pipelines/seahawks/nfl/<sha256-run-id>/receipt.json`.
- A successful repeated run ID reuses its receipt. A failed refresh may be
  retried deliberately after diagnosing the problem; it will make API calls.
- Valid old snapshots warn after 24 hours and retain their original timestamp.
  Missing/corrupt snapshots fail the importer rather than silently loading an
  unrelated repository snapshot. `npm run build:offline` remains available for
  intentional offline development; it does not perform the import.
- To stop future refreshes, pause `sfz_nfl_refresh`. Existing valid data remains
  available to builds. Pausing does not terminate a running host collection.
- For a complete rollback, pause the DAG and revert only the build-cutover
  commit through the normal Git workflow. That restores build-owned NFL fetch;
  avoid running both schedules indefinitely.
- Back up `/var/lib/sfz-nfl`, the Airflow metadata DB and Fernet key, and the
  dedicated SSH key securely. Source control alone is not a runtime backup.

## Verification during development

Focused tests cover request pacing/retries, explicit failure instead of stale
success, season filters, manifest corruption/mixed data, recap preservation,
publication failure/recovery, cached runs, SSH command handling, and ten daily
slots across Seattle DST transitions. Airflow import is checked against 3.3.1
and SSH provider 6.0.1. These offline checks do not establish account entitlement,
host connectivity, live installation, or production deployment.
