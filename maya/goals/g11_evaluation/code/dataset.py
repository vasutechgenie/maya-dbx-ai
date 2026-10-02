"""G11 load and plan: the business owner's evaluation questions, each truth SQL run on the governed metric views, the
measures no question covers, and more questions proposed by the eval_designer (checked, shown, never scored until the
business owner adds them to the questions file)."""
from concurrent.futures import ThreadPoolExecutor

from maya.core.spec import stable_hash
from maya.core.workspace import SqlError

from maya.goals.g05_genie_space.code.common import referenced

from . import ledger
from .common import certified_agents, metric_views, questions_file, schema_name

KEYS = ("questions", "schema", "pass_threshold", "genie_pass_threshold", "targets", "judge_endpoint", "min_questions",
        "regression")


def _context(ctx) -> dict:
    return ctx.read_artefact("context.json") or {}


def _allowed(ctx, views) -> set[str]:
    return {v["name"] for v in views} | {s for v in views for s in v["sources"]}


def check_sql(ctx, sql, allowed) -> dict:
    """{rows, preview} when the SQL runs and reads only governed sources; {problem} otherwise."""
    outside = sorted(referenced(sql) - allowed)
    if outside:
        return {"problem": f"reads objects that are not metric views or their sources: {', '.join(outside)}"}
    try:
        rows = ctx.ws.sql(f"SELECT * FROM ({sql.strip().rstrip(';')}) AS maya_truth LIMIT 51")
    except SqlError as e:
        return {"problem": str(e).splitlines()[0][:300]}
    if not rows:
        return {"problem": "returns no rows (the question has no answer in the data)"}
    if len(rows) > 50:
        return {"problem": "returns more than 50 rows; ask a question with a short answer"}
    return {"rows": len(rows), "preview": rows[:5]}


def coverage(views, questions) -> dict:
    text = " ".join(x["sql"].lower() for x in questions)
    out = {}
    for v in views:
        short = v["name"].rsplit(".", 1)[-1].lower()
        used = short in text
        out[v["name"]] = {"used": used, "uncovered_measures": [m for m in v["measures"] if not used or m.lower() not in text]}
    return out


def load(ctx):
    data, errors = questions_file(ctx)
    if errors:
        raise ValueError("; ".join(errors))
    targets = ctx.inputs.get("targets") or ["agent", "genie"]
    agents = certified_agents(ctx) if "agent" in targets else None
    if "agent" in targets and not agents:
        raise ValueError("the agents (G10) are not certified")
    if "genie" in targets and not (ctx.system.goal_settings("G5") or {}).get("title"):
        raise ValueError("G5 configures no Genie space")
    views = metric_views(ctx)
    allowed = _allowed(ctx, views)
    for x in data["questions"]:
        x["targets"] = [t for t in x.get("targets") or targets if t in targets]
    with ThreadPoolExecutor(max_workers=6) as pool:
        checked = list(pool.map(lambda x: check_sql(ctx, x["sql"], allowed), data["questions"]))
    cov = coverage(views, data["questions"])
    h = stable_hash({"questions": data["questions"], "agents": (agents or {}).get("version"), "views": views})
    cert = ledger.certified_plan(ctx) or {}
    reused = cert.get("suggestions") if cert.get("suggestions_hash") == h else None
    ctx.write_artefact("context.json", {"data": data, "checked": checked, "views": views, "coverage": cov,
                                        "agents": {k: (agents or {}).get(k) for k in ("version", "endpoint", "routing_examples")},
                                        "sub_agents": [{"name": s["name"], "description": s.get("routing_description")}
                                                       for s in ((agents or {}).get("design") or {}).get("sub_agents") or []],
                                        "hash": h, "reused": reused})
    bad = sum("problem" in c for c in checked)
    ctx.log(f"     {len(data['questions'])} questions ({bad} with problems); suggestions "
            f"{'reused from certification' if reused is not None else 'to write'}")
    return {"dataset": [] if reused is not None or not ctx.inputs.get("max_suggestions", 5) else ["system"]}


