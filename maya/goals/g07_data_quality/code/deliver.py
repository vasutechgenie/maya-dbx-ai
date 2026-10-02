"""What the approved plan becomes in the project bundle. Everything here is a pure function of the plan, so the same
plan always produces the same files, in every environment (catalog names become tokens or bundle variables).

scripts/G7/10_schema/quality_schema.sql     the quality schema, its tables and views, the approved rules, read grants
scripts/G7/20_dashboard/dq_dashboard.json   the data quality dashboard (created or updated in place, published)
jobs/G7/10_start/start.sql                  a check run starts
jobs/G7/20_rules/<rule id>.sql              one rule: its result, and its failing rows (keys and non-sensitive columns)
jobs/G7/30_freshness/freshness.json         each monitored table's last data change and age
jobs/G7/40_finish/finish.sql                the run ends; results older than the retention are deleted
jobs/G7/50_alerts/alerts.json               the alerts are evaluated on the run's results
resources/maya_g7.yml                       the check job (on the declared schedule) and the two alerts"""
import json

from maya.core import bundle
from maya.core.workspace import ident, lit
from maya.status.dashboard import _counter, _filter, _table, _text

from .common import ALERTS, JOB_KEY, alert_key, checked_relation, counts_sql, passed_sql, q, rule_columns

RUNS_LATEST = "(SELECT max_by(check_run_id, started_at) AS check_run_id FROM {s}.dq_runs)"
DASHBOARD = "20_dashboard/dq_dashboard.json"


def _s(spec):
    return q(spec["schema"])


def catalogs(spec) -> set[str]:
    return {spec["schema"].split(".")[0]} | {r["table"].split(".")[0] for r in spec["rules"]} | \
        {f["table"].split(".")[0] for f in spec["freshness"]}


