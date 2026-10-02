"""G10 delivery: pure functions of the approved plan. The bundle gets the schema of the registered model (a deploy
script), the agents' code and configuration (jobs/G10/agent) and the job that logs, registers and serves them."""
import json

from maya.core import bundle

from .common import JOB_KEY, TEMPLATES, q_schema


def agent_config(spec) -> dict:
    """agent_config.json: everything deploy_agent.py needs; workspace ids are resolved by name where it runs."""
    return {"version": spec["version"], "model": spec["model"], "endpoint": spec["endpoint"], "users": spec["users"],
            "scale_to_zero": spec["scale_to_zero"], "identity": {k: v for k, v in spec["identity"].items() if k != "service_principal"},
            "genie_title": spec["genie_title"], "ops_app": spec["ops_app"], "input_example": spec["input_example"],
            "runtime": spec["runtime"]}


def files(spec) -> dict:
    return {"10_schema/schema.sql": [f"CREATE SCHEMA IF NOT EXISTS {q_schema(spec['schema'])} COMMENT "
                                     f"'Registered agent models of the data product (MAYA G10)'"]}


def job_files(spec) -> dict:
    return {"agent/agent.py": (TEMPLATES / "agent" / "agent.py").read_text(),
            "agent/deploy_agent.py": (TEMPLATES / "agent" / "deploy_agent.py").read_text(),
            "agent/agent_config.json": agent_config(spec)}


def resources(system, spec, catalogs) -> dict:
    params = ["--code-dir", "${workspace.file_path}/jobs/G10/agent", "--warehouse-id", "${var.warehouse_id}",
              "--experiment", "${workspace.root_path}/maya_agents_experiment"]
    for c in catalogs:
        params += ["--catalog", f"{c}={{{{catalog:{c}}}}}"]
    return {"jobs": {JOB_KEY: {
        "name": f"MAYA agents deploy - {system.name}",
        "description": f"Logs, registers and serves the agents of {system.name} (endpoint {spec['endpoint']})",
        "max_concurrent_runs": 1, "timeout_seconds": 7200, "tags": {"maya_goal": "G10"},
        "environments": [{"environment_key": "agents", "spec": {
            "client": "2", "dependencies": ["mlflow>=3.1", "databricks-agents>=1.0", "databricks-sdk>=0.50", "requests"]}}],
        "tasks": [{"task_key": "deploy", "environment_key": "agents",
                   "spark_python_task": {"python_file": "../jobs/G10/agent/deploy_agent.py", "parameters": params}}]}}}


def bundle_content(system, spec) -> dict:
    cats = sorted(set(system.catalogs))
    return {"files": files(spec), "jobs": job_files(spec), "resources": resources(system, spec, cats), "catalogs": cats}


def write(ctx, spec) -> list[str]:
    content = bundle_content(ctx.system, spec)
    base, jbase = bundle.scripts_dir(ctx.system, ctx.goal.id), bundle.jobs_dir(ctx.system, ctx.goal.id)
    stale = [str(p.relative_to(base)) for p in bundle.script_files(base) if str(p.relative_to(base)) not in content["files"]]
    written = bundle.write(ctx, content["files"], remove=stale)
    jstale = [str(p.relative_to(jbase)) for p in bundle.script_files(jbase, any_file=True)
              if str(p.relative_to(jbase)) not in content["jobs"]]
    bundle.write(ctx, content["jobs"], catalogs=content["catalogs"], remove=jstale, jobs=True)
    bundle.write_resources(ctx, content["resources"], catalogs=content["catalogs"])
    return written
