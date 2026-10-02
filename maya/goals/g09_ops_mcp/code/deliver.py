"""G9 delivery: pure functions of the approved plan. The bundle gets one job per operation (the operation's notebook,
typed job parameters, one run at a time, a timeout) and the MCP server app, whose only resources are those jobs with
CAN_MANAGE_RUN, and whose users are the declared clients."""
import json

from maya.core import bundle

from .common import APP_KEY, TEMPLATES, notebook_source

APP_DIR = "app"


def _value(v) -> str:
    return json.dumps(v) if isinstance(v, bool) else str(v)


def server_spec(spec) -> dict:
    """operations.json: the whole tool list the app serves."""
    return {"server": spec["server"], "version": spec["version"], "max_wait_seconds": spec["max_wait_seconds"],
            "tools": [{"name": o["tool"], "operation": o["name"], "description": o["description"], "env": o["env"],
                       "writes": o["writes"], "parameters": o["parameters"]} for o in spec["operations"]]}


def app_yaml(spec) -> str:
    lines = ["# maintained by MAYA (G9)", 'command: ["python", "app.py"]', "env:"]
    for o in spec["operations"]:
        lines += [f"  - name: {o['env']}", f"    valueFrom: job_{o['name']}"]
    return "\n".join(lines) + "\n"


def job_files(ctx, spec) -> dict:
    out = {f"ops/{o['name']}.py": notebook_source(ctx, o) for o in spec["operations"]}
    out[f"{APP_DIR}/app.py"] = (TEMPLATES / "app" / "app.py").read_text()
    out[f"{APP_DIR}/requirements.txt"] = (TEMPLATES / "app" / "requirements.txt").read_text()
    out[f"{APP_DIR}/app.yaml"] = app_yaml(spec)
    out[f"{APP_DIR}/operations.json"] = json.dumps(server_spec(spec), indent=1, sort_keys=True) + "\n"
    return out


def job(system_name, o) -> dict:
    params = [{"name": p["name"], "default": _value(p.get("default", ""))} for p in o["parameters"]]
    params += [{"name": k, "default": _value(v)} for k, v in sorted(o["settings"].items())]
    return {"name": f"MAYA ops - {system_name} - {o['name']}",
            "description": o["description"][:1000],
            "max_concurrent_runs": 1,
            "timeout_seconds": 60 * int(o["timeout_minutes"]),
            "tags": {"maya_goal": "G9", "maya_operation": o["name"]},
            "parameters": params,
            "tasks": [{"task_key": o["name"],
                       "notebook_task": {"notebook_path": f"../jobs/G9/ops/{o['name']}.py"}}]}


def _principal(p) -> dict:
    from maya.goals.g04_governance.code.common import principal_kind
    k = principal_kind(p)
    return {"user_name": p} if k == "user" else {"service_principal_name": p} if k == "service_principal" else {"group_name": p}


def resources(ctx, spec) -> dict:
    jobs = {o["job_key"]: job(ctx.system.name, o) for o in spec["operations"]}
    app = {"name": spec["app_name"],
           "description": f"MAYA operations MCP server of {ctx.system.name}: {len(spec['operations'])} operations as tools",
           "source_code_path": f"../jobs/G9/{APP_DIR}",
           "resources": [{"name": f"job_{o['name']}", "description": f"Runs operation {o['name']}",
                          "job": {"id": f"${{resources.jobs.{o['job_key']}.id}}", "permission": "CAN_MANAGE_RUN"}}
                         for o in spec["operations"]],
           "permissions": [{"level": "CAN_USE", **_principal(p)} for p in spec["clients"]]}
    return {"jobs": jobs, "apps": {APP_KEY: app}}


def bundle_content(ctx, spec) -> dict:
    return {"files": {}, "jobs": job_files(ctx, spec), "resources": resources(ctx, spec)}


def write(ctx, spec) -> None:
    content = bundle_content(ctx, spec)
    jbase = bundle.jobs_dir(ctx.system, ctx.goal.id)
    stale = [str(p.relative_to(jbase)) for p in bundle.script_files(jbase, any_file=True)
             if str(p.relative_to(jbase)) not in content["jobs"]]
    bundle.write(ctx, content["jobs"], remove=stale, jobs=True)
    bundle.write_resources(ctx, content["resources"])
