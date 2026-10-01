"""G2 gates. Neither item is editable: the KPI definitions YAML is the single source of truth, so a change is made
there and re-planned, never patched at a gate."""
from .common import definitions


def review(ctx, data):
    plan = ctx.read_artefact("plan.json") or {}
    planned = set(plan.get("new", []) + plan.get("changed", []))
    defs = {d["full_name"]: d for d in definitions(ctx)}
    out, seen = [], set()
    for r in data:
        fn = r["metric_view"]
        if fn in seen:
            out.append(f"{fn} listed twice")
        seen.add(fn)
        if fn not in planned:
            out.append(f"{fn} is not a metric view planned for this run")
            continue
        want = sorted(m["name"] for m in defs[fn]["measures"])
        if sorted(m["name"] for m in r["measures"]) != want:
            out.append(f"{fn}: measures shown {sorted(m['name'] for m in r['measures'])} differ from the definition {want}")
    out += [f"{fn} is planned for this run but missing from the review" for fn in sorted(planned - seen)]
    return out


def measure_results(ctx, data):
    want = {(d["full_name"], m["name"]) for d in definitions(ctx) for m in d["measures"]}
    got = [(r["metric_view"], r["measure"]) for r in data]
    out = [f"{v}.{m}: result listed twice" for v, m in sorted({x for x in got if got.count(x) > 1})]
    out += [f"{v}.{m}: no result for this measure" for v, m in sorted(want - set(got))]
    out += [f"{v}.{m}: not a defined measure" for v, m in sorted(set(got) - want)]
    out += [f"{r['metric_view']}.{r['measure']}: does not match its reference ({r.get('problem')})"
            for r in data if not r["ok"]]
    return out
