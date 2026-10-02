"""Shared helpers for G10: the agents file, the tool catalog the agents may use (G8 functions, G3 lookup, G5 Genie, G9
MCP tools, all as certified), the rules every prompt carries, and calls to the served endpoint."""
import importlib.util
import json
import time
from pathlib import Path

import yaml

from maya.core import identity

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
JOB_KEY = "maya_g10_agents"
RULES = (
    "Rules you always follow:\n"
    "- Answer only from the results of the tools you called for this question. Never invent, estimate, extrapolate or "
    "round a number differently: every figure you state must appear in a tool result.\n"
    "- If no tool can answer, or a tool returns an error or no rows, say so plainly and name the tool you tried.\n"
    "- Say which tool each figure came from. Keep units (for example USD) and periods as the tool states them.\n"
    "- Never run an operation that changes data (mode=run) unless the user explicitly asks for it; validate first.")
SUPERVISOR_RULES = (
    "Rules you always follow:\n"
    "- Hand every question about data, figures, data quality or operations to the sub-agent whose description fits; "
    "use several when the question spans them. Never state a figure that a sub-agent did not return.\n"
    "- Report the sub-agents' figures exactly, with the tool they came from. If they could not answer, say so.\n"
    "- When a business term is unclear, look it up first (lookup_term) to find what it means and where it lives.")


def q_schema(full_name) -> str:
    from maya.core.workspace import ident
    return ident(*full_name.split("."))


def _schema_name(ctx, name) -> str:
    return name if "." in name else f"{ctx.system.catalogs[0]}.{name}"


def model_name(ctx, supervisor) -> str:
    return f"{_schema_name(ctx, ctx.inputs['schema'])}.{ctx.inputs.get('model_name') or supervisor}"


def agents_file(ctx) -> tuple[dict, list[str]]:
    ref = ctx.inputs["agents"]
    path = ctx.system.base_dir / ref
    if not path.exists():
        return {}, [f"agents file {ref} not found"]
    data = yaml.safe_load(path.read_text()) or {}
    import jsonschema
    schema = ctx.goal.inputs_schema()
    errors = sorted(jsonschema.Draft202012Validator({"$ref": "#/$defs/agents_file", "$defs": schema["$defs"]})
                    .iter_errors(data), key=lambda e: list(e.path))
    out = [f"{ref}: {e.message} at {'/'.join(map(str, e.path)) or 'root'}" for e in errors[:8]]
    if out:
        return {}, out
    names = [s["name"] for s in data["sub_agents"]]
    out += [f"{ref}: sub-agent {n} is declared twice" for n in sorted({n for n in names if names.count(n) > 1})]
    for ex in data["routing_examples"]:
        out += [f"{ref}: routing example '{ex['question'][:40]}' names unknown sub-agent {r}" for r in ex["route"] if r not in names]
    out += [f"{ref}: sub-agent {n} has no routing example" for n in names
            if not any(n in ex["route"] for ex in data["routing_examples"])]
    return data, out


# ---------------------------------------------------------------- the tool catalog
def _certified(ctx, goal_id, table) -> dict | None:
    from maya.core.workspace import lit
    try:
        rows = ctx.ws.sql(f"""SELECT max_by(plan_json, recorded_at) AS p FROM {ctx.state.t(table)}
            WHERE system = {lit(ctx.system.name)} AND run_id IN (SELECT run_id FROM {ctx.state.t('certifications')}
              WHERE system = {lit(ctx.system.name)} AND goal_id = {lit(goal_id)})""")
    except Exception:
        return None
    return json.loads(rows[0]["p"]) if rows and rows[0]["p"] else None


