"""G1 keys: declared (maya.yaml) and describer-proposed primary / foreign keys, verified against the data."""
import re

from maya.core import catalog
from maya.core.workspace import ident

from .common import declared_keys, keep_existing, proposals


def _q(full_name):
    return ".".join(ident(p) for p in full_name.split("."))


def pk_holds(ctx, full_name, cols):
    c = ", ".join(ident(x) for x in cols)
    nulls = " OR ".join(f"{ident(x)} IS NULL" for x in cols)
    r = ctx.ws.sql(f"SELECT count(*) AS n, count(DISTINCT struct({c})) AS d, count_if({nulls}) AS z FROM {_q(full_name)}")[0]
    n, d, z = int(r["n"]), int(r["d"]), int(r["z"])
    return {"rows": n, "distinct": d, "nulls": z, "holds": n > 0 and n == d and z == 0}


def fk_orphans(ctx, child, col, parent, pcol):
    r = ctx.ws.sql(f"SELECT count(*) AS o FROM {_q(child)} c LEFT ANTI JOIN {_q(parent)} p "
                   f"ON c.{ident(col)} = p.{ident(pcol)} WHERE c.{ident(col)} IS NOT NULL")[0]
    return int(r["o"])


def _profiled_key(ctx, p):
    """First single column matching key_candidate_patterns that is unique and non-null in the data. Columns whose
    name contains the table's stem (order_id for orders) are tried first."""
    pats = [re.compile(x) for x in ctx.inputs.get("key_candidate_patterns") or []]
    stem = p["full_name"].split(".")[-1].lower().rstrip("s")
    cands = [c["name"] for c in p["columns"] if any(x.search(c["name"]) for x in pats)]
    for name in sorted(cands, key=lambda n: stem not in n.lower()):
        ev = pk_holds(ctx, p["full_name"], [name])
        if ev["holds"]:
            return {"columns": [name], "source": "profiled", "verified": True, "evidence": ev}
    return None


def verify(ctx):
    props = proposals(ctx)
    by_name = {p["full_name"]: p for p in props}
    declared = declared_keys(ctx)
    existing_pk = {}  # keys already in Unity Catalog: kept for tables in this run, used as FK parents for the rest
    for r in catalog.constraints(ctx.ws, ctx.system):
        if r["constraint_type"] == "PRIMARY KEY":
            existing_pk.setdefault(r["full_name"], []).append(r["column_name"])
    for p in props:
        p["primary_key"], p["foreign_keys"] = None, []
        if p["kind"] != "table" or p["scope"] != "full":
            continue
        cand, source = None, None
        if keep_existing(ctx) and p["full_name"] in existing_pk:
            cand, source = existing_pk[p["full_name"]], "existing"
        elif p["full_name"] in declared:
            cand, source = declared[p["full_name"]], "declared"
        elif ctx.inputs.get("infer_keys", True) and p["proposed_pk"]:
            cand, source = p["proposed_pk"], "inferred"
        if cand:
            known = {c["name"] for c in p["columns"]}
            ev = pk_holds(ctx, p["full_name"], cand) if set(cand) <= known else {"holds": False, "error": "unknown column"}
            p["primary_key"] = {"columns": cand, "source": source, "verified": ev["holds"], "evidence": ev}
        if source not in ("declared", "existing") and not (p["primary_key"] or {}).get("verified"):
            found = _profiled_key(ctx, p)
            if found:
                p["primary_key"] = found
    missing_declared = [t for t in declared if t in by_name and not (by_name[t]["primary_key"] or {}).get("verified")]
    if ctx.inputs.get("infer_keys", True):
        for p in props:
            if p["kind"] != "table" or p["scope"] != "full":
                continue
            for c in p["columns"]:
                parent, _, pcol = (c.get("references") or "").rpartition(".")
                parent_name = catalog.resolve(ctx.system, parent) if parent else None
                if not parent_name or parent_name == p["full_name"]:
                    continue
                if parent_name in by_name:
                    ppk = by_name[parent_name].get("primary_key") or {}
                    pk_cols = ppk["columns"] if ppk.get("verified") else None
                else:
                    pk_cols = existing_pk.get(parent_name)
                if pk_cols != [pcol]:
                    continue
                orphans = fk_orphans(ctx, p["full_name"], c["name"], parent_name, pcol)
                p["foreign_keys"].append({"column": c["name"], "parent": parent_name, "parent_column": pcol,
                                          "orphans": orphans, "verified": orphans == 0})
    ctx.write_artefact("proposals.json", props)
    under_review = {p["full_name"] for p in ctx.read_artefact("review.json") or []}
    ctx.write_artefact("review.json", [p for p in props if p["full_name"] in under_review])
    return {"primary_keys": sum(1 for p in props if (p["primary_key"] or {}).get("verified")),
            "rejected_keys": [p["full_name"] for p in props if p["primary_key"] and not p["primary_key"]["verified"]],
            "foreign_keys": sum(1 for p in props for f in p["foreign_keys"] if f["verified"]),
            "declared_keys_not_holding": missing_declared}
