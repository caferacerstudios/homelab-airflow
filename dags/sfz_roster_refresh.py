"""Refresh official roster, injury and transaction data for each active Fan Zone team."""
from datetime import timedelta

import pendulum
from airflow.sdk import dag, task
from airflow.timetables.trigger import CronTriggerTimetable


@dag(
    dag_id="sfz_roster_refresh",
    schedule=CronTriggerTimetable("30 5,17 * * *", timezone="America/Los_Angeles"),
    start_date=pendulum.datetime(2026, 9, 12, tz="America/Los_Angeles"),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=True,
    default_args={"owner": "laura", "retries": 0},
    tags=["fan-zone", "roster", "injuries", "transactions"],
    doc_md="""Use fan_zone_active_sites to show a named update and receipt task
    for every enabled team. Refresh twice daily, 05:30 and 17:30 Pacific, from
    the clubs' official roster, injury-report and transaction pages. Publication
    is a separate validated team snapshot; the site imports it on its next build.
    Player performance statistics stay in sfz_nfl_refresh. This DAG makes no
    OpenAI calls and no additional BALLDONTLIE requests. Current validated NFL
    metadata can supply identity/week context, but never proves roster membership.
    Keep this new DAG paused until deployment/roster/install.py succeeds. Then
    unpause it and trigger one manual run for inspection. Existing DAG schedules and pause states are
    unchanged. Missing or unrecognized reports are not evidence of healthy players.
    """,
)
def sfz_roster_refresh():
    from fan_zone_tasks import active_sites
    sites = active_sites()

    @task(execution_timeout=timedelta(minutes=12))
    def refresh_roster(site):
        from airflow.sdk import get_current_context
        from sfz_roster_hook import RosterRefreshHook
        receipt = RosterRefreshHook().refresh(get_current_context()["run_id"], site)
        return {"site": site, "receipt": receipt}

    @task(execution_timeout=timedelta(minutes=2))
    def save_run_receipt(result):
        from airflow.sdk import get_current_context
        from sfz_roster_hook import save_receipt
        return save_receipt(result["receipt"], get_current_context()["run_id"], result["site"])

    for site in sites:
        slug = site["slug"]
        title = f"{site['city']} {site['name']}"
        result = refresh_roster.override(task_id=f"refresh_roster_{slug}",
            task_display_name=f"Refresh roster, injuries & transactions: {title}")(site)
        save_run_receipt.override(task_id=f"save_run_receipt_{slug}",
            task_display_name=f"Save roster receipt: {title}")(result)


sfz_roster_refresh()
