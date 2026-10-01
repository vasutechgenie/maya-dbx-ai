"""G4 load: the YAML becomes one explicit plan - functions, ABAC policies, column masks, row filters, grants, and what
to remove because an earlier certified plan delivered it and this one no longer declares it. Everything is checked
here, before anything is deployed: principals, objects, columns, types, and that every sensitive column is masked by
exactly one method."""
from maya.core import catalog
from maya.core.spec import stable_hash
from maya.core.workspace import lit

from . import ledger
from .common import (CONSUMER_SCOPES, WORKSPACE_LOCAL, exempt_sql, fn_name, gov_schema, layer_of_schema, member_sql,
                     principal_kind, product_schemas, resolve_object, scope_schemas, sensitive_columns)

STRING_ONLY = {"hash", "email", "last4"}


def _masked_expr(kind, typ):
    return {"redact": "'***'" if typ == "STRING" else f"CAST(NULL AS {typ})",
            "null": f"CAST(NULL AS {typ})",
            "hash": "sha2(CAST(value AS STRING), 256)",
            "email": "regexp_replace(value, '^[^@]+', '***')",
            "last4": "concat('***', right(value, 4))"}[kind]


def _stamp(text, spec) -> str:
    return f"{text} [maya:{stable_hash(spec)[:12]}]"


def _check_principals(ctx, principals, errors, notes):
    groups = None
    for p in sorted(set(principals)):
        kind = principal_kind(p)
        if kind == "group":
            if p in WORKSPACE_LOCAL:
                errors.append(f"principal {p!r} is a workspace-local group; Unity Catalog grants and policies need an account group")
                continue
            if groups is None:
                groups = {g.display_name for g in ctx.ws.client.groups.list(attributes="displayName")}
            if p not in groups and p != "account users":
                notes.append(f"group {p!r} is not assigned to this workspace (valid only if it is an account group)")
        elif kind == "user":
            if not list(ctx.ws.client.users.list(filter=f'userName eq "{p}"', attributes="userName")):
                errors.append(f"user {p!r} does not exist")
        elif not list(ctx.ws.client.service_principals.list(filter=f'applicationId eq "{p}"')):
            errors.append(f"service principal {p!r} does not exist")


def _functions(ctx, writers, errors):
    out = {}
    for name, spec in (ctx.inputs.get("mask_functions") or {}).items():
        typ = spec.get("type", "STRING").upper()
        kind = spec.get("kind")
        if kind in STRING_ONLY and typ != "STRING":
            errors.append(f"mask function {name}: kind {kind} masks STRING values, not {typ}")
            continue
        masked = spec.get("sql") or _masked_expr(kind, typ)
        exempt = list(spec.get("unmasked_for") or []) + writers
        d = {"name": name, "full_name": fn_name(ctx, name), "role": "mask", "params": [["value", typ]], "returns": typ,
             "body": f"CASE WHEN {exempt_sql(exempt)} THEN value ELSE {masked} END", "exempt": exempt}
        d["comment"] = _stamp(spec.get("comment") or f"Column mask ({kind or 'custom'}); unmasked for "
                              f"{', '.join(dict.fromkeys(exempt)) or 'nobody'} (MAYA G4)", d)
        out[name] = d
    return out


def _column_types(ctx, tables):
    cols = {}
    for c in catalog.columns(ctx.ws, ctx.system):
        if c["full_name"] in tables:
            cols.setdefault(c["full_name"], {})[c["column_name"]] = c["data_type"].upper()
    return cols


