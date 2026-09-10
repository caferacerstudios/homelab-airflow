"""Generate missing game recaps from the latest NFL snapshot on wkr."""
from datetime import timedelta

import pendulum
from airflow.sdk import dag, task
from airflow.timetables.trigger import CronTriggerTimetable


@dag(
    dag_id="sfz_game_recaps",
    schedule=CronTriggerTimetable("45 0,3,6,9,12,14,16,18,20,22 * * *", timezone="America/Los_Angeles"),
    start_date=pendulum.datetime(2026, 9, 10, tz="America/Los_Angeles"),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=True,
    default_args={"owner": "laura", "retries": 0},
    tags=["seahawks", "recaps", "openai"],
    doc_md="""Check ten times daily, 30 minutes after the NFL refresh slots.
    The Python host runner reads the latest completed NFL snapshot and uses the
    existing recap writer only for final games missing complete recaps. Existing
    complete recaps are reused, so a no-op uses no OpenAI or source API requests.
    Recaps publish under /var/lib/sfz-recaps/current and builds import them.
    The DAG does not rebuild the website. Existing prose style and model remain.
    """,
)
def sfz_game_recaps():
    @task(pool="balldontlie_api", execution_timeout=timedelta(minutes=70))
    def generate_recaps():
        from airflow.sdk import get_current_context
        from sfz_recap_hook import RecapRefreshHook

        return RecapRefreshHook().refresh(get_current_context()["run_id"])

    @task(execution_timeout=timedelta(minutes=2))
    def save_run_receipt(receipt: dict):
        import hashlib
        import json
        import os
        from pathlib import Path
        from airflow.sdk import get_current_context
        from sfz_recap_hook import validate_receipt

        run_id = get_current_context()["run_id"]
        validate_receipt(receipt, run_id)
        directory = Path("/opt/airflow/artifacts/seahawks/recaps") / hashlib.sha256(run_id.encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "receipt.json"
        temporary = directory / "receipt.json.tmp"
        with temporary.open("w") as handle:
            json.dump(receipt, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        print(f"Recaps saved: {target}; generated={receipt['generatedCount']}; "
              f"OpenAI requests={receipt['openaiRequestCount']}; NFL requests={receipt['requestCount']}")
        return str(target)

    save_run_receipt(generate_recaps())


sfz_game_recaps()
