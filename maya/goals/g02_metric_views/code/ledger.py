"""G2 ledger: the definition each metric view was created from. Only rows written by a certified G2 run are trusted,
so an incremental run re-creates only views whose definition changed since certification."""
import json

from maya.core import catalog
from maya.core.workspace import lit

COLS = ("system STRING, run_id STRING, full_name STRING, definition_hash STRING, definition_json STRING, "
        "recorded_at TIMESTAMP")


def table(ctx):
    return ctx.state.t("metric_view_ledger")


def record(ctx, defs):
    from .common import definition_hash
    if not defs:
        return
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ", ".join("(" + ", ".join([lit(ctx.system.name), lit(ctx.run_id), lit(d["full_name"]),
                                      lit(definition_hash(ctx, d)), lit(json.dumps(d, default=str)),
                                      "current_timestamp()"]) + ")" for d in defs)
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES {rows}")


def certified(ctx) -> dict:
    """full_name -> {definition_hash, definition} from the latest certified G2 run that covered each view."""
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT full_name, max_by(definition_hash, recorded_at) AS definition_hash,
            max_by(definition_json, recorded_at) AS definition_json
        FROM {table(ctx)}
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G2')
        GROUP BY full_name""")
    return {r["full_name"]: {"definition_hash": r["definition_hash"], "definition": json.loads(r["definition_json"])}
            for r in rows}


def drift(ctx):
    """Definitions edited since certification, certified views missing or edited in Unity Catalog, lost certification."""
    from .common import body, cert_tag, definition_hash, full_name, metric_views, raw_definitions, read_back, \
        same_body, view_tags_live
    cert = certified(ctx)
    current = {}
    for d in raw_definitions(ctx):
        d = dict(d, full_name=full_name(ctx, d["name"]),
                 source=catalog.resolve(ctx.system, d["source"]) or d["source"],
                 joins=[dict(j, source=catalog.resolve(ctx.system, j["source"]) or j["source"]) for j in d.get("joins") or []])
        current[d["full_name"]] = d
    live = metric_views(ctx)
    reasons = []
    edited = sorted(n for n, d in current.items() if cert.get(n, {}).get("definition_hash") != definition_hash(ctx, d))
    if edited:
        reasons.append(f"metric view definitions new or changed: {', '.join(edited)}")
    removed = sorted(n for n in cert if n not in current)
    if removed:
        reasons.append(f"metric views no longer defined: {', '.join(removed)}")
    missing = sorted(n for n in cert if n in current and n not in live)
    if missing:
        reasons.append(f"certified metric views missing in Unity Catalog: {', '.join(missing)}")
    key, value = cert_tag(ctx)
    for n in sorted(n for n in cert if n in current and n in live and n not in edited):
        if same_body(read_back(ctx, n), body(cert[n]["definition"])):
            reasons.append(f"metric view changed in Unity Catalog outside MAYA: {n}")
        elif view_tags_live(ctx, n).get(key) != value:
            reasons.append(f"metric view lost its certification tag: {n}")
    return reasons
