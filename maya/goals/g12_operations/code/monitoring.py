"""G12 monitoring dashboard: one AI/BI dashboard over what the earlier goals record (evaluation runs, data quality and
freshness, the agents' inference table) and the production jobs' runs from the system tables."""
import re

from maya.core.workspace import ident, lit

DEV_PREFIX = re.compile(r"^\[[^\]]+\]\s*")


def _q(full_name) -> str:
    return ident(*full_name.split("."))


def inference_table(ctx, endpoint) -> str | None:
    """The agents endpoint's payload table (AI Gateway inference tables or the legacy auto capture)."""
    if not endpoint:
        return None
    try:
        e = ctx.ws.client.serving_endpoints.get(endpoint)
    except Exception:
        return None
    gw = getattr(e, "ai_gateway", None)
    it = getattr(gw, "inference_table_config", None) if gw else None
    if it and it.enabled and it.catalog_name and it.schema_name:
        return f"{it.catalog_name}.{it.schema_name}.{it.table_name_prefix or endpoint.replace('-', '_')}_payload"
    ac = getattr(e.config, "auto_capture_config", None) if e.config else None
    if ac and getattr(ac, "enabled", None) is not False and ac.catalog_name and ac.schema_name:
        name = ac.state.payload_table.name if ac.state and ac.state.payload_table else f"{ac.table_name_prefix}_payload"
        return f"{ac.catalog_name}.{ac.schema_name}.{name}"
    return None


def _job_names(jobs) -> list[str]:
    return sorted({DEV_PREFIX.sub("", j["name"]) for j in jobs if j.get("name") and not j.get("missing")})


def datasets(ctx, facts, jobs) -> list[dict]:
    out = []
    ev = facts["evaluation"]
    g11 = ctx.system.goal_settings("G11") or {}
    if g11:
        from maya.goals.g11_evaluation.code.common import schema_name
        es = schema_name(ctx, g11.get("schema", "maya_eval"))
        out.append({"name": "eval_runs", "title": "Evaluation pass rate", "kind": "line", "x": "run_at", "y": "pass_rate",
                    "agg": "AVG", "color": "target", "sql": f"SELECT run_at, target, pass_rate, threshold, trigger FROM {_q(es + '.eval_runs')}"})
        out.append({"name": "eval_failures", "title": "Questions answered wrongly in the latest evaluation", "kind": "table",
                    "columns": ["target", "question", "reason", "run_at"],
                    "sql": f"SELECT target, question, reason, run_at FROM {_q(es + '.eval_results')} WHERE NOT correct AND "
                           f"run_id IN (SELECT max_by(run_id, run_at) FROM {_q(es + '.eval_runs')} GROUP BY target)"})
    q = facts["quality"]
    if (ctx.system.goal_settings("G7") or {}):
        out.append({"name": "dq_failures", "title": "Failing data quality rules (latest check)", "kind": "table",
                    "columns": ["table_name", "rule_id", "severity", "failed_rows", "total_rows", "checked_at"],
                    "sql": f"SELECT table_name, rule_id, severity, failed_rows, total_rows, checked_at FROM "
                           f"{_q(q['schema'] + '.dq_latest')} WHERE NOT passed"})
        out.append({"name": "freshness", "title": "Freshness (hours since the last change)", "kind": "table",
                    "columns": ["table_name", "layer", "age_hours", "max_hours", "stale", "last_data_change"],
                    "sql": f"SELECT table_name, layer, round(age_hours, 1) AS age_hours, max_hours, stale, last_data_change "
                           f"FROM {_q(q['schema'] + '.dq_freshness_latest')}"})
    names = _job_names(jobs)
    if names:
        like = " OR ".join(f"name LIKE {lit('%' + n)}" for n in names)
        out.append({"name": "job_runs", "title": "Production job runs (30 days)", "kind": "bar", "x": "day", "y": "runs",
                    "agg": "SUM", "color": "result_state",
                    "sql": "WITH j AS (SELECT job_id, max_by(name, change_time) AS name FROM system.lakeflow.jobs "
                           f"WHERE {like} GROUP BY job_id) SELECT date(r.period_start_time) AS day, j.name AS job, "
                           "r.result_state, count(*) AS runs FROM system.lakeflow.job_run_timeline r JOIN j USING (job_id) "
                           "WHERE r.period_start_time >= current_date() - INTERVAL 30 DAYS AND r.result_state IS NOT NULL "
                           "GROUP BY ALL"})
    it = inference_table(ctx, facts["agents"].get("endpoint"))
    if it:
        out.append({"name": "agent_requests", "title": "Agent requests per day", "kind": "bar", "x": "day", "y": "requests",
                    "agg": "SUM", "color": "status",
                    "sql": f"SELECT date(request_time) AS day, CAST(status_code AS STRING) AS status, count(*) AS requests, "
                           f"avg(execution_duration_ms) AS avg_ms FROM {_q(it)} GROUP BY ALL"})
        out.append({"name": "agent_latency", "title": "Agent response time (average milliseconds per day)", "kind": "line",
                    "x": "day", "y": "avg_ms", "agg": "AVG",
                    "sql": f"SELECT date(request_time) AS day, avg(execution_duration_ms) AS avg_ms FROM {_q(it)} GROUP BY ALL"})
    return out


