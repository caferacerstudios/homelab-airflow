"""Airflow 3.3 DAGs: controlled day trial and a source-free bridge smoke test."""
from datetime import datetime, timedelta, timezone

from airflow.exceptions import AirflowSkipException
from airflow.sdk import dag, get_current_context, task
from airflow.timetables.trigger import CronTriggerTimetable

from sfz_ticket_bridge import TicketCollectorHook


@dag(
    dag_id="sfz_eventspy_collect",
    schedule=CronTriggerTimetable(
        "0 3,6,9,12,15,18,21 * * *",
        timezone="America/Los_Angeles",
        run_immediately=False,
    ),
    start_date=datetime(2026, 9, 9, tzinfo=timezone.utc),
    catchup=False,
    is_paused_upon_creation=True,
    max_active_runs=1,
    max_active_tasks=1,
    default_args={"owner": "laura", "retries": 0},
    tags=["seahawks", "tickets", "day-test"],
    doc_md="""Runs the existing season collector through a restricted Python host bridge.
    Only remaining original slots on the armed Seattle date can collect.
    Manual and backfill runs skip. A source attempt is never automatically retried.
    Host receipts and the durable quota ledger survive task or scheduler restarts.
    At midnight the host disarms this test and restores the original timer.
    """,
)
def ticket_collection():
    @task(pool="eventspy_source", execution_timeout=timedelta(minutes=67))
    def collect_ticket_prices():
        context = get_current_context()
        run_type = context["dag_run"].run_type
        if getattr(run_type, "value", run_type) != "scheduled":
            raise AirflowSkipException("Only regular scheduled runs may contact EventSpy.")
        slot = context["data_interval_end"].astimezone(timezone.utc).isoformat()
        receipt = TicketCollectorHook().request("collect", slot=slot)
        print(receipt)
        if receipt["status"] == "skipped":
            raise AirflowSkipException(receipt["message"])
        if receipt["status"] != "success":
            raise RuntimeError(receipt["message"])
        return receipt

    @task(execution_timeout=timedelta(minutes=2))
    def save_run_receipt(receipt):
        import hashlib
        import json
        import os
        from pathlib import Path

        run_id = get_current_context()["run_id"]
        key = hashlib.sha256(run_id.encode()).hexdigest()
        folder = Path("/opt/airflow/artifacts/seahawks/ticket-day-test") / key
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "receipt.json"
        temporary = folder / "receipt.tmp"
        temporary.write_text(json.dumps({"run_id": run_id, **receipt}, indent=2) + "\n")
        os.replace(temporary, target)
        print(f"Saved verified host run receipt: {target}")
        return str(target)

    save_run_receipt(collect_ticket_prices())


@dag(
    dag_id="sfz_ticket_bridge_smoke",
    schedule=None,
    start_date=datetime(2026, 9, 9, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    default_args={"owner": "laura", "retries": 0},
    tags=["platform", "smoke", "tickets"],
)
def bridge_smoke():
    @task(execution_timeout=timedelta(minutes=2))
    def check_bridge():
        response = TicketCollectorHook().request("health", timeout=90)
        if response["status"] != "success":
            raise RuntimeError(response)
        print(response)
        return response

    check_bridge()


ticket_collection()
bridge_smoke()
