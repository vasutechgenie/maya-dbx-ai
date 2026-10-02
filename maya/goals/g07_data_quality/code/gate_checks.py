"""G7 gates. Neither item is editable: the rules file and the goal inputs are the source of truth (a suggestion the data
owner turns down goes to the rules file's 'reject' list), so a change is made there and re-planned."""
from .common import plan


def quality_plan(ctx, data):
    want = plan(ctx)
    out = [f"{k} differs from the planned rule set"
           for k in ("marker", "schema", "rules", "freshness", "schedule", "recipients", "dashboard", "readers")
           if data.get(k) != want.get(k)]
    if not data["rules"]:
        out.append("the plan has no rules")
    out += [f"rule {r['id']} has no dry-run result" for r in data["rules"] if "passed" not in (r.get("baseline") or {})]
    return out


def alert_tests(ctx, data):
    out = [f"alert {t['alert']} did not trigger on its test row (state {t.get('test_state')})"
           for t in data.get("tests") or [] if t.get("test_state") != "TRIGGERED"]
    if not data.get("tests"):
        out.append("no alert was tested")
    return out
