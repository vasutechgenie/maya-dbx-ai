# Databricks notebook source
# Operation (G9): load new source files from the landing volume through Bronze, Silver and Gold.
# mode=validate (the default) only reports which files would load and whether their columns fit; mode=run starts the
# foundation job, waits for it and reports what each layer did. Running it again when nothing is new changes nothing.
# Returns JSON (dbutils.notebook.exit), like every MAYA operation.

# COMMAND ----------
import json

dbutils.widgets.text("source", "all")
dbutils.widgets.text("mode", "validate")
dbutils.widgets.text("catalog", "solution_builder")
dbutils.widgets.text("bronze", "maya_bronze")
dbutils.widgets.text("foundation_job", "maya_example_foundation")
source, mode = dbutils.widgets.get("source"), dbutils.widgets.get("mode")
catalog, bronze = dbutils.widgets.get("catalog"), dbutils.widgets.get("bronze")
SOURCES = ["regions", "products", "customers", "orders", "shipments", "returns"]
if mode not in ("validate", "run"):
    dbutils.notebook.exit(json.dumps({"ok": False, "error": "mode must be validate or run"}))
if source != "all" and source not in SOURCES:
    dbutils.notebook.exit(json.dumps({"ok": False, "error": f"source must be all or one of {SOURCES}"}))

# COMMAND ----------
landing = f"/Volumes/{catalog}/{bronze}/landing"
loaded = {r.source_file for r in spark.sql(f"SELECT source_file FROM {catalog}.{bronze}.ingestion_log").collect()}
pending = []
for s in SOURCES if source == "all" else [source]:
    try:
        files = [f for f in dbutils.fs.ls(f"{landing}/{s}/") if f.name.endswith(".csv")]
    except Exception:
        files = []
    have = [c for c in spark.table(f"{catalog}.{bronze}.raw_{s}").columns if not c.startswith("_")]
    for f in files:
        path = f.path.replace("dbfs:", "")
        if path in loaded or f.path in loaded:
            continue
        df = spark.read.option("header", True).csv(path)
        pending.append({"source": s, "file": path, "rows": df.count(), "size_bytes": f.size,
                        "missing_columns": [c for c in have if c not in df.columns],
                        "new_columns": [c for c in df.columns if c not in have]})
mismatched = [p for p in pending if p["missing_columns"]]
result = {"ok": not mismatched, "mode": mode, "source": source, "pending_files": pending,
          "rows_to_load": sum(p["rows"] for p in pending), "changed": False}
if mismatched:
    result["error"] = "files lack columns the Bronze tables have: fix them before loading"

# COMMAND ----------
if mode == "run" and pending and not mismatched:
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    job = next(iter(w.jobs.list(name=dbutils.widgets.get("foundation_job"))), None)
    if job is None:
        result.update(ok=False, error=f"job {dbutils.widgets.get('foundation_job')} not found")
    else:
        run = w.jobs.run_now(job.job_id).result()
        tasks = {}
        for t in run.tasks or []:
            out = w.jobs.get_run_output(t.run_id)
            tasks[t.task_key] = {"state": t.state.result_state.value if t.state.result_state else None,
                                 "result": out.notebook_output.result if out.notebook_output else None}
        result.update(ok=run.state.result_state.value == "SUCCESS", changed=True, foundation_run_id=run.run_id,
                      foundation_tasks=tasks)
elif mode == "run":
    result["note"] = "nothing new to load" if not pending else result.get("error")

dbutils.notebook.exit(json.dumps(result, default=str))
