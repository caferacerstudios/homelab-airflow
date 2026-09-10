"""Refresh the Seahawks NFL snapshot ten times each Seattle calendar day."""
from datetime import timedelta

import pendulum
from airflow.sdk import dag, task
from airflow.timetables.trigger import CronTriggerTimetable


@dag(
    dag_id="sfz_nfl_refresh",
    schedule=CronTriggerTimetable("15 0,3,6,9,12,14,16,18,20,22 * * *", timezone="America/Los_Angeles"),
    start_date=pendulum.datetime(2026, 9, 10, tz="America/Los_Angeles"),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=True,
    default_args={"owner": "laura", "retries": 0},
    tags=["seahawks", "nfl", "balldontlie"],
    doc_md="""Refresh a validated, persistent NFL snapshot on wkr. Requests are paced
    at 15 seconds. Builds import this snapshot after the separate build cutover;
    collection alone does not update the website's static HTML. The existing
    Node normalizers are retained; orchestration and host publication are Python.
    Manual validation runs call the API. Reuse a successful run's receipt instead
    of re-fetching when its task is cleared. Never enable before installation checks.
    """,
)
def sfz_nfl_refresh():
    @task(pool="balldontlie_api", execution_timeout=timedelta(minutes=70))
    def refresh_nfl_snapshot():
        from airflow.sdk import get_current_context
        from sfz_nfl_hook import NflRefreshHook

        return NflRefreshHook().refresh(get_current_context()["run_id"])

    @task(execution_timeout=timedelta(minutes=2))
    def save_run_receipt(receipt: dict):
        import hashlib
        import json
        import os
        from pathlib import Path
        from airflow.sdk import get_current_context
        from sfz_nfl_hook import validate_receipt

        run_id = get_current_context()["run_id"]
        validate_receipt(receipt, run_id)
        directory = Path("/opt/airflow/artifacts/seahawks/nfl") / hashlib.sha256(run_id.encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "receipt.json"
        temporary = directory / "receipt.json.tmp"
        with temporary.open("w") as handle:
            json.dump(receipt, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        print(f"NFL receipt saved: {target}; requests={receipt['requestCount']}; updatedAt={receipt['updatedAt']}")
        return str(target)

    save_run_receipt(refresh_nfl_snapshot())


sfz_nfl_refresh()
