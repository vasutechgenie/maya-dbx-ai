"""G10 load and plan: the product owner's agents, the tool catalog, the agent_engineer's prompts, tool sets and
routing, and a dry run of every routing example with the agents run locally before anything is deployed."""
import json
from concurrent.futures import ThreadPoolExecutor

from maya.core import agents, identity
from maya.core.spec import stable_hash

from . import ledger
from .common import (RULES, SUPERVISOR_RULES, agents_file, ask_local, catalog, expand, local_config, model_name, ops_app)

TASK = "write the prompts, tool sets and routing of a supervisor and its sub-agents"


def _context(ctx) -> dict:
    return ctx.read_artefact("context.json") or {}


def load(ctx):
    data, errors = agents_file(ctx)
    if errors:
        raise ValueError("; ".join(errors))
    if not identity.principal(ctx.system):
        raise ValueError("maya.yaml declares no agent_identity: the agents need an identity to act as")
    cat = catalog(ctx)
    for s in data["sub_agents"]:
        unknown = [r for r in expand(s.get("tools") or [], cat) if r not in cat]
        if unknown:
            raise ValueError(f"sub-agent {s['name']}: tools {unknown} are not certified (G8, G9) or configured (G3, G5)")
    h = stable_hash({"agents": data, "catalog": cat})
    cert = ledger.certified_plan(ctx) or {}
    reused = cert.get("design") if cert.get("design_hash") == h else None
    ctx.write_artefact("context.json", {"agents": data, "catalog": cat, "hash": h, "reused": reused})
    ctx.log(f"     {len(data['sub_agents'])} sub-agents, {len(cat)} tools in the catalog; design "
            f"{'reused from certification' if reused else 'to write'}")
    return {"design": [] if reused else ["system"]}


def load_inputs_hash(ctx) -> str | None:
    data, errors = agents_file(ctx)
    if errors:
        return None
    keys = ("endpoint", "schema", "model_name", "llm_endpoint", "users", "scale_to_zero", "max_tools_per_agent", "max_turns")
    return stable_hash({"agents": data, "inputs": {k: ctx.inputs.get(k) for k in keys}})


def engineer_input(ctx, _item, previous=None):
    c = _context(ctx)
    a = c["agents"]
    out = {"supervisor": a["supervisor"],
           "sub_agents": [{**s, "declared_tools": expand(s.get("tools") or [], c["catalog"])} for s in a["sub_agents"]],
           "tools": [{"ref": k, "name": v["name"], "type": v["type"], "description": v["description"]} for k, v in c["catalog"].items()],
           "routing_examples": a["routing_examples"],
           "max_tools_per_agent": ctx.inputs.get("max_tools_per_agent", 8),
           "rules_added_by_maya": {"sub_agents": RULES, "supervisor": SUPERVISOR_RULES}}
    if previous:
        out["previous_attempt"] = previous
    return out


def _problems(ctx, a, cat, d) -> list[str]:
    out = []
    subs = {s["name"]: s for s in d.get("sub_agents") or []}
    want = [s["name"] for s in a["sub_agents"]]
    out += [f"no design for sub-agent {n}" for n in want if n not in subs]
    out += [f"design for undeclared sub-agent {n}" for n in subs if n not in want]
    limit = int(ctx.inputs.get("max_tools_per_agent", 8))
    for s in a["sub_agents"]:
        x = subs.get(s["name"])
        if not x:
            continue
        tools = x.get("tools") or []
        declared = expand(s.get("tools") or [], cat)
        out += [f"{s['name']}: unknown tool {t}" for t in tools if t not in cat]
        out += [f"{s['name']}: declared tool {t} is missing" for t in declared if t not in tools]
        if not tools:
            out.append(f"{s['name']}: no tools")
        if len(tools) > limit:
            out.append(f"{s['name']}: {len(tools)} tools, at most {limit}")
        if len(x.get("prompt") or "") < 100:
            out.append(f"{s['name']}: the prompt is too short")
        if len(x.get("routing_description") or "") < 30:
            out.append(f"{s['name']}: the routing description is too short")
    if len(d.get("supervisor_prompt") or "") < 100:
        out.append("the supervisor prompt is too short")
    return out


