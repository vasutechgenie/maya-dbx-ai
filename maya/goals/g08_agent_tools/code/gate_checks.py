"""G8 gates. Neither item is editable: the tools file and the goal inputs are the source of truth, so a change is made
there and re-planned."""
from .common import plan


def tool_plan(ctx, data):
    want = plan(ctx)
    out = [f"{k} differs from the planned tools" for k in ("schema", "tools", "executors", "revoke", "removed")
           if data.get(k) != want.get(k)]
    if not data["tools"]:
        out.append("the plan has no tools")
    out += [f"{t['name']}: {p}" for t in data["tools"] for p in t.get("problems") or []]
    out += [f for f in data.get("findings") or [] if "returned no draft" in f]
    if not data["executors"]:
        out.append("nobody may run the tools: declare maya.yaml agent_identity or G8 executors")
    return out


def tool_tests(ctx, data):
    out = [f"{t['tool']} {t['args']}: {'; '.join(t.get('problems') or ['failed'])}" for t in data["tests"] if not t["passed"]]
    if not data["tests"]:
        out.append("no tool was tested")
    return out
