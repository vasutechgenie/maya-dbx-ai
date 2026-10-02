"""Shared helpers for G8: the tools file, the schemas tools may read, the SQL of a tool function (DDL, typed calls,
tests) and how a test result is compared with what the product owner expects."""
import json
import re
from decimal import Decimal, InvalidOperation

import yaml

from maya.core import identity
from maya.core.workspace import ident, lit

MARK = "(MAYA G8)"
FORBIDDEN = re.compile(r"\b(EXECUTE\s+IMMEDIATE|INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE)\b", re.I)
RELATION = re.compile(r"\b(?:FROM|JOIN)\s+((?:`[^`]+`|[\w]+)(?:\.(?:`[^`]+`|[\w]+))*)", re.I)
SQL_FROM = re.compile(r"\b(EXTRACT|TRIM|SUBSTRING|POSITION|OVERLAY)\s*\([^()]*\)", re.I)  # FROM inside a function call
CTE = re.compile(r"(?:\bWITH|,)\s*(\w+)\s+AS\s*\(", re.I)


def q(full_name) -> str:
    return ident(*full_name.split("."))


def _schema_name(ctx, name) -> str:
    return name if "." in name else f"{ctx.system.catalogs[0]}.{name}"


def tools_schema(ctx) -> str:
    return _schema_name(ctx, ctx.inputs["schema"])


def full_name(ctx, tool) -> str:
    return f"{tools_schema(ctx)}.{tool['name']}"


def tools_file(ctx) -> tuple[list, list[str]]:
    ref = ctx.inputs["tools"]
    path = ctx.system.base_dir / ref
    if not path.exists():
        return [], [f"tools file {ref} not found"]
    data = yaml.safe_load(path.read_text()) or {}
    import jsonschema
    schema = ctx.goal.inputs_schema()
    errors = sorted(jsonschema.Draft202012Validator({"$ref": "#/$defs/tools_file", "$defs": schema["$defs"]})
                    .iter_errors(data), key=lambda e: list(e.path))
    out = [f"{ref}: {e.message} at {'/'.join(map(str, e.path)) or 'root'}" for e in errors[:8]]
    tools = [] if out else (data.get("tools") or [])
    names = [t["name"] for t in tools]
    out += [f"{ref}: tool {n} is declared more than once" for n in sorted({n for n in names if names.count(n) > 1})]
    for t in tools:
        params = {p["name"] for p in t.get("parameters") or []}
        for i, ex in enumerate(t["examples"]):
            unknown = sorted(set(ex["args"]) - params)
            missing = sorted(p["name"] for p in t.get("parameters") or [] if p["name"] not in ex["args"] and "default" not in p)
            if unknown:
                out.append(f"{ref}: tool {t['name']} example {i + 1} has unknown arguments {unknown}")
            if missing:
                out.append(f"{ref}: tool {t['name']} example {i + 1} lacks arguments {missing}")
    return tools, out


def executors(ctx) -> list[str]:
    sp = identity.principal(ctx.system)
    return list(dict.fromkeys([*([sp] if sp else []), *(ctx.inputs.get("executors") or [])]))


def read_schemas(ctx) -> list[str]:
    """'catalog.schema' names the tools may read."""
    out = []
    for scope in ctx.inputs.get("reads") or ["metric_views", "semantic", "gold"]:
        if scope in ctx.system.layers:
            out += [f"{s['catalog']}.{s['schema']}" for s in ctx.system.layers[scope]["sources"]]
        elif scope in ("metric_views", "semantic"):
            g = ctx.system.goal_settings("G2" if scope == "metric_views" else "G3") or {}
            out += [_schema_name(ctx, g["schema"])] if g.get("schema") else []
        else:
            out.append(_schema_name(ctx, scope))
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- SQL of a tool
def literal(type_, value) -> str:
    if value is None:
        return f"CAST(NULL AS {type_})"
    return f"CAST({lit(value)} AS {type_})"


def _norm(sql) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).strip()


def body_problems(ctx, name, sql, params) -> list[str]:
    """Why a body is not a safe, parameterised read of the allowed schemas."""
    s = _norm(sql)
    out = []
    if not re.match(r"^(SELECT|WITH)\b", s, re.I):
        out.append("the body must be one SELECT (or WITH ... SELECT)")
    if ";" in s:
        out.append("the body must be one statement (no ';')")
    if FORBIDDEN.search(s):
        out.append(f"the body may only read data ({FORBIDDEN.search(s).group(1).upper()} is not allowed)")
    if "${" in s or "IDENTIFIER(" in s.upper():
        out.append("the body may not build identifiers or SQL text")
    ctes = {m.lower() for m in CTE.findall(s)}
    allowed = {x.lower() for x in read_schemas(ctx)}
    for rel in RELATION.findall(SQL_FROM.sub("", s)):
        parts = [p.strip("`") for p in rel.split(".")]
        if len(parts) == 1 and parts[0].lower() in ctes:
            continue
        if parts[0] == "(" or rel.startswith("("):
            continue
        if len(parts) != 3:
            out.append(f"{rel}: name every table or view in full (catalog.schema.name)")
        elif f"{parts[0]}.{parts[1]}".lower() not in allowed:
            out.append(f"{rel}: tools may read only {', '.join(sorted(allowed))}")
    for p in params:
        if not re.search(rf"\b{re.escape(name)}\.{re.escape(p['name'])}\b", s):
            out.append(f"parameter {p['name']} is not used as {name}.{p['name']}")
    return out


