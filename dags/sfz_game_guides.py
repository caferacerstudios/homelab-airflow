"""Research game-day and viewing guides for every enabled active team."""
from datetime import timedelta

import pendulum
from airflow.sdk import dag, task
from airflow.timetables.trigger import CronTriggerTimetable


@dag(
    dag_id="sfz_game_guides",
    schedule=CronTriggerTimetable("45 4 * * *", timezone="America/Los_Angeles"),
    start_date=pendulum.datetime(2026, 9, 12, tz="America/Los_Angeles"),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=True,
    default_args={"owner": "laura", "retries": 0},
    tags=["fan-zone", "game-day", "where-to-watch", "guides"],
    doc_md="""Research verified regular-season game-day and viewing information at
    04:45 Pacific. Read fan_zone_active_sites at DAG parsing and show a named
    research task and receipt task for each enabled team, like the other DAGs.
    config/game-guides.json controls the research model and per-team workload.
    Each team publishes validated files under its own /var/lib/<prefix>-guides/current
    and returns a receipt. Host work uses the dedicated restricted sfz_guides_host
    SSH connection and existing OpenAI credential. The runner limits research to
    its configured per-run game budget and retains previous good publications.
    No build, production deployment, existing snapshot or existing DAG is changed.
    The website importer requires FAN_ZONE_GUIDES_ENABLED=1 explicitly.
    Keep this DAG paused until deployment/guides/install.py and source-free checks
    succeed. Review the first manual publication and preview before activating
    scheduled research or production consumption.
    """,
)
def sfz_game_guides():
    from fan_zone_tasks import active_sites
    sites = active_sites()

    @task(execution_timeout=timedelta(minutes=35))
    def refresh_guides(site):
        from airflow.sdk import get_current_context
        from sfz_guides_hook import GuidesRefreshHook
        receipt = GuidesRefreshHook().refresh(get_current_context()["run_id"], site)
        return {"site": site, "receipt": receipt}

    @task(execution_timeout=timedelta(minutes=2))
    def save_run_receipt(result):
        from airflow.sdk import get_current_context
        from sfz_guides_hook import save_receipt
        return save_receipt(result["receipt"], get_current_context()["run_id"], result["site"])

    for site in sites:
        slug = site["slug"]
        title = f"{site['city']} {site['name']}"
        result = refresh_guides.override(task_id=f"refresh_guides_{slug}",
            task_display_name=f"Research game-day and viewing guides: {title}")(site)
        save_run_receipt.override(task_id=f"save_run_receipt_{slug}",
            task_display_name=f"Save guide receipt: {title}")(result)


sfz_game_guides()
