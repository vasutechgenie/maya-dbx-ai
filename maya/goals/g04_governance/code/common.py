"""Shared helpers for G4: principals, scopes (layers and product schemas), and live reads of Unity Catalog."""
import re

from maya.core import catalog
from maya.core.workspace import SqlError, ident, lit

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
WORKSPACE_LOCAL = {"admins", "users"}
CONSUMER_SCOPES = {"gold", "metric_views", "semantic", "governance"}
TAG = re.compile(r"\[maya:([0-9a-f]+)\]")


def q(full_name):
    return ident(*full_name.split("."))


def principal_kind(p) -> str:
    return "user" if "@" in p else "service_principal" if UUID.match(p) else "group"


def member_sql(p) -> str:
    return f"is_account_group_member({lit(p)})" if principal_kind(p) == "group" else f"current_user() = {lit(p)}"


def exempt_sql(principals) -> str:
    return " OR ".join(member_sql(p) for p in dict.fromkeys(principals)) or "FALSE"


def _schema_name(ctx, name):
    parts = name.split(".")
    return name if len(parts) == 2 else f"{ctx.system.catalogs[0]}.{parts[0]}"


def gov_schema(ctx) -> str:
    return _schema_name(ctx, ctx.inputs["schema"])


def scope_schemas(ctx, scope) -> list[str]:
    """'catalog.schema' names a scope stands for."""
    if scope in ctx.system.layers:
        return [f"{s['catalog']}.{s['schema']}" for s in ctx.system.layers[scope]["sources"]]
    if scope == "metric_views":
        g2 = ctx.system.goal_settings("G2") or {}
        return [_schema_name(ctx, g2["schema"])] if g2.get("schema") else []
    if scope == "semantic":
        g3 = ctx.system.goal_settings("G3") or {}
        return [_schema_name(ctx, g3["schema"])] if g3.get("schema") else []
    if scope == "governance":
        return [gov_schema(ctx)]
    return [_schema_name(ctx, scope)]


def product_schemas(ctx) -> list[str]:
    out = []
    for s in [*ctx.system.layers, "metric_views", "semantic", "governance"]:
        out += [x for x in scope_schemas(ctx, s) if x not in out]
    return out


def layer_of_schema(ctx, schema) -> str | None:
    c, s = schema.split(".")
    return catalog.layer_of(ctx.system, c, s)


def resolve_object(ctx, name) -> str:
    """'schema.name' or 'catalog.schema.name' -> full name (foundation assets are resolved through their layers)."""
    parts = name.split(".")
    if len(parts) == 3:
        return name
    return catalog.resolve(ctx.system, name) or f"{ctx.system.catalogs[0]}.{name}"


def fn_name(ctx, name) -> str:
    return f"{gov_schema(ctx)}.{name}"


def plan(ctx) -> dict:
    """The approved plan of this run, else the certified one (drift checks)."""
    from . import ledger
    p = ctx.read_artefact("governance_plan.json")
    if p:
        return p
    cert = ledger.certified(ctx)
    return cert or {}


# ---------------------------------------------------------------- live reads
def _by_catalog(names):
    out = {}
    for n in names:
        out.setdefault(n.split(".")[0], []).append(n)
    return out


def privileges(ctx, schemas) -> list[dict]:
    """Direct (not inherited) grants on the catalogs and schemas given and on every table, view and function in them:
    {principal, privilege, securable_type, securable}."""
    out = []
    for cat, names in _by_catalog(schemas).items():
        sch = ", ".join(lit(n.split(".")[1]) for n in names)
        queries = [
            ("CATALOG", f"SELECT grantee, privilege_type, catalog_name AS securable FROM {ident(cat)}.information_schema.catalog_privileges "
                        f"WHERE inherited_from = 'NONE'"),
            ("SCHEMA", f"SELECT grantee, privilege_type, catalog_name || '.' || schema_name AS securable "
                       f"FROM {ident(cat)}.information_schema.schema_privileges WHERE inherited_from = 'NONE' AND schema_name IN ({sch})"),
            ("TABLE", f"SELECT grantee, privilege_type, table_catalog || '.' || table_schema || '.' || table_name AS securable "
                      f"FROM {ident(cat)}.information_schema.table_privileges WHERE inherited_from = 'NONE' AND table_schema IN ({sch})"),
            ("FUNCTION", f"SELECT grantee, privilege_type, routine_catalog || '.' || routine_schema || '.' || routine_name AS securable "
                         f"FROM {ident(cat)}.information_schema.routine_privileges WHERE inherited_from = 'NONE' AND routine_schema IN ({sch})"),
        ]
        for kind, sql in queries:
            try:
                rows = ctx.ws.sql(sql)
            except SqlError:
                continue
            out += [{"principal": r["grantee"], "privilege": r["privilege_type"].replace("_", " "),
                     "securable_type": kind, "securable": r["securable"]} for r in rows]
    return out


def column_masks(ctx, catalogs) -> dict:
    """(table full name, column) -> mask function full name"""
    out = {}
    for cat in catalogs:
        try:
            rows = ctx.ws.sql(f"SELECT table_schema, table_name, column_name, mask_name FROM {ident(cat)}.information_schema.column_masks")
        except SqlError:
            continue
        out.update({(f"{cat}.{r['table_schema']}.{r['table_name']}", r["column_name"]): r["mask_name"] for r in rows})
    return out


def row_filters(ctx, catalogs) -> dict:
    """table full name -> {function, columns}"""
    out = {}
    for cat in catalogs:
        try:
            rows = ctx.ws.sql(f"SELECT table_schema, table_name, filter_name, target_columns FROM {ident(cat)}.information_schema.row_filters")
        except SqlError:
            continue
        out.update({f"{cat}.{r['table_schema']}.{r['table_name']}":
                    {"function": r["filter_name"], "columns": [c.strip() for c in (r["target_columns"] or "").split(",") if c.strip()]}
                    for r in rows})
    return out


def policy(ctx, name, schema) -> dict | None:
    """DESCRIBE POLICY as {info_name: info_value}, or None when the policy does not exist."""
    try:
        rows = ctx.ws.sql(f"DESCRIBE POLICY {ident(name)} ON SCHEMA {q(schema)}")
    except SqlError:
        return None
    return {r["info_name"]: r["info_value"] for r in rows}


def routine_comments(ctx, schema) -> dict:
    c, s = schema.split(".")
    try:
        rows = ctx.ws.sql(f"SELECT routine_name, comment FROM {ident(c)}.information_schema.routines WHERE routine_schema = {lit(s)}")
    except SqlError:
        return {}
    return {f"{c}.{s}.{r['routine_name']}": r.get("comment") or "" for r in rows}


def sensitive_columns(ctx) -> list[dict]:
    """Foundation table columns carrying a sensitive tag value: {table, column, value, data_type}."""
    sens = ctx.inputs.get("sensitive") or {"tag": "sensitivity", "values": ["pii"]}
    tables = {t["full_name"] for t in catalog.tables(ctx.ws, ctx.system) if t["kind"] == "table"}
    types = {(c["full_name"], c["column_name"]): c["data_type"] for c in catalog.columns(ctx.ws, ctx.system)}
    tags = catalog.column_tags(ctx.ws, ctx.system, sens["tag"])
    return [{"table": fn, "column": col, "value": v, "data_type": types.get((fn, col))}
            for (fn, col), v in sorted(tags.items()) if v in sens["values"] and fn in tables]
