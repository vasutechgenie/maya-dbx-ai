"""Shared helpers for G7: the quality schema and its tables, the monitored tables and their profile, the SQL of a
rule, and the delivered job, alerts and dashboard read back from the workspace."""
import fnmatch
import re

import yaml

from maya.core import catalog
from maya.core.workspace import SqlError, ident, lit

JOB_KEY = "maya_g7_quality"
ALERTS = ("critical_failures", "stale_data")
TABLES = ("dq_rules", "dq_runs", "dq_results", "dq_quarantine", "dq_freshness", "dq_alert_tests")
VIEWS = ("dq_latest", "dq_freshness_latest", "dq_run_summary")
TYPES = ("not_null", "unique", "accepted_values", "range", "expression", "references", "row_count")
NUMERIC = re.compile(r"^(TINYINT|SMALLINT|INT|INTEGER|BIGINT|LONG|FLOAT|DOUBLE|REAL|DECIMAL|NUMERIC)", re.I)
COMPARABLE = re.compile(r"^(TINYINT|SMALLINT|INT|INTEGER|BIGINT|LONG|FLOAT|DOUBLE|REAL|DECIMAL|NUMERIC|DATE|TIMESTAMP)", re.I)


def slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def marker(ctx, key=None) -> str:
    """How the bundle runner and MAYA find what G7 manages (in each alert's description)."""
    return f"maya:{slug(ctx.system.name)}:G7" + (f":{key}" if key else "")


def dq_schema(ctx) -> str:
    name = ctx.inputs["schema"]
    return name if "." in name else f"{ctx.system.catalogs[0]}.{name}"


def q(full_name) -> str:
    return ".".join(ident(p) for p in full_name.split("."))


def t(ctx_or_schema, name) -> str:
    s = ctx_or_schema if isinstance(ctx_or_schema, str) else dq_schema(ctx_or_schema)
    return f"{s}.{name}"


def sensitive(ctx) -> dict:
    s = ctx.inputs.get("sensitive") or (ctx.system.goal_settings("G4") or {}).get("sensitive") or {}
    return {"tag": s.get("tag", "sensitivity"), "values": list(s.get("values") or ["pii"])}


def allow_data(ctx) -> bool:
    if "allow_data" in ctx.inputs:
        return bool(ctx.inputs["allow_data"])
    return bool((ctx.system.goal_settings("G1") or {}).get("allow_data", False))


def freshness_limits(ctx) -> dict:
    return dict(ctx.inputs.get("freshness_hours") or (ctx.system.goal_settings("G0") or {}).get("freshness_hours") or {})


def schedule(ctx) -> dict:
    s = ctx.inputs.get("schedule") or {"cron": "0 0 6 * * ?"}
    return {"cron": s["cron"], "timezone": s.get("timezone", "UTC"), "paused": bool(s.get("paused", False))}


# ---------------------------------------------------------------- the customer's rules file
def rules_file(ctx) -> tuple[dict, list[str]]:
    ref = ctx.inputs.get("rules")
    if not ref:
        return {}, []
    path = ctx.system.base_dir / ref
    if not path.exists():
        return {}, [f"rules file {ref} not found"]
    data = yaml.safe_load(path.read_text()) or {}
    import jsonschema
    schema = ctx.goal.inputs_schema()
    errors = sorted(jsonschema.Draft202012Validator({"$ref": "#/$defs/rules_file", "$defs": schema["$defs"]})
                    .iter_errors(data), key=lambda e: list(e.path))
    return data, [f"{ref}: {e.message} at {'/'.join(map(str, e.path)) or 'root'}" for e in errors[:8]]


