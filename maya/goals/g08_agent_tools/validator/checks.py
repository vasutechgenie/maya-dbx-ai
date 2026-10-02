"""G8 checks. They read the tool functions back from Unity Catalog, run every test as the agent identity, and list the
tools through the workspace's managed MCP server, so they certify the tools agents actually get."""
from datetime import datetime, timezone

from maya.core.workspace import SqlError, ident, lit

from ..code.common import MARK, FORBIDDEN, _norm, plan, run_test


def _result(items, **extra):
    return {"observed": len(items), "evidence": {"items": items[:200], **extra}}


def _live(ctx):
    cache = ctx.__dict__.setdefault("_g8_live", {})
    if "routines" not in cache:
        cat, sch = plan(ctx)["schema"].split(".")
        c = ident(cat)
        cache["routines"] = {r["routine_name"]: r for r in ctx.ws.sql(
            f"SELECT routine_name, routine_body, routine_definition, comment, data_type FROM {c}.information_schema.routines "
            f"WHERE routine_schema = {lit(sch)}")}
        params, cols = {}, {}
        for r in ctx.ws.sql(f"SELECT specific_name, parameter_name, full_data_type, comment, parameter_default, ordinal_position "
                            f"FROM {c}.information_schema.parameters WHERE specific_schema = {lit(sch)} ORDER BY ordinal_position"):
            params.setdefault(r["specific_name"], []).append(r)
        for r in ctx.ws.sql(f"SELECT specific_name, column_name, full_data_type, comment FROM {c}.information_schema.routine_columns "
                            f"WHERE specific_schema = {lit(sch)} ORDER BY ordinal_position"):
            cols.setdefault(r["specific_name"], []).append(r)
        cache["params"], cache["columns"] = params, cols
    return cache


def _type(t) -> str:
    return t.split(" COLLATE ")[0].split(" collate ")[0].replace(" ", "").upper()


def missing_functions(ctx):
    """AR-3.1: every tool is a SQL table function with exactly the approved parameters and result columns."""
    try:
        live = _live(ctx)
    except SqlError as e:
        return _result([{"kind": "function", "problem": f"cannot read the tools schema: {e}"}])
    items = []
    names = {t["name"] for t in plan(ctx)["tools"]}
    for t in plan(ctx)["tools"]:
        r = live["routines"].get(t["name"])
        if not r:
            items.append({"kind": "function", "tool": t["name"], "problem": "not deployed"})
            continue
        if r["routine_body"] != "SQL" or r["data_type"] != "TABLE_TYPE":
            items.append({"kind": "function", "tool": t["name"], "problem": f"not a SQL table function ({r['routine_body']}, {r['data_type']})"})
        have = [(p["parameter_name"], _type(p["full_data_type"])) for p in live["params"].get(t["name"], [])]
        want = [(p["name"], _type(p["type"])) for p in t["parameters"]]
        if have != want:
            items.append({"kind": "function", "tool": t["name"], "problem": f"parameters {have}, approved {want}"})
        have_c = [(c["column_name"], _type(c["full_data_type"])) for c in live["columns"].get(t["name"], [])]
        want_c = [(c["name"], _type(c["type"])) for c in t["columns"]]
        if have_c != want_c:
            items.append({"kind": "function", "tool": t["name"], "problem": f"result columns {have_c}, approved {want_c}"})
    items += [{"kind": "function", "tool": n, "problem": "a MAYA tool function that is no longer declared"}
              for n, r in live["routines"].items() if n not in names and (r["comment"] or "").endswith(MARK)]
    return _result(items)


def definition_differences(ctx):
    """AR-3.1: each function runs exactly the approved SQL, a read with typed parameters (no SQL built from strings)."""
    try:
        live = _live(ctx)["routines"]
    except SqlError as e:
        return _result([{"kind": "definition", "problem": str(e)}])
    items = []
    for t in plan(ctx)["tools"]:
        r = live.get(t["name"])
        if not r:
            continue
        d = r["routine_definition"] or ""
        if _norm(d) != _norm(t["sql"]):
            items.append({"kind": "definition", "tool": t["name"], "problem": "the deployed SQL differs from the approved SQL"})
        if FORBIDDEN.search(d) or "IDENTIFIER(" in d.upper():
            items.append({"kind": "definition", "tool": t["name"], "problem": "the SQL builds or changes something instead of reading"})
    return _result(items)


