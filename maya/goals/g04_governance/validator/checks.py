"""G4 checks. They read functions, policies, masks, filters and grants back from Unity Catalog, query the audit view
and lineage, and inspect job settings, so they certify what is actually in the workspace."""
import json
import re

from maya.core import catalog
from maya.core import bundle
from maya.core.workspace import SqlError, ident, lit

from ..code.common import (TAG, column_masks, plan, policy, principal_kind, privileges, product_schemas, q,
                           routine_comments, row_filters, sensitive_columns)

TOKEN = re.compile(r"dapi[0-9a-f]{32}")


def _result(items):
    return {"observed": len(items), "evidence": {"items": items[:200]}}


def _catalogs(p):
    return sorted({x.split(".")[0] for x in [p["schema"], *p["audit"]["schemas"]]})


def _live_policies(ctx, p):
    return {(x["name"], x["schema"]): policy(ctx, x["name"], x["schema"]) for x in p["policies"]}


def _stamp_ok(declared_comment, live_comment):
    want = TAG.search(declared_comment or "")
    return bool(want) and want.group(1) in (live_comment or "")


def unprotected_columns(ctx):
    p = plan(ctx)
    masks = column_masks(ctx, _catalogs(p))
    live = _live_policies(ctx, p)
    tag_values = {t: catalog.column_tags(ctx.ws, ctx.system, t) for t in {x["tag"] for x in p["policies"]}}
    items = []
    for c in sensitive_columns(ctx):
        by = [f"policy {x['name']}" for x in p["policies"]
              if live.get((x["name"], x["schema"])) and c["table"].rsplit(".", 1)[0] == x["schema"]
              and tag_values[x["tag"]].get((c["table"], c["column"])) == x["value"]]
        if (c["table"], c["column"]) in masks:
            by.append(f"column mask {masks[(c['table'], c['column'])]}")
        if len(by) != 1:
            items.append({"kind": "column", "key": f"{c['table']}.{c['column']}",
                          "problem": f"{c['value']} column masked by {' and '.join(by) if by else 'nothing'}"})
    return _result(items)


def function_differences(ctx):
    p = plan(ctx)
    live = routine_comments(ctx, p["schema"])
    return _result([{"kind": "function", "key": f["full_name"],
                     "problem": "missing" if f["full_name"] not in live else "definition differs from the plan"}
                    for f in p["functions"] if not _stamp_ok(f["comment"], live.get(f["full_name"]))])


def policy_differences(ctx):
    p = plan(ctx)
    items = []
    for (name, schema), info in _live_policies(ctx, p).items():
        x = next(y for y in p["policies"] if (y["name"], y["schema"]) == (name, schema))
        if info is None:
            items.append({"kind": "policy", "key": f"{schema}.{name}", "problem": "missing"})
        elif not _stamp_ok(x["comment"], info.get("Comment")):
            items.append({"kind": "policy", "key": f"{schema}.{name}", "problem": "definition differs from the plan"})
    items += [{"kind": "policy", "key": f"{x['schema']}.{x['name']}", "problem": "removed from the plan but still exists"}
              for x in p["removed"]["policies"] if policy(ctx, x["name"], x["schema"]) is not None]
    return _result(items)


def column_mask_differences(ctx):
    p = plan(ctx)
    live = column_masks(ctx, _catalogs(p))
    items = [{"kind": "column_mask", "key": f"{m['table']}.{m['column']}",
              "problem": f"mask {live.get((m['table'], m['column']))} != {m['function']}"}
             for m in p["column_masks"] if live.get((m["table"], m["column"])) != m["function"]]
    items += [{"kind": "column_mask", "key": f"{m['table']}.{m['column']}", "problem": "removed from the plan but still attached"}
              for m in p["removed"]["column_masks"] if (m["table"], m["column"]) in live]
    return _result(items)


def row_filter_differences(ctx):
    p = plan(ctx)
    live = row_filters(ctx, _catalogs(p))
    items = [{"kind": "row_filter", "key": f["table"], "problem": f"filter {live.get(f['table'])} != {f['function']} on {f['columns']}"}
             for f in p["row_filters"]
             if (live.get(f["table"]) or {}).get("function") != f["function"]
             or (live.get(f["table"]) or {}).get("columns") != f["columns"]]
    items += [{"kind": "row_filter", "key": f["table"], "problem": "removed from the plan but still attached"}
              for f in p["removed"]["row_filters"] if f["table"] in live]
    return _result(items)


def _key(g):
    return (g["principal"], g["privilege"], g["securable_type"], g["securable"])


def _live_grants(ctx, p):
    schemas = sorted({g["securable"] if g["securable_type"] == "SCHEMA" else g["securable"].rsplit(".", 1)[0]
                      for g in p["grants"] if g["securable_type"] != "CATALOG"} | set(product_schemas(ctx)))
    return privileges(ctx, schemas)


def missing_grants(ctx):
    p = plan(ctx)
    live = {_key(g) for g in _live_grants(ctx, p)}
    items = [{"kind": "grant", "key": " ".join(_key(g)), "problem": "not granted"} for g in p["grants"] if _key(g) not in live]
    items += [{"kind": "grant", "key": " ".join(_key(g)), "problem": "removed from the plan but still granted"}
              for g in p["removed"]["grants"] if _key(g) in live]
    return _result(items)


