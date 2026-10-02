# Databricks notebook source
# MAYA operation (G9, built in): the data quality results of G7's rules.
# mode=validate (the default) reports the latest check run without running anything; mode=run runs G7's check job
# first (it records a new check run), then reports it. Returns JSON (dbutils.notebook.exit).

# COMMAND ----------
import json

dbutils.widgets.text("table", "all")
dbutils.widgets.text("mode", "validate")
dbutils.widgets.text("config", "{}")
table, mode = dbutils.widgets.get("table"), dbutils.widgets.get("mode")
cfg = json.loads(dbutils.widgets.get("config"))
s = ".".join(f"`{p}`" for p in cfg["dq_schema"].split("."))
if mode not in ("validate", "run"):
    dbutils.notebook.exit(json.dumps({"ok": False, "error": "mode must be validate or run"}))

# COMMAND ----------
out = {"ok": True, "mode": mode, "table": table, "changed": False}
if mode == "run":
    from databricks.sdk import WorkspaceClient
    run = WorkspaceClient().jobs.run_now(int(cfg["job_id"])).result()
    out.update(changed=True, check_job_run_id=run.run_id, check_job_result=run.state.result_state.value)

latest = spark.sql(f"SELECT max_by(check_run_id, started_at) AS id, max(started_at) AS at FROM {s}.dq_runs "
                   f"WHERE finished_at IS NOT NULL").first()
where = "" if table == "all" else f"AND (table_name = '{table.replace(chr(39), '')}' OR table_name LIKE '%.{table.replace(chr(39), '')}')"
rows = spark.sql(f"""SELECT table_name, rule_id, severity, description, passed, failed_rows, total_rows FROM {s}.dq_results
                     WHERE check_run_id = '{latest.id}' {where} ORDER BY passed, severity, table_name, rule_id""").collect()
fresh = spark.sql(f"SELECT table_name, age_hours, max_hours, stale FROM {s}.dq_freshness_latest").collect()
failing = [r.asDict() for r in rows if not r.passed]
out.update(check_run_id=latest.id, checked_at=str(latest.at), rules_checked=len(rows), rules_failing=len(failing),
           critical_failing=sum(1 for r in failing if r["severity"] == "critical"), failing=failing[:50],
           stale_tables=[f.table_name for f in fresh if str(f.stale).lower() == "true"])
dbutils.notebook.exit(json.dumps(out, default=str))
