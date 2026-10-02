"""G9 ledger: the operations plan each certified run approved, with its MCP tests. Only rows of certified G9 runs are
trusted, so an incremental run asks tool_smith again only for operations whose declaration or notebook changed."""
import json

from maya.core.workspace import lit

COLS = "system STRING, run_id STRING, plan_json STRING, tests_json STRING, recorded_at TIMESTAMP"


def table(ctx):
    return ctx.state.t("ops_ledger")


def record(ctx, spec, tests):
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES ({lit(ctx.system.name)}, {lit(ctx.run_id)}, "
               f"{lit(json.dumps(spec, default=str))}, {lit(json.dumps(tests, default=str))}, current_timestamp())")


def certified_plan(ctx) -> dict | None:
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT max_by(plan_json, recorded_at) AS p FROM {table(ctx)}
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G9')""")
    return json.loads(rows[0]["p"]) if rows and rows[0]["p"] else None


def drift(ctx):
    """Operations, notebooks or clients changed since certification; a job or the app missing or stopped."""
    from maya.core import bundle
    from .common import APP_KEY
    from .operations import load_inputs_hash
    cert = certified_plan(ctx)
    if not cert:
        return []
    reasons = []
    now = load_inputs_hash(ctx)
    if now is None:
        reasons.append("the operations file or a notebook is missing or invalid")
    elif now != cert.get("inputs_hash"):
        reasons.append("operations, their notebooks or the clients changed since certification")
    res = bundle.resources(ctx.system, ctx.ws)
    for o in cert["operations"]:
        jid = ((res.get("jobs") or {}).get(o["job_key"]) or {}).get("id")
        try:
            ctx.ws.client.jobs.get(int(jid))
        except Exception:
            reasons.append(f"the job of operation {o['name']} no longer exists")
    name = ((res.get("apps") or {}).get(APP_KEY) or {}).get("name") or cert["app_name"]
    try:
        app = ctx.ws.client.apps.get(name)
        if not app.compute_status or app.compute_status.state.value != "ACTIVE":
            reasons.append(f"the MCP server app {name} is not running")
    except Exception:
        reasons.append(f"the MCP server app {name} no longer exists")
    return reasons
