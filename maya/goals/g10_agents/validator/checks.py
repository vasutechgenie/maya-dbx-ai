"""G10 checks. They read the registered model and the endpoint back from the workspace and ask the served agents every
routing example, so they certify the agents users actually query: routed as designed, answering from tool results
with a trace, registered and served with every resource declared."""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ..code.common import RULES, SUPERVISOR_RULES, ask_endpoint, plan

NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _result(items, **extra):
    return {"observed": len(items), "evidence": {"items": items[:200], **extra}}


def _applied(ctx) -> dict:
    return ctx.read_artefact("apply.json") or {}


def _tests(ctx) -> dict:
    cache = ctx.__dict__.setdefault("_g10", {})
    if "tests" in cache:
        return cache["tests"]
    spec = plan(ctx)

    def one(ex):
        r = ask_endpoint(ctx, spec["endpoint"], ex["question"])
        want = {f"ask_{x}" for x in ex["route"]}
        problem = r.get("error")
        if not problem and not want <= set(r.get("routes") or []):
            problem = f"routed to {r.get('routes')}, expected {sorted(want)}"
        if not problem and not r.get("trace"):
            problem = "the answer has no trace of tool calls"
        if not problem and r.get("config_version") != spec["version"]:
            problem = f"answered by configuration {r.get('config_version')}, approved {spec['version']}"
        return {**r, "expected": sorted(want), "passed": not problem, "problem": problem}

    with ThreadPoolExecutor(max_workers=3) as pool:
        tests = list(pool.map(one, spec["routing_examples"]))
    out = {"tested_at": datetime.now(timezone.utc).isoformat(), "endpoint": spec["endpoint"], "tests": tests}
    ctx.write_artefact("agent_tests.json", out)
    cache["tests"] = out
    return out


def routing_failures(ctx):
    """AR-5.1: a supervisor routes to sub-agents with small tool sets; the served agents route every example as the
    product owner expects."""
    spec = plan(ctx)
    rt, limit = spec.get("runtime") or {}, int(ctx.inputs.get("max_tools_per_agent", 8))
    items = []
    subs = rt.get("sub_agents") or []
    if len(subs) < 2:
        items.append({"kind": "design", "problem": f"{len(subs)} sub-agents (at least 2)"})
    for s in subs:
        if not s["tools"] or len(s["tools"]) > limit:
            items.append({"kind": "design", "agent": s["name"], "problem": f"{len(s['tools'])} tools (1 to {limit})"})
    delegates = {t["target"] for t in (rt.get("supervisor") or {}).get("tools") or [] if t["type"] == "delegate"}
    items += [{"kind": "design", "agent": s["name"], "problem": "the supervisor cannot route to it"} for s in subs if s["name"] not in delegates]
    items += [{"kind": "routing", "question": t["question"], "problem": t["problem"]} for t in _tests(ctx)["tests"]
              if not t["passed"] and (t.get("problem") or "").startswith("routed")]
    return _result(items)


def serving_gaps(ctx):
    """AR-5.2: registered in Unity Catalog (alias champion), served on Model Serving at that version with the agent
    identity's credentials from a secret scope, every tool declared as a resource for authentication passthrough."""
    spec, applied = plan(ctx), _applied(ctx)
    items = []
    version = applied.get("version")
    try:
        mv = ctx.ws.client.model_versions.get(spec["model"], int(version))
        if mv.status and mv.status.value != "READY":
            items.append({"kind": "model", "problem": f"model version {version} is {mv.status.value}"})
        champ = ctx.ws.client.registered_models.get(spec["model"], include_aliases=True)
        alias = {a.alias_name: str(a.version_num) for a in champ.aliases or []}
        if alias.get("champion") != str(version):
            items.append({"kind": "model", "problem": f"alias champion is {alias.get('champion')}, deployed {version}"})
    except Exception as e:
        items.append({"kind": "model", "problem": f"registered model {spec['model']} version {version}: {e}"})
    try:
        e = ctx.ws.client.serving_endpoints.get(spec["endpoint"])
        if not e.state or e.state.ready.value != "READY":
            items.append({"kind": "endpoint", "problem": f"endpoint state {e.state.ready.value if e.state else None}"})
        served = (e.config.served_entities if e.config else None) or []
        mine = [s for s in served if s.entity_name == spec["model"] and str(s.entity_version) == str(version)]
        if not mine:
            items.append({"kind": "endpoint", "problem": f"serves {[(s.entity_name, s.entity_version) for s in served]}"})
        routes = {r.served_entity_name or r.served_model_name: r.traffic_percentage
                  for r in ((e.config.traffic_config.routes if e.config and e.config.traffic_config else None) or [])}
        if mine and routes and routes.get(mine[0].name) != 100:
            items.append({"kind": "endpoint", "problem": f"version {version} gets {routes.get(mine[0].name)}% of the traffic"})
        for s in served:
            for k, v in (s.environment_vars or {}).items():
                if k.startswith("MAYA_AGENT_") and not str(v).startswith("{{secrets/"):
                    items.append({"kind": "endpoint", "problem": f"{k} is not a secret reference"})
            if not any(k == "MAYA_AGENT_CLIENT_SECRET" for k in (s.environment_vars or {})):
                items.append({"kind": "endpoint", "problem": "the agent identity's credentials are not configured"})
    except Exception as e:
        items.append({"kind": "endpoint", "problem": f"endpoint {spec['endpoint']}: {e}"})
    declared = " ".join(applied.get("resources") or [])
    rt = spec.get("runtime") or {}
    for t in [t for a in [rt.get("supervisor") or {}, *(rt.get("sub_agents") or [])] for t in a.get("tools") or []]:
        if t["type"] in ("function", "lookup") and t["target"].split(".")[-1] not in declared:
            items.append({"kind": "model", "tool": t["name"], "problem": "not declared as a resource of the logged model"})
    if rt.get("llm_endpoint") and rt["llm_endpoint"] not in declared:
        items.append({"kind": "model", "problem": f"the language model endpoint {rt['llm_endpoint']} is not declared"})
    return _result(items, version=version)


