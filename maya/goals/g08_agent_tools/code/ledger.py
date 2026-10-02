"""G8 ledger: the tool plan each certified run approved, with its tests. Only rows of certified G8 runs are trusted, so
an incremental run asks tool_smith again only for tools (or sources) that changed since certification."""
import json

from maya.core.workspace import lit

COLS = "system STRING, run_id STRING, plan_json STRING, tests_json STRING, recorded_at TIMESTAMP"


def table(ctx):
    return ctx.state.t("tools_ledger")


def record(ctx, spec, tests):
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES ({lit(ctx.system.name)}, {lit(ctx.run_id)}, "
               f"{lit(json.dumps(spec, default=str))}, {lit(json.dumps(tests, default=str))}, current_timestamp())")


def certified_plan(ctx) -> dict | None:
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT max_by(plan_json, recorded_at) AS p FROM {table(ctx)}
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G8')""")
    return json.loads(rows[0]["p"]) if rows and rows[0]["p"] else None


def drift(ctx):
    """Tools file or executors changed since certification; a tool function missing or changed outside MAYA."""
    from .common import _norm
    from .tools import load_inputs_hash
    cert = certified_plan(ctx)
    if not cert:
        return []
    reasons = []
    now = load_inputs_hash(ctx)
    if now is None:
        reasons.append(f"tools file {ctx.inputs['tools']} is missing or invalid")
    elif now != cert.get("inputs_hash"):
        reasons.append("tools file, tools schema or executors changed since certification")
    cat, sch = cert["schema"].split(".")
    try:
        live = {r["routine_name"]: r["routine_definition"] for r in ctx.ws.sql(
            f"SELECT routine_name, routine_definition FROM `{cat}`.information_schema.routines WHERE routine_schema = {lit(sch)}")}
    except Exception as e:
        return reasons + [f"cannot read the tools schema: {e}"]
    for t in cert["tools"]:
        if t["name"] not in live:
            reasons.append(f"tool function {t['full_name']} no longer exists")
        elif _norm(live[t["name"]] or "") != _norm(t["sql"]):
            reasons.append(f"tool function {t['full_name']} was changed outside MAYA")
    return reasons
