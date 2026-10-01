"""G3 checks. They read the registry, tags and tag policies back from Unity Catalog and call ontology_lookup(), so they
certify what is actually in the workspace."""
from concurrent.futures import ThreadPoolExecutor

from maya.core import catalog
from maya.core.workspace import SqlError, lit

from ..code.apply import _tag_policies
from ..code.common import LOOKUP, model, q, registry, scope_assets
from ..code.ledger import live_tags


def _result(items):
    return {"observed": len(items), "evidence": {"items": items[:200]}}


def _read(ctx, table, cols):
    try:
        return ctx.ws.sql(f"SELECT {cols} FROM {q(registry(ctx, table))}")
    except SqlError as e:
        return {"error": str(e).splitlines()[0][:300]}


def _diff(kind, want: dict, got: dict):
    items = [{"kind": kind, "key": k, "problem": "missing in Unity Catalog"} for k in sorted(set(want) - set(got))]
    items += [{"kind": kind, "key": k, "problem": "not in the approved model"} for k in sorted(set(got) - set(want))]
    items += [{"kind": kind, "key": k, "problem": f"differs: {got[k]} != {want[k]}"}
              for k in sorted(set(want) & set(got)) if got[k] != want[k]]
    return items


def registry_differences(ctx):
    m = model(ctx)
    rows = _read(ctx, "ontology_nodes", "node_id, node_type, parent_id, name, description, owner")
    if isinstance(rows, dict):
        return _result([{"kind": "registry", "problem": rows["error"]}])
    want = {d["id"]: ("domain", None, d["name"], d["description"], d["owner"]) for d in m["domains"]}
    want |= {s["id"]: ("subdomain", s["domain"], s["name"], s["description"], s["owner"]) for s in m["subdomains"]}
    want |= {p["id"]: ("page", p["subdomain"], p["name"], p["description"], p["owner"]) for p in m["pages"]}
    ids = [r["node_id"] for r in rows]
    items = [{"kind": "registry", "key": i, "problem": "listed twice"} for i in sorted({i for i in ids if ids.count(i) > 1})]
    got = {r["node_id"]: (r["node_type"], r["parent_id"], r["name"], r["description"], r["owner"]) for r in rows}
    items += _diff("registry", want, got)
    items += [{"kind": "registry", "key": k, "problem": "no description or owner"}
              for k, v in want.items() if not v[3] or not v[4]]
    return _result(items)


def governed_tag_differences(ctx):
    if not ctx.inputs.get("governed_tags", True):
        return _result([])
    items = []
    for p in _tag_policies(model(ctx))["tag_policies"]:
        values = catalog.tag_policy_values(ctx.ws, p["tag_key"])
        if values is None:
            items.append({"kind": "governed_tag", "key": p["tag_key"], "problem": "not a governed tag (or no allowed values)"})
        elif sorted(values) != p["values"]:
            items.append({"kind": "governed_tag", "key": p["tag_key"],
                          "problem": f"allowed values {sorted(values)} != taxonomy {p['values']}"})
    return _result(items)


def untagged_assets(ctx):
    m = model(ctx)
    keys = m["tag_keys"]
    placed = {a["asset"]: a for a in m["assignments"]}
    now = [a["full_name"] for a in scope_assets(ctx)]
    items = [{"kind": "tag", "asset": fn, "problem": "in scope but has no page in the model"} for fn in now if fn not in placed]
    live = live_tags(ctx, [fn for fn in now if fn in placed], list(keys.values()))
    for fn, tags in sorted(live.items()):
        a = placed[fn]
        want = {keys["domain"]: a["domain"], keys["subdomain"]: a["subdomain"], keys["page"]: a["page"]}
        bad = {k: tags.get(k) for k, v in want.items() if tags.get(k) != v}
        if bad:
            items.append({"kind": "tag", "asset": fn, "problem": f"tags {bad} != {want}"})
    return _result(items)


def page_object_differences(ctx):
    m = model(ctx)
    rows = _read(ctx, "ontology_page_objects", "object_full_name, page_id, subdomain_id, domain_id")
    if isinstance(rows, dict):
        return _result([{"kind": "page_objects", "problem": rows["error"]}])
    names = [r["object_full_name"] for r in rows]
    items = [{"kind": "page_objects", "key": n, "problem": "on more than one page"}
             for n in sorted({n for n in names if names.count(n) > 1})]
    want = {a["asset"]: (a["page"], a["subdomain"], a["domain"]) for a in m["assignments"]}
    got = {r["object_full_name"]: (r["page_id"], r["subdomain_id"], r["domain_id"]) for r in rows}
    return _result(items + _diff("page_objects", want, got))


def glossary_differences(ctx):
    m = model(ctx)
    rows = _read(ctx, "business_glossary", "term, definition, page_id, linked_object, linked_field")
    if isinstance(rows, dict):
        return _result([{"kind": "glossary", "problem": rows["error"]}])
    want = {}
    for t in m["glossary"]:
        for x in t["links"] or [{"object": None, "field": None}]:
            want[f"{t['term']} -> {x['object']}.{x['field']}"] = (t["definition"], t["page"])
    got = {f"{r['term']} -> {r['linked_object']}.{r['linked_field']}": (r["definition"], r["page_id"]) for r in rows}
    items = _diff("glossary", want, got)
    items += [{"kind": "glossary", "key": t["term"], "problem": "linked to no object or field"}
              for t in m["glossary"] if not t["links"]]
    return _result(items)


def lookup_failures(ctx):
    """Every glossary term and every page name must be answered by ontology_lookup() with its page."""
    m = model(ctx)
    fn = q(registry(ctx, LOOKUP))
    probes = [("term", t["term"], t["page"]) for t in m["glossary"]] + [("page", p["name"], p["id"]) for p in m["pages"]]

    def one(probe):
        kind, text, page = probe
        try:
            rows = ctx.ws.sql(f"SELECT * FROM {fn}({lit(text)})")
        except SqlError as e:
            return {"kind": "lookup", "search": text, "problem": str(e).splitlines()[0][:300]}
        if kind == "term":
            ok = any(r["match_type"] == "glossary" and r["matched"].lower() == text.lower()
                     and (page is None or r["page"] == page) for r in rows)
        else:
            ok = any(r["page"] == page for r in rows)
        return None if ok else {"kind": "lookup", "search": text, "expected_page": page,
                                "problem": f"no {'glossary' if kind == 'term' else 'page'} answer ({len(rows)} rows)"}

    with ThreadPoolExecutor(max_workers=6) as pool:
        items = [r for r in pool.map(one, probes) if r]
    res = _result(items)
    res["evidence"]["probes"] = len(probes)
    return res


def empty_pages(ctx):
    m = model(ctx)
    used = {a["page"] for a in m["assignments"]}
    return _result([{"kind": "page", "key": p["id"], "problem": "no asset on this page"} for p in m["pages"] if p["id"] not in used])


def low_confidence_assignments(ctx):
    floor = ctx.inputs.get("min_confidence", 0.6)
    return _result([{"kind": "placement", "asset": a["asset"], "page": a["page"], "confidence": a["confidence"],
                     "problem": a.get("rationale")}
                    for a in model(ctx)["assignments"] if a["source"] == "agent" and (a["confidence"] or 0) < floor])