# ---------------------------------------------------------------- monitored tables
def scope(ctx, named=()) -> dict:
    """full name -> {layer, comment} of the tables to monitor: the declared layers' tables plus any named in the
    rules file, minus the excluded ones. Views are not monitored (their tables are)."""
    layers = set(ctx.inputs.get("layers") or ["silver", "gold"])
    exclude = ctx.inputs.get("exclude") or []
    out = {}
    for a in catalog.tables(ctx.ws, ctx.system):
        if a["kind"] != "table":
            continue
        short = a["full_name"].split(".", 1)[1]
        if any(fnmatch.fnmatch(short, p) or fnmatch.fnmatch(a["full_name"], p) for p in exclude):
            continue
        if a["layer"] in layers or a["full_name"] in named:
            out[a["full_name"]] = {"layer": a["layer"], "comment": a.get("comment")}
    return out


def resolve_table(ctx, name, known) -> str | None:
    if name in known:
        return name
    full = f"{ctx.system.catalogs[0]}.{name}" if name.count(".") == 1 else name
    if full in known:
        return full
    hits = [fn for fn in known if fn.split(".", 1)[1] == name]
    return hits[0] if len(hits) == 1 else None


def columns(ctx, tables) -> dict:
    """full name -> [{name, type, comment}] in ordinal order."""
    out = {fn: [] for fn in tables}
    for c in catalog.columns(ctx.ws, ctx.system):
        if c["full_name"] in out:
            out[c["full_name"]].append({"name": c["column_name"], "type": (c.get("data_type") or "").upper(),
                                        "comment": c.get("comment")})
    return out


def keys(ctx, tables) -> dict:
    """full name -> {primary_key: [...], foreign_keys: [{column, to: 'cat.schema.table.column'}]} from Unity Catalog
    (single-column foreign keys; a composite one is checked by the customer's rules)."""
    out = {fn: {"primary_key": [], "foreign_keys": []} for fn in tables}
    for k in catalog.constraints(ctx.ws, ctx.system):
        if k["full_name"] in out and k["constraint_type"] == "PRIMARY KEY":
            out[k["full_name"]]["primary_key"].append(k["column_name"])
    for cat in sorted({fn.split(".")[0] for fn in tables}):
        try:
            rows = ctx.ws.sql(f"""SELECT k.constraint_name AS fk, k.table_schema, k.table_name, k.column_name,
                  u.table_catalog AS rc, u.table_schema AS rs, u.table_name AS rt, u.column_name AS rcol
                FROM {ident(cat)}.information_schema.referential_constraints r
                JOIN {ident(cat)}.information_schema.key_column_usage k
                  ON k.constraint_name = r.constraint_name AND k.constraint_schema = r.constraint_schema
                JOIN {ident(cat)}.information_schema.key_column_usage u
                  ON u.constraint_name = r.unique_constraint_name AND u.constraint_schema = r.unique_constraint_schema
                 AND u.ordinal_position = k.position_in_unique_constraint""")
        except SqlError:
            continue
        per_fk = {}
        for r in rows:
            per_fk.setdefault((f"{cat}.{r['table_schema']}.{r['table_name']}", r["fk"]), []).append(r)
        for (fn, _), cols in sorted(per_fk.items()):
            if fn in out and len(cols) == 1:
                c = cols[0]
                out[fn]["foreign_keys"].append({"column": c["column_name"], "to": f"{c['rc']}.{c['rs']}.{c['rt']}.{c['rcol']}"})
    return out


def sensitive_columns(ctx, tables) -> dict:
    s = sensitive(ctx)
    tags = catalog.column_tags(ctx.ws, ctx.system, s["tag"])
    out = {fn: [] for fn in tables}
    for (fn, col), v in tags.items():
        if fn in out and v in s["values"]:
            out[fn].append(col)
    return out


