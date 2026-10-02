"""Shared helpers for G5: the certified semantic model it builds on, the space's sources, the space definition in
Genie's format, and the live space read back from the workspace."""
import fnmatch
import json
import re
import uuid

import yaml

from maya.core import catalog
from maya.core.workspace import SqlError, ident, lit

GENIE_VERSION = 2
_NS = uuid.UUID("6f1c2b1e-5a0e-4d55-9a7e-3d0f6b5c9e21")


def slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def marker(ctx) -> str:
    """Written into the space description: how the bundle and MAYA find the space in every environment."""
    return f"maya:{slug(ctx.system.name)}:G5"


def _schema_of(ctx, goal_id):
    name = (ctx.system.goal_settings(goal_id) or {}).get("schema")
    if not name:
        return None
    parts = name.split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else (ctx.system.catalogs[0], parts[0])


def semantic_schema(ctx):
    return _schema_of(ctx, "G3")


def metric_view_schema(ctx):
    return _schema_of(ctx, "G2")


def layer(ctx, full_name) -> str | None:
    c, s, _ = full_name.split(".")
    if (c, s) == metric_view_schema(ctx):
        return "metric_view"
    return catalog.layer_of(ctx.system, c, s)


# ---------------------------------------------------------------- the certified semantic model (G3, in Unity Catalog)
def semantic_model(ctx) -> dict:
    """{pages: [...], objects: page id -> [full names], glossary: [...]} from G3's registry tables."""
    c, s = semantic_schema(ctx) or (None, None)
    if not c:
        raise RuntimeError("G5 needs G3's schema (goals.G3.schema in maya.yaml)")
    nodes = ctx.ws.sql(f"SELECT node_id, node_type, parent_id, domain_id, subdomain_id, name, description, owner, path "
                       f"FROM {ident(c, s, 'ontology_nodes')}")
    by_id = {n["node_id"]: n for n in nodes}
    pages = [{"id": n["node_id"], "name": n["name"], "description": n["description"], "path": n["path"],
              "domain": (by_id.get(n["domain_id"]) or {}).get("name"),
              "subdomain": (by_id.get(n["subdomain_id"]) or {}).get("name")}
             for n in nodes if n["node_type"] == "page"]
    objects = {}
    for r in ctx.ws.sql(f"SELECT page_id, object_full_name FROM {ident(c, s, 'ontology_page_objects')}"):
        objects.setdefault(r["page_id"], []).append(r["object_full_name"])
    glossary = ctx.ws.sql(f"SELECT term, first(definition) AS definition, first(synonyms) AS synonyms, "
                          f"first(page_id) AS page_id FROM {ident(c, s, 'business_glossary')} GROUP BY term ORDER BY term")
    for g in glossary:
        if isinstance(g.get("synonyms"), str):
            try:
                g["synonyms"] = json.loads(g["synonyms"])
            except ValueError:
                g["synonyms"] = []
    domains = sorted({(n["path"], n["name"], n["description"]) for n in nodes if n["node_type"] in ("domain", "subdomain")})
    return {"pages": sorted(pages, key=lambda p: p["path"]), "objects": objects, "glossary": glossary,
            "taxonomy": [{"path": p, "name": n, "description": d} for p, n, d in domains]}


def metric_view_fields(ctx, full_name) -> dict:
    """Measures and dimensions of a metric view as stored in Unity Catalog (display names, comments, synonyms)."""
    from maya.goals.g02_metric_views.code.common import read_back
    b = read_back(ctx, full_name) or {}
    keep = ("name", "display_name", "comment", "synonyms", "format")
    return {"comment": b.get("comment"),
            "measures": [{k: f[k] for k in keep if f.get(k)} for f in b.get("measures") or []],
            "dimensions": [{k: f[k] for k in keep if f.get(k)} for f in b.get("dimensions") or []]}


def columns_of(ctx, full_names) -> dict:
    out = {fn: [] for fn in full_names}
    groups = {}
    for fn in full_names:
        c, s, t = fn.split(".")
        groups.setdefault((c, s), set()).add(t)
    for (c, s), names in groups.items():
        rows = ctx.ws.sql(f"SELECT table_name, column_name, data_type, comment FROM {ident(c)}.information_schema.columns "
                          f"WHERE table_schema = {lit(s)} AND table_name IN ({', '.join(lit(n) for n in sorted(names))}) "
                          "ORDER BY table_name, ordinal_position")
        for r in rows:
            out[f"{c}.{s}.{r['table_name']}"].append({"name": r["column_name"], "type": r["data_type"],
                                                      "comment": r.get("comment")})
    return out