def catalog(ctx) -> dict:
    """{reference: tool spec} of every tool the agents may use, from the certified G8, G9 and the G3 and G5 settings."""
    out = {}
    g8 = _certified(ctx, "G8", "tools_ledger") or {}
    for t in g8.get("tools") or []:
        out[f"function:{t['name']}"] = {"type": "function", "name": t["name"], "description": t["comment"],
                                        "parameters": t["parameters"], "target": t["full_name"]}
    g3 = ctx.system.goal_settings("G3") or {}
    if g3.get("schema"):
        out["lookup"] = {"type": "lookup", "name": "lookup_term", "target": f"{_schema_name(ctx, g3['schema'])}.ontology_lookup",
                         "description": "What a business term means and where it lives: glossary terms and synonyms, "
                                        "domains, pages and the tables or metric views that hold it."}
    g5 = ctx.system.goal_settings("G5") or {}
    if g5.get("title"):
        out["genie"] = {"type": "genie", "name": "ask_genie", "target": g5["title"],
                        "description": f"Ask the Genie space '{g5['title']}' a question in plain language: {g5.get('description', '')} "
                                       "It writes and runs SQL on the governed metric views and returns its answer, the SQL "
                                       "and the rows. Use it for questions the other tools do not cover."}
    g9 = _certified(ctx, "G9", "ops_ledger") or {}
    for o in g9.get("operations") or []:
        props = {p["name"]: {k: p[k] for k in ("type", "enum", "default", "description") if k in p} for p in o["parameters"]}
        props["wait_seconds"] = {"type": "integer", "description": "How long to wait for the run (at most "
                                 f"{g9.get('max_wait_seconds', 280)} seconds)"}
        out[f"ops:{o['tool']}"] = {"type": "mcp", "name": o["tool"], "description": o["description"], "target": o["tool"],
                                   "input_schema": {"type": "object", "properties": props,
                                                    "required": [p["name"] for p in o["parameters"] if "default" not in p]}}
    if g9:
        out["ops:get_run_status"] = {"type": "mcp", "name": "get_run_status", "target": "get_run_status",
                                     "description": "Status and, once finished, the JSON result of an operation run started earlier.",
                                     "input_schema": {"type": "object", "required": ["run_id"],
                                                      "properties": {"run_id": {"type": "integer"}, "wait_seconds": {"type": "integer"}}}}
        out["ops:list_recent_runs"] = {"type": "mcp", "name": "list_recent_runs", "target": "list_recent_runs",
                                       "description": "The most recent runs of one operation (or all): id, state, start time, who started them.",
                                       "input_schema": {"type": "object", "properties": {"operation": {"type": "string"},
                                                                                         "limit": {"type": "integer"}}}}
    return out


def ops_app(ctx) -> str | None:
    g9 = _certified(ctx, "G9", "ops_ledger") or {}
    if not g9:
        return None
    from maya.core import bundle
    from maya.goals.g09_ops_mcp.code.common import APP_KEY
    res = bundle.resources(ctx.system, ctx.ws)
    return ((res.get("apps") or {}).get(APP_KEY) or {}).get("name") or g9.get("app_name")


def expand(refs, cat) -> list[str]:
    out = []
    for r in refs:
        if r == "functions":
            out += [k for k in cat if k.startswith("function:")]
        elif r == "ops":
            out += [k for k in cat if k.startswith("ops:")]
        else:
            out.append(r)
    return list(dict.fromkeys(out))


def plan(ctx) -> dict:
    return ctx.read_artefact("agents_plan.json") or {}


# ---------------------------------------------------------------- running the agents
def runtime_module():
    spec = importlib.util.spec_from_file_location("maya_agent_runtime", TEMPLATES / "agent" / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def local_config(ctx, runtime) -> dict:
    """The runtime configuration resolved for this workspace (what deploy_agent.py does in the job)."""
    cfg = dict(runtime, host=ctx.ws.host, warehouse_id=ctx.ws.warehouse_id)
    g5 = ctx.system.goal_settings("G5") or {}
    if g5.get("title"):
        spaces = [s for s in (ctx.ws.client.genie.list_spaces().spaces or []) if s.title == g5["title"]]
        cfg["genie_space_id"] = spaces[0].space_id if spaces else None
    app = ops_app(ctx)
    if app:
        cfg["mcp_url"] = ctx.ws.client.apps.get(app).url
    return cfg


def ask_local(ctx, cfg, question) -> dict:
    mod = runtime_module()
    t0 = time.time()
    try:
        answer, trace = mod.Supervisor(cfg, data=identity.client(ctx), llm=ctx.ws.client).ask(question)
    except Exception as e:
        return {"question": question, "error": str(e)[:1000], "seconds": round(time.time() - t0, 1)}
    sup = cfg["supervisor"]["name"]
    return {"question": question, "answer": answer, "trace": trace, "seconds": round(time.time() - t0, 1),
            "routes": list(dict.fromkeys(s["tool"] for s in trace if s["agent"] == sup))}


def ask_endpoint(ctx, endpoint, question, timeout=900) -> dict:
    """Ask the served agent (scale-to-zero endpoints may need a few minutes to wake)."""
    import requests
    url = f"{ctx.ws.host}/serving-endpoints/{endpoint}/invocations"
    t0, err = time.time(), None
    for attempt in range(4):
        try:
            r = requests.post(url, headers={**ctx.ws.auth_headers(), "Content-Type": "application/json"}, timeout=timeout,
                              json={"input": [{"role": "user", "content": question}]})
            if r.status_code in (429, 502, 503, 504):
                err = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(30 * (attempt + 1))
                continue
            r.raise_for_status()
            body = r.json()
            text = " ".join(c.get("text", "") for o in body.get("output") or [] for c in o.get("content") or []
                            if isinstance(c, dict))
            co = body.get("custom_outputs") or {}
            return {"question": question, "answer": text, "trace": co.get("trace") or [], "routes": co.get("routes") or [],
                    "config_version": co.get("config_version"), "seconds": round(time.time() - t0, 1)}
        except Exception as e:
            err = str(e)[:500]
            time.sleep(20)
    return {"question": question, "error": err, "seconds": round(time.time() - t0, 1)}
