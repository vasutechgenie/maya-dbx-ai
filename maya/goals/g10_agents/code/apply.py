"""G10 apply: the approved plan becomes bundle content (see deliver.py); the bundle deploys the model's schema and the
deploy job, MAYA runs the job (log, register, serve) and waits until the endpoint serves the new version."""
import time
from datetime import datetime, timezone

from maya.core import bundle

from . import deliver, ledger
from .common import JOB_KEY, plan


def wait_ready(ctx, endpoint, version, minutes=50) -> dict:
    deadline = time.time() + minutes * 60
    while True:
        e = ctx.ws.client.serving_endpoints.get(endpoint)
        served = [s for s in (e.config.served_entities if e.config else None) or []]
        ready = e.state and e.state.ready and e.state.ready.value == "READY"
        updating = e.state and e.state.config_update and e.state.config_update.value not in ("NOT_UPDATING",)
        if ready and not updating and any(str(s.entity_version) == str(version) for s in served):
            return {"ready": True, "waited_until": datetime.now(timezone.utc).isoformat()}
        if e.state and e.state.config_update and e.state.config_update.value == "UPDATE_FAILED":
            raise RuntimeError(f"endpoint {endpoint} failed to serve version {version}")
        if time.time() > deadline:
            raise RuntimeError(f"endpoint {endpoint} not ready with version {version} after {minutes} minutes")
        time.sleep(30)


def _deploy(ctx, spec, label):
    written = deliver.write(ctx, spec)
    res = bundle.deploy(ctx, written)
    run = bundle.run_job(ctx, JOB_KEY, deploy=False)
    out = dict(run["tasks"].get("deploy") or {})
    ctx.log(f"     {label}: model {out.get('model')} version {out.get('version')}; waiting for endpoint {spec['endpoint']}")
    out.update(wait_ready(ctx, spec["endpoint"], out["version"]), deploy_run_id=res.get("job_run_id"),
               agents_job_run_id=run["job_run_id"], applied_at=datetime.now(timezone.utc).isoformat())
    ctx.write_artefact("apply.json", out)
    ctx.log(f"     endpoint {spec['endpoint']} serves version {out['version']}")
    return out


def apply(ctx):
    return _deploy(ctx, plan(ctx), "apply")


REDEPLOY = {"endpoint", "model", "access"}


def repair(ctx):
    items = (ctx.outputs.get("validate") or {}).get("failed_items") or []
    kinds = {i.get("kind") for i in items if isinstance(i, dict)}
    if not kinds & REDEPLOY:
        return {"repaired": [], "note": "failures need a change to the agents file or the tools"}
    return {"repaired": sorted(kinds & REDEPLOY), **_deploy(ctx, plan(ctx), "repair")}


def record(ctx):
    ledger.record(ctx, plan(ctx), ctx.read_artefact("agent_tests.json") or {})
    return {"recorded": plan(ctx)["version"]}


def export(ctx) -> dict:
    spec = ledger.certified_plan(ctx)
    if not spec:
        return {"files": {}}
    return deliver.bundle_content(ctx.system, spec)
