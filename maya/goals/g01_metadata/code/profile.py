"""G1 scope + profiling: what to describe (from G0's certified inventory) and statistics for each asset."""
from maya.core import catalog
from maya.core.workspace import SqlError, ident, lit

from . import ledger
from .common import has_gaps, tag_name

_COMPARABLE = ("STRING", "BIGINT", "INT", "SMALLINT", "TINYINT", "DOUBLE", "FLOAT", "DECIMAL", "DATE", "TIMESTAMP",
               "BOOLEAN")


def _inventory(ctx, tables):
    """G0's certified inventory; when G0 is self-certified in maya.yaml (no inventory), the configured foundation."""
    cert = ctx.state.latest_certification("G0")
    rows = ctx.ws.sql(f"SELECT full_name, layer, kind, row_count FROM {ctx.state.t('asset_inventory')} "
                      f"WHERE system = {lit(ctx.system.name)} AND run_id = {lit(cert['run_id'])} ORDER BY full_name")
    if rows or ctx.system.self_certified("G0") is None:
        return rows
    return [{"full_name": t["full_name"], "layer": t["layer"], "kind": t["kind"], "row_count": None}
            for t in tables.values() if t["kind"] in ("table", "view")]


def scope(ctx):
    tables = {t["full_name"]: t for t in catalog.tables(ctx.ws, ctx.system)}
    rows = _inventory(ctx, tables)
    tags = catalog.column_tags(ctx.ws, ctx.system, tag_name(ctx, "sensitivity"))
    cols = {}
    for c in catalog.columns(ctx.ws, ctx.system):
        cols.setdefault(c["full_name"], []).append(
            {"name": c["column_name"], "type": c["data_type"], "nullable": c["is_nullable"] == "YES", "comment": c["comment"],
             "sensitivity_tag": tags.get((c["full_name"], c["column_name"]))})
    views = catalog.views(ctx.ws, ctx.system)
    assets = []
    for r in rows:
        fn = r["full_name"]
        if fn not in tables:
            continue  # dropped since G0 certified it
        assets.append({"full_name": fn, "layer": r["layer"], "kind": r["kind"],
                       "scope": ctx.system.layers[r["layer"]].get("metadata_scope", "full"),
                       "row_count": int(r["row_count"]) if r["row_count"] is not None else None,
                       "comment": tables[fn].get("comment"), "columns": cols.get(fn, []),
                       "view_definition": views.get(fn)})
    ctx.write_artefact("scope.json", assets)
    policy = _sensitivity_policy(ctx)
    plan = _plan(ctx, assets)
    ctx.write_artefact("plan.json", plan)
    for k in ("new", "changed", "restore"):
        if plan[k]:
            ctx.log(f"     {k}: {', '.join(plan[k])}")
    ctx.log(f"     unchanged (skipped): {len(plan['unchanged'])}")
    return {"assets": len(assets), "columns": sum(len(a["columns"]) for a in assets), "g0_run": ctx.state.latest_certification("G0")["run_id"],
            **{k: len(v) for k, v in plan.items()}, **policy}


def _intact(ctx, a, prop, cons):
    """The certified metadata for this asset is still in Unity Catalog."""
    if not a["comment"]:
        return False
    if a["scope"] == "full" and not all(c["comment"] for c in a["columns"]):
        return False
    if any(c["sensitivity_tag"] is None for c in a["columns"]):
        return False
    if (prop.get("primary_key") or {}).get("verified") and "PRIMARY KEY" not in cons.get(a["full_name"], set()):
        return False
    return True


def _plan(ctx, assets):
    """new: never certified -> the describer fills what is missing. changed: shape differs from the certified one
    and something is missing -> the describer fills only the gaps. restore: nothing new to describe, but gaps or keys
    to put back from the certified proposal (no agent, no re-review). unchanged: certified and intact -> skipped.
    Values already in Unity Catalog are kept in every case unless overwrite_existing. mode=full: everything is new."""
    plan = {"new": [], "changed": [], "restore": [], "unchanged": []}
    if ctx.inputs.get("mode", "incremental") == "full":
        plan["new"] = [a["full_name"] for a in assets]
        return plan
    done = ledger.certified(ctx)
    cons = {}
    for r in catalog.constraints(ctx.ws, ctx.system):
        cons.setdefault(r["full_name"], set()).add(r["constraint_type"])
    for a in assets:
        prev = done.get(a["full_name"])
        if not prev:
            plan["new"].append(a["full_name"])
        elif prev["fingerprint"] != ledger.fingerprint(a):
            plan["changed" if has_gaps(ctx, a) else "restore"].append(a["full_name"])
        elif _intact(ctx, a, prev["proposal"], cons):
            plan["unchanged"].append(a["full_name"])
        else:
            plan["restore"].append(a["full_name"])
    return plan