# ---------------------------------------------------------------- the quality schema
def schema_statements(spec) -> list[str]:
    s = _s(spec)
    out = [
        f"CREATE SCHEMA IF NOT EXISTS {s} COMMENT 'Data quality of the {spec['project']} data product: rules, check runs, "
        f"results, quarantined rows and freshness (maintained by MAYA G7)'",
        f"CREATE TABLE IF NOT EXISTS {s}.dq_runs (check_run_id STRING, started_at TIMESTAMP, finished_at TIMESTAMP) "
        "COMMENT 'One row per run of the data quality check job'",
        f"""CREATE TABLE IF NOT EXISTS {s}.dq_results (check_run_id STRING, checked_at TIMESTAMP, rule_id STRING,
            table_name STRING, rule_type STRING, column_name STRING, severity STRING, description STRING,
            total_rows BIGINT, failed_rows BIGINT, passed BOOLEAN) COMMENT 'Result of every rule in every check run'""",
        f"""CREATE TABLE IF NOT EXISTS {s}.dq_quarantine (check_run_id STRING, checked_at TIMESTAMP, rule_id STRING,
            table_name STRING, row_key STRING, row_json STRING)
            COMMENT 'Sample of the rows that broke a rule: their key and non-sensitive columns, as JSON'""",
        f"""CREATE TABLE IF NOT EXISTS {s}.dq_freshness (check_run_id STRING, checked_at TIMESTAMP, table_name STRING,
            layer STRING, last_data_change TIMESTAMP, age_hours DOUBLE, max_hours DOUBLE, stale BOOLEAN, error STRING)
            COMMENT 'Last data change of every monitored table in every check run, against its freshness limit'""",
        f"CREATE TABLE IF NOT EXISTS {s}.dq_alert_tests (alert STRING, note STRING, inserted_at TIMESTAMP) "
        "COMMENT 'Test rows that make an alert fire; empty except while MAYA test-fires the alerts'",
        f"""CREATE OR REPLACE TABLE {s}.dq_rules (rule_id STRING, table_name STRING, rule_type STRING, column_name STRING,
            severity STRING, tolerance DOUBLE, description STRING, origin STRING, definition STRING)
            COMMENT 'The approved data quality rules (origin: customer, key or agent suggestion)'""",
    ]
    if spec["rules"]:
        rows = [f"({lit(r['id'])}, {lit(r['table'])}, {lit(r['type'])}, {lit(', '.join(rule_columns(r)) or None)}, "
                f"{lit(r['severity'])}, {float(r.get('tolerance') or 0)}, {lit(r['description'])}, {lit(r['origin'])}, "
                f"{lit(json.dumps(definition(r), sort_keys=True))})" for r in spec["rules"]]
        out.append(f"INSERT INTO {s}.dq_rules VALUES {', '.join(rows)}")
    latest = f"(SELECT max_by(check_run_id, started_at) AS id, max(started_at) AS started_at FROM {s}.dq_runs WHERE finished_at IS NOT NULL)"
    out += [
        f"""CREATE OR REPLACE VIEW {s}.dq_latest COMMENT 'Rule results of the latest finished check run' AS
            SELECT r.*, l.started_at AS run_started_at FROM {s}.dq_results r JOIN {latest} l ON r.check_run_id = l.id""",
        f"""CREATE OR REPLACE VIEW {s}.dq_freshness_latest COMMENT 'Freshness of every table in the latest finished check run' AS
            SELECT f.*, l.started_at AS run_started_at FROM {s}.dq_freshness f JOIN {latest} l ON f.check_run_id = l.id""",
        f"""CREATE OR REPLACE VIEW {s}.dq_run_summary COMMENT 'Rules checked and failing per check run' AS
            SELECT u.check_run_id, u.started_at, u.finished_at, count(r.rule_id) AS rules,
              count_if(NOT r.passed) AS failing, count_if(NOT r.passed AND r.severity = 'critical') AS critical_failing,
              count_if(NOT r.passed AND r.severity = 'warning') AS warning_failing
            FROM {s}.dq_runs u LEFT JOIN {s}.dq_results r ON r.check_run_id = u.check_run_id
            GROUP BY u.check_run_id, u.started_at, u.finished_at""",
    ]
    for fn, cols in sorted((spec.get("hidden") or {}).items()):
        if cols:
            names = ", ".join(lit(c) for c in cols)
            out.append(f"""DELETE FROM {s}.dq_quarantine WHERE table_name = {lit(fn)}
                AND (arrays_overlap(coalesce(json_object_keys(row_json), array()), array({names}))
                  OR arrays_overlap(coalesce(json_object_keys(row_key), array()), array({names})))""")
    for p in spec["readers"]:
        who = p.get("group") or p["service_principal"]
        out.append(f"GRANT USE SCHEMA, SELECT ON SCHEMA {s} TO {ident(who)}")
    return out


def definition(rule) -> dict:
    """What makes a rule the rule (compared with the deployed one)."""
    keep = ("type", "column", "columns", "values", "min", "max", "expression", "to", "severity", "tolerance")
    return {k: rule[k] for k in keep if rule.get(k) not in (None, [], "")}


# ---------------------------------------------------------------- the check job's scripts
def rule_statements(spec, r) -> list[str]:
    s, run = _s(spec), RUNS_LATEST.format(s=_s(spec))
    col = ", ".join(rule_columns(r)) or None
    out = [f"""INSERT INTO {s}.dq_results
SELECT r.check_run_id, current_timestamp(), {lit(r['id'])}, {lit(r['table'])}, {lit(r['type'])}, {lit(col)},
  {lit(r['severity'])}, {lit(r['description'])}, c.total, c.failed, {passed_sql(r)}
FROM ({counts_sql(r['table'], r)}) c CROSS JOIN {run} r"""]
    n = int(spec.get("quarantine_rows") or 0)
    if n and r["type"] != "row_count":
        def struct(cols):
            return f"to_json(named_struct({', '.join(f'{lit(c)}, b.{ident(c)}' for c in cols)}))" if cols else "NULL"
        out.append(f"""INSERT INTO {s}.dq_quarantine
SELECT r.check_run_id, current_timestamp(), {lit(r['id'])}, {lit(r['table'])}, {struct(r['key_columns'])},
  {struct(r['quarantine_columns'])}
FROM (SELECT * FROM ({checked_relation(r['table'], r)}) WHERE _maya_bad LIMIT {n}) b CROSS JOIN {run} r""")
    return out


