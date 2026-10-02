"""G6 load / author / assemble. The certified semantic pages and metric views and the customer's page content are the
inputs; the bi_author agent drafts each page's tiles; MAYA proves every tile, adds what the customer required and the
agent left out, and assembles the dashboard the business owner approves."""
from maya.core.spec import stable_hash

from . import ledger
from .common import (content_file, dimension_names, label, marker, measure_names, metric_views, resolve_kpi, semantic_model,
                     slug, tile_dims, tile_rows, tile_shape_problem, tile_sql)


def _page_key(model, key):
    for p in model["pages"]:
        if key in (p["id"], p["name"]) or str(key).lower() == p["name"].lower():
            return p["id"]
    return None


def _inputs(ctx):
    """(semantic model, metric views, page content, errors)."""
    errors = []
    cfile, e1 = content_file(ctx)
    errors += e1
    model = semantic_model(ctx)
    mvs = metric_views(ctx)
    if not mvs:
        errors.append("no metric views to build on (G2's schema has none, or goals.G6.metric_views matches none)")
    if not model["pages"]:
        errors.append("the semantic model (G3) has no pages")
    content = {p["id"]: {} for p in model["pages"]}
    for key, c in (cfile.get("pages") or {}).items():
        pid = _page_key(model, key)
        if not pid:
            errors.append(f"content file: page {key!r} is not a page of the semantic model "
                          f"({', '.join(p['id'] for p in model['pages'])})")
            continue
        kpis = []
        for ref in c.get("kpis") or []:
            fn, m, why = resolve_kpi(mvs, ref)
            if why:
                errors.append(f"page {pid}: {why}")
            else:
                kpis.append({"metric_view": fn, "measure": m})
        for f in c.get("filters") or []:
            if not any(f in dimension_names(mv) for mv in mvs.values()):
                errors.append(f"page {pid}: filter {f!r} is not a dimension of any metric view")
        content[pid] = {"audience": c.get("audience"), "kpis": kpis, "filters": list(c.get("filters") or []),
                        "notes": c.get("notes")}
    return model, mvs, content, errors


def _inputs_hash(model, mvs, content):
    return stable_hash({"content": content, "metric_views": mvs, "pages": model["pages"]})


def load_inputs_hash(ctx) -> str | None:
    """Hash of the current inputs, for drift (None when they are invalid; the next run reports why)."""
    model, mvs, content, errors = _inputs(ctx)
    return None if errors else _inputs_hash(model, mvs, content)


def load(ctx):
    model, mvs, content, errors = _inputs(ctx)
    if errors:
        raise RuntimeError("G6 inputs are invalid:\n  - " + "\n  - ".join(errors))

    maximum = ctx.inputs.get("max_tiles_per_page", 8)
    agent = (ctx.goal.dir / "harness" / "agents" / "bi_author.md").read_text()
    pages, hashes = [], {}
    for p in model["pages"]:
        c = content[p["id"]]
        page = {"page": p, "audience": c.get("audience"), "kpis": c.get("kpis") or [], "filters": c.get("filters") or [],
                "notes": c.get("notes"), "page_metric_views": sorted(o for o in model["objects"].get(p["id"], []) if o in mvs)}
        hashes[p["id"]] = stable_hash({"page": page, "metric_views": mvs, "max": maximum, "agent": agent})
        pages.append(page)
    certified = ledger.certified_authoring(ctx)
    full = ctx.inputs.get("mode") == "full"
    reused = {pid: a for pid, a in certified.items() if not full and hashes.get(pid) == a.get("hash")}
    todo = [p["page"]["id"] for p in pages if p["page"]["id"] not in reused]
    ctx.write_artefact("context.json", {"metric_views": mvs, "pages": pages, "hashes": hashes, "reused": reused,
                                        "inputs_hash": _inputs_hash(model, mvs, content)})
    ctx.log(f"     {len(pages)} pages, {len(mvs)} metric views; authoring {len(todo)} pages"
            + (f", reusing {len(reused)} unchanged" if reused else ""))
    return {"author_pages": todo, "reused_pages": sorted(reused), "pages": len(pages), "metric_views": len(mvs)}


def _context(ctx):
    return ctx.read_artefact("context.json") or {}


def author_input(ctx, page_id):
    c = _context(ctx)
    p = next(x for x in c["pages"] if x["page"]["id"] == page_id)
    return {"page": p["page"], "audience": p["audience"], "notes": p["notes"],
            "required_kpis": [f"{k['metric_view']}.{k['measure']}" for k in p["kpis"]],
            "required_filters": p["filters"], "page_metric_views": p["page_metric_views"],
            "metric_views": list(c["metric_views"].values()), "max_tiles": ctx.inputs.get("max_tiles_per_page", 8)}


def _key(t):
    return (t["kind"], t["metric_view"], tuple(t["measures"]), t.get("dimension"), t.get("series"))


def _clean(t, origin):
    out = {"kind": t["kind"], "title": " ".join(t["title"].split()), "metric_view": t["metric_view"],
           "measures": list(t["measures"]), "origin": origin}
    for k in ("dimension", "series"):
        if t.get(k):
            out[k] = t[k]
    return out


