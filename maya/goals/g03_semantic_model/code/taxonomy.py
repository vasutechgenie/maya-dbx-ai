"""G3 load / plan / resolve / assemble. Domains and subdomains come only from the user's taxonomy YAML. Pages come
from the YAML where declared; agents design the rest ('auto', partial pages, allow_new_pages) and place every asset
the YAML does not place. Certified agent work is kept on later runs unless its subdomain declaration changed."""
import jsonschema
import yaml

from maya.core import catalog
from maya.core.spec import stable_hash

from . import ledger
from .common import (columns_of, metric_view_schemas, raw_taxonomy, resolve_asset, resolve_link, scope_assets, slug,
                     tag_keys, target_schema)

ID_OK = jsonschema.Draft202012Validator({"type": "string", "pattern": "^[a-z][a-z0-9_]{0,62}$"})


def _tax(ctx):
    return ctx.read_artefact("taxonomy.json")


def _assets(ctx) -> dict:
    return {a["full_name"]: a for a in ctx.read_artefact("assets.json") or []}


def _brief(a) -> dict:
    """What an agent needs to know about an asset (bounded in size)."""
    return {"full_name": a["full_name"], "kind": a["kind"], "layer": a["layer"],
            "description": (a.get("comment") or "")[:400],
            "columns": [c["name"] + (f": {c['comment'][:80]}" if c.get("comment") else "") for c in a["columns"][:40]]}


# ---------------------------------------------------------------- load
def load(ctx):
    errors = []
    c, s = target_schema(ctx)
    if catalog.layer_of(ctx.system, c, s):
        errors.append(f"schema {c}.{s} is a foundation layer schema; the semantic model needs its own schema")
    if (c, s) in metric_view_schemas(ctx):
        errors.append(f"schema {c}.{s} holds the metric views; the semantic model needs its own schema")
    raw = raw_taxonomy(ctx)
    defs = ctx.goal.inputs_schema()["$defs"]
    bad = sorted(jsonschema.Draft202012Validator({"$ref": "#/$defs/taxonomy", "$defs": defs}).iter_errors(raw),
                 key=lambda e: list(e.path))
    if bad:
        raise RuntimeError(f"taxonomy {ctx.inputs['taxonomy']} is invalid:\n  - " + "\n  - ".join(
            f"{e.message} at {'/'.join(map(str, e.path)) or 'root'}" for e in bad[:12]))
    assets = scope_assets(ctx)
    known = [a["full_name"] for a in assets]
    if not assets:
        errors.append("no Silver, Gold or metric-view assets in scope")
    cols = columns_of(ctx, known)

    domains, subdomains, pages, pins = [], [], [], {}
    for d in raw["domains"]:
        domains.append({k: d[k] for k in ("id", "name", "description", "owner")})
        for sd in d["subdomains"]:
            given = sd.get("pages", "auto")
            owner = sd.get("owner") or d["owner"]
            mode = "auto" if given == "auto" else ("extend" if sd.get("allow_new_pages") else "fixed")
            subdomains.append({"id": sd["id"], "domain": d["id"], "name": sd["name"], "description": sd["description"],
                               "owner": owner, "pages_mode": mode,
                               "declaration_hash": stable_hash({"domain": {k: d[k] for k in ("id", "name", "description")},
                                                                "subdomain": sd})})
            for p in [] if given == "auto" else given:
                pid = p.get("id") or slug(p["name"])
                pages.append({"id": pid, "domain": d["id"], "subdomain": sd["id"], "name": p["name"],
                              "description": p.get("description"), "owner": p.get("owner") or owner, "source": "user"})
                for a in p.get("assets") or []:
                    fn = resolve_asset(ctx, a, known)
                    if not fn:
                        errors.append(f"page {pid}: {a!r} is not a Silver, Gold or metric-view asset in scope")
                    elif fn in pins:
                        errors.append(f"{fn} is placed on two pages ({pins[fn]}, {pid}); an asset belongs to one page")
                    else:
                        pins[fn] = pid
    for kind, items in (("domain", domains), ("subdomain", subdomains), ("page", pages)):
        ids = [i["id"] for i in items]
        errors += [f"{kind} id {i!r} is used twice" for i in sorted({i for i in ids if ids.count(i) > 1})]

    terms = []
    for t in raw.get("glossary") or []:
        links = []
        for link in t.get("links") or []:
            fn, field = resolve_link(ctx, link, known, cols)
            if fn:
                links.append({"object": fn, "field": field})
            else:
                errors.append(f"glossary term {t['term']!r}: {field}")
        terms.append({"term": t["term"], "definition": t["definition"], "synonyms": t.get("synonyms") or [],
                      "links": links, "source": "user"})
    names = [t["term"].lower() for t in terms]
    errors += [f"glossary term {n!r} is defined twice" for n in sorted({n for n in names if names.count(n) > 1})]
    if errors:
        raise RuntimeError("the semantic model input is invalid:\n  - " + "\n  - ".join(errors))

    ctx.write_artefact("taxonomy.json", {"domains": domains, "subdomains": subdomains, "pages": pages, "pins": pins,
                                         "glossary": terms, "declaration_hash": stable_hash(raw)})
    ctx.write_artefact("assets.json", [dict(a, columns=cols[a["full_name"]]) for a in assets])
    return {"domains": len(domains), "subdomains": len(subdomains), "declared_pages": len(pages),
            "assets": len(assets), "placed_by_yaml": len(pins), "glossary_terms": len(terms)}