def job_files(spec, alert_marker) -> dict:
    s = _s(spec)
    days = int(spec.get("retention_days") or 90)
    files = {"10_start/start.sql": [f"INSERT INTO {s}.dq_runs SELECT uuid(), current_timestamp(), CAST(NULL AS TIMESTAMP)"]}
    for r in spec["rules"]:
        files[f"20_rules/{r['id']}.sql"] = rule_statements(spec, r)
    files["30_freshness/freshness.json"] = {"freshness": {
        "into": f"{spec['schema']}.dq_freshness", "runs": f"{spec['schema']}.dq_runs",
        "tables": [{"table": f["table"], "layer": f["layer"], "max_hours": f["max_hours"]} for f in spec["freshness"]]}}
    files["40_finish/finish.sql"] = [
        f"""MERGE INTO {s}.dq_runs t USING {RUNS_LATEST.format(s=s)} l ON t.check_run_id = l.check_run_id
            WHEN MATCHED THEN UPDATE SET finished_at = current_timestamp()""",
        *(f"DELETE FROM {s}.{tbl} WHERE checked_at < current_timestamp() - INTERVAL {days} DAYS"
          for tbl in ("dq_results", "dq_quarantine", "dq_freshness")),
        f"DELETE FROM {s}.dq_runs WHERE started_at < current_timestamp() - INTERVAL {days} DAYS"]
    files["50_alerts/alerts.json"] = {"alert_runs": [{"marker": alert_marker}]}
    return files


# ---------------------------------------------------------------- bundle resources: the job and the alerts
def alert_queries(spec) -> dict:
    s = spec["schema"]
    return {
        "critical_failures": (f"SELECT count(*) AS value FROM (SELECT rule_id FROM {s}.dq_latest WHERE severity = 'critical' "
                              f"AND NOT passed UNION ALL SELECT alert FROM {s}.dq_alert_tests WHERE alert = 'critical_failures')"),
        "stale_data": (f"SELECT count(*) AS value FROM (SELECT table_name FROM {s}.dq_freshness_latest WHERE stale "
                       f"UNION ALL SELECT alert FROM {s}.dq_alert_tests WHERE alert = 'stale_data')"),
    }


ALERT_TEXT = {
    "critical_failures": ("critical rule failures", "Critical data quality rules fail",
                          "Critical data quality rules of {p} fail in the latest check run. The data quality dashboard "
                          "shows which rules and a sample of the failing rows."),
    "stale_data": ("stale data", "Data is stale",
                   "Tables of {p} have not changed within their freshness limit. The data quality dashboard shows which "
                   "tables and when they last changed."),
}


def alerts(spec) -> dict:
    sched = spec["schedule"]
    subs = [{"user_email": x["user"]} if x.get("user") else {"destination_id": x["destination"]} for x in spec["recipients"]]
    out = {}
    for name, sql in alert_queries(spec).items():
        title, summary, desc = ALERT_TEXT[name]
        out[alert_key(name)] = {
            "display_name": f"{spec['project']} data quality: {title}",
            "query_text": sql, "warehouse_id": "${var.warehouse_id}",
            "custom_summary": summary,
            "custom_description": f"{desc.format(p=spec['project'])} ({spec['marker']}:{name})",
            "evaluation": {"source": {"name": "value", "aggregation": "MAX"}, "comparison_operator": "GREATER_THAN",
                           "threshold": {"value": {"double_value": 0}}, "empty_result_state": "OK",
                           **({"notification": {"notify_on_ok": True, "subscriptions": subs}} if subs else {})},
            # evaluated by the check job at the end of every run, so the alert's own schedule stays paused
            "schedule": {"quartz_cron_schedule": sched["cron"], "timezone_id": sched["timezone"], "pause_status": "PAUSED"},
        }
    return out


