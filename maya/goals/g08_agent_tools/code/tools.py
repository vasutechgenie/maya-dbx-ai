"""G8 load and plan: the product owner's tools, the sources they may read, the tool_smith drafts (SQL body and
descriptions), and the dry run of every example before anything is deployed."""
import json
from concurrent.futures import ThreadPoolExecutor

import yaml

from maya.core import agents
from maya.core.spec import stable_hash
from maya.core.workspace import SqlError, ident, lit

from . import ledger
from .common import MARK, body_problems, bind, compare, executors, full_name, read_schemas, tools_file, tools_schema

TASK = "draft the SQL body and the descriptions of one agent tool"


def _metric_views(ctx) -> list[dict]:
    g2 = ctx.system.goal_settings("G2") or {}
    path = ctx.system.base_dir / g2.get("definitions", "")
    if not g2.get("definitions") or not path.exists():
        return []
    schema = g2["schema"] if "." in g2["schema"] else f"{ctx.system.catalogs[0]}.{g2['schema']}"
    out = []
    for v in (yaml.safe_load(path.read_text()) or {}).get("metric_views") or []:
        out.append({"name": f"{schema}.{v['name']}", "comment": v.get("comment"),
                    "dimensions": [{"name": d["name"], "comment": d.get("comment")} for d in v.get("dimensions") or []],
                    "measures": [{"name": m["name"], "comment": m.get("comment")} for m in v.get("measures") or []]})
    return out


def _tables(ctx) -> list[dict]:
    """Tables, views and functions of the readable schemas other than the metric views."""
    mv = {v["name"].rsplit(".", 1)[0] for v in _metric_views(ctx)}
    out = []
    for s in read_schemas(ctx):
        if s in mv:
            continue
        cat, sch = s.split(".")
        cols = ctx.ws.sql(f"""SELECT c.table_name, t.comment AS table_comment, c.column_name, c.full_data_type, c.comment
            FROM {ident(cat)}.information_schema.columns c JOIN {ident(cat)}.information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = {lit(sch)} AND t.table_type <> 'METRIC_VIEW' ORDER BY c.table_name, c.ordinal_position""")
        by = {}
        for r in cols:
            t = by.setdefault(r["table_name"], {"name": f"{s}.{r['table_name']}", "comment": r["table_comment"], "columns": []})
            t["columns"].append({"name": r["column_name"], "type": r["full_data_type"], "comment": r["comment"]})
        out += list(by.values())
        for r in ctx.ws.sql(f"SELECT routine_name, comment FROM {ident(cat)}.information_schema.routines "
                            f"WHERE routine_schema = {lit(sch)}"):
            out.append({"name": f"{s}.{r['routine_name']}", "kind": "function", "comment": r["comment"]})
    return out


def _context(ctx) -> dict:
    return ctx.read_artefact("context.json") or {}


def load(ctx):
    tools, errors = tools_file(ctx)
    if errors:
        raise ValueError("; ".join(errors))
    sources = {"metric_views": _metric_views(ctx), "tables": _tables(ctx), "schemas": read_schemas(ctx)}
    src_hash = stable_hash(sources)
    hashes = {t["name"]: stable_hash({"tool": t, "sources": src_hash, "schema": tools_schema(ctx)}) for t in tools}
    done = (ledger.certified_plan(ctx) or {}).get("tools") or []
    reused = {t["name"]: t["draft"] for t in done if hashes.get(t["name"]) == t.get("hash") and t.get("draft")}
    ctx.write_artefact("context.json", {"tools": tools, "sources": sources, "hashes": hashes, "reused": reused})
    todo = [t["name"] for t in tools if t["name"] not in reused]
    ctx.log(f"     {len(tools)} tools; {len(reused)} drafts reused from certification, {len(todo)} to draft")
    return {"tools": len(tools), "draft_tools": todo}


def load_inputs_hash(ctx) -> str | None:
    tools, errors = tools_file(ctx)
    if errors:
        return None
    return stable_hash({"tools": tools, "schema": tools_schema(ctx), "executors": executors(ctx)})


def draft_input(ctx, name, previous=None):
    c = _context(ctx)
    tool = next(t for t in c["tools"] if t["name"] == name)
    out = {"tool": {k: tool[k] for k in ("name", "intent") if k in tool},
           "parameters": tool.get("parameters") or [],
           "example_calls": [e["args"] for e in tool["examples"]],
           "expected_columns": sorted({col for e in tool["examples"] for col in e["expect"].get("columns") or []}),
           "given_sql": tool.get("sql"),
           "metric_views": c["sources"]["metric_views"], "tables": c["sources"]["tables"],
           "readable_schemas": c["sources"]["schemas"]}
    if previous:
        out["previous_attempt"] = previous
    return out