def bind(name, sql, params, args) -> str:
    """The body with each parameter replaced by a typed literal (for DESCRIBE QUERY and dry runs)."""
    out = sql
    for p in params:
        v = args.get(p["name"], p.get("default"))
        out = re.sub(rf"\b{re.escape(name)}\.{re.escape(p['name'])}\b", lambda _m, p=p, v=v: literal(p["type"], v), out)
    return out


def create_sql(fn, tool) -> str:
    params = ", ".join(
        f"{ident(p['name'])} {p['type']}"
        + (f" DEFAULT {literal(p['type'], p['default'])}" if "default" in p else "")
        + f" COMMENT {lit(p['comment'])}" for p in tool["parameters"])
    cols = ", ".join(f"{ident(c['name'])} {c['type']} COMMENT {lit(c['comment'])}" for c in tool["columns"])
    return (f"CREATE OR REPLACE FUNCTION {q(fn)}({params})\nRETURNS TABLE ({cols})\n"
            f"COMMENT {lit(tool['comment'] + ' ' + MARK)}\nRETURN {tool['sql'].strip().rstrip(';')}")


def call_sql(fn, tool, args) -> str:
    named = ", ".join(f"{p['name']} => {literal(p['type'], args[p['name']])}" for p in tool["parameters"] if p["name"] in args)
    return f"SELECT * FROM {q(fn)}({named})"


# ---------------------------------------------------------------- tests
def _num(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    x, y = _num(a), _num(b)
    if x is not None and y is not None:
        return abs(x - y) <= max(Decimal("0.01"), abs(y) * Decimal("0.000001"))
    return str(a) == str(b)


def compare(rows, expect, truth=None) -> list[str]:
    """Where a test result differs from its expectation (truth: rows of expect.matches.sql)."""
    out = []
    if "min_rows" in expect and len(rows) < expect["min_rows"]:
        out.append(f"{len(rows)} rows, expected at least {expect['min_rows']}")
    if "max_rows" in expect and len(rows) > expect["max_rows"]:
        out.append(f"{len(rows)} rows, expected at most {expect['max_rows']}")
    have = set(rows[0]) if rows else None
    if have is not None:
        miss = [c for c in expect.get("columns") or [] if c not in have]
        if miss:
            out.append(f"missing columns {miss}")
    m = expect.get("matches")
    if m and truth is not None:
        key = m["key"]
        cols = [c for c in (truth[0] if truth else {}) if c not in key]
        if rows and any(k not in rows[0] for k in key + cols):
            out.append(f"result lacks the columns of the matching SQL: {[c for c in key + cols if c not in rows[0]]}")
            return out
        got = {tuple(str(r[k]) for k in key): r for r in rows}
        want = {tuple(str(r[k]) for k in key): r for r in truth}
        for k in sorted(set(want) - set(got))[:5]:
            out.append(f"{dict(zip(key, k))}: missing from the result")
        for k in sorted(set(got) - set(want))[:5]:
            out.append(f"{dict(zip(key, k))}: not in the matching SQL's result")
        for k in sorted(set(got) & set(want)):
            for c in cols:
                if not same(got[k][c], want[k][c]):
                    out.append(f"{dict(zip(key, k))} {c}: {got[k][c]}, expected {want[k][c]}")
    return out[:10]


def run_test(ctx, fn, tool, example, as_agent: bool) -> dict:
    """Run one example (as the agent identity or the deployer) and compare it with its expectation."""
    sql = call_sql(fn, tool, example["args"])
    run = (lambda s: identity.sql(ctx, s)) if as_agent else ctx.ws.sql
    out = {"tool": tool["name"], "args": example["args"], "expect": example["expect"], "sql": sql}
    try:
        rows = run(sql)
        truth = ctx.ws.sql(example["expect"]["matches"]["sql"]) if example["expect"].get("matches") else None
    except Exception as e:
        return {**out, "passed": False, "problems": [str(e)[:500]]}
    problems = compare(rows, example["expect"], truth)
    limit = int(ctx.inputs.get("max_rows", 50))
    return {**out, "passed": not problems, "problems": problems, "rows": len(rows), "sample": rows[:limit]}


def dumps(x) -> str:
    return json.dumps(x, sort_keys=True, default=str)


def plan(ctx) -> dict:
    return ctx.read_artefact("tool_plan.json") or {}
