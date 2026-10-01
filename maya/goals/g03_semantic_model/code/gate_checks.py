"""G3 gate: the semantic model is accepted only when it is complete and consistent with the taxonomy YAML. The
business owner may edit it (move assets, rename or describe agent-built pages); what the YAML declares - domains,
subdomains, declared pages and placements, glossary terms - can only be changed in the YAML."""
import re

from .common import tag_keys
from .taxonomy import resolved_yaml

ID = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
MIN_DESCRIPTION = 20


def _dups(xs):
    return sorted({x for x in xs if xs.count(x) > 1})


def model(ctx, m):
    tax = ctx.read_artefact("taxonomy.json")
    assets = {a["full_name"]: a for a in ctx.read_artefact("assets.json") or []}
    out = []
    if m["tag_keys"] != tag_keys(ctx):
        out.append(f"tag_keys {m['tag_keys']} differ from the configured {tag_keys(ctx)}")
    for kind, want in (("domains", tax["domains"]),
                       ("subdomains", [{k: s[k] for k in ("id", "domain", "name", "description", "owner")}
                                       for s in tax["subdomains"]])):
        if m[kind] != want:
            out.append(f"{kind} differ from the taxonomy YAML; change them there")
    subs = {s["id"]: s for s in m["subdomains"]}
    pages = {p["id"]: p for p in m["pages"]}
    out += [f"page id {i!r} is used twice" for i in _dups([p["id"] for p in m["pages"]])]
    for p in m["pages"]:
        if not ID.match(p["id"]):
            out.append(f"page id {p['id']!r} is not a valid tag value (lower case, digits, underscores)")
        s = subs.get(p["subdomain"])
        if not s:
            out.append(f"page {p['id']}: unknown subdomain {p['subdomain']!r}")
        elif s["domain"] != p["domain"]:
            out.append(f"page {p['id']}: domain {p['domain']!r} is not the domain of subdomain {s['id']}")
        if len((p.get("description") or "").strip()) < MIN_DESCRIPTION:
            out.append(f"page {p['id']}: needs a description of at least {MIN_DESCRIPTION} characters")
    for d in tax["pages"]:
        p = pages.get(d["id"])
        if not p:
            out.append(f"declared page {d['id']} is missing")
        elif p["name"] != d["name"] or p["subdomain"] != d["subdomain"] or (d["description"] and p["description"] != d["description"]):
            out.append(f"declared page {d['id']}: name, subdomain or description differ from the taxonomy YAML")
    placed = [a["asset"] for a in m["assignments"]]
    out += [f"{a} is placed on more than one page" for a in _dups(placed)]
    out += [f"{a} has no page" for a in sorted(set(assets) - set(placed))]
    out += [f"{a} is not an asset in scope" for a in sorted(set(placed) - set(assets))]
    for a in m["assignments"]:
        p = pages.get(a["page"])
        if not p:
            out.append(f"{a['asset']}: unknown page {a['page']!r}")
        elif (a["domain"], a["subdomain"]) != (p["domain"], p["subdomain"]):
            out.append(f"{a['asset']}: domain / subdomain differ from those of page {p['id']}")
    at = {a["asset"]: a["page"] for a in m["assignments"]}
    out += [f"{fn} is declared on page {pid} in the taxonomy YAML" for fn, pid in sorted(tax["pins"].items())
            if at.get(fn) != pid]
    terms = [t["term"].lower() for t in m["glossary"]]
    out += [f"glossary term {t!r} listed twice" for t in _dups(terms)]
    for t in tax["glossary"]:
        g = next((x for x in m["glossary"] if x["term"].lower() == t["term"].lower()), None)
        if not g or g["definition"] != t["definition"]:
            out.append(f"glossary term {t['term']!r} is missing or its definition differs from the taxonomy YAML")
    for t in m["glossary"]:
        out += [f"glossary term {t['term']!r}: link {x['object']} is not an asset in scope"
                for x in t["links"] if x["object"] not in assets]
        if t.get("page") and t["page"] not in pages:
            out.append(f"glossary term {t['term']!r}: unknown page {t['page']!r}")
    return out


def accept_model_edit(ctx, m):
    """Placements and pages the business owner changed are recorded as theirs; the resolved YAML follows the edit."""
    before = ctx.read_artefact("semantic_model.proposed.json") or {}
    was = {a["asset"]: a["page"] for a in before.get("assignments") or []}
    pages_before = {p["id"]: p for p in before.get("pages") or []}
    for a in m["assignments"]:
        if was.get(a["asset"]) != a["page"]:
            a.update(source="steward", confidence=1.0, rationale="placed by the business owner at review")
    for p in m["pages"]:
        if pages_before.get(p["id"]) != p and p["source"] != "user":
            p["source"] = "steward"
    at = {a["asset"]: a["page"] for a in m["assignments"]}
    for t in m["glossary"]:
        t["page"] = next((at[x["object"]] for x in t["links"] if x["object"] in at), None)
    ctx.write_artefact("semantic_model.json", m)
    ctx.artefact("taxonomy.resolved.yaml").write_text(resolved_yaml(m))
