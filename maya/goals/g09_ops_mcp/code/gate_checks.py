"""G9 gates. Neither item is editable: the operations file, the notebooks and the goal inputs are the source of truth."""
from .common import plan


def ops_plan(ctx, data):
    want = plan(ctx)
    out = [f"{k} differs from the planned operations" for k in ("app_name", "operations", "clients", "max_wait_seconds")
           if data.get(k) != want.get(k)]
    if not data["operations"]:
        out.append("the plan has no operations")
    out += [f"{o['name']}: {p}" for o in data["operations"] for p in o.get("problems") or []]
    out += [f for f in data.get("findings") or [] if "returned no" in f or "used twice" in f or "clashes" in f]
    if not data["clients"]:
        out.append("nobody may call the server: declare maya.yaml agent_identity or G9 clients")
    return out


def mcp_tests(ctx, data):
    out = [f"{t['test']}: {t.get('problem') or 'failed'}" for t in data["tests"] if not t["passed"]]
    if not data["tests"]:
        out.append("the server was not tested")
    return out
