"""G4 ledger: the plan every certified run delivered. Grants, policies, masks and filters a certified plan delivered
and the current one no longer declares are removed; anything MAYA never delivered is left as it is."""
import json

from maya.core.workspace import lit

COLS = "system STRING, run_id STRING, plan_json STRING, recorded_at TIMESTAMP"


def table(ctx):
    return ctx.state.t("governance_ledger")


def record(ctx, plan):
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES ({lit(ctx.system.name)}, {lit(ctx.run_id)}, "
               f"{lit(json.dumps(plan, default=str))}, current_timestamp())")


def _plans(ctx) -> list[dict]:
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT plan_json FROM {table(ctx)}
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G4')
        ORDER BY recorded_at DESC""")
    return [json.loads(r["plan_json"]) for r in rows]


def certified(ctx) -> dict | None:
    plans = _plans(ctx)
    return plans[0] if plans else None


def delivered(ctx) -> dict:
    """Everything any certified plan delivered (or still had to remove), de-duplicated."""
    out = {"grants": [], "policies": [], "column_masks": [], "row_filters": []}
    for p in _plans(ctx):
        for k in out:
            for x in (p.get(k) or []) + ((p.get("removed") or {}).get(k) or []):
                if x not in out[k]:
                    out[k].append(x)
    return out


def drift(ctx):
    """Sensitive columns no longer masked, and declared controls or grants missing in Unity Catalog."""
    if not certified(ctx):
        return []
    from ..validator import checks
    reasons = []
    for name, label in (("unprotected_columns", "sensitive columns not masked as declared"),
                        ("policy_differences", "policies differ from the certified plan"),
                        ("column_mask_differences", "column masks differ from the certified plan"),
                        ("row_filter_differences", "row filters differ from the certified plan"),
                        ("missing_grants", "declared grants missing")):
        res = getattr(checks, name)(ctx)
        if res["observed"]:
            first = res["evidence"]["items"][0]
            reasons.append(f"{label} ({res['observed']}): {first.get('key') or first.get('problem')}")
    return reasons
