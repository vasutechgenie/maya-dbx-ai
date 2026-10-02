"""G8 apply: the approved tool plan becomes bundle scripts (the tools schema, one CREATE FUNCTION per tool, the grants),
deployed through the project bundle."""
from datetime import datetime, timezone

from maya.core import bundle
from maya.core.workspace import ident, lit

from . import ledger
from .common import MARK, create_sql, plan, q


def files(spec) -> dict:
    s = spec["schema"]
    out = {"10_schema/schema.sql": [f"CREATE SCHEMA IF NOT EXISTS {q(s)} COMMENT "
                             f"{lit('Business actions agents may call: parameterised, self-describing table functions ' + MARK)}"]}
    for t in spec["tools"]:
        out[f"20_functions/{t['name']}.sql"] = [create_sql(t["full_name"], t)]
    if spec.get("removed"):
        out["20_functions/_removed.sql"] = [f"DROP FUNCTION IF EXISTS {q(fn)}" for fn in spec["removed"]]
    grants = []
    for p in spec["executors"]:
        grants.append(f"GRANT USE SCHEMA ON SCHEMA {q(s)} TO {ident(p)}")
        grants += [f"GRANT EXECUTE ON FUNCTION {q(t['full_name'])} TO {ident(p)}" for t in spec["tools"]]
    for p in spec.get("revoke") or []:
        grants += [f"REVOKE EXECUTE ON FUNCTION {q(t['full_name'])} FROM {ident(p)}" for t in spec["tools"]]
        grants.append(f"REVOKE USE SCHEMA ON SCHEMA {q(s)} FROM {ident(p)}")
    out["30_grants/grants.sql"] = grants
    return out


def _deploy(ctx, spec, label):
    content = files(spec)
    base = bundle.scripts_dir(ctx.system, ctx.goal.id)
    stale = [str(p.relative_to(base)) for p in bundle.script_files(base)]
    written = bundle.write(ctx, content, remove=[s for s in stale if s not in content])
    res = bundle.deploy(ctx, written)
    out = {"deployed": res.get("deployed"), "deploy_run_id": res.get("job_run_id"),
           "applied_at": datetime.now(timezone.utc).isoformat()}
    ctx.write_artefact("apply.json", out)
    ctx.log(f"     {label}: {len(spec['tools'])} tool functions, grants to {len(spec['executors'])} executors")
    return out


def apply(ctx):
    return _deploy(ctx, plan(ctx), "apply")


REDEPLOY = {"function", "definition", "description", "grant", "mcp"}


def repair(ctx):
    items = (ctx.outputs.get("validate") or {}).get("failed_items") or []
    kinds = {i.get("kind") for i in items if isinstance(i, dict)}
    if not kinds & REDEPLOY:
        return {"repaired": [], "note": "failures need a change to the tools file (not repairable by re-deploying)"}
    return {"repaired": sorted(kinds & REDEPLOY), **_deploy(ctx, plan(ctx), "repair")}


def record(ctx):
    ledger.record(ctx, plan(ctx), ctx.read_artefact("tool_tests.json") or {})
    return {"recorded": len(plan(ctx)["tools"])}


def export(ctx) -> dict:
    spec = ledger.certified_plan(ctx)
    return {"files": files(spec) if spec else {}}
