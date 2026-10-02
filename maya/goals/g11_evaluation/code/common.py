"""Shared helpers for G11: the evaluation dataset (questions with truth SQL on the governed metric views), what the
evaluation runs against (the certified G10 endpoint and the G5 Genie space) and the results tables."""
import json
import re
from pathlib import Path

import yaml

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
JOB_KEY = "maya_g11_evaluation"
TABLES = {
    "eval_dataset": "dataset_version STRING, question_id STRING, question STRING, sql STRING, targets STRING, tags STRING",
    "eval_results": "run_id STRING, run_at TIMESTAMP, dataset_version STRING, agent_version STRING, trigger STRING, "
                    "target STRING, question_id STRING, question STRING, expected STRING, answer STRING, correct BOOLEAN, "
                    "reason STRING, error STRING, detail STRING, seconds DOUBLE",
    "eval_runs": "run_id STRING, run_at TIMESTAMP, dataset_version STRING, agent_version STRING, trigger STRING, "
                 "target STRING, questions INT, correct INT, pass_rate DOUBLE, threshold DOUBLE, passed BOOLEAN"}
COMMENTS = {
    "eval_dataset": "Evaluation questions of the data product with the SQL that gives the right answer (MAYA G11)",
    "eval_results": "Every evaluated answer of the agents and the Genie space, judged against the truth SQL (MAYA G11)",
    "eval_runs": "Pass rate per evaluation run and target (MAYA G11)"}


def schema_name(ctx_or_system, name) -> str:
    system = getattr(ctx_or_system, "system", ctx_or_system)
    return name if "." in name else f"{system.catalogs[0]}.{name}"


def table(spec, name) -> str:
    return f"{spec['schema']}.{name}"


def q(full_name) -> str:
    from maya.core.workspace import ident
    return ident(*full_name.split("."))


def slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]


def questions_file(ctx) -> tuple[dict, list[str]]:
    ref = ctx.inputs["questions"]
    path = ctx.system.base_dir / ref
    if not path.exists():
        return {}, [f"questions file {ref} not found"]
    data = yaml.safe_load(path.read_text()) or {}
    import jsonschema
    schema = ctx.goal.inputs_schema()
    errors = sorted(jsonschema.Draft202012Validator({"$ref": "#/$defs/questions_file", "$defs": schema["$defs"]})
                    .iter_errors(data), key=lambda e: list(e.path))
    out = [f"{ref}: {e.message} at {'/'.join(map(str, e.path)) or 'root'}" for e in errors[:8]]
    if out:
        return {}, out
    seen = {}
    for i, x in enumerate(data["questions"]):
        x["id"] = x.get("id") or slug(x["question"])
        if x["id"] in seen:
            out.append(f"{ref}: question '{x['question'][:50]}' is declared twice (id {x['id']})")
        seen[x["id"]] = i
    return data, out


def metric_views(ctx) -> list[dict]:
    """The G2 metric views with their dimensions, measures and source tables."""
    g2 = ctx.system.goal_settings("G2") or {}
    path = ctx.system.base_dir / g2.get("definitions", "")
    if not g2.get("definitions") or not path.exists():
        return []
    schema = schema_name(ctx, g2["schema"])
    cat = ctx.system.catalogs[0]
    full = lambda n: n if n.count(".") == 2 else f"{cat}.{n}"
    out = []
    for v in (yaml.safe_load(path.read_text()) or {}).get("metric_views") or []:
        sources = [full(v["source"])] + [full(j["source"]) for j in v.get("joins") or [] if j.get("source")]
        out.append({"name": f"{schema}.{v['name']}", "comment": v.get("comment"), "sources": sources,
                    "dimensions": [d["name"] for d in v.get("dimensions") or []],
                    "measures": [m["name"] for m in v.get("measures") or []]})
    return out


def certified_agents(ctx) -> dict | None:
    from maya.goals.g10_agents.code.common import _certified
    return _certified(ctx, "G10", "agents_ledger")


def plan(ctx) -> dict:
    return ctx.read_artefact("eval_plan.json") or {}


def dumps(x) -> str:
    return json.dumps(x, sort_keys=True, default=str)
