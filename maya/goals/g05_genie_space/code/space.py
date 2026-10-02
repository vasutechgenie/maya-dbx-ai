"""G5 load / author / assemble. The customer's rules, questions and benchmarks and the certified semantic model are the
inputs; the bi_author agent adds page guidance and the missing questions; MAYA proves every SQL and assembles the
space definition the business owner approves."""
from maya.core.spec import stable_hash

from . import ledger
from .common import (benchmarks_file, business_context, columns_of, layer, marker, metric_view_fields, questions_file,
                     referenced, semantic_model, sources, sql_problem, table_comments)


def _norm(q):
    return " ".join(q.lower().split()).rstrip("?")


def _page_key(model, key):
    for p in model["pages"]:
        if key in (p["id"], p["name"]) or str(key).lower() == p["name"].lower():
            return p["id"]
    return None


def _customer_questions(model, qfile, errors):
    out = {p["id"]: [] for p in model["pages"]}
    for key, items in (qfile.get("pages") or {}).items():
        pid = _page_key(model, key)
        if not pid:
            errors.append(f"questions file: page {key!r} is not a page of the semantic model "
                          f"({', '.join(p['id'] for p in model['pages'])})")
            continue
        for it in items:
            it = {"question": it} if isinstance(it, str) else dict(it)
            out[pid].append({"question": it["question"].strip(), "sql": (it.get("sql") or "").strip() or None,
                             "critical": bool(it.get("critical"))})
    return out


def load(ctx):
    errors = []
    qfile, e1 = questions_file(ctx)
    bfile, e2 = benchmarks_file(ctx)
    errors += e1 + e2
    model = semantic_model(ctx)
    srcs, e3 = sources(ctx, model)
    errors += e3
    customer = _customer_questions(model, qfile, errors)
    rules = [r.strip() for r in qfile.get("rules") or []]

    benchmarks = [{"question": b["question"].strip(), "sql": b["sql"].strip()} for b in bfile.get("benchmarks") or []]
    need = ctx.inputs.get("min_benchmarks", 10)
    if len(benchmarks) < need:
        errors.append(f"{len(benchmarks)} benchmark questions; at least {need} are needed (goals.G5.min_benchmarks)")
    seen = [_norm(b["question"]) for b in benchmarks]
    errors += [f"benchmark question listed twice: {q!r}" for q in sorted({q for q in seen if seen.count(q) > 1})]
    if not errors:
        for b in benchmarks:
            why = sql_problem(ctx, b["sql"], srcs)
            if why:
                errors.append(f"benchmark {b['question']!r}: expected answer SQL {why}")
        for pid, qs in customer.items():
            for q in qs:
                why = q["sql"] and sql_problem(ctx, q["sql"], srcs)
                if why:
                    errors.append(f"page {pid}: trusted SQL of {q['question']!r} {why}")
    if errors:
        raise RuntimeError("G5 inputs are invalid:\n  - " + "\n  - ".join(errors))

    on_pages = sorted({o for objs in model["objects"].values() for o in objs})
    cols = columns_of(ctx, sorted(set(srcs) | set(on_pages)))
    comments = table_comments(ctx, sorted(set(srcs) | set(on_pages)))
    fields = {fn: metric_view_fields(ctx, fn) for fn in srcs if layer(ctx, fn) == "metric_view"}

    def asset(fn):
        a = {"full_name": fn, "layer": layer(ctx, fn), "description": comments.get(fn)}
        if fn in fields:
            a.update(measures=fields[fn]["measures"], dimensions=fields[fn]["dimensions"])
        else:
            a["columns"] = cols.get(fn, [])
        return a

    minimum = ctx.inputs.get("min_questions_per_page", 5)
    pages, hashes = [], {}
    for p in model["pages"]:
        ctx_page = {"page": p, "assets": [asset(fn) for fn in sorted(model["objects"].get(p["id"], [])) if fn in srcs],
                    "glossary": [g for g in model["glossary"] if g.get("page_id") == p["id"]],
                    "customer_questions": customer[p["id"]],
                    "questions_needed": max(0, minimum - len(customer[p["id"]]))}
        hashes[p["id"]] = stable_hash({"page": ctx_page, "rules": rules, "sources": srcs})
        pages.append(ctx_page)
    certified = ledger.certified_authoring(ctx)
    full = ctx.inputs.get("mode") == "full"
    reused = {pid: a for pid, a in certified.items() if not full and hashes.get(pid) == a.get("hash")}
    todo = [p["page"]["id"] for p in pages if p["page"]["id"] not in reused]
    ctx.write_artefact("context.json", {"rules": rules, "sources": [asset(fn) for fn in srcs], "pages": pages,
                                        "taxonomy": model["taxonomy"], "glossary": model["glossary"],
                                        "benchmarks": benchmarks, "hashes": hashes, "reused": reused,
                                        "business": business_context(ctx)})
    ctx.log(f"     {len(model['pages'])} pages, {len(srcs)} sources, {len(benchmarks)} benchmarks; "
            f"authoring {len(todo)} pages" + (f", reusing {len(reused)} unchanged" if reused else ""))
    return {"author_pages": todo, "reused_pages": sorted(reused), "pages": len(pages), "sources": len(srcs),
            "benchmarks": len(benchmarks)}


