"""Generate one daily article per active team; builds import each team's snapshot."""
from datetime import timedelta
import pendulum
from airflow.sdk import dag, task
from airflow.timetables.trigger import CronTriggerTimetable


@dag(
    dag_id="sfz_daily_article",
    schedule=CronTriggerTimetable("0 8 * * *", timezone="America/Los_Angeles"),
    start_date=pendulum.datetime(2026, 1, 1, tz="America/Los_Angeles"),
    catchup=False, max_active_runs=1, max_active_tasks=1, is_paused_upon_creation=True,
    default_args={"owner": "laura", "retries": 0}, tags=["fan-zone", "news", "openai"],
    doc_md="""Read fan_zone_active_sites at run time and create one article task per
    enabled news site. Each team has its own prompt, photo directory and persistent
    snapshot on wkr. Repeated runs reuse accepted content. The next website build
    imports its selected team's snapshot; this DAG does not build or deploy sites.
    """,
)
def sfz_daily_article():
    @task
    def load_active_sites():
        from fan_zone_tasks import active_sites
        return active_sites()

    @task(map_index_template="{{ site_slug }}", execution_timeout=timedelta(minutes=13))
    def generate_article(site):
        from airflow.sdk import get_current_context
        from sfz_news_hook import NewsRefreshHook, publication_day
        context = get_current_context()
        context["site_slug"] = site["slug"]
        receipt = NewsRefreshHook().refresh(context["run_id"], publication_day(context, site["timezone"]), site)
        return {"site": site, "receipt": receipt}

    @task(map_index_template="{{ site_slug }}", execution_timeout=timedelta(minutes=2))
    def save_run_receipt(result):
        from airflow.sdk import get_current_context
        from fan_zone_tasks import save_receipt
        from sfz_news_hook import publication_day, validate_receipt
        context = get_current_context()
        site, receipt = result["site"], result["receipt"]
        context["site_slug"] = site["slug"]
        validate_receipt(receipt, context["run_id"], publication_day(context, site["timezone"]), site)
        target = save_receipt(receipt, context["run_id"], site)
        print(f"News: generated={receipt['generatedCount']}; articles={receipt['articleCount']}; API calls={receipt['openaiRequestCount']}")
        for note in receipt.get("notes", []):
            print(note)
        return target

    save_run_receipt.expand(result=generate_article.expand(site=load_active_sites()))


sfz_daily_article()
