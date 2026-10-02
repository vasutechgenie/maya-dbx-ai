"""G11 delivery: pure functions of the approved plan. The bundle gets the evaluation schema and tables (deploy
scripts), the evaluation code and dataset (jobs/G11/eval) and the regression job: it runs whenever a source table of
the metric views changes (or on a schedule) and fails, notifying the owners, when a pass rate drops below its
threshold."""
from maya.core import bundle

from .common import COMMENTS, JOB_KEY, TABLES, TEMPLATES, q, table


def eval_config(spec) -> dict:
    return {"version": spec["version"], "targets": spec["targets"], "thresholds": spec["thresholds"],
            "endpoint": spec["endpoint"], "genie_title": spec["genie_title"], "judge_endpoint": spec["judge_endpoint"],
            "results_table": table(spec, "eval_results"), "runs_table": table(spec, "eval_runs"),
            "dataset_table": table(spec, "eval_dataset"),
            "questions": [{k: x[k] for k in ("id", "question", "sql", "targets", "tags")} for x in spec["questions"]]}


def files(spec) -> dict:
    return {"10_schema/schema.sql": [f"CREATE SCHEMA IF NOT EXISTS {q(spec['schema'])} COMMENT "
                                     f"'Evaluation dataset and results of the data product (MAYA G11)'"],
            "20_tables/tables.sql": [f"CREATE TABLE IF NOT EXISTS {q(table(spec, n))} ({cols}) COMMENT '{COMMENTS[n]}'"
                                     for n, cols in TABLES.items()]}


def job_files(spec) -> dict:
    return {"eval/run_eval.py": (TEMPLATES / "eval" / "run_eval.py").read_text(), "eval/eval_config.json": eval_config(spec)}


def job(system, spec, catalogs, trigger="job") -> dict:
    params = ["--code-dir", "${workspace.file_path}/jobs/G11/eval",
              "--experiment", "${workspace.root_path}/maya_evaluation_experiment", "--trigger", trigger]
    for c in catalogs:
        params += ["--catalog", f"{c}={{{{catalog:{c}}}}}"]
    reg = spec["regression"]
    out = {"name": f"MAYA evaluation - {system.name}",
           "description": f"Evaluates the agents ({spec['endpoint']}) and the Genie space of {system.name} against the "
                          f"evaluation dataset; fails below the pass thresholds {spec['thresholds']}",
           "max_concurrent_runs": 1, "timeout_seconds": 5400, "tags": {"maya_goal": "G11"},
           "environments": [{"environment_key": "eval", "spec": {
               "client": "2", "dependencies": ["mlflow>=3.1", "databricks-sdk>=0.50"]}}],
           "tasks": [{"task_key": "evaluate", "environment_key": "eval", "max_retries": 1, "min_retry_interval_millis": 600000,
                      "spark_python_task": {"python_file": "../jobs/G11/eval/run_eval.py", "parameters": params}}]}
    if reg["notify"]:
        out["email_notifications"] = {"on_failure": reg["notify"]}
    if reg["schedule"]:
        out["schedule"] = {"quartz_cron_expression": reg["schedule"], "timezone_id": reg["timezone"]}
    elif reg["on_change"] and reg["tables"]:
        out["trigger"] = {"table_update": {"table_names": reg["tables"], "condition": "ANY_UPDATED",
                                           "min_time_between_triggers_seconds": 3600, "wait_after_last_change_seconds": 600}}
    return out


def resources(system, spec, catalogs) -> dict:
    return {"jobs": {JOB_KEY: job(system, spec, catalogs)}}


def bundle_content(system, spec) -> dict:
    cats = sorted(set(system.catalogs))
    return {"files": files(spec), "jobs": job_files(spec), "resources": resources(system, spec, cats), "catalogs": cats}


def write(ctx, spec) -> list[str]:
    content = bundle_content(ctx.system, spec)
    base, jbase = bundle.scripts_dir(ctx.system, ctx.goal.id), bundle.jobs_dir(ctx.system, ctx.goal.id)
    stale = [str(p.relative_to(base)) for p in bundle.script_files(base) if str(p.relative_to(base)) not in content["files"]]
    written = bundle.write(ctx, content["files"], catalogs=content["catalogs"], remove=stale)
    jstale = [str(p.relative_to(jbase)) for p in bundle.script_files(jbase, any_file=True)
              if str(p.relative_to(jbase)) not in content["jobs"]]
    bundle.write(ctx, content["jobs"], catalogs=content["catalogs"], remove=jstale, jobs=True)
    bundle.write_resources(ctx, content["resources"], catalogs=content["catalogs"])
    return written
