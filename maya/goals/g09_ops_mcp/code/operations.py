"""G9 load and plan: the operations agents may run (the customer's and MAYA's built-in ones), the tool_smith agent's
tool names and descriptions, and the plan security approves: jobs, MCP tools, clients."""
import re

from maya.core.spec import stable_hash

from . import ledger
from .common import (STATUS_TOOLS, clients, env_name, job_key, notebook_source, operations, tool_parameters)

NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


def _context(ctx) -> dict:
    return ctx.read_artefact("context.json") or {}


def load(ctx):
    ops, errors = operations(ctx)
    if errors:
        raise ValueError("; ".join(errors))
    if not ops:
        raise ValueError("no operations: declare an operations file or keep MAYA's built-in operations")
    hashes = {o["name"]: stable_hash({"op": o, "source": notebook_source(ctx, o)}) for o in ops}
    done = (ledger.certified_plan(ctx) or {}).get("operations") or []
    reused = {o["name"]: o["draft"] for o in done if hashes.get(o["name"]) == o.get("hash") and o.get("draft")}
    ctx.write_artefact("context.json", {"operations": ops, "hashes": hashes, "reused": reused})
    todo = [o["name"] for o in ops if o["name"] not in reused]
    ctx.log(f"     {len(ops)} operations; {len(reused)} tool descriptions reused, {len(todo)} to write")
    return {"operations": len(ops), "describe_operations": todo}


def load_inputs_hash(ctx) -> str | None:
    ops, errors = operations(ctx)
    if errors:
        return None
    return stable_hash({"ops": ops, "sources": [notebook_source(ctx, o) for o in ops], "clients": clients(ctx),
                        "app": ctx.inputs["app_name"], "wait": ctx.inputs.get("max_wait_seconds", 280)})


def describe_input(ctx, name):
    op = next(o for o in _context(ctx)["operations"] if o["name"] == name)
    return {"operation": name, "intent": op["intent"], "writes": bool(op.get("writes")),
            "parameters": tool_parameters(op), "notebook": notebook_source(ctx, op)[:12000],
            "other_tools": [o["name"] for o in _context(ctx)["operations"] if o["name"] != name] + list(STATUS_TOOLS)}


def _problems(op, d) -> list[str]:
    out = []
    if d.get("operation") != op["name"]:
        out.append(f"the description is for {d.get('operation')}, not {op['name']}")
    if not NAME.match(d.get("tool_name") or ""):
        out.append(f"tool name {d.get('tool_name')} is not lowercase letters, digits and underscores")
    if len(d.get("description") or "") < 60:
        out.append("the tool description is too short")
    if op.get("writes") and "validate" not in (d.get("description") or "").lower():
        out.append("the description of an operation that changes data must say that mode=validate is a dry run")
    desc = {p["name"]: p.get("description") for p in d.get("parameters") or []}
    out += [f"parameter {p['name']} has no description" for p in tool_parameters(op) if not (desc.get(p["name"]) or "").strip()]
    return out


def plan(ctx):
    c = _context(ctx)
    drafts = {r["operation"]: r for r in ctx.read_artefact("tool_descriptions.json") or [] if isinstance(r, dict) and r.get("operation")}
    out, findings = [], []
    for op in c["operations"]:
        d = c["reused"].get(op["name"]) or drafts.get(op["name"])
        if not d:
            findings.append(f"{op['name']}: tool_smith returned no tool description")
            continue
        problems = _problems(op, d)
        desc = {p["name"]: p["description"] for p in d.get("parameters") or []}
        out.append({"name": op["name"], "tool": d.get("tool_name"), "description": (d.get("description") or "").strip(),
                    "intent": op["intent"], "writes": bool(op.get("writes")), "builtin": bool(op.get("builtin")),
                    "notebook": None if op.get("builtin") else op["notebook"],
                    "parameters": [{**p, "description": (desc.get(p["name"]) or p["description"]).strip()} for p in tool_parameters(op)],
                    "settings": op.get("settings") or {}, "timeout_minutes": op.get("timeout_minutes", 60),
                    "job_key": job_key(op["name"]), "env": env_name(op["name"]),
                    "draft": d, "hash": c["hashes"][op["name"]], "problems": problems})
        findings += [f"{op['name']}: {p}" for p in problems]
    tools = [o["tool"] for o in out]
    findings += [f"tool name {t} is used twice" for t in sorted({t for t in tools if tools.count(t) > 1})]
    findings += [f"tool name {t} clashes with a status tool" for t in tools if t in STATUS_TOOLS]
    spec = {"app_name": ctx.inputs["app_name"], "server": f"maya-ops-{ctx.system.name}",
            "operations": out, "clients": clients(ctx), "max_wait_seconds": ctx.inputs.get("max_wait_seconds", 280),
            "status_tools": list(STATUS_TOOLS), "inputs_hash": load_inputs_hash(ctx), "findings": findings}
    spec["version"] = stable_hash({k: spec[k] for k in ("operations", "clients", "max_wait_seconds")})[:12]
    ctx.write_artefact("ops_plan.json", spec)
    ctx.log(f"     plan: {len(out)} operation tools + {len(STATUS_TOOLS)} status tools, {len(spec['clients'])} clients, "
            f"{len(findings)} findings")
    return {"operations": len(out), "findings": len(findings)}