def _check(ctx, tool, draft) -> tuple[dict, list[str]]:
    """The tool as it will be deployed, and why it cannot be (empty when it can)."""
    params = tool.get("parameters") or []
    pc = {p["name"]: p.get("comment") for p in draft.get("parameters") or []}
    problems = [f"parameter {p['name']} has no description" for p in params if not (pc.get(p["name"]) or "").strip()]
    if draft.get("name") != tool["name"]:
        problems.append(f"the draft is for {draft.get('name')}, not {tool['name']}")
    if len((draft.get("comment") or "").strip()) < 20:
        problems.append("the function comment is missing or too short")
    sql = (tool.get("sql") or draft.get("sql") or "").strip().rstrip(";")
    problems += body_problems(ctx, tool["name"], sql, params)
    entry = {"name": tool["name"], "full_name": full_name(ctx, tool), "intent": tool["intent"],
             "comment": (draft.get("comment") or "").strip(), "sql": sql,
             "source": "customer" if tool.get("sql") else "tool_smith",
             "parameters": [{**{k: p[k] for k in ("name", "type") if k in p}, **({"default": p["default"]} if "default" in p else {}),
                             "comment": (pc.get(p["name"]) or p["description"]).strip()} for p in params],
             "examples": tool["examples"], "columns": [], "dry_run": []}
    if problems:
        return entry, problems
    try:
        cols = ctx.ws.sql(f"DESCRIBE QUERY {bind(tool['name'], sql, params, tool['examples'][0]['args'])}")
    except SqlError as e:
        return entry, [f"the body does not compile: {str(e)[:400]}"]
    cc = {c["name"]: c.get("comment") for c in draft.get("columns") or []}
    entry["columns"] = [{"name": c["col_name"], "type": c["data_type"].upper(), "comment": (cc.get(c["col_name"]) or "").strip()}
                        for c in cols]
    problems += [f"result column {c['name']} has no description" for c in entry["columns"] if not c["comment"]]
    names = [c["name"] for c in entry["columns"]]
    problems += [f"result column {n} appears twice" for n in sorted({n for n in names if names.count(n) > 1})]
    for i, ex in enumerate(tool["examples"]):
        try:
            rows = ctx.ws.sql(f"SELECT * FROM ({bind(tool['name'], sql, params, ex['args'])})")
            truth = ctx.ws.sql(ex["expect"]["matches"]["sql"]) if ex["expect"].get("matches") else None
            diff = compare(rows, ex["expect"], truth)
        except SqlError as e:
            rows, diff = [], [f"the example fails: {str(e)[:400]}"]
        entry["dry_run"].append({"args": ex["args"], "rows": len(rows), "passed": not diff, "problems": diff})
        problems += [f"example {i + 1} {json.dumps(ex['args'])}: {d}" for d in diff]
    return entry, problems


def plan(ctx):
    c = _context(ctx)
    drafts = {r["name"]: r for r in ctx.read_artefact("tool_drafts.json") or [] if isinstance(r, dict) and r.get("name")}
    model = ctx.system.model(ctx.inputs.get("model") or ctx.goal.spec["spec"]["harness"].get("model"))
    schema_file = json.loads((ctx.goal.dir / "harness" / "schemas" / "tool_draft.schema.json").read_text())

    def one(i_tool):
        i, tool = i_tool
        draft = c["reused"].get(tool["name"]) or drafts.get(tool["name"])
        if not draft:
            return tool, None, None, [f"tool_smith returned no draft for {tool['name']}"]
        entry, problems = _check(ctx, tool, draft)
        if problems and tool["name"] not in c["reused"]:
            ctx.log(f"     {tool['name']}: redrafting ({problems[0][:120]})")
            res = agents.run_agent(ctx, "tool_smith", TASK,
                                   draft_input(ctx, tool["name"], {"draft": draft, "problems": problems}),
                                   schema_file, model, label=f"redraft-{i}")
            draft = res["result"]
            entry, problems = _check(ctx, tool, draft)
        return tool, draft, entry, problems

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, enumerate(c["tools"])))
    tools, findings = [], []
    for tool, draft, entry, problems in results:
        if entry is None:
            findings += problems
            continue
        tools.append({**entry, "draft": draft, "hash": c["hashes"][tool["name"]], "problems": problems})
        findings += [f"{tool['name']}: {p}" for p in problems]

    schema = tools_schema(ctx)
    cat, sch = schema.split(".")
    try:
        live = [r["routine_name"] for r in ctx.ws.sql(
            f"SELECT routine_name FROM {ident(cat)}.information_schema.routines WHERE routine_schema = {lit(sch)} "
            f"AND comment LIKE {lit('%' + MARK)}")]
    except SqlError:
        live = []
    names = {t["name"] for t in tools}
    cert = ledger.certified_plan(ctx) or {}
    spec = {"schema": schema, "tools": tools, "executors": executors(ctx),
            "revoke": [p for p in cert.get("executors") or [] if p not in executors(ctx)],
            "removed": sorted(f"{schema}.{n}" for n in live if n not in names),
            "test_as": ctx.inputs.get("test_as", "agent_identity"),
            "inputs_hash": load_inputs_hash(ctx), "findings": findings}
    ctx.write_artefact("tool_plan.json", spec)
    ctx.log(f"     plan: {len(tools)} tools, {len(spec['executors'])} executors, {len(findings)} findings")
    return {"tools": len(tools), "findings": len(findings)}
