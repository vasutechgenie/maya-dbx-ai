"""Goal harness graph: parse graph.yaml and execute it.

Edges:  "a -> b",  "a -> [b, c]" (parallel branches),  "[a, b] -> c" (join: waits for all),
        "a -> b when: <expr>" (conditional; expr sees the source node's output and the run context).
A node runs when any one of its incoming edge groups is fully satisfied, so loops (validate -> repair -> apply)
re-trigger nodes. Gate nodes may pause the run; the checkpoint lets `maya review` resume it.
"""
import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_EDGE = re.compile(r"^\s*(.+?)\s*->\s*(.+?)(?:\s+when:\s*(.+))?\s*$")
MAX_STEPS = 200


class Paused(Exception):
    def __init__(self, node, reason):
        super().__init__(reason)
        self.node, self.reason = node, reason


class GraphError(Exception):
    pass


def _names(s):
    s = s.strip()
    if s.startswith("["):
        return [x.strip() for x in s.strip("[]").split(",") if x.strip()]
    return [s]


@dataclass
class EdgeGroup:
    sources: list
    target: str
    when: str | None


def parse(graph: dict):
    nodes = graph.get("nodes") or {}
    groups = []
    for raw in graph.get("edges") or []:
        if isinstance(raw, dict):  # YAML reads "a -> b when: x" as {"a -> b when": "x"}
            (k, v), = raw.items()
            line = re.sub(r"\s+when$", "", k) + " when: " + str(v)
        else:
            line = raw
        m = _EDGE.match(line)
        if not m:
            raise GraphError(f"cannot parse edge: {raw}")
        parts = [p.strip() for p in re.split(r"\s+->\s+", line.split(" when:")[0])]
        when = m.group(3)
        for i in range(len(parts) - 1):
            srcs, tgts = _names(parts[i]), _names(parts[i + 1])
            for t in tgts:
                groups.append(EdgeGroup(srcs, t, when if i == len(parts) - 2 else None))
    for g in groups:
        for n in g.sources + [g.target]:
            if n not in nodes:
                raise GraphError(f"edge references unknown node '{n}'")
    return nodes, groups


_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare, ast.Name, ast.Load,
            ast.Constant, ast.Attribute, ast.Subscript, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)


class _Dot(dict):
    __getattr__ = dict.get


def evaluate(expr: str, scope: dict) -> bool:
    tree = ast.parse(expr, mode="eval")
    for n in ast.walk(tree):
        if not isinstance(n, _ALLOWED):
            raise GraphError(f"unsupported expression in when: {expr}")
    wrap = {k: _Dot(v) if isinstance(v, dict) else v for k, v in scope.items()}
    return bool(eval(compile(tree, "<when>", "eval"), {"__builtins__": {}}, wrap))


@dataclass
class Checkpoint:
    queue: list = field(default_factory=list)
    marks: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    attempts: dict = field(default_factory=dict)
    done: list = field(default_factory=list)

    def save(self, path: Path):
        path.write_text(json.dumps(self.__dict__, default=str, indent=1))

    @classmethod
    def load(cls, path: Path):
        return cls(**json.loads(path.read_text())) if path.exists() else None


class GraphRunner:
    """Executes a parsed goal graph. `execute(name, spec, ctx)` runs one node and returns its output dict."""

    def __init__(self, graph: dict, execute, scope_extra: dict, checkpoint_path: Path, log=print):
        self.nodes, self.groups = parse(graph)
        self.execute, self.scope_extra, self.cp_path, self.log = execute, scope_extra, checkpoint_path, log

    def run(self, resume=False) -> dict:
        cp = Checkpoint.load(self.cp_path) if resume else None
        if cp is None:
            cp = Checkpoint()
            targets = {g.target for g in self.groups}
            cp.queue = [n for n in self.nodes if n not in targets]
        steps = 0
        while cp.queue:
            steps += 1
            if steps > MAX_STEPS:
                raise GraphError("graph exceeded the step limit (check loop conditions)")
            name = cp.queue.pop(0)
            cp.attempts[name] = cp.attempts.get(name, 0) + 1
            try:
                out = self.execute(name, self.nodes[name], cp.outputs, cp.attempts[name])
            except Paused:
                cp.queue.insert(0, name)
                cp.attempts[name] -= 1
                cp.save(self.cp_path)
                raise
            except Exception:
                cp.queue.insert(0, name)
                cp.save(self.cp_path)
                raise
            cp.outputs[name] = out or {}
            cp.done.append(name)
            scope = {**self.scope_extra, **(out or {}), "attempts": cp.attempts, "outputs": cp.outputs}
            for i, g in enumerate(self.groups):
                if name not in g.sources:
                    continue
                if g.when and not evaluate(g.when, scope):
                    continue
                key = str(i)
                marks = set(cp.marks.get(key, [])) | {name}
                if marks >= set(g.sources):
                    cp.marks[key] = []
                    if g.target not in cp.queue:
                        cp.queue.append(g.target)
                else:
                    cp.marks[key] = sorted(marks)
            cp.save(self.cp_path)
        return cp.outputs
