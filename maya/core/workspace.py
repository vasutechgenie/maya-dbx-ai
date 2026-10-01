"""Thin access layer over a Databricks workspace: SQL on a warehouse and the SDK client."""
from databricks.sdk import WorkspaceClient


class SqlError(Exception):
    pass


def lit(value) -> str:
    """SQL literal for strings / numbers / None."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def ident(*parts) -> str:
    return ".".join(f"`{p.replace('`', '``')}`" for p in parts)


class Workspace:
    def __init__(self, target: dict):
        profile = target.get("profile")
        self.client = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
        self.warehouse_id = target["warehouse_id"]
        self._user = None

    @property
    def host(self) -> str:
        return self.client.config.host.rstrip("/")

    def auth_headers(self) -> dict:
        return self.client.config.authenticate()

    @property
    def user(self) -> str:
        if self._user is None:
            self._user = self.client.current_user.me().user_name
        return self._user

    def sql(self, statement: str) -> list[dict]:
        r = self.client.statement_execution.execute_statement(
            warehouse_id=self.warehouse_id, statement=statement, wait_timeout="50s")
        while r.status.state.value in ("PENDING", "RUNNING"):
            r = self.client.statement_execution.get_statement(r.statement_id)
        if r.status.state.value != "SUCCEEDED":
            raise SqlError(f"{r.status.error.message if r.status.error else r.status.state}: {statement[:200]}")
        if not r.manifest or not r.result:
            return []
        cols = [c.name for c in r.manifest.schema.columns]
        rows = [dict(zip(cols, row)) for row in (r.result.data_array or [])]
        chunk = r.result.next_chunk_index
        while chunk:
            part = self.client.statement_execution.get_statement_result_chunk_n(r.statement_id, chunk)
            rows += [dict(zip(cols, row)) for row in (part.data_array or [])]
            chunk = part.next_chunk_index
        return rows
