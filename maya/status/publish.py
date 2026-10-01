"""Publish a project's status: `systems` + `goal_overview` in the project's own state schema (read by its dashboard),
and a row in the optional workspace registry (read by the portfolio dashboard)."""
import json
import socket

from maya.core.workspace import lit

SYSTEMS = ("system STRING, owner STRING, description STRING, origin STRING, workspace STRING, catalogs STRING, "
           "foundation_json STRING, model STRING, spec_hash STRING, goals_total INT, goals_certified INT, "
           "pending_approvals INT, next_action STRING, registered_by STRING, registered_at TIMESTAMP, refreshed_at TIMESTAMP")
OVERVIEW = ("system STRING, goal_id STRING, goal_order INT, title STRING, milestone STRING, prerequisites STRING, "
            "status STRING, reason STRING, checks_passed INT, checks_total INT, mandatory_failing INT, "
            "certified_by STRING, certified_at TIMESTAMP, expires_at TIMESTAMP, certified_run_id STRING, "
            "last_run_id STRING, last_run_status STRING, last_run_started TIMESTAMP, last_run_ended TIMESTAMP, "
            "model STRING, config_hash STRING, refreshed_at TIMESTAMP")


# Workspace registry (optional target.registry schema, shared by projects of any MAYA version). This contract is
# frozen: later versions may only add columns, and writers fill only the columns the live table has.
REGISTRY = ("project STRING, owner STRING, description STRING, maya_version STRING, state_version INT, "
            "state_schema STRING, dashboard_url STRING, goals_total INT, goals_certified INT, pending_approvals INT, "
            "next_action STRING, origin STRING, refreshed_at TIMESTAMP")


def register(engine, report, dashboard_url=None) -> list[str]:
    """Upsert this project into the workspace registry, if one is configured. Returns warnings."""
    from maya import __version__
    from maya.core.project import STATE_VERSION
    reg = engine.system.spec["target"].get("registry")
    if not reg:
        return []
    ws, t = engine.ws, f"{reg}.projects"
    ws.sql(f"CREATE SCHEMA IF NOT EXISTS {reg}")
    ws.sql(f"CREATE TABLE IF NOT EXISTS {t} ({REGISTRY})")
    have = {r["col_name"] for r in ws.sql(f"DESCRIBE TABLE {t}")}
    meta = engine.system.spec["metadata"]
    goals = report["goals"]
    values = {"project": lit(engine.system.name), "owner": lit(meta.get("owner")), "description": lit(meta.get("description")),
              "maya_version": lit(__version__), "state_version": str(STATE_VERSION), "state_schema": lit(engine.state.schema),
              "dashboard_url": lit(dashboard_url), "goals_total": str(len(goals)),
              "goals_certified": str(sum(g["status"] == "certified" for g in goals)),
              "pending_approvals": str(len(report["pending_approvals"])),
              "next_action": lit((report["next_actions"] or [None])[0]), "origin": lit(origin(engine)),
              "refreshed_at": "current_timestamp()"}
    values = {k: v for k, v in values.items() if k in have}
    if dashboard_url is None:
        values.pop("dashboard_url", None)
    prev = ws.sql(f"SELECT state_schema FROM {t} WHERE project = {values['project']}") if "state_schema" in have else []
    if prev and prev[0]["state_schema"] not in (None, engine.state.schema):
        return [f"project name {engine.system.name!r} is already registered in {t} for state schema "
                f"{prev[0]['state_schema']}; project names must be unique per workspace (registry not updated)"]
    sets = ", ".join(f"{k} = {v}" for k, v in values.items() if k != "project")
    ws.sql(f"""MERGE INTO {t} r USING (SELECT {values['project']} AS project) s ON r.project = s.project
        WHEN MATCHED THEN UPDATE SET {sets}
        WHEN NOT MATCHED THEN INSERT ({', '.join(values)}) VALUES ({', '.join(values.values())})""")
    return []


def origin(engine) -> str:
    return f"{socket.gethostname()}:{engine.system.path}"


def _ts(v):
    return f"timestamp{lit(str(v))}" if v else "NULL"


def publish(engine, report) -> list[str]:
    """Upsert the project into `systems` and replace its rows in `goal_overview`. Returns warnings."""
    st, ws, name = engine.state, engine.ws, engine.system.name
    t_sys, t_goals = st.t("systems"), st.t("goal_overview")
    warnings = []
    meta = engine.system.spec["metadata"]
    goals = report["goals"]
    certified = sum(g["status"] == "certified" for g in goals)
    nxt = (report["next_actions"] or [None])[0]
    ws.sql(f"""MERGE INTO {t_sys} t USING (SELECT {lit(name)} AS system) s ON t.system = s.system
        WHEN MATCHED THEN UPDATE SET owner = {lit(meta.get('owner'))}, description = {lit(meta.get('description'))},
             origin = {lit(origin(engine))}, workspace = {lit(report['workspace'])}, catalogs = {lit(', '.join(report['catalogs']))},
             foundation_json = {lit(json.dumps(report['foundation']))}, model = {lit(report['ai_gateway_model'])},
             spec_hash = {lit(report['spec_hash'])}, goals_total = {len(goals)}, goals_certified = {certified},
             pending_approvals = {len(report['pending_approvals'])}, next_action = {lit(nxt)}, refreshed_at = current_timestamp()
        WHEN NOT MATCHED THEN INSERT (system, owner, description, origin, workspace, catalogs, foundation_json, model, spec_hash,
             goals_total, goals_certified, pending_approvals, next_action, registered_by, registered_at, refreshed_at)
        VALUES ({lit(name)}, {lit(meta.get('owner'))}, {lit(meta.get('description'))}, {lit(origin(engine))},
             {lit(report['workspace'])}, {lit(', '.join(report['catalogs']))}, {lit(json.dumps(report['foundation']))},
             {lit(report['ai_gateway_model'])}, {lit(report['spec_hash'])}, {len(goals)}, {certified},
             {len(report['pending_approvals'])}, {lit(nxt)}, {lit(ws.user)}, current_timestamp(), current_timestamp())""")
    rows = []
    for g in goals:
        cert, run = g["certification"] or {}, g["last_run"] or {}
        reason = g["detail"] or "; ".join(g["stale_reasons"]) or (
            f"needs {', '.join(g['missing_prerequisites'])}" if g["missing_prerequisites"] else None)
        rows.append("(" + ", ".join([
            lit(name), lit(g["id"]), str(int(g["id"][1:])), lit(g["title"]), lit(g["milestone"]),
            lit(", ".join(g["prerequisites"])), lit(g["status"]), lit(reason),
            str(sum(c["passed"] for c in g["checks"])), str(len(g["checks"])),
            str(sum(1 for c in g["checks"] if c["severity"] == "mandatory" and not c["passed"])),
            lit(cert.get("certified_by")), _ts(cert.get("certified_at")), _ts(cert.get("expires_at")), lit(cert.get("run_id")),
            lit(run.get("run_id")), lit(run.get("status")), _ts(run.get("started_at")), _ts(run.get("ended_at")),
            lit(g["model"]), lit(g["config_hash"]), "current_timestamp()"]) + ")")
    ws.sql(f"DELETE FROM {t_goals} WHERE system = {lit(name)}")
    if rows:
        ws.sql(f"INSERT INTO {t_goals} VALUES {', '.join(rows)}")
    return warnings
