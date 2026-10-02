"""G12 load and plan: the production jobs and their operability, the facts about the data product (read from what the
earlier goals certified), the documents the tech_writer writes from them (product documentation, onboarding page, a
runbook per production job and per served component) and the monitoring dashboard."""
import json
import re
from concurrent.futures import ThreadPoolExecutor

from maya.core import agents
from maya.core.spec import stable_hash
from maya.core.workspace import ident, lit

from . import ledger
from .common import DOC_SECTIONS, _certified, doc_problems, job_problems, kind_of, production_jobs
from .monitoring import dashboard_spec

TASK = "write one document of the data product's documentation from the facts given"
KEYS = ("notify", "production_jobs", "min_retries", "dashboard", "support", "docs")


def _context(ctx) -> dict:
    return ctx.read_artefact("context.json") or {}


def _schema(ctx, name):
    return name if "." in name else f"{ctx.system.catalogs[0]}.{name}"


def facts(ctx, jobs) -> dict:
    sp = ctx.system.spec
    s = lambda g: ctx.system.goal_settings(g) or {}
    from maya.goals.g11_evaluation.code.common import metric_views
    gold = []
    for src in (ctx.system.layers.get("gold") or {}).get("sources") or []:
        try:
            gold += [f"{src['catalog']}.{src['schema']}.{r['table_name']}: {r['comment'] or ''}".strip() for r in ctx.ws.sql(
                f"SELECT table_name, comment FROM {ident(src['catalog'])}.information_schema.tables "
                f"WHERE table_schema = {lit(src['schema'])} ORDER BY table_name")]
        except Exception:
            pass
    g8, g9, g10 = (_certified(ctx, g, t) or {} for g, t in (("G8", "tools_ledger"), ("G9", "ops_ledger"), ("G10", "agents_ledger")))
    g11 = _certified(ctx, "G11", "eval_ledger") or {}
    g11_results = _certified(ctx, "G11", "eval_ledger", "results_json") or {}
    roles = s("G4").get("roles") or {}
    out = {
        "product": {k: sp["metadata"].get(k) for k in ("name", "owner", "description")},
        "owners": (sp.get("certification") or {}).get("approvers") or {},
        "support": ctx.inputs.get("support"),
        "gold_tables": gold,
        "metric_views": [{"name": v["name"], "comment": v["comment"], "measures": v["measures"], "dimensions": v["dimensions"]}
                         for v in metric_views(ctx)],
        "access": {n: {"principals": r.get("principals"), "reads": r.get("read"), "consumer": bool(r.get("consumer"))}
                   for n, r in roles.items()},
        "genie": {"title": s("G5").get("title"), "description": s("G5").get("description"),
                  "who": [a.get("group") or a.get("service_principal") for a in s("G5").get("access") or []]},
        "dashboards": [{"title": s("G6").get("title"), "refresh": s("G6").get("schedule"),
                        "who": [a.get("group") for a in s("G6").get("access") or [] if a.get("group")]},
                       {"title": (ctx.inputs.get("dashboard") or {}).get("title", "Operations monitoring"),
                        "purpose": "operations monitoring (this goal)"}],
        "quality": {"schema": _schema(ctx, s("G7").get("schema", "maya_quality")), "schedule": s("G7").get("schedule"),
                    "recipients": [r.get("user") or r.get("destination") for r in s("G7").get("recipients") or []]},
        "tools": [{"name": t["full_name"], "description": t.get("comment")} for t in g8.get("tools") or []],
        "operations": {"app": g9.get("app_name"), "clients": g9.get("clients"),
                       "tools": [{"name": o["tool"], "description": o.get("description")} for o in g9.get("operations") or []]},
        "agents": {"endpoint": g10.get("endpoint"), "model": g10.get("model"), "users": g10.get("users"),
                   "supervisor": ((g10.get("runtime") or {}).get("supervisor") or {}).get("name"),
                   "sub_agents": [{"name": x["name"], "handles": x.get("routing_description")}
                                  for x in (g10.get("design") or {}).get("sub_agents") or []],
                   "example_questions": [e["question"] for e in g10.get("routing_examples") or []]},
        "evaluation": {"questions": len(g11.get("questions") or []), "thresholds": g11.get("thresholds"),
                       "results": g11_results.get("summary"), "regression": g11.get("regression"),
                       "sample_questions": [x["question"] for x in (g11.get("questions") or [])[:6]]},
        "production_jobs": [{k: j.get(k) for k in ("key", "name", "owner", "description", "trigger", "tasks", "timeout_seconds",
                                                    "notify_on_failure", "missing")} for j in jobs],
    }
    return out


