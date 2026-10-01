"""Generic Unity Catalog discovery over information_schema, limited to the foundation sources in maya.yaml
(per layer: catalog.schema sources, a table selection and exclusions). Every row carries its full_name."""
import fnmatch

from .workspace import Workspace, lit


def sources(system) -> dict:
    """catalog -> {schema: layer}"""
    out = {}
    for layer, cfg in system.layers.items():
        for s in cfg["sources"]:
            out.setdefault(s["catalog"], {})[s["schema"]] = layer
    return out


def layer_of(system, catalog, schema):
    return sources(system).get(catalog, {}).get(schema)


def _matches(patterns, schema, table):
    return any(fnmatch.fnmatch(f"{schema}.{table}" if "." in p else table, p) for p in patterns)


def selected(system, catalog, schema, table) -> str | None:
    """The layer an asset belongs to, or None when the foundation does not select it."""
    layer = layer_of(system, catalog, schema)
    if not layer:
        return None
    cfg = system.layers[layer]
    if cfg["tables"] != "all" and not _matches(cfg["tables"], schema, table):
        return None
    if _matches(cfg["exclude"], schema, table):
        return None
    return layer


def resolve(system, name) -> str | None:
    """'catalog.schema.table' or 'schema.table' -> full name inside the foundation, else None."""
    parts = name.split(".")
    if len(parts) == 3:
        return name if layer_of(system, parts[0], parts[1]) else None
    if len(parts) == 2:
        hits = [c for c, schemas in sources(system).items() if parts[0] in schemas]
        return f"{hits[0]}.{name}" if len(hits) == 1 else None
    return None


def _scan(ws: Workspace, system, view, select, schema_col="table_schema", table_col="table_name", where=""):
    out = []
    for cat, schemas in sources(system).items():
        rows = ws.sql(f"SELECT {select} FROM {cat}.information_schema.{view} "
                      f"WHERE {schema_col} IN ({', '.join(lit(s) for s in schemas)}) {where}")
        for r in rows:
            layer = selected(system, cat, r[schema_col], r[table_col])
            if layer:
                out.append(dict(r, table_catalog=cat, layer=layer,
                                full_name=f"{cat}.{r[schema_col]}.{r[table_col]}"))
    return out


def tables(ws: Workspace, system) -> list[dict]:
    rows = _scan(ws, system, "tables", "table_schema, table_name, table_type, comment, table_owner, "
                                       "data_source_format, last_altered")
    for r in rows:
        r["kind"] = {"VIEW": "view", "METRIC_VIEW": "metric_view", "MATERIALIZED_VIEW": "view"}.get(r["table_type"], "table")
    return sorted(rows, key=lambda r: r["full_name"])


def columns(ws: Workspace, system) -> list[dict]:
    rows = _scan(ws, system, "columns", "table_schema, table_name, column_name, data_type, is_nullable, comment, "
                                        "ordinal_position")
    return sorted(rows, key=lambda r: (r["full_name"], int(r["ordinal_position"])))


def views(ws: Workspace, system) -> dict:
    return {r["full_name"]: r["view_definition"]
            for r in _scan(ws, system, "views", "table_schema, table_name, view_definition")}


def column_tags(ws: Workspace, system, tag_name) -> dict:
    """(full_name, column) -> tag value"""
    rows = _scan(ws, system, "column_tags", "schema_name, table_name, column_name, tag_value",
                 schema_col="schema_name", where=f"AND tag_name = {lit(tag_name)}")
    return {(r["full_name"], r["column_name"]): r["tag_value"] for r in rows}


def table_tags(ws: Workspace, system, tag_name) -> dict:
    """full_name -> tag value"""
    rows = _scan(ws, system, "table_tags", "schema_name, table_name, tag_value",
                 schema_col="schema_name", where=f"AND tag_name = {lit(tag_name)}")
    return {r["full_name"]: r["tag_value"] for r in rows}


def constraints(ws: Workspace, system) -> list[dict]:
    out = []
    for cat, schemas in sources(system).items():
        rows = ws.sql(f"""SELECT tc.table_schema, tc.table_name, tc.constraint_name, tc.constraint_type, kcu.column_name
            FROM {cat}.information_schema.table_constraints tc
            JOIN {cat}.information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema IN ({', '.join(lit(s) for s in schemas)})""")
        out += [dict(r, full_name=f"{cat}.{r['table_schema']}.{r['table_name']}") for r in rows]
    return out


def tag_policy_values(ws: Workspace, tag_key) -> list[str] | None:
    """Allowed values when tag_key is a governed tag, else None (any value may be set)."""
    from databricks.sdk.errors import NotFound
    try:
        policy = ws.client.tag_policies.get_tag_policy(tag_key)
    except NotFound:
        return None
    return [v.name for v in policy.values or []] or None


def describe_layers(system) -> dict:
    """What each layer covers, for reports and agent prompts."""
    return {k: {"sources": [f"{s['catalog']}.{s['schema']}" for s in v["sources"]], "tables": v["tables"],
                "exclude": v["exclude"]} for k, v in system.layers.items()}
