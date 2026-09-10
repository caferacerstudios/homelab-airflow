# Seahawks ticket collector: Airflow day test

Prepared for Laura's `wkr` host, 2026-09-09. This is an executable first migration
step for the already installed Airflow 3.3.1 / Python 3.12 / LocalExecutor stack.

The Python DAG schedules the **existing, working season collector**. Its Node
implementation and Docker image remain the execution engine for this test.
The DAG, bridge client, host control logic, installer, and activation are Python.
The collector continues publishing to the mirror already read by the production
website. This test does not require an Astro build.

## What happens today

1. Install the bridge and DAGs; the original timer keeps running.
2. Run a source-free smoke DAG to verify Airflow can exchange requests with the host.
3. Activate the trial. It pauses `sfz-eventspy-season.timer`, allows any current
   collection to finish, and asks for today's earlier attempt count.
4. Airflow uses the remaining original slots: 03:00, 06:00, 09:00, 12:00, 15:00,
   18:00, and 21:00 in `America/Los_Angeles`. It does not collect immediately.
5. At the following **Seattle midnight**, a persistent host timer disarms the
   bridge and restores the original collector timer. If the host is down at
   midnight, restoration runs when the host returns. Date gating independently
   prevents this trial from collecting the next day.

The trial DAG remains installed and visible afterward. Future scheduled runs
skip because the bridge is disarmed. Pause the DAG after reviewing the test.
Continuing Airflow permanently is a separate cutover, not implied by this
one-day activation.

## 1. Transfer the package

Download `airflow-ticket-day-test.zip` to your laptop's Downloads directory.
Run this in a **laptop terminal**, not inside your SSH session:

```bash
scp ~/Downloads/airflow-ticket-day-test.zip laurawkr@192.168.88.3:airflow-ticket-day-test.zip
```

Connect to wkr if needed:

```bash
ssh laurawkr@192.168.88.3
```

## 2. Install and run the smoke test

On **wkr**, copy this entire block:

```bash
(
set -e
cd ~
python3 -m zipfile -e airflow-ticket-day-test.zip .
cd ~/airflow-ticket-day-test
python3 install.py
)
```

The installer requests sudo for host files, adds two narrowly scoped scheduler
mounts through `compose.override.yaml`, installs the DAGs, recreates the scheduler
to apply those mounts, and restarts the DAG processor. Existing production and
development website containers are not restarted. Run this while the new Airflow
platform has no other running work.

It then runs **`sfz_ticket_bridge_smoke`**, which sends only a health request.
It does not start the source collector or consume a collection attempt.

Proceed only after `INSTALL COMPLETE`. Both `sfz_eventspy_collect` and
`sfz_ticket_bridge_smoke` should appear in Airflow. The collection DAG starts
paused. If installation stops on an existing differing override/file, keep the
error and merge that file before retrying; do not overwrite it blindly. The
installer never disables the existing collector timer.
Reinstalling while a trial is armed or has an unresolved attempt is refused.

## 3. Pause the original timer and activate today's Airflow trial

On **wkr**:

```bash
sudo python3 /opt/sfz-airflow-ticket-test/activate.py
```

The program shows the next eligible Seattle slot and the automatic restoration
time. It installs the midnight restoration timer before disabling the old timer.
An in-progress old collection is allowed to finish naturally.

When prompted, enter the **total number of full-season collector attempts
already made today in Seattle time**. Count successful, failed, and manual
attempts. The program shows journal-visible invocations as evidence, but direct
Docker runs or rotated logs may not be represented. Enter `unknown` if you cannot
establish the count; the program cancels and requests restoration of the old timer.

Example only: if the 03:00, 06:00, 09:00, and 12:00 collections ran and there were
no other attempts, enter `4`. That leaves at most three attempts for Airflow.
Do not automatically enter `4`; use your actual count.

Seven attempts leaves no remaining daily allowance. After the final slot, or
too close to a slot for safe activation, the program refuses the trial rather
than inventing an additional collection. It calculates the date and remaining
slots when you run it; it does not assume the date in this document.

