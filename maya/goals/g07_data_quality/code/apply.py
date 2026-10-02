"""G7 apply: the approved plan becomes bundle content (see deliver.py), deployed through the project bundle; then the
check job runs once so the results, the dashboard and the alerts have data to certify."""
from datetime import datetime, timezone

from maya.core import bundle

from . import deliver, ledger
from .common import JOB_KEY, plan


def _deploy(ctx, spec, label):
    written = deliver.write(ctx, spec)
    res = bundle.deploy(ctx, written)
    run = bundle.run_job(ctx, JOB_KEY, deploy=False)
    out = {"deployed": res.get("deployed"), "deploy_run_id": res.get("job_run_id"), "check_run_id": run["job_run_id"],
           "applied_at": datetime.now(timezone.utc).isoformat()}
    ctx.write_artefact("apply.json", out)
    ctx.log(f"     {label}: {len(spec['rules'])} rules, {len(spec['freshness'])} tables; check job run {run['job_run_id']}")
    return out


def apply(ctx):
    return _deploy(ctx, plan(ctx), "apply")


REDEPLOY = {"tables", "rules", "results", "job", "alert", "dashboard", "access", "fire", "quarantine"}


def repair(ctx):
    """Re-deploy (and re-run the check job) when anything delivered is missing or differs from the plan."""
    items = (ctx.outputs.get("validate") or {}).get("failed_items") or []
    kinds = {i.get("kind") for i in items if isinstance(i, dict)}
    if not kinds & REDEPLOY:
        return {"repaired": [], "note": "failures need a change to the inputs (not repairable by re-deploying)"}
    return {"repaired": sorted(kinds & REDEPLOY), **_deploy(ctx, plan(ctx), "repair")}


def record(ctx):
    """After the data owner's sign-off: record the approved plan and the alert tests."""
    ledger.record(ctx, plan(ctx), ctx.read_artefact("alert_tests.json") or {})
    return {"recorded": plan(ctx)["marker"]}


def export(ctx) -> dict:
    """The bundle content of the certified plan, for `maya bundle`."""
    spec = ledger.certified_plan(ctx)
    if not spec:
        return {"files": {}}
    return deliver.bundle_content(spec)