def ungrounded_design(ctx):
    """AR-5.3: every prompt forbids inventing numbers, and every served answer returns the trace of its tool calls."""
    rt = plan(ctx).get("runtime") or {}
    items = []
    if SUPERVISOR_RULES not in (rt.get("supervisor") or {}).get("prompt", ""):
        items.append({"kind": "design", "agent": "supervisor", "problem": "the prompt lacks MAYA's rules"})
    items += [{"kind": "design", "agent": s["name"], "problem": "the prompt lacks MAYA's rules"}
              for s in rt.get("sub_agents") or [] if RULES not in s["prompt"]]
    for t in _tests(ctx)["tests"]:
        if t.get("error"):
            items.append({"kind": "endpoint", "question": t["question"], "problem": t["error"]})
        elif not t.get("trace"):
            items.append({"kind": "endpoint", "question": t["question"], "problem": "no trace of tool calls"})
        elif (t.get("problem") or "").startswith("answered by configuration"):
            items.append({"kind": "endpoint", "question": t["question"], "problem": t["problem"]})
    return _result(items)


def missing_access(ctx):
    """Every declared user may query the endpoint (CAN_QUERY)."""
    spec = plan(ctx)
    try:
        eid = ctx.ws.client.serving_endpoints.get(spec["endpoint"]).id
        acl = ctx.ws.client.serving_endpoints.get_permissions(eid).access_control_list or []
    except Exception as e:
        return _result([{"kind": "access", "problem": f"cannot read endpoint permissions: {e}"}])
    have = {a.group_name or a.user_name or a.service_principal_name for a in acl
            if any(p.permission_level.value in ("CAN_QUERY", "CAN_MANAGE") for p in a.all_permissions or [])}
    return _result([{"kind": "access", "principal": p, "problem": "may not query the endpoint"} for p in spec["users"] if p not in have])


def unsupported_numbers(ctx):
    """Numbers in served answers that appear in no tool result of that answer (informational)."""
    items = []
    for t in _tests(ctx)["tests"]:
        if not t.get("answer"):
            continue
        results = " ".join(s.get("result") or "" for s in t.get("trace") or []).replace(",", "")
        found = {n.replace(",", "") for n in NUMBER.findall(results)}
        for n in NUMBER.findall(t["answer"]):
            v = n.replace(",", "")
            if len(v.replace("-", "").replace(".", "")) < 3 or re.fullmatch(r"20\d\d", v):
                continue
            if not any(f == v or f.startswith(v) or (("." in f) and f.split(".")[0] == v.split(".")[0]) for f in found):
                items.append({"question": t["question"], "number": n})
    return _result(items)


def semantic_routing(ctx):
    """AR-5.4: the supervisor can look business terms up in the semantic model before routing (informational)."""
    rt = plan(ctx).get("runtime") or {}
    has = any(t["type"] == "lookup" for t in (rt.get("supervisor") or {}).get("tools") or [])
    return _result([] if has else [{"problem": "the supervisor has no ontology lookup (G3 not configured)"}])
