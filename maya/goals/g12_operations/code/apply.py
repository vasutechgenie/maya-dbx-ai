"""G12 apply: the approved plan becomes bundle content (see deliver.py) and the bundle deploys it: the monitoring
dashboard is created or updated and published, the documents are synced to the workspace. Production jobs are not
changed here: each belongs to the goal (or the team) that delivers it, and the checks name what each one lacks."""
from datetime import datetime, timezone

from maya.core import bundle

from . import deliver, ledger
from .common import plan


def _deploy(ctx, spec, label):
    written = deliver.write(ctx, spec)
    res = bundle.deploy(ctx, written)
    out = {"deploy_run_id": res.get("job_run_id"), "applied_at": datetime.now(timezone.utc).isoformat(),
           "documents": sorted(spec["documents"]), "dashboard": spec["dashboard"]["title"]}
    ctx.write_artefact("apply.json", out)
    ctx.log(f"     {label}: dashboard {spec['dashboard']['title']!r}, {len(spec['documents'])} documents")
    return out


def apply(ctx):
    return _deploy(ctx, plan(ctx), "apply")


REDEPLOY = {"dashboard", "document"}


def repair(ctx):
    items = (ctx.outputs.get("validate") or {}).get("failed_items") or []
    kinds = {i.get("kind") for i in items if isinstance(i, dict)}
    if not kinds & REDEPLOY:
        return {"repaired": [], "note": "production jobs are fixed by the goal or team that owns them"}
    return {"repaired": sorted(kinds & REDEPLOY), **_deploy(ctx, plan(ctx), "repair")}


def record(ctx):
    ledger.record(ctx, plan(ctx), ctx.read_artefact("operations_review.json") or {})
    return {"recorded": plan(ctx)["version"]}


def export(ctx) -> dict:
    spec = ledger.certified_plan(ctx)
    if not spec:
        return {"files": {}}
    return deliver.bundle_content(ctx.system, spec)
