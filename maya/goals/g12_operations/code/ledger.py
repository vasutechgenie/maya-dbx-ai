"""G12 ledger: the operations plan (documents, monitoring dashboard) each certified run approved, with the review of
the production jobs. Only rows of certified G12 runs are trusted, so an incremental run asks tech_writer again only
when the facts about the data product changed."""
import json

from maya.core.workspace import lit

COLS = "system STRING, run_id STRING, plan_json STRING, review_json STRING, recorded_at TIMESTAMP"


def table(ctx):
    return ctx.state.t("operations_ledger")


def record(ctx, spec, review):
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES ({lit(ctx.system.name)}, {lit(ctx.run_id)}, "
               f"{lit(json.dumps(spec, default=str))}, {lit(json.dumps(review, default=str))}, current_timestamp())")


def certified_plan(ctx) -> dict | None:
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT max_by(plan_json, recorded_at) AS p FROM {table(ctx)}
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G12')""")
    return json.loads(rows[0]["p"]) if rows and rows[0]["p"] else None


def drift(ctx):
    """Settings changed since certification; a production job lost its schedule, retries or notifications; the
    monitoring dashboard is gone."""
    from .common import job_problems, production_jobs
    from .docs import load_inputs_hash
    cert = certified_plan(ctx)
    if not cert:
        return []
    reasons = []
    if load_inputs_hash(ctx) != cert.get("inputs_hash"):
        reasons.append("operations settings changed since certification")
    try:
        for j in production_jobs(ctx):
            reasons += [f"production job {j['name']}: {p}" for p in job_problems(j, cert["notify"], cert["min_retries"])]
    except Exception as e:
        reasons.append(f"production jobs cannot be read: {str(e)[:200]}")
    from maya.goals.g06_dashboards.code.common import find_dashboard
    if not find_dashboard(ctx, cert["dashboard"]):
        reasons.append(f"monitoring dashboard {cert['dashboard']['title']!r} no longer exists")
    return reasons
