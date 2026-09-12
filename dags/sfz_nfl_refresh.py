"""Refresh each active team NFL snapshot ten times each Seattle calendar day."""
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
    tags=["fan-zone", "nfl", "balldontlie"],
    doc_md="""Read fan_zone_active_sites at DAG parsing and show named tasks for
    each enabled team. Preserve the existing ten daily Seattle-time slots.
    Refresh validated, persistent NFL snapshots on wkr. Requests are paced
    at 15 seconds. Builds import this snapshot after the separate build cutover;
    collection alone does not update the website's static HTML. The existing
    Node normalizers are retained; orchestration and host publication are Python.
    Manual validation runs call the API. Reuse a successful run's receipt instead
    of re-fetching when its task is cleared. Never enable before installation checks.
    """,
)
def sfz_nfl_refresh():
    from fan_zone_tasks import active_sites
    sites = active_sites()

    @task(pool="balldontlie_api", execution_timeout=timedelta(minutes=70))
    def refresh_nfl_snapshot(site):
        from airflow.sdk import get_current_context
        from sfz_nfl_hook import NflRefreshHook

        return {"site": site, "receipt": NflRefreshHook().refresh(get_current_context()["run_id"], site)}

    @task(execution_timeout=timedelta(minutes=2))
    def save_run_receipt(result: dict):
        import hashlib
        import json
        import os
        from pathlib import Path
        from airflow.sdk import get_current_context
        from sfz_nfl_hook import validate_receipt

        run_id = get_current_context()["run_id"]
        site, receipt = result["site"], result["receipt"]
        validate_receipt(receipt, run_id, site)
        directory = Path("/opt/airflow/artifacts") / site["slug"] / "nfl" / hashlib.sha256(run_id.encode()).hexdigest()
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

    for site in sites:
        slug = site["slug"]
        title = f"{site['city']} {site['name']}"
        refreshed = refresh_nfl_snapshot.override(
            task_id=f"refresh_nfl_snapshot_{slug}",
            task_display_name=f"Refresh NFL: {title}",
        )(site)
        save_run_receipt.override(
            task_id=f"save_run_receipt_{slug}",
            task_display_name=f"Save NFL receipt: {title}",
        )(refreshed)


sfz_nfl_refresh()
