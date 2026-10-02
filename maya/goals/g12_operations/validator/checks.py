"""G12 checks. They read the production jobs, the monitoring dashboard and the documents back from the workspace, so
they certify what operators and users actually find: jobs that run on their own, retry and tell someone when they
fail; a published dashboard whose every query runs; and the documents, as approved, covering what they must."""
from maya.core import bundle
from maya.core.workspace import SqlError

from maya.goals.g06_dashboards.code.common import dashboard_permissions, find_dashboard, published

from ..code.common import doc_path, doc_problems, job_problems, kind_of, plan, production_jobs


def _result(items, **extra):
    return {"observed": len(items), "evidence": {"items": items[:200], **extra}}


def _review(ctx) -> dict:
    cache = ctx.__dict__.setdefault("_g12", {"jobs": [], "documents": {}, "monitoring": []})
    ctx.write_artefact("operations_review.json", cache)
    return cache


def _jobs(ctx) -> list[dict]:
    cache = ctx.__dict__.setdefault("_g12", {"jobs": [], "documents": {}, "monitoring": []})
    if not cache.get("_jobs_read"):
        spec = plan(ctx)
        cache["jobs"] = [{"name": j["name"], "owner": j.get("owner"), "trigger": j.get("trigger"),
                          "recent_runs": j.get("recent_runs") or [],
                          "problems": job_problems(j, spec["notify"], spec["min_retries"])} for j in production_jobs(ctx)]
        cache["_jobs_read"] = True
    return cache["jobs"]


def jobs_not_operable(ctx):
    """AR-7.1: every production job runs on its own (schedule, trigger or continuous), retries failed tasks and
    notifies the declared owners when it fails."""
    jobs = _jobs(ctx)
    _review(ctx)
    items = [{"kind": "job", "job": j["name"], "owner": j["owner"], "problem": p} for j in jobs for p in j["problems"]]
    if not jobs:
        items.append({"kind": "job", "problem": "no production jobs found"})
    return _result(items, jobs=[{k: j[k] for k in ("name", "owner", "trigger")} for j in jobs])


def monitoring_gaps(ctx):
    """AR-7.3: the monitoring dashboard is published as approved, every dataset query runs, the operators may view
    it, and the agents endpoint records its requests in an inference table."""
    spec = plan(ctx)
    d, items = spec["dashboard"], []
    did = find_dashboard(ctx, d)
    if not did:
        items.append({"kind": "dashboard", "problem": f"dashboard {d['title']!r} not found"})
    else:
        if not published(ctx, did):
            items.append({"kind": "dashboard", "problem": "not published"})
        perms = dashboard_permissions(ctx, did)
        items += [{"kind": "dashboard", "problem": f"{g} may not view it"} for g in d["viewers"]
                  if not perms.get(g, set()) & {"CAN_RUN", "CAN_EDIT", "CAN_MANAGE"}]
    for ds in d["datasets"]:
        try:
            ctx.ws.sql(f"SELECT * FROM ({ds['sql']}) AS maya_check LIMIT 1")
        except SqlError as e:
            items.append({"kind": "dashboard", "dataset": ds["name"], "problem": str(e).splitlines()[0][:300]})
    if (ctx.system.goal_settings("G10") or {}) and not d.get("inference_table"):
        items.append({"kind": "endpoint", "problem": "the agents endpoint records no inference table"})
    _review(ctx)["monitoring"] = [i["problem"] for i in items]
    _review(ctx)
    return _result(items, dashboard_id=did, datasets=[x["name"] for x in d["datasets"]])


def _published_docs(ctx, kinds) -> list[dict]:
    spec = plan(ctx)
    root = (bundle.summary(ctx.system, ctx.ws).get("workspace") or {}).get("file_path")
    items = []
    review = ctx.__dict__.setdefault("_g12", {"jobs": [], "documents": {}, "monitoring": []})
    for doc_id, doc in sorted(spec["documents"].items()):
        if kind_of(doc_id) not in kinds:
            continue
        problems = list(spec["doc_problems"].get(doc_id) or [])
        path = f"{root}/jobs/{ctx.goal.id}/{doc_path(doc_id)}"
        try:
            live = ctx.ws.client.workspace.download(path).read().decode()
            if live.strip() != doc["markdown"].strip():
                problems.append("the workspace copy differs from the approved document")
            problems += [p for p in doc_problems(doc_id, live, []) if p not in problems]
        except Exception as e:
            problems.append(f"not in the workspace at {path}: {str(e)[:120]}")
        review["documents"][doc_id] = problems
        items += [{"kind": "document", "document": doc_id, "problem": p} for p in problems]
    _review(ctx)
    return items


def runbook_gaps(ctx):
    """AR-7.4: a runbook for every production job and served component, in the workspace, with what it does, when
    it runs, what to do when it fails, how to rerun it and whom to escalate to."""
    spec = plan(ctx)
    items = _published_docs(ctx, {"runbook"})
    have = {i.split(":", 1)[1] for i in spec["documents"] if kind_of(i) == "runbook"}
    items += [{"kind": "document", "problem": f"no runbook for production job {j['name']}"}
              for j in spec["jobs"] if not j.get("missing") and j["key"] not in have]
    return _result(items)


def product_doc_gaps(ctx):
    """AR-8.1: the product documentation in the workspace covers the data, metrics, AI access, quality, evaluation
    and owners."""
    return _result(_published_docs(ctx, {"product"}))


def onboarding_gaps(ctx):
    """AR-8.2: the onboarding page in the workspace tells a new user how to get access, ask questions and get help."""
    return _result(_published_docs(ctx, {"onboarding"}))


def recent_failures(ctx):
    """Production jobs whose recent runs failed (informational)."""
    return _result([{"job": j["name"], "run_id": r["run_id"], "result": r["result"]} for j in _jobs(ctx)
                    for r in j["recent_runs"] if r.get("result") in ("FAILED", "TIMEDOUT", "INTERNAL_ERROR")])
