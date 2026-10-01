"""Project bootstrap. Each MAYA project owns its state schema (target.state_schema) and its dashboard, so projects
running different MAYA versions never touch each other's tables.

On every command: the state schema is checked and brought to this MAYA's STATE_VERSION (additive only: missing
tables and columns are created, nothing is dropped or rewritten; each table's Delta version is recorded first as a
restore point), and the project dashboard is reused when it exists and is current, updated in place when an older
MAYA built it, and created when it is missing. A state schema written by a newer MAYA is refused, not downgraded."""
import importlib
import json
import re

from maya import __version__

from .workspace import SqlError, lit

STATE_VERSION = 2          # 1: shared state schema without version tracking; 2: one state schema per project
DASHBOARD_VERSION = 1
META = "key STRING, value STRING, updated_at TIMESTAMP"
HISTORY = ("state_version INT, from_version INT, maya_version STRING, applied_by STRING, applied_at TIMESTAMP, "
           "restore_points STRING, statements STRING")
# Version steps that are more than "add missing tables / columns" go here: {version: fn(ws, schema) -> [sql]}
STEPS = {}


class ProjectError(Exception):
    pass


def _cols(decl: str) -> dict:
    return dict(re.match(r"\s*(\w+)\s+(.+?)\s*$", c).groups() for c in decl.split(","))


_ALIASES = {"BIGINT": "LONG", "INTEGER": "INT", "SMALLINT": "SHORT", "TINYINT": "BYTE", "REAL": "FLOAT"}


def _base(typ):
    t = typ.split("(")[0].strip().upper()
    return _ALIASES.get(t, t)


def tables(goals) -> dict:
    """Every state table: core ones plus those the goals declare (code.STATE_TABLES)."""
    from maya.status.publish import OVERVIEW, SYSTEMS
    from .state import TABLES
    out = {**TABLES, "systems": SYSTEMS, "goal_overview": OVERVIEW}
    for g in goals.values():
        try:
            out.update(getattr(importlib.import_module(f"{g.package}.code"), "STATE_TABLES", {}))
        except ImportError:
            pass
    return out


def _meta(ws, schema) -> dict:
    try:
        return {r["key"]: r["value"] for r in ws.sql(f"SELECT key, value FROM {schema}._maya_meta")}
    except SqlError:
        return {}


def set_meta(ws, schema, values: dict):
    for k, v in values.items():
        ws.sql(f"""MERGE INTO {schema}._maya_meta t USING (SELECT {lit(k)} AS key) s ON t.key = s.key
            WHEN MATCHED THEN UPDATE SET value = {lit(str(v))}, updated_at = current_timestamp()
            WHEN NOT MATCHED THEN INSERT (key, value, updated_at) VALUES ({lit(k)}, {lit(str(v))}, current_timestamp())""")


def ensure_state(ws, schema, project, goals) -> dict:
    catalog, name = schema.split(".")
    ws.sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    live = {}
    for r in ws.sql(f"SELECT table_name, column_name, data_type FROM {catalog}.information_schema.columns "
                    f"WHERE table_schema = {lit(name)}"):
        live.setdefault(r["table_name"], {})[r["column_name"]] = r["data_type"]
    meta = _meta(ws, schema) if "_maya_meta" in live else {}
    owner = meta.get("project")
    version = int(meta.get("state_version", 0)) if meta else (1 if live else 0)
    if owner and owner != project:
        raise ProjectError(f"state schema {schema} belongs to project {owner!r}; give {project!r} its own "
                           f"target.state_schema")
    if version > STATE_VERSION:
        raise ProjectError(f"state schema {schema} is at version {version} (written by MAYA {meta.get('maya_version')}); "
                           f"this MAYA {__version__} supports up to {STATE_VERSION}. Upgrade MAYA for this project.")
    if not owner and "goal_runs" in live:
        others = {r["system"] for r in ws.sql(f"SELECT DISTINCT system FROM {schema}.goal_runs")} - {project}
        if others:
            raise ProjectError(f"state schema {schema} holds state of other projects {sorted(others)}; give {project!r} "
                               f"its own target.state_schema and copy its history with "
                               f"`maya migrate --from {schema}`")
    wanted = tables(goals)
    stmts, warnings = [], []
    for t, decl in wanted.items():
        cols = _cols(decl)
        if t not in live:
            stmts.append(f"CREATE TABLE IF NOT EXISTS {schema}.{t} ({decl})")
            continue
        missing = [f"{c} {typ}" for c, typ in cols.items() if c not in live[t]]
        if missing:
            stmts.append(f"ALTER TABLE {schema}.{t} ADD COLUMNS ({', '.join(missing)})")
        for c, typ in cols.items():
            if c in live[t] and _base(live[t][c]) != _base(typ):
                warnings.append(f"{t}.{c} is {live[t][c]}, MAYA {__version__} declares {typ}; left unchanged")
    for v in range(version + 1, STATE_VERSION + 1):
        stmts += STEPS.get(v, lambda ws, schema: [])(ws, schema)
    if "_maya_meta" not in live:
        stmts.insert(0, f"CREATE TABLE IF NOT EXISTS {schema}._maya_meta ({META})")
    if "_maya_migrations" not in live:
        stmts.insert(0, f"CREATE TABLE IF NOT EXISTS {schema}._maya_migrations ({HISTORY})")
    changed = bool(stmts) or version != STATE_VERSION
    restore = {}
    if changed:
        for t in live:
            if not t.startswith("_maya"):
                try:
                    restore[t] = ws.sql(f"DESCRIBE HISTORY {schema}.{t} LIMIT 1")[0]["version"]
                except (SqlError, IndexError):
                    pass
        for s in stmts:
            ws.sql(s)
        ws.sql(f"INSERT INTO {schema}._maya_migrations VALUES ({STATE_VERSION}, {version}, {lit(__version__)}, "
               f"{lit(ws.user)}, current_timestamp(), {lit(json.dumps(restore))}, {lit(json.dumps(stmts))})")
    want = {"project": project, "state_version": str(STATE_VERSION), "maya_version": __version__}
    set_meta(ws, schema, {k: v for k, v in want.items() if meta.get(k) != v})
    return {"schema": schema, "from_version": version, "version": STATE_VERSION, "statements": stmts,
            "restore_points": restore, "warnings": warnings}