# ---------------------------------------------------------------- plan
def plan(ctx):
    """Subdomains whose pages agents must design (or complete). Certified designs are reused while the subdomain's
    declaration is unchanged, so incremental runs keep pages and tags stable."""
    tax, cert = _tax(ctx), ledger.certified(ctx)
    full = ctx.inputs.get("mode") == "full"
    design, reused = [], []
    for sd in tax["subdomains"]:
        given = [p for p in tax["pages"] if p["subdomain"] == sd["id"]]
        if sd["pages_mode"] == "fixed" and all(p["description"] for p in given):
            continue
        if not full and cert and cert["subdomains"].get(sd["id"]) == sd["declaration_hash"]:
            reused.append(sd["id"])
        else:
            design.append(sd["id"])
    ctx.write_artefact("plan.json", {"design": design, "reused": reused})
    ctx.log(f"     pages: {len(design)} subdomains for the page designer"
            + (f", {len(reused)} kept from certification" if reused else ""))
    return {"design": design, "reused": reused}


def designer_input(ctx, sid):
    tax, assets = _tax(ctx), _assets(ctx)
    sd = next(s for s in tax["subdomains"] if s["id"] == sid)
    d = next(x for x in tax["domains"] if x["id"] == sd["domain"])
    pins = tax["pins"]
    given = [p for p in tax["pages"] if p["subdomain"] == sid]
    return {
        "domain": {k: d[k] for k in ("id", "name", "description")},
        "subdomain": {"id": sid, "name": sd["name"], "description": sd["description"],
                      "pages": "auto" if sd["pages_mode"] == "auto" else
                      [{"id": p["id"], "name": p["name"], "description": p["description"],
                        "assets": sorted(fn for fn, pid in pins.items() if pid == p["id"])} for p in given],
                      "may_add_pages": sd["pages_mode"] != "fixed"},
        "taxonomy": [{"domain": s["domain"], "subdomain": s["id"], "name": s["name"], "description": s["description"]}
                     for s in tax["subdomains"]],
        "page_ids_in_use": sorted(p["id"] for p in tax["pages"] if p["subdomain"] != sid),
        "assets": [_brief(a) for fn, a in assets.items() if fn not in pins],
        "already_placed": [{"asset": fn, "page": pid} for fn, pid in sorted(pins.items())],
    }


# ---------------------------------------------------------------- resolve
def _unique(pid, taken):
    n, out = 2, pid
    while out in taken:
        out, n = f"{pid[:60]}_{n}", n + 1
    return out