def _page(ctx, c, p, authored, findings):
    pid, mvs, maximum = p["page"]["id"], c["metric_views"], ctx.inputs.get("max_tiles_per_page", 8)
    tiles, seen = [], set()

    def add(t, origin, where):
        t = _clean(t, origin)
        if _key(t) in seen:
            return False
        why = tile_shape_problem(t, mvs)
        if not why:
            _, why = tile_rows(ctx, tile_sql(t["metric_view"], tile_dims(t), t["measures"]))
        if why:
            findings.append(f"page {pid}: {where} tile {t['title']!r} left out: {why}")
            return False
        seen.add(_key(t))
        tiles.append(t)
        return True

    shown = lambda fn, m: any(t["metric_view"] == fn and m in t["measures"] for t in tiles)
    for t in (authored or {}).get("tiles") or []:
        add(t, "agent", "agent")
    required = []
    for k in p["kpis"]:
        if not shown(k["metric_view"], k["measure"]):
            mv = mvs[k["metric_view"]]
            required.append({"kind": "counter", "title": label(mv, k["measure"]), "metric_view": k["metric_view"],
                             "measures": [k["measure"]]})
    before = len(tiles)
    for t in required:
        add(t, "customer", "required")
    tiles = tiles[before:] + tiles[:before]
    must = {(k["metric_view"], k["measure"]) for k in p["kpis"]}
    while len(tiles) > maximum:
        drop = next((t for t in reversed(tiles) if t["origin"] == "agent"
                     and not any((t["metric_view"], m) in must for m in t["measures"])), None)
        if not drop:
            break
        tiles.remove(drop)
        findings.append(f"page {pid}: tile {drop['title']!r} left out: more than {maximum} tiles")

    filters = []
    for f in list(p["filters"]) + list((authored or {}).get("filters") or []):
        if f in filters:
            continue
        if not any(f in dimension_names(mvs[t["metric_view"]]) for t in tiles):
            if f not in p["filters"]:
                findings.append(f"page {pid}: agent filter {f!r} left out: no tile on the page has that dimension")
                continue
            fn = next((t["metric_view"] for t in tiles if f in dimension_names(mvs[t["metric_view"]])), None) or \
                next((k["metric_view"] for k in p["kpis"] if f in dimension_names(mvs[k["metric_view"]])), None) or \
                next((x for x in p["page_metric_views"] if f in dimension_names(mvs[x])), None) or \
                next(x for x, mv in mvs.items() if f in dimension_names(mv))
            m = measure_names(mvs[fn])[0]
            add({"kind": "bar", "title": f"{label(mvs[fn], m)} by {label(mvs[fn], f).lower()}", "metric_view": fn,
                 "measures": [m], "dimension": f}, "maya", "filter")
        filters.append(f)
    if len(filters) > 4:
        findings += [f"page {pid}: filter {f!r} left out: at most 4 filters" for f in filters[4:] if f not in p["filters"]]
        filters = [f for f in filters if f in p["filters"]] + [f for f in filters if f not in p["filters"]][:max(0, 4 - len(p["filters"]))]
    if not tiles:
        raise RuntimeError(f"page {pid} has no tile: the bi_author agent returned none that run. Run G6 again.")
    return {"id": pid, "name": p["page"]["name"], "description": p["page"].get("description"), "path": p["page"]["path"],
            "audience": p["audience"], "tiles": tiles, "filters": filters}


def assemble(ctx):
    c = _context(ctx)
    todo = (ctx.outputs.get("load") or {}).get("author_pages") or []
    results = {r["page"]: r for r in ctx.read_artefact("authoring.json") or [] if isinstance(r, dict)}
    authoring, findings = dict(c["reused"]), []
    for pid in todo:
        r = results.get(pid)
        if not r:
            findings.append(f"page {pid}: the bi_author agent returned no result")
            continue
        authoring[pid] = {"hash": c["hashes"][pid], "tiles": r["tiles"], "filters": r["filters"]}
    pages = [_page(ctx, c, p, authoring.get(p["page"]["id"]), findings) for p in c["pages"]]
    used = sorted({t["metric_view"] for p in pages for t in p["tiles"]})
    names, datasets = set(), []
    for fn in used:
        n = f"ds_{slug(fn.split('.')[2])}"
        n = n if n not in names else f"ds_{slug(fn)}"
        names.add(n)
        datasets.append({"name": n, "metric_view": fn, "display_name": fn.split(".")[2].replace("_", " ").capitalize()})
    spec = {"marker": marker(ctx), "title": ctx.inputs["title"], "parent_path": ctx.inputs.get("parent_path", "MAYA"),
            "credentials": ctx.inputs.get("credentials", "embedded"), "schedule": _schedule(ctx),
            "subscribers": ctx.inputs.get("subscribers") or [], "access": ctx.inputs.get("access") or [],
            "datasets": datasets, "pages": pages, "metric_views": {fn: c["metric_views"][fn] for fn in used},
            "inputs_hash": c["inputs_hash"], "findings": findings, "authoring": authoring}
    ctx.write_artefact("dashboard.json", spec)
    n = sum(len(p["tiles"]) for p in pages)
    ctx.log(f"     dashboard: {len(pages)} pages, {n} tiles on {len(datasets)} metric views"
            + (f"; {len(findings)} findings" if findings else ""))
    return {"pages": len(pages), "tiles": n, "datasets": len(datasets), "findings": len(findings)}


def _schedule(ctx):
    s = ctx.inputs.get("schedule")
    if not s:
        return None
    return {"cron": s["cron"], "timezone": s.get("timezone", "UTC"), "paused": bool(s.get("paused", False))}
