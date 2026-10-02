"""G7 ledger: the quality plan each certified run approved, with its alert tests. Only rows of certified G7 runs are
trusted, so an incremental run asks the agent again only for tables that changed since certification."""
import json

from maya.core.workspace import lit

COLS = "system STRING, run_id STRING, marker STRING, plan_json STRING, alert_tests_json STRING, recorded_at TIMESTAMP"


def table(ctx):
    return ctx.state.t("quality_ledger")


def record(ctx, spec, tests):
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES ({lit(ctx.system.name)}, {lit(ctx.run_id)}, {lit(spec['marker'])}, "
               f"{lit(json.dumps(spec, default=str))}, {lit(json.dumps(tests, default=str))}, current_timestamp())")


def certified_plan(ctx) -> dict | None:
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT max_by(plan_json, recorded_at) AS p FROM {table(ctx)}
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G7')""")
    return json.loads(rows[0]["p"]) if rows and rows[0]["p"] else None


def certified_suggestions(ctx) -> dict:
    return (certified_plan(ctx) or {}).get("suggestions") or {}


def drift(ctx):
    """Rules file, monitored tables, keys or classifications changed since certification; the check job, an alert or
    the dashboard missing or changed outside MAYA."""
    from .common import ALERTS, JOB_KEY, alert_key, bundle_ids
    from .rules import load_inputs_hash
    from . import live
    cert = certified_plan(ctx)
    if not cert:
        return []
    reasons = []
    if ctx.inputs.get("rules") and not (ctx.system.base_dir / ctx.inputs["rules"]).exists():
        reasons.append(f"rules file {ctx.inputs['rules']} is missing")
    ids = bundle_ids(ctx)
    if not (ids.get("jobs") or {}).get(JOB_KEY) or not live.job(ctx, ids["jobs"][JOB_KEY]):
        reasons.append("the data quality check job no longer exists")
    for name in ALERTS:
        aid = (ids.get("alerts") or {}).get(alert_key(name))
        if not aid or not live.alert(ctx, aid):
            reasons.append(f"the {name} alert no longer exists")
    diffs = live.dashboard_differences(ctx, cert)
    if diffs:
        reasons.append(f"data quality dashboard: {diffs[0]}")
    now = load_inputs_hash(ctx)
    if now and now != cert.get("inputs_hash"):
        reasons.append("rules file, monitored tables, keys or sensitivity classes changed since certification")
    return reasons
