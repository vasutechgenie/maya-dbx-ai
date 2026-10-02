"""G10 ledger: the agents plan each certified run approved, with its endpoint tests. Only rows of certified G10 runs
are trusted, so an incremental run asks agent_engineer again only when the agents file or the tool catalog changed."""
import json

from maya.core.workspace import lit

COLS = "system STRING, run_id STRING, plan_json STRING, tests_json STRING, recorded_at TIMESTAMP"


def table(ctx):
    return ctx.state.t("agents_ledger")


def record(ctx, spec, tests):
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES ({lit(ctx.system.name)}, {lit(ctx.run_id)}, "
               f"{lit(json.dumps(spec, default=str))}, {lit(json.dumps(tests, default=str))}, current_timestamp())")


def certified_plan(ctx) -> dict | None:
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT max_by(plan_json, recorded_at) AS p FROM {table(ctx)}
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G10')""")
    return json.loads(rows[0]["p"]) if rows and rows[0]["p"] else None


def drift(ctx):
    """Agents file or settings changed since certification; the endpoint missing, not ready or serving another
    configuration."""
    from .design import load_inputs_hash
    cert = certified_plan(ctx)
    if not cert:
        return []
    reasons = []
    now = load_inputs_hash(ctx)
    if now is None:
        reasons.append(f"agents file {ctx.inputs['agents']} is missing or invalid")
    elif now != cert.get("inputs_hash"):
        reasons.append("agents file or agent settings changed since certification")
    try:
        e = ctx.ws.client.serving_endpoints.get(cert["endpoint"])
        if not e.state or not e.state.ready or e.state.ready.value != "READY":
            reasons.append(f"endpoint {cert['endpoint']} is not ready")
        tags = {t.key: t.value for t in e.tags or []}
        if tags.get("maya_config_version") not in (None, cert["version"]):
            reasons.append(f"endpoint {cert['endpoint']} serves another configuration ({tags.get('maya_config_version')})")
    except Exception:
        reasons.append(f"endpoint {cert['endpoint']} no longer exists")
    return reasons