Successful activation prints that the trial is armed, the remaining allowance,
and the next collection time. There is no manual live test or catch-up collection.
Use the scheduled run for the live test. Manually triggering the collection DAG
skips; use the smoke DAG for immediate diagnostics before activation.

## 4. Confirm scheduler ownership and restoration

On **wkr**:

```bash
(
set -e
cd ~/homelab-airflow
sudo python3 /opt/sfz-airflow-ticket-test/bridge.py status
systemctl show sfz-eventspy-season.timer --property=ActiveState,UnitFileState
systemctl list-timers --all sfz-airflow-ticket-test-restore.timer
docker compose exec -T airflow-scheduler airflow dags list
)
```

During the trial, expect the original timer to be **inactive and disabled**,
the bridge to be armed for today's Seattle date, the restore timer to have a
future trigger, and `sfz_eventspy_collect` to be unpaused. The collector's
**service** stays installed and is started by the bridge when the DAG requests
an eligible scheduled slot. That is expected: the timer is what was replaced.

The host may display timer times in UTC. Seattle's timezone is encoded in both
the DAG timetable and the restoration timer.

## 5. Check the first scheduled collection

After the next scheduled slot, on **wkr**:

```bash
(
set -e
cd ~/homelab-airflow
docker compose exec -T airflow-scheduler airflow dags list-runs sfz_eventspy_collect
sudo python3 /opt/sfz-airflow-ticket-test/bridge.py status
sudo journalctl -u sfz-airflow-ticket-test.service -u sfz-eventspy-season.service --since "-2 hours" --no-pager -n 100
)
```

For a completed successful source run, the journal must include:

```json
{"outcome":"EVENTSPY_SEASON_SUCCESS","authorized":16,"succeeded":16,"failed":0,"unavailable":1}
```

The bridge verifies the new service invocation, its successful exit, and that
summary. The DAG then saves its receipt below
`/var/lib/homelab-pipelines/seahawks/ticket-day-test/` on the host. The durable
host response is in `/var/lib/sfz-airflow-ticket-test/responses/`.

Read one published game through the production website locally:

```bash
curl --fail --silent --show-error http://127.0.0.1:4322/data/eventspy-mirror/1392216.json | python3 -m json.tool
```

Check `collectedAt` advanced after the scheduled run and inspect the summary and
history. This is a read of the existing website mirror, not an EventSpy collection.
The run receipt is execution evidence; it does not by itself verify every public
snapshot, so inspect the published mirror as part of the live acceptance test.

Use your existing Airflow browser tunnel. If needed, open another **laptop terminal**:

```bash
ssh -N -L 8085:127.0.0.1:8085 laurawkr@192.168.88.3
```

Open <http://localhost:8085>, select `sfz_eventspy_collect`, and inspect the
scheduled run's `collect_ticket_prices` and `save_run_receipt` task logs.

## 6. End the test early if needed

This asks systemd to disarm the bridge and restore the original timer. It waits
for any collector already running; it does not kill that collection. If the
collector is still busy, the restore service retries every minute.

```bash
(
set -e
cd ~/homelab-airflow
sudo systemctl start --no-block sfz-airflow-ticket-test-restore.service
docker compose exec -T airflow-scheduler airflow dags pause sfz_eventspy_collect
)
```

Then inspect the result:

```bash
sudo python3 /opt/sfz-airflow-ticket-test/bridge.py status
systemctl show sfz-eventspy-season.timer --property=ActiveState,UnitFileState
sudo journalctl -u sfz-airflow-ticket-test-restore.service --since "-15 minutes" --no-pager
```

Expect the bridge disarmed and the old timer enabled/active once any current
collection finishes. Its normal schedule resumes; `Persistent=false` prevents
replaying the old timer's missed slots. Keep the trial ledger and receipts for
review. If a task times out, inspect the host service before any intervention:
an Airflow timeout does not prove the source process stopped.

