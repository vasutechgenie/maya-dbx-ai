"""G1 review gate: the data steward sees (and may edit) the proposals for the assets planned in this run. An edit is
accepted only when it still covers exactly those assets and columns and meets the same rules the validator applies."""
from .common import classes, min_chars, proposals, rule_class, scope, weak


def review(ctx, data):
    plan = ctx.read_artefact("plan.json") or {}
    planned = set(plan.get("new", []) + plan.get("changed", []))
    assets = {a["full_name"]: a for a in scope(ctx)}
    allowed = set(classes(ctx))
    out, seen = [], set()
    for p in data:
        fn = p["full_name"]
        if fn in seen:
            out.append(f"{fn} listed twice")
        seen.add(fn)
        if fn not in planned or fn not in assets:
            out.append(f"{fn} is not an asset planned for this run")
            continue
        a = assets[fn]
        why = weak(p["comment"], fn.split(".")[-1], min_chars(ctx))
        if why:
            out.append(f"{fn}: table description {why}")
        want = [c["name"] for c in a["columns"]]
        got = [c["name"] for c in p["columns"]]
        if sorted(want) != sorted(got):
            missing, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
            out.append(f"{fn}: columns must be exactly the asset's columns"
                       + (f"; missing {missing}" if missing else "") + (f"; unknown {extra}" if extra else ""))
        for c in p["columns"]:
            where = f"{fn}.{c['name']}"
            if a["scope"] == "full":
                text = c.get("comment")
                why = "missing" if not text else weak(text, c["name"], min_chars(ctx, column=True))
                if why:
                    out.append(f"{where}: description {why}")
            if c["sensitivity"] not in allowed:
                out.append(f"{where}: sensitivity {c['sensitivity']!r} is not one of {sorted(allowed)}")
            rule = rule_class(ctx, c["name"])
            if rule and c["sensitivity"] != rule:
                out.append(f"{where}: a sensitivity rule requires {rule!r}, got {c['sensitivity']!r}")
        unknown_pk = [k for k in p.get("proposed_pk") or [] if k not in want]
        if unknown_pk:
            out.append(f"{fn}: proposed_pk columns {unknown_pk} are not columns of the asset")
    out += [f"{fn} is planned for this run but missing from the review" for fn in sorted(planned - seen)]
    return out


def accept_review_edit(ctx, data):
    """Carry the steward's edit into proposals.json (what apply writes); changed classes are marked as the steward's."""
    edited = {p["full_name"]: p for p in data}
    out = []
    for p in proposals(ctx):
        e = edited.get(p["full_name"])
        if e:
            p = dict(p, comment=e["comment"], grain=e.get("grain", p.get("grain")),
                     proposed_pk=e.get("proposed_pk", p.get("proposed_pk")))
            cols = {c["name"]: c for c in e["columns"]}
            merged = []
            for c in p["columns"]:
                ec = cols[c["name"]]
                changed = ec["sensitivity"] != c["sensitivity"]
                merged.append(dict(c, comment=ec.get("comment"), sensitivity=ec["sensitivity"],
                                   sensitivity_source="steward" if changed else c["sensitivity_source"]))
            p["columns"] = merged
        out.append(p)
    ctx.write_artefact("proposals.json", out)
