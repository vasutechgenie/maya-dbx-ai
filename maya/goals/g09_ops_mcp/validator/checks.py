"""G9 checks. They read the delivered jobs and app back from the workspace and call the MCP server as the agent identity
(OAuth): list its tools, run every operation in its safe mode, start one twice, ask for run status, and try a
non-OAuth token, so they certify the server agents actually use."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from maya.core import bundle, identity

from ..code.common import APP_KEY, MCP, STATUS_TOOLS, plan
from ..code.deliver import server_spec

PRIVILEGE_ORDER = ["CAN_VIEW", "CAN_MANAGE_RUN", "IS_OWNER", "CAN_MANAGE"]


def _result(items, **extra):
    return {"observed": len(items), "evidence": {"items": items[:200], **extra}}


def _res(ctx):
    cache = ctx.__dict__.setdefault("_g9", {})
    if "res" not in cache:
        cache["res"] = bundle.resources(ctx.system, ctx.ws)
    return cache["res"]


def _app(ctx):
    cache = ctx.__dict__.setdefault("_g9", {})
    if "app" not in cache:
        name = ((_res(ctx).get("apps") or {}).get(APP_KEY) or {}).get("name") or plan(ctx)["app_name"]
        try:
            cache["app"] = ctx.ws.client.apps.get(name)
        except Exception as e:
            cache["app"] = None
            cache["app_error"] = str(e)
    return cache["app"]


def _job_id(ctx, o):
    return ((_res(ctx).get("jobs") or {}).get(o["job_key"]) or {}).get("id")


def _expected_tools(spec) -> dict:
    out = {}
    for t in server_spec(spec)["tools"]:
        props = {p["name"]: {k: p[k] for k in ("type", "enum", "default") if k in p} for p in t["parameters"]}
        props["wait_seconds"] = {"type": "integer"}
        out[t["name"]] = {"description": t["description"], "properties": props,
                          "required": sorted(p["name"] for p in t["parameters"] if "default" not in p)}
    return out


def _tests(ctx) -> dict:
    """Call the server once per check run; every check reads these results (written as mcp_tests.json)."""
    cache = ctx.__dict__.setdefault("_g9", {})
    if "tests" in cache:
        return cache["tests"]
    spec, app, tests = plan(ctx), _app(ctx), []
    out = {"tested_at": datetime.now(timezone.utc).isoformat(), "url": app.url if app else None, "tests": tests,
           "listed": {}, "runs": {}}
    cache["tests"] = out
    if not app or not app.url:
        tests.append({"test": "server", "passed": False, "problem": "the app does not exist or has no URL"})
        ctx.write_artefact("mcp_tests.json", out)
        return out
    mcp = MCP(ctx, app.url)
    try:
        init = mcp.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "maya-validator", "version": "1"}})
        tests.append({"test": "initialize as the agent identity (OAuth)", "passed": True, "server": init.get("serverInfo")})
        listed = {t["name"]: t for t in mcp.tools()}
        out["listed"] = listed
        tests.append({"test": "tools/list", "passed": True, "tools": sorted(listed)})
    except Exception as e:
        tests.append({"test": "initialize and tools/list as the agent identity", "passed": False, "problem": str(e)[:500]})
        ctx.write_artefact("mcp_tests.json", out)
        return out
    wait = int(spec["max_wait_seconds"])

    def run(o):
        try:
            return o["name"], mcp.call(o["tool"], {"wait_seconds": wait}), None
        except Exception as e:
            return o["name"], None, str(e)[:500]

    with ThreadPoolExecutor(max_workers=4) as pool:
        for name, res, err in pool.map(run, spec["operations"]):
            o = next(x for x in spec["operations"] if x["name"] == name)
            if res and res.get("life_cycle_state") != "TERMINATED":
                try:
                    res = {**res, **mcp.call("get_run_status", {"run_id": res["run_id"], "wait_seconds": wait})}
                except Exception as e:
                    err = str(e)[:500]
            out["runs"][name] = res
            r = (res or {}).get("result")
            problem = err or (None if res and res.get("result_state") == "SUCCESS" and isinstance(r, dict) else
                              f"run {(res or {}).get('run_id')} ended {(res or {}).get('result_state')} with result {str(r)[:300]}")
            if not problem and o["writes"] and (r.get("mode") != "validate" or r.get("changed")):
                problem = f"called without mode, it did not run as a dry run (mode {r.get('mode')}, changed {r.get('changed')})"
            tests.append({"test": f"{o['tool']} with default arguments", "operation": name, "passed": not problem,
                          "problem": problem, "run_id": (res or {}).get("run_id"), "result": r if isinstance(r, dict) else None})
    # idempotent start: a second call while the first runs returns the same run
    o = spec["operations"][0]
    try:
        first = mcp.call(o["tool"], {"wait_seconds": 0})
        second = mcp.call(o["tool"], {"wait_seconds": 0})
        same = first.get("run_id") == second.get("run_id")
        tests.append({"test": f"{o['tool']} called twice while running", "passed": same,
                      "problem": None if same else f"started two runs ({first.get('run_id')}, {second.get('run_id')})",
                      "run_id": first.get("run_id")})
        st = mcp.call("get_run_status", {"run_id": first["run_id"], "wait_seconds": wait})
        tests.append({"test": "get_run_status", "passed": st.get("run_id") == first["run_id"], "state": st.get("life_cycle_state")})
        recent = mcp.call("list_recent_runs", {"operation": o["tool"], "limit": 3})
        tests.append({"test": "list_recent_runs", "passed": any(r["run_id"] == first["run_id"] for r in recent.get("runs") or []),
                      "runs": len(recent.get("runs") or [])})
    except Exception as e:
        tests.append({"test": "idempotent start and status tools", "passed": False, "problem": str(e)[:500]})
    try:
        bad = mcp.raw("tools/call", {"name": o["tool"], "arguments": {"no_such_argument": 1}}).json()
        rejected = bool(((bad.get("result") or {}).get("isError")) or bad.get("error"))
        tests.append({"test": "unknown argument rejected", "passed": rejected})
    except Exception as e:
        tests.append({"test": "unknown argument rejected", "passed": False, "problem": str(e)[:300]})
    # OAuth only: the deployer's personal access token must not get in
    h = ctx.ws.auth_headers()
    if ctx.ws.client.config.auth_type == "pat":
        r = requests.post(app.url.rstrip("/") + "/mcp", headers={**h, "Content-Type": "application/json"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=60, allow_redirects=False)
        tests.append({"test": "personal access token rejected (OAuth only)", "passed": r.status_code in (301, 302, 303, 307, 401, 403),
                      "status": r.status_code})
    ctx.write_artefact("mcp_tests.json", out)
    return out


def job_differences(ctx):
    """AR-4.1: each operation is a job running its notebook with the approved typed parameters, one run at a time."""
    from ..code.deliver import job as job_spec
    items = []
    for o in plan(ctx)["operations"]:
        jid = _job_id(ctx, o)
        try:
            s = ctx.ws.client.jobs.get(int(jid)).settings
        except Exception:
            items.append({"kind": "job", "operation": o["name"], "problem": "the job does not exist"})
            continue
        want = job_spec(ctx.system.name, o)
        have = sorted(p.name for p in s.parameters or [])
        if have != sorted(p["name"] for p in want["parameters"]):
            items.append({"kind": "job", "operation": o["name"], "problem": f"parameters {have}"})
        if s.max_concurrent_runs != 1:
            items.append({"kind": "job", "operation": o["name"], "problem": f"max_concurrent_runs {s.max_concurrent_runs}"})
        if not s.timeout_seconds:
            items.append({"kind": "job", "operation": o["name"], "problem": "no timeout"})
        nb = ((s.tasks or [None])[0].notebook_task.notebook_path if s.tasks and s.tasks[0].notebook_task else "") or ""
        if not nb.endswith(f"jobs/G9/ops/{o['name']}"):
            items.append({"kind": "job", "operation": o["name"], "problem": f"runs {nb or 'no notebook'}"})
    return _result(items)


def non_json_results(ctx):
    """AR-4.1: every operation, run through the server with its default arguments, succeeds and returns JSON."""
    t = _tests(ctx)
    items = [{"kind": "tools" if "with default" not in x["test"] else "result", "test": x["test"], "problem": x.get("problem")}
             for x in t["tests"] if "with default" in x["test"] and not x["passed"]]
    if not any("with default" in x["test"] for x in t["tests"]):
        items += [{"kind": "app", "test": x["test"], "problem": x.get("problem")} for x in t["tests"] if not x["passed"]]
    return _result(items)


def tool_differences(ctx):
    """AR-4.2: the server lists exactly the approved tools with their descriptions and typed inputs, plus the status
    tools."""
    t, spec = _tests(ctx), plan(ctx)
    listed = t.get("listed") or {}
    if not listed:
        return _result([{"kind": "app", "problem": next((x.get("problem") for x in t["tests"] if not x["passed"]), "no tools listed")}])
    items = []
    for name, want in _expected_tools(spec).items():
        x = listed.get(name)
        if not x:
            items.append({"kind": "tools", "tool": name, "problem": "not listed"})
            continue
        if x.get("description") != want["description"]:
            items.append({"kind": "tools", "tool": name, "problem": "description differs from the approved one"})
        props = (x.get("inputSchema") or {}).get("properties") or {}
        for p, s in want["properties"].items():
            have = {k: props.get(p, {}).get(k) for k in s}
            if have != s:
                items.append({"kind": "tools", "tool": name, "parameter": p, "problem": f"input {have}, approved {s}"})
        extra = sorted(set(props) - set(want["properties"]))
        if extra:
            items.append({"kind": "tools", "tool": name, "problem": f"unapproved inputs {extra}"})
        if sorted((x.get("inputSchema") or {}).get("required") or []) != want["required"]:
            items.append({"kind": "tools", "tool": name, "problem": "required inputs differ"})
    items += [{"kind": "tools", "tool": n, "problem": "status tool not listed"} for n in STATUS_TOOLS if n not in listed]
    items += [{"kind": "tools", "tool": n, "problem": "not an approved tool"} for n in listed
              if n not in _expected_tools(spec) and n not in STATUS_TOOLS]
    return _result(items, listed=sorted(listed))


def unsafe_operations(ctx):
    """AR-4.3: operations that change data default to a dry run (mode=validate), calls wait a bounded time, a repeated
    call does not start a second run, unknown arguments are rejected and the status tools work."""
    spec, t = plan(ctx), _tests(ctx)
    items = []
    for o in spec["operations"]:
        if o["writes"]:
            m = next((p for p in o["parameters"] if p["name"] == "mode"), None)
            if not m or m.get("default") != "validate":
                items.append({"kind": "tools", "operation": o["name"], "problem": "no mode parameter defaulting to validate"})
    if int(spec["max_wait_seconds"]) > 280:
        items.append({"kind": "tools", "problem": "max_wait_seconds above 280"})
    for x in t["tests"]:
        if not x["passed"] and (x["test"].endswith("called twice while running") or x["test"] in
                                ("get_run_status", "list_recent_runs", "unknown argument rejected", "idempotent start and status tools")):
            items.append({"kind": "tools", "test": x["test"], "problem": x.get("problem") or "failed"})
        if not x["passed"] and "with default" in x["test"] and "dry run" in (x.get("problem") or ""):
            items.append({"kind": "result", "test": x["test"], "problem": x["problem"]})
    return _result(items)


def privilege_gaps(ctx):
    """AR-4.4: OAuth only; the app's service principal holds CAN_MANAGE_RUN on its operation jobs and the app declares
    no other resource; the declared clients may use the app."""
    spec, app, t = plan(ctx), _app(ctx), _tests(ctx)
    if not app:
        return _result([{"kind": "app", "problem": ctx.__dict__.get("_g9", {}).get("app_error", "the app does not exist")}])
    items = []
    sp = app.service_principal_client_id
    want = {f"job_{o['name']}" for o in spec["operations"]}
    have = {r.name: r for r in app.resources or []}
    items += [{"kind": "app", "resource": n, "problem": "the app declares a resource that is not an operation job"}
              for n in have if n not in want]
    items += [{"kind": "app", "resource": n, "problem": "missing"} for n in want if n not in have]
    for n, r in have.items():
        if n in want and (not r.job or r.job.permission.value != "CAN_MANAGE_RUN"):
            items.append({"kind": "app", "resource": n, "problem": f"permission {r.job.permission.value if r.job else None}"})
    for o in spec["operations"]:
        jid = _job_id(ctx, o)
        try:
            acl = ctx.ws.client.permissions.get("jobs", str(jid)).access_control_list or []
        except Exception as e:
            items.append({"kind": "privilege", "operation": o["name"], "problem": f"cannot read job permissions: {e}"})
            continue
        lv = [p.permission_level.value for a in acl if a.service_principal_name == sp for p in a.all_permissions or []
              if not p.inherited]
        if lv != ["CAN_MANAGE_RUN"]:
            items.append({"kind": "privilege", "operation": o["name"], "problem": f"the app's service principal holds {lv or 'nothing'}"})
    try:
        acl = ctx.ws.client.permissions.get("apps", app.name).access_control_list or []
        users = {a.group_name or a.user_name or a.service_principal_name for a in acl
                 if any(p.permission_level.value in ("CAN_USE", "CAN_MANAGE") for p in a.all_permissions or [])}
        items += [{"kind": "privilege", "principal": p, "problem": "may not use the app"} for p in spec["clients"] if p not in users]
    except Exception as e:
        items.append({"kind": "privilege", "problem": f"cannot read app permissions: {e}"})
    for x in t["tests"]:
        if not x["passed"] and ("token rejected" in x["test"] or "OAuth" in x["test"]):
            items.append({"kind": "app", "test": x["test"], "problem": x.get("problem") or f"status {x.get('status')}"})
    return _result(items, service_principal=sp)


def app_not_running(ctx):
    """The MCP server app runs the delivered source."""
    app = _app(ctx)
    if not app:
        return _result([{"kind": "app", "problem": "the app does not exist"}])
    items = []
    if not app.compute_status or app.compute_status.state.value != "ACTIVE":
        items.append({"kind": "app", "problem": f"compute {app.compute_status.state.value if app.compute_status else None}"})
    d = app.active_deployment
    if not d or not d.status or d.status.state.value != "SUCCEEDED":
        items.append({"kind": "app", "problem": f"deployment {d.status.state.value if d and d.status else 'none'}"})
    elif not (d.source_code_path or "").rstrip("/").endswith("jobs/G9/app"):
        items.append({"kind": "app", "problem": f"runs {d.source_code_path}, not the delivered source"})
    return _result(items, url=app.url)


def failed_recent_runs(ctx):
    """Operation runs that failed in the last 7 days (informational)."""
    import time
    since = int((time.time() - 7 * 86400) * 1000)
    items = []
    for o in plan(ctx)["operations"]:
        jid = _job_id(ctx, o)
        try:
            for r in ctx.ws.client.jobs.list_runs(job_id=int(jid), start_time_from=since, completed_only=True, limit=25):
                if r.state and r.state.result_state and r.state.result_state.value not in ("SUCCESS",):
                    items.append({"operation": o["name"], "run_id": r.run_id, "result": r.state.result_state.value})
        except Exception:
            continue
    return _result(items)