def undescribed(ctx):
    """AR-3.2: the function, every parameter and every result column carry a description agents read."""
    try:
        live = _live(ctx)
    except SqlError as e:
        return _result([{"kind": "description", "problem": str(e)}])
    items = []
    for t in plan(ctx)["tools"]:
        r = live["routines"].get(t["name"])
        if not r:
            continue
        if len((r["comment"] or "").replace(MARK, "").strip()) < 20:
            items.append({"kind": "description", "tool": t["name"], "problem": "the function has no description"})
        items += [{"kind": "description", "tool": t["name"], "parameter": p["parameter_name"], "problem": "no description"}
                  for p in live["params"].get(t["name"], []) if not (p["comment"] or "").strip()]
        items += [{"kind": "description", "tool": t["name"], "column": c["column_name"], "problem": "no description"}
                  for c in live["columns"].get(t["name"], []) if not (c["comment"] or "").strip()]
    return _result(items)


def failing_tests(ctx):
    """AR-3.3: every example call returns what the product owner expects, run as the agent identity."""
    spec = plan(ctx)
    as_agent = spec.get("test_as", "agent_identity") == "agent_identity"
    tests = []
    for t in spec["tools"]:
        tests += [run_test(ctx, t["full_name"], t, ex, as_agent) for ex in t["examples"]]
    ctx.write_artefact("tool_tests.json", {"tested_at": datetime.now(timezone.utc).isoformat(),
                                            "as": "agent identity" if as_agent else "deployer", "tests": tests})
    items = [{"kind": "test", "tool": x["tool"], "args": x["args"], "problem": "; ".join(x["problems"])}
             for x in tests if not x["passed"]]
    return _result(items, tests=len(tests))


def missing_grants(ctx):
    """AR-3.3: every executor (the agent identity first) holds USE SCHEMA and EXECUTE on every tool."""
    spec = plan(ctx)
    cat, sch = spec["schema"].split(".")
    c = ident(cat)
    try:
        ex = {(r["grantee"], r["routine_name"]) for r in ctx.ws.sql(
            f"SELECT grantee, routine_name FROM {c}.information_schema.routine_privileges "
            f"WHERE routine_schema = {lit(sch)} AND privilege_type = 'EXECUTE'")}
        us = {r["grantee"] for r in ctx.ws.sql(
            f"SELECT grantee, privilege_type FROM {c}.information_schema.schema_privileges WHERE schema_name = {lit(sch)}")
            if r["privilege_type"].replace("_", " ") in ("USE SCHEMA", "ALL PRIVILEGES")}
    except SqlError as e:
        return _result([{"kind": "grant", "problem": f"cannot read the grants: {e}"}])
    items = [{"kind": "grant", "principal": p, "problem": "no USE SCHEMA on the tools schema"} for p in spec["executors"] if p not in us]
    items += [{"kind": "grant", "principal": p, "tool": t["name"], "problem": "no EXECUTE"}
              for p in spec["executors"] for t in spec["tools"] if (p, t["name"]) not in ex]
    items += [{"kind": "grant", "principal": p, "tool": t["name"], "problem": "EXECUTE still granted to a removed executor"}
              for p in spec.get("revoke") or [] for t in spec["tools"] if (p, t["name"]) in ex]
    return _result(items)


def mcp_gaps(ctx):
    """AR-3.4: the workspace's managed MCP server for the tools schema lists every tool with its description and typed
    inputs, so any MCP client (agents, IDEs) can call them."""
    spec = plan(ctx)
    cat, sch = spec["schema"].split(".")
    path = f"/api/2.0/mcp/functions/{cat}/{sch}"
    try:
        res = ctx.ws.client.api_client.do("POST", path, body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                          headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
        listed = {x["name"]: x for x in (res.get("result") or {}).get("tools") or []}
    except Exception as e:
        return _result([{"kind": "mcp", "problem": f"managed MCP {path} did not answer tools/list: {e}"}])
    items = []
    for t in spec["tools"]:
        name = f"{cat}__{sch}__{t['name']}"
        x = listed.get(name)
        if not x:
            items.append({"kind": "mcp", "tool": t["name"], "problem": f"not listed by managed MCP as {name}"})
            continue
        props = (x.get("inputSchema") or {}).get("properties") or {}
        want = [p["name"] for p in t["parameters"]]
        if sorted(props) != sorted(want):
            items.append({"kind": "mcp", "tool": t["name"], "problem": f"MCP inputs {sorted(props)}, approved {sorted(want)}"})
        req = sorted((x.get("inputSchema") or {}).get("required") or [])
        if req != sorted(p["name"] for p in t["parameters"] if "default" not in p):
            items.append({"kind": "mcp", "tool": t["name"], "problem": f"MCP required inputs {req}"})
        if not (x.get("description") or "").strip():
            items.append({"kind": "mcp", "tool": t["name"], "problem": "listed without a description"})
    return _result(items, server=path, listed=len(listed))


def shape_only_tools(ctx):
    """Tools whose tests check only the shape of the result, never its numbers (informational)."""
    return _result([{"tool": t["name"], "problem": "no example compares the result with independent SQL (expect.matches)"}
                    for t in plan(ctx)["tools"] if not any(e["expect"].get("matches") for e in t["examples"])])