def load_inputs_hash(ctx) -> str | None:
    data, errors = questions_file(ctx)
    if errors:
        return None
    return stable_hash({"questions": data, "inputs": {k: ctx.inputs.get(k) for k in KEYS}})


def designer_input(ctx, _item):
    c = _context(ctx)
    return {"questions": [{"question": x["question"], "tags": x.get("tags") or []} for x in c["data"]["questions"]],
            "metric_views": [{k: v[k] for k in ("name", "comment", "dimensions", "measures")} for v in c["views"]],
            "uncovered": {k: v["uncovered_measures"] for k, v in c["coverage"].items() if v["uncovered_measures"]},
            "sub_agents": c["sub_agents"], "max_suggestions": ctx.inputs.get("max_suggestions", 5),
            "sql_rules": "Truth SQL reads only the metric views, with MEASURE(<measure>) for measures and GROUP BY for "
                         "dimensions, filters every period explicitly with dates, and returns at most 50 rows."}


def plan(ctx):
    c = _context(ctx)
    data, views = c["data"], c["views"]
    allowed = _allowed(ctx, views)
    proposed = c["reused"] if c["reused"] is not None else (next(iter(ctx.read_artefact("suggestions.json") or []), None)
                                                            or {}).get("suggestions") or []
    known = {x["question"].strip().lower() for x in data["questions"]}
    suggestions = []
    for s in proposed:
        if s["question"].strip().lower() in known:
            continue
        r = check_sql(ctx, s["sql"], allowed)
        suggestions.append({**s, **{k: r[k] for k in ("problem", "rows") if k in r}})
    targets = ctx.inputs.get("targets") or ["agent", "genie"]
    thr = ctx.inputs.get("pass_threshold", 0.9)
    questions = [{"id": x["id"], "question": x["question"], "sql": x["sql"].strip(), "targets": x["targets"],
                  "tags": x.get("tags") or [], **{k: ck[k] for k in ("rows", "problem") if k in ck}}
                 for x, ck in zip(data["questions"], c["checked"])]
    need = int(ctx.inputs.get("min_questions", 20))
    problems = [f"'{x['question']}': {x['problem']}" for x in questions if x.get("problem")]
    if len(questions) < need:
        problems.append(f"{len(questions)} questions; at least {need}")
    reg = ctx.inputs.get("regression") or {}
    agents = c["agents"]
    g5 = ctx.system.goal_settings("G5") or {}
    spec = {"schema": schema_name(ctx, ctx.inputs.get("schema", "maya_eval")), "targets": targets,
            "thresholds": {"agent": thr, "genie": ctx.inputs.get("genie_pass_threshold", thr)},
            "endpoint": agents.get("endpoint"), "agent_version": agents.get("version"),
            "genie_title": g5.get("title"),
            "judge_endpoint": ctx.inputs.get("judge_endpoint") or "databricks-claude-sonnet-4-6",
            "regression": {"on_change": reg.get("on_change", True), "schedule": reg.get("schedule"),
                           "timezone": reg.get("timezone", "UTC"), "notify": list(reg.get("notify") or []),
                           "tables": sorted({s for v in views for s in v["sources"]})},
            "questions": questions, "coverage": c["coverage"], "suggestions": suggestions,
            "suggestions_raw": proposed, "suggestions_hash": c["hash"], "problems": problems,
            "inputs_hash": load_inputs_hash(ctx)}
    spec["suggestions"] = suggestions
    spec["version"] = stable_hash({k: spec[k] for k in ("questions", "thresholds", "targets", "judge_endpoint", "regression",
                                                         "endpoint", "genie_title", "schema")})[:12]
    ctx.write_artefact("eval_plan.json", spec)
    unc = sum(len(v["uncovered_measures"]) for v in c["coverage"].values())
    ctx.log(f"     plan: {len(questions)} questions on {targets}; {unc} measures uncovered; "
            f"{len(suggestions)} suggestions; {len(problems)} problems")
    return {"questions": len(questions), "problems": len(problems)}
