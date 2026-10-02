# Databricks notebook source
# MAYA operation (G9, built in): row count, last data change and freshness of the data product's tables.
# Read only. Returns JSON (dbutils.notebook.exit).

# COMMAND ----------
import json
from datetime import datetime, timezone

dbutils.widgets.text("layer", "all")
dbutils.widgets.text("config", "{}")
layer = dbutils.widgets.get("layer")
cfg = json.loads(dbutils.widgets.get("config"))
DATA_OPS = {"WRITE", "MERGE", "UPDATE", "DELETE", "STREAMING UPDATE", "CREATE TABLE AS SELECT", "REPLACE TABLE AS SELECT",
            "COPY INTO", "TRUNCATE", "CREATE OR REPLACE TABLE AS SELECT", "RESTORE"}
if layer != "all" and layer not in cfg["layers"]:
    dbutils.notebook.exit(json.dumps({"ok": False, "error": f"layer must be all or one of {sorted(cfg['layers'])}"}))

# COMMAND ----------
now = datetime.now(timezone.utc)
tables = []
for name, schemas in cfg["layers"].items():
    if layer not in ("all", name):
        continue
    limit = (cfg.get("freshness_hours") or {}).get(name)
    for schema in schemas:
        cat, sch = schema.split(".")
        for r in spark.sql(f"SELECT table_name, table_type FROM `{cat}`.information_schema.tables "
                           f"WHERE table_schema = '{sch}' ORDER BY table_name").collect():
            if r.table_name in cfg.get("exclude", []):
                continue
            fn = f"`{cat}`.`{sch}`.`{r.table_name}`"
            row = {"layer": name, "table": f"{schema}.{r.table_name}", "type": r.table_type}
            try:
                row["rows"] = spark.sql(f"SELECT COUNT(*) AS n FROM {fn}").first().n
            except Exception as e:
                row["error"] = str(e)[:300]
            if r.table_type in ("MANAGED", "EXTERNAL"):
                h = [x for x in spark.sql(f"DESCRIBE HISTORY {fn} LIMIT 50").collect() if x.operation in DATA_OPS]
                if h:
                    ts = h[0].timestamp.replace(tzinfo=timezone.utc)
                    row["last_change"] = ts.isoformat()
                    row["hours_since_change"] = round((now - ts).total_seconds() / 3600, 1)
                    row["stale"] = limit is not None and row["hours_since_change"] > limit
                row["freshness_limit_hours"] = limit
            tables.append(row)

dbutils.notebook.exit(json.dumps({"ok": True, "mode": "read", "changed": False, "layer": layer, "checked_at": now.isoformat(),
                                  "tables": tables, "stale": [t["table"] for t in tables if t.get("stale")]}, default=str))
