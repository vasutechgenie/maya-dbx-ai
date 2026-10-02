"""G5 ledger: the space each certified run approved, with its benchmark result. Only rows of certified G5 runs are
trusted, so an incremental run re-authors only pages whose inputs changed since certification."""
import json

from maya.core.spec import stable_hash
from maya.core.workspace import lit

COLS = ("system STRING, run_id STRING, marker STRING, space_id STRING, definition_hash STRING, definition_json STRING, "
        "benchmark_json STRING, recorded_at TIMESTAMP")


def table(ctx):
    return ctx.state.t("genie_ledger")


def definition_hash(space) -> str:
    from .common import serialized
    return stable_hash({"space": serialized(space), "title": space["title"], "description": space.get("description"),
                        "access": space.get("access")})


def record(ctx, space, bench):
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    ctx.ws.sql(f"INSERT INTO {table(ctx)} VALUES ({lit(ctx.system.name)}, {lit(ctx.run_id)}, {lit(space['marker'])}, "
               f"{lit(bench.get('space_id'))}, {lit(definition_hash(space))}, {lit(json.dumps(space, default=str))}, "
               f"{lit(json.dumps(bench, default=str))}, current_timestamp())")


def certified_space(ctx) -> dict | None:
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {table(ctx)} ({COLS})")
    rows = ctx.ws.sql(f"""SELECT max_by(definition_json, recorded_at) AS d FROM {table(ctx)}
        WHERE system = {lit(ctx.system.name)} AND run_id IN (
            SELECT run_id FROM {ctx.state.t('certifications')} WHERE system = {lit(ctx.system.name)} AND goal_id = 'G5')""")
    return json.loads(rows[0]["d"]) if rows and rows[0]["d"] else None


def certified_authoring(ctx) -> dict:
    return (certified_space(ctx) or {}).get("authoring") or {}


def drift(ctx):
    """Inputs or semantic model changed since certification, the space missing, or edited outside MAYA."""
    from .common import differences, find_space, live_space, serialized
    cert = certified_space(ctx)
    if not cert:
        return []
    reasons = []
    for key, name in (("questions", "questions file"), ("benchmarks", "benchmarks file")):
        p = ctx.system.base_dir / ctx.inputs[key]
        if not p.exists():
            reasons.append(f"{name} {ctx.inputs[key]} is missing")
    want = serialized(cert)
    sid = find_space(ctx)
    if not sid:
        return reasons + ["the certified Genie space no longer exists"]
    diffs = differences(want, live_space(ctx, sid)["serialized"])
    if diffs:
        reasons.append(f"Genie space changed outside MAYA ({len(diffs)}): {diffs[0]}")
    from .space import _customer_questions
    from .common import questions_file, benchmarks_file, semantic_model
    qfile, _ = questions_file(ctx)
    bfile, _ = benchmarks_file(ctx)
    bench = sorted((b["question"].strip(), b["sql"].strip()) for b in bfile.get("benchmarks") or [])
    if bench != sorted((b["question"], b["sql"]) for b in cert["benchmarks"]):
        reasons.append("benchmarks changed since certification")
    if [r.strip() for r in qfile.get("rules") or []] != [r for r in _cert_rules(cert)]:
        reasons.append("business rules changed since certification")
    errors = []
    customer = _customer_questions(semantic_model(ctx), qfile, errors)
    have = {q["question"] for q in cert["sample_questions"] if q["origin"] == "customer"}
    now = {q["question"] for qs in customer.values() for q in qs}
    if now != have:
        reasons.append("customer questions changed since certification")
    return reasons


def _cert_rules(cert):
    lines, out, on = cert.get("instructions") or [], [], False
    for line in lines:
        if line.startswith("Business rules"):
            on = True
            continue
        if on and line.startswith("- "):
            out.append(line[2:])
        elif on:
            break
    return out