def table_comments(ctx, full_names) -> dict:
    out = {}
    groups = {}
    for fn in full_names:
        c, s, t = fn.split(".")
        groups.setdefault((c, s), set()).add(t)
    for (c, s), names in groups.items():
        for r in ctx.ws.sql(f"SELECT table_name, comment FROM {ident(c)}.information_schema.tables "
                            f"WHERE table_schema = {lit(s)} AND table_name IN ({', '.join(lit(n) for n in sorted(names))})"):
            out[f"{c}.{s}.{r['table_name']}"] = r.get("comment")
    return out


# ---------------------------------------------------------------- customer inputs
def _validate(ctx, data, ref, where):
    import jsonschema
    schema = ctx.goal.inputs_schema()
    errors = sorted(jsonschema.Draft202012Validator({"$ref": f"#/$defs/{ref}", "$defs": schema["$defs"]}).iter_errors(data),
                    key=lambda e: list(e.path))
    return [f"{where}: {e.message} at {'/'.join(map(str, e.path)) or 'root'}" for e in errors[:8]]


def questions_file(ctx) -> tuple[dict, list[str]]:
    path = ctx.system.base_dir / ctx.inputs["questions"]
    if not path.exists():
        return {}, [f"questions file {ctx.inputs['questions']} not found"]
    data = yaml.safe_load(path.read_text()) or {}
    return data, _validate(ctx, data, "questions_file", ctx.inputs["questions"])


def benchmarks_file(ctx) -> tuple[dict, list[str]]:
    path = ctx.system.base_dir / ctx.inputs["benchmarks"]
    if not path.exists():
        return {}, [f"benchmarks file {ctx.inputs['benchmarks']} not found"]
    data = yaml.safe_load(path.read_text()) or {}
    return data, _validate(ctx, data, "benchmarks_file", ctx.inputs["benchmarks"])


def business_context(ctx) -> dict:
    ref = (ctx.system.goal_settings("G1") or {}).get("context")
    p = ctx.system.base_dir / ref if ref else None
    return (yaml.safe_load(p.read_text()) or {}) if p and p.exists() else {}


# ---------------------------------------------------------------- sources
def _excluded(ctx, fn):
    return any(fnmatch.fnmatch(fn, p) or fnmatch.fnmatch(fn.split(".", 1)[1], p) for p in ctx.inputs.get("exclude") or [])


def sources(ctx, model) -> tuple[list[str], list[str]]:
    """(full names, errors). Declared sources are resolved; the default is every metric view and Gold asset the
    semantic model places on a page."""
    errors = []
    declared = ctx.inputs.get("sources")
    if declared:
        out = []
        known = sorted({o for objs in model["objects"].values() for o in objs})
        for name in declared:
            parts = name.split(".")
            hits = [k for k in known if k == name or (len(parts) == 2 and k.split(".", 1)[1] == name)]
            fn = hits[0] if len(hits) == 1 else catalog.resolve(ctx.system, name)
            if not fn:
                errors.append(f"source {name!r} is not an asset of the semantic model or the foundation")
                continue
            out.append(fn)
    else:
        out = [o for objs in model["objects"].values() for o in objs if layer(ctx, o) in ("gold", "metric_view")]
    out = sorted({fn for fn in out if not _excluded(ctx, fn)})
    for fn in out:
        lay = layer(ctx, fn)
        if lay == "bronze" or (lay == "silver" and not ctx.inputs.get("allow_silver")) or lay is None:
            errors.append(f"source {fn} is {lay or 'outside the foundation'}; Genie reads metric views and Gold"
                          + (" (set allow_silver to use Silver)" if lay == "silver" else ""))
    if not out:
        errors.append("the space has no sources: G3 places no metric view or Gold asset on a page")
    if len(out) > 30:
        errors.append(f"{len(out)} sources; a Genie space takes at most 30 (use goals.G5.sources or exclude)")
    return out, errors


_NAME = re.compile(r"`?([A-Za-z_][\w-]*)`?\s*\.\s*`?([A-Za-z_][\w-]*)`?\s*\.\s*`?([A-Za-z_][\w-]*)`?")


def referenced(sql) -> set[str]:
    """Three-part names a SQL statement reads (string literals removed first)."""
    text = re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql)
    return {".".join(m.groups()) for m in _NAME.finditer(text)}


def sql_problem(ctx, sql, allowed) -> str | None:
    """None when the SQL runs and reads only allowed sources; otherwise the reason."""
    outside = sorted(referenced(sql) - set(allowed))
    if outside:
        return f"reads objects that are not sources of the space: {', '.join(outside)}"
    try:
        ctx.ws.sql(f"SELECT * FROM ({sql.strip().rstrip(';')}) AS maya_check LIMIT 1")
    except SqlError as e:
        return str(e).splitlines()[0][:300]
    return None


