"""AI/BI (Lakeview) dashboards.
  project dashboard  - one per project, over that project's own state schema: goals, checks, runs, approvals,
                       certifications, with a goal filter. Created / reused / upgraded by maya.core.project.
  portfolio          - optional, over the workspace registry (target.registry): every project, its MAYA version,
                       progress and the link to its own dashboard, with a project filter."""
import json


def _project_datasets(s, p):
    w = f"WHERE system = '{p}'"
    return {
        "goals": ("Goals", f"""SELECT goal_id AS goal, goal_order, title, status, reason, prerequisites,
            concat(checks_passed, ' / ', checks_total) AS checks, mandatory_failing, certified_by, certified_at, expires_at,
            last_run_id, last_run_status, last_run_started, last_run_ended, model, refreshed_at,
            CASE WHEN status = 'certified' THEN 1 ELSE 0 END AS is_certified,
            CASE WHEN status IN ('certified', 'not_configured') THEN 0 ELSE 1 END AS needs_attention
            FROM {s}.goal_overview {w}"""),
        "checks": ("Latest checks", f"""WITH latest AS (
              SELECT goal_id, max_by(run_id, checked_at) AS run_id FROM {s}.check_results {w} GROUP BY goal_id)
            SELECT c.goal_id AS goal, c.check_id AS check, c.checklist, c.severity,
              CASE WHEN c.passed THEN 'PASS' ELSE 'FAIL' END AS result, c.observed, c.expected, c.run_id, c.checked_at,
              CASE WHEN NOT c.passed AND c.severity = 'mandatory' THEN 1 ELSE 0 END AS mandatory_fail
            FROM {s}.check_results c JOIN latest l ON c.goal_id = l.goal_id AND c.run_id = l.run_id"""),
        "runs": ("Runs", f"""SELECT goal_id AS goal, run_id, status, started_at, ended_at,
            round((unix_timestamp(ended_at) - unix_timestamp(started_at)) / 60, 1) AS minutes FROM {s}.goal_runs {w}"""),
        "approvals": ("Approvals", f"""SELECT goal_id AS goal, gate, approver, status, decided_by, decided_at, note,
            created_at, run_id, CASE WHEN status = 'pending' THEN 1 ELSE 0 END AS is_pending FROM {s}.approvals {w}"""),
        "certs": ("Certifications", f"""SELECT goal_id AS goal, run_id, certified_by, certified_at, expires_at,
            CASE WHEN expires_at < current_timestamp() THEN 'expired' ELSE 'valid' END AS validity
            FROM {s}.certifications {w}"""),
        "project": ("Project", f"""SELECT system AS project, owner, description, catalogs, model, goals_certified,
            goals_total, pending_approvals, next_action, refreshed_at FROM {s}.systems {w}"""),
        "versions": ("MAYA versions", f"""SELECT state_version, from_version, maya_version, applied_by, applied_at
            FROM {s}._maya_migrations"""),
    }


def _text(name, md, x, y, w, h):
    return {"widget": {"name": name, "multilineTextboxSpec": {"lines": [md]}},
            "position": {"x": x, "y": y, "width": w, "height": h}}


def _counter(name, ds, expr, title, x, y, w=2, h=2):
    return {"widget": {"name": name, "queries": [{"name": "main_query", "query": {
                "datasetName": ds, "fields": [{"name": "value", "expression": expr}], "disaggregated": False}}],
            "spec": {"version": 2, "widgetType": "counter", "encodings": {"value": {"fieldName": "value", "displayName": title}},
                     "frame": {"showTitle": True, "title": title}}},
            "position": {"x": x, "y": y, "width": w, "height": h}}


def _table(name, ds, cols, title, x, y, w, h):
    return {"widget": {"name": name, "queries": [{"name": "main_query", "query": {
                "datasetName": ds, "fields": [{"name": c, "expression": f"`{c}`"} for c, _ in cols], "disaggregated": True}}],
            "spec": {"version": 2, "widgetType": "table", "frame": {"showTitle": True, "title": title}, "rowsPerPage": 25,
                     "encodings": {"columns": [{"fieldName": c, "displayName": d} for c, d in cols]}}},
            "position": {"x": x, "y": y, "width": w, "height": h}}


def _filter(name, field, title, datasets, kind, x, y, w=3, h=1):
    queries = [{"name": f"{name}_{ds}", "query": {"datasetName": ds, "fields": [{"name": field, "expression": f"`{field}`"}],
                                                  "disaggregated": False}} for ds in datasets]
    return {"widget": {"name": name, "queries": queries,
            "spec": {"version": 2, "widgetType": kind, "frame": {"showTitle": True, "title": title},
                     "encodings": {"fields": [{"fieldName": field, "displayName": title, "queryName": q["name"]} for q in queries]}}},
            "position": {"x": x, "y": y, "width": w, "height": h}}


def _datasets(d):
    return [{"name": k, "displayName": v[0], "queryLines": [v[1]]} for k, v in d.items()]


