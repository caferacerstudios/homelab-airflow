"""Generate each active team's missing final-game recaps every Tuesday."""
from datetime import timedelta

import pendulum
from airflow.sdk import dag, task
from airflow.timetables.trigger import CronTriggerTimetable


@dag(
    dag_id="sfz_game_recaps",
    schedule=CronTriggerTimetable("45 6 * * 2", timezone="America/Los_Angeles"),
    start_date=pendulum.datetime(2026, 9, 10, tz="America/Los_Angeles"),
    catchup=False, max_active_runs=1, max_active_tasks=1, is_paused_upon_creation=True,
    default_args={"owner": "laura", "retries": 0}, tags=["fan-zone", "recaps", "openai"],
    doc_md="""At 06:45 Pacific each Tuesday, generate missing final-game recaps for
    sites enabled in fan_zone_active_sites. Each team appears as a separate task
    and uses prompts.recap with its own NFL input and recap output directory.
    Complete recaps and historical entries are retained. No-op runs make no API
    requests. Seattle continues publishing /var/lib/sfz-recaps/current/gameRecaps.json.
    This DAG does not generate daily news, build a website, or deploy a site.
    """,
)
def sfz_game_recaps():
    from fan_zone_tasks import active_sites
    sites = active_sites()

    @task(pool="balldontlie_api", execution_timeout=timedelta(minutes=70))
    def generate_recaps(site):
        from airflow.sdk import get_current_context
        from sfz_recap_hook import RecapRefreshHook
        receipt = RecapRefreshHook().refresh(get_current_context()["run_id"], site)
        return {"site": site, "receipt": receipt}

    @task(execution_timeout=timedelta(minutes=2))
    def save_run_receipt(result):
        from airflow.sdk import get_current_context
        from sfz_recap_hook import save_receipt
        return save_receipt(result["receipt"], get_current_context()["run_id"], result["site"])

    for site in sites:
        slug = site["slug"]
        title = f"{site['city']} {site['name']}"
        recaps = generate_recaps.override(
            task_id=f"generate_recaps_{slug}", task_display_name=f"Generate game recaps: {title}",
        )(site)
        save_run_receipt.override(
            task_id=f"save_run_receipt_{slug}", task_display_name=f"Save recap receipt: {title}",
        )(recaps)


sfz_game_recaps()