def profile(ctx, fn, cols, hidden, values=True) -> dict:
    """Row count and, per column, nulls and distinct values; minimum, maximum and top values only for columns that
    are not sensitive, and only when data may be shown to the model."""
    parts = ["count(*) AS n"]
    for i, c in enumerate(cols):
        col = ident(c["name"])
        parts += [f"count_if({col} IS NULL) AS n{i}", f"approx_count_distinct({col}) AS d{i}"]
        if values and c["name"] not in hidden and COMPARABLE.match(c["type"]):
            parts += [f"CAST(min({col}) AS STRING) AS lo{i}", f"CAST(max({col}) AS STRING) AS hi{i}"]
    row = ctx.ws.sql(f"SELECT {', '.join(parts)} FROM {q(fn)}")[0]
    n = int(row["n"] or 0)
    out = []
    for i, c in enumerate(cols):
        p = {"name": c["name"], "type": c["type"], "comment": c.get("comment"), "sensitive": c["name"] in hidden,
             "nulls": int(row[f"n{i}"] or 0), "distinct": int(row[f"d{i}"] or 0)}
        if f"lo{i}" in row:
            p.update(min=row[f"lo{i}"], max=row[f"hi{i}"])
        if values and c["name"] not in hidden and c["type"].startswith("STRING") and 0 < p["distinct"] <= 25:
            top = ctx.ws.sql(f"SELECT {ident(c['name'])} AS v, count(*) AS n FROM {q(fn)} WHERE {ident(c['name'])} IS NOT NULL "
                             f"GROUP BY 1 ORDER BY 2 DESC LIMIT 25")
            p["top_values"] = [{"value": r["v"], "rows": int(r["n"])} for r in top]
        out.append(p)
    return {"rows": n, "columns": out}


# ---------------------------------------------------------------- rules
def rule_columns(rule) -> list[str]:
    if rule["type"] == "unique":
        return list(rule.get("columns") or ([rule["column"]] if rule.get("column") else []))
    return [rule["column"]] if rule.get("column") else []


def rule_name(rule) -> str:
    if rule.get("name"):
        return rule["name"]
    cols = rule_columns(rule)
    return slug("_".join([rule["type"], *cols])) if cols else rule["type"]


def rule_id(fn, rule) -> str:
    return f"{fn.split('.', 1)[1]}.{rule_name(rule)}"


def _value(v) -> str:
    return lit(v) if isinstance(v, str) else ("true" if v is True else "false" if v is False else str(v))


def rule_problem(rule, cols, known_tables) -> str | None:
    """Why a rule cannot be checked on its table (None when it can)."""
    kind, names = rule["type"], {c["name"]: c for c in cols}
    if kind not in TYPES:
        return f"unknown rule type {kind!r}"
    need = rule_columns(rule)
    if kind in ("not_null", "accepted_values", "range", "references", "unique") and not need:
        return f"{kind} needs a column"
    missing = [c for c in need if c not in names]
    if missing:
        return f"no column {', '.join(missing)}"
    if kind == "accepted_values" and not rule.get("values"):
        return "accepted_values needs values"
    if kind == "range":
        if rule.get("min") is None and rule.get("max") is None:
            return "range needs min or max"
        if not COMPARABLE.match(names[need[0]]["type"]):
            return f"range on a {names[need[0]]['type']} column"
    if kind == "expression" and not (rule.get("expression") and rule.get("name")):
        return "expression needs a name and an expression"
    if kind == "references":
        to = (rule.get("to") or "").rsplit(".", 1)
        if len(to) != 2 or to[0] not in known_tables:
            return f"references target {rule.get('to')!r} is not 'table.column' of a known table"
    if kind == "row_count" and rule.get("min") is None and rule.get("max") is None:
        return "row_count needs min or max"
    return None


