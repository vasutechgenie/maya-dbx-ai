"""G12 gates. Neither item is editable: the facts come from what the earlier goals certified."""
from .common import plan


def operations_plan(ctx, data):
    want = plan(ctx)
    out = [f"{k} differs from the planned operations" for k in ("documents", "dashboard", "notify", "version")
           if data.get(k) != want.get(k)]
    out += list(data.get("problems") or [])
    return list(dict.fromkeys(out))


def operations_review(ctx, data):
    out = [f"production job {j['name']}: {p}" for j in data.get("jobs") or [] for p in j.get("problems") or []]
    out += [f"document {d}: {p}" for d, ps in (data.get("documents") or {}).items() for p in ps]
    out += [f"monitoring: {p}" for p in data.get("monitoring") or []]
    return out
