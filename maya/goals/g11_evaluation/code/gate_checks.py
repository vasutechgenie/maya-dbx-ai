"""G11 gates. Neither item is editable: the questions file and the goal inputs are the source of truth."""
from .common import plan


def eval_plan(ctx, data):
    want = plan(ctx)
    out = [f"{k} differs from the planned evaluation" for k in ("questions", "thresholds", "targets", "regression", "version")
           if data.get(k) != want.get(k)]
    out += list(data.get("problems") or [])
    return list(dict.fromkeys(out))


def eval_results(ctx, data):
    out = []
    for t, s in (data.get("summary") or {}).items():
        if not s["passed"]:
            out.append(f"{t}: {s['correct']}/{s['questions']} correct, below the threshold {s['threshold']:.0%}")
    if not data.get("summary"):
        out.append("the evaluation did not run")
    return out
