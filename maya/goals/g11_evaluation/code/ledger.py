"""G11 ledger: the evaluation plan each certified run approved, with its results. Only rows of certified G11 runs are
trusted, so an incremental run asks eval_designer again only when the questions or the agents changed."""
import json

from maya.core.workspace import lit

COLS = "system STRING, run_id STRING, plan_json STRING, results_json STRING, recorded_at TIMESTAMP"


def table(ctx):
    return ctx.state.t("eval_ledger")


def record(ctx, spec, results):
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES ({lit(ctx.system.name)}, {lit(ctx.run_id)}, "
               f"{lit(json.dumps(spec, default=str))}, {lit(json.dumps(results, default=str))}, current_timestamp())")


def certified_plan(ctx) -> dict | None:
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT max_by(plan_json, recorded_at) AS p FROM {table(ctx)}
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G11')""")
    return json.loads(rows[0]["p"]) if rows and rows[0]["p"] else None


def drift(ctx):
    """Questions or settings changed since certification; the agents serve another configuration; the latest
    regression run failed."""
    from .common import certified_agents, q, table as tname
    from .dataset import load_inputs_hash
    cert = certified_plan(ctx)
    if not cert:
        return []
    reasons = []
    now = load_inputs_hash(ctx)
    if now is None:
        reasons.append(f"questions file {ctx.inputs['questions']} is missing or invalid")
    elif now != cert.get("inputs_hash"):
        reasons.append("questions or evaluation settings changed since certification")
    agents = certified_agents(ctx) or {}
    if "agent" in cert["targets"] and agents.get("version") != cert.get("agent_version"):
        reasons.append(f"the agents changed since certification (configuration {agents.get('version')})")
    try:
        rows = ctx.ws.sql(f"""SELECT target, pass_rate, threshold, passed, run_at FROM {q(tname(cert, 'eval_runs'))}
            WHERE run_at = (SELECT max(run_at) FROM {q(tname(cert, 'eval_runs'))})""")
        reasons += [f"the latest regression run ({r['run_at']}) failed for {r['target']}: pass rate {float(r['pass_rate']):.0%}, "
                    f"threshold {float(r['threshold']):.0%}" for r in rows if str(r["passed"]).lower() != "true"]
    except Exception:
        reasons.append(f"evaluation results table {tname(cert, 'eval_runs')} cannot be read")
    return reasons
