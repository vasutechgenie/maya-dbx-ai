"""G1 apply: approved proposals become bundle scripts (one per asset and step), deployed through the project bundle.

scripts/G1/10_metadata/<asset>.sql      table + column descriptions, layer and sensitivity tags (full intended state)
scripts/G1/20_primary_keys/<asset>.sql  primary key, added only when the table has none
scripts/G1/30_foreign_keys/<asset>.sql  foreign keys, each added only when its column has none
The steps run in that order in every environment, so every primary key exists before a foreign key refers to it."""
import uuid

from maya.core import bundle
from maya.core.workspace import ident, lit

from . import ledger
from .common import proposals, scope, split, tag_name
from .keys import _q

STEPS = ("10_metadata", "20_primary_keys", "30_foreign_keys")


def _metadata(ctx, p):
    fn = p["full_name"]
    t, s_tag, l_tag = _q(fn), tag_name(ctx, "sensitivity"), tag_name(ctx, "layer")
    out = []
    if p["comment"]:
        out.append(f"COMMENT ON TABLE {t} IS {lit(p['comment'])}")
    out.append(f"ALTER {'VIEW' if p['kind'] == 'view' else 'TABLE'} {t} SET TAGS ({lit(l_tag)} = {lit(p['layer'])})")
    for c in p["columns"]:
        if c["comment"]:
            out.append(f"COMMENT ON COLUMN {t}.{ident(c['name'])} IS {lit(c['comment'])}")
        if c["sensitivity"]:
            out.append(f"ALTER TABLE {t} ALTER COLUMN {ident(c['name'])} SET TAGS ({lit(s_tag)} = {lit(c['sensitivity'])})")
    return out


def _has(fn, kind, column=None):
    cat, sch, tbl = fn.split(".")
    col = f" AND k.column_name = {lit(column)}" if column else ""
    return (f"EXISTS (SELECT 1 FROM {ident(cat)}.information_schema.table_constraints tc "
            f"JOIN {ident(cat)}.information_schema.key_column_usage k "
            f"ON tc.constraint_name = k.constraint_name AND tc.table_schema = k.table_schema "
            f"WHERE tc.table_schema = {lit(sch)} AND tc.table_name = {lit(tbl)} "
            f"AND tc.constraint_type = {lit(kind)}{col})")


def _primary_key(p):
    pk = p.get("primary_key") or {}
    if not pk.get("verified"):
        return []
    fn, name = p["full_name"], split(p["full_name"])[1]
    body = [f"ALTER TABLE {_q(fn)} ALTER COLUMN {ident(c)} SET NOT NULL;" for c in pk["columns"]]
    body.append(f"ALTER TABLE {_q(fn)} ADD CONSTRAINT {ident(name + '_pk')} PRIMARY KEY "
                f"({', '.join(ident(c) for c in pk['columns'])});")
    return ["BEGIN\n  IF NOT " + _has(fn, "PRIMARY KEY") + " THEN\n    " + "\n    ".join(body) + "\n  END IF;\nEND"]


def _foreign_keys(p):
    fn, name = p["full_name"], split(p["full_name"])[1]
    out = []
    for fk in p.get("foreign_keys") or []:
        if fk["verified"]:
            out.append("BEGIN\n  IF NOT " + _has(fn, "FOREIGN KEY", fk["column"]) + " THEN\n    "
                       f"ALTER TABLE {_q(fn)} ADD CONSTRAINT {ident(name + '_' + fk['column'] + '_fk')} "
                       f"FOREIGN KEY ({ident(fk['column'])}) REFERENCES {_q(fk['parent'])} ({ident(fk['parent_column'])});"
                       "\n  END IF;\nEND")
    return out


def scripts(ctx, props) -> tuple[dict, list]:
    """{path: statements} for the given proposals, and the paths of steps an asset no longer needs."""
    files, remove = {}, []
    for p in props:
        for step, stmts in zip(STEPS, (_metadata(ctx, p), _primary_key(p), _foreign_keys(p))):
            rel = f"{step}/{p['full_name']}.sql"
            if stmts:
                files[rel] = stmts
            else:
                remove.append(rel)
    return files, remove


def export(ctx) -> dict:
    """Scripts for everything certified (latest certified proposal per asset), for `maya bundle`."""
    files, _ = scripts(ctx, [c["proposal"] for c in ledger.certified(ctx).values()])
    return {"files": files}


def _apply(ctx, props, label):
    files, remove = scripts(ctx, props)
    written = bundle.write(ctx, files, remove=remove)
    res = bundle.deploy(ctx, written)
    shapes = {a["full_name"]: ledger.fingerprint(a) for a in scope(ctx)}
    ledger.record(ctx, [p for p in props if p["full_name"] in shapes], shapes)
    ctx.log(f"     {label}: {len(props)} assets, {len(written)} scripts")
    return {"assets": len(props), "scripts": written, **res}


def apply(ctx):
    return _apply(ctx, proposals(ctx), "apply")


def repair(ctx):
    """Re-describe assets whose descriptions failed quality checks, then re-deploy every asset with a failing item."""
    from .proposals import proposal_for, redescribe
    items = (ctx.outputs.get("validate") or {}).get("failed_items") or []
    assets = sorted({i["asset"] for i in items if isinstance(i, dict) and "asset" in i})
    if not assets:
        return {"repaired": [], "note": "no asset-level failures to repair"}
    props = {p["full_name"]: p for p in proposals(ctx)}
    in_scope = {a["full_name"]: a for a in scope(ctx)}
    redescribed = []
    for n, fn in enumerate(assets):
        problems = [i["problem"] for i in items if i.get("asset") == fn and i.get("kind") == "description"]
        if problems and fn in in_scope:
            res = redescribe(ctx, fn, problems, f"repair-{n}-{uuid.uuid4().hex[:4]}")
            fresh = proposal_for(ctx, in_scope[fn], res)
            fresh["primary_key"], fresh["foreign_keys"] = props[fn].get("primary_key"), props[fn].get("foreign_keys", [])
            props[fn] = fresh
            redescribed.append(fn)
    ctx.write_artefact("proposals.json", list(props.values()))
    out = _apply(ctx, [props[a] for a in assets if a in props], "repair")
    return {"repaired": assets, "redescribed": redescribed, **out}
