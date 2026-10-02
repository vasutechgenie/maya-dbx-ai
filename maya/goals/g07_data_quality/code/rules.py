"""G7 load / suggest / plan. The rules come from three places: the customer's rules file, the keys in Unity Catalog
(primary keys unique and not null, foreign keys resolve) and the dq_analyst agent's suggestions from each table's
profile. MAYA checks every rule against the table, dry-runs it, and plans the rule set the data owner approves."""
from concurrent.futures import ThreadPoolExecutor

from maya.core import catalog
from maya.core.spec import stable_hash

from . import ledger
from .common import (allow_data, columns, dq_schema, dry_run, freshness_limits, keys, marker, profile, resolve_table,
                     rule_columns, rule_id, rule_problem, rules_file, schedule, scope, sensitive, sensitive_columns)

RULE_KEYS = ("type", "name", "column", "columns", "values", "min", "max", "expression", "to", "severity", "tolerance",
             "description")


def _clean(rule, origin, reason=None):
    out = {k: rule[k] for k in RULE_KEYS if rule.get(k) not in (None, [], "")}
    out.setdefault("severity", "warning")
    out["origin"] = origin
    if reason:
        out["reason"] = " ".join(str(reason).split())
    return out


def _key_rules(k) -> list[dict]:
    out = []
    pk = k["primary_key"]
    for c in pk:
        out.append({"type": "not_null", "column": c, "severity": "critical", "origin": "key",
                    "reason": "primary key column"})
    if pk:
        out.append({"type": "unique", "columns": list(pk), "severity": "critical", "origin": "key", "reason": "primary key"}
                   if len(pk) > 1 else
                   {"type": "unique", "column": pk[0], "severity": "critical", "origin": "key", "reason": "primary key"})
    for fk in k["foreign_keys"]:
        out.append({"type": "references", "column": fk["column"], "to": fk["to"], "severity": "warning", "origin": "key",
                    "reason": "foreign key"})
    return out


def _resolve_to(ctx, rule, known):
    if rule.get("type") == "references" and rule.get("to"):
        target, col = rule["to"].rsplit(".", 1) if "." in rule["to"] else (rule["to"], "")
        fn = resolve_table(ctx, target, known)
        if fn:
            rule = {**rule, "to": f"{fn}.{col}"}
    return rule


def _inputs(ctx):
    """Everything the plan is built from, and the errors in the customer's inputs."""
    rfile, errors = rules_file(ctx)
    every = {a["full_name"]: a for a in catalog.tables(ctx.ws, ctx.system) if a["kind"] == "table"}
    declared = {}
    for name, spec in (rfile.get("tables") or {}).items():
        fn = resolve_table(ctx, name, every)
        if not fn:
            errors.append(f"rules file: table {name!r} is not a table of the foundation")
            continue
        declared[fn] = spec or {}
    tables = scope(ctx, set(declared))
    cols = columns(ctx, tables)
    k = keys(ctx, tables)
    hidden = sensitive_columns(ctx, tables)
    customer = {}
    for fn, spec in declared.items():
        customer[fn] = []
        for r in spec.get("rules") or []:
            r = _resolve_to(ctx, r, every)
            why = rule_problem(r, cols.get(fn) or [], every)
            if why:
                errors.append(f"rules file: {fn.split('.', 1)[1]} rule {r.get('name') or r['type']}: {why}")
            else:
                customer[fn].append(_clean(r, "customer"))
    if not tables:
        errors.append("no tables to monitor (goals.G7.layers and the rules file name none)")
    limits = freshness_limits(ctx)
    freshness = [{"table": fn, "layer": tables[fn]["layer"],
                  "max_hours": (declared.get(fn) or {}).get("freshness_hours") or limits.get(tables[fn]["layer"])}
                 for fn in sorted(tables)]
    return {"tables": tables, "columns": cols, "keys": k, "sensitive": hidden, "customer": customer,
            "reject": sorted(set(rfile.get("reject") or [])), "freshness": freshness}, errors


def inputs_hash(i) -> str:
    return stable_hash({k: i[k] for k in ("tables", "columns", "keys", "sensitive", "customer", "reject", "freshness")})


def load_inputs_hash(ctx) -> str | None:
    i, errors = _inputs(ctx)
    return None if errors else inputs_hash(i)


