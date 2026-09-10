from datetime import datetime, timezone

from airflow.sdk import dag, task


@dag(
    dag_id="platform_smoke",
    schedule=None,
    start_date=datetime(2026, 9, 9, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    tags=["platform", "smoke"],
)
def platform_smoke():
    @task
    def write_artifact():
        import hashlib
        import json
        from pathlib import Path

        from airflow.sdk import get_current_context

        run_id = get_current_context()["run_id"]
        key = hashlib.sha256(run_id.encode()).hexdigest()

        folder = Path("/opt/airflow/artifacts/smoke") / key
        folder.mkdir(parents=True, exist_ok=True)

        target = folder / "result.json"
        target.write_text(
            json.dumps({"ok": True, "run_id": run_id})
        )

        return str(target)

    @task
    def verify_artifact(path):
        import json
        from pathlib import Path

        value = json.loads(Path(path).read_text())

        if value.get("ok") is not True:
            raise ValueError("Smoke artifact failed validation")

        print("Smoke artifact and XCom handoff verified.")

    verify_artifact(write_artifact())


platform_smoke()
