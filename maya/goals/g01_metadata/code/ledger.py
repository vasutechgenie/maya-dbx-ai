"""G1 ledger: the proposal applied to each asset and the asset's shape at the time. Only rows written by a
certified G1 run are trusted, so an incremental run reuses exactly what was certified."""
import json

from maya.core.spec import stable_hash
from maya.core.workspace import lit

COLS = "system STRING, run_id STRING, full_name STRING, fingerprint STRING, proposal_json STRING, recorded_at TIMESTAMP"


def table(ctx):
    return ctx.state.t("metadata_ledger")


def fingerprint(asset) -> str:
    return stable_hash({"columns": [(c["name"], c["type"]) for c in asset["columns"]],
                        "view": asset.get("view_definition")})


def record(ctx, props, fingerprints):
    if not props:
        return
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ", ".join("(" + ", ".join([lit(ctx.system.name), lit(ctx.run_id), lit(p["full_name"]),
                                      lit(fingerprints[p["full_name"]]), lit(json.dumps(p, default=str)),
                                      "current_timestamp()"]) + ")" for p in props)
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES {rows}")


def drift(ctx):
    """Certified assets whose description has since disappeared from Unity Catalog (e.g. a table was replaced)."""
    from maya.core import catalog
    live = {t["full_name"]: t.get("comment") for t in catalog.tables(ctx.ws, ctx.system)}
    lost = sorted(n for n in certified(ctx) if n in live and not live[n])
    return [f"metadata lost in Unity Catalog: {', '.join(lost)}"] if lost else []


def certified(ctx) -> dict:
    """full_name -> {fingerprint, proposal} from the latest certified G1 run that covered each asset."""
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT full_name, max_by(fingerprint, recorded_at) AS fingerprint,
            max_by(proposal_json, recorded_at) AS proposal_json
        FROM {table(ctx)} l
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G1')
        GROUP BY full_name""")
    return {r["full_name"]: {"fingerprint": r["fingerprint"], "proposal": json.loads(r["proposal_json"])} for r in rows}