def job(spec) -> dict:
    sched = spec["schedule"]
    users = [x["user"] for x in spec["recipients"] if x.get("user")]
    dests = [{"id": x["destination"]} for x in spec["recipients"] if x.get("destination")]
    params = ["--scripts", "${workspace.file_path}/jobs/G7", "--warehouse-id", "${var.warehouse_id}", "--keep-going"]
    for c in sorted(catalogs(spec)):
        params += ["--catalog", f"{c}={{{{catalog:{c}}}}}"]
    j = {"name": f"MAYA data quality - {spec['project']}",
         "description": f"Checks the data quality rules of {spec['project']}, records freshness and evaluates the "
                        f"alerts ({spec['marker']}). Maintained by MAYA G7.",
         "max_concurrent_runs": 1,
         "schedule": {"quartz_cron_expression": sched["cron"], "timezone_id": sched["timezone"],
                      "pause_status": "PAUSED" if sched.get("paused") else "UNPAUSED"},
         "environments": [{"environment_key": "maya", "spec": {"client": "2", "dependencies": ["databricks-sdk>=0.40"]}}],
         "tasks": [{"task_key": "checks", "environment_key": "maya", "max_retries": 1,
                    "spark_python_task": {"python_file": "../maya_deploy/run_scripts.py", "parameters": params}}]}
    if users:
        j["email_notifications"] = {"on_failure": users}
    if dests:
        j["webhook_notifications"] = {"on_failure": dests}
    return j


def resources(spec) -> dict:
    return {"jobs": {JOB_KEY: job(spec)}, "alerts": alerts(spec)}


# ---------------------------------------------------------------- the dashboard
def _sql(spec):
    s, short = spec["schema"], "substring_index({}, '.', -2)"
    return {
        "latest": ("Latest results", f"SELECT {short.format('table_name')} AS table_name, substring_index(rule_id, '.', -1) AS rule, "
                   f"rule_id, rule_type, column_name, severity, description, total_rows, failed_rows, "
                   f"CASE WHEN passed THEN 'pass' ELSE 'fail' END AS status, CASE WHEN passed THEN 0 ELSE 1 END AS failing, "
                   f"CASE WHEN NOT passed AND severity = 'critical' THEN 1 ELSE 0 END AS critical_failing, "
                   f"run_started_at FROM {s}.dq_latest ORDER BY failing DESC, severity, table_name, rule"),
        "history": ("History", f"SELECT u.started_at AS run_at, {short.format('r.table_name')} AS table_name, r.rule_id, "
                    f"r.severity, r.failed_rows, CASE WHEN r.passed THEN 0 ELSE 1 END AS failing FROM {s}.dq_results r "
                    f"JOIN {s}.dq_runs u ON r.check_run_id = u.check_run_id"),
        "runs": ("Check runs", f"SELECT started_at, finished_at, rules, failing, critical_failing, warning_failing "
                 f"FROM {s}.dq_run_summary ORDER BY started_at DESC"),
        "quarantine": ("Quarantined rows", f"SELECT q.checked_at, {short.format('q.table_name')} AS table_name, "
                       f"substring_index(q.rule_id, '.', -1) AS rule, r.severity, q.row_key, q.row_json "
                       f"FROM {s}.dq_quarantine q LEFT JOIN {s}.dq_rules r ON q.rule_id = r.rule_id ORDER BY q.checked_at DESC"),
        "freshness": ("Freshness", f"SELECT {short.format('table_name')} AS table_name, layer, last_data_change, age_hours, "
                      f"max_hours, CASE WHEN stale THEN 'stale' WHEN max_hours IS NULL THEN 'no limit' ELSE 'fresh' END AS status, "
                      f"CASE WHEN stale THEN 1 ELSE 0 END AS is_stale, run_started_at FROM {s}.dq_freshness_latest "
                      f"ORDER BY is_stale DESC, layer, table_name"),
    }


