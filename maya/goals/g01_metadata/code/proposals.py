"""G1: describer input, and turning describer results into reviewed proposals (rules win over the agent)."""
import json

from maya.core import agents, catalog

from . import ledger
from .common import (business_context, classes, existing_comment, existing_tag, min_chars, open_columns, rule_class,
                     scope, weak)


def describer_input(ctx, full_name, feedback=None):
    assets = {a["full_name"]: a for a in scope(ctx)}
    a = assets[full_name]
    prof = (ctx.read_artefact("profiles.json") or {}).get(full_name, {})
    todo = {c["name"] for c in open_columns(ctx, a)}
    inp = {
        "asset": {"full_name": full_name, "layer": a["layer"], "kind": a["kind"], "rows": prof.get("rows"),
                  "existing_description": existing_comment(ctx, a), "view_definition": a["view_definition"]},
        "describe_columns": a["scope"] == "full",
        "columns": [{"name": c["name"], "type": c["type"], "nullable": c["nullable"],
                     "existing_description": existing_comment(ctx, a, c), "existing_sensitivity": existing_tag(ctx, c),
                     "profile": prof.get("columns", {}).get(c["name"])} for c in a["columns"] if c["name"] in todo],
        "documented_columns": [{"name": c["name"], "type": c["type"], "description": existing_comment(ctx, a, c),
                                "sensitivity": existing_tag(ctx, c)} for c in a["columns"] if c["name"] not in todo],
        "sensitivity_classes": classes(ctx),
        "rule_classified": {c["name"]: rule_class(ctx, c["name"]) for c in a["columns"] if rule_class(ctx, c["name"])},
        "business_context": business_context(ctx),
        "layers": catalog.describe_layers(ctx.system),
        "other_assets": [{"full_name": o["full_name"], "layer": o["layer"], "kind": o["kind"],
                          "columns": [c["name"] for c in o["columns"]]} for o in assets.values() if o["full_name"] != full_name],
    }
    if feedback:
        inp["feedback_on_previous_attempt"] = feedback
    return inp


def _problems(ctx, asset, result):
    if not result:
        return ["no result"]
    got = {c["name"]: c for c in result["columns"]}
    out = []
    if existing_comment(ctx, asset) is None:
        why = weak(result["description"], asset["full_name"].split(".")[-1], min_chars(ctx))
        if why:
            out.append(f"table description {why}")
    for c in open_columns(ctx, asset):
        r = got.get(c["name"])
        if not r:
            out.append(f"column {c['name']} missing from result")
            continue
        if existing_tag(ctx, c) is None and r["sensitivity"] not in classes(ctx):
            out.append(f"column {c['name']}: sensitivity {r['sensitivity']!r} is not an allowed class")
        if asset["scope"] == "full" and existing_comment(ctx, asset, c) is None:
            why = weak(r.get("description"), c["name"], min_chars(ctx, column=True))
            if why:
                out.append(f"column {c['name']}: description {why}")
    return out


def redescribe(ctx, full_name, feedback, label):
    model = ctx.system.model(ctx.inputs.get("model") or ctx.goal.spec["spec"]["harness"].get("model"))
    schema = json.loads((ctx.goal.dir / "harness/schemas/description.schema.json").read_text())
    return agents.run_agent(ctx, "metadata_describer", "describe one data asset and classify its columns",
                            describer_input(ctx, full_name, feedback), schema, model, label)["result"]


def keep_what_exists(ctx, asset, prop):
    """Lay the values already in Unity Catalog over a proposal: they are never changed (unless overwrite_existing).
    The proposal's columns follow the asset's current columns."""
    full = asset["scope"] == "full"
    old = {c["name"]: c for c in prop["columns"]}
    cols = []
    for c in asset["columns"]:
        col = dict(old.get(c["name"]) or {"name": c["name"], "comment": None, "sensitivity": None,
                                          "sensitivity_source": None, "agent_sensitivity": None, "reason": None,
                                          "references": None})
        col["comment"] = (existing_comment(ctx, asset, c) or col.get("comment")) if full else None
        if existing_tag(ctx, c) is not None:
            col["sensitivity"], col["sensitivity_source"] = existing_tag(ctx, c), "existing"
        cols.append(col)
    return dict(prop, layer=asset["layer"], kind=asset["kind"], scope=asset["scope"],
                comment=existing_comment(ctx, asset) or prop["comment"], columns=cols)


