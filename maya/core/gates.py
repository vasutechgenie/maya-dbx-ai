"""Gate input validation. Every item a gate shows its approver declares a JSON Schema and, optionally, a goal check.
A gate opens only when its items validate, an approver's edit is accepted only when it validates, and an approval is
recorded only when the items still validate and are byte-for-byte what was shown (or what the approver submitted).

graph.yaml:
  review:
    type: gate
    approver: data_steward
    items:
      review.json:
        schema: harness/schemas/gates/review.schema.json   # JSON Schema (required)
        check: proposals.check_review                      # code/<module>.<fn>(ctx, data) -> [problems] (optional)
        editable: true                                     # approver may submit a YAML / JSON replacement
        on_edit: proposals.accept_review_edit              # code/<module>.<fn>(ctx, data): carry the edit downstream
"""
import hashlib
import importlib
import json

import jsonschema
import yaml

CERTIFICATION = "certification"  # built-in gate opened by the certify node when a goal has no sign-off gate


class GateInputError(Exception):
    def __init__(self, gate, problems):
        self.gate, self.problems = gate, problems
        super().__init__(f"gate {gate}: {len(problems)} validation problem(s):\n  - " + "\n  - ".join(problems[:25]))


def specs(gate_spec) -> dict:
    items = gate_spec.get("items") or {}
    return items if isinstance(items, dict) else {i: {} for i in items}


def digest(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _func(goal, ref):
    module, fn = ref.rsplit(".", 1)
    return getattr(importlib.import_module(f"{goal.package}.code.{module}"), fn)


def _schema(goal, ref):
    return json.loads((goal.dir / ref).read_text())


def lint(goal) -> list[str]:
    """Static rules for `maya validate`: every gate item has a readable, valid schema; checks and hooks import."""
    out = []
    for name, spec in (goal.graph().get("nodes") or {}).items():
        if spec.get("type") != "gate":
            continue
        if not isinstance(spec.get("items"), dict) or not spec["items"]:
            out.append(f"gate {name}: items must map each item to its validator (schema, optional check)")
            continue
        for item, s in spec["items"].items():
            where = f"gate {name} item {item}"
            if not (s or {}).get("schema"):
                out.append(f"{where}: no schema")
                continue
            try:
                jsonschema.Draft202012Validator.check_schema(_schema(goal, s["schema"]))
            except Exception as e:
                out.append(f"{where}: schema {s['schema']}: {str(e).splitlines()[0]}")
            for key in ("check", "on_edit"):
                if s.get(key):
                    try:
                        _func(goal, s[key])
                    except Exception as e:
                        out.append(f"{where}: {key} {s[key]} does not import: {e}")
            if s.get("editable") and not s.get("on_edit"):
                out.append(f"{where}: editable items need on_edit (how the edit reaches the nodes after the gate)")
    return out


def validate_item(ctx, name, spec, data) -> list[str]:
    if data is None:
        return [] if spec.get("optional") else [f"{name}: missing"]
    problems = []
    if spec.get("schema"):
        v = jsonschema.Draft202012Validator(_schema(ctx.goal, spec["schema"]))
        problems = [f"{name}: {e.message} (at {'/'.join(map(str, e.absolute_path)) or 'root'})"
                    for e in sorted(v.iter_errors(data), key=lambda e: list(e.absolute_path))[:20]]
    if not problems and spec.get("check"):
        problems = [f"{name}: {p}" for p in _func(ctx.goal, spec["check"])(ctx, data) or []]
    return problems


def gate_spec(ctx, gate) -> dict:
    return (ctx.goal.graph().get("nodes") or {}).get(gate) or {}


def validate(ctx, gate) -> tuple[dict, list[str]]:
    """Current items of a gate and every problem with them."""
    if gate == CERTIFICATION:
        return {}, _certification_problems(ctx)
    items, problems = {}, []
    for name, spec in specs(gate_spec(ctx, gate)).items():
        items[name] = ctx.read_artefact(name)
        problems += validate_item(ctx, name, spec, items[name])
    return items, problems


def unchanged(items, approved: dict) -> list[str]:
    """Items that differ from what the approval covers (edited outside `maya review --edit`)."""
    return [f"{n}: changed since it was validated for approval (expected {approved[n]}, found {digest(d)})"
            for n, d in items.items() if n in approved and digest(d) != approved[n]]


def load_edit(path) -> object:
    """An approver's edit, YAML or JSON (JSON is valid YAML)."""
    with open(path) as f:
        return yaml.safe_load(f)


def accept_edit(ctx, gate, item, data) -> list[str]:
    """Validate an approver's replacement for one item; only when it is valid is it written and carried downstream."""
    spec = specs(gate_spec(ctx, gate)).get(item)
    if spec is None:
        return [f"{item}: not an item of gate {gate}"]
    if not spec.get("editable"):
        return [f"{item}: gate {gate} does not accept edits of this item"]
    problems = validate_item(ctx, item, spec, data)
    if problems:
        return problems
    ctx.write_artefact(item, data)
    _func(ctx.goal, spec["on_edit"])(ctx, data)
    return []


def approval_problems(ctx, gate, digests: dict, edits: dict) -> list[str]:
    """Everything that must hold before an approval is recorded: each item (or the approver's edit of it) validates,
    edits only touch editable items, and items that were not edited are still what was validated when the gate opened."""
    if gate == CERTIFICATION:
        return ([f"{i}: the certification gate takes no edits" for i in edits]) + _certification_problems(ctx)
    item_specs, problems = specs(gate_spec(ctx, gate)), []
    for item in edits:
        if item not in item_specs:
            problems.append(f"{item}: not an item of gate {gate} (items: {', '.join(item_specs)})")
        elif not item_specs[item].get("editable"):
            problems.append(f"{item}: gate {gate} does not accept edits of this item")
    current = {}
    for name, spec in item_specs.items():
        data = edits[name] if name in edits else ctx.read_artefact(name)
        current[name] = data
        problems += validate_item(ctx, name, spec, data)
    problems += unchanged({n: d for n, d in current.items() if n not in edits}, digests)
    return problems


def _certification_problems(ctx):
    checks = ctx.state.latest_checks(ctx.goal.id)
    if not checks:
        return ["no validation results recorded for this goal"]
    if any(c.get("run_id") not in (None, ctx.run_id) for c in checks):
        return ["the latest validation results belong to another run"]
    return [f"mandatory check {c['check_id']} is not passing" for c in checks
            if c["severity"] == "mandatory" and str(c["passed"]).lower() != "true"]
