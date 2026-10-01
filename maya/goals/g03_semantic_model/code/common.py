"""Shared helpers for G3 nodes and checks: where the registry lives, the tag keys, the assets in scope."""
import fnmatch
import re

import yaml

from maya.core import catalog
from maya.core.workspace import SqlError, ident, lit

REGISTRY = ("ontology_nodes", "ontology_page_objects", "business_glossary")
LOOKUP = "ontology_lookup"


def q(full_name):
    return ident(*full_name.split("."))


def _split_schema(ctx, name):
    parts = name.split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else (ctx.system.catalogs[0], parts[0])


def target_schema(ctx):
    """(catalog, schema) of the ontology registry, glossary and lookup function."""
    return _split_schema(ctx, ctx.inputs["schema"])


def registry(ctx, name) -> str:
    c, s = target_schema(ctx)
    return f"{c}.{s}.{name}"


def slug(text) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return (s if s[:1].isalpha() else f"p_{s}")[:63] or "page"


def tag_keys(ctx) -> dict:
    prefix = slug(ctx.system.name) + "_"
    keys = {k: f"{prefix}{k}" for k in ("domain", "subdomain", "page")}
    keys.update(ctx.inputs.get("tag_keys") or {})
    return keys


def raw_taxonomy(ctx) -> dict:
    return yaml.safe_load((ctx.system.base_dir / ctx.inputs["taxonomy"]).read_text()) or {}


def metric_view_schemas(ctx) -> list[tuple]:
    names = ctx.inputs.get("metric_view_schemas")
    if names is None:
        g2 = ctx.system.goal_settings("G2") or {}
        names = [g2["schema"]] if g2.get("schema") else []
    return [_split_schema(ctx, n) for n in names]


def _excluded(ctx, fn):
    _, s, t = fn.split(".")
    return any(fnmatch.fnmatch(f"{s}.{t}" if "." in p else t, p) for p in ctx.inputs.get("exclude") or [])


def scope_assets(ctx) -> list[dict]:
    """Every asset the semantic model must place: foundation tables and views of the chosen layers, and the metric
    views of the metric view schemas. {full_name, kind, layer, comment}."""
    layers = set(ctx.inputs.get("layers") or ["silver", "gold"])
    out = [{"full_name": t["full_name"], "kind": t["kind"], "layer": t["layer"], "comment": t.get("comment")}
           for t in catalog.tables(ctx.ws, ctx.system) if t["layer"] in layers and t["kind"] in ("table", "view")]
    for c, s in metric_view_schemas(ctx):
        try:
            rows = ctx.ws.sql(f"SELECT table_name, comment FROM {ident(c)}.information_schema.tables "
                              f"WHERE table_schema = {lit(s)} AND table_type = 'METRIC_VIEW'")
        except SqlError:
            rows = []
        out += [{"full_name": f"{c}.{s}.{r['table_name']}", "kind": "metric_view", "layer": "metric_view",
                 "comment": r.get("comment")} for r in rows]
    return sorted((a for a in out if not _excluded(ctx, a["full_name"])), key=lambda a: a["full_name"])


def columns_of(ctx, full_names) -> dict:
    """full_name -> [{name, type, comment}] (metric views list their dimensions and measures)."""
    groups = {}
    for fn in full_names:
        c, s, t = fn.split(".")
        groups.setdefault((c, s), set()).add(t)
    out = {fn: [] for fn in full_names}
    for (c, s), names in groups.items():
        rows = ctx.ws.sql(f"SELECT table_name, column_name, data_type, comment FROM {ident(c)}.information_schema.columns "
                          f"WHERE table_schema = {lit(s)} AND table_name IN ({', '.join(lit(n) for n in sorted(names))}) "
                          "ORDER BY table_name, ordinal_position")
        for r in rows:
            out[f"{c}.{s}.{r['table_name']}"].append({"name": r["column_name"], "type": r["data_type"],
                                                      "comment": r.get("comment")})
    return out


def resolve_asset(ctx, name, known) -> str | None:
    """'schema.name' or 'catalog.schema.name' -> full name of an asset in `known` (full names)."""
    parts = name.split(".")
    if len(parts) == 3:
        return name if name in known else None
    if len(parts) == 2:
        hits = [k for k in known if k.split(".", 1)[1] == name]
        return hits[0] if len(hits) == 1 else None
    return None


def resolve_link(ctx, link, known, columns) -> tuple:
    """A glossary link -> (object full name, field or None), or (None, reason)."""
    parts = link.split(".")
    for n_obj in (3, 2):
        if len(parts) < n_obj:
            continue
        fn = resolve_asset(ctx, ".".join(parts[:n_obj]), known)
        rest = parts[n_obj:]
        if fn and len(rest) <= 1:
            if not rest:
                return fn, None
            if rest[0] in {c["name"] for c in columns.get(fn, [])}:
                return fn, rest[0]
            return None, f"{fn} has no column or measure {rest[0]!r}"
    return None, f"{link!r} is not an asset of the semantic model"


def model(ctx) -> dict:
    return ctx.read_artefact("semantic_model.json") or {}


def page_path(m) -> dict:
    """page id -> (domain id, subdomain id)"""
    return {p["id"]: (p["domain"], p["subdomain"]) for p in m.get("pages") or []}
