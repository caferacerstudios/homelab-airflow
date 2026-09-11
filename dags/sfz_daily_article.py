"""Generate one Seahawks article daily; website builds import the snapshot."""
from datetime import timedelta
import pendulum
from airflow.sdk import dag, task
from airflow.timetables.trigger import CronTriggerTimetable


@dag(
    dag_id='sfz_daily_article',
    schedule=CronTriggerTimetable('0 8 * * *', timezone='America/Los_Angeles'),
    start_date=pendulum.datetime(2026, 9, 11, tz='America/Los_Angeles'),
    catchup=False, max_active_runs=1, max_active_tasks=1, is_paused_upon_creation=True,
    default_args={'owner': 'laura', 'retries': 0}, tags=['seahawks', 'news', 'openai'],
    doc_md='One article per Seattle day. Research and generated prose are saved on wkr. Repeated runs reuse accepted content. The DAG publishes /var/lib/sfz-news/current; the next website build imports it. It does not build or deploy the site.',
)
def sfz_daily_article():
    @task(execution_timeout=timedelta(minutes=13))
    def generate_article():
        from airflow.sdk import get_current_context
        from sfz_news_hook import NewsRefreshHook, publication_day
        context = get_current_context()
        return NewsRefreshHook().refresh(context['run_id'], publication_day(context))

    @task(execution_timeout=timedelta(minutes=2))
    def save_run_receipt(receipt):
        import hashlib
        import json
        import os
        from pathlib import Path
        from airflow.sdk import get_current_context
        from sfz_news_hook import publication_day, validate_receipt
        context = get_current_context()
        validate_receipt(receipt, context['run_id'], publication_day(context))
        directory = Path('/opt/airflow/artifacts/seahawks/news') / hashlib.sha256(context['run_id'].encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        temporary, target = directory / 'receipt.json.tmp', directory / 'receipt.json'
        with temporary.open('w') as out:
            json.dump(receipt, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
        print(f"News: generated={receipt['generatedCount']}; articles={receipt['articleCount']}; API calls={receipt['openaiRequestCount']}")
        for note in receipt.get('notes', []):
            print(note)
        return str(target)

    save_run_receipt(generate_article())


sfz_daily_article()