def resolve(ctx):
    """Merge declared, certified and designed pages; decide every asset that has one clear page; batch the rest
    (unclaimed, or claimed by several pages) for the assigner."""
    tax, assets, cert = _tax(ctx), _assets(ctx), ledger.certified(ctx)
    p = ctx.read_artefact("plan.json")
    designs = ctx.read_artefact("designs.json") or []
    subs = {s["id"]: s for s in tax["subdomains"]}
    pages = {x["id"]: dict(x) for x in tax["pages"]}
    notes = []

    for x in (cert["model"]["pages"] if cert else []):
        if x["subdomain"] not in p["reused"]:
            continue
        if x["id"] in pages:
            pages[x["id"]]["description"] = pages[x["id"]]["description"] or x["description"]
        else:
            pages[x["id"]] = dict(x, owner=subs[x["subdomain"]]["owner"], source="certified")

    claims = {}
    for sid, res in zip(p["design"], designs):
        sd = subs[sid]
        for x in (res or {}).get("pages") or []:
            pid = x.get("id") if not list(ID_OK.iter_errors(x.get("id"))) else slug(x.get("name", "page"))
            if pid in pages and pages[pid]["subdomain"] == sid:
                if not pages[pid]["description"]:
                    pages[pid]["description"] = x.get("description")
            elif sd["pages_mode"] == "fixed":
                notes.append(f"{sid}: page {pid!r} proposed but the subdomain's pages are fixed; ignored")
                continue
            else:
                pid = _unique(pid, pages)
                pages[pid] = {"id": pid, "domain": sd["domain"], "subdomain": sid, "name": x["name"],
                              "description": x.get("description"), "owner": sd["owner"], "source": "agent"}
            for c in x.get("assets") or []:
                fn = resolve_asset(ctx, c.get("asset", ""), list(assets))
                if fn and fn not in tax["pins"]:
                    claims.setdefault(fn, []).append({"page": pid, "confidence": c.get("confidence", 0.5),
                                                      "rationale": c.get("rationale")})

    decided = {fn: {"page": pid, "source": "user", "confidence": 1.0, "rationale": "declared in the taxonomy"}
               for fn, pid in tax["pins"].items()}
    kept = {} if not cert or ctx.inputs.get("mode") == "full" else {a["asset"]: a for a in cert["model"]["assignments"]}
    pending = []
    for fn in assets:
        if fn in decided:
            continue
        k = kept.get(fn)
        if k and k["page"] in pages and pages[k["page"]]["subdomain"] not in p["design"]:
            decided[fn] = {"page": k["page"], "source": "certified", "confidence": k.get("confidence"),
                           "rationale": k.get("rationale")}
            continue
        cands = {}
        for c in claims.get(fn, []):
            if c["page"] not in cands or c["confidence"] > cands[c["page"]]["confidence"]:
                cands[c["page"]] = c
        if len(cands) == 1:
            (pid, c), = cands.items()
            decided[fn] = {"page": pid, "source": "agent", "confidence": c["confidence"], "rationale": c["rationale"]}
        else:
            pending.append({"asset": fn, "candidates": sorted(cands)})
    if pending and not pages:
        raise RuntimeError("no pages to place assets on: declare pages or set a subdomain's pages to 'auto'")
    n = max(1, int(ctx.inputs.get("assign_batch", 8)))
    batches = [[x["asset"] for x in pending[i:i + n]] for i in range(0, len(pending), n)]
    ctx.write_artefact("draft.json", {"pages": list(pages.values()), "decided": decided, "pending": pending,
                                      "notes": notes})
    ctx.log(f"     placement: {len(decided)} decided, {len(pending)} for the assigner"
            + (f"; {len(notes)} notes" if notes else ""))
    return {"batches": batches, "pages": len(pages), "decided": len(decided), "pending": len(pending), "notes": notes}


def assigner_input(ctx, batch):
    draft, assets = ctx.read_artefact("draft.json"), _assets(ctx)
    subs = {s["id"]: s for s in _tax(ctx)["subdomains"]}
    on = {}
    for fn, d in draft["decided"].items():
        on.setdefault(d["page"], []).append(fn)
    cands = {x["asset"]: x["candidates"] for x in draft["pending"]}
    return {
        "pages": [{"id": x["id"], "name": x["name"], "description": x["description"],
                   "subdomain": f"{subs[x['subdomain']]['name']} ({x['domain']})",
                   "assets": sorted(on.get(x["id"], []))} for x in draft["pages"]],
        "assets": [dict(_brief(assets[fn]), candidate_pages=cands.get(fn, [])) for fn in batch],
    }


# ---------------------------------------------------------------- assemble
def _measure_terms(ctx, assets):
    from maya.goals.g02_metric_views.code.common import read_back
    terms = {}
    for fn, a in assets.items():
        if a["kind"] != "metric_view":
            continue
        for m in (read_back(ctx, fn) or {}).get("measures") or []:
            name = m.get("display_name") or m["name"]
            t = terms.setdefault(name.lower(), {"term": name, "definition": m.get("comment") or name,
                                                "synonyms": list(m.get("synonyms") or []), "links": [],
                                                "source": "metric_view"})
            t["links"].append({"object": fn, "field": m["name"]})
            t["synonyms"] += [s for s in m.get("synonyms") or [] if s not in t["synonyms"]]
    return terms


def glossary(ctx, tax, assets, placed) -> list[dict]:
    terms = _measure_terms(ctx, assets) if ctx.inputs.get("glossary_from_metric_views", True) else {}
    for t in tax["glossary"]:
        auto = terms.get(t["term"].lower())
        links = t["links"] + [x for x in (auto or {}).get("links", []) if x not in t["links"]]
        terms[t["term"].lower()] = dict(t, links=links)
    out = []
    for t in sorted(terms.values(), key=lambda t: t["term"].lower()):
        page = next((placed[x["object"]] for x in t["links"] if x["object"] in placed), None)
        out.append(dict(t, page=page))
    return out


