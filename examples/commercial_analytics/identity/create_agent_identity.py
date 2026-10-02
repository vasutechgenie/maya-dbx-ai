"""Provision the agent identity of the example: the service principal the agents, the operations MCP clients and the
tool tests act as (maya.yaml agent_identity). This is the security team's job in a real project (CR-AR-3.2): MAYA
never creates identities or credentials, it only uses the ones declared.

Creates (or reuses) the service principal, an OAuth secret for it (machine-to-machine, OAuth only), stores the client
id and secret in a secret scope the deployer can read, and lets the principal use the SQL warehouse. The secret is never
printed. Prints the application id to export as MAYA_AGENT_SP.

Creating a service principal needs a workspace admin. Without admin rights the script stands in with the service
principal of a stopped Databricks App (no compute) that it creates: its creator may mint OAuth secrets for it.

Usage: python create_agent_identity.py --profile <cli-profile> --warehouse <id>
         [--name maya-example-agent] [--scope maya_agent] [--rotate]
"""
import argparse
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service import apps, iam


def service_principal(w, name):
    """(scim id, application id) of the identity, created when missing."""
    try:
        sp = next(iter(w.service_principals.list(filter=f'displayName eq "{name}"')), None)
        if sp is None:
            sp = w.service_principals.create(display_name=name, active=True,
                                             entitlements=[iam.ComplexValue(value="workspace-access")])
        return sp.id, sp.application_id
    except PermissionDenied:
        pass
    try:
        app = w.apps.get(name)
    except NotFound:
        print(f"not a workspace admin: creating stopped app {name} for its service principal")
        w.apps.create(apps.App(name=name, description="Identity of the MAYA example agents (no code, never started)"),
                      no_compute=True)
    for _ in range(60):
        app = w.apps.get(name)
        if app.service_principal_id:
            break
        time.sleep(5)
    return str(app.service_principal_id), app.service_principal_client_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--name", default="maya-example-agent")
    ap.add_argument("--scope", default="maya_agent")
    ap.add_argument("--rotate", action="store_true", help="create a new secret even if the scope already holds one")
    a = ap.parse_args()
    w = WorkspaceClient(profile=a.profile)

    sp_id, app_id = service_principal(w, a.name)
    print(f"service principal {a.name}: application id {app_id}")

    if a.scope not in {s.name for s in w.secrets.list_scopes()}:
        w.secrets.create_scope(a.scope)
    have = {s.key for s in w.secrets.list_secrets(a.scope)}
    if a.rotate or not {"client_id", "client_secret"} <= have:
        secret = w.service_principal_secrets_proxy.create(int(sp_id))
        w.secrets.put_secret(a.scope, "client_id", string_value=app_id)
        w.secrets.put_secret(a.scope, "client_secret", string_value=secret.secret)
        print(f"stored a new OAuth secret in scope {a.scope} (not shown)")
    else:
        print(f"scope {a.scope} already holds the principal's OAuth secret")

    try:
        w.permissions.update("warehouses", a.warehouse, access_control_list=[
            iam.AccessControlRequest(service_principal_name=app_id, permission_level=iam.PermissionLevel.CAN_USE)])
        print(f"CAN_USE on warehouse {a.warehouse}")
    except PermissionDenied:
        print(f"cannot grant CAN_USE on warehouse {a.warehouse} (needs CAN_MANAGE): ask its owner unless every user may use it")
    print(f"\nexport MAYA_AGENT_SP={app_id}")


if __name__ == "__main__":
    main()
