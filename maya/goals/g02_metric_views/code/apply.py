"""G2 apply: metric views become bundle scripts, deployed through the project bundle.

scripts/G2/00_schema.sql                 the metric view schema
scripts/G2/10_views/<view>.sql           CREATE OR REPLACE of the view, keeping the tags and grants the view already
                                         has in that environment (except the certification tag), then its MAYA tags
scripts/G2/90_certification/<view>.sql   certification tag, written only after the KPI owner's sign-off
A changed view therefore loses its certification wherever its new definition is deployed, until it is re-certified."""
from maya.core import bundle
from maya.core.workspace import ident, lit

from . import ledger
from .common import cert_tag, ddl, definitions, q, target_schema, view_tags, view_tags_live


def _quote(expr):
    return f"char(39) || replace(replace({expr}, char(92), char(92) || char(92)), char(39), char(92) || char(39)) || char(39)"


def _view(ctx, d):
    fn = d["full_name"]
    cat, sch, name = fn.split(".")
    key, _ = cert_tag(ctx)
    where = f"table_schema = {lit(sch)} AND table_name = {lit(name)}"
    tags = view_tags(ctx, d)
    set_tags = (f"\n  ALTER VIEW {q(fn)} SET TAGS ({', '.join(f'{lit(k)} = {lit(v)}' for k, v in tags.items())});"
                if tags else "")
    return [f"""BEGIN
  DECLARE kept_grants ARRAY<STRING>;
  DECLARE kept_tags STRING;
  SET kept_grants = (SELECT coalesce(collect_list('GRANT ' || privilege_type || ' ON TABLE {q(fn)} TO `' || grantee || '`'), array())
    FROM {ident(cat)}.information_schema.table_privileges WHERE {where} AND inherited_from = 'NONE');
  SET kept_tags = (SELECT CASE WHEN count(*) > 0 THEN 'ALTER VIEW {q(fn)} SET TAGS ('
      || concat_ws(', ', collect_list({_quote('tag_name')} || ' = ' || {_quote('tag_value')})) || ')' END
    FROM {ident(cat)}.information_schema.table_tags
    WHERE schema_name = {lit(sch)} AND table_name = {lit(name)} AND tag_name <> {lit(key)});
  {ddl(d)};
  FOR g AS SELECT explode(kept_grants) AS stmt DO
    EXECUTE IMMEDIATE g.stmt;
  END FOR;
  IF kept_tags IS NOT NULL THEN
    EXECUTE IMMEDIATE kept_tags;
  END IF;{set_tags}
END"""]


def _schema(ctx):
    c, s = target_schema(ctx)
    return [f"CREATE SCHEMA IF NOT EXISTS {ident(c, s)} COMMENT 'Metric views (KPIs) managed by MAYA G2'"]


def _catalogs(ctx):
    return {target_schema(ctx)[0]}


def _deploy_views(ctx, names, label):
    defs = {d["full_name"]: d for d in definitions(ctx)}
    files = {"00_schema.sql": _schema(ctx)}
    files.update({f"10_views/{fn.split('.')[-1]}.sql": _view(ctx, defs[fn]) for fn in names})
    written = bundle.write(ctx, files, catalogs=_catalogs(ctx))
    res = bundle.deploy(ctx, written)
    ctx.log(f"     {label}: {len(names)} metric views in {len(written)} scripts")
    return {"created": list(names), **res}


def export(ctx) -> dict:
    """Scripts for every certified metric view (schema, view, certification tag), for `maya bundle`."""
    key, value = cert_tag(ctx)
    files = {"00_schema.sql": _schema(ctx)}
    for fn, c in ledger.certified(ctx).items():
        name = fn.split(".")[-1]
        files[f"10_views/{name}.sql"] = _view(ctx, c["definition"])
        files[f"90_certification/{name}.sql"] = [f"ALTER VIEW {q(fn)} SET TAGS ({lit(key)} = {lit(value)})"]
    return {"files": files, "catalogs": _catalogs(ctx)}


def apply(ctx):
    names = (ctx.outputs.get("render") or {}).get("views") or []
    defined = {d["full_name"].split(".")[-1] for d in definitions(ctx)}
    base = bundle.scripts_dir(ctx.system, ctx.goal.id)
    stale = [p for p in (base / "10_views").glob("*.sql") if p.stem not in defined] if base.exists() else []
    for p in stale + [base / "90_certification" / f"{n.split('.')[-1]}.sql" for n in names]:
        p.unlink(missing_ok=True)
    return _deploy_views(ctx, names, "apply")


def repair(ctx):
    """Re-deploy every view with a failing item that a re-deploy can fix (missing, or not as defined)."""
    items = (ctx.outputs.get("validate") or {}).get("failed_items") or []
    names = sorted({i["metric_view"] for i in items if isinstance(i, dict) and i.get("kind") in ("missing", "definition")})
    if not names:
        return {"repaired": [], "note": "failures need a change to the definitions (not repairable by re-deploying)"}
    return {"repaired": names, **_deploy_views(ctx, names, "repair")}


def mark_certified(ctx):
    """After the KPI owner's sign-off: deploy the certification tag of every view and record the definitions."""
    key, value = cert_tag(ctx)
    defs = definitions(ctx)
    files = {f"90_certification/{d['full_name'].split('.')[-1]}.sql":
             [f"ALTER VIEW {q(d['full_name'])} SET TAGS ({lit(key)} = {lit(value)})"] for d in defs}
    written = bundle.write(ctx, files, catalogs=_catalogs(ctx))
    res = bundle.deploy(ctx, written)
    bad = [d["full_name"] for d in defs if view_tags_live(ctx, d["full_name"]).get(key) != value]
    if bad:
        raise RuntimeError(f"certification tag {key}={value} missing after deploy on {bad}")
    ledger.record(ctx, defs)
    return {"tagged": len(defs), "tag": f"{key}={value}", **res}
