"""G2 checks. They read Unity Catalog back and query the views, so they certify what is actually in the workspace."""
from concurrent.futures import ThreadPoolExecutor

from ..code.common import body, definitions, metric_views, read_back, same_body
from ..code.compare import check_measure

MIN_COMMENT = 20


def _result(items):
    return {"observed": len(items), "evidence": {"items": items[:200]}}


def missing_metric_views(ctx):
    live = metric_views(ctx)
    return _result([{"metric_view": d["full_name"], "kind": "missing", "problem": "not a metric view in Unity Catalog"}
                    for d in definitions(ctx) if d["full_name"] not in live])


def definition_differences(ctx):
    items = []
    for d in definitions(ctx):
        stored = read_back(ctx, d["full_name"])
        if stored is None:
            continue  # reported by missing_metric_views
        items += [{"metric_view": d["full_name"], "kind": "definition", "problem": x}
                  for x in same_body(stored, body(d))]
    return _result(items)


def unfriendly_fields(ctx):
    items = []
    for d in definitions(ctx):
        stored = read_back(ctx, d["full_name"]) or {}
        for kind in ("dimensions", "measures"):
            for f in stored.get(kind) or []:
                need = ["display_name", "comment", "synonyms"] + (["format"] if kind == "measures" else [])
                gaps = [k for k in need if not f.get(k)]
                if gaps:
                    items.append({"metric_view": d["full_name"], "field": f["name"], "kind": "friendly",
                                  "problem": f"{kind[:-1]} {f['name']} has no {', '.join(gaps)}"})
    return _result(items)


def mismatched_measures(ctx):
    tol = ctx.inputs.get("tolerance", 1e-6)
    jobs = [(d["full_name"], m) for d in definitions(ctx) for m in d["measures"]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda j: check_measure(ctx.ws, j[0], j[1], tol), jobs))
    ctx.write_artefact("measure_results.json", results)
    return {"observed": sum(not r["ok"] for r in results),
            "evidence": {"items": [{"metric_view": r["metric_view"], "measure": r["measure"], "kind": "measure",
                                    "problem": r["problem"], "mismatches": r.get("mismatches")}
                                   for r in results if not r["ok"]],
                         "verified": [{"metric_view": r["metric_view"], "measure": r["measure"], "by": r["by"],
                                       "groups": r.get("groups"), "value": r.get("value"),
                                       "max_relative_difference": r.get("max_relative_difference")}
                                      for r in results if r["ok"]]}}


def undescribed_metric_views(ctx):
    live = metric_views(ctx)
    items = []
    for d in definitions(ctx):
        text = (live.get(d["full_name"]) or {}).get("comment") or ""
        if len(text.strip()) < MIN_COMMENT:
            items.append({"metric_view": d["full_name"], "kind": "description",
                          "problem": "no description" if not text else f"description shorter than {MIN_COMMENT} characters"})
    return _result(items)


def reviewer_warnings(ctx):
    return _result([{"metric_view": i["metric_view"], "target": f["target"], "problem": f["issue"]}
                    for i in ctx.read_artefact("review.json") or [] for f in i["findings"] if f["severity"] == "warning"])
