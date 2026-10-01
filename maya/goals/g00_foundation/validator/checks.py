"""G0 checks. Each returns {"observed": number, "evidence": {...}}; goal.yaml declares the expectation."""
from maya.core.workspace import lit

from ..code.intake import inventory_table


def _inv(ctx):
    return ctx.read_artefact("inventory.json") or []


def _assess(ctx):
    return ctx.read_artefact("assessment.json") or {}


def layers_populated(ctx):
    bad = [k for k, v in _assess(ctx).get("layers", {}).items() if v["below_minimum"]]
    return {"observed": len(bad), "evidence": {"items": bad}}


def assets_readable(ctx):
    items = _assess(ctx).get("unreadable", [])
    return {"observed": len(items), "evidence": {"items": items}}


def empty_assets(ctx):
    items = _assess(ctx).get("empty", [])
    return {"observed": len(items), "evidence": {"items": items}}


def stale_layers(ctx):
    layers = _assess(ctx).get("layers", {})
    bad = [k for k, v in layers.items() if v["stale"]]
    return {"observed": len(bad), "evidence": {"items": bad, "ages": {k: v["age_hours"] for k, v in layers.items()}}}


def inventory_recorded(ctx):
    """Re-read the state table (not the artefact) and diff it against discovery."""
    rows = ctx.ws.sql(f"SELECT full_name FROM {inventory_table(ctx)} WHERE system = {lit(ctx.system.name)} "
                      f"AND run_id = {lit(ctx.run_id)}")
    recorded, found = {r["full_name"] for r in rows}, {i["full_name"] for i in _inv(ctx)}
    diff = sorted(recorded ^ found)
    return {"observed": len(diff), "evidence": {"items": diff, "recorded": len(recorded), "discovered": len(found)}}


def attestation_complete(ctx):
    att = (ctx.read_artefact("attestation.json") or [None])[0]
    if not att:
        return {"observed": 1, "evidence": {"items": ["no attestation"]}}
    known = {i["full_name"] for i in _inv(ctx)} | set(ctx.system.layers)
    problems = [f"layer {l} not attested" for l in ctx.system.layers if l not in {x["layer"] for x in att["layers"]}]
    problems += [f"gap refers to unknown asset {g['asset']}" for g in att["gaps"] if g["asset"] not in known]
    counts = {k: sum(1 for i in _inv(ctx) if i["layer"] == k) for k in ctx.system.layers}
    problems += [f"layer {x['layer']} asset_count {x['asset_count']} != {counts.get(x['layer'])}"
                 for x in att["layers"] if counts.get(x["layer"]) != x["asset_count"]]
    if any(x["assessment"] == "not_ready" for x in att["layers"]) or not att["ready_for_metadata"]:
        problems.append("analyst attests the foundation is not ready for metadata")
    return {"observed": len(problems), "evidence": {"items": problems, "summary": att["summary"]}}


def undocumented_assets(ctx):
    items = _assess(ctx).get("undocumented", [])
    return {"observed": len(items), "evidence": {"items": items}}
