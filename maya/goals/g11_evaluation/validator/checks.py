"""G11 checks. They read the evaluation run back from the results tables in Unity Catalog and the regression job from
the workspace, so they certify what was recorded and what will keep running: a dataset of business questions with
truth SQL, the agents and the Genie space answering enough of them correctly, and the regression job armed."""
import math

from maya.core import bundle

from ..code.common import JOB_KEY, TABLES, plan, q, table


def _result(items, **extra):
    return {"observed": len(items), "evidence": {"items": items[:200], **extra}}


def _run(ctx) -> dict:
    """The evaluation run MAYA started in apply, from the tables (cached for the validation)."""
    cache = ctx.__dict__.setdefault("_g11", {})
    if "run" in cache:
        return cache["run"]
    spec, applied = plan(ctx), ctx.read_artefact("apply.json") or {}
    rid = applied.get("eval_run_id")
    out = {"eval_run_id": rid, "summary": {}, "results": []}
    if rid:
        from maya.core.workspace import lit
        runs = ctx.ws.sql(f"SELECT * FROM {q(table(spec, 'eval_runs'))} WHERE run_id = {lit(rid)}")
        out["summary"] = {r["target"]: {"questions": int(r["questions"]), "correct": int(r["correct"]),
                                        "pass_rate": float(r["pass_rate"]), "threshold": float(r["threshold"]),
                                        "passed": str(r["passed"]).lower() == "true"} for r in runs}
        out["agent_version"] = runs[0]["agent_version"] if runs else None
        out["dataset_version"] = runs[0]["dataset_version"] if runs else None
        out["results"] = [{k: r[k] for k in ("target", "question_id", "question", "correct", "reason", "error", "answer")}
                          for r in ctx.ws.sql(f"SELECT * FROM {q(table(spec, 'eval_results'))} WHERE run_id = {lit(rid)} "
                                              "ORDER BY target, question_id")]
        for r in out["results"]:
            r["correct"] = str(r["correct"]).lower() == "true"
    out["mlflow_run_id"] = applied.get("mlflow_run_id")
    ctx.write_artefact("eval_results.json", out)
    cache["run"] = out
    return out


def dataset_gaps(ctx):
    """AR-6.1: enough questions, each with truth SQL that runs on the governed metric views; the dataset is stored in
    Unity Catalog at the approved version."""
    spec = plan(ctx)
    need = int(ctx.inputs.get("min_questions", 20))
    items = [] if len(spec["questions"]) >= need else [{"kind": "dataset", "problem": f"{len(spec['questions'])} questions; at least {need}"}]
    items += [{"kind": "dataset", "question": x["question"], "problem": x["problem"]} for x in spec["questions"] if x.get("problem")]
    try:
        rows = ctx.ws.sql(f"SELECT dataset_version, count(*) AS n FROM {q(table(spec, 'eval_dataset'))} GROUP BY ALL")
        stored = {r["dataset_version"]: int(r["n"]) for r in rows}
        if stored.get(spec["version"]) != len(spec["questions"]):
            items.append({"kind": "results", "problem": f"eval_dataset holds {stored}, expected {len(spec['questions'])} "
                                                         f"questions of version {spec['version']}"})
    except Exception as e:
        items.append({"kind": "results", "problem": f"cannot read eval_dataset: {str(e)[:200]}"})
    return _result(items)


def _shortfall(ctx, target):
    spec, run = plan(ctx), _run(ctx)
    if target not in spec["targets"]:
        return {"observed": 0, "evidence": {"items": [], "note": f"{target} is not evaluated"}}
    s = run["summary"].get(target)
    if not s:
        return {"observed": 1, "evidence": {"items": [{"kind": "results", "problem": f"no recorded evaluation of {target}"}]}}
    needed = math.ceil(s["threshold"] * s["questions"] - 1e-9)
    failed = [{"kind": target, "question": r["question"], "problem": r["error"] or r["reason"]}
              for r in run["results"] if r["target"] == target and not r["correct"]]
    return {"observed": max(0, needed - s["correct"]),
            "evidence": {"items": failed, "correct": s["correct"], "questions": s["questions"], "needed": needed,
                         "pass_rate": s["pass_rate"], "eval_run_id": run["eval_run_id"]}}


def agent_shortfall(ctx):
    """AR-6.2: questions the agents must still answer correctly to reach the pass threshold."""
    return _shortfall(ctx, "agent")


def genie_shortfall(ctx):
    """AR-6.2: questions the Genie space must still answer correctly to reach the pass threshold."""
    return _shortfall(ctx, "genie")


def evaluated_other_version(ctx):
    """The recorded run evaluated the approved dataset against the agents certified now."""
    spec, run = plan(ctx), _run(ctx)
    items = []
    if run.get("dataset_version") != spec["version"]:
        items.append({"kind": "results", "problem": f"evaluated dataset version {run.get('dataset_version')}, approved {spec['version']}"})
    if "agent" in spec["targets"] and run.get("agent_version") != spec["agent_version"]:
        items.append({"kind": "results", "problem": f"evaluated agents {run.get('agent_version')}, certified {spec['agent_version']}"})
    return _result(items)


def regression_gaps(ctx):
    """AR-6.3: the regression job exists as approved: it reruns the evaluation when a source table changes (or on a
    schedule) and notifies the owners when it fails."""
    spec = plan(ctx)
    jobs = (bundle.resources(ctx.system, ctx.ws).get("jobs") or {})
    jid = (jobs.get(JOB_KEY) or {}).get("id")
    if not jid:
        return _result([{"kind": "job", "problem": f"the bundle has no job {JOB_KEY}"}])
    s = ctx.ws.client.jobs.get(int(jid)).settings
    reg, items = spec["regression"], []
    if reg["schedule"]:
        if not s.schedule or s.schedule.quartz_cron_expression != reg["schedule"]:
            items.append({"kind": "job", "problem": f"schedule {s.schedule and s.schedule.quartz_cron_expression}, approved {reg['schedule']}"})
    elif reg["on_change"]:
        tu = s.trigger.table_update if s.trigger else None
        if not tu or sorted(tu.table_names or []) != sorted(reg["tables"]):
            items.append({"kind": "job", "problem": f"not triggered by changes to {reg['tables']}"})
    else:
        items.append({"kind": "job", "problem": "neither a schedule nor a trigger on change is configured"})
    have = set((s.email_notifications.on_failure if s.email_notifications else None) or [])
    items += [{"kind": "job", "problem": f"{n} is not notified when the evaluation fails"} for n in reg["notify"] if n not in have]
    if not reg["notify"]:
        items.append({"kind": "job", "problem": "nobody is notified when the evaluation fails (regression.notify)"})
    return _result(items, job_id=jid, paused=str(getattr(s.trigger or s.schedule, "pause_status", None)))


def untracked(ctx):
    """The evaluation run is in MLflow with its pass rates (informational)."""
    run = _run(ctx)
    return _result([] if run.get("mlflow_run_id") else [{"problem": "the evaluation run was not logged to MLflow"}])


def uncovered_measures(ctx):
    """Measures of the metric views that no question asks about (informational)."""
    return _result([{"metric_view": k, "measure": m} for k, v in plan(ctx)["coverage"].items() for m in v["uncovered_measures"]])