def checked_relation(fn, rule) -> str:
    """SELECT of the table's rows with _maya_bad marking the rows that break the rule."""
    kind, cols = rule["type"], rule_columns(rule)
    col = ident(cols[0]) if cols else None
    if kind == "not_null":
        return f"SELECT *, {col} IS NULL AS _maya_bad FROM {q(fn)}"
    if kind == "unique":
        part = ", ".join(ident(c) for c in cols)
        return f"SELECT *, count(*) OVER (PARTITION BY {part}) > 1 AS _maya_bad FROM {q(fn)}"
    if kind == "accepted_values":
        return (f"SELECT *, {col} IS NOT NULL AND {col} NOT IN ({', '.join(_value(v) for v in rule['values'])}) "
                f"AS _maya_bad FROM {q(fn)}")
    if kind == "range":
        cond = [f"{col} < {_value(rule['min'])}" if rule.get("min") is not None else None,
                f"{col} > {_value(rule['max'])}" if rule.get("max") is not None else None]
        return f"SELECT *, coalesce({' OR '.join(c for c in cond if c)}, false) AS _maya_bad FROM {q(fn)}"
    if kind == "expression":
        return f"SELECT *, NOT coalesce(({rule['expression']}), false) AS _maya_bad FROM {q(fn)}"
    if kind == "references":
        target, tcol = rule["to"].rsplit(".", 1)
        return (f"SELECT t.*, t.{col} IS NOT NULL AND p._maya_key IS NULL AS _maya_bad FROM {q(fn)} t "
                f"LEFT JOIN (SELECT DISTINCT {ident(tcol)} AS _maya_key FROM {q(target)}) p ON t.{col} = p._maya_key")
    raise ValueError(kind)


def counts_sql(fn, rule) -> str:
    """total, failed of one rule (row_count: the row count, failed 1 when outside its bounds)."""
    if rule["type"] == "row_count":
        cond = [f"n < {_value(rule['min'])}" if rule.get("min") is not None else None,
                f"n > {_value(rule['max'])}" if rule.get("max") is not None else None]
        return (f"SELECT n AS total, CASE WHEN {' OR '.join(c for c in cond if c)} THEN 1 ELSE 0 END AS failed "
                f"FROM (SELECT count(*) AS n FROM {q(fn)})")
    return f"SELECT count(*) AS total, count_if(_maya_bad) AS failed FROM ({checked_relation(fn, rule)})"


def passed_sql(rule) -> str:
    tol = float(rule.get("tolerance") or 0)
    return "c.failed = 0" if not tol else f"c.failed <= floor(c.total * {tol})"


def dry_run(ctx, fn, rule) -> dict:
    """The rule checked now: {total, failed, passed} or {error}."""
    try:
        r = ctx.ws.sql(counts_sql(fn, rule))[0]
    except SqlError as e:
        return {"error": str(e).splitlines()[0][:300]}
    total, failed = int(r["total"] or 0), int(r["failed"] or 0)
    tol = float(rule.get("tolerance") or 0)
    return {"total": total, "failed": failed, "passed": failed == 0 if not tol else failed <= int(total * tol)}


# ---------------------------------------------------------------- the delivered objects, read back
def bundle_ids(ctx) -> dict:
    """{"jobs": {key: id}, "alerts": {key: id}} of the bundle's deployed resources (bundle summary)."""
    import json
    from maya.core import bundle
    r = bundle.ensure(ctx.system, ctx.ws)
    code, out = bundle._cli(ctx.system, ["bundle", "summary", "-t", bundle.target(ctx.system), "-o", "json"], r)
    if code:
        return {}
    data = json.loads(out[out.index("{"):])
    res = data.get("resources") or {}
    return {kind: {k: v.get("id") for k, v in (res.get(kind) or {}).items()} for kind in ("jobs", "alerts")}


def dev_mode(ctx) -> bool:
    from maya.core import bundle
    p = bundle.root(ctx.system) / "databricks.yml"
    if not p.exists():
        return False
    d = yaml.safe_load(p.read_text()) or {}
    return ((d.get("targets") or {}).get(bundle.target(ctx.system)) or {}).get("mode") == "development"


def alert_key(name) -> str:
    return f"maya_g7_{name}"


def plan(ctx) -> dict:
    return ctx.read_artefact("quality_plan.json") or {}