def ensure_dashboard(ws, system, warehouse_id) -> dict:
    """Reuse the project's dashboard when it exists and is current; update in place if older; create if missing."""
    from maya.status.dashboard import build_project, publish_dashboard
    schema = system.state_schema
    meta = _meta(ws, schema)
    did, d = meta.get("dashboard_id"), None
    if did:
        try:
            d = ws.client.lakeview.get(did)
            if d.lifecycle_state and d.lifecycle_state.value == "TRASHED":
                d = None
        except Exception:
            d = None
    current = d is not None and meta.get("dashboard_version") == str(DASHBOARD_VERSION)
    if current:
        return {"action": "reused", "dashboard_id": did, "url": meta.get("dashboard_url")}
    r = publish_dashboard(ws, build_project(schema, system.name), warehouse_id, f"MAYA - {system.name}",
                          system.spec["target"].get("dashboard_path") or f"/Users/{ws.user}/MAYA/{system.name}",
                          dashboard_id=did if d else None)
    set_meta(ws, schema, {"dashboard_id": r["dashboard_id"], "dashboard_url": r["url"], "dashboard_path": r["path"],
                          "dashboard_version": DASHBOARD_VERSION})
    return {"action": "updated" if d else "created", **r}


def migrate_from(ws, source, target, project, goals) -> dict:
    """Copy one project's history out of a shared (pre-version-2) state schema. The source is not modified."""
    live_target = {r["table_name"] for r in ws.sql(
        f"SELECT table_name FROM {target.split('.')[0]}.information_schema.tables WHERE table_schema = {lit(target.split('.')[1])}")}
    src_cols = {}
    for r in ws.sql(f"SELECT table_name, column_name FROM {source.split('.')[0]}.information_schema.columns "
                    f"WHERE table_schema = {lit(source.split('.')[1])}"):
        src_cols.setdefault(r["table_name"], []).append(r["column_name"])
    copied = {}
    for t, decl in tables(goals).items():
        if t not in src_cols or t not in live_target:
            continue
        if ws.sql(f"SELECT 1 FROM {target}.{t} LIMIT 1"):
            raise ProjectError(f"{target}.{t} already has rows; migrate only into a fresh state schema")
        cols = [c for c in _cols(decl) if c in src_cols[t]]
        if "system" in src_cols[t]:
            where = f"system = {lit(project)}"
        elif "run_id" in src_cols[t]:
            where = f"run_id IN (SELECT run_id FROM {source}.goal_runs WHERE system = {lit(project)})"
        else:
            continue
        ws.sql(f"INSERT INTO {target}.{t} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM {source}.{t} WHERE {where}")
        copied[t] = int(ws.sql(f"SELECT count(*) AS n FROM {target}.{t}")[0]["n"])
    set_meta(ws, target, {"migrated_from": source})
    return copied
