"""G5 checks. They read the space back from the workspace and ask Genie the benchmark questions, so they certify
what users actually get."""
import math
import time

from ..code.common import (canonical, definition, differences, find_space, layer, live_space, serialized,
                           space_permissions, sql_problem)

ORDER = {"CAN_READ": 0, "CAN_RUN": 1, "CAN_EDIT": 2, "CAN_MANAGE": 3}


def _result(items, **extra):
    return {"observed": len(items), "evidence": {"items": items[:200], **extra}}


def _live(ctx):
    """(space id, serialized space) of the live space, read once per validation."""
    cache = ctx.__dict__.setdefault("_g5_live", {})
    if "id" not in cache:
        sid = find_space(ctx)
        cache["id"], cache["space"] = sid, (live_space(ctx, sid)["serialized"] if sid else None)
    return cache["id"], cache["space"]


def missing_space(ctx):
    sid, _ = _live(ctx)
    return _result([] if sid else [{"kind": "missing", "problem": f"no Genie space carries {definition(ctx)['marker']}"}],
                   space_id=sid)


def space_differences(ctx):
    sid, live = _live(ctx)
    if not sid:
        return _result([])
    return _result([{"kind": "definition", "problem": d} for d in differences(serialized(definition(ctx)), live)])


def uncurated_sources(ctx):
    _, live = _live(ctx)
    if live is None:
        return _result([])
    items = []
    for fn in canonical(live)["sources"]:
        lay = layer(ctx, fn)
        if lay == "bronze" or lay is None or (lay == "silver" and not ctx.inputs.get("allow_silver")):
            items.append({"kind": "source", "source": fn, "problem": f"source is {lay or 'outside the foundation'}"})
    return _result(items)


def instruction_gaps(ctx):
    _, live = _live(ctx)
    if live is None:
        return _result([])
    text = canonical(live)["instructions"].lower()
    space = definition(ctx)
    ctxfile = ctx.read_artefact("context.json") or {}
    need = [("rule", r) for r in ctxfile.get("rules") or []]
    for s in ctxfile.get("sources") or []:
        need += [("KPI", m.get("display_name") or m["name"]) for m in s.get("measures") or []]
    need += [("page", p["name"]) for p in space["pages"]]
    items = [{"kind": "instructions", "problem": f"{kind} not in the instructions: {what!r}"}
             for kind, what in need if " ".join(what.lower().split()) not in text]
    return _result(items)


def thin_pages(ctx):
    _, live = _live(ctx)
    if live is None:
        return _result([])
    asked = set(canonical(live)["sample_questions"])
    minimum = ctx.inputs.get("min_questions_per_page", 5)
    items = []
    for p in definition(ctx)["pages"]:
        n = sum(1 for q in definition(ctx)["sample_questions"]
                if q["page"] == p["id"] and " ".join(q["question"].split()) in asked)
        if n < minimum:
            items.append({"kind": "questions", "page": p["id"], "problem": f"{n} sample questions; at least {minimum}"})
    return _result(items)


def benchmark_gaps(ctx):
    space = definition(ctx)
    need = ctx.inputs.get("min_benchmarks", 10)
    items = [] if len(space["benchmarks"]) >= need else [
        {"kind": "benchmarks", "problem": f"{len(space['benchmarks'])} benchmark questions; at least {need}"}]
    for b in space["benchmarks"]:
        why = sql_problem(ctx, b["sql"], space["sources"])
        if why:
            items.append({"kind": "benchmarks", "question": b["question"], "problem": f"expected answer SQL {why}"})
    return _result(items)


def _eval(ctx, sid):
    api = ctx.ws.client.api_client
    base = f"/api/2.0/genie/spaces/{sid}/eval-runs"
    run = api.do("POST", base, body={})
    rid = run["eval_run_id"]
    deadline = time.time() + 60 * ctx.inputs.get("benchmark_timeout_minutes", 30)
    while run.get("eval_run_status") not in ("DONE", "FAILED", "CANCELLED"):
        if time.time() > deadline:
            raise RuntimeError(f"benchmark run {rid} did not finish in {ctx.inputs.get('benchmark_timeout_minutes', 30)} minutes")
        time.sleep(10)
        run = api.do("GET", f"{base}/{rid}")
    if run["eval_run_status"] != "DONE":
        raise RuntimeError(f"benchmark run {rid} ended {run['eval_run_status']}")
    results, token = [], None
    while True:
        page = api.do("GET", f"{base}/{rid}/results", query={"page_size": 100, **({"page_token": token} if token else {})})
        results += page.get("eval_results") or []
        token = page.get("next_page_token")
        if not token:
            break
    out = []
    for r in results:
        d = api.do("GET", f"{base}/{rid}/results/{r['result_id']}")
        genie = next((x.get("response") for x in d.get("actual_response") or [] if x.get("response_type") == "SQL"), None)
        out.append({"question": r.get("question"), "assessment": d.get("assessment") or "UNKNOWN",
                    "reasons": d.get("assessment_reasons") or [], "expected_sql": r.get("benchmark_answer"),
                    "genie_sql": genie})
    return rid, out


def benchmark_shortfall(ctx):
    sid, _ = _live(ctx)
    if not sid:
        return _result([])
    threshold = ctx.inputs.get("pass_threshold", 0.9)
    rid, results = _eval(ctx, sid)
    correct = sum(r["assessment"] == "GOOD" for r in results)
    needed = math.ceil(threshold * len(results))
    ctx.write_artefact("benchmark_results.json", {"space_id": sid, "eval_run_id": rid, "questions": len(results),
                                                  "correct": correct, "needed": needed, "threshold": threshold,
                                                  "results": results})
    return {"observed": max(0, needed - correct),
            "evidence": {"items": [{"kind": "benchmark", "question": r["question"], "problem": r["assessment"],
                                    "reasons": r["reasons"]} for r in results if r["assessment"] != "GOOD"][:200],
                         "correct": correct, "questions": len(results), "needed": needed, "eval_run_id": rid}}


def failing_trusted_sql(ctx):
    space = definition(ctx)
    return _result([{"kind": "trusted_sql", "question": t["question"], "problem": why}
                    for t in space["trusted_sql"] if (why := sql_problem(ctx, t["sql"], space["sources"]))])


def missing_access(ctx):
    sid, _ = _live(ctx)
    if not sid:
        return _result([])
    held = space_permissions(ctx, sid)
    items = []
    for a in definition(ctx).get("access") or []:
        who, level = a.get("group") or a.get("service_principal"), a.get("level", "CAN_RUN")
        if max((ORDER.get(x, -1) for x in held.get(who, ())), default=-1) < ORDER[level]:
            items.append({"kind": "access", "principal": who, "problem": f"does not hold {level}"})
    return _result(items)


def untrusted_critical_questions(ctx):
    space = definition(ctx)
    trusted = {t["question"] for t in space["trusted_sql"]}
    return _result([{"page": q["page"], "question": q["question"], "problem": "critical question without trusted SQL"}
                    for q in space["sample_questions"] if q.get("critical") and q["question"] not in trusted])


def benchmarks_needing_review(ctx):
    b = ctx.read_artefact("benchmark_results.json") or {}
    return _result([{"question": r["question"], "problem": r["assessment"]} for r in b.get("results") or []
                    if r["assessment"] in ("NEEDS_REVIEW", "UNKNOWN")])