def resolved_yaml(m) -> str:
    """The complete taxonomy (every page and its assets) as the user's YAML: copy it to freeze agent-built pages."""
    on = {}
    for a in m["assignments"]:
        on.setdefault(a["page"], []).append(a["asset"].split(".", 1)[1])
    doc = {"domains": []}
    for d in m["domains"]:
        dd = dict(d, subdomains=[])
        for s in (s for s in m["subdomains"] if s["domain"] == d["id"]):
            dd["subdomains"].append({"id": s["id"], "name": s["name"], "description": s["description"],
                                     "owner": s["owner"],
                                     "pages": [{"id": p["id"], "name": p["name"], "description": p["description"],
                                                "assets": sorted(on.get(p["id"], []))}
                                               for p in m["pages"] if p["subdomain"] == s["id"]] or "auto"})
        doc["domains"].append(dd)
    user = [{k: t[k] for k in ("term", "definition", "synonyms")} |
            {"links": [x["object"].split(".", 1)[1] + (f".{x['field']}" if x["field"] else "") for x in t["links"]]}
            for t in m["glossary"] if t["source"] == "user"]
    if user:
        doc["glossary"] = user
    return ("# Resolved by MAYA G3: every page and the assets on it. Use it as the taxonomy file to fix the pages.\n"
            + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120))


def build_model(ctx, pages, decided) -> dict:
    tax, assets = _tax(ctx), _assets(ctx)
    used = {d["page"] for d in decided.values()}
    pages = [x for x in pages if x["source"] == "user" or x["id"] in used]
    path = {x["id"]: x for x in pages}
    assignments = [{"asset": fn, "kind": assets[fn]["kind"], "layer": assets[fn]["layer"], "page": d["page"],
                    "subdomain": path[d["page"]]["subdomain"], "domain": path[d["page"]]["domain"],
                    "source": d["source"], "confidence": d.get("confidence"), "rationale": d.get("rationale")}
                   for fn, d in sorted(decided.items())]
    return {"tag_keys": tag_keys(ctx), "declaration_hash": tax["declaration_hash"],
            "domains": tax["domains"],
            "subdomains": [{k: s[k] for k in ("id", "domain", "name", "description", "owner")} for s in tax["subdomains"]],
            "pages": [{k: x.get(k) for k in ("id", "domain", "subdomain", "name", "description", "owner", "source")}
                      for x in pages],
            "assignments": assignments,
            "glossary": glossary(ctx, tax, assets, {a["asset"]: a["page"] for a in assignments})}


def assemble(ctx):
    draft, assets = ctx.read_artefact("draft.json"), _assets(ctx)
    batches = (ctx.outputs.get("resolve") or {}).get("batches") or []
    results = ctx.read_artefact("assignments.json") or []
    pages = {x["id"]: x for x in draft["pages"]}
    cands = {x["asset"]: x["candidates"] for x in draft["pending"]}
    decided = dict(draft["decided"])
    for batch, res in zip(batches, results):
        got = {}
        for x in (res or {}).get("assignments") or []:
            fn = resolve_asset(ctx, x.get("asset", ""), list(assets))
            if fn in batch and x.get("page") in pages:
                got[fn] = x
        for fn in batch:
            x = got.get(fn)
            if x:
                decided[fn] = {"page": x["page"], "source": "agent", "confidence": x.get("confidence"),
                               "rationale": x.get("rationale")}
            else:
                decided[fn] = {"page": (cands.get(fn) or list(pages))[0], "source": "agent", "confidence": 0.0,
                               "rationale": "the assigner returned no valid page; placed on the first candidate for review"}
    m = build_model(ctx, list(pages.values()), decided)
    ctx.write_artefact("semantic_model.json", m)
    ctx.write_artefact("semantic_model.proposed.json", m)
    ctx.artefact("taxonomy.resolved.yaml").write_text(resolved_yaml(m))
    low = [a["asset"] for a in m["assignments"]
           if a["source"] == "agent" and (a["confidence"] or 0) < ctx.inputs.get("min_confidence", 0.6)]
    ctx.log(f"     model: {len(m['domains'])} domains, {len(m['subdomains'])} subdomains, {len(m['pages'])} pages, "
            f"{len(m['assignments'])} assets, {len(m['glossary'])} glossary terms"
            + (f"; {len(low)} low-confidence placements" if low else ""))
    return {"pages": len(m["pages"]), "assets": len(m["assignments"]), "glossary": len(m["glossary"]),
            "low_confidence": low, "by_source": {s: sum(a["source"] == s for a in m["assignments"])
                                                 for s in ("user", "certified", "agent")}}