def build_project(state_schema, project) -> dict:
    goal_ds = ["goals", "checks", "runs", "approvals", "certs"]
    overview = [
        _text("t_title", f"## MAYA - {project}\nGoals of this project, from `{state_schema}`. Refreshed by "
                         "`maya status`, `maya run` and `maya review`.", 0, 0, 4, 2),
        _filter("f_goal", "goal", "Goal", goal_ds, "filter-multi-select", 4, 0, 2, 2),
        _counter("c_cert", "goals", "SUM(`is_certified`)", "Goals certified", 0, 2),
        _counter("c_attn", "goals", "SUM(`needs_attention`)", "Goals needing attention", 2, 2),
        _counter("c_fail", "checks", "SUM(`mandatory_fail`)", "Mandatory checks failing", 4, 2, 1, 2),
        _counter("c_pend", "approvals", "SUM(`is_pending`)", "Pending approvals", 5, 2, 1, 2),
        _table("tb_goals", "goals", [("goal", "Goal"), ("title", "Title"), ("status", "Status"), ("reason", "Why / next"),
                                     ("prerequisites", "Needs"), ("checks", "Checks passed"),
                                     ("certified_by", "Certified by"), ("expires_at", "Valid until"),
                                     ("last_run_status", "Last run"), ("last_run_ended", "Last run ended"),
                                     ("refreshed_at", "Refreshed")], "Goal status", 0, 4, 6, 5),
        _table("tb_checks", "checks", [("goal", "Goal"), ("check", "Check"), ("result", "Result"), ("severity", "Severity"),
                                       ("observed", "Observed"), ("expected", "Expected"), ("checklist", "Checklist item"),
                                       ("checked_at", "Checked")], "Latest checks", 0, 9, 6, 7),
    ]
    history = [
        _filter("f_goal_h", "goal", "Goal", goal_ds, "filter-multi-select", 0, 0, 2, 1),
        _table("tb_runs", "runs", [("goal", "Goal"), ("run_id", "Run"), ("status", "Status"), ("started_at", "Started"),
                                   ("ended_at", "Ended"), ("minutes", "Minutes")], "Runs", 0, 1, 3, 7),
        _table("tb_approvals", "approvals", [("goal", "Goal"), ("gate", "Gate"), ("status", "Status"),
                                             ("approver", "Approver"), ("decided_by", "Decided by"),
                                             ("decided_at", "Decided"), ("note", "Note")], "Approvals", 3, 1, 3, 7),
        _table("tb_certs", "certs", [("goal", "Goal"), ("run_id", "Run"), ("certified_by", "Certified by"),
                                     ("certified_at", "Certified"), ("expires_at", "Expires"), ("validity", "Validity")],
               "Certifications", 0, 8, 6, 6),
        _table("tb_project", "project", [("owner", "Owner"), ("catalogs", "Catalogs"), ("model", "AI Gateway model"),
                                         ("next_action", "Next action"), ("refreshed_at", "Refreshed")],
               "Project", 0, 14, 4, 3),
        _table("tb_versions", "versions", [("maya_version", "MAYA"), ("state_version", "State version"),
                                           ("from_version", "From"), ("applied_at", "Applied")],
               "State schema versions", 4, 14, 2, 3),
    ]
    return {"datasets": _datasets(_project_datasets(state_schema, project)), "pages": [
        {"name": "overview", "displayName": "Goals", "pageType": "PAGE_TYPE_CANVAS", "layout": overview},
        {"name": "history", "displayName": "History", "pageType": "PAGE_TYPE_CANVAS", "layout": history}]}


def build_portfolio(registry_table) -> dict:
    ds = {"projects": ("Projects", f"""SELECT project, owner, maya_version, state_version, state_schema,
        concat(goals_certified, ' / ', goals_total) AS certified, goals_certified, goals_total, pending_approvals,
        next_action, dashboard_url, refreshed_at FROM {registry_table}""")}
    layout = [
        _text("t_title", f"## MAYA portfolio\nEvery MAYA project registered in `{registry_table}`. Each project keeps its "
                         "own state schema and dashboard (link in the table), so projects can run different MAYA versions.",
              0, 0, 4, 2),
        _filter("f_project", "project", "Project", ["projects"], "filter-multi-select", 4, 0, 2, 2),
        _counter("c_projects", "projects", "COUNT(DISTINCT `project`)", "Projects", 0, 2),
        _counter("c_certified", "projects", "SUM(`goals_certified`)", "Goals certified", 2, 2),
        _counter("c_pending", "projects", "SUM(`pending_approvals`)", "Pending approvals", 4, 2),
        _table("tb_projects", "projects", [("project", "Project"), ("certified", "Goals certified"),
                                           ("pending_approvals", "Pending approvals"), ("next_action", "Next action"),
                                           ("maya_version", "MAYA"), ("owner", "Owner"), ("state_schema", "State schema"),
                                           ("dashboard_url", "Project dashboard"), ("refreshed_at", "Refreshed")],
               "Projects", 0, 4, 6, 8),
    ]
    return {"datasets": _datasets(ds), "pages": [
        {"name": "portfolio", "displayName": "Projects", "pageType": "PAGE_TYPE_CANVAS", "layout": layout}]}


def publish_dashboard(ws, spec, warehouse_id, name, parent, dashboard_id=None) -> dict:
    """Create (or update in place, keeping its id and URL) and publish a dashboard."""
    from databricks.sdk.service.dashboards import Dashboard
    ws.client.workspace.mkdirs(parent)
    body = Dashboard(display_name=name, parent_path=parent, warehouse_id=warehouse_id, serialized_dashboard=json.dumps(spec))
    if not dashboard_id:
        from databricks.sdk.errors import NotFound
        try:
            dashboard_id = ws.client.workspace.get_status(f"{parent}/{name}.lvdash.json").resource_id
        except NotFound:
            dashboard_id = None
    d = ws.client.lakeview.update(dashboard_id, body) if dashboard_id else ws.client.lakeview.create(body)
    ws.client.lakeview.publish(d.dashboard_id, embed_credentials=True, warehouse_id=warehouse_id)
    return {"dashboard_id": d.dashboard_id, "path": d.path, "url": f"{ws.host}/dashboardsv3/{d.dashboard_id}/published"}
