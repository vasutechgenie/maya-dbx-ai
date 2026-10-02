"""Shared helpers for G12: the production jobs (MAYA's scheduled or triggered bundle jobs and the declared jobs that feed
the product), the facts the documentation is written from (all read from what the earlier goals certified), the
monitoring dashboard and the documents."""
import json
import re

from maya.core import bundle

DOC_SECTIONS = {
    "product": ["Overview", "Data", "Metrics", "AI access", "Quality and evaluation", "Owners and support"],
    "onboarding": ["Get access", "Ask questions", "Dashboards", "Get help"],
    "runbook": ["What it does", "Schedule", "When it fails", "Rerun", "Escalation"]}


def _certified(ctx, goal_id, table, col="plan_json") -> dict | None:
    from maya.core.workspace import lit
    try:
        rows = ctx.ws.sql(f"""SELECT max_by({col}, recorded_at) AS p FROM {ctx.state.t(table)}
            WHERE system = {lit(ctx.system.name)} AND run_id IN (SELECT run_id FROM {ctx.state.t('certifications')}
              WHERE system = {lit(ctx.system.name)} AND goal_id = {lit(goal_id)})""")
    except Exception:
        return None
    return json.loads(rows[0]["p"]) if rows and rows[0]["p"] else None


# ---------------------------------------------------------------- production jobs
def _trigger(s) -> dict:
    if s.schedule:
        return {"kind": "schedule", "cron": s.schedule.quartz_cron_expression, "timezone": s.schedule.timezone_id,
                "paused": bool(s.schedule.pause_status and s.schedule.pause_status.value == "PAUSED")}
    if s.trigger:
        t = s.trigger
        kind = "table_update" if t.table_update else "file_arrival" if t.file_arrival else "periodic" if t.periodic else "trigger"
        tables = (t.table_update.table_names if t.table_update else None) or []
        return {"kind": kind, "tables": tables, "paused": bool(t.pause_status and t.pause_status.value == "PAUSED")}
    if s.continuous:
        return {"kind": "continuous", "paused": bool(s.continuous.pause_status and s.continuous.pause_status.value == "PAUSED")}
    return {"kind": "none"}


def describe_job(ctx, job_id, owner) -> dict:
    j = ctx.ws.client.jobs.get(int(job_id))
    s = j.settings
    tasks = [{"task_key": t.task_key, "max_retries": t.max_retries or 0,
              "kind": next((k for k in ("notebook_task", "spark_python_task", "sql_task", "python_wheel_task", "run_job_task",
                                        "pipeline_task", "dbt_task") if getattr(t, k, None)), "task")} for t in s.tasks or []]
    email = s.email_notifications
    hooks = s.webhook_notifications
    runs = []
    try:
        for r in ctx.ws.client.jobs.list_runs(job_id=int(job_id), limit=10):
            runs.append({"run_id": r.run_id, "start": r.start_time, "result": r.state.result_state.value if r.state and
                         r.state.result_state else (r.state.life_cycle_state.value if r.state and r.state.life_cycle_state else None)})
            if len(runs) >= 10:
                break
    except Exception:
        pass
    return {"job_id": str(job_id), "name": s.name, "owner": owner, "description": s.description,
            "trigger": _trigger(s), "tasks": tasks, "timeout_seconds": s.timeout_seconds or 0,
            "notify_on_failure": sorted((email.on_failure if email else None) or []),
            "webhooks_on_failure": len((hooks.on_failure if hooks else None) or []), "recent_runs": runs}


def production_jobs(ctx) -> list[dict]:
    """MAYA's bundle jobs that run on their own (schedule, trigger, continuous) and the declared jobs."""
    out = []
    jobs = (bundle.resources(ctx.system, ctx.ws).get("jobs") or {})
    for key, j in sorted(jobs.items()):
        if not j.get("id") or key == "maya_deploy":
            continue
        d = describe_job(ctx, j["id"], owner="MAYA " + _goal_of(key))
        if d["trigger"]["kind"] != "none":
            out.append({**d, "key": key})
    for name in ctx.inputs.get("production_jobs") or []:
        found = list(ctx.ws.client.jobs.list(name=name))
        if not found:
            out.append({"key": slug(name), "name": name, "owner": "declared", "missing": True})
            continue
        out.append({**describe_job(ctx, found[0].job_id, owner="declared"), "key": slug(name)})
    return out


def _goal_of(key) -> str:
    m = re.match(r"maya_g(\d+)_", key)
    return f"G{m.group(1)}" if m else "bundle"


def job_problems(job, notify, min_retries) -> list[str]:
    if job.get("missing"):
        return ["the job does not exist"]
    out = []
    if job["trigger"]["kind"] == "none":
        out.append("runs only by hand (no schedule, trigger or continuous run)")
    low = [t["task_key"] for t in job["tasks"] if t["max_retries"] < min_retries]
    if low:
        out.append(f"tasks without {min_retries} retr{'y' if min_retries == 1 else 'ies'}: {', '.join(low)}")
    if not job["notify_on_failure"] and not job["webhooks_on_failure"]:
        out.append("nobody is notified when it fails")
    missing = [n for n in notify if n not in job["notify_on_failure"]]
    if missing:
        out.append(f"not notified on failure: {', '.join(missing)}")
    return out


def slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]


# ---------------------------------------------------------------- the documents
def doc_path(doc_id) -> str:
    return f"docs/{doc_id.replace(':', '/')}.md"


def kind_of(doc_id) -> str:
    return doc_id.split(":")[0]


def doc_problems(doc_id, markdown, must_mention) -> list[str]:
    out = []
    heads = {h.strip().lower() for h in re.findall(r"^#{2,3}\s+(.+?)\s*$", markdown or "", re.M)}
    out += [f"section '## {s}' is missing" for s in DOC_SECTIONS[kind_of(doc_id)] if s.lower() not in heads]
    text = (markdown or "").lower()
    out += [f"does not mention {m}" for m in must_mention if m.lower() not in text]
    if len(markdown or "") < 400:
        out.append("is too short")
    return out


def plan(ctx) -> dict:
    return ctx.read_artefact("operations_plan.json") or {}
