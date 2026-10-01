"""G2 load / render / review: the user-supplied definitions are the single input. MAYA checks them, resolves every
source inside the certified foundation, plans the incremental work and hands new or changed views to the reviewer."""
import re

import jsonschema

from maya.core import catalog
from maya.core.workspace import SqlError

from . import ledger
from .common import (body, definition_hash, definitions, ddl, full_name, metric_views, raw_definitions, read_back,
                     same_body, source_columns, target_schema, view_tags)
from .common import render as render_yaml


def _resolve_source(ctx, name, errors, where):
    fn = catalog.resolve(ctx.system, name)
    if not fn:
        errors.append(f"{where}: source {name!r} is not an asset of the foundation")
        return name
    c, s, _ = fn.split(".")
    if catalog.layer_of(ctx.system, c, s) == "bronze" and not ctx.inputs.get("allow_bronze_sources"):
        errors.append(f"{where}: source {fn} is Bronze; metric views are built on Silver or Gold")
    return fn


def _reference_columns(ctx, sql):
    rows = ctx.ws.sql(f"DESCRIBE QUERY {sql.strip().rstrip(';')}")
    return [r["col_name"] for r in rows]


def _check(ctx, d, errors):
    where = f"metric view {d['name']}"
    names = [f["name"] for f in d["dimensions"] + d["measures"]]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        errors.append(f"{where}: field names used twice {dup}")
    joins = [j["name"] for j in d.get("joins") or []]
    if len(set(joins)) != len(joins):
        errors.append(f"{where}: join names used twice")
    dims = {f["name"] for f in d["dimensions"]}
    c, s = target_schema(ctx)
    own = re.compile(rf"\b{re.escape(s)}\s*\.", re.I)
    for m in d["measures"]:
        ref = m["reference"]
        by = ref.get("by") or []
        unknown = [b for b in by if b not in dims]
        if unknown:
            errors.append(f"{where}: measure {m['name']} reference.by {unknown} are not dimensions of the view")
        if own.search(ref["sql"]):
            errors.append(f"{where}: measure {m['name']} reference SQL reads the metric view schema {c}.{s}; "
                          "it must be computed independently")
            continue
        try:
            cols = _reference_columns(ctx, ref["sql"])
        except SqlError as e:
            errors.append(f"{where}: measure {m['name']} reference SQL does not compile: {str(e).splitlines()[0][:240]}")
            continue
        missing = [x for x in ["value", *by] if x not in cols]
        if missing:
            errors.append(f"{where}: measure {m['name']} reference SQL must return columns {['value', *by]} "
                          f"(missing {missing}, got {cols})")


def load(ctx):
    c, s = target_schema(ctx)
    errors = []
    if catalog.layer_of(ctx.system, c, s):
        errors.append(f"schema {c}.{s} is a foundation layer schema; metric views need their own schema")
    raw = raw_definitions(ctx)
    if not raw:
        errors.append("no metric views defined (set goals.G2.definitions or goals.G2.metric_views)")
    names = [d.get("name") for d in raw]
    for n in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"metric view {n} is defined twice")
    view_schema = ctx.goal.inputs_schema()
    validator = jsonschema.Draft202012Validator({"$ref": "#/$defs/view", "$defs": view_schema["$defs"]})
    out = []
    for i, d in enumerate(raw):
        bad = sorted(validator.iter_errors(d), key=lambda e: list(e.path))
        if bad:
            errors += [f"metric view #{i + 1} ({d.get('name')}): {e.message} at {'/'.join(map(str, e.path)) or 'root'}"
                       for e in bad[:5]]
            continue
        d = dict(d, full_name=full_name(ctx, d["name"]))
        d["source"] = _resolve_source(ctx, d["source"], errors, f"metric view {d['name']}")
        d["joins"] = [dict(j, source=_resolve_source(ctx, j["source"], errors, f"metric view {d['name']} join {j['name']}"))
                      for j in d.get("joins") or []]
        _check(ctx, d, errors)
        out.append(d)
    if errors:
        raise RuntimeError("metric view definitions are invalid:\n  - " + "\n  - ".join(errors))
    ctx.write_artefact("definitions.json", out)
    return {"schema": f"{c}.{s}", "metric_views": len(out),
            "measures": sum(len(d["measures"]) for d in out), "dimensions": sum(len(d["dimensions"]) for d in out)}


