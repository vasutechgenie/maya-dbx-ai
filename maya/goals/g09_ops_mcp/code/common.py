"""Shared helpers for G9: the operations (the customer's and MAYA's built-in ones), the job and app keys, and an MCP
client that calls the delivered server as the agent identity (OAuth)."""
import json
import re
from pathlib import Path

import requests
import yaml

from maya.core import identity

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
APP_KEY = "maya_g9_ops_mcp"
STATUS_TOOLS = ("get_run_status", "list_recent_runs")
NOTEBOOK = "# Databricks notebook source"


def slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def job_key(op_name) -> str:
    return f"maya_g9_{op_name}"


def env_name(op_name) -> str:
    return f"MAYA_OPS_JOB_{op_name.upper()}"


def clients(ctx) -> list[str]:
    sp = identity.principal(ctx.system)
    return list(dict.fromkeys([*([sp] if sp else []), *(ctx.inputs.get("clients") or [])]))


def _schema_name(ctx, name) -> str:
    return name if "." in name else f"{ctx.system.catalogs[0]}.{name}"


def builtins(ctx) -> list[dict]:
    """MAYA's own operations, with their fixed configuration."""
    g7 = ctx.system.goal_settings("G7") or {}
    names = ctx.inputs.get("builtins") or (["table_status"] + (["quality_checks"] if g7.get("schema") else []))
    out = []
    if "table_status" in names:
        layers = {k: [f"{s['catalog']}.{s['schema']}" for s in v["sources"]] for k, v in ctx.system.layers.items()}
        cfg = {"layers": layers, "freshness_hours": (ctx.system.goal_settings("G0") or {}).get("freshness_hours") or {},
               "exclude": (ctx.system.spec.get("foundation") or {}).get("exclude") or []}
        out.append({"name": "table_status", "builtin": True, "writes": False, "timeout_minutes": 30,
                    "intent": "Row count, last data change and freshness (against the freshness limit) of every table of "
                              "the data product, per layer. Read only. Use it to answer whether data is up to date.",
                    "parameters": [{"name": "layer", "type": "string", "enum": ["all", *layers], "default": "all",
                                    "description": "Which layer to report"}],
                    "settings": {"config": json.dumps(cfg, sort_keys=True)}})
    if "quality_checks" in names and g7.get("schema"):
        cfg = {"dq_schema": _schema_name(ctx, g7["schema"]), "job_id": "${resources.jobs.maya_g7_quality.id}"}
        out.append({"name": "quality_checks", "builtin": True, "writes": True, "timeout_minutes": 60,
                    "intent": "Data quality results of the data product's rules (G7): rules checked and failing, critical "
                              "failures, the failing rules with their failed rows, and stale tables. validate reports the "
                              "latest check run; run runs every rule now and reports the new run.",
                    "parameters": [{"name": "table", "type": "string", "default": "all",
                                    "description": "A table name (for example orders or maya_silver.orders), or all"}],
                    "settings": {"config": json.dumps(cfg, sort_keys=True)}})
    return out


def operations_file(ctx) -> tuple[list, list[str]]:
    ref = ctx.inputs.get("operations")
    if not ref:
        return [], []
    path = ctx.system.base_dir / ref
    if not path.exists():
        return [], [f"operations file {ref} not found"]
    data = yaml.safe_load(path.read_text()) or {}
    import jsonschema
    schema = ctx.goal.inputs_schema()
    errors = sorted(jsonschema.Draft202012Validator({"$ref": "#/$defs/operations_file", "$defs": schema["$defs"]})
                    .iter_errors(data), key=lambda e: list(e.path))
    out = [f"{ref}: {e.message} at {'/'.join(map(str, e.path)) or 'root'}" for e in errors[:8]]
    ops = [] if out else (data.get("operations") or [])
    for op in ops:
        nb = ctx.system.base_dir / op["notebook"]
        if not nb.exists():
            out.append(f"{ref}: notebook {op['notebook']} of {op['name']} not found")
        elif not nb.read_text().startswith(NOTEBOOK):
            out.append(f"{ref}: {op['notebook']} is not a notebook source file (first line '{NOTEBOOK}')")
        for p in op.get("parameters") or []:
            if "enum" in p and "default" in p and p["default"] not in p["enum"]:
                out.append(f"{ref}: {op['name']}.{p['name']} default is not one of its enum values")
    return ops, out


def operations(ctx) -> tuple[list, list[str]]:
    ops, errors = operations_file(ctx)
    ops = builtins(ctx) + ops
    names = [o["name"] for o in ops]
    errors += [f"operation {n} is declared twice (or clashes with a built-in)" for n in sorted({n for n in names if names.count(n) > 1})]
    return ops, errors


def notebook_source(ctx, op) -> str:
    if op.get("builtin"):
        return (TEMPLATES / "ops" / f"{op['name']}.py").read_text()
    return (ctx.system.base_dir / op["notebook"]).read_text()


def tool_parameters(op) -> list[dict]:
    """Typed parameters of an operation's tool: its own, plus mode for operations that change data."""
    out = [dict(p) for p in op.get("parameters") or []]
    if op.get("writes"):
        out.append({"name": "mode", "type": "string", "enum": ["validate", "run"], "default": "validate",
                    "description": "validate (the default) is a dry run that changes nothing; run makes the change"})
    return out


def plan(ctx) -> dict:
    return ctx.read_artefact("ops_plan.json") or {}


# ---------------------------------------------------------------- the delivered server
def app_name(ctx) -> str | None:
    """The app's deployed name (the bundle target may prefix it)."""
    from maya.core import bundle
    res = bundle.resources(ctx.system, ctx.ws)
    return ((res.get("apps") or {}).get(APP_KEY) or {}).get("name") or plan(ctx).get("app_name")


class MCP:
    """Minimal MCP client (streamable HTTP, JSON-RPC 2.0) for the delivered server."""

    def __init__(self, ctx, url, auth="agent"):
        self.ctx, self.url, self._id, self.auth = ctx, url.rstrip("/") + "/mcp", 0, auth

    def _headers(self):
        h = identity.headers(self.ctx) if self.auth == "agent" else self.ctx.ws.auth_headers()
        return {**h, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def raw(self, method, params=None, timeout=330):
        self._id += 1
        return requests.post(self.url, headers=self._headers(), timeout=timeout, allow_redirects=False,
                             json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})

    def rpc(self, method, params=None, timeout=330):
        r = self.raw(method, params, timeout)
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            body = json.loads([x[5:].strip() for x in r.text.splitlines() if x.startswith("data:")][-1])
        else:
            body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return body["result"]

    def tools(self):
        return self.rpc("tools/list")["tools"]

    def call(self, name, arguments):
        res = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if res.get("isError"):
            raise RuntimeError("".join(c.get("text", "") for c in res.get("content") or []))
        return res.get("structuredContent") or json.loads("".join(c.get("text", "") for c in res.get("content") or []))