## 7. Check the next morning

```bash
(
set -e
cd ~/homelab-airflow
sudo python3 /opt/sfz-airflow-ticket-test/bridge.py status
systemctl show sfz-eventspy-season.timer --property=ActiveState,UnitFileState
sudo journalctl -u sfz-airflow-ticket-test-restore.service --since "-12 hours" --no-pager
docker compose exec -T airflow-scheduler airflow dags list-runs sfz_eventspy_collect
docker compose exec -T airflow-scheduler airflow dags pause sfz_eventspy_collect
)
```

Review source success, published freshness, task duration, receipt persistence,
and automatic restoration. A permanent migration should replace the temporary
date gate with ongoing daily quota accounting, update the deployment script that
currently expects the old timer active, and add alerting/operational ownership.
The full rollout runbook covers the later site pipelines and content-writing DAGs.

## Integration details

| Item | Location / value |
|---|---|
| Airflow project | `/home/laurawkr/homelab-airflow` |
| Collection DAG | `dags/sfz_eventspy_collect.py` |
| Reusable Python client | `dags/sfz_ticket_bridge.py` |
| Root host code | `/opt/sfz-airflow-ticket-test/` |
| Airflow-writable request queue | `/var/lib/sfz-airflow-ticket-test/requests/` |
| Root-owned response directory | `/var/lib/sfz-airflow-ticket-test/responses/` |
| Private durable SQLite state | `/var/lib/sfz-airflow-ticket-test/state/` |
| Request watcher | `sfz-airflow-ticket-test.path` |
| Fixed host worker | `sfz-airflow-ticket-test.service` |
| Date-specific restoration | `sfz-airflow-ticket-test-restore.timer` |
| Preserved source service | `sfz-eventspy-season.service` |
| Temporarily paused source timer | `sfz-eventspy-season.timer` |
| Airflow concurrency pool | `eventspy_source`, one slot |

Airflow receives only a request-directory write mount and a response-directory
read mount. The host bridge accepts two operations: health and the fixed season
collection. It does not accept arbitrary commands, image names, output paths,
or source URLs. systemd.path activates the bridge in response to a request; it
does not independently schedule source collections.

Each accepted source slot reserves one full-season attempt durably before
starting the original service. The runbook's pinned runner and image always
collect the same 16 authorized games, so limiting season attempts to seven also
limits each authorized game to seven. This depends on entering all prior attempts
and avoiding independent manual collection while the test owns scheduling.
Failures are not automatically refunded. Duplicate slots reuse their receipts;
unresolved starts block further attempts. The unavailable game remains unavailable.

Airflow uses `catchup=False`, `retries=0`, one active run, and a
`CronTriggerTimetable`. The host independently checks the armed date, original
slots, activation watermark, ten-minute maximum scheduling delay, old timer
inactivity, source service state, fingerprint, and remaining daily allowance.
The exact runner/image pins come from the supplied collector runbook. A mismatch
stops activation/collection for inspection.

During the trial, do not run the existing production deployment helper unchanged:
its old-timer-active requirement is intentionally false during this cutover.

## Verification scope and references

This package is prepared and tested locally with mocked host operations. No
EventSpy request or remote homelab modification was performed while preparing it.
`install.py` performs the real host import, permissions, and source-free bridge
checks. The first scheduled run provides the live collector acceptance test.

- User attachment: `EVENTSPY_SEASON_COLLECTOR_RUNBOOK(1).md` — working runner,
  image pins, game mapping, seven daily Seattle slots, output path and summary.
- [Airflow 3.3.1 trigger timetable API](https://airflow.apache.org/docs/apache-airflow/3.3.1/_api/airflow/timetables/trigger/index.html).
- [Airflow 3.3.1 CLI reference](https://airflow.apache.org/docs/apache-airflow/3.3.1/cli-and-env-variables-ref.html).
- [systemd.path source documentation](https://github.com/systemd/systemd/blob/main/man/systemd.path.xml).