def plan(ctx) -> dict:
    """new: not in Unity Catalog; changed: definition differs from the certified one or from Unity Catalog;
    unchanged: certified and identical in Unity Catalog. 'full' mode re-creates everything."""
    certified = ledger.certified(ctx)
    live = metric_views(ctx)
    out = {"new": [], "changed": [], "unchanged": [], "reasons": {}}
    for d in definitions(ctx):
        fn = d["full_name"]
        if fn not in live:
            out["new"].append(fn)
            continue
        why = []
        if certified.get(fn, {}).get("definition_hash") != definition_hash(ctx, d):
            why.append("definition differs from the certified one" if fn in certified else "not certified by MAYA yet")
        diffs = same_body(read_back(ctx, fn), body(d))
        if diffs:
            why.append(f"Unity Catalog differs ({len(diffs)}): {diffs[0]}")
        if ctx.inputs.get("mode") == "full":
            why.append("full mode")
        (out["changed"] if why else out["unchanged"]).append(fn)
        if why:
            out["reasons"][fn] = why
    defined = {d["full_name"] for d in definitions(ctx)}
    out["orphans"] = sorted(n for n in live if n not in defined)
    return out


def render(ctx):
    p = plan(ctx)
    defs = {d["full_name"]: d for d in definitions(ctx)}
    ctx.write_artefact("plan.json", p)
    ctx.write_artefact("rendered.json", {fn: {"yaml": render_yaml(d), "ddl": ddl(d), "tags": view_tags(ctx, d),
                                              "hash": definition_hash(ctx, d)} for fn, d in defs.items()})
    work = p["new"] + p["changed"]
    ctx.log(f"     plan: {len(p['new'])} new, {len(p['changed'])} changed, {len(p['unchanged'])} unchanged"
            + (f", {len(p['orphans'])} not in the definitions (left untouched)" if p["orphans"] else ""))
    return {"views": work, "new": p["new"], "changed": p["changed"], "unchanged": p["unchanged"],
            "orphans": p["orphans"], "reasons": p["reasons"]}


def reviewer_input(ctx, fn):
    d = {x["full_name"]: x for x in definitions(ctx)}[fn]
    assets = [d["source"]] + [j["source"] for j in d.get("joins") or []]
    return {
        "metric_view": {"full_name": fn, "comment": d.get("comment"), "owner": d.get("owner"), "source": d["source"],
                        "filter": d.get("filter"), "joins": d.get("joins") or []},
        "dimensions": d["dimensions"],
        "measures": d["measures"],
        "source_columns": {a: source_columns(ctx, a) for a in assets},
        "layers": catalog.describe_layers(ctx.system),
    }


def collect_review(ctx):
    """review.json for the data steward: every new or changed view with its measures, references and findings."""
    work = (ctx.outputs.get("render") or {}).get("views") or []
    reviews = ctx.read_artefact("reviews.json") or []
    defs = {d["full_name"]: d for d in definitions(ctx)}
    reasons = (ctx.outputs.get("render") or {}).get("reasons") or {}
    items = []
    for fn, r in zip(work, reviews):
        d = defs[fn]
        items.append({"metric_view": fn, "owner": d.get("owner"), "plan": reasons.get(fn) or ["new"],
                      "summary": (r or {}).get("summary"), "findings": (r or {}).get("findings") or [],
                      "measures": [{"name": m["name"], "display_name": m.get("display_name"), "expr": m["expr"],
                                    "reference": m["reference"]} for m in d["measures"]],
                      "dimensions": [f["name"] for f in d["dimensions"]]})
    ctx.write_artefact("review.json", items)
    warnings = sum(f["severity"] == "warning" for i in items for f in i["findings"])
    return {"reviewed": len(items), "warnings": warnings}
