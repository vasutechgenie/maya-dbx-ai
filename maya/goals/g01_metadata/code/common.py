"""Shared helpers for G1 nodes and checks."""
import re

import yaml

PLACEHOLDERS = re.compile(r"(?i)(\b(todo|tbd|lorem ipsum|placeholder|description here|to be (filled|added|defined))\b|^\s*n/a\s*$)")


def rule_class(ctx, column_name):
    for r in ctx.inputs.get("sensitivity_rules") or []:
        if re.search(r["pattern"], column_name):
            return r["class"]
    return None


def scope(ctx):
    return ctx.read_artefact("scope.json") or []


def classes(ctx):
    """Sensitivity classes in effect: the configured ones a governed tag policy allows (see profile.scope)."""
    return (ctx.read_artefact("policy.json") or {}).get("sensitivity_classes") or ctx.inputs["sensitivity_classes"]


def proposals(ctx):
    return ctx.read_artefact("proposals.json") or []


def business_context(ctx):
    ref = ctx.inputs.get("context")
    if not ref:
        return {}
    return yaml.safe_load((ctx.system.base_dir / ref).read_text()) or {}


def tag_name(ctx, key):
    return (ctx.inputs.get("tag_names") or {}).get(key, {"sensitivity": "sensitivity", "layer": "maya_layer"}[key])


def declared_keys(ctx) -> dict:
    """full_name -> declared primary key. A table that does not resolve inside the foundation keeps its name as
    written, so the declared_keys check reports it instead of silently skipping it."""
    from maya.core import catalog
    return {catalog.resolve(ctx.system, d["table"]) or d["table"]: d["primary_key"]
            for d in ctx.inputs.get("declared_keys") or []}


def keep_existing(ctx) -> bool:
    return not ctx.inputs.get("overwrite_existing", False)


def min_chars(ctx, column=False):
    m = ctx.inputs.get("min_description_chars", 20)
    return max(8, m // 2) if column else m


def existing_comment(ctx, asset, column=None):
    """The description already in Unity Catalog when it is kept: present and not a placeholder / too short."""
    if not keep_existing(ctx):
        return None
    if column is None:
        text, name, m = asset["comment"], asset["full_name"].split(".")[-1], min_chars(ctx)
    else:
        text, name, m = column["comment"], column["name"], min_chars(ctx, column=True)
    return text if text and not weak(text, name, m) else None


def existing_tag(ctx, column):
    return column.get("sensitivity_tag") if keep_existing(ctx) else None


def open_columns(ctx, asset) -> list[dict]:
    """Columns G1 still has to fill (a description in full scope, or a sensitivity class)."""
    return [c for c in asset["columns"] if existing_tag(ctx, c) is None
            or (asset["scope"] == "full" and existing_comment(ctx, asset, c) is None)]


def has_gaps(ctx, asset) -> bool:
    return existing_comment(ctx, asset) is None or bool(open_columns(ctx, asset))


def split(full_name):
    _, schema, name = full_name.split(".")
    return schema, name


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def weak(text, name, min_chars):
    """Why a description is weak, or None."""
    if not text or not text.strip():
        return "missing"
    if len(text.strip()) < min_chars:
        return f"shorter than {min_chars} characters"
    if _norm(text) in (_norm(name), _norm(name.replace("_", " ")) + "s"):
        return "restates the name"
    if PLACEHOLDERS.search(text):
        return "contains a placeholder"
    return None