def _documents(f, jobs) -> list[dict]:
    """[{id, kind, subject, must_mention}]: what each document must cover, checked after writing."""
    mv = [v["name"].rsplit(".", 1)[-1] for v in f["metric_views"]]
    ag, gn = f["agents"], f["genie"]
    serve = [x for x in (ag.get("endpoint"), gn.get("title")) if x]
    groups = sorted({p for r in f["access"].values() if r.get("consumer") for p in r.get("principals") or []
                     if not re.fullmatch(r"[0-9a-f-]{36}", p)})
    docs = [{"id": "product", "kind": "product", "subject": f["product"]["name"],
             "must_mention": mv + serve + [t["name"].rsplit(".", 1)[-1] for t in f["tools"]][:8]},
            {"id": "onboarding", "kind": "onboarding", "subject": f["product"]["name"],
             "must_mention": serve + [d["title"] for d in f["dashboards"][:1] if d.get("title")] + groups}]
    for j in jobs:
        if not j.get("missing"):
            docs.append({"id": f"runbook:{j['key']}", "kind": "runbook", "subject": j["name"], "must_mention": [j["name"]]})
    if ag.get("endpoint"):
        docs.append({"id": "runbook:agents_endpoint", "kind": "runbook", "subject": f"agents endpoint {ag['endpoint']}",
                     "must_mention": [ag["endpoint"]]})
    if f["operations"].get("app"):
        docs.append({"id": "runbook:operations_app", "kind": "runbook", "subject": f"operations app {f['operations']['app']}",
                     "must_mention": [f["operations"]["app"]]})
    return docs


def load(ctx):
    jobs = production_jobs(ctx)
    f = facts(ctx, jobs)
    docs = _documents(f, jobs)
    h = stable_hash({"facts": {k: v for k, v in f.items() if k != "production_jobs"},
                     "jobs": [{k: j.get(k) for k in ("key", "name", "trigger", "tasks", "notify_on_failure")} for j in jobs],
                     "docs": docs, "inputs": {k: ctx.inputs.get(k) for k in KEYS}})
    cert = ledger.certified_plan(ctx) or {}
    reused = cert.get("documents") if cert.get("facts_hash") == h else None
    ctx.write_artefact("context.json", {"facts": f, "jobs": jobs, "docs": docs, "hash": h, "reused": reused})
    ctx.log(f"     {len(jobs)} production jobs, {len(docs)} documents "
            f"{'reused from certification' if reused else 'to write'}")
    return {"documents": [] if reused else [d["id"] for d in docs]}


def load_inputs_hash(ctx) -> str:
    return stable_hash({k: ctx.inputs.get(k) for k in KEYS})


def _facts_for(f, doc) -> dict:
    if doc["kind"] != "runbook":
        return {k: v for k, v in f.items() if k != "production_jobs"} | {
            "production_jobs": [{k: j.get(k) for k in ("name", "owner", "trigger")} for j in f["production_jobs"]]}
    base = {k: f[k] for k in ("product", "owners", "support", "quality")}
    key = doc["id"].split(":", 1)[1]
    if key == "agents_endpoint":
        return base | {"agents": f["agents"], "evaluation": {k: f["evaluation"][k] for k in ("thresholds", "results", "regression")}}
    if key == "operations_app":
        return base | {"operations": f["operations"]}
    job = next(j for j in f["production_jobs"] if j["key"] == key)
    return base | {"job": job, "evaluation": f["evaluation"] if "eval" in key else None}


