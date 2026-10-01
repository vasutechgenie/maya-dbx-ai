"""G3 apply: the approved semantic model becomes bundle scripts, deployed through the project bundle.

scripts/G3/00_schema/registry.sql             schema and registry tables (created when missing)
scripts/G3/10_governed_tags/tag_policies.json domain, subdomain and page governed tags, allowed values = taxonomy ids
scripts/G3/20_registry/<table>.sql            ontology nodes, page objects and glossary (full content, overwritten)
scripts/G3/30_tags/<asset>.sql                the asset's domain, subdomain and page tags
scripts/G3/40_lookup/ontology_lookup.sql      ontology_lookup(search_term) table function
Object names inside the registry are catalog tokens too, so each environment's registry names its own objects."""
from maya.core import bundle
from maya.core.workspace import ident, lit

from . import ledger
from .common import LOOKUP, model, q, registry, target_schema

TABLES = {
    "ontology_nodes": ("node_id STRING NOT NULL, node_type STRING, parent_id STRING, domain_id STRING, subdomain_id STRING, "
                       "name STRING, description STRING, owner STRING, path STRING, source STRING",
                       "Semantic taxonomy: every domain, subdomain and page with its description and owner (MAYA G3)"),
    "ontology_page_objects": ("page_id STRING NOT NULL, subdomain_id STRING, domain_id STRING, object_full_name STRING NOT NULL, "
                              "object_kind STRING, layer STRING, placed_by STRING, confidence DOUBLE",
                              "The page every Silver, Gold and metric-view asset belongs to, exactly one per asset (MAYA G3)"),
    "business_glossary": ("term STRING NOT NULL, definition STRING, synonyms ARRAY<STRING>, page_id STRING, "
                          "linked_object STRING, linked_field STRING, source STRING",
                          "Business glossary: each term once, one row per object or field that implements it (MAYA G3)"),
}


def _arr(xs):
    return f"array({', '.join(lit(x) for x in xs)})" if xs else "CAST(array() AS ARRAY<STRING>)"


def _num(x):
    return "NULL" if x is None else repr(float(x))


def _overwrite(table, rows):
    if not rows:
        return [f"INSERT OVERWRITE {q(table)} SELECT * FROM {q(table)} WHERE false"]
    return [f"INSERT OVERWRITE {q(table)} VALUES\n  " + ",\n  ".join("(" + ", ".join(r) + ")" for r in rows)]


def node_rows(m):
    subs = {s["id"]: s for s in m["subdomains"]}
    doms = {d["id"]: d for d in m["domains"]}
    rows = [[lit(d["id"]), lit("domain"), "NULL", lit(d["id"]), "NULL", lit(d["name"]), lit(d["description"]),
             lit(d["owner"]), lit(d["name"]), lit("user")] for d in m["domains"]]
    rows += [[lit(s["id"]), lit("subdomain"), lit(s["domain"]), lit(s["domain"]), lit(s["id"]), lit(s["name"]),
              lit(s["description"]), lit(s["owner"]), lit(f"{doms[s['domain']]['name']} > {s['name']}"), lit("user")]
             for s in m["subdomains"]]
    rows += [[lit(p["id"]), lit("page"), lit(p["subdomain"]), lit(p["domain"]), lit(p["subdomain"]), lit(p["name"]),
              lit(p["description"]), lit(p["owner"]),
              lit(f"{doms[p['domain']]['name']} > {subs[p['subdomain']]['name']} > {p['name']}"), lit(p["source"])]
             for p in m["pages"]]
    return rows


def object_rows(m):
    return [[lit(a["page"]), lit(a["subdomain"]), lit(a["domain"]), lit(a["asset"]), lit(a["kind"]), lit(a["layer"]),
             lit(a["source"]), _num(a.get("confidence"))] for a in m["assignments"]]


def glossary_rows(m):
    rows = []
    for t in m["glossary"]:
        for x in t["links"] or [{"object": None, "field": None}]:
            rows.append([lit(t["term"]), lit(t["definition"]), _arr(t["synonyms"]), lit(t["page"]) if t["page"] else "NULL",
                         lit(x["object"]) if x["object"] else "NULL", lit(x["field"]) if x["field"] else "NULL",
                         lit(t["source"])])
    return rows


def _schema(ctx):
    c, s = target_schema(ctx)
    out = [f"CREATE SCHEMA IF NOT EXISTS {ident(c, s)} COMMENT 'Semantic model: ontology registry, glossary and lookup (MAYA G3)'"]
    for name, (cols, comment) in TABLES.items():
        out.append(f"CREATE TABLE IF NOT EXISTS {q(registry(ctx, name))} ({cols}) COMMENT {lit(comment)}")
    return out


def _tag_policies(m):
    k = m["tag_keys"]
    return {"tag_policies": [
        {"tag_key": k["domain"], "description": "Business domain of the asset (semantic model, MAYA G3)",
         "values": sorted(d["id"] for d in m["domains"])},
        {"tag_key": k["subdomain"], "description": "Business subdomain of the asset (semantic model, MAYA G3)",
         "values": sorted(s["id"] for s in m["subdomains"])},
        {"tag_key": k["page"], "description": "Semantic page the asset belongs to (semantic model, MAYA G3)",
         "values": sorted(p["id"] for p in m["pages"])}]}


