"""MAYA G10 deploy task (run by the bundle job maya_g10_agents): package the agents, register them in Unity Catalog and
serve them on Model Serving.

Reads agent_config.json next to this file (catalog tokens resolved for the bundle target), resolves the workspace's
ids by name (Genie space, operations app), writes agent.py with the configuration in it, logs it with MLflow (code
based) with every resource it uses declared for authentication passthrough, registers the version in Unity Catalog,
deploys it with Mosaic AI Agent Framework (inference tables, tracing to the experiment), and grants CAN_QUERY.
Prints one MAYA_DEPLOY_RESULT line.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
import traceback

RESULT = "MAYA_DEPLOY_RESULT "


def _wait_not_updating(w, name, minutes=45):
    """An endpoint takes no new configuration while an update is in progress."""
    from databricks.sdk.errors import NotFound
    deadline = time.time() + minutes * 60
    while True:
        try:
            e = w.serving_endpoints.get(name)
        except NotFound:
            return
        if not (e.state and e.state.config_update and e.state.config_update.value == "IN_PROGRESS"):
            return
        if time.time() > deadline:
            raise RuntimeError(f"endpoint {name} is still updating after {minutes} minutes")
        time.sleep(30)


def _set_tags(w, name, tags):
    """The tags API rejects a key that is both added and deleted, so changed values are deleted first."""
    from databricks.sdk.service.serving import EndpointTag
    current = {t.key: t.value for t in (w.serving_endpoints.get(name).tags or [])}
    changed = [k for k, v in tags.items() if k in current and current[k] != v]
    if changed:
        w.serving_endpoints.patch(name, delete_tags=changed)
    missing = [EndpointTag(key=k, value=v) for k, v in tags.items() if current.get(k) != v]
    if missing:
        w.serving_endpoints.patch(name, add_tags=missing)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--code-dir", required=True)
    ap.add_argument("--warehouse-id", required=True)
    ap.add_argument("--experiment", required=True)
    ap.add_argument("--catalog", action="append", default=[], help="dev=target catalog name")
    a = ap.parse_args(argv)
    cats = dict(x.split("=", 1) for x in a.catalog)
    sub = lambda s: re.sub(r"\{\{catalog:([A-Za-z0-9_\-]+)\}\}", lambda m: cats.get(m.group(1), m.group(1)), s)
    code_dir = a.code_dir if os.path.exists(a.code_dir) else "/Workspace" + a.code_dir
    plan = json.loads(sub(open(os.path.join(code_dir, "agent_config.json")).read()))

    import mlflow
    from databricks import agents
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.serving import ServingEndpointAccessControlRequest, ServingEndpointPermissionLevel
    from mlflow.models import resources as R

    w = WorkspaceClient()
    config = dict(plan["runtime"], host=w.config.host.rstrip("/"), warehouse_id=a.warehouse_id)
    if plan.get("genie_title"):
        spaces = [s for s in (w.genie.list_spaces().spaces or []) if s.title == plan["genie_title"]]
        if not spaces:
            raise RuntimeError(f"Genie space '{plan['genie_title']}' not found")
        config["genie_space_id"] = spaces[0].space_id
    if plan.get("ops_app"):
        config["mcp_url"] = w.apps.get(plan["ops_app"]).url

    src = open(os.path.join(code_dir, "agent.py")).read()
    marker = "CONFIG = None  # MAYA:CONFIG"
    assert marker in src, "agent.py lacks the configuration marker"
    path = os.path.join(tempfile.mkdtemp(prefix="maya_agent_"), "agent.py")
    with open(path, "w") as f:
        f.write(src.replace(marker, "CONFIG = " + json.dumps(config, indent=1)))

    res = [R.DatabricksServingEndpoint(endpoint_name=config["llm_endpoint"]),
           R.DatabricksSQLWarehouse(warehouse_id=a.warehouse_id)]
    tools = [t for ag in [config["supervisor"], *config["sub_agents"]] for t in ag["tools"]]
    res += [R.DatabricksFunction(function_name=t["target"]) for t in tools if t["type"] in ("function", "lookup")]
    if config.get("genie_space_id"):
        res.append(R.DatabricksGenieSpace(genie_space_id=config["genie_space_id"]))
    res += [R.DatabricksTable(table_name=t) for t in plan.get("tables") or []]
    if plan.get("ops_app") and hasattr(R, "DatabricksApp"):
        res.append(R.DatabricksApp(app_name=plan["ops_app"]))

    mlflow.set_registry_uri("databricks-uc")
    exp = mlflow.set_experiment(a.experiment)
    example = {"input": [{"role": "user", "content": plan["input_example"]}]}
    with mlflow.start_run(run_name=f"maya-{plan['version']}"):
        info = mlflow.pyfunc.log_model(name="agent", python_model=path, resources=res, input_example=example,
                                       pip_requirements=["mlflow>=3.1", "databricks-sdk>=0.50", "requests"],
                                       registered_model_name=plan["model"])
    version = info.registered_model_version
    from mlflow import MlflowClient
    client = MlflowClient(registry_uri="databricks-uc")
    client.set_model_version_tag(plan["model"], version, "maya_config_version", plan["version"])
    client.set_registered_model_alias(plan["model"], "champion", version)

    scope = plan["identity"]["secret_scope"]
    env = {"MAYA_AGENT_CLIENT_ID": f"{{{{secrets/{scope}/{plan['identity']['client_id_key']}}}}}",
           "MAYA_AGENT_CLIENT_SECRET": f"{{{{secrets/{scope}/{plan['identity']['client_secret_key']}}}}}"}
    for attempt in range(4):
        _wait_not_updating(w, plan["endpoint"])
        try:
            d = agents.deploy(plan["model"], version, endpoint_name=plan["endpoint"], scale_to_zero=plan["scale_to_zero"],
                              environment_vars=env)
            break
        except ValueError as e:
            if "currently updating" not in str(e) or attempt == 3:
                raise
    _set_tags(w, plan["endpoint"], {"maya_goal": "G10", "maya_config_version": plan["version"]})
    acl = []
    for p in plan["users"]:
        k = "user_name" if "@" in p else "service_principal_name" if re.match(r"^[0-9a-f-]{36}$", p) else "group_name"
        acl.append(ServingEndpointAccessControlRequest(**{k: p}, permission_level=ServingEndpointPermissionLevel.CAN_QUERY))
    endpoint_id = w.serving_endpoints.get(plan["endpoint"]).id
    if acl:
        w.serving_endpoints.update_permissions(endpoint_id, access_control_list=acl)
    return {"ok": True, "model": plan["model"], "version": str(version), "endpoint": plan["endpoint"],
            "endpoint_id": endpoint_id, "experiment_id": exp.experiment_id, "model_uri": info.model_uri,
            "resources": [type(r).__name__ + ":" + json.dumps(r.to_dict(), default=str) for r in res],
            "query_endpoint": getattr(d, "query_endpoint", None)}


if __name__ == "__main__":
    t0 = time.time()
    try:
        out = main()
    except Exception as e:
        traceback.print_exc()
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"[:3000]}
    out["seconds"] = round(time.time() - t0, 1)
    print(RESULT + json.dumps(out, default=str))
    if not out["ok"]:  # a successful job task must not call sys.exit
        sys.exit(1)
