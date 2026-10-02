"""G10 gates. Neither item is editable: the agents file and the goal inputs are the source of truth."""
from .common import plan


def agents_plan(ctx, data):
    want = plan(ctx)
    out = [f"{k} differs from the planned agents" for k in ("endpoint", "model", "users", "runtime", "routing_examples")
           if data.get(k) != want.get(k)]
    out += list(data.get("problems") or [])
    if not data.get("runtime"):
        out.append("the plan has no agent configuration")
    out += [f"dry run: '{t['question']}' went to {t.get('routes') or t.get('error')}, expected {t['expected']}"
            for t in data.get("dry_run") or [] if not t.get("passed")]
    return list(dict.fromkeys(out))


def agent_tests(ctx, data):
    out = [f"'{t['question']}': {t.get('problem') or 'failed'}" for t in data["tests"] if not t["passed"]]
    if not data["tests"]:
        out.append("the endpoint was not tested")
    return out