def runtime(ctx, a, cat, d, version) -> dict:
    """The runtime configuration (template agent.py), workspace ids left out (resolved where it runs)."""
    subs = {s["name"]: s for s in d["sub_agents"]}
    sup_tools = [{"type": "delegate", "name": f"ask_{s['name']}", "target": s["name"],
                  "description": subs[s["name"]]["routing_description"]} for s in a["sub_agents"]]
    if "lookup" in cat:
        sup_tools.append(cat["lookup"])
    return {"version": version, "llm_endpoint": ctx.inputs.get("llm_endpoint", "databricks-claude-sonnet-4-6"),
            "max_turns": ctx.inputs.get("max_turns", 6),
            "supervisor": {"name": a["supervisor"]["name"], "prompt": d["supervisor_prompt"].strip() + "\n\n" + SUPERVISOR_RULES,
                           "tools": sup_tools},
            "sub_agents": [{"name": s["name"], "prompt": subs[s["name"]]["prompt"].strip() + "\n\n" + RULES,
                            "tools": [cat[t] for t in subs[s["name"]]["tools"]]} for s in a["sub_agents"]]}


def _dry_run(ctx, cfg, examples) -> list[dict]:
    def one(ex):
        r = ask_local(ctx, cfg, ex["question"])
        want = {f"ask_{x}" for x in ex["route"]}
        r["expected"] = sorted(want)
        r["passed"] = not r.get("error") and want <= set(r.get("routes") or [])
        return r
    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(one, examples))


def plan(ctx):
    c = _context(ctx)
    a, cat = c["agents"], c["catalog"]
    design = c["reused"] or next(iter(ctx.read_artefact("design.json") or []), None)
    if not design:
        raise ValueError("agent_engineer returned no design")
    model = ctx.system.model(ctx.inputs.get("model") or ctx.goal.spec["spec"]["harness"].get("model"))
    schema_file = json.loads((ctx.goal.dir / "harness" / "schemas" / "design.schema.json").read_text())
    problems = _problems(ctx, a, cat, design)
    cfg, tests = None, []
    for attempt in (1, 2):
        if not problems:
            version = stable_hash({"design": design, "catalog": cat, "inputs": load_inputs_hash(ctx)})[:12]
            rt = runtime(ctx, a, cat, design, version)
            cfg = local_config(ctx, rt)
            tests = _dry_run(ctx, cfg, a["routing_examples"])
            failed = [t for t in tests if not t["passed"]]
            if not failed:
                break
            problems = [f"'{t['question']}' went to {t.get('routes') or t.get('error')}, expected {t['expected']}" for t in failed]
        if attempt == 2 or c["reused"]:
            break
        ctx.log(f"     redesigning ({problems[0][:140]})")
        res = agents.run_agent(ctx, "agent_engineer", TASK, engineer_input(ctx, None, {"design": design, "problems": problems}),
                               schema_file, model, label="redesign-0")
        design = res["result"]
        problems = _problems(ctx, a, cat, design)
    sup = a["supervisor"]["name"]
    spec = {"endpoint": ctx.inputs["endpoint"], "model": model_name(ctx, sup), "schema": model_name(ctx, sup).rsplit(".", 1)[0],
            "users": list(ctx.inputs.get("users") or []), "scale_to_zero": ctx.inputs.get("scale_to_zero", True),
            "identity": {"service_principal": identity.principal(ctx.system), "secret_scope": identity.scope(ctx.system),
                         "client_id_key": identity.settings(ctx.system).get("client_id_key", "client_id"),
                         "client_secret_key": identity.settings(ctx.system).get("client_secret_key", "client_secret")},
            "genie_title": (cat.get("genie") or {}).get("target"), "ops_app": ops_app(ctx),
            "runtime": runtime(ctx, a, cat, design, cfg["version"]) if cfg else None,
            "routing_examples": a["routing_examples"], "suggested_examples": design.get("routing_examples") or [],
            "dry_run": [{k: t.get(k) for k in ("question", "expected", "routes", "passed", "error", "seconds", "answer")} for t in tests],
            "design": design, "design_hash": c["hash"], "inputs_hash": load_inputs_hash(ctx), "problems": problems if not cfg or
            any(not t["passed"] for t in tests) else []}
    spec["version"] = spec["runtime"]["version"] if spec["runtime"] else None
    spec["input_example"] = a["routing_examples"][0]["question"]
    ctx.write_artefact("agents_plan.json", spec)
    ctx.log(f"     plan: supervisor {sup} with {len(a['sub_agents'])} sub-agents; dry run "
            f"{sum(t['passed'] for t in tests)}/{len(tests)} routed as expected; {len(spec['problems'])} problems")
    return {"sub_agents": len(a["sub_agents"]), "problems": len(spec["problems"])}
