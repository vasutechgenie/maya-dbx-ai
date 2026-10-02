"""Deploy the example data foundation: schemas, landing volume, source files, layer notebooks, and a Bronze -> Silver -> Gold job.

Usage: python deploy_foundation.py --profile <cli-profile> --warehouse <id> [--catalog solution_builder] [--prefix maya]
       [--notify owner@example.com] [--no-run] [--teardown]

The job is scheduled daily (paused: unpause it where it should run), retries failed tasks, and emails --notify on failure.
"""
import argparse
import base64
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import jobs, workspace

HERE = Path(__file__).parent
JOB_NAME = "maya_example_foundation"


def sql(w, wh, stmt):
    r = w.statement_execution.execute_statement(warehouse_id=wh, statement=stmt, wait_timeout="50s")
    if r.status.state.value != "SUCCEEDED":
        raise RuntimeError(f"{stmt[:80]}... -> {r.status.error}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--catalog", default="solution_builder")
    ap.add_argument("--prefix", default="maya")
    ap.add_argument("--teardown", action="store_true")
    ap.add_argument("--notify", action="append", default=[], help="email notified when the job fails (repeatable)")
    ap.add_argument("--no-run", action="store_true", help="update the job without running it")
    a = ap.parse_args()

    w = WorkspaceClient(profile=a.profile)
    c, p = a.catalog, a.prefix
    bronze, silver, gold = f"{p}_bronze", f"{p}_silver", f"{p}_gold"
    nb_dir = f"/Users/{w.current_user.me().user_name}/maya_example/foundation"

    existing = [j for j in w.jobs.list(name=JOB_NAME)]
    if a.teardown:
        for j in existing:
            w.jobs.delete(j.job_id)
        for s in (gold, silver, bronze):
            sql(w, a.warehouse, f"DROP SCHEMA IF EXISTS {c}.{s} CASCADE")
        w.workspace.delete(nb_dir, recursive=True)
        print("foundation removed")
        return

    if not a.no_run:
        for s in (bronze, silver, gold):
            sql(w, a.warehouse, f"CREATE SCHEMA IF NOT EXISTS {c}.{s}")
        sql(w, a.warehouse, f"CREATE VOLUME IF NOT EXISTS {c}.{bronze}.landing")

        for f in sorted((HERE / "data").glob("*/*.csv")):
            with open(f, "rb") as fh:
                w.files.upload(f"/Volumes/{c}/{bronze}/landing/{f.parent.name}/{f.name}", fh, overwrite=True)

        w.workspace.mkdirs(nb_dir)
        for nb in sorted((HERE / "notebooks").glob("*.py")):
            w.workspace.import_(f"{nb_dir}/{nb.stem}", content=base64.b64encode(nb.read_bytes()).decode(),
                                format=workspace.ImportFormat.SOURCE, language=workspace.Language.PYTHON, overwrite=True)

    params = {"catalog": c, "bronze": bronze, "silver": silver, "gold": gold}
    steps = [("bronze_load", "01_bronze_load", None), ("silver_build", "02_silver_build", "bronze_load"),
             ("gold_build", "03_gold_build", "silver_build")]
    tasks = [jobs.Task(task_key=k, notebook_task=jobs.NotebookTask(notebook_path=f"{nb_dir}/{nb}", base_parameters=params,
                                                                   source=jobs.Source.WORKSPACE),
                       depends_on=[jobs.TaskDependency(task_key=d)] if d else None,
                       max_retries=2, min_retry_interval_millis=300000, retry_on_timeout=True) for k, nb, d in steps]
    settings = dict(name=JOB_NAME, tasks=tasks, max_concurrent_runs=1, tags={"maya_example": "foundation"},
                    timeout_seconds=7200,
                    schedule=jobs.CronSchedule(quartz_cron_expression="0 0 5 * * ?", timezone_id="UTC",
                                               pause_status=jobs.PauseStatus.PAUSED))
    if a.notify:
        settings["email_notifications"] = jobs.JobEmailNotifications(on_failure=a.notify)
    if existing:
        job_id = existing[0].job_id
        w.jobs.reset(job_id, new_settings=jobs.JobSettings(**settings))
    else:
        job_id = w.jobs.create(**settings).job_id
    if a.no_run:
        print(f"job {job_id} updated (not run)")
        return
    print(f"job {job_id}: running Bronze -> Silver -> Gold ...")
    run = w.jobs.run_now(job_id).result()
    print("run", run.run_id, run.state.result_state.value)
    for t in run.tasks:
        out = w.jobs.get_run_output(t.run_id)
        print(f"  {t.task_key}: {t.state.result_state.value} {out.notebook_output.result if out.notebook_output else ''}")


if __name__ == "__main__":
    main()
