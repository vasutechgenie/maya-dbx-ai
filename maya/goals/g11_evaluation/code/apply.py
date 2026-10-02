"""G11 apply: the approved plan becomes bundle content (see deliver.py); the bundle deploys the tables and the
regression job, and MAYA runs the job once so the certification is based on an evaluation of what is served now."""
from datetime import datetime, timezone

from maya.core import bundle

from . import deliver, ledger
from .common import JOB_KEY, plan


def _deploy(ctx, spec, label):
    if spec.get("problems"):
        raise ValueError(f"the evaluation plan has problems: {spec['problems'][:3]}")
    written = deliver.write(ctx, spec)
    res = bundle.deploy(ctx, written)
    ctx.log(f"     {label}: evaluating {len(spec['questions'])} questions on {spec['targets']} (this takes a while)")
    run = bundle.run_job(ctx, JOB_KEY, deploy=False, raise_on_failure=False)
    out = dict(run["tasks"].get("evaluate") or {})
    if not out.get("ok"):
        raise RuntimeError(f"evaluation job failed (run {run['job_run_id']}): {out.get('error')}")
    out.update(deploy_run_id=res.get("job_run_id"), eval_job_run_id=run["job_run_id"],
               applied_at=datetime.now(timezone.utc).isoformat())
    ctx.write_artefact("apply.json", out)
    ctx.log("     " + "; ".join(f"{t}: {s['correct']}/{s['questions']} correct ({s['pass_rate']:.0%}, threshold "
                                f"{s['threshold']:.0%})" for t, s in (out.get("summary") or {}).items()))
    return out


def apply(ctx):
    return _deploy(ctx, plan(ctx), "apply")


def repair(ctx):
    items = (ctx.outputs.get("validate") or {}).get("failed_items") or []
    kinds = {i.get("kind") for i in items if isinstance(i, dict)}
    if not kinds & {"job", "results"}:
        return {"repaired": [], "note": "low pass rates need better agents, Genie instructions or questions"}
    return {"repaired": sorted(kinds & {"job", "results"}), **_deploy(ctx, plan(ctx), "repair")}


def record(ctx):
    ledger.record(ctx, plan(ctx), ctx.read_artefact("eval_results.json") or {})
    return {"recorded": plan(ctx)["version"]}


def export(ctx) -> dict:
    spec = ledger.certified_plan(ctx)
    if not spec:
        return {"files": {}}
    return deliver.bundle_content(ctx.system, spec)