def _sensitivity_policy(ctx):
    key = tag_name(ctx, "sensitivity")
    configured = ctx.inputs["sensitivity_classes"]
    allowed = catalog.tag_policy_values(ctx.ws, key)
    effective = [c for c in configured if allowed is None or c in allowed]
    if allowed is not None:
        bad_rules = sorted({r["class"] for r in ctx.inputs.get("sensitivity_rules") or []} - set(allowed))
        if bad_rules:
            raise RuntimeError(f"sensitivity_rules use {bad_rules}, not allowed by the governed tag policy '{key}' "
                               f"(allowed: {allowed}). Change the rules in maya.yaml.")
        if len(effective) < 2:
            raise RuntimeError(f"governed tag policy '{key}' allows {allowed}; fewer than two of the configured "
                               f"sensitivity_classes {configured} remain. Align sensitivity_classes in maya.yaml.")
    policy = {"tag_key": key, "governed": allowed is not None, "policy_values": allowed,
              "sensitivity_classes": effective, "dropped_classes": [c for c in configured if c not in effective]}
    ctx.write_artefact("policy.json", policy)
    if policy["dropped_classes"]:
        ctx.log(f"     governed tag '{key}' allows {allowed}: not using {policy['dropped_classes']}")
    return {"sensitivity_classes": effective, "dropped_classes": policy["dropped_classes"]}


def _profile_one(ctx, a):
    allow, cols = ctx.inputs.get("allow_data", False), a["columns"]
    t = ".".join(ident(p) for p in a["full_name"].split("."))
    parts = ["count(*) AS r"]
    for i, c in enumerate(cols):
        q = ident(c["name"])
        parts += [f"count({q}) AS n{i}", f"approx_count_distinct({q}) AS d{i}"]
        if allow and c["type"].split("(")[0].upper() in _COMPARABLE:
            parts += [f"cast(min({q}) AS STRING) AS mn{i}", f"cast(max({q}) AS STRING) AS mx{i}"]
    s = ctx.ws.sql(f"SELECT {', '.join(parts)} FROM {t}")[0]
    rows = int(s["r"])
    prof = {"rows": rows, "columns": {}}
    low = []
    for i, c in enumerate(cols):
        nn, nd = int(s[f"n{i}"]), int(s[f"d{i}"])
        p = {"null_fraction": round(1 - nn / rows, 4) if rows else None, "distinct": nd}
        if allow and f"mn{i}" in s:
            p.update(min=s[f"mn{i}"], max=s[f"mx{i}"])
        if allow and 0 < nd <= ctx.inputs.get("max_distinct", 12):
            low.append((i, c["name"]))
        prof["columns"][c["name"]] = p
    if low:
        v = ctx.ws.sql("SELECT " + ", ".join(
            f"to_json(slice(array_sort(collect_set(cast({ident(n)} AS STRING))), 1, 50)) AS v{i}" for i, n in low) + f" FROM {t}")[0]
        for i, n in low:
            prof["columns"][n]["values"] = v[f"v{i}"]
    if allow and ctx.inputs.get("sample_rows", 20):
        sample = ctx.ws.sql(f"SELECT * FROM {t} LIMIT {int(ctx.inputs.get('sample_rows', 20))}")
        for c in cols:
            if "values" not in prof["columns"][c["name"]]:
                vals = list(dict.fromkeys(str(r[c["name"]])[:60] for r in sample if r.get(c["name"]) is not None))[:5]
                prof["columns"][c["name"]]["samples"] = vals
    return prof


def profile(ctx):
    plan = ctx.read_artefact("plan.json")
    work = set(plan["new"] + plan["changed"])
    assets = [a for a in ctx.read_artefact("scope.json") if a["full_name"] in work]
    profiles, errors = {}, {}
    for a in assets:
        try:
            profiles[a["full_name"]] = _profile_one(ctx, a)
        except SqlError as e:
            errors[a["full_name"]] = str(e)[:300]
    if errors:
        raise RuntimeError(f"profiling failed for {sorted(errors)}: {list(errors.values())[0]}")
    ctx.write_artefact("profiles.json", profiles)
    return {"assets": [a["full_name"] for a in assets], "data_shared_with_model": ctx.inputs.get("allow_data", False)}