# ---------------------------------------------------------------- the space in Genie's format
def _id(*parts) -> str:
    return uuid.uuid5(_NS, "\x1f".join(parts)).hex


def serialized(space) -> dict:
    """The approved space (space.json) as Genie's serialized_space. Ids derive from the content, so an unchanged
    space serializes identically."""
    key = space["marker"]
    text = [{"id": _id(key, "instructions"), "content": [line + "\n" for line in space["instructions"]]}]
    samples = [{"id": _id(key, "q", q["question"]), "question": [q["question"]]} for q in space["sample_questions"]]
    sqls = [{"id": _id(key, "sql", t["question"]), "question": [t["question"]], "sql": [t["sql"]]}
            for t in space["trusted_sql"]]
    bench = [{"id": _id(key, "b", b["question"]), "question": [b["question"]],
              "answer": [{"format": "SQL", "content": [b["sql"]]}]} for b in space["benchmarks"]]
    by_id = lambda xs: sorted(xs, key=lambda x: x["id"])
    return {"version": GENIE_VERSION,
            "config": {"sample_questions": by_id(samples)},
            "data_sources": {"tables": [{"identifier": t} for t in sorted(space["sources"])]},
            "instructions": {"text_instructions": text, "example_question_sqls": by_id(sqls)},
            "benchmarks": {"questions": by_id(bench)}}


def _join(x):
    return "".join(x) if isinstance(x, list) else (x or "")


def _ws(text):
    return " ".join(str(text).split())


def canonical(s: dict) -> dict:
    """Comparable form of a serialized space (ids, ordering and whitespace ignored)."""
    s = s or {}
    ins = s.get("instructions") or {}
    return {
        "sources": sorted(t.get("identifier") for t in (s.get("data_sources") or {}).get("tables") or []),
        "instructions": _ws(" ".join(_join(t.get("content")) for t in ins.get("text_instructions") or [])),
        "sample_questions": sorted(_ws(_join(q.get("question"))) for q in (s.get("config") or {}).get("sample_questions") or []),
        "trusted_sql": sorted((_ws(_join(q.get("question"))), _ws(_join(q.get("sql"))))
                              for q in ins.get("example_question_sqls") or []),
        "benchmarks": sorted((_ws(_join(q.get("question"))),
                              _ws(" ".join(_join(a.get("content")) for a in q.get("answer") or [])))
                             for q in (s.get("benchmarks") or {}).get("questions") or []),
    }


def differences(want: dict, live: dict) -> list[str]:
    a, b = canonical(want), canonical(live)
    out = []
    for k in ("sources", "sample_questions", "trusted_sql", "benchmarks"):
        missing = [x for x in a[k] if x not in b[k]]
        extra = [x for x in b[k] if x not in a[k]]
        out += [f"{k}: missing {_short(x)}" for x in missing[:10]] + [f"{k}: not approved {_short(x)}" for x in extra[:10]]
    if a["instructions"] != b["instructions"]:
        out.append("instructions differ from the approved text")
    return out


def _short(x):
    t = x if isinstance(x, str) else x[0]
    return repr(t[:90])


# ---------------------------------------------------------------- the live space
def find_space(ctx) -> str | None:
    token, m = None, marker(ctx)
    while True:
        r = ctx.ws.client.api_client.do("GET", "/api/2.0/genie/spaces",
                                        query={"page_size": 100, **({"page_token": token} if token else {})})
        for sp in r.get("spaces") or []:
            if m in (sp.get("description") or ""):
                return sp["space_id"]
        token = r.get("next_page_token")
        if not token:
            return None


def live_space(ctx, space_id) -> dict:
    r = ctx.ws.client.api_client.do("GET", f"/api/2.0/genie/spaces/{space_id}", query={"include_serialized_space": "true"})
    r["serialized"] = json.loads(r.get("serialized_space") or "{}")
    return r


def space_permissions(ctx, space_id) -> dict:
    """principal -> set of permission levels held directly on the space."""
    r = ctx.ws.client.api_client.do("GET", f"/api/2.0/permissions/genie/{space_id}")
    out = {}
    for a in r.get("access_control_list") or []:
        who = a.get("group_name") or a.get("service_principal_name") or a.get("user_name")
        out.setdefault(who, set()).update(p["permission_level"] for p in a.get("all_permissions") or [])
    return out


def definition(ctx) -> dict:
    return ctx.read_artefact("space.json") or {}