def _chart(name, ds, kind, x, y_expr, y_name, title, color=None, pos=(0, 0, 3, 6)):
    fields = [{"name": x[0], "expression": f"`{x[0]}`"}, {"name": y_name, "expression": y_expr}]
    enc = {"x": {"fieldName": x[0], "scale": {"type": x[1]}, "displayName": x[2]},
           "y": {"fieldName": y_name, "scale": {"type": "quantitative"}, "displayName": title.split(" by ")[0]}}
    if color:
        fields.append({"name": color[0], "expression": f"`{color[0]}`"})
        enc["color"] = {"fieldName": color[0], "scale": {"type": "categorical"}, "displayName": color[1]}
    return {"widget": {"name": name, "queries": [{"name": "main_query", "query": {"datasetName": ds, "fields": fields,
                                                                                 "disaggregated": False}}],
                       "spec": {"version": 3, "widgetType": kind, "encodings": enc,
                                "frame": {"showTitle": True, "title": title}}},
            "position": dict(zip(("x", "y", "width", "height"), pos))}


def lakeview(spec) -> dict:
    p = spec["project"]
    latest = [
        _text("latest__header", f"## Latest results\nEvery data quality rule of {p}, as checked by the latest run of "
                                f"the check job. Rules come from the rules file, the keys in Unity Catalog and "
                                f"suggestions the data owner approved.", 0, 0, 6, 2),
        _filter("latest__f_table", "table_name", "Table", ["latest"], "filter-multi-select", 0, 2, 2, 1),
        _filter("latest__f_severity", "severity", "Severity", ["latest"], "filter-multi-select", 2, 2, 2, 1),
        _counter("latest__rules", "latest", "COUNT(`rule_id`)", "Rules checked", 0, 3, 2, 3),
        _counter("latest__failing", "latest", "SUM(`failing`)", "Rules failing", 2, 3, 1, 3),
        _counter("latest__critical", "latest", "SUM(`critical_failing`)", "Critical rules failing", 3, 3, 1, 3),
        _counter("latest__stale", "freshness", "SUM(`is_stale`)", "Stale tables", 4, 3, 2, 3),
        _chart("latest__by_table", "latest", "bar", ("table_name", "categorical", "Table"), "COUNT(`rule_id`)",
               "count(rule_id)", "Rules by table", ("status", "Status"), (0, 6, 6, 6)),
        _table("latest__results", "latest", [("table_name", "Table"), ("rule", "Rule"), ("description", "Checks that"),
                                             ("severity", "Severity"), ("status", "Status"), ("failed_rows", "Failed rows"),
                                             ("total_rows", "Rows checked"), ("run_started_at", "Checked")],
               "Rule results", 0, 12, 6, 9),
    ]
    history = [
        _text("history__header", "## History\nFailing rules per check run, by severity, and every run of the check job.",
              0, 0, 6, 2),
        _filter("history__f_table", "table_name", "Table", ["history"], "filter-multi-select", 0, 2, 2, 1),
        _filter("history__f_run", "run_at", "Run", ["history"], "filter-date-range-picker", 2, 2, 2, 1),
        _chart("history__trend", "history", "line", ("run_at", "temporal", "Run"), "SUM(`failing`)", "sum(failing)",
               "Rules failing by run", ("severity", "Severity"), (0, 3, 6, 6)),
        _table("history__runs", "runs", [("started_at", "Started"), ("finished_at", "Finished"), ("rules", "Rules"),
                                         ("failing", "Failing"), ("critical_failing", "Critical failing"),
                                         ("warning_failing", "Warnings failing")], "Check runs", 0, 9, 6, 7),
    ]
    quarantine = [
        _text("quarantine__header", "## Quarantine\nA sample of the rows that broke a rule in each run: their key and "
                                    "their non-sensitive columns (columns classified as sensitive are never copied).",
              0, 0, 6, 2),
        _filter("quarantine__f_table", "table_name", "Table", ["quarantine"], "filter-multi-select", 0, 2, 2, 1),
        _filter("quarantine__f_rule", "rule", "Rule", ["quarantine"], "filter-multi-select", 2, 2, 2, 1),
        _filter("quarantine__f_at", "checked_at", "Checked", ["quarantine"], "filter-date-range-picker", 4, 2, 2, 1),
        _table("quarantine__rows", "quarantine", [("checked_at", "Checked"), ("table_name", "Table"), ("rule", "Rule"),
                                                  ("severity", "Severity"), ("row_key", "Row key"), ("row_json", "Row")],
               "Quarantined rows", 0, 3, 6, 10),
    ]
    freshness = [
        _text("freshness__header", "## Freshness\nWhen each monitored table last changed (its newest data-writing "
                                   "operation), against its freshness limit.", 0, 0, 6, 2),
        _filter("freshness__f_layer", "layer", "Layer", ["freshness"], "filter-multi-select", 0, 2, 2, 1),
        _counter("freshness__stale", "freshness", "SUM(`is_stale`)", "Stale tables", 0, 3, 3, 3),
        _counter("freshness__tables", "freshness", "COUNT(`table_name`)", "Tables monitored", 3, 3, 3, 3),
        _table("freshness__tables_t", "freshness", [("table_name", "Table"), ("layer", "Layer"), ("status", "Status"),
                                                    ("last_data_change", "Last data change"), ("age_hours", "Age (hours)"),
                                                    ("max_hours", "Limit (hours)"), ("run_started_at", "Checked")],
               "Tables", 0, 6, 6, 8),
    ]
    pages = [("latest", "Latest results", latest), ("history", "History", history),
             ("quarantine", "Quarantine", quarantine), ("freshness", "Freshness", freshness)]
    return {"datasets": [{"name": k, "displayName": v[0], "queryLines": [v[1]]} for k, v in _sql(spec).items()],
            "pages": [{"name": n, "displayName": d, "pageType": "PAGE_TYPE_CANVAS", "layout": layout} for n, d, layout in pages]}