def _asset_tags(m, a):
    k = m["tag_keys"]
    kind = "TABLE" if a["kind"] == "table" else "VIEW"
    return [f"ALTER {kind} {q(a['asset'])} SET TAGS ({lit(k['domain'])} = {lit(a['domain'])}, "
            f"{lit(k['subdomain'])} = {lit(a['subdomain'])}, {lit(k['page'])} = {lit(a['page'])})"]


def _lookup(ctx):
    n, o, g = (q(registry(ctx, t)) for t in ("ontology_nodes", "ontology_page_objects", "business_glossary"))
    t = "lower(trim(search_term))"
    like = f"concat('%', {t}, '%')"
    return [f"""CREATE OR REPLACE FUNCTION {q(registry(ctx, LOOKUP))}(search_term STRING)
RETURNS TABLE (match_type STRING, matched STRING, definition STRING, domain STRING, subdomain STRING, page STRING,
               page_name STRING, object_full_name STRING, object_kind STRING, field STRING)
COMMENT 'What does a business term mean and where does it live: glossary terms and synonyms, domains, subdomains, pages and objects matching search_term, with the page and object for each (MAYA G3)'
RETURN
WITH hits AS (
  SELECT CASE WHEN lower(g.term) = {t} OR exists(g.synonyms, s -> lower(s) = {t}) THEN 1 ELSE 3 END AS rnk,
         'glossary' AS match_type, g.term AS matched, g.definition, g.page_id AS page,
         g.linked_object AS object_full_name, g.linked_field AS field
  FROM {g} g
  WHERE {t} <> '' AND (lower(g.term) LIKE {like} OR exists(g.synonyms, s -> lower(s) LIKE {like}))
  UNION ALL
  SELECT CASE WHEN lower(n.name) = {t} OR n.node_id = {t} THEN 1 ELSE 3 END, n.node_type, n.name, n.description,
         o.page_id, o.object_full_name, CAST(NULL AS STRING)
  FROM {n} n JOIN {o} o ON n.node_id IN (o.page_id, o.subdomain_id, o.domain_id)
  WHERE {t} <> '' AND (lower(n.name) LIKE {like} OR n.node_id = {t})
  UNION ALL
  SELECT CASE WHEN lower(element_at(split(o.object_full_name, '[.]'), -1)) = {t} THEN 2 ELSE 4 END, 'object',
         o.object_full_name, CAST(NULL AS STRING), o.page_id, o.object_full_name, CAST(NULL AS STRING)
  FROM {o} o
  WHERE {t} <> '' AND lower(element_at(split(o.object_full_name, '[.]'), -1)) LIKE {like}
)
SELECT h.match_type, h.matched, h.definition, p.domain_id AS domain, p.subdomain_id AS subdomain, h.page,
       p.name AS page_name, h.object_full_name, ob.object_kind, h.field
FROM hits h
LEFT JOIN {n} p ON p.node_type = 'page' AND p.node_id = h.page
LEFT JOIN {o} ob ON ob.object_full_name = h.object_full_name
ORDER BY h.rnk, h.match_type, h.matched, h.object_full_name"""]


def scripts(ctx, m) -> dict:
    files = {"00_schema/registry.sql": _schema(ctx)}
    if ctx.inputs.get("governed_tags", True):
        files["10_governed_tags/tag_policies.json"] = _tag_policies(m)
    files["20_registry/ontology_nodes.sql"] = _overwrite(registry(ctx, "ontology_nodes"), node_rows(m))
    files["20_registry/ontology_page_objects.sql"] = _overwrite(registry(ctx, "ontology_page_objects"), object_rows(m))
    files["20_registry/business_glossary.sql"] = _overwrite(registry(ctx, "business_glossary"), glossary_rows(m))
    files.update({f"30_tags/{a['asset']}.sql": _asset_tags(m, a) for a in m["assignments"]})
    files["40_lookup/ontology_lookup.sql"] = _lookup(ctx)
    return files


def _catalogs(ctx, m):
    return {target_schema(ctx)[0]} | {a["asset"].split(".")[0] for a in m["assignments"]}


def _deploy(ctx, changed_only):
    m = model(ctx)
    files = scripts(ctx, m)
    base = bundle.scripts_dir(ctx.system, ctx.goal.id)
    stale = [str(p.relative_to(base)) for p in bundle.script_files(base) if str(p.relative_to(base)) not in files]
    written = bundle.write(ctx, files, catalogs=_catalogs(ctx, m), remove=stale)
    todo = bundle.undelivered(ctx, written) if changed_only else written
    res = bundle.deploy(ctx, todo)
    ctx.log(f"     {'apply' if changed_only else 'repair'}: {len(todo)} of {len(files)} scripts deployed"
            + (" (the rest are as certified)" if changed_only and len(todo) < len(files) else ""))
    return {"scripts": len(files), "deployed_scripts": len(todo), "removed": stale, **res}


def apply(ctx):
    return _deploy(ctx, changed_only=True)


def repair(ctx):
    """Re-deploy every script: a failing check means the workspace differs from the scripts."""
    return _deploy(ctx, changed_only=False)


def record(ctx):
    tax = ctx.read_artefact("taxonomy.json")
    ledger.record(ctx, model(ctx), {s["id"]: s["declaration_hash"] for s in tax["subdomains"]})
    return {"recorded": True}


def export(ctx) -> dict:
    """Scripts of the certified semantic model, for `maya bundle`."""
    cert = ledger.certified(ctx)
    if not cert:
        return {"files": {}}
    return {"files": scripts(ctx, cert["model"]), "catalogs": _catalogs(ctx, cert["model"])}