def _existing(i, fn) -> list[dict]:
    """The rules a table already has (customer first, then its keys), without duplicates."""
    out, seen = [], set()
    for r in i["customer"].get(fn, []) + _key_rules(i["keys"][fn]):
        if rule_id(fn, r) not in seen:
            seen.add(rule_id(fn, r))
            out.append(r)
    return out


def load(ctx):
    i, errors = _inputs(ctx)
    if errors:
        raise RuntimeError("G7 inputs are invalid:\n  - " + "\n  - ".join(errors))
    agent = (ctx.goal.dir / "harness" / "agents" / "dq_analyst.md").read_text()
    show = allow_data(ctx)
    maximum = ctx.inputs.get("max_suggestions_per_table", 6)
    hashes = {fn: stable_hash({"columns": i["columns"][fn], "keys": i["keys"][fn], "sensitive": i["sensitive"][fn],
                               "existing": _existing(i, fn), "reject": i["reject"], "show": show, "max": maximum,
                               "agent": agent}) for fn in i["tables"]}
    certified = ledger.certified_suggestions(ctx)
    full = ctx.inputs.get("mode") == "full"
    suggest = ctx.inputs.get("suggest", True) and maximum > 0
    reused = {fn: s for fn, s in certified.items() if not full and hashes.get(fn) == s.get("hash")} if suggest else {}
    todo = [fn for fn in sorted(i["tables"]) if suggest and fn not in reused]
    profiles = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for fn, p in zip(todo, pool.map(lambda fn: profile(ctx, fn, i["columns"][fn], set(i["sensitive"][fn]), show), todo)):
            profiles[fn] = p
    ctx.write_artefact("context.json", {**i, "hashes": hashes, "reused": reused, "profiles": profiles,
                                        "inputs_hash": inputs_hash(i)})
    ctx.log(f"     {len(i['tables'])} tables to monitor; {sum(len(v) for v in i['customer'].values())} customer rules; "
            f"profiling and suggesting for {len(todo)}" + (f", reusing {len(reused)} unchanged" if reused else ""))
    return {"suggest_tables": todo, "reused_tables": sorted(reused), "tables": len(i["tables"])}


def _context(ctx):
    return ctx.read_artefact("context.json") or {}


def _summary(fn, r) -> str:
    cols = ", ".join(rule_columns(r))
    return {"not_null": f"{cols} is never null", "unique": f"{cols} is unique",
            "accepted_values": f"{cols} is one of {', '.join(map(str, r.get('values') or []))}",
            "range": f"{cols} is " + (f"between {r.get('min')} and {r.get('max')}" if r.get("min") is not None and r.get("max") is not None
                                      else f"at least {r['min']}" if r.get("min") is not None else f"at most {r.get('max')}"),
            "expression": f"every row satisfies {r.get('expression')}",
            "references": f"{cols} matches {'.'.join((r.get('to') or '').split('.')[-2:])}",
            "row_count": "the table has " + (f"{r['min']} to {r['max']} rows" if r.get("min") is not None and r.get("max") is not None
                                              else f"at least {r['min']} rows" if r.get("min") is not None else f"at most {r.get('max')} rows")
            }[r["type"]]


def suggest_input(ctx, fn):
    c = _context(ctx)
    k = c["keys"][fn]
    return {"table": fn, "layer": c["tables"][fn]["layer"], "description": c["tables"][fn].get("comment"),
            "rows": c["profiles"][fn]["rows"], "columns": c["profiles"][fn]["columns"],
            "primary_key": k["primary_key"], "foreign_keys": k["foreign_keys"],
            "existing_rules": [f"{rule_id(fn, r)}: {_summary(fn, r)}" for r in _existing(c, fn)],
            "rejected": [r for r in c["reject"] if r.startswith(fn.split(".", 1)[1] + ".")],
            "max_rules": ctx.inputs.get("max_suggestions_per_table", 6)}


def _same(a, b) -> bool:
    return a["type"] == b["type"] and rule_columns(a) == rule_columns(b) and a["type"] not in ("expression", "row_count")