def _context(ctx):
    return ctx.read_artefact("context.json") or {}


def author_input(ctx, page_id):
    c = _context(ctx)
    p = next(x for x in c["pages"] if x["page"]["id"] == page_id)
    return {"page": p["page"], "page_assets": p["assets"], "sources": c["sources"], "glossary": p["glossary"],
            "rules": c["rules"], "customer_questions": [q["question"] for q in p["customer_questions"]],
            "questions_needed": p["questions_needed"]}


def _instructions(ctx, c, guidance) -> list[str]:
    biz = c.get("business") or {}
    title = ctx.inputs["title"]
    lines = [f"You answer business questions for {title}." + (f" {biz['business']}" if biz.get("business") else "")]
    if biz.get("conventions"):
        lines += ["Conventions:"] + [f"- {x}" for x in biz["conventions"]]
    if c["rules"]:
        lines += ["Business rules (always follow them):"] + [f"- {r}" for r in c["rules"]]
    mvs = [s for s in c["sources"] if s["layer"] == "metric_view"]
    if mvs:
        lines.append("KPIs: use the metric views. Query a measure with MEASURE(<measure>) and GROUP BY the dimensions "
                     "asked for; do not re-derive a KPI from Gold when a metric view has it.")
        for s in mvs:
            lines.append(f"- {s['full_name']}: {s.get('description') or ''}".rstrip(": "))
            for m in s.get("measures") or []:
                syn = f" Also called: {', '.join(m['synonyms'])}." if m.get("synonyms") else ""
                lines.append(f"  - {m.get('display_name') or m['name']} (measure {m['name']}): {m.get('comment') or ''}{syn}")
            dims = [d.get("display_name") or d["name"] for d in s.get("dimensions") or []]
            if dims:
                lines.append(f"  - dimensions: {', '.join(dims)}")
    other = [s for s in c["sources"] if s["layer"] != "metric_view"]
    if other:
        lines += ["Other sources:"] + [f"- {s['full_name']}: {s.get('description') or ''}".rstrip(": ") for s in other]
    lines.append("Business taxonomy (domain > subdomain > page):")
    srcs = {s["full_name"] for s in c["sources"]}
    for p in c["pages"]:
        on = [a["full_name"] for a in p["assets"] if a["full_name"] in srcs]
        lines.append(f"- {p['page']['path']}: {p['page'].get('description') or ''}"
                     + (f" Sources: {', '.join(on)}." if on else ""))
    if c["glossary"]:
        lines.append("Glossary:")
        for g in c["glossary"]:
            syn = f" (also: {', '.join(g['synonyms'])})" if g.get("synonyms") else ""
            lines.append(f"- {g['term']}: {g.get('definition') or ''}{syn}")
    if guidance:
        lines.append("Page guidance:")
        for p in c["pages"]:
            for s in guidance.get(p["page"]["id"]) or []:
                lines.append(f"- {p['page']['name']}: {s}")
    return lines


