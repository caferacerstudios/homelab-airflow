# Homelab Airflow baseline

Host: wkr
Project: /home/laurawkr/homelab-airflow
Airflow: 3.3.1, Python 3.12
Executor: LocalExecutor
Metadata database: PostgreSQL 16
UI: host loopback port 8085

## Verified live collection

DAG: sfz_eventspy_collect
Run: scheduled__2026-09-10T04:00:00+00:00
Seattle time: September 9, 2026 at 21:00 PDT

- collect_ticket_prices: success
- save_run_receipt: success
- Prior collector attempts: 6
- Airflow bridge attempts: 1
- Total daily attempts: 7
- Unresolved attempts: 0

This is a temporary, date-limited trial.
Automatic restoration of the original timer is scheduled for
September 10, 2026 at 00:00 PDT (07:00 UTC).
Actual midnight restoration is pending verification.

## Source layout

Dockerfile and Compose files define the Airflow platform.
dags/ contains the deployed Python DAGs and bridge client.
deployment/ticket-day-test/ preserves the installer, host bridge,
activation script, DAG copies, Compose override, and tests.

The deployed files are authoritative. If they change, update the
corresponding deployment package copies before committing again.

## External dependencies

The existing collector remains a host dependency:
- sfz-eventspy-season.service
- sfz-eventspy-season.timer
- /usr/local/sbin/sfz-eventspy-season-collect
- Docker image seahawksfanzone-eventspy-season:1

Bridge scripts are installed under /opt/sfz-airflow-ticket-test.
Bridge runtime state is under /var/lib/sfz-airflow-ticket-test.
Airflow logs are under /var/lib/homelab-airflow/logs.
Pipeline artifacts are under /var/lib/homelab-pipelines.
Collector output is under /var/lib/sfz-eventspy-mirror/dev/public.

Secrets, databases, runtime state, outputs, and the collector image
are not backed up by this repository.

On a fresh checkout, create the local config/ and plugins/ directories
and supply real environment secrets before deployment.
