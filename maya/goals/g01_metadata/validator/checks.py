"""G1 checks. They read Unity Catalog back (not the artefacts), so they certify what is actually in the workspace."""
import re

from maya.core import catalog

from ..code.common import classes, declared_keys, proposals, rule_class, scope, tag_name, weak
from ..code.keys import fk_orphans, pk_holds


def _uc(ctx):
    tables = {t["full_name"]: t for t in catalog.tables(ctx.ws, ctx.system)}
    cols = {}
    for c in catalog.columns(ctx.ws, ctx.system):
        cols[(c["full_name"], c["column_name"])] = c
    tags = catalog.column_tags(ctx.ws, ctx.system, tag_name(ctx, "sensitivity"))
    cons = {}
    for r in catalog.constraints(ctx.ws, ctx.system):
        cons.setdefault(r["full_name"], {}).setdefault(r["constraint_type"], set()).add(r["column_name"])
    return tables, cols, tags, cons


def _tag(ctx, tags, fn, col):
    return tags.get((fn, col))


def _result(items):
    return {"observed": len(items), "evidence": {"items": items[:200]}}


def undescribed_assets(ctx):
    tables, *_ = _uc(ctx)
    return _result([{"asset": a["full_name"], "kind": "description", "problem": "table description missing"}
                    for a in scope(ctx) if not (tables.get(a["full_name"]) or {}).get("comment")])


def undescribed_columns(ctx):
    _, cols, _, _ = _uc(ctx)
    return _result([{"asset": a["full_name"], "column": c["name"], "kind": "description",
                     "problem": f"column {c['name']} description missing"}
                    for a in scope(ctx) if a["scope"] == "full" for c in a["columns"]
                    if not (cols.get((a["full_name"], c["name"])) or {}).get("comment")])


def weak_descriptions(ctx):
    tables, cols, _, _ = _uc(ctx)
    m = ctx.inputs.get("min_description_chars", 20)
    items = []
    for a in scope(ctx):
        text = (tables.get(a["full_name"]) or {}).get("comment")
        why = weak(text, a["full_name"].split(".")[-1], m) if text else None
        if why:
            items.append({"asset": a["full_name"], "kind": "description", "problem": f"table description {why}"})
        if a["scope"] != "full":
            continue
        for c in a["columns"]:
            text = (cols.get((a["full_name"], c["name"])) or {}).get("comment")
            why = weak(text, c["name"], max(8, m // 2)) if text else None
            if why:
                items.append({"asset": a["full_name"], "column": c["name"], "kind": "description",
                              "problem": f"column {c['name']} description {why}"})
    return _result(items)


def untagged_columns(ctx):
    _, _, tags, _ = _uc(ctx)
    allowed = set(classes(ctx))
    return _result([{"asset": a["full_name"], "column": c["name"], "kind": "tag",
                     "problem": f"{c['name']} sensitivity tag {_tag(ctx, tags, a['full_name'], c['name'])!r}"}
                    for a in scope(ctx) for c in a["columns"]
                    if _tag(ctx, tags, a["full_name"], c["name"]) not in allowed])


def rule_violations(ctx):
    _, _, tags, _ = _uc(ctx)
    items = []
    for a in scope(ctx):
        for c in a["columns"]:
            want = rule_class(ctx, c["name"])
            have = _tag(ctx, tags, a["full_name"], c["name"])
            if want and have != want:
                items.append({"asset": a["full_name"], "column": c["name"], "kind": "tag",
                              "problem": f"{c['name']} is {have!r}, rule requires {want!r}"})
    return _result(items)


def missing_declared_keys(ctx):
    *_, cons = _uc(ctx)
    items = []
    for fn, pk in declared_keys(ctx).items():
        if (cons.get(fn) or {}).get("PRIMARY KEY") != set(pk):
            items.append({"asset": fn, "kind": "key", "problem": f"declared primary key {pk} not in place"})
    return _result(items)


def invalid_keys(ctx):
    *_, cons = _uc(ctx)
    items = []
    for fn, c in cons.items():
        if "PRIMARY KEY" in c:
            ev = pk_holds(ctx, fn, sorted(c["PRIMARY KEY"]))
            if not ev["holds"]:
                items.append({"asset": fn, "kind": "key", "problem": f"primary key does not hold: {ev}"})
    for p in proposals(ctx):
        for fk in p.get("foreign_keys") or []:
            if fk["verified"] and fk["column"] in (cons.get(p["full_name"]) or {}).get("FOREIGN KEY", set()):
                o = fk_orphans(ctx, p["full_name"], fk["column"], fk["parent"], fk["parent_column"])
                if o:
                    items.append({"asset": p["full_name"], "kind": "key", "problem": f"{fk['column']} has {o} orphans"})
    return _result(items)


def unapplied_proposals(ctx):
    tables, cols, tags, cons = _uc(ctx)
    norm = lambda s: re.sub(r"\s+", " ", (s or "").strip())
    items = []
    for p in proposals(ctx):
        fn = p["full_name"]
        if norm((tables.get(fn) or {}).get("comment")) != norm(p["comment"]):
            items.append({"asset": fn, "kind": "apply", "problem": "table comment differs from proposal"})
        for c in p["columns"]:
            if c["comment"] and norm((cols.get((fn, c["name"])) or {}).get("comment")) != norm(c["comment"]):
                items.append({"asset": fn, "column": c["name"], "kind": "apply", "problem": f"{c['name']} comment differs"})
            if _tag(ctx, tags, fn, c["name"]) != c["sensitivity"]:
                items.append({"asset": fn, "column": c["name"], "kind": "apply", "problem": f"{c['name']} tag differs"})
        pk = p.get("primary_key") or {}
        if pk.get("verified") and (cons.get(fn) or {}).get("PRIMARY KEY") != set(pk["columns"]):
            items.append({"asset": fn, "kind": "apply", "problem": "verified primary key not applied"})
        for fk in p.get("foreign_keys") or []:
            if fk["verified"] and fk["column"] not in (cons.get(fn) or {}).get("FOREIGN KEY", set()):
                items.append({"asset": fn, "kind": "apply", "problem": f"verified foreign key {fk['column']} not applied"})
    return _result(items)


def tables_without_pk(ctx):
    *_, cons = _uc(ctx)
    return _result([a["full_name"] for a in scope(ctx) if a["kind"] == "table" and a["scope"] == "full"
                    and "PRIMARY KEY" not in (cons.get(a["full_name"]) or {})])