def dashboard_declaration(spec) -> dict:
    d = spec["dashboard"]
    return {"dashboards": [{"marker": spec["marker"], "title": d["title"], "parent_path": d["parent_path"],
                            "serialized_dashboard": lakeview(spec), "embed_credentials": d["credentials"] == "embedded",
                            "schedule": None, "subscribers": [],
                            "permissions": [{k: v for k, v in p.items()} for p in spec["readers"]]}]}


def bundle_content(spec) -> dict:
    """{"files", "jobs", "resources", "catalogs"} for bundle.write / write_resources (and BUNDLE_EXPORT)."""
    return {"files": {"10_schema/quality_schema.sql": schema_statements(spec), DASHBOARD: dashboard_declaration(spec)},
            "jobs": job_files(spec, spec["marker"]), "resources": resources(spec), "catalogs": catalogs(spec)}


def write(ctx, spec) -> list[str]:
    content = bundle_content(spec)
    base = bundle.scripts_dir(ctx.system, ctx.goal.id)
    stale = [str(p.relative_to(base)) for p in bundle.script_files(base) if str(p.relative_to(base)) not in content["files"]]
    written = bundle.write(ctx, content["files"], catalogs=content["catalogs"], remove=stale)
    jbase = bundle.jobs_dir(ctx.system, ctx.goal.id)
    jstale = [str(p.relative_to(jbase)) for p in bundle.script_files(jbase) if str(p.relative_to(jbase)) not in content["jobs"]]
    bundle.write(ctx, content["jobs"], catalogs=content["catalogs"], remove=jstale, jobs=True)
    bundle.write_resources(ctx, content["resources"], catalogs=content["catalogs"])
    return written