def assemble(ctx):
    c = _context(ctx)
    todo = (ctx.outputs.get("load") or {}).get("author_pages") or []
    results = {r["page"]: r for r in ctx.read_artefact("authoring.json") or [] if isinstance(r, dict)}
    authoring, findings = dict(c["reused"]), []
    for pid in todo:
        r = results.get(pid)
        if not r:
            findings.append(f"page {pid}: the bi_author agent returned no result")
            continue
        authoring[pid] = {"hash": c["hashes"][pid], "guidance": r["guidance"], "questions": r["questions"]}
    srcs = [s["full_name"] for s in c["sources"]]
    for pid, a in authoring.items():
        keep = []
        for g in a.get("guidance") or []:
            outside = sorted(referenced(g) - set(srcs))
            if outside:
                findings.append(f"page {pid}: guidance left out, it names objects outside the space ({', '.join(outside)}): {g!r}")
            else:
                keep.append(g)
        a["guidance"] = keep
    seen, samples, trusted = set(), [], []
    pages = []
    for p in c["pages"]:
        pid, count = p["page"]["id"], 0
        own = [dict(q, origin="customer") for q in p["customer_questions"]]
        agent = [dict(q, origin="agent") for q in (authoring.get(pid) or {}).get("questions") or []]
        for q in own + agent:
            key = _norm(q["question"])
            if key in seen:
                if q["origin"] == "agent":
                    findings.append(f"page {pid}: agent question repeats an existing one, left out: {q['question']!r}")
                continue
            seen.add(key)
            samples.append({"question": q["question"], "page": pid, "origin": q["origin"],
                            "critical": bool(q.get("critical"))})
            count += 1
            sql = (q.get("sql") or "").strip()
            if sql:
                why = sql_problem(ctx, sql, srcs) if q["origin"] == "agent" else None
                if why:
                    findings.append(f"page {pid}: agent SQL for {q['question']!r} left out: {why}")
                else:
                    trusted.append({"question": q["question"], "sql": sql, "page": pid, "origin": q["origin"]})
        pages.append({"id": pid, "name": p["page"]["name"], "path": p["page"]["path"], "questions": count})
    guidance = {pid: a.get("guidance") or [] for pid, a in authoring.items()}
    space = {"marker": marker(ctx), "title": ctx.inputs["title"], "description": ctx.inputs.get("description"),
             "parent_path": ctx.inputs.get("parent_path", "MAYA"), "sources": srcs,
             "instructions": _instructions(ctx, c, guidance), "pages": pages, "sample_questions": samples,
             "trusted_sql": trusted, "benchmarks": c["benchmarks"], "access": ctx.inputs.get("access") or [],
             "findings": findings, "authoring": authoring}
    ctx.write_artefact("space.json", space)
    thin = [p["id"] for p in pages if p["questions"] < ctx.inputs.get("min_questions_per_page", 5)]
    ctx.log(f"     space: {len(srcs)} sources, {len(samples)} sample questions, {len(trusted)} trusted SQL, "
            f"{len(c['benchmarks'])} benchmarks" + (f"; {len(findings)} findings" if findings else "")
            + (f"; pages below the minimum: {', '.join(thin)}" if thin else ""))
    return {"sources": len(srcs), "sample_questions": len(samples), "trusted_sql": len(trusted),
            "benchmarks": len(c["benchmarks"]), "findings": len(findings), "thin_pages": thin}
