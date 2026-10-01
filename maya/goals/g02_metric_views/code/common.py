"""Shared helpers for G2 nodes and checks."""
import re

import yaml

from maya.core import catalog
from maya.core.spec import stable_hash
from maya.core.workspace import SqlError, ident

FIELD_KEYS = ("name", "expr", "display_name", "comment", "synonyms", "format")
SPEC_VERSION = 1.1


def q(full_name):
    return ident(*full_name.split("."))


def target_schema(ctx):
    """(catalog, schema) the metric views live in; a bare schema goes into the first foundation catalog."""
    parts = ctx.inputs["schema"].split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else (ctx.system.catalogs[0], parts[0])


def full_name(ctx, view_name):
    c, s = target_schema(ctx)
    return f"{c}.{s}.{view_name}"


def cert_tag(ctx):
    t = ctx.inputs.get("certification_tag") or {}
    return t.get("key", "system.certification_status"), t.get("value", "certified")


def raw_definitions(ctx) -> list[dict]:
    views = list(ctx.inputs.get("metric_views") or [])
    ref = ctx.inputs.get("definitions")
    if ref:
        doc = yaml.safe_load((ctx.system.base_dir / ref).read_text()) or {}
        views += doc.get("metric_views") if isinstance(doc, dict) else doc
    return views


def body(d) -> dict:
    """The metric view YAML exactly as Unity Catalog receives it (reference SQL and MAYA-only keys removed)."""
    out = {"version": SPEC_VERSION, "source": d["source"]}
    if d.get("comment"):
        out["comment"] = d["comment"]
    if d.get("filter"):
        out["filter"] = d["filter"]
    if d.get("joins"):
        out["joins"] = [{"name": j["name"], "source": j["source"], "on": j["on"]} for j in d["joins"]]
    for kind in ("dimensions", "measures"):
        out[kind] = [{k: f[k] for k in FIELD_KEYS if f.get(k) not in (None, "", [])} for f in d[kind]]
    return out


def render(d) -> str:
    return yaml.safe_dump(body(d), sort_keys=False, allow_unicode=True, width=10_000)


def ddl(d) -> str:
    return f"CREATE OR REPLACE VIEW {q(d['full_name'])} WITH METRICS LANGUAGE YAML AS $$\n{render(d)}$$"


def view_tags(ctx, d) -> dict:
    tags = dict(ctx.inputs.get("tags") or {})
    tags.update(d.get("tags") or {})
    if d.get("owner"):
        tags["kpi_owner"] = d["owner"]
    return tags


def definition_hash(ctx, d) -> str:
    return stable_hash({"body": body(d), "tags": view_tags(ctx, d)})


def definitions(ctx) -> list[dict]:
    return ctx.read_artefact("definitions.json") or []


# ---------------------------------------------------------------- Unity Catalog read-back
def _canon(b) -> dict:
    """Comparable form of a metric view body: Unity Catalog may re-order keys or re-type scalars."""
    b = b or {}
    out = {"version": str(b.get("version")), "source": b.get("source"), "comment": b.get("comment") or None,
           "filter": " ".join(str(b.get("filter") or "").split()) or None,
           "joins": sorted((j.get("name"), j.get("source"), " ".join(str(j.get("on")).split())) for j in b.get("joins") or [])}
    for kind in ("dimensions", "measures"):
        out[kind] = {f["name"]: {k: (" ".join(str(f[k]).split()) if k in ("expr", "comment") else f[k])
                                 for k in FIELD_KEYS if f.get(k) not in (None, "", [])} for f in b.get(kind) or []}
    return out


def same_body(a, b) -> list[str]:
    """Differences between two bodies, as readable strings (empty when equal)."""
    a, b = _canon(a), _canon(b)
    diffs = [f"{k}: {a[k]!r} != {b[k]!r}" for k in ("version", "source", "comment", "filter", "joins") if a[k] != b[k]]
    for kind in ("dimensions", "measures"):
        for n in sorted(set(a[kind]) | set(b[kind])):
            fa, fb = a[kind].get(n), b[kind].get(n)
            if fa is None or fb is None:
                diffs.append(f"{kind[:-1]} {n}: {'missing in Unity Catalog' if fb is None else 'not in definition'}")
            else:
                diffs += [f"{kind[:-1]} {n}.{k}: {fa.get(k)!r} != {fb.get(k)!r}"
                          for k in FIELD_KEYS if fa.get(k) != fb.get(k)]
    return diffs


_YAML = re.compile(r"\$\$\s*\n(.*)\$\$", re.S)


def read_back(ctx, fn):
    """The metric view body as stored in Unity Catalog (None when the view does not exist or is not a metric view).
    SHOW CREATE TABLE returns the full YAML; the SDK view_definition drops display names, synonyms and formats."""
    try:
        rows = ctx.ws.sql(f"SHOW CREATE TABLE {q(fn)}")
    except SqlError:
        return None
    text = list(rows[0].values())[0] if rows else ""
    m = _YAML.search(text or "")
    return yaml.safe_load(m.group(1)) if m and "WITH METRICS" in text else None


def metric_views(ctx) -> dict:
    """full_name -> {comment} for every metric view in the target schema."""
    c, s = target_schema(ctx)
    try:
        rows = ctx.ws.sql(f"SELECT table_name, comment FROM {ident(c)}.information_schema.tables "
                          f"WHERE table_schema = '{s}' AND table_type = 'METRIC_VIEW'")
    except SqlError:
        return {}
    return {f"{c}.{s}.{r['table_name']}": r for r in rows}


def view_tags_live(ctx, fn) -> dict:
    c, s, t = fn.split(".")
    try:
        rows = ctx.ws.sql(f"SELECT tag_name, tag_value FROM {ident(c)}.information_schema.table_tags "
                          f"WHERE schema_name = '{s}' AND table_name = '{t}'")
    except SqlError:
        return {}
    return {r["tag_name"]: r["tag_value"] for r in rows}


def source_columns(ctx, fn) -> list[dict]:
    return [{"name": c["column_name"], "type": c["data_type"], "description": c.get("comment")}
            for c in catalog.columns(ctx.ws, ctx.system) if c["full_name"] == fn]