def proposal_for(ctx, asset, result):
    got = {c["name"]: c for c in result["columns"]}
    cols = []
    for c in asset["columns"]:
        r = got.get(c["name"], {})
        rule = rule_class(ctx, c["name"])
        cols.append({
            "name": c["name"],
            "comment": r.get("description") if asset["scope"] == "full" else None,
            "sensitivity": rule or r.get("sensitivity"),
            "sensitivity_source": "rule" if rule else "agent",
            "agent_sensitivity": r.get("sensitivity"),
            "reason": r.get("sensitivity_reason"),
            "references": r.get("references"),
        })
    return keep_what_exists(ctx, asset, {
        "full_name": asset["full_name"], "comment": result["description"], "grain": result.get("grain"),
        "proposed_pk": result.get("primary_key") or [], "columns": cols})


def _make_consistent(ctx, props, certified, assets):
    """Same column name -> same class for agent-classified columns. A class already certified or already in Unity
    Catalog for that name wins; otherwise the most restrictive class proposed in this run."""
    rank = {c: i for i, c in enumerate(classes(ctx))}
    settled = {}
    known = [c for prev in certified.values() for c in prev["proposal"]["columns"]]
    known += [{"name": c["name"], "sensitivity": existing_tag(ctx, c)} for a in assets for c in a["columns"]]
    for c in known:
        if c["sensitivity"] in rank and rank[c["sensitivity"]] > rank.get(settled.get(c["name"]), -1):
            settled[c["name"]] = c["sensitivity"]
    strictest = dict(settled)
    for p in props:
        for c in p["columns"]:
            if c["name"] not in settled and c["sensitivity"] in rank \
                    and rank[c["sensitivity"]] > rank.get(strictest.get(c["name"]), -1):
                strictest[c["name"]] = c["sensitivity"]
    for p in props:
        for c in p["columns"]:
            if c["sensitivity_source"] == "agent" and strictest.get(c["name"]) not in (None, c["sensitivity"]):
                c["sensitivity"], c["sensitivity_source"] = strictest[c["name"]], "consistency"


def build(ctx):
    plan = ctx.read_artefact("plan.json")
    work = set(plan["new"] + plan["changed"])
    certified = {} if ctx.inputs.get("mode", "incremental") == "full" else ledger.certified(ctx)
    results = {r["asset"]: r for r in ctx.read_artefact("descriptions.json") or []}
    out, retried = [], []
    for i, asset in enumerate(scope(ctx)):
        if asset["full_name"] not in work:
            continue
        res = results.get(asset["full_name"])
        problems = _problems(ctx, asset, res)
        if problems:
            retried.append({"asset": asset["full_name"], "problems": problems})
            res = redescribe(ctx, asset["full_name"], problems, f"redescribe-{i}")
            still = _problems(ctx, asset, res)
            if still:
                raise RuntimeError(f"describer could not produce a complete result for {asset['full_name']}: {still}")
            results[asset["full_name"]] = res
        out.append(proposal_for(ctx, asset, res))
    if ctx.inputs.get("sensitivity_consistency", "most_restrictive") == "most_restrictive":
        _make_consistent(ctx, out, certified, scope(ctx))
    by_name = {a["full_name"]: a for a in scope(ctx)}
    restored = [keep_what_exists(ctx, by_name[n], certified[n]["proposal"]) for n in plan["restore"]]
    ctx.write_artefact("descriptions.json", list(results.values()))
    ctx.write_artefact("review.json", out)
    ctx.write_artefact("proposals.json", out + restored)
    cols = [c for p in out for c in p["columns"]]
    kept = [c for p in out + restored for c in p["columns"] if c["sensitivity_source"] == "existing"]
    return {"assets": len(out), "restored": len(restored), "columns": len(cols), "retried": retried,
            "kept_existing_tags": len(kept),
            "existing_conflicts_with_rules": [
                f"{p['full_name']}.{c['name']}: {c['sensitivity']} kept, rule says {rule_class(ctx, c['name'])}"
                for p in out + restored for c in p["columns"] if c["sensitivity_source"] == "existing"
                and rule_class(ctx, c["name"]) not in (None, c["sensitivity"])],
            "made_consistent": sorted({f"{c['name']} -> {c['sensitivity']}" for c in cols if c["sensitivity_source"] == "consistency"}),
            "by_sensitivity": {k: sum(1 for c in cols if c["sensitivity"] == k) for k in classes(ctx)},
            "rule_overrides": [f"{p['full_name']}.{c['name']}: agent {c['agent_sensitivity']} -> rule {c['sensitivity']}"
                               for p in out for c in p["columns"]
                               if c["sensitivity_source"] == "rule" and c["agent_sensitivity"] != c["sensitivity"]]}
