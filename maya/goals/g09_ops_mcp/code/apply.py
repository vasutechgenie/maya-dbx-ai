"""G9 apply: the approved plan becomes bundle content (see deliver.py); the bundle deploys the jobs and the app, then
`bundle run` deploys the app's source and starts it."""
from datetime import datetime, timezone

from maya.core import bundle

from . import deliver, ledger
from .common import APP_KEY, plan


def _deploy(ctx, spec, label):
    deliver.write(ctx, spec)
    bundle.deploy_resources(ctx)
    bundle.run_resource(ctx, APP_KEY)
    out = {"operations": [o["name"] for o in spec["operations"]], "applied_at": datetime.now(timezone.utc).isoformat()}
    ctx.write_artefact("apply.json", out)
    ctx.log(f"     {label}: {len(spec['operations'])} operation jobs and the MCP server app {spec['app_name']}")
    return out


def apply(ctx):
    return _deploy(ctx, plan(ctx), "apply")


REDEPLOY = {"job", "app", "tools", "privilege"}


def repair(ctx):
    items = (ctx.outputs.get("validate") or {}).get("failed_items") or []
    kinds = {i.get("kind") for i in items if isinstance(i, dict)}
    if not kinds & REDEPLOY:
        return {"repaired": [], "note": "failures need a change to the operations or their notebooks"}
    return {"repaired": sorted(kinds & REDEPLOY), **_deploy(ctx, plan(ctx), "repair")}


def record(ctx):
    ledger.record(ctx, plan(ctx), ctx.read_artefact("mcp_tests.json") or {})
    return {"recorded": len(plan(ctx)["operations"])}


def export(ctx) -> dict:
    spec = ledger.certified_plan(ctx)
    if not spec:
        return {"files": {}}
    return deliver.bundle_content(ctx, spec)
