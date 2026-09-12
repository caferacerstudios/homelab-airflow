"""Named EventSpy tasks for teams in the shared fan_zone_active_sites Variable."""
from datetime import datetime, timedelta, timezone

from airflow.exceptions import AirflowSkipException
from airflow.sdk import dag, task
from airflow.timetables.trigger import CronTriggerTimetable

from sfz_ticket_bridge import TicketCollectorHook


@dag(
    dag_id="sfz_eventspy_collect",
    schedule=CronTriggerTimetable("0 3,6,9,12,15,18,21 * * *", timezone="America/Los_Angeles", run_immediately=False),
    start_date=datetime(2026, 9, 9, tzinfo=timezone.utc),
    catchup=False, is_paused_upon_creation=True, max_active_runs=1, max_active_tasks=1,
    default_args={"owner": "laura", "retries": 0},
    tags=["fan-zone", "tickets", "eventspy"],
    doc_md="""Read fan_zone_active_sites once when parsing and create separate visible
    collection tasks for enabled teams with EventSpy settings. Variable changes apply on
    the next parse. Seven original Pacific slots; manual and backfill runs skip.
    The existing balldontlie_api pool also serializes the new-team schedule lookup with NFL refreshes.
    The host bridge admits only slots after installation and keeps durable receipts.
    Completed or started games make no source requests. Seahawks JSON keeps its original
    schema and directory; each other site has its own configured output. Shared games
    reuse the same event-and-slot cache. Neither ticket collection nor this DAG builds a site.
    """,
)
def ticket_collection():
    from fan_zone_tasks import active_sites
    sites = [site for site in active_sites() if "eventspy" in site]

    @task(pool="balldontlie_api", execution_timeout=timedelta(minutes=67))
    def collect_ticket_prices(site):
        from airflow.sdk import get_current_context
        context = get_current_context()
        run_type = context["dag_run"].run_type
        if getattr(run_type, "value", run_type) != "scheduled":
            raise AirflowSkipException("Only regular scheduled runs may contact EventSpy.")
        slot = context["data_interval_end"].astimezone(timezone.utc).isoformat()
        receipt = TicketCollectorHook().request("collect", slot=slot, site=site)
        print(receipt)
        if receipt["status"] == "skipped":
            raise AirflowSkipException(receipt["message"])
        if receipt["status"] != "success":
            raise RuntimeError(receipt["message"])
        return receipt

    @task(execution_timeout=timedelta(minutes=2))
    def save_run_receipt(receipt, site):
        from airflow.sdk import get_current_context
        from sfz_ticket_bridge import save_receipt
        return save_receipt(receipt, get_current_context()["run_id"], site)

    for site in sites:
        slug, title = site["slug"], f"{site['city']} {site['name']}"
        result = collect_ticket_prices.override(
            task_id=f"collect_ticket_prices_{slug}", task_display_name=f"Collect tickets: {title}",
        )(site)
        save_run_receipt.override(
            task_id=f"save_run_receipt_{slug}", task_display_name=f"Save ticket receipt: {title}",
        )(result, site)


@dag(
    dag_id="sfz_ticket_bridge_smoke", schedule=None,
    start_date=datetime(2026, 9, 9, tzinfo=timezone.utc), catchup=False, max_active_runs=1,
    default_args={"owner": "laura", "retries": 0}, tags=["platform", "smoke", "tickets"],
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
