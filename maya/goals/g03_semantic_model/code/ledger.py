"""G3 ledger: the semantic model each certified run delivered, with the declaration of every subdomain it was built
from. Only rows of certified G3 runs are trusted; they let later runs keep agent-built pages and placements."""
import json

from maya.core.spec import stable_hash
from maya.core.workspace import SqlError, ident, lit

COLS = ("system STRING, run_id STRING, declaration_hash STRING, subdomains_json STRING, model_json STRING, "
        "recorded_at TIMESTAMP")


def table(ctx):
    return ctx.state.t("semantic_ledger")


def record(ctx, model, subdomains):
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES ({lit(ctx.system.name)}, {lit(ctx.run_id)}, "
               f"{lit(model['declaration_hash'])}, {lit(json.dumps(subdomains))}, "
               f"{lit(json.dumps(model, default=str))}, current_timestamp())")


def certified(ctx) -> dict | None:
    """{model, subdomains: {id: declaration_hash}} of the latest certified G3 run, or None."""
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT subdomains_json, model_json FROM {table(ctx)}
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G3')
        ORDER BY recorded_at DESC LIMIT 1""")
    if not rows:
        return None
    return {"model": json.loads(rows[0]["model_json"]), "subdomains": json.loads(rows[0]["subdomains_json"])}


def live_tags(ctx, full_names, keys) -> dict:
    """full_name -> {tag: value} for the given tag keys."""
    groups, out = {}, {fn: {} for fn in full_names}
    for fn in full_names:
        c, s, t = fn.split(".")
        groups.setdefault((c, s), []).append(t)
    for (c, s), names in groups.items():
        try:
            rows = ctx.ws.sql(f"SELECT table_name, tag_name, tag_value FROM {ident(c)}.information_schema.table_tags "
                              f"WHERE schema_name = {lit(s)} AND table_name IN ({', '.join(lit(n) for n in names)}) "
                              f"AND tag_name IN ({', '.join(lit(k) for k in keys)})")
        except SqlError:
            continue
        for r in rows:
            out[f"{c}.{s}.{r['table_name']}"][r["tag_name"]] = r["tag_value"]
    return out


def drift(ctx):
    """Taxonomy edited since certification, assets added to or gone from scope, tags changed outside MAYA."""
    from .common import raw_taxonomy, scope_assets
    cert = certified(ctx)
    if not cert:
        return []
    m, reasons = cert["model"], []
    if stable_hash(raw_taxonomy(ctx)) != m["declaration_hash"]:
        reasons.append(f"taxonomy {ctx.inputs['taxonomy']} changed since certification")
    placed = {a["asset"]: a for a in m["assignments"]}
    now = {a["full_name"] for a in scope_assets(ctx)}
    new = sorted(now - set(placed))
    if new:
        reasons.append(f"assets without a page: {', '.join(new[:10])}" + (" ..." if len(new) > 10 else ""))
    gone = sorted(set(placed) - now)
    if gone:
        reasons.append(f"placed assets no longer in scope: {', '.join(gone[:10])}")
    keys = m["tag_keys"]
    live = live_tags(ctx, sorted(set(placed) & now), list(keys.values()))
    off = sorted(fn for fn, tags in live.items()
                 if tags != {keys["domain"]: placed[fn]["domain"], keys["subdomain"]: placed[fn]["subdomain"],
                             keys["page"]: placed[fn]["page"]})
    if off:
        reasons.append(f"semantic tags changed outside MAYA on: {', '.join(off[:10])}")
    return reasons