def plan(ctx):
    c = _context(ctx)
    todo = (ctx.outputs.get("load") or {}).get("suggest_tables") or []
    results = {r["table"]: r for r in ctx.read_artefact("suggestions.json") or [] if isinstance(r, dict) and r.get("table")}
    suggestions, findings = dict(c["reused"]), []
    for fn in todo:
        r = results.get(fn)
        if not r:
            findings.append(f"{fn}: the dq_analyst agent returned no result")
            continue
        suggestions[fn] = {"hash": c["hashes"][fn], "rules": r.get("rules") or []}
    every = {a["full_name"] for a in catalog.tables(ctx.ws, ctx.system) if a["kind"] == "table"}
    maximum = ctx.inputs.get("max_suggestions_per_table", 6)
    candidates = []
    for fn in sorted(c["tables"]):
        chosen = _existing(c, fn)
        added = 0
        for s in (suggestions.get(fn) or {}).get("rules") or []:
            r = _clean(_resolve_to(ctx, s, every), "agent", s.get("reason"))
            rid = rule_id(fn, r)
            why = rule_problem(r, c["columns"][fn], every)
            if why:
                findings.append(f"{rid}: suggestion left out: {why}")
            elif rid in c["reject"]:
                continue
            elif any(rule_id(fn, x) == rid or _same(x, r) for x in chosen):
                continue
            elif added >= maximum:
                findings.append(f"{rid}: suggestion left out: more than {maximum} suggestions for the table")
            else:
                chosen.append(r)
                added += 1
        candidates += [(fn, r) for r in chosen]

    with ThreadPoolExecutor(max_workers=8) as pool:
        baselines = list(pool.map(lambda x: dry_run(ctx, *x), candidates))
    rules, broken = [], []
    for (fn, r), b in zip(candidates, baselines):
        rid = rule_id(fn, r)
        if b.get("error"):
            (broken if r["origin"] == "customer" else findings).append(
                f"{rid}: " + ("" if r["origin"] == "customer" else "left out: ") + f"its query fails: {b['error']}")
            continue
        if r["origin"] == "agent" and not b["passed"]:
            findings.append(f"{rid}: suggested rule fails today on {b['failed']} of {b['total']} rows (review it)")
        hidden = set(c["sensitive"][fn])
        cols = [x["name"] for x in c["columns"][fn]]
        rules.append({"id": rid, "table": fn, **r, "description": r.get("description") or _summary(fn, r),
                      "key_columns": [k for k in c["keys"][fn]["primary_key"] if k not in hidden],
                      "quarantine_columns": [x for x in cols if x not in hidden],
                      "baseline": b})
    if broken:
        raise RuntimeError("G7 rules in the rules file do not run:\n  - " + "\n  - ".join(broken))

    title = (ctx.inputs.get("dashboard") or {}).get("title") or f"{ctx.system.name} data quality"
    spec = {"marker": marker(ctx), "schema": dq_schema(ctx), "rules": rules, "freshness": c["freshness"],
            "schedule": schedule(ctx), "recipients": ctx.inputs.get("recipients") or [],
            "quarantine_rows": ctx.inputs.get("quarantine_rows", 100), "retention_days": ctx.inputs.get("retention_days", 90),
            "sensitive": sensitive(ctx), "hidden": {fn: sorted(v) for fn, v in c["sensitive"].items() if v},
            "dashboard": {"title": title, "parent_path": (ctx.inputs.get("dashboard") or {}).get("parent_path", "MAYA"),
                          "credentials": (ctx.inputs.get("dashboard") or {}).get("credentials", "viewer")},
            "readers": ctx.inputs.get("readers") or [], "project": ctx.system.name,
            "inputs_hash": c["inputs_hash"], "findings": findings, "suggestions": suggestions}
    ctx.write_artefact("quality_plan.json", spec)
    by = {o: sum(r["origin"] == o for r in rules) for o in ("customer", "key", "agent")}
    failing = sum(not r["baseline"]["passed"] for r in rules)
    ctx.log(f"     plan: {len(rules)} rules on {len(c['tables'])} tables ({by['customer']} customer, {by['key']} from keys, "
            f"{by['agent']} suggested); {failing} fail today; {len(findings)} findings")
    return {"rules": len(rules), "tables": len(c["tables"]), "failing_today": failing, "findings": len(findings), **by}