def writer_input(ctx, doc_id, previous=None):
    c = _context(ctx)
    doc = next(d for d in c["docs"] if d["id"] == doc_id)
    out = {"doc_id": doc_id, "kind": doc["kind"], "subject": doc["subject"], "sections": DOC_SECTIONS[doc["kind"]],
           "must_mention": doc["must_mention"], "facts": _facts_for(c["facts"], doc),
           "audience": (ctx.inputs.get("docs") or {}).get("audience") or
                       {"product": "business users, data consumers and data engineers",
                        "onboarding": "a new business user or analyst on their first day",
                        "runbook": "the on-call data engineer"}[doc["kind"]]}
    if previous:
        out["previous_attempt"] = previous
    return out


def plan(ctx):
    c = _context(ctx)
    docs = {d["id"]: d for d in c["docs"]}
    written = c["reused"] or {}
    if not written:
        for r in ctx.read_artefact("documents.json") or []:
            if r and r.get("doc_id") in docs:
                written[r["doc_id"]] = {"title": r["title"], "markdown": r["markdown"]}
    model = ctx.system.model(ctx.inputs.get("model") or ctx.goal.spec["spec"]["harness"].get("model"))
    schema_file = json.loads((ctx.goal.dir / "harness" / "schemas" / "document.schema.json").read_text())

    def fix(doc_id):
        d = docs[doc_id]
        got = written.get(doc_id) or {}
        problems = doc_problems(doc_id, got.get("markdown"), d["must_mention"]) if got else ["was not written"]
        if problems and not c["reused"]:
            ctx.log(f"     rewriting {doc_id} ({problems[0]})")
            res = agents.run_agent(ctx, "tech_writer", TASK, writer_input(ctx, doc_id, {"document": got, "problems": problems}),
                                   schema_file, model, label=f"rewrite-{doc_id.replace(':', '-')}")
            got = {"title": res["result"]["title"], "markdown": res["result"]["markdown"]}
            problems = doc_problems(doc_id, got["markdown"], d["must_mention"])
        return doc_id, got, problems

    with ThreadPoolExecutor(max_workers=4) as pool:
        done = list(pool.map(fix, docs))
    documents = {i: g for i, g, _ in done}
    notify = list(ctx.inputs.get("notify") or [])
    min_retries = int(ctx.inputs.get("min_retries", 1))
    jobs = [{**{k: j.get(k) for k in ("key", "name", "job_id", "owner", "trigger", "notify_on_failure", "missing")},
             "problems": job_problems(j, notify, min_retries)} for j in c["jobs"]]
    spec = {"documents": documents, "doc_problems": {i: p for i, _, p in done if p},
            "jobs": jobs, "notify": notify, "min_retries": min_retries,
            "dashboard": dashboard_spec(ctx, c["facts"], c["jobs"]),
            "facts_hash": c["hash"], "inputs_hash": load_inputs_hash(ctx)}
    spec["problems"] = [f"{i}: {p}" for i, ps in spec["doc_problems"].items() for p in ps]
    spec["version"] = stable_hash({k: spec[k] for k in ("documents", "dashboard", "notify", "min_retries")})[:12]
    ctx.write_artefact("operations_plan.json", spec)
    bad = [j for j in jobs if j["problems"]]
    ctx.log(f"     plan: {len(documents)} documents ({len(spec['doc_problems'])} with problems); {len(jobs)} production jobs, "
            f"{len(bad)} not operable; monitoring dashboard with {len(spec['dashboard']['datasets'])} datasets")
    return {"documents": len(documents), "problems": len(spec["problems"])}