def catalog_access_gaps(ctx):
    p = plan(ctx)
    items = []
    for cat in sorted({g["securable"].split(".")[0] for g in p["grants"]}):
        try:
            rows = ctx.ws.sql(f"SELECT grantee, privilege_type FROM {ident(cat)}.information_schema.catalog_privileges")
        except SqlError as e:
            items.append({"kind": "catalog", "key": cat, "problem": str(e).splitlines()[0][:200]})
            continue
        users = {r["grantee"] for r in rows if r["privilege_type"] in ("USE_CATALOG", "ALL_PRIVILEGES")}
        for who in sorted({g["principal"] for g in p["grants"] if g["securable"].split(".")[0] == cat}):
            if who not in users and "account users" not in users:
                items.append({"kind": "catalog", "key": f"{cat} {who}", "problem": "no USE CATALOG"})
    return _result(items)


def non_unity_catalog_objects(ctx):
    ok = {"MANAGED", "EXTERNAL", "VIEW", "METRIC_VIEW", "MATERIALIZED_VIEW", "STREAMING_TABLE"}
    items = [{"kind": "catalog", "key": c, "problem": "Hive metastore catalog"} for c in ctx.system.catalogs
             if c in ("hive_metastore", "spark_catalog")]
    items += [{"kind": "object", "key": t["full_name"], "problem": f"table type {t['table_type']}"}
              for t in catalog.tables(ctx.ws, ctx.system) if t["table_type"] not in ok]
    return _result(items)


def audit_gaps(ctx):
    p = plan(ctx)
    try:
        rows = ctx.ws.sql(f"SELECT count(*) AS n, count(DISTINCT object_full_name) AS objects FROM {q(p['audit']['full_name'])}")
    except SqlError as e:
        return _result([{"kind": "audit", "key": p["audit"]["full_name"], "problem": str(e).splitlines()[0][:300]}])
    n = int(rows[0]["n"])
    res = _result([] if n else [{"kind": "audit", "key": p["audit"]["full_name"], "problem": "no audit events"}])
    res["evidence"].update(events=n, objects=int(rows[0]["objects"]))
    return res


def consumer_exposure(ctx):
    p = plan(ctx)
    consumers = set(p["consumers"])
    raw = [s for layer in ("bronze", "silver") if layer in ctx.system.layers
           for s in (f"{x['catalog']}.{x['schema']}" for x in ctx.system.layers[layer]["sources"])]
    return _result([{"kind": "grant", "key": " ".join(_key(g)), "problem": "consumer privilege on Bronze or Silver (left as is)"}
                    for g in privileges(ctx, raw) if g["principal"] in consumers and g["securable_type"] != "CATALOG"])


def undeclared_grants(ctx):
    p = plan(ctx)
    declared = {_key(g) for g in p["grants"]}
    return _result([{"kind": "grant", "key": " ".join(_key(g)),
                     "problem": ("granted to an individual" if principal_kind(g["principal"]) == "user" else "not declared")
                     + " (left as is)"}
                    for g in _live_grants(ctx, p) if g["securable_type"] != "CATALOG" and _key(g) not in declared])


def unexempt_writers(ctx):
    """Identities that wrote tables from masked tables in the last 30 days but are not exempt from every mask."""
    p = plan(ctx)
    masked = {m["table"] for m in p["column_masks"]} | {c["table"] for c in p["coverage"] if c["masked_by"]}
    if not masked:
        return _result([])
    exempt = set(p["writers"])
    try:
        rows = ctx.ws.sql("SELECT DISTINCT source_table_full_name AS src, target_table_full_name AS dst, created_by "
                          "FROM system.access.table_lineage WHERE event_date >= current_date() - 30 "
                          f"AND target_table_full_name IS NOT NULL AND source_table_full_name IN ({', '.join(lit(t) for t in sorted(masked))})")
    except SqlError as e:
        return _result([{"kind": "lineage", "problem": str(e).splitlines()[0][:300]}])
    return _result([{"kind": "writer", "key": r["created_by"], "problem": f"writes {r['dst']} from masked {r['src']} but is not a writer"}
                    for r in rows if r["created_by"] not in exempt])


def job_identity_findings(ctx):
    names = [f"MAYA deploy - {ctx.system.name}", *(ctx.inputs.get("jobs") or [])]
    short = re.sub(r"[^A-Za-z0-9]+", "_", ctx.ws.user.split("@")[0])
    items = []
    for name in names:
        jobs = [j for n in (name, f"[dev {short}] {name}") for j in ctx.ws.client.jobs.list(name=n)]
        if not jobs:
            items.append({"kind": "job", "key": name, "problem": "job not found"})
        for j in jobs:
            full = ctx.ws.client.jobs.get(j.job_id)
            run_as = full.run_as_user_name or full.creator_user_name or ""
            if principal_kind(run_as) == "user":
                items.append({"kind": "job", "key": full.settings.name, "problem": f"runs as the person {run_as}"})
            if TOKEN.search(json.dumps(full.settings.as_dict())):
                items.append({"kind": "job", "key": full.settings.name, "problem": "a personal access token is in the job definition"})
    root = bundle.root(ctx.system)
    for f in (x for x in root.rglob("*") if x.is_file() and x.suffix in (".yml", ".yaml", ".py", ".sql", ".json")) if root.exists() else []:
        if TOKEN.search(f.read_text(errors="ignore")):
            items.append({"kind": "code", "key": str(f.relative_to(root)), "problem": "a personal access token is in the bundle"})
    return _result(items)
