"""Goal lifecycle: derive each goal's status, enforce the prerequisite rule, run / resume a goal's harness."""
import json
from datetime import datetime, timezone

from .graph import Paused
from .harness import Harness, RunContext, config_hash
from .spec import Goal, System, load_goals, stable_hash
from .state import State, new_id
from .workspace import Workspace


def truthy(v) -> bool:
    return v is True or str(v).lower() == "true"


def _ts(v):
    if not v:
        return None
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class Engine:
    def __init__(self, system: System, log=print):
        self.system, self.log = system, log
        self.goals = load_goals()
        self.ws = Workspace(system.spec["target"])
        self.state = State(self.ws, system.state_schema, system.name)
        self.bootstrap = None

    def ensure_state(self) -> dict:
        """Bring this project's own state schema to this MAYA's version (once per process)."""
        if self.bootstrap is None:
            from .project import ensure_state
            self.bootstrap = {"state": ensure_state(self.ws, self.state.schema, self.system.name, self.goals)}
        return self.bootstrap["state"]

    def open(self) -> dict:
        """Project start-up: state schema checked / upgraded, dashboard reused or created."""
        from .project import ensure_dashboard
        self.ensure_state()
        if "dashboard" not in self.bootstrap:
            self.bootstrap["dashboard"] = ensure_dashboard(self.ws, self.system, self.system.spec["target"]["warehouse_id"])
        return self.bootstrap

    def ordered(self) -> list[Goal]:
        order, seen = [], set()

        def visit(g):
            if g.id in seen:
                return
            seen.add(g.id)
            for p in g.prerequisites:
                visit(self.goals[p])
            order.append(g)
        for g in sorted(self.goals.values(), key=lambda g: g.id[1:].zfill(3)):
            visit(g)
        return order

    # ------------------------------------------------------------------ status
    def goal_states(self) -> dict:
        """Status per goal: certified | stale | awaiting_* | failed | running | ready | blocked | not_configured."""
        self.ensure_state()
        declared = self.system.spec.get("goals") or {}
        recorded = self.state.statuses()
        out = {}
        for g in self.ordered():
            decl = self.system.self_certified(g.id)
            if decl is not None:
                out[g.id] = self._self_certified(g, decl)
                continue
            try:
                inputs = g.effective_inputs(self.system)
                chash, err = config_hash(g, inputs, self.system), None
            except Exception as e:
                inputs, chash, err = None, None, str(e)
            cert = self.state.latest_certification(g.id)
            rec = recorded.get(g.id) or {}
            missing = [p for p in g.prerequisites if out[p]["status"] not in ("certified",)]
            reasons = []
            if cert:
                if _ts(cert["expires_at"]) < datetime.now(timezone.utc):
                    reasons.append("certification expired")
                if chash and cert["config_hash"] != chash:
                    reasons.append("configuration changed since certification")
                for p in g.prerequisites:
                    pc = out[p].get("certification")
                    if pc and _ts(pc["certified_at"]) > _ts(cert["certified_at"]):
                        reasons.append(f"prerequisite {p} re-certified")
                    elif out[p]["status"] != "certified":
                        reasons.append(f"prerequisite {p} is {out[p]['status']}")
            if cert and inputs is not None and g.spec["spec"].get("drift"):
                reasons += self._drift(g, inputs, cert)
            run_status = rec.get("status")
            if err and g.id not in declared:
                status = "not_configured"
            elif err:
                status = "invalid_config"
            elif cert and not reasons and run_status in (None, "certified"):
                status = "certified"
            elif run_status in ("awaiting_approval", "awaiting_sign_off", "failed", "running"):
                status = run_status
            elif cert and reasons:
                status = "stale"
            elif missing:
                status = "blocked"
            else:
                status = "ready"
            out[g.id] = {"goal": g, "status": status, "inputs": inputs, "config_hash": chash, "config_error": err,
                         "certification": cert, "missing_prerequisites": missing, "stale_reasons": reasons,
                         "detail": rec.get("detail"), "run_id": rec.get("run_id")}
        return out

    def _self_certified(self, g, decl) -> dict:
        """A goal attested as done in maya.yaml (certification.self_certified): recorded once as a certification so
        the goals after it can run; nothing is run or checked for it."""
        base = {"goal": g, "inputs": None, "config_hash": "self-certified", "missing_prerequisites": [],
                "stale_reasons": [], "self_certified": True, "run_id": None}
        if not isinstance(decl, dict) or not str(decl.get("by") or "").strip():
            return {**base, "status": "invalid_config", "certification": None, "detail": None,
                    "config_error": f"certification.self_certified.{g.id} needs 'by' (who attests the goal is done)"}
        until = decl.get("valid_until")
        try:
            expires = datetime.fromisoformat(str(until)).replace(tzinfo=timezone.utc) if until else None
        except ValueError:
            return {**base, "status": "invalid_config", "certification": None, "detail": None,
                    "config_error": f"certification.self_certified.{g.id}.valid_until must be a date (YYYY-MM-DD)"}
        if expires and expires < datetime.now(timezone.utc):
            return {**base, "status": "stale", "certification": self.state.latest_certification(g.id), "detail": None,
                    "config_error": None, "stale_reasons": [f"self-certification expired on {until}"]}
        run_id = f"self-{g.id.lower()}-{stable_hash(decl)[:8]}"
        cert = self.state.latest_certification(g.id)
        if not cert or cert["run_id"] != run_id:
            days = max(1, (expires - datetime.now(timezone.utc)).days) if expires else 3650
            self.state.start_run(run_id, g.id, "self-certified", {"self_certified": decl})
            self.state.certify(run_id, g.id, decl["by"], days, "self-certified", "maya.yaml certification.self_certified", [])
            self.state.end_run(run_id, "certified", {"self_certified": decl})
            self.state.set_status(g.id, "certified", config_hash="self-certified", run_id=run_id,
                                  detail=f"self-certified by {decl['by']}")
            cert = self.state.latest_certification(g.id)
        return {**base, "status": "certified", "certification": cert, "config_error": None, "run_id": run_id,
                "detail": f"self-certified by {decl['by']}" + (f": {decl['note']}" if decl.get("note") else "")}

    def _drift(self, g, inputs, cert) -> list[str]:
        """Goal-declared check (goal.yaml spec.drift) for workspace changes since certification."""
        import importlib
        module, fn = g.spec["spec"]["drift"].rsplit(".", 1)
        run_dir = self.system.base_dir / ".maya" / "runs" / g.id / cert["run_id"]
        ctx = RunContext(self.system, g, self.ws, self.state, cert["run_id"], inputs, run_dir, log=lambda *_: None)
        try:
            return getattr(importlib.import_module(f"{g.package}.code.{module}"), fn)(ctx) or []
        except Exception as e:
            return [f"drift check failed: {e}"]

    def next_goal(self, states=None):
        states = states or self.goal_states()
        for gid, s in states.items():
            if s["status"] in ("ready", "stale", "failed", "awaiting_approval", "awaiting_sign_off"):
                return gid
        return None

    def context(self, goal_id, run_id) -> RunContext:
        """The context of an existing run (used to validate gate items outside the run, e.g. by `maya review`)."""
        g = self.goals[goal_id]
        run_dir = self.system.base_dir / ".maya" / "runs" / g.id / run_id
        return RunContext(self.system, g, self.ws, self.state, run_id, g.effective_inputs(self.system), run_dir,
                          log=self.log)

    # ------------------------------------------------------------------ run
    def run_goal(self, goal_id, resume=False, approve_as_tester=False, force=False) -> dict:
        states = self.goal_states()
        s = states[goal_id]
        g = s["goal"]
        if s["status"] == "invalid_config":
            raise RuntimeError(f"{goal_id}: {s['config_error']}")
        if s["status"] == "not_configured":
            raise RuntimeError(f"{goal_id} is not configured: add goals.{goal_id} to maya.yaml ({s['config_error']})")
        if s.get("self_certified") and s["status"] == "certified":
            self.log(f"{goal_id} is self-certified in maya.yaml ({s['detail']}); remove certification.self_certified."
                     f"{goal_id} to run it with MAYA")
            return {"status": "certified", "run_id": s["run_id"]}
        if s["missing_prerequisites"]:
            raise RuntimeError(f"{goal_id} is blocked: prerequisites not certified: {s['missing_prerequisites']}")
        if s["status"] == "certified" and not force:
            self.log(f"{goal_id} is already certified and current (use --force to re-run)")
            return {"status": "certified", "run_id": s["certification"]["run_id"]}

        if resume or s["status"] in ("awaiting_approval", "awaiting_sign_off"):
            run_id, resumed = s["run_id"], True
        else:
            run_id, resumed = new_id(g.id.lower()), False
        run_dir = self.system.base_dir / ".maya" / "runs" / g.id / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        ctx = RunContext(self.system, g, self.ws, self.state, run_id, s["inputs"], run_dir, log=self.log)
        if not resumed:
            self.state.start_run(run_id, g.id, s["config_hash"], s["inputs"])
        self.state.set_status(g.id, "running", config_hash=s["config_hash"], run_id=run_id)
        self.log(f"{'Resuming' if resumed else 'Running'} {g.id} {g.title}  run={run_id}  model={self.system.model(g.spec['spec']['harness'].get('model'))}")
        try:
            outputs = Harness(ctx, approve_as_tester).run(resume=resumed)
        except Paused as p:
            self.log(f"PAUSED at {p.node}: {p.reason}\n  approve with: maya review --system {self.system.path} --approve")
            return {"status": "paused", "run_id": run_id, "node": p.node, "reason": p.reason}
        except Exception as e:
            self.state.set_status(g.id, "failed", run_id=run_id, detail=str(e)[:500])
            self.state.end_run(run_id, "failed", {"error": str(e)})
            self.log(f"FAILED: {e}")
            return {"status": "failed", "run_id": run_id, "error": str(e)}
        summary = {k: v for k, v in outputs.items()}
        nodes = g.graph()["nodes"]
        has_certify = any(n["type"] == "certify" for n in nodes.values())
        certified = any(nodes[k]["type"] == "certify" for k in outputs)
        final = "certified" if certified else "failed" if has_certify else "completed"
        detail = None
        if final == "failed":
            failed = sorted({c for k, v in outputs.items() if nodes[k]["type"] == "validator" for c in v.get("failed", [])})
            detail = f"not certified: mandatory checks failing {failed}" if failed else "graph ended before certification"
        self.state.end_run(run_id, final, summary)
        self.state.set_status(g.id, final, config_hash=s["config_hash"], run_id=run_id, detail=detail)
        if detail:
            self.log(detail)
        self.log(f"{g.id} {final.upper()}  run={run_id}  artefacts={run_dir}")
        return {"status": final, "run_id": run_id, "run_dir": str(run_dir)}
