"""Load and resolve the system spec (maya.yaml) and goal packages (maya/goals/*/goal.yaml)."""
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml

GOALS_DIR = Path(__file__).resolve().parent.parent / "goals"
_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


class SpecError(Exception):
    pass


def _resolve_env(value):
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in os.environ:
                raise SpecError(f"environment variable {m.group(1)} is not set")
            return os.environ[m.group(1)]
        return _REF.sub(sub, value)
    if isinstance(value, dict):
        return {_resolve_env(k): _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def stable_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


@dataclass
class System:
    path: Path
    raw: dict
    spec: dict = field(init=False)

    def __post_init__(self):
        self.spec = _resolve_env(self.raw)
        for key in ("metadata", "target", "foundation"):
            if key not in self.spec:
                raise SpecError(f"maya.yaml is missing '{key}'")
        if not self.spec["foundation"].get("layers"):
            raise SpecError("foundation.layers must list at least one layer")
        self._layers = _normalize_layers(self.spec["foundation"])

    @property
    def name(self):
        return self.spec["metadata"]["name"]

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    @property
    def layers(self) -> dict:
        """layer -> {sources: [{catalog, schema}], tables: 'all' | [patterns], exclude: [patterns], metadata_scope}"""
        return self._layers

    @property
    def catalogs(self) -> list[str]:
        return sorted({s["catalog"] for cfg in self._layers.values() for s in cfg["sources"]})

    @property
    def state_schema(self):
        return self.spec["target"]["state_schema"]

    def goal_settings(self, goal_id) -> dict:
        return (self.spec.get("goals") or {}).get(goal_id) or {}

    def approver(self, role):
        return ((self.spec.get("certification") or {}).get("approvers") or {}).get(role)

    def auto_approve(self) -> bool:
        """certification.approvals: auto (default) approves a gate as soon as its items validate."""
        return (self.spec.get("certification") or {}).get("approvals", "auto") != "manual"

    def self_certified(self, goal_id) -> dict | None:
        """certification.self_certified.<goal>: the goal is done outside MAYA, attested in maya.yaml."""
        return ((self.spec.get("certification") or {}).get("self_certified") or {}).get(goal_id)

    def model(self, override=None):
        return override or (self.spec.get("ai_gateway") or {}).get("model")

    @property
    def spec_hash(self):
        return stable_hash(self.raw)


def _normalize_layers(foundation) -> dict:
    """Each layer names exactly where its assets live; nothing outside these catalog.schema sources is in scope.
      schema: s | schemas: [s, other_catalog.s2]   catalog: c (else foundation.catalog)
      tables: all (default) | [name or schema.name, globs allowed]   exclude: [patterns] (plus foundation.exclude)"""
    default_catalog, shared_exclude = foundation.get("catalog"), list(foundation.get("exclude") or [])
    known = {"schema", "schemas", "catalog", "tables", "exclude", "metadata_scope"}
    out, owner = {}, {}
    for layer, cfg in foundation["layers"].items():
        cfg = dict(cfg or {})
        where = f"foundation.layers.{layer}"
        if unknown := set(cfg) - known:
            raise SpecError(f"{where}: unknown keys {sorted(unknown)} (allowed: {sorted(known)})")
        schemas = cfg.get("schemas") or ([cfg["schema"]] if cfg.get("schema") else [])
        if isinstance(schemas, str):
            schemas = [schemas]
        if not schemas:
            raise SpecError(f"{where}: set 'schema' or 'schemas' (catalog.schema or schema)")
        sources = []
        for s in schemas:
            parts = str(s).split(".")
            if len(parts) > 2 or not all(parts):
                raise SpecError(f"{where}: {s!r} must be 'schema' or 'catalog.schema'")
            cat, sch = parts if len(parts) == 2 else (cfg.get("catalog") or default_catalog, parts[0])
            if not cat:
                raise SpecError(f"{where}: no catalog for schema {sch!r}; set {where}.catalog or foundation.catalog")
            if (cat, sch) in owner:
                raise SpecError(f"{cat}.{sch} is declared for both layers {owner[(cat, sch)]!r} and {layer!r}")
            owner[(cat, sch)] = layer
            sources.append({"catalog": cat, "schema": sch})
        tables = cfg.get("tables", "all")
        if tables != "all" and not (isinstance(tables, list) and tables and all(isinstance(t, str) for t in tables)):
            raise SpecError(f"{where}.tables must be 'all' or a non-empty list of table names / glob patterns")
        scope = cfg.get("metadata_scope", "full")
        if scope not in ("full", "tables"):
            raise SpecError(f"{where}.metadata_scope must be 'full' or 'tables'")
        out[layer] = {"sources": sources, "tables": tables, "metadata_scope": scope,
                      "exclude": list(cfg.get("exclude") or []) + shared_exclude}
    return out


def load_system(path) -> System:
    p = Path(path).resolve()
    if not p.exists():
        raise SpecError(f"{p} not found")
    raw = yaml.safe_load(p.read_text())
    if raw.get("kind") != "System":
        raise SpecError(f"{p}: kind must be System")
    return System(p, raw)


@dataclass
class Goal:
    dir: Path
    spec: dict

    @property
    def id(self):
        return self.spec["metadata"]["id"]

    @property
    def name(self):
        return self.spec["metadata"]["name"]

    @property
    def title(self):
        return self.spec["metadata"].get("title", self.name)

    @property
    def milestone(self):
        return self.spec["metadata"].get("milestone")

    @property
    def prerequisites(self):
        return self.spec["spec"].get("prerequisites") or []

    @property
    def package(self):
        return f"maya.goals.{self.dir.name}"

    def inputs_schema(self) -> dict:
        ref = self.spec["spec"].get("inputs")
        return json.loads((self.dir / ref).read_text()) if ref else {"type": "object"}

    def effective_inputs(self, system: System) -> dict:
        schema = self.inputs_schema()
        values = {k: v.get("default") for k, v in schema.get("properties", {}).items() if "default" in v}
        values.update(system.goal_settings(self.id))
        try:
            jsonschema.validate(values, schema)
        except jsonschema.ValidationError as e:
            raise SpecError(f"{self.id} inputs: {e.message} (at {'/'.join(map(str, e.path)) or 'root'})")
        return values

    def graph(self) -> dict:
        return yaml.safe_load((self.dir / self.spec["spec"]["harness"]["graph"]).read_text())

    def agent_file(self, name) -> Path:
        return self.dir / "harness" / "agents" / f"{name}.yaml"


def load_goals(goals_dir: Path = GOALS_DIR) -> dict:
    goals = {}
    for f in sorted(goals_dir.glob("*/goal.yaml")):
        spec = yaml.safe_load(f.read_text())
        if spec.get("kind") != "Goal":
            raise SpecError(f"{f}: kind must be Goal")
        g = Goal(f.parent, spec)
        goals[g.id] = g
    for g in goals.values():
        for p in g.prerequisites:
            if p not in goals:
                raise SpecError(f"{g.id}: unknown prerequisite {p}")
    return goals