def load(ctx):
    errors, notes = [], []
    gov = gov_schema(ctx)
    if layer_of_schema(ctx, gov):
        errors.append(f"schema {gov} is a foundation layer schema; governance objects need their own schema")
    writers = list(ctx.inputs.get("writers") or [])
    roles = ctx.inputs["roles"]
    every_principal = list(writers)
    tables = {t["full_name"]: t for t in catalog.tables(ctx.ws, ctx.system)}
    types = _column_types(ctx, {fn for fn, t in tables.items() if t["kind"] == "table"})

    functions = _functions(ctx, writers, errors)
    policies, masks = [], []
    for m in ctx.inputs.get("masks") or []:
        fn = functions.get(m["function"])
        if not fn:
            errors.append(f"mask uses undefined mask function {m['function']!r}")
            continue
        if "abac" in m:
            schemas = [s for scope in m["scopes"] for s in scope_schemas(ctx, scope)]
            if not schemas:
                errors.append(f"policy {m['abac']}: scopes {m['scopes']} name no schema")
            exc = list(dict.fromkeys(list(m.get("except") or []) + fn["exempt"]))
            every_principal += exc
            for s in schemas:
                d = {"name": m["abac"], "schema": s, "function": fn["full_name"], "tag": m["match"]["tag"],
                     "value": m["match"]["value"], "except": exc, "type": fn["returns"]}
                d["comment"] = _stamp(f"Masks columns tagged {d['tag']}={d['value']} with {fn['name']} (MAYA G4)", d)
                policies.append(d)
        else:
            parts = m["column"].split(".")
            table, col = resolve_object(ctx, ".".join(parts[:-1])), parts[-1]
            if table not in tables or tables[table]["kind"] != "table":
                errors.append(f"column mask {m['column']}: {table} is not a foundation table (views inherit the masks of their tables)")
                continue
            if col not in types.get(table, {}):
                errors.append(f"column mask {m['column']}: {table} has no column {col!r}")
                continue
            if types[table][col] != fn["returns"]:
                errors.append(f"column mask {m['column']}: column type {types[table][col]} != function type {fn['returns']}")
            masks.append({"table": table, "column": col, "function": fn["full_name"]})
    keys = [(p["name"], p["schema"]) for p in policies]
    errors += [f"policy {n} declared twice on {s}" for n, s in sorted({k for k in keys if keys.count(k) > 1})]
    mkeys = [(m["table"], m["column"]) for m in masks]
    errors += [f"column {t}.{c} has two column masks" for t, c in sorted({k for k in mkeys if mkeys.count(k) > 1})]

    # coverage: each sensitive column masked by exactly one method; each policy-matched column of the function's type
    tag_values = {t: catalog.column_tags(ctx.ws, ctx.system, t) for t in {p["tag"] for p in policies}}
    coverage = []
    for p in policies:
        for (tbl, col), v in tag_values[p["tag"]].items():
            if v == p["value"] and tbl.rsplit(".", 1)[0] == p["schema"] and tables.get(tbl, {}).get("kind") == "table":
                if types.get(tbl, {}).get(col) != p["type"]:
                    errors.append(f"policy {p['name']} on {p['schema']} matches {tbl}.{col} of type "
                                  f"{types.get(tbl, {}).get(col)}, but its function masks {p['type']}")
    for c in sensitive_columns(ctx):
        by = [f"policy {p['name']}" for p in policies
              if c["table"].rsplit(".", 1)[0] == p["schema"]
              and tag_values[p["tag"]].get((c["table"], c["column"])) == p["value"]]
        by += [f"column mask {m['function'].split('.')[-1]}" for m in masks if (m["table"], m["column"]) == (c["table"], c["column"])]
        if len(by) != 1:
            errors.append(f"sensitive column {c['table']}.{c['column']} ({c['value']}) is masked by "
                          f"{' and '.join(by) if by else 'nothing'}; declare exactly one policy or column mask")
        coverage.append(dict(c, masked_by=by))

    filters, filter_fns = [], {}
    for f in ctx.inputs.get("row_filters") or []:
        table = resolve_object(ctx, f["table"])
        if table not in tables or tables[table]["kind"] != "table":
            errors.append(f"row filter on {f['table']}: not a foundation table")
            continue
        missing = [c for c in f["columns"] if c not in types.get(table, {})]
        if missing:
            errors.append(f"row filter on {table}: no columns {missing}")
            continue
        exempt = list(f.get("unfiltered_for") or []) + writers
        if f.get("by_group"):
            if len(f["columns"]) != 1:
                errors.append(f"row filter on {table}: by_group filters on exactly one column")
                continue
            col = f["columns"][0]
            every_principal += list(f["by_group"])
            cond = " OR ".join(f"({member_sql(p)} AND {col} IN ({', '.join(lit(v) for v in vals)}))"
                               for p, vals in f["by_group"].items()) or "FALSE"
        else:
            cond = f["sql"]
        every_principal += exempt
        name = "rf_" + "_".join(table.split(".")[1:])
        d = {"name": name, "full_name": fn_name(ctx, name), "role": "row_filter",
             "params": [[c, types[table][c]] for c in f["columns"]], "returns": "BOOLEAN",
             "body": f"{exempt_sql(exempt)} OR ({cond})", "exempt": exempt}
        d["comment"] = _stamp(f"Row filter of {table} (MAYA G4)", d)
        filter_fns[name] = d
        filters.append({"table": table, "function": d["full_name"], "columns": f["columns"]})

    grants, role_grants = [], {}
    for role, r in roles.items():
        every_principal += r["principals"]
        tuples = []
        for kind, priv in (("read", "SELECT"), ("execute", "EXECUTE")):
            for scope in r.get(kind) or []:
                schemas = scope_schemas(ctx, scope)
                if r.get("consumer") and scope not in CONSUMER_SCOPES and \
                        any(layer_of_schema(ctx, s) in ("bronze", "silver") for s in schemas):
                    errors.append(f"role {role} is a consumer: it may not read {scope}")
                if not schemas:
                    errors.append(f"role {role}: scope {scope!r} names no schema")
                tuples += [("USE SCHEMA", "SCHEMA", s) for s in schemas] + [(priv, "SCHEMA", s) for s in schemas]
        objs = r.get("objects") or {}
        for kind, priv, typ in (("select", "SELECT", "TABLE"), ("execute", "EXECUTE", "FUNCTION")):
            for o in objs.get(kind) or []:
                fn = resolve_object(ctx, o)
                if r.get("consumer") and layer_of_schema(ctx, fn.rsplit(".", 1)[0]) in ("bronze", "silver"):
                    errors.append(f"role {role} is a consumer: it may not read {fn}")
                tuples += [("USE SCHEMA", "SCHEMA", fn.rsplit(".", 1)[0]), (priv, typ, fn)]
        if ctx.inputs.get("catalog_access", "grant") == "grant":
            tuples += [("USE CATALOG", "CATALOG", c) for c in sorted({t[2].split(".")[0] for t in tuples})]
        rows = [{"principal": p, "privilege": pr, "securable_type": ty, "securable": se}
                for p in r["principals"] for pr, ty, se in dict.fromkeys(tuples)]
        role_grants[role] = rows
        grants += [g for g in rows if g not in grants]
    individuals = sorted({g["principal"] for g in grants if principal_kind(g["principal"]) == "user"})
    errors += [f"grants go to groups or service principals, not individuals: {p}" for p in individuals]
    _check_principals(ctx, every_principal, errors, notes)
    if errors:
        raise RuntimeError("the governance input is invalid:\n  - " + "\n  - ".join(errors))

    delivered = ledger.delivered(ctx)
    gkey = lambda g: (g["principal"], g["privilege"], g["securable_type"], g["securable"])  # noqa: E731
    now = {gkey(g) for g in grants}
    removed = {
        "grants": [g for g in delivered["grants"] if gkey(g) not in now],
        "policies": [p for p in delivered["policies"] if (p["name"], p["schema"]) not in set(keys)],
        "column_masks": [m for m in delivered["column_masks"] if (m["table"], m["column"]) not in set(mkeys)],
        "row_filters": [f for f in delivered["row_filters"] if f["table"] not in {x["table"] for x in filters}],
    }
    audit = dict({"view": "audit_access", "days": 90}, **(ctx.inputs.get("audit") or {}))
    audit.update(full_name=fn_name(ctx, audit["view"]), schemas=product_schemas(ctx))
    out = {"schema": gov, "writers": writers, "functions": list(functions.values()) + list(filter_fns.values()),
           "policies": policies, "column_masks": masks, "row_filters": filters, "grants": grants,
           "role_grants": role_grants, "removed": removed, "coverage": coverage, "audit": audit,
           "consumers": sorted({p for r in roles.values() if r.get("consumer") for p in r["principals"]}),
           "notes": notes}
    ctx.write_artefact("governance_plan.json", out)
    for n in notes:
        ctx.log(f"     note: {n}")
    ctx.log(f"     plan: {len(coverage)} sensitive columns, {len(policies)} policies, {len(masks)} column masks, "
            f"{len(filters)} row filters, {len(grants)} grants"
            + (f"; removing {sum(len(v) for v in removed.values())} no longer declared" if any(removed.values()) else ""))
    return {"sensitive_columns": len(coverage), "policies": len(policies), "column_masks": len(masks),
            "row_filters": len(filters), "grants": len(grants), "removed": {k: len(v) for k, v in removed.items()}}