def dashboard_spec(ctx, facts, jobs) -> dict:
    d = ctx.inputs.get("dashboard") or {}
    return {"title": d.get("title", "Operations monitoring"), "parent_path": d.get("parent_path", "MAYA"),
            "marker": f"maya:{ctx.system.name}:G12", "viewers": list(d.get("viewers") or []),
            "inference_table": inference_table(ctx, facts["agents"].get("endpoint")),
            "datasets": datasets(ctx, facts, jobs)}


# ---------------------------------------------------------------- AI/BI serialization
def _widget(ds):
    if ds["kind"] == "table":
        fields = [{"name": c, "expression": f"`{c}`"} for c in ds["columns"]]
        spec = {"version": 2, "widgetType": "table", "frame": {"showTitle": True, "title": ds["title"]},
                "encodings": {"columns": [{"fieldName": c, "displayName": c.replace("_", " ")} for c in ds["columns"]]}}
        return {"name": f"w_{ds['name']}", "queries": [{"name": "main_query", "query": {
            "datasetName": ds["name"], "fields": fields, "disaggregated": True}}], "spec": spec}
    y = f"{ds['agg'].lower()}({ds['y']})"
    fields = [{"name": ds["x"], "expression": f"`{ds['x']}`"}, {"name": y, "expression": f"{ds['agg']}(`{ds['y']}`)"}]
    enc = {"x": {"fieldName": ds["x"], "scale": {"type": "temporal"}, "displayName": ds["x"].replace("_", " ")},
           "y": {"fieldName": y, "scale": {"type": "quantitative"}, "displayName": ds["y"].replace("_", " ")}}
    if ds.get("color"):
        fields.append({"name": ds["color"], "expression": f"`{ds['color']}`"})
        enc["color"] = {"fieldName": ds["color"], "scale": {"type": "categorical"}, "displayName": ds["color"].replace("_", " ")}
    return {"name": f"w_{ds['name']}", "queries": [{"name": "main_query", "query": {
        "datasetName": ds["name"], "fields": fields, "disaggregated": False}}],
            "spec": {"version": 3, "widgetType": ds["kind"], "frame": {"showTitle": True, "title": ds["title"]}, "encodings": enc}}


def serialized(spec, system_name) -> dict:
    layout, y = [{"widget": {"name": "header", "multilineTextboxSpec": {"lines": [
        f"## {spec['title']}\n", f"Operations of {system_name}: evaluation, data quality, freshness, production jobs and "
                                 "agent traffic. Maintained by MAYA G12."]}}, "position": {"x": 0, "y": 0, "width": 6, "height": 2}}], 2
    charts = [d for d in spec["datasets"] if d["kind"] != "table"]
    tables = [d for d in spec["datasets"] if d["kind"] == "table"]
    for i in range(0, len(charts), 2):
        row = charts[i:i + 2]
        for k, d in enumerate(row):
            layout.append({"widget": _widget(d), "position": {"x": k * (6 // len(row)), "y": y, "width": 6 // len(row), "height": 6}})
        y += 6
    for d in tables:
        layout.append({"widget": _widget(d), "position": {"x": 0, "y": y, "width": 6, "height": 6}})
        y += 6
    return {"datasets": [{"name": d["name"], "displayName": d["title"], "queryLines": [d["sql"]]} for d in spec["datasets"]],
            "pages": [{"name": "operations", "displayName": "Operations", "pageType": "PAGE_TYPE_CANVAS", "layout": layout}]}
