"""Goal harness: runs one goal's graph.yaml with the five node types (code, agent, gate, validator, certify)."""
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import agents, gates
from .graph import GraphRunner, Paused
from .spec import Goal, System, stable_hash
from .state import State, now
from .workspace import Workspace


@dataclass
class RunContext:
    system: System
    goal: Goal
    ws: Workspace
    state: State
    run_id: str
    inputs: dict
    run_dir: Path
    outputs: dict = field(default_factory=dict)
    log: callable = print

    def artefact(self, name) -> Path:
        return self.run_dir / "artefacts" / name

    def write_artefact(self, name, data) -> Path:
        p = self.artefact(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=1, default=str))
        return p

    def read_artefact(self, name):
        p = self.artefact(name)
        return json.loads(p.read_text()) if p.exists() else None

    def prerequisite_summary(self, goal_id) -> dict:
        run = self.state.latest_certification(goal_id)
        if not run:
            return {}
        r = self.state.ws.sql(f"SELECT summary_json FROM {self.state.t('goal_runs')} WHERE run_id = '{run['run_id']}'")
        return json.loads(r[0]["summary_json"] or "{}") if r else {}


def _resolve(path: str, outputs: dict):
    cur = outputs
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return []
    return cur


class Harness:
    def __init__(self, ctx: RunContext, approve_as_tester=False):
        self.ctx, self.approve_as_tester = ctx, approve_as_tester
        self.graph = ctx.goal.graph()
        limits = ctx.goal.spec["spec"]["harness"].get("limits") or {}
        self.runner = GraphRunner(self.graph, self.execute, {"limits": limits}, ctx.run_dir / "checkpoint.json", ctx.log)

    def run(self, resume=False):
        return self.runner.run(resume=resume)

    # ------------------------------------------------------------------ dispatch
    def execute(self, name, spec, outputs, attempt):
        self.ctx.outputs = outputs
        started = now()
        kind = spec["type"]
        self.ctx.log(f"  [{kind}] {name}" + (f" (attempt {attempt})" if attempt > 1 else ""))
        try:
            out = getattr(self, f"_{kind}")(name, spec, attempt)
        except Paused:
            self.ctx.state.node(self.ctx.run_id, self.ctx.goal.id, name, kind, "paused", attempt, started, {})
            raise
        except Exception as e:
            self.ctx.state.node(self.ctx.run_id, self.ctx.goal.id, name, kind, "failed", attempt, started, {"error": str(e)})
            raise
        self.ctx.state.node(self.ctx.run_id, self.ctx.goal.id, name, kind, "done", attempt, started, out)
        return out

    def _func(self, ref):
        module, fn = ref.rsplit(".", 1)
        return getattr(importlib.import_module(f"{self.ctx.goal.package}.code.{module}"), fn)

    # ------------------------------------------------------------------ node types
    def _code(self, name, spec, attempt):
        return self._func(spec["run"])(self.ctx, **(spec.get("with") or {})) or {}

    def _agent(self, name, spec, attempt):
        items = _resolve(spec["for_each"], self.ctx.outputs) if spec.get("for_each") else [None]
        build_input = self._func(spec["input"]) if spec.get("input") else (lambda ctx, item: item)
        schema = json.loads((self.ctx.goal.dir / spec["schema"]).read_text()) if spec.get("schema") else None
        model = self.ctx.system.model(self.ctx.inputs.get("model") or self.ctx.goal.spec["spec"]["harness"].get("model"))
        jobs = [(i, ti) for i, ti in ((i, build_input(self.ctx, item)) for i, item in enumerate(items)) if ti is not None]

        def one(job):
            i, task_input = job
            res = agents.run_agent(self.ctx, agent=spec["agent"], task=spec.get("task", name), task_input=task_input,
                                   schema=schema, model=model, label=f"{name}-{i}")
            self.ctx.log(f"     agent {res['label']}: {res['seconds']}s")
            return res

        with ThreadPoolExecutor(max_workers=max(1, int(spec.get("concurrency", 1)))) as pool:
            done = list(pool.map(one, jobs))
        results = [r["result"] for r in done]
        sessions = [{k: r[k] for k in ("label", "agent", "model", "seconds", "session")} for r in done]
        out_name = spec.get("output", f"{name}.json")
        self.ctx.write_artefact(out_name, results)
        return {"artefact": out_name, "count": len(results), "sessions": sessions}

    def _gate(self, name, spec, attempt):
        st, ctx = self.ctx.state, self.ctx
        appr = st.approval(ctx.run_id, name)
        if appr and appr["status"] == "rejected":
            raise RuntimeError(f"gate {name} rejected by {appr['decided_by']}: {appr['note']}")
        items, problems = gates.validate(ctx, name)
        if spec.get("skip_when_empty") and not any(items.values()):
            return {"skipped": "nothing to review"}
        if appr and appr["status"] == "approved":
            problems += gates.unchanged(items, json.loads(appr["items_json"] or "{}").get("digests") or {})
            if problems:
                raise gates.GateInputError(name, problems)
            return {"approved_by": appr["decided_by"], "note": appr["note"],
                    "digests": {n: gates.digest(d) for n, d in items.items()}}
        if problems:
            raise gates.GateInputError(name, problems)
        approver = ctx.system.approver(spec["approver"]) or spec["approver"]
        if not appr:
            aid = st.request_approval(ctx.run_id, ctx.goal.id, name, approver,
                                      {"items": list(items), "summary": _summarise(items),
                                       "digests": {n: gates.digest(d) for n, d in items.items()}})
        else:
            aid = appr["approval_id"]
        if self.approve_as_tester or ctx.system.auto_approve():
            by = ctx.ws.user if self.approve_as_tester else f"auto ({approver})"
            note = ("approved in tester mode (--approve-as-tester)" if self.approve_as_tester
                    else "auto-approved: every gate item validated (certification.approvals: auto)")
            st.decide(aid, "approved", by, note)
            return {"approved_by": by, "note": note, "digests": {n: gates.digest(d) for n, d in items.items()}}
        st.set_status(ctx.goal.id, "awaiting_approval", run_id=ctx.run_id, detail=f"{name}: {approver}")
        raise Paused(name, f"waiting for {approver} to approve {name} ({aid})")

    def _validator(self, name, spec, attempt):
        from .validation import run_checks
        results = run_checks(self.ctx)
        self.ctx.state.checks(self.ctx.run_id, self.ctx.goal.id, results)
        mandatory = [r for r in results if r["severity"] == "mandatory"]
        failed = [r for r in mandatory if not r["passed"]]
        for r in results:
            self.ctx.log(f"     {'PASS' if r['passed'] else 'FAIL'} {r['id']}: observed {r['observed']} (expected {r['expected']})")
        return {"mandatory_pass": not failed, "mandatory_fail": bool(failed), "failed": [r["id"] for r in failed],
                "failed_items": [i for r in failed for i in (r.get("evidence") or {}).get("items", [])][:200],
                "passed": sum(r["passed"] for r in results), "total": len(results)}

    def _certify(self, name, spec, attempt):
        from .certify import certify
        return certify(self.ctx, self.approve_as_tester)


def _summarise(items):
    return {k: (len(v) if isinstance(v, list) else "present" if v else "missing") for k, v in items.items()}


def config_hash(goal: Goal, inputs: dict, system: System):
    return stable_hash({"goal": goal.spec, "inputs": inputs, "foundation": system.layers})


def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
