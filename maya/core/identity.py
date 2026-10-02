"""The agent identity (maya.yaml agent_identity): the service principal the agents, the operations MCP clients and the
tool tests act as. Security provisions it and keeps its OAuth client id and secret in a secret scope the deployer can
read; MAYA reads them only in memory, never writes or prints them, and authenticates with OAuth (machine-to-machine).

agent_identity:
  service_principal: <application id>
  secret_scope: maya_agent          # keys client_id and client_secret (override with client_id_key, client_secret_key)
"""
import base64

from databricks.sdk import WorkspaceClient

from .workspace import SqlError


class IdentityError(RuntimeError):
    pass


def settings(system) -> dict:
    return system.spec.get("agent_identity") or {}


def principal(system) -> str | None:
    return settings(system).get("service_principal")


def scope(system) -> str | None:
    return settings(system).get("secret_scope")


def _secret(ws, scope_name, key) -> str:
    return base64.b64decode(ws.client.secrets.get_secret(scope_name, key).value).decode()


def client(ctx) -> WorkspaceClient:
    """A workspace client authenticated as the agent identity (cached per context)."""
    cache = ctx.__dict__.setdefault("_agent_identity", {})
    if "client" not in cache:
        s = settings(ctx.system)
        if not s.get("service_principal") or not s.get("secret_scope"):
            raise IdentityError("maya.yaml declares no agent_identity (service_principal and secret_scope)")
        try:
            cid = _secret(ctx.ws, s["secret_scope"], s.get("client_id_key", "client_id"))
            csec = _secret(ctx.ws, s["secret_scope"], s.get("client_secret_key", "client_secret"))
        except Exception as e:
            raise IdentityError(f"cannot read the agent identity's OAuth secret from scope {s['secret_scope']}: {e}")
        if cid != s["service_principal"]:
            raise IdentityError(f"scope {s['secret_scope']} holds the secret of another principal than {s['service_principal']}")
        cache["client"] = WorkspaceClient(host=ctx.ws.host, client_id=cid, client_secret=csec, auth_type="oauth-m2m")
    return cache["client"]


def headers(ctx) -> dict:
    return client(ctx).config.authenticate()


def sql(ctx, statement, parameters=None) -> list[dict]:
    """Run a statement as the agent identity (named parameters: {name: value})."""
    from databricks.sdk.service.sql import StatementParameterListItem
    c = client(ctx)
    params = [StatementParameterListItem(name=k, value=None if v is None else str(v)) for k, v in (parameters or {}).items()]
    r = c.statement_execution.execute_statement(warehouse_id=ctx.ws.warehouse_id, statement=statement,
                                                parameters=params or None, wait_timeout="50s")
    while r.status.state.value in ("PENDING", "RUNNING"):
        r = c.statement_execution.get_statement(r.statement_id)
    if r.status.state.value != "SUCCEEDED":
        raise SqlError(f"{r.status.error.message if r.status.error else r.status.state}: {statement[:200]}")
    if not r.manifest or not r.result:
        return []
    cols = [x.name for x in r.manifest.schema.columns]
    return [dict(zip(cols, row)) for row in (r.result.data_array or [])]
