"""G6 ledger: the dashboard each certified run approved, with its tile results. Only rows of certified G6 runs are
trusted, so an incremental run re-authors only pages whose inputs changed since certification."""
import json

from maya.core.spec import stable_hash
from maya.core.workspace import lit

COLS = ("system STRING, run_id STRING, marker STRING, dashboard_id STRING, definition_hash STRING, definition_json STRING, "
        "tiles_json STRING, recorded_at TIMESTAMP")


def table(ctx):
    return ctx.state.t("dashboard_ledger")


def definition_hash(spec) -> str:
    from .apply import declaration
    return stable_hash(declaration(spec))


def record(ctx, spec, tiles):
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES ({lit(ctx.system.name)}, {lit(ctx.run_id)}, {lit(spec['marker'])}, "
               f"{lit(tiles.get('dashboard_id'))}, {lit(definition_hash(spec))}, {lit(json.dumps(spec, default=str))}, "
               f"{lit(json.dumps(tiles, default=str))}, current_timestamp())")


def certified_dashboard(ctx) -> dict | None:
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT max_by(definition_json, recorded_at) AS d FROM {table(ctx)}
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G6')""")
    return json.loads(rows[0]["d"]) if rows and rows[0]["d"] else None


def certified_authoring(ctx) -> dict:
    return (certified_dashboard(ctx) or {}).get("authoring") or {}


def drift(ctx):
    """Page content, metric views or semantic pages changed since certification; the dashboard missing, edited
    outside MAYA or with unpublished changes."""
    from .common import differences, find_dashboard, live_dashboard, published, serialized
    cert = certified_dashboard(ctx)
    if not cert:
        return []
    reasons = []
    if ctx.inputs.get("content") and not (ctx.system.base_dir / ctx.inputs["content"]).exists():
        reasons.append(f"content file {ctx.inputs['content']} is missing")
    sid = find_dashboard(ctx, cert)
    if not sid:
        return reasons + ["the certified dashboard no longer exists"]
    live = live_dashboard(ctx, sid)
    diffs = differences(serialized(cert), live["serialized"])
    if diffs:
        reasons.append(f"dashboard changed outside MAYA ({len(diffs)}): {diffs[0]}")
    pub = published(ctx, sid)
    if not pub:
        reasons.append("the dashboard is no longer published")
    from .layout import load_inputs_hash
    now = load_inputs_hash(ctx)
    if now and now != cert.get("inputs_hash"):
        reasons.append("page content, metric views or semantic pages changed since certification")
    return reasons
